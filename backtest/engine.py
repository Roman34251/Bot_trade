"""
BACKTEST ENGINE
================
Симулює торгівлю на 5 роках історичних даних.

Що робить:
  1. Завантажує OHLCV з PostgreSQL
  2. Запускає всі індикатори
  3. Симулює кожну угоду зі справжніми комісіями (0.055% × 2)
  4. Відстежує cooldown між угодами
  5. Виводить детальну статистику

Чому НЕ NautilusTrader на цьому етапі:
  Для першого MVP — pandas backtest простіший і швидший.
  NautilusTrader підключимо коли стратегія покаже результати.

Метрики які рахуємо:
  - Winrate (% прибуткових угод)
  - Avg R:R (середній risk:reward)
  - Sharpe ratio (прибуток відносно ризику)
  - Max Drawdown (максимальне просідання)
  - Total net PnL (чистий прибуток після комісій)
  - Profit factor (сума виграшів / сума програшів)
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger

from indicators.signal_engine import run_all_indicators, generate_signals

# ─── ПАРАМЕТРИ БЕКТЕСТУ ───────────────────────────────────────
BYBIT_TAKER_FEE            = 0.00055
DEPOSIT                    = 500.0
RISK_PER_TRADE_PCT         = 0.01     # 1% = $5
MIN_CANDLES_BETWEEN_TRADES = 3
MAX_TRADES_PER_HOUR        = 4
MAX_TRADES_PER_DAY         = 12
COOLDOWN_AFTER_LOSS_MIN    = 10
MAX_CONSECUTIVE_LOSSES     = 3
COOLDOWN_AFTER_SERIES_MIN  = 60


@dataclass
class Trade:
    """Одна угода в бектесті."""
    idx:        int
    timestamp:  pd.Timestamp
    symbol:     str
    direction:  str           # 'long' або 'short'
    entry:      float
    sl:         float
    tp:         float
    quantity:   float
    score:      int

    exit_price:  float = 0.0
    exit_time:   Optional[pd.Timestamp] = None
    outcome:     str = "open"   # 'win', 'loss', 'open'
    gross_pnl:   float = 0.0
    fee_total:   float = 0.0
    net_pnl:     float = 0.0


@dataclass
class BacktestState:
    """Стан бектесту в кожен момент часу."""
    balance:           float = DEPOSIT
    peak_balance:      float = DEPOSIT
    trades:            list = field(default_factory=list)
    last_trade_time:   Optional[pd.Timestamp] = None
    last_loss_time:    Optional[pd.Timestamp] = None
    pause_until:       Optional[pd.Timestamp] = None
    consecutive_losses: int = 0
    trades_this_hour:  int = 0
    trades_today:      int = 0
    current_hour:      Optional[pd.Timestamp] = None
    current_day:       Optional[pd.Timestamp] = None


class BacktestEngine:
    """
    Основний клас бектесту.

    Використання:
        engine = BacktestEngine()
        results = engine.run(df, symbol="BTC/USDT:USDT")
        engine.print_report(results)
    """

    def run(self, df: pd.DataFrame, symbol: str) -> dict:
        """
        Запускає повний бектест на переданому DataFrame.

        Вхід:  df з OHLCV (індекс = timestamp)
        Вихід: dict з усіма метриками і списком угод
        """
        logger.info(f"🔬 Бектест: {symbol} | {len(df)} свічок")
        logger.info(f"   Від {df.index[0]} до {df.index[-1]}")

        # Крок 1: розраховуємо всі індикатори
        logger.info("📊 Розрахунок індикаторів...")
        df = run_all_indicators(df)

        # Крок 2: генеруємо сигнали
        logger.info("🎯 Генерація сигналів...")
        df = generate_signals(df)

        # Крок 3: симулюємо угоди
        logger.info("💹 Симуляція торгівлі...")
        state = BacktestState()
        self._simulate(df, state, symbol)

        # Крок 4: рахуємо метрики
        results = self._calculate_metrics(state, symbol)
        return results

    def _simulate(
        self, df: pd.DataFrame, state: BacktestState, symbol: str
    ) -> None:
        """
        Проходить по кожній свічці і симулює торгівлю.

        Для кожної свічки:
        1. Перевіряємо чи є відкрита угода (чи спрацював TP/SL)
        2. Перевіряємо cooldown і ліміти
        3. Якщо є сигнал і все OK → відкриваємо нову угоду
        """
        open_trade: Optional[Trade] = None

        for i in range(1, len(df)):
            row  = df.iloc[i]
            prev = df.iloc[i - 1]
            ts   = df.index[i]

            # ─── Оновлення лічильників (година/день) ──────────
            self._update_counters(state, ts)

            # ─── Перевіряємо відкриту угоду ───────────────────
            if open_trade is not None:
                closed = self._check_exit(open_trade, row, state)
                if closed:
                    open_trade = None
                continue  # одна угода за раз

            # ─── Перевірка можливості торгівлі ────────────────
            can, reason = self._can_trade(state, ts)
            if not can:
                continue

            # ─── Відкриття нової угоди ─────────────────────────
            signal = prev.get("signal")   # сигнал попередньої свічки
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
    ) -> Optional[Trade]:
        """Відкриває нову угоду."""
        entry = float(entry_row["open"])  # входимо по ціні відкриття наступної свічки
        sl    = float(signal_row["sl"])
        tp    = float(signal_row["tp"])

        if sl <= 0 or tp <= 0 or entry <= 0:
            return None

        # Розмір позиції: ризикуємо 1% депозиту
        risk_usdt   = state.balance * RISK_PER_TRADE_PCT
        sl_distance = abs(entry - sl)
        if sl_distance < 1e-8:
            return None

        quantity = round(risk_usdt / sl_distance, 6)

        trade = Trade(
            idx       = idx,
            timestamp = entry_row.name,
            symbol    = symbol,
            direction = direction,
            entry     = entry,
            sl        = sl,
            tp        = tp,
            quantity  = quantity,
            score     = int(signal_row.get("signal_score", 1)),
        )

        # Комісія на вхід
        fee_in = entry * quantity * BYBIT_TAKER_FEE
        state.balance -= fee_in
        trade.fee_total += fee_in

        state.last_trade_time = entry_row.name
        state.trades_this_hour += 1
        state.trades_today += 1

        return trade

    def _check_exit(
        self, trade: Trade, row: pd.Series, state: BacktestState
    ) -> bool:
        """
        Перевіряє чи спрацював SL або TP.
        Повертає True якщо угода закрита.
        """
        high  = float(row["high"])
        low   = float(row["low"])
        close = float(row["close"])

        hit_tp = hit_sl = False

        if trade.direction == "long":
            hit_tp = high >= trade.tp
            hit_sl = low  <= trade.sl
        else:  # short
            hit_tp = low  <= trade.tp
            hit_sl = high >= trade.sl

        if not (hit_tp or hit_sl):
            return False

        # Якщо обидва — консервативно вважаємо що спочатку SL
        if hit_tp and hit_sl:
            hit_tp = False

        exit_price = trade.tp if hit_tp else trade.sl
        fee_out    = exit_price * trade.quantity * BYBIT_TAKER_FEE

        # PnL
        if trade.direction == "long":
            gross = (exit_price - trade.entry) * trade.quantity
        else:
            gross = (trade.entry - exit_price) * trade.quantity

        net_pnl = gross - trade.fee_total - fee_out

        trade.exit_price = exit_price
        trade.exit_time  = row.name
        trade.outcome    = "win" if hit_tp else "loss"
        trade.gross_pnl  = round(gross, 4)
        trade.fee_total  = round(trade.fee_total + fee_out, 4)
        trade.net_pnl    = round(net_pnl, 4)

        state.balance += gross - fee_out

        # Оновлення стану після збитку
        if trade.outcome == "loss":
            state.last_loss_time    = row.name
            state.consecutive_losses += 1
            if state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                state.pause_until = row.name + timedelta(
                    minutes=COOLDOWN_AFTER_SERIES_MIN
                )
                logger.warning(
                    f"⏸️  Пауза {COOLDOWN_AFTER_SERIES_MIN} хв "
                    f"після {MAX_CONSECUTIVE_LOSSES} збитків підряд"
                )
        else:
            state.consecutive_losses = 0

        state.peak_balance = max(state.peak_balance, state.balance)
        return True

    def _can_trade(
        self, state: BacktestState, ts: pd.Timestamp
    ) -> tuple[bool, str]:
        """Перевіряє cooldown і ліміти."""
        # Пауза після серії збитків
        if state.pause_until and ts < state.pause_until:
            return False, "pause_after_losses"

        # Cooldown після збитку
        if state.last_loss_time:
            cooldown_end = state.last_loss_time + timedelta(
                minutes=COOLDOWN_AFTER_LOSS_MIN
            )
            if ts < cooldown_end:
                return False, "cooldown_after_loss"

        # Мін. пауза між угодами
        if state.last_trade_time:
            min_gap = state.last_trade_time + timedelta(
                minutes=MIN_CANDLES_BETWEEN_TRADES
            )
            if ts < min_gap:
                return False, "too_soon"

        # Ліміти
        if state.trades_this_hour >= MAX_TRADES_PER_HOUR:
            return False, "hourly_limit"

        if state.trades_today >= MAX_TRADES_PER_DAY:
            return False, "daily_limit"

        return True, "ok"

    def _update_counters(
        self, state: BacktestState, ts: pd.Timestamp
    ) -> None:
        """Скидає лічильники при зміні години/дня."""
        hour = ts.floor("h")
        day  = ts.floor("D")

        if state.current_hour != hour:
            state.current_hour    = hour
            state.trades_this_hour = 0

        if state.current_day != day:
            state.current_day  = day
            state.trades_today = 0

    def _calculate_metrics(
        self, state: BacktestState, symbol: str
    ) -> dict:
        """Розраховує всі метрики бектесту."""
        closed = [t for t in state.trades if t.outcome != "open"]

        if not closed:
            logger.warning("❌ Жодної закритої угоди — перевір параметри")
            return {}

        wins   = [t for t in closed if t.outcome == "win"]
        losses = [t for t in closed if t.outcome == "loss"]

        total_net   = sum(t.net_pnl   for t in closed)
        total_fees  = sum(t.fee_total  for t in closed)
        total_gross = sum(t.gross_pnl  for t in closed)

        winrate = len(wins) / len(closed) * 100

        win_pnl  = sum(t.net_pnl for t in wins)
        loss_pnl = abs(sum(t.net_pnl for t in losses))
        profit_factor = win_pnl / loss_pnl if loss_pnl > 0 else float("inf")

        # Avg R:R
        rr_list = []
        for t in closed:
            sl_dist = abs(t.entry - t.sl)
            tp_dist = abs(t.entry - t.tp)
            if sl_dist > 0:
                rr_list.append(tp_dist / sl_dist)
        avg_rr = np.mean(rr_list) if rr_list else 0

        # Max Drawdown
        balance_curve = self._equity_curve(state.trades, DEPOSIT)
        peak    = balance_curve.cummax()
        drawdown = (balance_curve - peak) / peak * 100
        max_dd  = drawdown.min()

        # Sharpe ratio (спрощений, без risk-free rate)
        daily_returns = balance_curve.resample("D").last().pct_change().dropna()
        sharpe = (
            daily_returns.mean() / daily_returns.std() * np.sqrt(252)
            if daily_returns.std() > 0 else 0
        )

        return {
            "symbol":         symbol,
            "total_trades":   len(closed),
            "wins":           len(wins),
            "losses":         len(losses),
            "winrate_pct":    round(winrate, 1),
            "profit_factor":  round(profit_factor, 2),
            "avg_rr":         round(avg_rr, 2),
            "total_gross":    round(total_gross, 2),
            "total_fees":     round(total_fees, 2),
            "total_net":      round(total_net, 2),
            "final_balance":  round(state.balance, 2),
            "return_pct":     round((state.balance - DEPOSIT) / DEPOSIT * 100, 1),
            "max_drawdown_pct": round(max_dd, 1),
            "sharpe_ratio":   round(sharpe, 2),
            "trades":         closed,
            "equity_curve":   balance_curve,
        }

    def _equity_curve(
        self, trades: list, start_balance: float
    ) -> pd.Series:
        """Будує криву балансу по часу."""
        if not trades:
            return pd.Series([start_balance])

        closed = [t for t in trades if t.exit_time]
        if not closed:
            return pd.Series([start_balance])

        balance = start_balance
        records = []

        for t in sorted(closed, key=lambda x: x.exit_time):
            balance += t.net_pnl
            records.append((t.exit_time, balance))

        ts, vals = zip(*records)
        return pd.Series(vals, index=pd.DatetimeIndex(ts))

    def print_report(self, results: dict) -> None:
        """Виводить читабельний звіт бектесту."""
        if not results:
            return

        # Мета: Sharpe > 1.5, DD < 15%, winrate > 45%
        sharpe_ok = results["sharpe_ratio"] >= 1.5
        dd_ok     = abs(results["max_drawdown_pct"]) <= 15
        wr_ok     = results["winrate_pct"] >= 45

        print("\n" + "=" * 55)
        print(f"  BACKTEST: {results['symbol']}")
        print("=" * 55)
        print(f"  Угод всього:     {results['total_trades']}")
        print(f"  Виграші:         {results['wins']}")
        print(f"  Програші:        {results['losses']}")
        print(f"  Winrate:         {results['winrate_pct']}%  {'✅' if wr_ok else '❌ (потрібно > 45%)'}")
        print(f"  Avg R:R:         {results['avg_rr']}")
        print(f"  Profit Factor:   {results['profit_factor']}")
        print("-" * 55)
        print(f"  Gross PnL:       ${results['total_gross']:+.2f}")
        print(f"  Комісії:         -${abs(results['total_fees']):.2f}")
        print(f"  Net PnL:         ${results['total_net']:+.2f}")
        print(f"  Фінальний баланс: ${results['final_balance']:.2f}")
        print(f"  Дохідність:      {results['return_pct']:+.1f}%")
        print("-" * 55)
        print(f"  Max Drawdown:    {results['max_drawdown_pct']:.1f}%  {'✅' if dd_ok else '❌ (потрібно < 15%)'}")
        print(f"  Sharpe Ratio:    {results['sharpe_ratio']}  {'✅' if sharpe_ok else '❌ (потрібно > 1.5)'}")
        print("=" * 55)

        if sharpe_ok and dd_ok and wr_ok:
            print("  🎉 СТРАТЕГІЯ ПРОЙШЛА ВСІ КРИТЕРІЇ → Demo trading")
        else:
            print("  ⚠️  Потрібна оптимізація параметрів")
        print()
