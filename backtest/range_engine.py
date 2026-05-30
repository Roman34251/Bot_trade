"""
БЕКТЕСТ ENGINE — Range Scalping
==================================
Симулює стратегію на історичних OHLCV даних з PostgreSQL.

Що враховується:
  ✅ Taker комісія 0.055% (маркет ордери)
  ✅ Slippage BTC 0.03% / SOL 0.05%
  ✅ Фільтр торгових сесій (Asian + London pre)
  ✅ 1h рейндж + 30m паралельний рейндж з 1h bias
  ✅ Cooldown між угодами
  ✅ Денні ліміти (max 12 угод, max -3% збиток)
  ✅ Розмір позиції за ризиком (1% депозиту = $5)

Запуск:
  python main.py --backtest
  python main.py --backtest --symbol BTC/USDT:USDT
  python main.py --backtest --mode dual  (1h + 30m паралельно)
"""

from __future__ import annotations

import pandas as pd
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger

from config.settings import (
    TRADING_PAIRS, DEPOSIT_USDT, RISK_PER_TRADE_PCT,
    MIN_RISK_REWARD, RANGE_MIN_CANDLES,
    ATR_PERIOD, DEVIATION_ATR_MULT,
)
from indicators.range_detector import detect_range, calculate_atr
from signal.generator import generate_scalp_signal
from signal.dual_tf import generate_dual_tf_signal, get_1h_bias
from signal.session_filter import is_trading_allowed
from signal.calculator import calculate_position, check_daily_limits


# ── Структури даних ────────────────────────────────────────

@dataclass
class Trade:
    """Одна симульована угода."""
    trade_id:    str
    symbol:      str
    direction:   str         # "long" / "short"
    mode:        str         # "1h_15m" / "dual_30m_1h"
    session:     str         # "asian" / "london_pre"
    bias_1h:     str         # "bullish" / "bearish" / "neutral"

    entry_time:  datetime
    entry_price: Decimal
    stop_loss:   Decimal
    take_profit: Decimal
    quantity:    Decimal
    position_value: Decimal

    fee_total:   Decimal
    slippage_cost: Decimal
    risk_usdt:   Decimal
    reward_usdt: Decimal
    rr_ratio:    Decimal

    # Заповнюється при закритті
    exit_time:   Optional[datetime] = None
    exit_price:  Optional[Decimal]  = None
    exit_reason: Optional[str]      = None   # "tp" / "sl" / "timeout"
    pnl_gross:   Optional[Decimal]  = None
    pnl_net:     Optional[Decimal]  = None   # після комісій і slippage


@dataclass
class BacktestState:
    """Поточний стан бектесту."""
    equity:           Decimal
    deposit:          Decimal
    open_trade:       Optional[Trade] = None

    # Статистика дня
    daily_pnl:        Decimal = Decimal("0")
    daily_trades:     int     = 0
    loss_streak:      int     = 0
    last_trade_time:  Optional[datetime] = None

    # Кешовані рейнджі (оновлюються раз на годину)
    cached_range_1h:  Optional[dict] = None
    cached_range_30m: Optional[dict] = None
    range_updated_at: Optional[datetime] = None

    # Всі закриті угоди
    closed_trades:    list = field(default_factory=list)


# ── Головний клас ──────────────────────────────────────────

class BacktestEngine:

    # Cooldown між угодами = 3 хвилини (3 × 1m свічки)
    COOLDOWN_MINUTES = 3
    # Оновлюємо рейндж раз на годину
    RANGE_UPDATE_MINUTES = 60

    def __init__(self, db, deposit: float = 500.0, mode: str = "dual"):
        """
        db:      об'єкт Database
        deposit: початковий депозит в USDT
        mode:    "1h" — тільки 1h рейндж
                 "dual" — 1h + 30m паралельно
        """
        self.db      = db
        self.mode    = mode
        self.state   = BacktestState(
            equity  = Decimal(str(deposit)),
            deposit = Decimal(str(deposit)),
        )
        logger.info(f"🔧 Бектест Engine | depot=${deposit} | mode={mode}")

    def run(self, symbol: str,
            start_date: Optional[datetime] = None,
            end_date:   Optional[datetime] = None) -> dict:
        """
        Запускає бектест для одного символу.

        start_date/end_date: діапазон дат (якщо None — всі дані в БД)
        Повертає dict зі статистикою.
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"▶ Бектест {symbol} | mode={self.mode}")

        # Завантажуємо дані з БД
        logger.info("📊 Завантаження даних з PostgreSQL...")
        df_1h  = self.db.load_ohlcv("bybit", symbol, "1h",
                                     start_date=start_date)
        df_30m = self.db.load_ohlcv("bybit", symbol, "30m",
                                     start_date=start_date)
        df_5m  = self.db.load_ohlcv("bybit", symbol, "5m",
                                     start_date=start_date)
        df_1m  = self.db.load_ohlcv("bybit", symbol, "1m",
                                     start_date=start_date)

        if df_1m.empty:
            logger.error(f"Немає 1m даних для {symbol}")
            return {}

        logger.info(f"   1h: {len(df_1h)} свічок | "
                    f"30m: {len(df_30m)} | "
                    f"5m: {len(df_5m)} | "
                    f"1m: {len(df_1m)}")

        # Перебираємо 1m свічки
        # Починаємо з достатньої кількості свічок для індикаторів
        MIN_WARMUP = 100
        total      = len(df_1m)
        signals_found = 0
        signals_filtered = 0

        for i in range(MIN_WARMUP, total):
            current_time = df_1m.index[i]

            # Перевірка діапазону дат
            if end_date and current_time > end_date:
                break

            # ── Скидаємо денну статистику о 00:00 UTC ──────
            self._check_daily_reset(current_time)

            # ── Перевіряємо відкриту позицію ───────────────
            if self.state.open_trade:
                self._check_exit(df_1m.iloc[:i+1], current_time)
                continue   # поки є позиція — нову не відкриваємо

            # ── Фільтр сесії ───────────────────────────────
            session_result = is_trading_allowed(current_time)
            if not session_result["allowed"]:
                continue

            # ── Cooldown між угодами ────────────────────────
            if self.state.last_trade_time:
                cooldown = (current_time - self.state.last_trade_time
                            ).total_seconds() / 60
                if cooldown < self.COOLDOWN_MINUTES:
                    continue

            # ── Денні ліміти ───────────────────────────────
            limits = check_daily_limits(
                daily_pnl   = self.state.daily_pnl,
                trade_count = self.state.daily_trades,
                loss_streak = self.state.loss_streak,
                deposit     = self.state.deposit,
            )
            if not limits["can_trade"]:
                continue

            # ── Зрізи даних на поточний момент ────────────
            slice_1h  = self._get_slice(df_1h,  current_time, 60)
            slice_30m = self._get_slice(df_30m, current_time, 40)
            slice_5m  = self._get_slice(df_5m,  current_time, 50)
            slice_1m  = df_1m.iloc[max(0, i-99):i+1]

            if len(slice_1h) < 25 or len(slice_1m) < 25:
                continue

            # ── Оновлення кешу рейнджу ─────────────────────
            self._update_range_cache(slice_1h, slice_30m, current_time)

            # ── Генерація сигналу ──────────────────────────
            signal = self._get_signal(
                slice_1h, slice_30m, slice_5m, slice_1m,
                symbol, session_result["session"]
            )

            if signal is None:
                continue

            signals_found += 1

            # ── Калькулятор позиції ────────────────────────
            pos = calculate_position(
                symbol      = symbol,
                deposit     = self.state.equity,
                risk_pct    = Decimal(str(RISK_PER_TRADE_PCT)),
                entry_price = Decimal(str(signal["entry"])),
                stop_loss   = Decimal(str(signal["sl"])),
                take_profit = Decimal(str(signal["tp"])),
            )

            if "error" in pos:
                logger.debug(f"Position error: {pos['error']}")
                signals_filtered += 1
                continue

            if not pos["rr_ok"]:
                logger.debug(f"RR {pos['rr_ratio']} < {MIN_RISK_REWARD}")
                signals_filtered += 1
                continue

            # ── Відкриваємо угоду ──────────────────────────
            trade = self._open_trade(signal, pos, current_time,
                                     session_result["session"])
            self.state.open_trade = trade
            self.state.last_trade_time = current_time

        # Закриваємо відкриту позицію в кінці (timeout)
        if self.state.open_trade:
            self._force_close(df_1m.iloc[-1], df_1m.index[-1])

        # Статистика
        stats = self._calculate_stats(symbol, signals_found, signals_filtered)
        self._print_stats(stats)
        return stats

    # ── Допоміжні методи ──────────────────────────────────

    def _get_signal(self, slice_1h, slice_30m, slice_5m, slice_1m,
                    symbol, session) -> dict | None:
        """Генерує сигнал залежно від режиму."""

        signal = None

        # Режим 1h (Режим А зі скілу)
        if self.mode in ("1h", "dual"):
            signal = generate_scalp_signal(
                df_1h  = slice_1h,
                df_5m  = slice_5m,
                df_1m  = slice_1m,
                symbol = symbol,
                cached_range = self.state.cached_range_1h,
                mode   = "A",
            )

        # Режим 30m з 1h bias (якщо 1h не дав сигналу)
        if signal is None and self.mode == "dual":
            signal = generate_dual_tf_signal(
                df_1h   = slice_1h,
                df_30m  = slice_30m,
                df_5m   = slice_5m,
                df_1m   = slice_1m,
                symbol  = symbol,
                cached_1h_range  = self.state.cached_range_1h,
                cached_30m_range = self.state.cached_range_30m,
            )

        return signal

    def _check_exit(self, df_1m: pd.DataFrame,
                    current_time: datetime) -> None:
        """Перевіряє чи спрацював TP або SL."""
        trade = self.state.open_trade
        if not trade:
            return

        last = df_1m.iloc[-1]
        h    = Decimal(str(last['high']))
        l    = Decimal(str(last['low']))
        c    = Decimal(str(last['close']))

        tp_hit = False
        sl_hit = False

        if trade.direction == "long":
            tp_hit = h >= trade.take_profit
            sl_hit = l <= trade.stop_loss
        else:
            tp_hit = l <= trade.take_profit
            sl_hit = h >= trade.stop_loss

        if tp_hit or sl_hit:
            exit_reason = "tp" if tp_hit else "sl"
            exit_price  = trade.take_profit if tp_hit else trade.stop_loss
            self._close_trade(trade, exit_price, exit_reason, current_time)

    def _open_trade(self, signal: dict, pos: dict,
                    current_time: datetime, session: str) -> Trade:
        """Створює об'єкт угоди."""
        bias = signal.get("bias_1h", get_1h_bias(pd.DataFrame()))

        trade = Trade(
            trade_id      = f"{signal['symbol']}_{current_time.strftime('%Y%m%d%H%M%S')}",
            symbol        = signal["symbol"],
            direction     = signal["direction"],
            mode          = signal.get("mode", "1h_15m"),
            session       = session,
            bias_1h       = signal.get("bias_1h", "unknown"),
            entry_time    = current_time,
            entry_price   = Decimal(str(signal["entry"])),
            stop_loss     = Decimal(str(signal["sl"])),
            take_profit   = Decimal(str(signal["tp"])),
            quantity      = pos["quantity"],
            position_value = pos["position_value"],
            fee_total     = pos["fee_total"],
            slippage_cost = pos["slippage_cost"],
            risk_usdt     = pos["risk_usdt"],
            reward_usdt   = pos["reward_usdt"],
            rr_ratio      = pos["rr_ratio"],
        )

        logger.info(
            f"📈 OPEN {trade.direction.upper()} {trade.symbol} | "
            f"entry={trade.entry_price} qty={trade.quantity} | "
            f"TP={trade.take_profit} SL={trade.stop_loss} | "
            f"risk=${trade.risk_usdt} RR={trade.rr_ratio}"
        )
        return trade

    def _close_trade(self, trade: Trade, exit_price: Decimal,
                     reason: str, current_time: datetime) -> None:
        """Закриває угоду і оновлює статистику."""
        from signal.calculator import SLIPPAGE, BYBIT_TAKER

        slip = SLIPPAGE[trade.symbol]

        # Реальна ціна виходу з slippage
        if trade.direction == "long":
            real_exit = exit_price * (1 - slip)
            pnl_gross = (real_exit - trade.entry_price) * trade.quantity
        else:
            real_exit = exit_price * (1 + slip)
            pnl_gross = (trade.entry_price - real_exit) * trade.quantity

        fee_out = trade.quantity * real_exit * BYBIT_TAKER
        pnl_net = pnl_gross - trade.fee_total - fee_out

        trade.exit_time   = current_time
        trade.exit_price  = exit_price
        trade.exit_reason = reason
        trade.pnl_gross   = pnl_gross
        trade.pnl_net     = pnl_net

        # Оновлення стану
        self.state.equity    += pnl_net
        self.state.daily_pnl += pnl_net
        self.state.daily_trades += 1
        self.state.open_trade = None

        if pnl_net > 0:
            self.state.loss_streak = 0
            icon = "✅"
        else:
            self.state.loss_streak += 1
            icon = "❌"

        self.state.closed_trades.append(trade)

        logger.info(
            f"{icon} CLOSE {trade.direction.upper()} {trade.symbol} | "
            f"{reason.upper()} | pnl=${pnl_net:.2f} | "
            f"equity=${self.state.equity:.2f} | "
            f"streak={self.state.loss_streak}"
        )

    def _force_close(self, last_candle, current_time: datetime) -> None:
        """Примусово закриває позицію в кінці бектесту."""
        if self.state.open_trade:
            price = Decimal(str(last_candle['close']))
            self._close_trade(
                self.state.open_trade, price, "timeout", current_time
            )

    def _update_range_cache(self, slice_1h, slice_30m,
                            current_time: datetime) -> None:
        """Оновлює кеш рейнджів раз на годину."""
        if (self.state.range_updated_at is None or
                (current_time - self.state.range_updated_at
                 ).total_seconds() >= self.RANGE_UPDATE_MINUTES * 60):
            self.state.cached_range_1h  = detect_range(slice_1h)
            self.state.cached_range_30m = detect_range(
                slice_30m, min_candles=10
            )
            self.state.range_updated_at = current_time

    def _check_daily_reset(self, current_time: datetime) -> None:
        """Скидає денну статистику о 00:00 UTC."""
        if (self.state.last_trade_time and
                current_time.date() > self.state.last_trade_time.date()):
            logger.info(
                f"📅 Новий день | попередній P&L: ${self.state.daily_pnl:.2f} | "
                f"угод: {self.state.daily_trades}"
            )
            self.state.daily_pnl    = Decimal("0")
            self.state.daily_trades = 0

    def _get_slice(self, df: pd.DataFrame,
                   current_time: datetime, n: int) -> pd.DataFrame:
        """Повертає останні N свічок до current_time."""
        mask = df.index <= current_time
        sub  = df[mask]
        return sub.iloc[-n:] if len(sub) >= n else sub

    def _calculate_stats(self, symbol: str,
                         signals_found: int,
                         signals_filtered: int) -> dict:
        """Розраховує підсумкову статистику бектесту."""
        trades = self.state.closed_trades
        if not trades:
            return {"symbol": symbol, "error": "Немає угод"}

        wins  = [t for t in trades if t.pnl_net and t.pnl_net > 0]
        losses = [t for t in trades if t.pnl_net and t.pnl_net <= 0]

        total_pnl    = sum(t.pnl_net for t in trades if t.pnl_net)
        total_fees   = sum(t.fee_total for t in trades)
        total_slip   = sum(t.slippage_cost for t in trades)
        win_rate     = len(wins) / len(trades) * 100 if trades else 0

        avg_win  = (sum(t.pnl_net for t in wins) / len(wins)
                    if wins else Decimal("0"))
        avg_loss = (sum(t.pnl_net for t in losses) / len(losses)
                    if losses else Decimal("0"))

        # Max drawdown
        equity_curve = [self.state.deposit]
        running = self.state.deposit
        for t in trades:
            running += t.pnl_net or Decimal("0")
            equity_curve.append(running)

        peak = equity_curve[0]
        max_dd = Decimal("0")
        for e in equity_curve:
            if e > peak:
                peak = e
            dd = (peak - e) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # По сесіях
        asian_trades  = [t for t in trades if t.session == "asian"]
        london_trades = [t for t in trades if t.session == "london_pre"]

        # По режимах
        mode_1h   = [t for t in trades if "dual" not in t.mode]
        mode_dual = [t for t in trades if "dual" in t.mode]

        return {
            "symbol":           symbol,
            "deposit":          self.state.deposit,
            "final_equity":     self.state.equity,
            "total_pnl":        total_pnl,
            "total_pnl_pct":    total_pnl / self.state.deposit * 100,
            "total_trades":     len(trades),
            "wins":             len(wins),
            "losses":           len(losses),
            "win_rate_pct":     Decimal(str(win_rate)).quantize(Decimal("0.1")),
            "avg_win":          avg_win.quantize(Decimal("0.01")),
            "avg_loss":         avg_loss.quantize(Decimal("0.01")),
            "max_drawdown_pct": max_dd.quantize(Decimal("0.1")),
            "total_fees":       total_fees.quantize(Decimal("0.01")),
            "total_slippage":   total_slip.quantize(Decimal("0.01")),
            "signals_found":    signals_found,
            "signals_filtered": signals_filtered,
            "asian_trades":     len(asian_trades),
            "london_trades":    len(london_trades),
            "mode_1h_trades":   len(mode_1h),
            "mode_dual_trades": len(mode_dual),
            "equity_curve":     equity_curve,
        }

    def _print_stats(self, stats: dict) -> None:
        """Виводить результати бектесту."""
        if "error" in stats:
            logger.error(f"Бектест помилка: {stats['error']}")
            return

        logger.info(f"\n{'='*60}")
        logger.info(f"📊 РЕЗУЛЬТАТИ БЕКТЕСТУ — {stats['symbol']}")
        logger.info(f"{'='*60}")
        logger.info(f"  Депозит:        ${stats['deposit']}")
        logger.info(f"  Фінальний:      ${stats['final_equity']:.2f}")
        logger.info(f"  P&L:            ${stats['total_pnl']:.2f} "
                    f"({stats['total_pnl_pct']:.1f}%)")
        logger.info(f"  Угод:           {stats['total_trades']} "
                    f"({stats['wins']}W / {stats['losses']}L)")
        logger.info(f"  Win Rate:       {stats['win_rate_pct']}%")
        logger.info(f"  Avg Win/Loss:   ${stats['avg_win']} / ${stats['avg_loss']}")
        logger.info(f"  Max Drawdown:   {stats['max_drawdown_pct']}%")
        logger.info(f"  Комісії:        ${stats['total_fees']}")
        logger.info(f"  Slippage:       ${stats['total_slippage']}")
        logger.info(f"  Сесії:          Asian={stats['asian_trades']} "
                    f"London={stats['london_trades']}")
        logger.info(f"  Режими:         1h={stats['mode_1h_trades']} "
                    f"Dual={stats['mode_dual_trades']}")
        logger.info(f"  Сигналів:       знайдено={stats['signals_found']} "
                    f"відфільтровано={stats['signals_filtered']}")
        logger.info(f"{'='*60}\n")