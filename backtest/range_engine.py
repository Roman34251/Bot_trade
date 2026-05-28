from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import (
    ATR_PERIOD,
    ATR_PERIOD_FAST,
    BYBIT_TAKER_FEE,
    DEPOSIT_USDT,
    MARKET_SLIPPAGE,
    RISK_PER_TRADE_PCT,
    SYMBOL_CONFIG,
    RANGE_LOOKBACK_CANDLES,
    RANGE_MIN_TOUCHES_EACH_SIDE,
    RANGE_TOUCH_ATR_MULT,
    RANGE_MIN_WIDTH_PCT,
    DEVIATION_VOLUME_LOOKBACK,
    DEVIATION_VOLUME_MAX_RATIO,
    MAX_SIGNAL_AGE_MIN,
    STOCH_K,
    STOCH_D,
    STOCH_SMOOTH,
    STOCH_LONG_MAX,
    STOCH_SHORT_MIN,
    USE_STOCH_CROSS,
    STOCH_LONG_CROSS_MAX,
    STOCH_SHORT_CROSS_MIN,
    USE_1M_VOLUME_CONFIRMATION,
    ENTRY_VOLUME_LOOKBACK,
    ENTRY_VOLUME_MIN_RATIO,
    MAX_LEVERAGE,
    MIN_SL_DISTANCE_PCT,
    SL_ZONE_BUFFER_PCT,
    MIN_CANDLES_BETWEEN_TRADES,
    MAX_TRADES_PER_HOUR,
    MAX_TRADES_PER_DAY,
    COOLDOWN_AFTER_LOSS_MIN,
    MAX_CONSECUTIVE_LOSSES,
    COOLDOWN_AFTER_SERIES_MIN,
)


@dataclass
class Trade:
    idx: int
    timestamp: pd.Timestamp
    symbol: str
    direction: str
    entry: float
    sl: float
    tp: float
    quantity: float
    score: int

    exit_price: float = 0.0
    exit_time: Optional[pd.Timestamp] = None
    outcome: str = "open"
    gross_pnl: float = 0.0
    fee_total: float = 0.0
    net_pnl: float = 0.0


@dataclass
class BacktestState:
    balance: float = float(DEPOSIT_USDT)
    peak_balance: float = float(DEPOSIT_USDT)
    trades: list = field(default_factory=list)
    last_trade_time: Optional[pd.Timestamp] = None
    last_loss_time: Optional[pd.Timestamp] = None
    pause_until: Optional[pd.Timestamp] = None
    consecutive_losses: int = 0
    trades_this_hour: int = 0
    trades_today: int = 0
    current_hour: Optional[pd.Timestamp] = None
    current_day: Optional[pd.Timestamp] = None


class RangeDeviationBacktestEngine:
    """
    Clean candle-only range strategy:
    1h range by touches -> 15m fake breakout -> 5m stochastic -> 1m return candle.
    """

    def run(
        self,
        data: dict[str, pd.DataFrame],
        symbol: str,
        start_date=None,
        end_date=None,
    ) -> dict:
        self._validate(data)

        h1 = self._prepare_1h_range(data["1h"])
        m15 = self._prepare_15m_deviation(data["15m"], h1)
        m5 = self._prepare_5m_stoch(data["5m"])
        m1 = self._prepare_1m_entry(data["1m"], h1)

        df = self._merge_latest(m1, h1, "h1", "1h")
        df = self._merge_latest(df, m15, "m15", "15m")
        df = self._merge_latest(df, m5, "m5", "5m")
        df = self._build_signals(df)

        if start_date is not None:
            df = df[df.index >= start_date]
        if end_date is not None:
            df = df[df.index <= end_date]

        self._log_funnel(df)

        state = BacktestState()
        self._simulate(df, state, symbol)
        return self._calculate_metrics(state, symbol)

    def _validate(self, data: dict[str, pd.DataFrame]) -> None:
        for tf in ["1m", "5m", "15m", "1h"]:
            if tf not in data or data[tf].empty:
                raise ValueError(f"Missing or empty dataframe: {tf}")

    def _add_atr(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        tr = pd.concat(
            [
                out["high"] - out["low"],
                (out["high"] - out["close"].shift(1)).abs(),
                (out["low"] - out["close"].shift(1)).abs(),
            ],
            axis=1,
        ).max(axis=1)
        out["atr_slow"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
        out["atr_fast"] = tr.ewm(span=ATR_PERIOD_FAST, adjust=False).mean()
        return out

    def _add_stochastic(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        low_min = out["low"].rolling(STOCH_K).min()
        high_max = out["high"].rolling(STOCH_K).max()

        k_raw = 100 * (out["close"] - low_min) / (high_max - low_min + 1e-10)
        out["stoch_k"] = k_raw.rolling(STOCH_SMOOTH).mean()
        out["stoch_d"] = out["stoch_k"].rolling(STOCH_D).mean()

        k_prev = out["stoch_k"].shift(1)
        d_prev = out["stoch_d"].shift(1)

        cross_up = (out["stoch_k"] > out["stoch_d"]) & (k_prev <= d_prev)
        cross_down = (out["stoch_k"] < out["stoch_d"]) & (k_prev >= d_prev)

        zone_long = out["stoch_k"] < STOCH_LONG_MAX
        zone_short = out["stoch_k"] > STOCH_SHORT_MIN

        cross_long = cross_up & (out["stoch_k"] < STOCH_LONG_CROSS_MAX)
        cross_short = cross_down & (out["stoch_k"] > STOCH_SHORT_CROSS_MIN)

        out["stoch_long_ok"] = zone_long | (USE_STOCH_CROSS and cross_long)
        out["stoch_short_ok"] = zone_short | (USE_STOCH_CROSS and cross_short)
        return out

    def _prepare_1h_range(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self._add_atr(df.copy().sort_index())

        out["range_high"] = out["high"].rolling(RANGE_LOOKBACK_CANDLES).max()
        out["range_low"] = out["low"].rolling(RANGE_LOOKBACK_CANDLES).min()
        out["range_mid"] = (out["range_high"] + out["range_low"]) / 2
        out["range_width_pct"] = (out["range_high"] - out["range_low"]) / out["range_mid"]

        tolerance = out["atr_slow"] * RANGE_TOUCH_ATR_MULT
        high_touch = (out["high"] >= out["range_high"] - tolerance).astype(int)
        low_touch = (out["low"] <= out["range_low"] + tolerance).astype(int)

        out["touches_high"] = high_touch.rolling(RANGE_LOOKBACK_CANDLES).sum()
        out["touches_low"] = low_touch.rolling(RANGE_LOOKBACK_CANDLES).sum()

        out["valid_range"] = (
            (out["touches_high"] >= RANGE_MIN_TOUCHES_EACH_SIDE)
            & (out["touches_low"] >= RANGE_MIN_TOUCHES_EACH_SIDE)
            & (out["range_width_pct"] >= RANGE_MIN_WIDTH_PCT)
        )

        return out[
            [
                "range_high",
                "range_low",
                "range_mid",
                "range_width_pct",
                "touches_high",
                "touches_low",
                "valid_range",
                "atr_fast",
                "atr_slow",
            ]
        ]

    def _prepare_15m_deviation(
        self,
        df: pd.DataFrame,
        h1: pd.DataFrame,
    ) -> pd.DataFrame:
        out = self._add_atr(df.copy().sort_index())
        out = self._merge_latest(out, h1, "h1", "1h")

        avg_volume = out["volume"].rolling(DEVIATION_VOLUME_LOOKBACK).mean()
        volume_ok = out["volume"] <= avg_volume * DEVIATION_VOLUME_MAX_RATIO

        out["dev_long"] = (
            out["h1_valid_range"].fillna(False)
            & volume_ok.fillna(False)
            & (out["low"] < out["h1_range_low"])
            & (out["close"] > out["h1_range_low"])
        )

        out["dev_short"] = (
            out["h1_valid_range"].fillna(False)
            & volume_ok.fillna(False)
            & (out["high"] > out["h1_range_high"])
            & (out["close"] < out["h1_range_high"])
        )

        out["dev_signal"] = None
        out.loc[out["dev_long"], "dev_signal"] = "long"
        out.loc[out["dev_short"], "dev_signal"] = "short"

        long_support_sl = out["h1_range_low"] * (1 - SL_ZONE_BUFFER_PCT)
        short_resistance_sl = out["h1_range_high"] * (1 + SL_ZONE_BUFFER_PCT)

        long_min_distance_sl = out["close"] * (1 - MIN_SL_DISTANCE_PCT)
        short_min_distance_sl = out["close"] * (1 + MIN_SL_DISTANCE_PCT)

        out["sl_long"] = np.minimum(long_support_sl, long_min_distance_sl)
        out["sl_short"] = np.maximum(short_resistance_sl, short_min_distance_sl)

        out["tp_long"] = out["h1_range_mid"]
        out["tp_short"] = out["h1_range_mid"]
        out["signal_time"] = out.index + self._tf_delta("15m")

        return out[
            [
                "dev_signal",
                "dev_long",
                "dev_short",
                "sl_long",
                "sl_short",
                "tp_long",
                "tp_short",
                "signal_time",
            ]
        ]

    def _prepare_5m_stoch(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self._add_stochastic(df.copy().sort_index())
        out["signal_time"] = out.index + self._tf_delta("5m")

        return out[
            [
                "stoch_k",
                "stoch_d",
                "stoch_long_ok",
                "stoch_short_ok",
                "signal_time",
            ]
        ]

    def _prepare_1m_entry(self, df: pd.DataFrame, h1: pd.DataFrame) -> pd.DataFrame:
        out = df.copy().sort_index()
        out = self._merge_latest(out, h1, "h1", "1h")

        avg_volume = out["volume"].rolling(ENTRY_VOLUME_LOOKBACK).mean()
        volume_ok = out["volume"] >= avg_volume * ENTRY_VOLUME_MIN_RATIO

        inside = (
            (out["close"] > out["h1_range_low"])
            & (out["close"] < out["h1_range_high"])
        )

        out["entry_long_ok"] = inside & (out["close"] > out["open"])
        out["entry_short_ok"] = inside & (out["close"] < out["open"])

        if USE_1M_VOLUME_CONFIRMATION:
            out["entry_long_ok"] = out["entry_long_ok"] & volume_ok.fillna(False)
            out["entry_short_ok"] = out["entry_short_ok"] & volume_ok.fillna(False)

        return out[
            [
                "open",
                "high",
                "low",
                "close",
                "volume",
                "entry_long_ok",
                "entry_short_ok",
            ]
        ]

    def _build_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["signal"] = None
        out["signal_score"] = 0
        out["sl"] = np.nan
        out["tp"] = np.nan
        out["rr_net"] = np.nan

        dev_age = (out.index.to_series() - out["m15_signal_time"]).dt.total_seconds() / 60
        stoch_age = (out.index.to_series() - out["m5_signal_time"]).dt.total_seconds() / 60

        recent_dev_long = (
            (out["m15_dev_signal"] == "long")
            & dev_age.ge(0)
            & dev_age.le(MAX_SIGNAL_AGE_MIN)
        )
        recent_dev_short = (
            (out["m15_dev_signal"] == "short")
            & dev_age.ge(0)
            & dev_age.le(MAX_SIGNAL_AGE_MIN)
        )

        recent_stoch_long = (
            out["m5_stoch_long_ok"].fillna(False)
            & stoch_age.ge(0)
            & stoch_age.le(MAX_SIGNAL_AGE_MIN)
        )
        recent_stoch_short = (
            out["m5_stoch_short_ok"].fillna(False)
            & stoch_age.ge(0)
            & stoch_age.le(MAX_SIGNAL_AGE_MIN)
        )

        long_signal = (
            out["h1_valid_range"].fillna(False)
            & recent_dev_long
            & recent_stoch_long
            & out["entry_long_ok"].fillna(False)
        )

        short_signal = (
            out["h1_valid_range"].fillna(False)
            & recent_dev_short
            & recent_stoch_short
            & out["entry_short_ok"].fillna(False)
        )

        out.loc[long_signal, "signal"] = "long"
        out.loc[long_signal, "sl"] = out.loc[long_signal, "m15_sl_long"]
        out.loc[long_signal, "tp"] = out.loc[long_signal, "m15_tp_long"]
        out.loc[long_signal, "signal_score"] = 4

        out.loc[short_signal, "signal"] = "short"
        out.loc[short_signal, "sl"] = out.loc[short_signal, "m15_sl_short"]
        out.loc[short_signal, "tp"] = out.loc[short_signal, "m15_tp_short"]
        out.loc[short_signal, "signal_score"] = 4

        return out

    def _simulate(self, df: pd.DataFrame, state: BacktestState, symbol: str) -> None:
        open_trade: Optional[Trade] = None

        for i in range(1, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]
            ts = df.index[i]

            self._update_counters(state, ts)

            if open_trade is not None:
                if self._check_exit(open_trade, row, state):
                    open_trade = None
                continue

            can_trade, _reason = self._can_trade(state, ts)
            if not can_trade:
                continue

            signal = prev.get("signal")
            if signal not in ("long", "short"):
                continue

            trade = self._open_trade(prev, row, state, symbol, signal, i)
            if trade:
                open_trade = trade
                state.trades.append(trade)

    def _open_trade(
        self,
        signal_row: pd.Series,
        entry_row: pd.Series,
        state: BacktestState,
        symbol: str,
        direction: str,
        idx: int,
    ):
        raw_entry = float(entry_row["open"])
        slippage = float(MARKET_SLIPPAGE.get(symbol, 0.0))

        entry = raw_entry * (1 + slippage) if direction == "long" else raw_entry * (1 - slippage)
        sl = float(signal_row["sl"])
        tp = float(signal_row["tp"])

        if entry <= 0 or sl <= 0 or tp <= 0:
            return None
        if direction == "long" and not (sl < entry < tp):
            return None
        if direction == "short" and not (tp < entry < sl):
            return None

        sl_distance = abs(entry - sl)
        if sl_distance < entry * MIN_SL_DISTANCE_PCT:
            return None

        risk_usdt = state.balance * RISK_PER_TRADE_PCT
        risk_quantity = risk_usdt / sl_distance

        max_notional = state.balance * MAX_LEVERAGE
        max_quantity = max_notional / entry

        quantity = self._round_quantity(symbol, min(risk_quantity, max_quantity))
        if quantity <= 0:
            return None

        trade = Trade(
            idx=idx,
            timestamp=entry_row.name,
            symbol=symbol,
            direction=direction,
            entry=entry,
            sl=sl,
            tp=tp,
            quantity=quantity,
            score=int(signal_row.get("signal_score", 1)),
        )

        fee_in = entry * quantity * BYBIT_TAKER_FEE
        state.balance -= fee_in
        trade.fee_total += fee_in

        state.last_trade_time = entry_row.name
        state.trades_this_hour += 1
        state.trades_today += 1

        return trade

    def _check_exit(self, trade: Trade, row: pd.Series, state: BacktestState) -> bool:
        high = float(row["high"])
        low = float(row["low"])
        slippage = float(MARKET_SLIPPAGE.get(trade.symbol, 0.0))

        if trade.direction == "long":
            hit_tp = high >= trade.tp
            hit_sl = low <= trade.sl
        else:
            hit_tp = low <= trade.tp
            hit_sl = high >= trade.sl

        if not (hit_tp or hit_sl):
            return False

        if hit_tp and hit_sl:
            hit_tp = False

        target_exit = trade.tp if hit_tp else trade.sl

        if trade.direction == "long":
            exit_price = target_exit * (1 - slippage)
            gross = (exit_price - trade.entry) * trade.quantity
        else:
            exit_price = target_exit * (1 + slippage)
            gross = (trade.entry - exit_price) * trade.quantity

        fee_out = exit_price * trade.quantity * BYBIT_TAKER_FEE
        net_pnl = gross - trade.fee_total - fee_out

        trade.exit_price = exit_price
        trade.exit_time = row.name
        trade.outcome = "win" if net_pnl > 0 else "loss"
        trade.gross_pnl = round(gross, 4)
        trade.fee_total = round(trade.fee_total + fee_out, 4)
        trade.net_pnl = round(net_pnl, 4)

        state.balance += gross - fee_out
        self._after_trade_close(trade, row.name, state)
        return True

    def _can_trade(self, state: BacktestState, ts: pd.Timestamp) -> tuple[bool, str]:
        if state.pause_until and ts < state.pause_until:
            return False, "pause_after_losses"

        if state.last_loss_time:
            cooldown_end = state.last_loss_time + timedelta(minutes=COOLDOWN_AFTER_LOSS_MIN)
            if ts < cooldown_end:
                return False, "cooldown_after_loss"

        if state.last_trade_time:
            min_gap = state.last_trade_time + timedelta(minutes=MIN_CANDLES_BETWEEN_TRADES)
            if ts < min_gap:
                return False, "too_soon"

        if state.trades_this_hour >= MAX_TRADES_PER_HOUR:
            return False, "hourly_limit"

        if state.trades_today >= MAX_TRADES_PER_DAY:
            return False, "daily_limit"

        return True, "ok"

    def _update_counters(self, state: BacktestState, ts: pd.Timestamp) -> None:
        hour = ts.floor("h")
        day = ts.floor("D")

        if state.current_hour != hour:
            state.current_hour = hour
            state.trades_this_hour = 0

        if state.current_day != day:
            state.current_day = day
            state.trades_today = 0

    def _after_trade_close(self, trade: Trade, ts: pd.Timestamp, state: BacktestState) -> None:
        if trade.outcome == "loss":
            state.last_loss_time = ts
            state.consecutive_losses += 1
            if state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                state.pause_until = ts + timedelta(minutes=COOLDOWN_AFTER_SERIES_MIN)
        else:
            state.consecutive_losses = 0

        state.peak_balance = max(state.peak_balance, state.balance)

    def _calculate_metrics(self, state: BacktestState, symbol: str) -> dict:
        closed = [t for t in state.trades if t.outcome != "open"]

        if not closed:
            logger.warning("No closed trades. Check filters and parameters.")
            return {}

        wins = [t for t in closed if t.outcome == "win"]
        losses = [t for t in closed if t.outcome == "loss"]

        total_net = sum(t.net_pnl for t in closed)
        total_fees = sum(t.fee_total for t in closed)
        total_gross = sum(t.gross_pnl for t in closed)

        winrate = len(wins) / len(closed) * 100
        win_pnl = sum(t.net_pnl for t in wins)
        loss_pnl = abs(sum(t.net_pnl for t in losses))
        profit_factor = win_pnl / loss_pnl if loss_pnl > 0 else float("inf")

        rr_list = []
        for trade in closed:
            sl_dist = abs(trade.entry - trade.sl)
            tp_dist = abs(trade.entry - trade.tp)
            if sl_dist > 0:
                rr_list.append(tp_dist / sl_dist)

        equity_curve = self._equity_curve(closed)
        peak = equity_curve.cummax()
        drawdown = (equity_curve - peak) / peak * 100

        daily_returns = equity_curve.resample("D").last().pct_change().dropna()
        sharpe = (
            daily_returns.mean() / daily_returns.std() * np.sqrt(252)
            if daily_returns.std() > 0
            else 0
        )

        return {
            "symbol": symbol,
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "winrate_pct": round(winrate, 1),
            "profit_factor": round(profit_factor, 2),
            "avg_rr": round(float(np.mean(rr_list)) if rr_list else 0, 2),
            "total_gross": round(total_gross, 2),
            "total_fees": round(total_fees, 2),
            "total_net": round(total_net, 2),
            "final_balance": round(state.balance, 2),
            "return_pct": round((state.balance - float(DEPOSIT_USDT)) / float(DEPOSIT_USDT) * 100, 1),
            "max_drawdown_pct": round(drawdown.min(), 1),
            "sharpe_ratio": round(sharpe, 2),
            "trades": closed,
            "equity_curve": equity_curve,
        }

    def _equity_curve(self, closed_trades: list) -> pd.Series:
        balance = float(DEPOSIT_USDT)
        records = []

        for trade in sorted(closed_trades, key=lambda x: x.exit_time):
            balance += trade.net_pnl
            records.append((trade.exit_time, balance))

        if not records:
            return pd.Series([balance], index=pd.DatetimeIndex([pd.Timestamp.utcnow()]))

        ts, vals = zip(*records)
        return pd.Series(vals, index=pd.DatetimeIndex(ts))

    def print_report(self, results: dict) -> None:
        if not results:
            return

        print("\n" + "=" * 60)
        print(f"  BACKTEST: {results['symbol']}")
        print("=" * 60)
        print(f"  Trades:          {results['total_trades']}")
        print(f"  Wins:            {results['wins']}")
        print(f"  Losses:          {results['losses']}")
        print(f"  Winrate:         {results['winrate_pct']}%")
        print(f"  Avg R:R:         {results['avg_rr']}")
        print(f"  Profit Factor:   {results['profit_factor']}")
        print("-" * 60)
        print(f"  Gross PnL:       ${results['total_gross']:+.2f}")
        print(f"  Fees:            -${abs(results['total_fees']):.2f}")
        print(f"  Net PnL:         ${results['total_net']:+.2f}")
        print(f"  Final Balance:   ${results['final_balance']:.2f}")
        print(f"  Return:          {results['return_pct']:+.1f}%")
        print("-" * 60)
        print(f"  Max Drawdown:    {results['max_drawdown_pct']:.1f}%")
        print(f"  Sharpe Ratio:    {results['sharpe_ratio']}")
        print("=" * 60)
        print()

    def _round_quantity(self, symbol: str, quantity: float) -> float:
        cfg = SYMBOL_CONFIG.get(symbol, {})
        min_qty = float(cfg.get("min_qty", 0.0))
        qty_step = float(cfg.get("qty_step", 0.000001))

        if qty_step <= 0:
            return round(quantity, 6)

        rounded = np.floor(quantity / qty_step) * qty_step
        if rounded < min_qty:
            return 0.0

        return float(round(rounded, 8))

    def _merge_latest(self, left: pd.DataFrame, right: pd.DataFrame, prefix: str, timeframe: str) -> pd.DataFrame:
        right_ready = right.copy().sort_index()
        right_ready.index = right_ready.index + self._tf_delta(timeframe)
        right_ready = right_ready.add_prefix(f"{prefix}_")

        return pd.merge_asof(
            left.sort_index(),
            right_ready,
            left_index=True,
            right_index=True,
            direction="backward",
        )

    def _tf_delta(self, timeframe: str):
        return {
            "1m": pd.Timedelta(minutes=1),
            "5m": pd.Timedelta(minutes=5),
            "15m": pd.Timedelta(minutes=15),
            "1h": pd.Timedelta(hours=1),
        }[timeframe]

    def _log_funnel(self, df: pd.DataFrame) -> None:
        logger.info(f"1m candles: {len(df):,}")
        logger.info(f"Period: {df.index[0]} -> {df.index[-1]}")
        logger.info(f"Valid 1h range carried to 1m: {int(df['h1_valid_range'].fillna(False).sum()):,}")
        logger.info(f"15m long deviations carried to 1m: {int((df['m15_dev_signal'] == 'long').sum()):,}")
        logger.info(f"15m short deviations carried to 1m: {int((df['m15_dev_signal'] == 'short').sum()):,}")
        logger.info(f"5m stoch long ok carried to 1m: {int(df['m5_stoch_long_ok'].fillna(False).sum()):,}")
        logger.info(f"5m stoch short ok carried to 1m: {int(df['m5_stoch_short_ok'].fillna(False).sum()):,}")
        logger.info(f"1m entry long ok: {int(df['entry_long_ok'].fillna(False).sum()):,}")
        logger.info(f"1m entry short ok: {int(df['entry_short_ok'].fillna(False).sum()):,}")
        logger.info(f"Final range-deviation signals: {int(df['signal'].notna().sum()):,}")