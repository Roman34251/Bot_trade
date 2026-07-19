"""
LIVE TRADER — Bybit Demo / Live
=================================
Торгує в реальному часі на Bybit demo або live рахунку.
Використовує WebSocket для order book (50 рівнів) і свічок.

Архітектура:
  WebSocket потік 1 → order book (bid/ask дисбаланс, великі стіни)
  WebSocket потік 2 → 1m свічки (real-time OHLCV)
  Головний цикл    → кожні 15с перевіряє сигнал
  При сигналі      → підтверджує order book → маркет ордер

Запуск:
  python main.py --demo    ← demo рахунок (BYBIT_DEMO=true в .env)
  python main.py --live    ← live рахунок (BYBIT_DEMO=false в .env)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import ccxt
import ccxt.pro as ccxtpro
import pandas as pd
import websockets
from loguru import logger

from config.settings import (
    TRADING_PAIRS, BYBIT_DEMO,
    ACTIVE_API_KEY, ACTIVE_API_SECRET,
    DEPOSIT_USDT, RISK_PER_TRADE_PCT, USE_REAL_BALANCE,
    MIN_RISK_REWARD,
    COOLDOWN_AFTER_LOSS_MIN, MIN_CANDLES_BETWEEN_TRADES,
    MAX_CONSECUTIVE_LOSSES, COOLDOWN_AFTER_SERIES_MIN,
    OB_IMBALANCE_LONG_MIN, OB_IMBALANCE_SHORT_MAX,
    OB_MAX_AGE_SECONDS, OB_WALL_THRESHOLD_MULT, OB_WALL_BLOCK_PCT,
    USE_DUAL_TF_STRATEGY,
    USE_ORDER_BOOK_CONFIRMATION,
    USE_ORDER_BOOK_WALL_FILTER,
    SWEEP_USE_OB_CONFIRM,
    USE_FTA_FILTER, FTA_TF, FTA_SWING_LOOKBACK, FTA_BUFFER_PCT,
    SYMBOL_CONFIG,
    USE_SWEEP_STRATEGY, USE_MEANREV_STRATEGY, USE_VWAP_STRATEGY,
    USE_TREND_STRATEGY,
    STRATEGY_PRIORITY,
    TRADE_HOURS_ONLY, TRADE_HOUR_START, TRADE_HOUR_END,
    BYBIT_LEVERAGE, MAX_NOTIONAL_EQUITY_MULT,
    SIGNALS_USE_CLOSED_CANDLES, SIGNAL_DEDUP_ENABLED, MAX_ENTRY_DRIFT_BPS,
    DAILY_RISK_SYNC_INTERVAL_SEC, DAILY_RISK_SYNC_FAILURE_LIMIT,
    CLOSED_PNL_GRACE_SEC,
    USE_MAKER_ENTRY, MAKER_ENTRY_TTL_SEC, MAKER_FALLBACK_TO_MARKET,
    OB_PERSISTENCE_WINDOW_SEC, OB_PERSISTENCE_MIN_SEC,
    OB_PERSISTENCE_MIN_SAMPLES, OB_PERSISTENCE_MIN_RATIO,
    SWEEP_REQUIRE_TRADE_FLOW, TRADE_FLOW_LOOKBACK_SEC,
    TRADE_FLOW_IMBALANCE_MIN, TRADE_FLOW_MIN_NOTIONAL,
    ENABLE_TRADE_DB_LOG,
)
from indicators.range_detector import detect_active_range, calculate_atr
from structure import first_trouble_area
from signals.generator import generate_scalp_signal
from signals.mean_reversion import generate_meanrev_signal
from signals.vwap_strategy import generate_vwap_signal
from signals.dual_tf import generate_trend_signal, generate_dual_tf_signal
from signals.calculator import calculate_position, check_daily_limits
from storage.database import Database


# ── Order Book ────────────────────────────────────────────────

@dataclass
class OrderBookSnapshot:
    """
    Поточний стан order book (топ 25 рівнів).

    imbalance = (bid_total - ask_total) / (bid_total + ask_total) × 100
      > +12% → покупці домінують → лонг підтвердження
      < -20% → продавці домінують → шорт підтвердження
    """
    symbol:    str
    timestamp: datetime
    bids:      list   # [[price, qty], ...]
    asks:      list
    # Monotonic receive time is authoritative for freshness/persistence;
    # wall-clock and exchange clocks can jump or drift.
    received_mono: float = field(default_factory=time.monotonic)

    bid_total: float = 0.0
    ask_total: float = 0.0
    imbalance: float = 0.0
    bid_walls: list  = field(default_factory=list)
    ask_walls: list  = field(default_factory=list)

    def analyze(self) -> None:
        if not self.bids or not self.asks:
            return

        bid_qtys = [float(b[1]) for b in self.bids[:25]]
        ask_qtys = [float(a[1]) for a in self.asks[:25]]

        self.bid_total = sum(bid_qtys)
        self.ask_total = sum(ask_qtys)
        total = self.bid_total + self.ask_total
        if total > 0:
            self.imbalance = (self.bid_total - self.ask_total) / total * 100

        avg_bid = self.bid_total / len(bid_qtys) if bid_qtys else 0
        avg_ask = self.ask_total / len(ask_qtys) if ask_qtys else 0

        self.bid_walls = [
            [float(b[0]), float(b[1])] for b in self.bids[:25]
            if float(b[1]) > avg_bid * OB_WALL_THRESHOLD_MULT
        ]
        self.ask_walls = [
            [float(a[0]), float(a[1])] for a in self.asks[:25]
            if float(a[1]) > avg_ask * OB_WALL_THRESHOLD_MULT
        ]

    def has_wall_against(self, direction: str, entry_price: float) -> bool:
        """Велика стіна в межах OB_WALL_BLOCK_PCT від entry → блокує вхід."""
        threshold = entry_price * OB_WALL_BLOCK_PCT
        if direction == "long":
            return any(p <= entry_price + threshold for p, _ in self.ask_walls)
        if direction == "short":
            return any(p >= entry_price - threshold for p, _ in self.bid_walls)
        return False

    def summary(self) -> str:
        sign = "+" if self.imbalance >= 0 else ""
        return (
            f"OB imbalance={sign}{self.imbalance:.1f}% "
            f"bid={self.bid_total:.2f} ask={self.ask_total:.2f} | "
            f"bid_walls={len(self.bid_walls)} ask_walls={len(self.ask_walls)}"
        )


# ── Стан трейдера ─────────────────────────────────────────────

@dataclass
class LiveState:
    equity:           Decimal
    deposit:          Decimal

    open_trade:       Optional[dict] = None

    daily_pnl:        Decimal = Decimal("0")
    daily_trades:     int     = 0
    risk_day:         date    = field(
        default_factory=lambda: datetime.now(timezone.utc).date()
    )
    loss_streak:      int     = 0
    last_trade_time:  Optional[datetime] = None
    last_loss_time:   Optional[datetime] = None

    # Кешовані рейнджі (оновлення раз на RANGE_UPDATE_MIN хвилин)
    cached_range_1h:  Optional[dict] = None
    cached_range_30m: Optional[dict] = None
    range_updated_at: Optional[datetime] = None

    ob_snapshots:         dict = field(default_factory=dict)   # symbol → OBSnapshot
    ob_imbalance_history: dict = field(default_factory=dict)   # symbol → deque
    ob_snapshot_history:  dict = field(default_factory=dict)   # symbol → deque[OrderBookSnapshot]
    trade_flow_history:   dict = field(default_factory=dict)   # symbol → deque[(recv_mono, exchange_epoch, signed notional)]
    last_signal_keys:     dict = field(default_factory=dict)   # symbol+strategy → останній оброблений setup
    pending_sweeps:       dict = field(default_factory=dict)   # symbol → {key, first_seen_mono}
    recent_outcomes:      deque = field(default_factory=lambda: deque(maxlen=50))  # 1 win, 0 loss
    safety_latch_reason:   Optional[str] = None  # restart/reconciliation required

    candles: dict = field(default_factory=dict)  # "BTC/USDT:USDT_1h" → deque(200)

    # LIVE-дані: Bybit пушить оновлення ПОТОЧНОЇ (незакритої) свічки кожні
    # ~1-3с. Тримаємо їх окремо від закритих — бот бачить ціну в реальному
    # часі, а не раз на закриту хвилину.
    live_candles: dict = field(default_factory=dict)  # key → остання live-свічка
    ws_msg_at:    dict = field(default_factory=dict)  # key → epoch останнього push'а

    # Стан WS-потоків для діагностики: key → {connected, connects, msgs,
    # last_error, last_error_at}. Показується у /diag — щоб причина обриву
    # була видна одразу, без походу в логи сервера.
    ws_status:    dict = field(default_factory=dict)
    # key -> actual closed-candle age after a failed REST freshness recovery.
    candle_freshness_errors: dict = field(default_factory=dict)

    # Лічильники ВИКОНАННЯ: де саме «вмирають» сигнали на шляху
    # стратегія → фільтри → калькулятор → біржа. Показуються у /diag,
    # щоб причина «сетапи є, угод нема» була видна одразу.
    exec_stats:   dict = field(default_factory=lambda: {
        "signals": 0,            # сигналів дійшло від стратегій
        "wall_blocked": 0,       # зарізав фільтр стіни OB
        "obdir_blocked": 0,      # зарізав фільтр напрямку OB
        "calc_rejected": 0,      # зарізав калькулятор (net RR / розмір)
        "sent": 0,               # ордерів надіслано на біржу
        "exchange_rejected": 0,  # біржа відхилила
        "opened": 0,             # позицій реально відкрито
        "fta_blocked": 0,        # зарізав FTA-фільтр (TP за проблемною зоною HTF)
        "dedup_blocked": 0,      # повтор того самого closed-bar setup
        "flow_blocked": 0,       # sweep без executed-flow підтвердження
        "maker_fills": 0,
        "maker_timeouts": 0,
        "last_execution": None,
        "last_reject": None,     # текст останньої відмови біржі
    })


# ── Головний клас ─────────────────────────────────────────────

class LiveTrader:

    CANDLE_BUFFER    = 310   # ≥288×5m для повного UTC session-VWAP + запас
    RANGE_UPDATE_MIN = 60

    # Мапа ТФ → інтервал Bybit v5 WS. КОРІНЬ «мовчазного live-потоку»:
    # kline-топіки Bybit приймають ХВИЛИНИ ЧИСЛОМ (1/5/30/60/240) або D/W/M.
    # "kline.1m..."/"kline.1h..." — НЕІСНУЮЧІ топіки: біржа приймала підписку
    # і не слала НІЧОГО (тому OB працював, а свічки — ніколи).
    WS_KLINE_INTERVAL = {
        "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
        "1h": "60", "2h": "120", "4h": "240", "1d": "D",
    }
    TIMEFRAME_MS = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
        "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
        "4h": 14_400_000, "1d": 86_400_000,
    }

    @staticmethod
    def _required_timeframes() -> list[str]:
        """ТФ live-потоків із фактичного config, включно з TREND_ENTRY_TF=15m."""
        result = {"1m", "5m", "30m", "1h", FTA_TF}
        for symbol_cfg in SYMBOL_CONFIG.values():
            result.add(str(symbol_cfg.get("trend", {}).get("trend_tf", "1h")))
            result.add(str(symbol_cfg.get("trend", {}).get("entry_tf", "5m")))
            result.add(str(symbol_cfg.get("vwap", {}).get("tf", "5m")))
            result.add(str(symbol_cfg.get("meanrev", {}).get("tf", "5m")))
        order = {tf: i for i, tf in enumerate(LiveTrader.TIMEFRAME_MS)}
        return sorted(result, key=lambda tf: order.get(tf, 999))

    def __init__(self):
        self._running     = False
        self._ob_lock     = threading.Lock()
        self._market_rest_lock = threading.Lock()
        self._execution_lock = asyncio.Lock()
        self._real_balance = float(DEPOSIT_USDT)
        self._paused      = asyncio.Event()
        self._paused.set()

        self.rest   = self._connect_rest()
        self.market = self._connect_market_rest()
        self.ws     = None

        # Опційний нотифаєр (Telegram). telegrambot.py виставляє
        # trader.notifier = tg_bot. Якщо None — трейдер працює мовчки.
        # Усі виклики йдуть через self._notify() і НІКОЛИ не ламають торгівлю.
        self.notifier = None

        # Самолікування замерзлих даних (див. _ensure_fresh_data).
        # -1e9, а не 0: time.monotonic() на свіжому сервері стартує біля 0,
        # і 0.0 заблокувала б ПЕРШЕ перезавантаження тротлінгом.
        self._last_data_refresh = -1e9
        self._last_stale_alert  = -1e9
        # Тротлінг алертів про відхилені ордери (1 на 10 хв; у лог — все)
        self._last_reject_alert = -1e9
        self._last_daily_risk_sync = -1e9
        self._daily_risk_sync_failures = 0
        self._entry_block_until = 0.0
        self._last_ob_debug: dict[str, float] = {}

        self.state = LiveState(
            equity  = Decimal(str(self._real_balance)),
            deposit = Decimal(str(self._real_balance)),
        )

        mode = "DEMO" if BYBIT_DEMO else "LIVE 🔴"
        logger.info(f"🤖 LiveTrader | режим={mode}")
        logger.info(f"   Депозит: ${self._real_balance:.2f} | Ризик: {RISK_PER_TRADE_PCT*100:.1f}%/угоду")
        logger.info(f"   Пари: {TRADING_PAIRS}")

    def _trip_safety_latch(self, reason: str) -> None:
        """Non-bypassable safety stop; Telegram resume must not clear it."""
        self.state.safety_latch_reason = reason[:160]
        self._paused.clear()

    # ── Підключення ───────────────────────────────────────────

    def _connect_rest(self) -> ccxt.bybit:
        exchange = ccxt.bybit({
            "apiKey":          ACTIVE_API_KEY,
            "secret":          ACTIVE_API_SECRET,
            "enableRateLimit": True,
            "options":         {"defaultType": "linear"},
        })

        if BYBIT_DEMO:
            demo_url = "https://api-demo.bybit.com"
            exchange.urls["api"] = {
                k: demo_url for k in ("spot", "futures", "v2", "public", "private")
            }
            logger.info("⚠️  Demo endpoint: api-demo.bybit.com")

        try:
            from pybit.unified_trading import HTTP
            session = HTTP(
                testnet    = False,
                demo       = bool(BYBIT_DEMO),
                api_key    = ACTIVE_API_KEY,
                api_secret = ACTIVE_API_SECRET,
            )
            self._pybit = session
            resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            usdt = float(resp["result"]["list"][0]["coin"][0]["walletBalance"])
            # Капітал тягнемо З АКАУНТА (запит власника): USE_REAL_BALANCE=true
            # → торгуємо від реального walletBalance USDT. DEPOSIT_USDT — лише
            # fallback/фіксований режим.
            if USE_REAL_BALANCE:
                self._real_balance = usdt
                logger.info(
                    f"✅ Bybit підключено | капітал з акаунта: ${usdt:,.2f} "
                    f"(ризик/угоду {RISK_PER_TRADE_PCT*100:.1f}% = "
                    f"${usdt * RISK_PER_TRADE_PCT:,.2f})"
                )
            else:
                self._real_balance = float(DEPOSIT_USDT)
                logger.info(
                    f"✅ Bybit підключено | баланс ${usdt:,.2f}, "
                    f"торгуємо від фіксованих ${DEPOSIT_USDT:,.2f}"
                )
        except Exception as e:
            logger.error(f"❌ Помилка підключення: {e}")
            raise

        return exchange

    def _connect_market_rest(self) -> ccxt.bybit:
        """Публічний REST для завантаження свічок (завжди api.bybit.com)."""
        return ccxt.bybit({
            "enableRateLimit": True,
            "options": {"defaultType": "linear", "defaultSubType": "linear"},
        })

    async def _connect_ws(self) -> ccxtpro.bybit:
        ws = ccxtpro.bybit({
            "apiKey":          ACTIVE_API_KEY,
            "secret":          ACTIVE_API_SECRET,
            "enableRateLimit": True,
            "options": {
                "defaultType":     "linear",
                "defaultSubType":  "linear",
                "fetchCurrencies": False,
            },
        })
        ws.has["fetchCurrencies"] = False

        if BYBIT_DEMO:
            ws.urls["api"] = {k: "https://api-demo.bybit.com"
                              for k in ("spot", "futures", "v2", "private")}
            ws.urls["ws"] = {
                "public":  "wss://stream.bybit.com/v5/public/linear",
                "private": "wss://stream-demo.bybit.com/v5/private",
            }

        try:
            markets = await ws.fetch_markets({"category": "linear"})
            ws.set_markets(markets)
            logger.info(f"✅ WS private: {len(markets)} linear markets")
        except Exception as e:
            logger.warning(f"⚠️ WS fetch_markets: {e}")

        return ws

    # ── Головний цикл ─────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        self.ws = await self._connect_ws()

        if ENABLE_TRADE_DB_LOG:
            try:
                await asyncio.to_thread(self._initialize_trade_db)
            except Exception as e:
                logger.warning(f"DB schema init failed; trade log буде недоступний: {e}")

        try:
            daily_rows = await asyncio.to_thread(self._fetch_today_closed_pnl_rows)
            self._restore_daily_risk_state(daily_rows)
        except Exception as e:
            # Restart must not silently erase a daily loss/streak. If the
            # authoritative history cannot be restored, require explicit resume.
            self._trip_safety_latch("daily risk restore failed")
            self.state.exec_stats["last_reject"] = "daily risk restore failed"
            logger.error(f"Daily risk restore failed — бот на паузі: {e}")
            await self._notify(
                "send_alert",
                f"🆘 *Не вдалося відновити денний PnL після рестарту.* "
                f"Бот поставлено на паузу. `{e}`",
            )
        try:
            recent_rows = await asyncio.to_thread(self._fetch_recent_closed_pnl_rows)
            self._restore_recent_outcomes(recent_rows)
        except Exception as e:
            self._trip_safety_latch("recent loss-streak restore failed")
            logger.error(f"Recent loss-streak restore failed — бот на паузі: {e}")
            await self._notify(
                "send_alert",
                "🆘 Не вдалося відновити останню серію угод; "
                "safety latch активний.",
            )

        # Прогрів маркетів для торгового REST (demo-endpoint): беремо
        # довідник інструментів з mainnet і віддаємо demo-інстансу, щоб
        # перший create_order не залежав від load_markets на api-demo.
        try:
            mkts = await asyncio.to_thread(self.market.load_markets)
            self.rest.set_markets(mkts)
            logger.info(f"✅ Markets warmup: {len(mkts)} інструментів")
        except Exception as e:
            logger.warning(f"Markets warmup не вдався (не критично): {e}")

        # BYBIT_LEVERAGE=0: зберігаємо плече, яке власник задав на біржі.
        if BYBIT_LEVERAGE > 0:
            for symbol in TRADING_PAIRS:
                try:
                    await asyncio.to_thread(self.rest.set_leverage, BYBIT_LEVERAGE, symbol)
                    logger.info(f"✅ Leverage {symbol}: {BYBIT_LEVERAGE}x")
                except Exception as e:
                    logger.warning(f"Leverage {symbol} не змінено: {e}")

        await self._load_initial_candles()

        # Відновлюємо відкриту позицію з біржі (якщо бот перезапустили, поки
        # угода жива) — інакше бот "забуває" її і може відкрити другу поверх.
        await self._recover_open_position()
        # Give eventual-consistent closed-PnL enough time, then force another
        # risk sync before the first possible entry after restart.
        self._entry_block_until = time.monotonic() + 16.0

        tasks = []
        for symbol in TRADING_PAIRS:
            tasks.append(self._stream_orderbook(symbol))
            # Раніше стрімився ТІЛЬКИ 1m → 1h/5m/30m після старту "застигали"
            # (рейндж рахувався з протухлого вікна, 5m CVD/volume були мертві).
            for tf in self._required_timeframes():
                tasks.append(self._stream_candles(symbol, tf))
        tasks.append(self._trading_loop())

        logger.info("▶ Всі потоки запущено")
        try:
            await asyncio.gather(*tasks)
        finally:
            self._running = False
            self._paused.set()
            if self.ws is not None:
                try:
                    await self.ws.close()
                except Exception as e:
                    logger.debug(f"CCXT WS close: {e}")

    def _upsert_closed_candle(
        self, key: str, tf: str, candle: list, *, exchange_confirmed: bool = False
    ) -> None:
        """Єдине місце запису confirmed candle без дублів REST/WS."""
        tf_ms = self.TIMEFRAME_MS.get(tf)
        if tf_ms is None:
            return
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if not exchange_confirmed and int(candle[0]) + tf_ms > now_ms:
            return
        buf = self.state.candles.setdefault(key, deque(maxlen=self.CANDLE_BUFFER))
        rows = {int(row[0]): list(row) for row in buf}
        rows[int(candle[0])] = list(candle)
        ordered = [rows[ts] for ts in sorted(rows)]
        self.state.candles[key] = deque(ordered[-300:], maxlen=self.CANDLE_BUFFER)

    def _fetch_market_ohlcv(self, symbol: str, tf: str, limit: int) -> list:
        """Serialize access to the synchronous public CCXT client."""
        with self._market_rest_lock:
            return self.market.fetch_ohlcv(symbol, tf, limit=limit)

    def _fetch_market_ticker(self, symbol: str) -> dict:
        with self._market_rest_lock:
            return self.market.fetch_ticker(symbol)

    async def _load_initial_candles(self) -> None:
        logger.info("📊 Завантаження початкових свічок...")
        for symbol in TRADING_PAIRS:
            for tf in self._required_timeframes():
                try:
                    raw = await asyncio.to_thread(
                        self._fetch_market_ohlcv, symbol, tf, self.CANDLE_BUFFER
                    )
                    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                    tf_ms = self.TIMEFRAME_MS[tf]
                    closed = [list(c) for c in raw if int(c[0]) + tf_ms <= now_ms]
                    self.state.candles[f"{symbol}_{tf}"] = deque(
                        closed[-300:], maxlen=self.CANDLE_BUFFER
                    )
                    logger.info(f"   {symbol} {tf}: {len(raw)} свічок")
                except Exception as e:
                    logger.error(f"Помилка завантаження {symbol} {tf}: {e}")
        logger.info("✅ Початкові свічки завантажено")

    async def _recover_open_position(self) -> None:
        """
        Підтягує ВІДКРИТУ позицію з біржі у state.open_trade після рестарту.
        Без цього бот "забуває" угоду (state тримається лише в пам'яті) і
        може відкрити другу поверх неї + не порахує її закриття.
        TP/SL прикріплені до позиції на біржі — тож захист живий незалежно.
        """
        for symbol in TRADING_PAIRS:
            positions = None
            last_error = None
            for _ in range(3):
                try:
                    positions = await asyncio.to_thread(
                        self.rest.fetch_positions, [symbol]
                    )
                    break
                except Exception as e:
                    last_error = e
                    await asyncio.sleep(0.5)
            if positions is None:
                self._trip_safety_latch("position recovery failed")
                self.state.exec_stats["last_reject"] = "position recovery failed"
                logger.error(
                    f"Recover: fetch_positions {symbol} не підтверджено — бот на паузі: "
                    f"{last_error}"
                )
                await self._notify(
                    "send_alert",
                    f"🆘 *Не вдалося перевірити відкриті позиції {symbol} після "
                    "рестарту.* Бот поставлено на паузу.",
                )
                return

            active = [p for p in positions if float(p.get("contracts", 0) or 0) > 0]
            if not active:
                continue

            p = active[0]
            info = p.get("info", {}) or {}

            side = str(p.get("side") or info.get("side", "")).lower()
            direction = "long" if side in ("long", "buy") else "short"
            entry = float(p.get("entryPrice") or info.get("avgPrice") or 0)
            qty = float(p.get("contracts") or info.get("size") or 0)

            def _num(v):
                try:
                    f = float(v)
                    return f if f > 0 else None
                except (TypeError, ValueError):
                    return None

            tp = _num(info.get("takeProfit"))
            sl = _num(info.get("stopLoss"))

            raw_rr = None
            if tp and sl and entry and abs(entry - sl) > 0:
                raw_rr = abs(tp - entry) / abs(entry - sl)

            # Час поточного входу з біржі (openTime, fallback created/updatedTime).
            opened_at = datetime.now(timezone.utc)
            ct = info.get("openTime") or info.get("createdTime") or info.get("updatedTime")
            try:
                if ct:
                    opened_at = datetime.fromtimestamp(int(ct) / 1000, tz=timezone.utc)
            except (TypeError, ValueError):
                pass

            persisted_trade_id = None
            if ENABLE_TRADE_DB_LOG:
                try:
                    persisted_trade_id = await asyncio.to_thread(
                        self._find_persisted_open_trade_id,
                        symbol,
                        direction,
                        entry,
                        qty,
                        opened_at,
                    )
                except Exception as e:
                    logger.warning(f"Recover DB lookup failed: {e}")
            recovery_seed = f"{symbol}:{ct}:{entry}:{qty}:{direction}"
            recovered_id = "recovered-" + hashlib.sha1(
                recovery_seed.encode("utf-8")
            ).hexdigest()[:24]

            self.state.open_trade = {
                "symbol":     symbol,
                "direction":  direction,
                "entry":      entry,
                "qty":        qty,
                "tp":         tp,
                "sl":         sl,
                "order_id":   persisted_trade_id or info.get("orderId") or recovered_id,
                "opened_at":  opened_at,
                "raw_rr":     raw_rr,
                "strategy":   "recovered",
                "mode":       "recovered",
            }
            if opened_at.date() == datetime.now(timezone.utc).date():
                self.state.daily_trades += 1
            if self.state.last_trade_time is None or opened_at > self.state.last_trade_time:
                self.state.last_trade_time = opened_at
            logger.info(
                f"♻️ Відновлено позицію з біржі: {direction.upper()} {qty} {symbol} "
                f"@ {entry:.2f} | TP={tp} SL={sl}"
            )
            if tp is None or sl is None:
                self._trip_safety_latch("recovered position has no visible TP/SL")
                await self._notify(
                    "send_alert",
                    f"🆘 *Відновлена позиція {symbol} без видимих TP/SL.* "
                    "Бот поставлено на паузу; перевір захист на Bybit.",
                )
            else:
                try:
                    await asyncio.to_thread(
                        self._save_trade_open_db, self.state.open_trade
                    )
                except Exception as e:
                    logger.warning(f"Recover DB save failed: {e}")
            await self._notify(
                "send_alert",
                f"♻️ *Відновлено відкриту позицію після рестарту*\n"
                f"{direction.upper()} {qty} `{symbol}` @ {entry:.2f}\n"
                f"TP `{tp}` SL `{sl}`",
            )
            return  # бот тримає рівно 1 позицію


    @staticmethod
    async def _bybit_keepalive(ws, label: str) -> None:
        """
        Bybit v5 потребує ping кожні ~20с інакше рве з'єднання.
        Надсилає {"op":"ping"} — саме такий формат розуміє Bybit.
        Живе поки живе ws з'єднання.
        """
        while True:
            await asyncio.sleep(20)
            try:
                await ws.send(json.dumps({"op": "ping"}))
            except Exception:
                break  # з'єднання закрите — виходимо
 
    async def _stream_orderbook(self, symbol: str) -> None:
        ws_symbol = symbol.replace("/", "").replace(":USDT", "")
        url       = "wss://stream.bybit.com/v5/public/linear"
        sub_msg   = json.dumps({
            "op": "subscribe",
            "args": [f"orderbook.50.{ws_symbol}", f"publicTrade.{ws_symbol}"],
        })
 
        full_bids: dict = {}
        full_asks: dict = {}
        seen_trade_ids: set[str] = set()
        seen_trade_order: deque = deque(maxlen=10_000)
 
        while self._running:
            try:
                async with websockets.connect(
                    url,
                    # ping_interval=None свідомо — див. коментар у _stream_candles
                    # (захист від зомбі — вотчдог тиші, не протокольні пінги)
                    ping_interval = None,
                    ping_timeout  = None,
                    close_timeout = 10,
                    open_timeout  = 10,
                ) as ws:
                    await ws.send(sub_msg)
                    self._ws_note(f"{symbol}_OB", connected=True, inc_connects=True)
                    self._ws_note(f"{symbol}_FLOW", connected=True, inc_connects=True)
                    full_bids.clear()
                    full_asks.clear()

                    keepalive = asyncio.create_task(
                        self._bybit_keepalive(ws, f"OB {symbol}")
                    )
 
                    try:
                        first_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        first     = json.loads(first_raw)
 
                        if first.get("type") == "snapshot":
                            logger.info(f"✅ OB WebSocket підключено: {symbol}")
                            for b in first.get("data", {}).get("b", []):
                                full_bids[b[0]] = b[1]
                            for a in first.get("data", {}).get("a", []):
                                full_asks[a[0]] = a[1]
                        elif first.get("op") == "subscribe":
                            if not first.get("success"):
                                logger.error(f"OB підписка відхилена: {first}")
                                await asyncio.sleep(5)
                                continue
                            logger.info(f"✅ OB WebSocket підключено: {symbol}")
                        elif first.get("op") == "pong":
                            logger.debug(f"OB {symbol}: pong")
                        else:
                            logger.warning(f"OB несподіваний перший меседж: {first}")
 
                        last_msg = time.monotonic()
                        last_ob_msg = last_msg
                        last_flow_msg = last_msg
                        while self._running:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=25)
                            except asyncio.TimeoutError:
                                if time.monotonic() - last_msg > 75:
                                    raise ConnectionError("OB WS: тиша >75с — force reconnect")
                                continue
                            last_msg = time.monotonic()
                            data     = json.loads(raw)
                            msg_type = data.get("type")
                            topic    = data.get("topic", "")
                            ob_data  = data.get("data", {})
 
                            if data.get("op") == "pong":
                                continue
 
                            if topic.startswith("publicTrade"):
                                last_flow_msg = time.monotonic()
                                self._ws_note(f"{symbol}_FLOW", connected=True, inc_msgs=True)
                                now_epoch = datetime.now(timezone.utc).timestamp()
                                received_mono = time.monotonic()
                                flow_rows = []
                                for trade in data.get("data", []):
                                    try:
                                        block_raw = trade.get("BT", False)
                                        if (
                                            block_raw is True
                                            or str(block_raw).strip().lower() in {"1", "true", "yes"}
                                        ):
                                            continue
                                        trade_id = str(trade.get("i") or "")
                                        if trade_id and trade_id in seen_trade_ids:
                                            continue
                                        if trade_id:
                                            if len(seen_trade_order) == seen_trade_order.maxlen:
                                                seen_trade_ids.discard(seen_trade_order.popleft())
                                            seen_trade_order.append(trade_id)
                                            seen_trade_ids.add(trade_id)
                                        price = float(trade.get("p") or 0)
                                        size = float(trade.get("v") or 0)
                                        side = str(trade.get("S") or "").lower()
                                        ts = float(trade.get("T") or 0) / 1000.0 or now_epoch
                                        signed = price * size * (1.0 if side == "buy" else -1.0)
                                        if price > 0 and size > 0 and side in ("buy", "sell"):
                                            flow_rows.append((received_mono, ts, signed))
                                    except (TypeError, ValueError):
                                        continue
                                if flow_rows:
                                    with self._ob_lock:
                                        hist = self.state.trade_flow_history.setdefault(
                                            symbol, deque(maxlen=4000)
                                        )
                                        hist.extend(flow_rows)
                                if time.monotonic() - last_ob_msg > 75:
                                    raise ConnectionError(
                                        "OB topic мовчить >75с при живому publicTrade"
                                    )
                                continue

                            if not topic.startswith("orderbook"):
                                continue
                            last_ob_msg = time.monotonic()
                            if last_ob_msg - last_flow_msg > 75:
                                raise ConnectionError(
                                    "publicTrade topic мовчить >75с при живому OB"
                                )
                            self._ws_note(f"{symbol}_OB", inc_msgs=True)
 
                            if msg_type == "snapshot":
                                full_bids = {b[0]: b[1] for b in ob_data.get("b", [])}
                                full_asks = {a[0]: a[1] for a in ob_data.get("a", [])}
                            elif msg_type == "delta":
                                for price, qty in ob_data.get("b", []):
                                    if qty == "0":
                                        full_bids.pop(price, None)
                                    else:
                                        full_bids[price] = qty
                                for price, qty in ob_data.get("a", []):
                                    if qty == "0":
                                        full_asks.pop(price, None)
                                    else:
                                        full_asks[price] = qty
 
                            if full_bids and full_asks:
                                sorted_bids = sorted(
                                    full_bids.items(), key=lambda x: float(x[0]), reverse=True
                                )[:25]
                                sorted_asks = sorted(
                                    full_asks.items(), key=lambda x: float(x[0])
                                )[:25]
 
                                snapshot = OrderBookSnapshot(
                                    symbol    = symbol,
                                    timestamp = datetime.now(timezone.utc),
                                    bids      = [[float(p), float(q)] for p, q in sorted_bids],
                                    asks      = [[float(p), float(q)] for p, q in sorted_asks],
                                )
                                snapshot.analyze()
 
                                with self._ob_lock:
                                    self.state.ob_snapshots[symbol] = snapshot
                                    if symbol not in self.state.ob_imbalance_history:
                                        self.state.ob_imbalance_history[symbol] = deque(maxlen=50)
                                    self.state.ob_imbalance_history[symbol].append(snapshot.imbalance)
                                    history = self.state.ob_snapshot_history.setdefault(
                                        symbol, deque(maxlen=500)
                                    )
                                    history.append(snapshot)
 
                                # WS може давати десятки delta-повідомлень/с. Одного
                                # діагностичного snapshot на секунду достатньо, а лог
                                # більше не розростається до сотень МБ за добу.
                                now_debug = time.monotonic()
                                if now_debug - self._last_ob_debug.get(symbol, -1e9) >= 1.0:
                                    self._last_ob_debug[symbol] = now_debug
                                    logger.debug(f"{symbol} OB [{msg_type}]: {snapshot.summary()}")
 
                    finally:
                        keepalive.cancel()
                        await asyncio.gather(keepalive, return_exceptions=True)
 
            except asyncio.CancelledError:
                break
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                self._ws_note(f"{symbol}_OB", connected=False, error=err)
                self._ws_note(f"{symbol}_FLOW", connected=False, error=err)
                logger.warning(f"OB WebSocket {symbol}: {err} — перепідключення...")
                full_bids.clear()
                full_asks.clear()
                await asyncio.sleep(5)
 
    async def _stream_candles(self, symbol: str, tf: str) -> None:
        ws_symbol = symbol.replace("/", "").replace(":USDT", "")
        url       = "wss://stream.bybit.com/v5/public/linear"
        interval  = self.WS_KLINE_INTERVAL.get(tf, tf)   # "1m"→"1", "1h"→"60"
        sub_msg   = json.dumps({"op": "subscribe", "args": [f"kline.{interval}.{ws_symbol}"]})
        key       = f"{symbol}_{tf}"
 
        while self._running:
            try:
                async with websockets.connect(
                    url,
                    # ping_interval=None СВІДОМО: Bybit вимагає app-рівневий
                    # {"op":"ping"} (наш keepalive шле його кожні 20с, сервер
                    # відповідає pong-ПОВІДОМЛЕННЯМ = «пульс»). Протокольні
                    # ping-фрейми сервер може ігнорувати — тоді бібліотека сама
                    # вбивала б здорове з'єднання кожні ~35с. Захист від зомбі
                    # (тихих обривів мережі Oracle) — вотчдог тиші нижче.
                    ping_interval = None,
                    ping_timeout  = None,
                    close_timeout = 10,
                    open_timeout  = 10,
                ) as ws:
                    await ws.send(sub_msg)
                    self._ws_note(key, connected=True, inc_connects=True)

                    keepalive = asyncio.create_task(
                        self._bybit_keepalive(ws, f"Candles {symbol} {tf}")
                    )

                    try:
                        first_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        first     = json.loads(first_raw)

                        if first.get("op") == "subscribe" and not first.get("success", True):
                            # Відхилена підписка (невалідний топік тощо) РАНІШЕ
                            # логувалась як "✅" і потік вічно висів без даних.
                            # Тепер — гучний фейл, видимий у /diag.
                            raise ConnectionError(
                                f"kline підписку відхилено: {first.get('ret_msg') or first}"
                            )
                        logger.info(
                            f"✅ Candles WebSocket: {symbol} {tf} "
                            f"(kline.{interval}.{ws_symbol})"
                        )

                        # Після (пере)підключення докачуємо пропущені закриті
                        # свічки через REST — обриви мережі Oracle не лишають
                        # дірок в історії.
                        await asyncio.to_thread(self._backfill_candles, symbol, tf)

                        last_msg = time.monotonic()
                        while self._running:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=25)
                            except asyncio.TimeoutError:
                                # Kline-топік шле апдейти щосекунди, pong на наш
                                # app-ping — кожні 20с. 75с повної тиші = зомбі
                                # (мережа тихо вбила TCP) → force reconnect.
                                if time.monotonic() - last_msg > 75:
                                    raise ConnectionError("kline WS: тиша >75с — force reconnect")
                                continue
                            last_msg = time.monotonic()
                            self._ws_note(key, inc_msgs=True)

                            data = json.loads(raw)
 
                            if data.get("op") == "pong":
                                continue
 
                            if data.get("topic", "").startswith("kline"):
                                for candle in data.get("data", []):
                                    c = [
                                        int(candle["start"]),
                                        float(candle["open"]),
                                        float(candle["high"]),
                                        float(candle["low"]),
                                        float(candle["close"]),
                                        float(candle["volume"]),
                                    ]
                                    # LIVE: кожен пуш (~1-3с) оновлює поточну
                                    # свічку — стратегії бачать ціну в реальному
                                    # часі, а не з запізненням до 60с.
                                    self.state.live_candles[key] = c
                                    self.state.ws_msg_at[key] = datetime.now(timezone.utc).timestamp()

                                    if candle.get("confirm", False):
                                        self._upsert_closed_candle(
                                            key, tf, c, exchange_confirmed=True
                                        )
                                        logger.debug(
                                            f"Закрита {symbol} {tf}: close={c[4]:.4f} vol={c[5]:.2f}"
                                        )
 
                    finally:
                        keepalive.cancel()
                        await asyncio.gather(keepalive, return_exceptions=True)

            except asyncio.CancelledError:
                break
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                self._ws_note(key, connected=False, error=err)
                logger.warning(f"Candles WebSocket {symbol} {tf}: {err} — reconnect за 5с")
                await asyncio.sleep(5)
    # ── Торговий цикл ─────────────────────────────────────────

    async def _trading_loop(self) -> None:
        logger.info("🔄 Торговий цикл запущено")

        while self._running:
            try:
                now = datetime.now(timezone.utc)
                self._check_daily_reset(now)

                # Pause/latch blocks only NEW entries. Existing or
                # closing-pending positions must still be reconciled.
                if self.state.open_trade:
                    await self._monitor_position()
                    await asyncio.sleep(10)
                    continue

                await self._paused.wait()
                if not self._running:
                    break

                now = datetime.now(timezone.utc)

                # Свіжість даних: замерзлі свічки → REST-перезавантаження
                await self._ensure_fresh_data()

                # ФІКС ДЕДДОКУ серії збитків: streak скидався ЛИШЕ виграшем,
                # але заблокований бот не торгує → не виграє → вічний стоп.
                # Тепер після паузи COOLDOWN_AFTER_SERIES_MIN серія скидається.
                if (self.state.loss_streak >= MAX_CONSECUTIVE_LOSSES
                        and self.state.last_loss_time is not None):
                    mins = (now - self.state.last_loss_time).total_seconds() / 60
                    if mins >= COOLDOWN_AFTER_SERIES_MIN:
                        logger.info(
                            f"⏯ Серію з {self.state.loss_streak} збитків скинуто "
                            f"після паузи {COOLDOWN_AFTER_SERIES_MIN} хв — торгуємо далі"
                        )
                        self.state.loss_streak = 0

                now_mono = time.monotonic()
                if now_mono - self._last_daily_risk_sync >= DAILY_RISK_SYNC_INTERVAL_SEC:
                    try:
                        rows = await asyncio.to_thread(
                            self._fetch_today_closed_pnl_rows
                        )
                        if len(rows) < self.state.daily_trades:
                            raise RuntimeError(
                                f"daily history incomplete: {len(rows)} < "
                                f"{self.state.daily_trades}"
                            )
                        self._restore_daily_risk_state(rows, log_result=False)
                        self._last_daily_risk_sync = now_mono
                        self._daily_risk_sync_failures = 0
                    except Exception as e:
                        self._last_daily_risk_sync = now_mono
                        self._daily_risk_sync_failures += 1
                        self._entry_block_until = max(
                            self._entry_block_until,
                            now_mono + DAILY_RISK_SYNC_INTERVAL_SEC,
                        )
                        logger.warning(
                            "Periodic daily risk sync failed "
                            f"({self._daily_risk_sync_failures}/"
                            f"{DAILY_RISK_SYNC_FAILURE_LIMIT}): {e}"
                        )
                        if self._daily_risk_sync_failures >= DAILY_RISK_SYNC_FAILURE_LIMIT:
                            self._trip_safety_latch("periodic daily risk sync failed")
                            await self._notify(
                                "send_alert",
                                "🆘 Не вдалося синхронізувати денний PnL "
                                f"{self._daily_risk_sync_failures} рази поспіль; "
                                "safety latch активний.",
                            )
                        continue
                if now_mono < self._entry_block_until:
                    await asyncio.sleep(1)
                    continue

                # Денні ліміти
                limits = check_daily_limits(
                    daily_pnl   = self.state.daily_pnl,
                    trade_count = self.state.daily_trades,
                    loss_streak = self.state.loss_streak,
                    deposit     = self.state.deposit,
                )
                if not limits["can_trade"]:
                    for reason in limits["reasons"]:
                        logger.warning(f"⛔ {reason}")
                    await asyncio.sleep(60)
                    continue

                # Cooldown після збитку
                if self.state.last_loss_time:
                    elapsed = (now - self.state.last_loss_time).total_seconds() / 60
                    if elapsed < COOLDOWN_AFTER_LOSS_MIN:
                        logger.debug(f"Cooldown: ще {COOLDOWN_AFTER_LOSS_MIN - elapsed:.1f} хв")
                        await asyncio.sleep(30)
                        continue

                # Cooldown між угодами
                if self.state.last_trade_time:
                    elapsed = (now - self.state.last_trade_time).total_seconds() / 60
                    if elapsed < MIN_CANDLES_BETWEEN_TRADES:
                        await asyncio.sleep(15)
                        continue

                # Торгові сесії: поза вікном [START,END) UTC нові входи
                # блокуємо (тонка ліквідність вночі → чоп і фальшиві рухи).
                # Відкрита позиція вище вже оброблена (моніторинг завжди).
                if TRADE_HOURS_ONLY and not (TRADE_HOUR_START <= now.hour < TRADE_HOUR_END):
                    await asyncio.sleep(30)
                    continue

                # Перевіряємо сигнали
                for symbol in TRADING_PAIRS:
                    signal = await self._check_signal(symbol)
                    if signal:
                        await self._execute_trade(signal)
                        break

                # 3с: live-свічки оновлюються кожні ~1-3с (пуш Bybit),
                # тож повна реакція бота на рух ціни ≤ ~3-6 секунд.
                await asyncio.sleep(3)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Помилка торгового циклу: {e}")
                await asyncio.sleep(30)

    # ── Сигнал + OB підтвердження ─────────────────────────────

    async def _check_signal(self, symbol: str) -> Optional[dict]:
        """
        Перевіряє сигнал і підтверджує через order book.

        Порядок:
          1. DataFrame зі свічок
          2. Оновлення кешу рейнджу (раз на годину)
          3. generate_scalp_signal() — Режим A
          4. generate_dual_tf_signal() — якщо A не дав
          5. OB підтвердження: дисбаланс + відсутність стіни
        """
        # Усі ТФ, що впливають на рішення, повинні мати актуальну закриту свічку.
        for tf in self._required_timeframes():
            candle_lag = self._closed_candle_lag_sec(symbol, tf)
            if candle_lag is None or candle_lag > 0:
                logger.warning(
                    f"{symbol}: закриті {tf} дані протухли "
                    f"(lag={candle_lag if candle_lag is not None else 'нема'}с) — skip"
                )
                return None

        dfs = {
            tf: self._get_df(symbol, tf, closed_only=SIGNALS_USE_CLOSED_CANDLES)
            for tf in self._required_timeframes()
        }
        df_1h = dfs.get("1h")
        df_30m = dfs.get("30m")
        df_5m = dfs.get("5m")
        df_1m = dfs.get("1m")

        if df_1m is None or len(df_1m) < 25:
            return None

        self._update_range_cache(symbol, df_1h, df_30m)

        # ── Диспетчер стратегій за пріоритетом ──────────────────
        # Перша стратегія зі STRATEGY_PRIORITY, що дала валідний сигнал —
        # перемагає. Усі три незалежні: sweep / meanrev / vwap.
        signal = None
        for name in STRATEGY_PRIORITY:
            name = name.strip().lower()
            if name == "trend" and USE_TREND_STRATEGY:
                signal = generate_trend_signal(dfs, symbol)
            elif name == "sweep" and USE_SWEEP_STRATEGY:
                signal = generate_scalp_signal(
                    df_1h=df_1h, df_5m=df_5m, df_1m=df_1m,
                    symbol=symbol, cached_range=self.state.cached_range_1h, mode="A",
                )
            elif name == "meanrev" and USE_MEANREV_STRATEGY:
                signal = generate_meanrev_signal(dfs, symbol)
            elif name == "vwap" and USE_VWAP_STRATEGY:
                signal = generate_vwap_signal(dfs, symbol)
            else:
                continue
            if signal is not None:
                break

        # DUAL TF — опційний легасі-фолбек (вимкнений за замовчуванням)
        if signal is None and USE_DUAL_TF_STRATEGY and df_30m is not None:
            signal = generate_dual_tf_signal(
                df_1h            = df_1h,
                df_30m           = df_30m,
                df_5m            = df_5m,
                df_1m            = df_1m,
                symbol           = symbol,
                cached_1h_range  = self.state.cached_range_1h,
                cached_30m_range = self.state.cached_range_30m,
            )

        if signal is None:
            return None

        self.state.exec_stats["signals"] += 1

        # ── FTA: проблемна зона старшого ТФ між входом і TP ──────
        # Бот «бачить» найближчу зустрічну зону HTF. За USE_FTA_FILTER=false
        # лише позначає (для сповіщення/логів); =true — ріже такі угоди.
        self._annotate_fta(signal, dfs.get(FTA_TF))
        if USE_FTA_FILTER and signal.get("fta_blocks_tp"):
            self.state.exec_stats["fta_blocked"] = (
                self.state.exec_stats.get("fta_blocked", 0) + 1
            )
            _fp = signal.get("fta_price") or 0.0
            _fd = signal.get("fta_dist_pct") or 0.0
            logger.info(
                f"{symbol}: TP за проблемною зоною HTF "
                f"(FTA={_fp:.2f} dist={_fd:.2f}%) — skip"
            )
            return None

                # ── Order Book confirmation / logging ───────────────
        #
        # Нова логіка:
        # - OB imbalance НЕ є обов'язковим фільтром за замовчуванням.
        # - Велика стіна проти входу залишається hard-filter.
        # - Якщо USE_ORDER_BOOK_CONFIRMATION=true, тоді OB direction знову буде hard-filter.
        #
        with self._ob_lock:
            ob = self.state.ob_snapshots.get(symbol)

        direction = signal["direction"]
        strat = str(signal.get("strategy", ""))

        # Для sweep збираємо depth/flow ПІСЛЯ першого виявлення setup.
        gate_since_mono = None
        if strat == "sweep" and (SWEEP_USE_OB_CONFIRM or SWEEP_REQUIRE_TRADE_FLOW):
            setup_key = self._signal_key(signal, dfs)
            pending = self.state.pending_sweeps.get(symbol)
            now_mono = time.monotonic()
            if pending is None or pending.get("key") != setup_key:
                self.state.pending_sweeps[symbol] = {
                    "key": setup_key,
                    "first_seen_mono": now_mono,
                }
                logger.debug(f"{symbol}: sweep pending {setup_key} — збираю OB/flow")
                return None
            if pending.get("terminal"):
                return None
            elapsed = now_mono - float(pending["first_seen_mono"])
            if elapsed < OB_PERSISTENCE_MIN_SEC:
                return None
            if elapsed > OB_PERSISTENCE_WINDOW_SEC + 2.0:
                pending["terminal"] = "expired"
                logger.debug(f"{symbol}: sweep pending expired {setup_key}")
                return None
            gate_since_mono = float(pending["first_seen_mono"])

        # OB як ПІДТВЕРДЖЕННЯ напрямку: вмикається глобально
        # (USE_ORDER_BOOK_CONFIRMATION) АБО адресно для sweep
        # (SWEEP_USE_OB_CONFIRM). Для sweep це критичний «підпис»
        # розвороту: після зняття ліквідності перекіс стакана має бути
        # в бік входу (абсорбція пасивними заявками).
        need_ob_confirm = USE_ORDER_BOOK_CONFIRMATION or (
            strat == "sweep" and SWEEP_USE_OB_CONFIRM
        )
        signal["ob_required"] = need_ob_confirm
        need_ob_data = need_ob_confirm or USE_ORDER_BOOK_WALL_FILTER

        if ob is None:
            # Якщо OB — обов'язкове підтвердження, а даних немає → не входимо
            # (краще пропустити sweep, ніж торгувати наосліп).
            if need_ob_data:
                self.state.exec_stats["obdir_blocked"] += 1
                logger.debug(f"{symbol}: увімкнений OB gate, а даних нема — skip")
                return None
            logger.debug(f"{symbol}: немає order book даних — продовжуємо без OB")
            signal["ob_imbalance"] = None
            signal["ob_bid_total"] = None
            signal["ob_ask_total"] = None
            return self._deduplicate_signal(signal, dfs)

        ob_age = (datetime.now(timezone.utc) - ob.timestamp).total_seconds()

        if ob_age > OB_MAX_AGE_SECONDS:
            if need_ob_data:
                self.state.exec_stats["obdir_blocked"] += 1
                logger.debug(f"{symbol}: увімкнений OB gate, а він застарів ({ob_age:.1f}с) — skip")
                return None
            logger.debug(
                f"{symbol}: order book застарів ({ob_age:.1f}с) — продовжуємо без OB"
            )
            signal["ob_imbalance"] = ob.imbalance
            signal["ob_bid_total"] = ob.bid_total
            signal["ob_ask_total"] = ob.ask_total
            signal["ob_stale"] = True
            return self._deduplicate_signal(signal, dfs)

        persistence_rows = self._recent_ob_snapshots(
            symbol, since_mono=gate_since_mono
        )
        if need_ob_data and not self._history_is_persistent(persistence_rows):
            self.state.exec_stats["obdir_blocked"] += 1
            logger.debug(f"{symbol}: OB history ще не persistent — skip")
            return None

        ob_direction = self._get_ob_signal(symbol, since_mono=gate_since_mono)

        # Hard-filter тільки для великої стіни проти входу
        if USE_ORDER_BOOK_WALL_FILTER and self._has_persistent_wall_against(
            symbol, direction, signal["entry"], since_mono=gate_since_mono
        ):
            self.state.exec_stats["wall_blocked"] += 1
            logger.info(f"{symbol}: велика стіна проти {direction.upper()} — skip")
            return None

        # Direction-фільтр: глобально або адресно для sweep
        if need_ob_confirm and ob_direction != direction:
            self.state.exec_stats["obdir_blocked"] += 1
            logger.debug(
                f"{symbol}: OB не підтверджує {direction.upper()} "
                f"(OB: {ob_direction}, strat={strat}) | {ob.summary()}"
            )
            return None

        flow_direction, flow = self._get_trade_flow_signal(
            symbol, since_mono=gate_since_mono
        )
        if strat == "sweep" and SWEEP_REQUIRE_TRADE_FLOW and flow_direction != direction:
            self.state.exec_stats["flow_blocked"] += 1
            logger.debug(
                f"{symbol}: executed flow не підтверджує {direction.upper()} "
                f"(flow={flow_direction}, imbalance={flow['imbalance']:+.1f}%, "
                f"notional=${flow['total']:.0f}) — skip"
            )
            return None

        signal["ob_imbalance"] = ob.imbalance
        signal["ob_bid_total"] = ob.bid_total
        signal["ob_ask_total"] = ob.ask_total
        signal["ob_direction"] = ob_direction
        signal["ob_confirmed"] = ob_direction == direction
        signal["trade_flow_direction"] = flow_direction
        signal["trade_flow_imbalance"] = flow["imbalance"]
        signal["trade_flow_notional"] = flow["total"]
        if strat == "sweep":
            pending = self.state.pending_sweeps.get(symbol)
            if pending is not None:
                pending["terminal"] = "accepted"

        logger.info(
            f"✅ SIGNAL ACCEPTED {direction.upper()} {symbol} | "
            f"OB={ob_direction} imbalance={ob.imbalance:+.1f}% | "
            f"hard_ob={need_ob_confirm}"
        )

        return self._deduplicate_signal(signal, dfs)

    def _annotate_fta(self, signal: dict, df_htf) -> None:
        """
        Рахує FTA (найближчу проблемну зону HTF між входом і TP) і кладе в
        сигнал поля: fta_price / fta_blocks_tp / fta_dist_pct. Ніколи не
        ламає торгівлю — при будь-якій помилці просто лишає None.
        """
        signal["fta_price"] = None
        signal["fta_blocks_tp"] = False
        signal["fta_dist_pct"] = None
        try:
            res = first_trouble_area(
                df_htf,
                direction=signal["direction"],
                entry=float(signal["entry"]),
                tp=float(signal["tp"]),
                lookback=FTA_SWING_LOOKBACK,
                buffer_pct=FTA_BUFFER_PCT,
            )
        except Exception as e:
            logger.debug(f"FTA calc failed: {e}")
            return
        if not res:
            return
        signal["fta_price"] = res["fta"]
        signal["fta_blocks_tp"] = res["blocks_tp"]
        signal["fta_dist_pct"] = res["dist_pct"]
        if res["blocks_tp"]:
            logger.info(
                f"{signal['symbol']}: ⚠️ FTA {signal['direction'].upper()} "
                f"зона={res['fta']:.2f} ({res['dist_pct']:.2f}%) МІЖ входом і TP"
            )

    def _recent_ob_snapshots(
        self, symbol: str, since_mono: Optional[float] = None
    ) -> list[OrderBookSnapshot]:
        now_mono = time.monotonic()
        with self._ob_lock:
            rows = list(self.state.ob_snapshot_history.get(symbol, []))
        return [
            row for row in rows
            if 0 <= now_mono - row.received_mono <= OB_PERSISTENCE_WINDOW_SEC
            and (since_mono is None or row.received_mono >= since_mono)
        ]

    @staticmethod
    def _history_is_persistent(rows: list[OrderBookSnapshot]) -> bool:
        if len(rows) < OB_PERSISTENCE_MIN_SAMPLES:
            return False
        span = rows[-1].received_mono - rows[0].received_mono
        latest_age = time.monotonic() - rows[-1].received_mono
        gaps = [
            right.received_mono - left.received_mono
            for left, right in zip(rows, rows[1:])
        ]
        return (
            span >= OB_PERSISTENCE_MIN_SEC
            and latest_age <= 0.75
            and (not gaps or max(gaps) <= 0.75)
        )

    def _get_ob_signal(
        self, symbol: str, since_mono: Optional[float] = None
    ) -> Optional[str]:
        """Напрямок лише коли imbalance тримається у кількох updates 1–3 секунди."""
        import statistics

        rows = self._recent_ob_snapshots(symbol, since_mono=since_mono)
        if not self._history_is_persistent(rows):
            return None

        values = [row.imbalance for row in rows]
        avg = sum(values) / len(values)
        median = statistics.median(values)
        long_ratio = sum(v > OB_IMBALANCE_LONG_MIN for v in values) / len(values)
        short_ratio = sum(v < OB_IMBALANCE_SHORT_MAX for v in values) / len(values)
        logger.debug(
            f"{symbol} OB persistent {len(values)} samples: avg={avg:+.1f}% "
            f"median={median:+.1f}% long={long_ratio:.0%} short={short_ratio:.0%}"
        )
        if (
            long_ratio >= OB_PERSISTENCE_MIN_RATIO
            and avg > OB_IMBALANCE_LONG_MIN * 0.7
            and median > 0
            and values[-1] > OB_IMBALANCE_LONG_MIN
        ):
            return "long"
        if (
            short_ratio >= OB_PERSISTENCE_MIN_RATIO
            and avg < OB_IMBALANCE_SHORT_MAX * 0.7
            and median < 0
            and values[-1] < OB_IMBALANCE_SHORT_MAX
        ):
            return "short"
        return None

    def _has_persistent_wall_against(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        since_mono: Optional[float] = None,
    ) -> bool:
        rows = self._recent_ob_snapshots(symbol, since_mono=since_mono)
        if not self._history_is_persistent(rows):
            return False
        ratio = sum(row.has_wall_against(direction, entry_price) for row in rows) / len(rows)
        return (
            ratio >= OB_PERSISTENCE_MIN_RATIO
            and rows[-1].has_wall_against(direction, entry_price)
        )

    def _get_trade_flow_signal(
        self, symbol: str, since_mono: Optional[float] = None
    ) -> tuple[Optional[str], dict]:
        """Executed taker flow з publicTrade; displayed depth тут не використовується."""
        now_mono = time.monotonic()
        with self._ob_lock:
            rows = list(self.state.trade_flow_history.get(symbol, []))
        recent_rows = [
            (recv_mono, exchange_ts, signed)
            for recv_mono, exchange_ts, signed in rows
            if 0 <= now_mono - recv_mono <= TRADE_FLOW_LOOKBACK_SEC
            and (since_mono is None or recv_mono >= since_mono)
        ]
        recent = [signed for _, _, signed in recent_rows]
        buy = sum(v for v in recent if v > 0)
        sell = -sum(v for v in recent if v < 0)
        total = buy + sell
        imbalance = (buy - sell) / total * 100 if total > 0 else 0.0
        latest_age = now_mono - recent_rows[-1][0] if recent_rows else None
        meta = {
            "buy": buy, "sell": sell, "total": total, "imbalance": imbalance,
            "trades": len(recent_rows), "latest_age": latest_age,
        }
        if latest_age is None or latest_age > 0.75 or total < TRADE_FLOW_MIN_NOTIONAL:
            return None, meta
        if imbalance >= TRADE_FLOW_IMBALANCE_MIN:
            return "long", meta
        if imbalance <= -TRADE_FLOW_IMBALANCE_MIN:
            return "short", meta
        return None, meta

    @staticmethod
    def _signal_key(signal: dict, dfs: dict) -> Optional[str]:
        strategy = str(signal.get("strategy") or signal.get("mode") or "unknown")
        cfg = SYMBOL_CONFIG.get(signal.get("symbol"), {})
        if strategy == "trend":
            tf = cfg.get("trend", {}).get("entry_tf", "5m")
        elif strategy == "vwap":
            tf = cfg.get("vwap", {}).get("tf", "5m")
        elif strategy == "meanrev":
            tf = cfg.get("meanrev", {}).get("tf", "5m")
        else:
            tf = "1m"
        df = dfs.get(tf)
        if df is None or df.empty:
            return None
        ts = int(df.index[-1].timestamp() * 1000)
        return (
            f"{signal.get('symbol')}:{strategy}:"
            f"{signal.get('direction')}:{tf}:{ts}"
        )

    def _deduplicate_signal(self, signal: dict, dfs: dict) -> Optional[dict]:
        if not SIGNAL_DEDUP_ENABLED:
            return signal
        key = self._signal_key(signal, dfs)
        if key is None:
            return signal
        strategy = str(signal.get("strategy") or signal.get("mode") or "unknown")
        scope = f"{signal.get('symbol')}:{strategy}"
        if self.state.last_signal_keys.get(scope) == key:
            self.state.exec_stats["dedup_blocked"] += 1
            logger.debug(f"{signal.get('symbol')}: duplicate setup {key} — skip")
            return None
        self.state.last_signal_keys[scope] = key
        signal["signal_key"] = key
        return signal

    # ── Виконання ордера ──────────────────────────────────────

    @staticmethod
    def _order_link_id(signal: dict, suffix: str = "") -> str:
        setup = str(signal.get("signal_key") or signal.get("signal_time") or repr(signal))
        digest = hashlib.sha1(setup.encode("utf-8")).hexdigest()[:24]
        return f"bt-{digest}{suffix}"[:36]

    @staticmethod
    def _adverse_sizing_entry(
        signal_entry: Decimal,
        actionable_entry: Decimal,
        direction: str,
        max_drift_bps: float,
    ) -> Decimal:
        """Price used for sizing so every allowed fill remains inside risk budget."""
        drift = Decimal(str(max_drift_bps)) / Decimal("10000")
        boundary = signal_entry * (
            Decimal("1") + drift if direction == "long" else Decimal("1") - drift
        )
        return (
            max(actionable_entry, boundary)
            if direction == "long"
            else min(actionable_entry, boundary)
        )

    @staticmethod
    def _full_tpsl_params(tp: float, sl: float) -> dict:
        return {
            "takeProfit": tp,
            "stopLoss": sl,
            "tpslMode": "Full",
            "tpOrderType": "Market",
            "slOrderType": "Market",
        }

    def _set_full_position_protection(self, symbol: str, tp: float, sl: float) -> dict:
        pybit = getattr(self, "_pybit", None)
        if pybit is None:
            raise RuntimeError("pybit session unavailable for position TP/SL")
        ws_symbol = symbol.replace("/", "").replace(":USDT", "")
        response = pybit.set_trading_stop(
            category="linear",
            symbol=ws_symbol,
            positionIdx=0,
            takeProfit=str(tp),
            stopLoss=str(sl),
            tpslMode="Full",
            tpOrderType="Market",
            slOrderType="Market",
        )
        if str(response.get("retCode", 0)) != "0":
            raise RuntimeError(
                f"set_trading_stop retCode={response.get('retCode')}: "
                f"{response.get('retMsg')}"
            )
        return response

    def _lookup_order_by_link_id(self, symbol: str, order_link_id: str) -> Optional[dict]:
        """Reconcile невизначений submit; не дозволяє другий market-вхід."""
        pybit = getattr(self, "_pybit", None)
        if pybit is None:
            return None
        ws_symbol = symbol.replace("/", "").replace(":USDT", "")
        for method_name in ("get_open_orders", "get_order_history"):
            method = getattr(pybit, method_name, None)
            if method is None:
                continue
            try:
                resp = method(
                    category="linear", symbol=ws_symbol,
                    orderLinkId=order_link_id, limit=10,
                )
                rows = resp.get("result", {}).get("list", [])
                if rows:
                    return rows[0]
            except Exception as e:
                logger.debug(f"Order reconcile {method_name}: {e}")
        return None

    def _fetch_execution_summary(self, symbol: str, order_id: str) -> Optional[dict]:
        pybit = getattr(self, "_pybit", None)
        if pybit is None or not order_id:
            return None
        ws_symbol = symbol.replace("/", "").replace(":USDT", "")
        try:
            resp = pybit.get_executions(
                category="linear", symbol=ws_symbol, orderId=order_id, limit=100,
            )
            rows = resp.get("result", {}).get("list", [])
        except Exception as e:
            logger.debug(f"Executions {order_id}: {e}")
            return None
        fills = []
        for row in rows:
            try:
                qty = float(row.get("execQty") or 0)
                price = float(row.get("execPrice") or 0)
                if qty <= 0 or price <= 0:
                    continue
                maker_raw = row.get("isMaker", False)
                is_maker = (
                    maker_raw is True
                    or str(maker_raw).strip().lower() in {"1", "true", "yes"}
                )
                fills.append({
                    "qty": qty,
                    "price": price,
                    "fee": float(row.get("execFee") or 0),
                    "is_maker": is_maker,
                    "time_ms": int(row.get("execTime") or 0),
                    "exec_id": row.get("execId"),
                })
            except (TypeError, ValueError):
                continue
        if not fills:
            return None
        qty = sum(row["qty"] for row in fills)
        return {
            "qty": qty,
            "vwap": sum(row["price"] * row["qty"] for row in fills) / qty,
            "fee": sum(row["fee"] for row in fills),
            "maker_qty": sum(row["qty"] for row in fills if row["is_maker"]),
            "first_fill_ms": min(row["time_ms"] for row in fills),
            "last_fill_ms": max(row["time_ms"] for row in fills),
            "fills": fills,
        }

    def _fetch_closed_pnl(
        self,
        symbol: str,
        opened_at_ms: int,
        expected_entry: Optional[float] = None,
        expected_qty: Optional[float] = None,
    ) -> Optional[dict]:
        pybit = getattr(self, "_pybit", None)
        if pybit is None:
            return None
        ws_symbol = symbol.replace("/", "").replace(":USDT", "")
        try:
            resp = pybit.get_closed_pnl(
                category="linear",
                symbol=ws_symbol,
                startTime=max(0, opened_at_ms - 5_000),
                endTime=int(datetime.now(timezone.utc).timestamp() * 1000),
                limit=50,
            )
            rows = resp.get("result", {}).get("list", [])
        except Exception as e:
            logger.debug(f"Closed PnL fetch: {e}")
            return None
        eligible = []
        for row in rows:
            try:
                ts = int(row.get("updatedTime") or row.get("createdTime") or 0)
                if ts < opened_at_ms - 5_000:
                    continue
                row_entry = float(row.get("avgEntryPrice") or 0)
                # closedSize є фактично закритою кількістю; qty може бути
                # початковим розміром позиції для кожного partial-close рядка.
                row_qty = float(row.get("closedSize") or row.get("qty") or 0)
                if expected_entry:
                    if row_entry <= 0 or abs(row_entry - expected_entry) / expected_entry > 0.001:
                        continue
                if expected_qty and row_qty <= 0:
                    continue
                eligible.append((ts, row, row_qty))
            except (TypeError, ValueError):
                continue
        if not eligible:
            return None
        try:
            selected = eligible
            if expected_qty:
                qty_tolerance = max(1e-9, expected_qty * 0.01)
                exact = [
                    item for item in eligible
                    if abs(item[2] - expected_qty) <= qty_tolerance
                ]
                if exact:
                    selected = [max(exact, key=lambda item: item[0])]
                elif abs(sum(item[2] for item in eligible) - expected_qty) > qty_tolerance:
                    return None
            else:
                selected = [max(eligible, key=lambda item: item[0])]

            qty = sum(item[2] for item in selected)
            if qty <= 0:
                return None
            return {
                "pnl": sum(float(item[1].get("closedPnl") or 0) for item in selected),
                "avg_entry": sum(
                    float(item[1].get("avgEntryPrice") or 0) * item[2]
                    for item in selected
                ) / qty,
                "avg_exit": sum(
                    float(item[1].get("avgExitPrice") or 0) * item[2]
                    for item in selected
                ) / qty,
                "open_fee": sum(
                    float(item[1].get("openFee") or 0) for item in selected
                ),
                "close_fee": sum(
                    float(item[1].get("closeFee") or 0) for item in selected
                ),
                "qty": qty,
                "raw": selected[0][1] if len(selected) == 1 else [item[1] for item in selected],
            }
        except (TypeError, ValueError):
            return None

    def _fetch_today_closed_pnl_rows(self) -> list[dict]:
        pybit = getattr(self, "_pybit", None)
        if pybit is None:
            raise RuntimeError("pybit session unavailable")
        now = datetime.now(timezone.utc)
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        result: list[dict] = []
        for symbol in TRADING_PAIRS:
            ws_symbol = symbol.replace("/", "").replace(":USDT", "")
            resp = pybit.get_closed_pnl(
                category="linear",
                symbol=ws_symbol,
                startTime=start_ms,
                endTime=end_ms,
                limit=100,
            )
            rows = resp.get("result", {}).get("list", [])
            for row in rows:
                ts = int(row.get("updatedTime") or row.get("createdTime") or 0)
                if start_ms <= ts <= end_ms:
                    result.append(row)
        return result

    def _fetch_recent_closed_pnl_rows(self) -> list[dict]:
        pybit = getattr(self, "_pybit", None)
        if pybit is None:
            raise RuntimeError("pybit session unavailable")
        result: list[dict] = []
        for symbol in TRADING_PAIRS:
            ws_symbol = symbol.replace("/", "").replace(":USDT", "")
            resp = pybit.get_closed_pnl(
                category="linear", symbol=ws_symbol, limit=50
            )
            result.extend(resp.get("result", {}).get("list", []))
        return result

    def _restore_daily_risk_state(self, rows: list[dict], *, log_result: bool = True) -> None:
        parsed = []
        for row in rows:
            try:
                ts = int(row.get("updatedTime") or row.get("createdTime") or 0)
                pnl = Decimal(str(row.get("closedPnl") or 0))
                parsed.append((ts, pnl))
            except (TypeError, ValueError):
                continue
        parsed.sort(key=lambda item: item[0])
        self.state.daily_pnl = sum((pnl for _, pnl in parsed), Decimal("0"))
        self.state.daily_trades = len(parsed)
        if parsed:
            last_ts = parsed[-1][0]
            self.state.last_trade_time = datetime.fromtimestamp(
                last_ts / 1000, tz=timezone.utc
            )
            self.state.loss_streak = 0
            self.state.last_loss_time = None
            for ts, pnl in parsed:
                if pnl < 0:
                    self.state.loss_streak += 1
                    self.state.last_loss_time = datetime.fromtimestamp(
                        ts / 1000, tz=timezone.utc
                    )
                else:
                    self.state.loss_streak = 0
                    self.state.last_loss_time = None
        if log_result:
            logger.info(
                f"♻️ Денний risk-state: trades={self.state.daily_trades}, "
                f"PnL=${self.state.daily_pnl:.2f}, streak={self.state.loss_streak}"
            )

    def _restore_recent_outcomes(self, rows: list[dict]) -> None:
        parsed = []
        for row in rows:
            try:
                ts = int(row.get("updatedTime") or row.get("createdTime") or 0)
                pnl = Decimal(str(row.get("closedPnl") or 0))
                parsed.append((ts, pnl))
            except (TypeError, ValueError):
                continue
        parsed.sort(key=lambda item: item[0])
        self.state.recent_outcomes = deque(
            (1 if pnl > 0 else 0 for _, pnl in parsed if pnl != 0),
            maxlen=50,
        )
        streak = 0
        last_loss_time = None
        for ts, pnl in reversed(parsed):
            if pnl >= 0:
                break
            streak += 1
            if last_loss_time is None:
                last_loss_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        if (
            streak >= MAX_CONSECUTIVE_LOSSES
            and last_loss_time is not None
            and (datetime.now(timezone.utc) - last_loss_time).total_seconds()
                >= COOLDOWN_AFTER_SERIES_MIN * 60
        ):
            streak = 0
            last_loss_time = None
        self.state.loss_streak = streak
        self.state.last_loss_time = last_loss_time

    @staticmethod
    def _initialize_trade_db() -> None:
        with Database() as db:
            db.initialize_tables()

    @staticmethod
    def _find_persisted_open_trade_id(
        symbol: str,
        direction: str,
        entry_price: float,
        quantity: float,
        opened_at: datetime,
    ) -> Optional[str]:
        with Database() as db:
            row = db.get_latest_open_trade(
                symbol,
                direction,
                entry_price=entry_price,
                quantity=quantity,
                opened_after=opened_at - timedelta(minutes=10),
            )
        return str(row["trade_id"]) if row and row.get("trade_id") else None

    @staticmethod
    def _save_trade_open_db(trade: dict) -> None:
        if not ENABLE_TRADE_DB_LOG:
            return
        payload = {
            "trade_id": str(trade.get("order_id") or trade.get("signal_key")),
            "exchange": "bybit",
            "symbol": trade["symbol"],
            "direction": trade["direction"],
            "mode": str(trade.get("strategy") or trade.get("mode") or "unknown")[:10],
            "entry_price": trade["entry"],
            "stop_loss": trade["sl"],
            "take_profit": trade["tp"],
            "quantity": trade["qty"],
            "entry_reason": {
                "signal_key": trade.get("signal_key"),
                "raw_rr": trade.get("raw_rr"),
                "ob_imbalance": trade.get("ob_imbalance"),
                "trade_flow_imbalance": trade.get("trade_flow_imbalance"),
                "execution": trade.get("execution"),
            },
            "status": "open",
            "opened_at": trade["opened_at"],
        }
        with Database() as db:
            db.save_trade(payload)

    @staticmethod
    def _save_trade_close_db(trade: dict, pnl: float, exit_price: float) -> None:
        if not ENABLE_TRADE_DB_LOG:
            return
        trade_id = str(trade.get("order_id") or trade.get("signal_key"))
        notional = abs(float(trade.get("entry") or 0) * float(trade.get("qty") or 0))
        pnl_pct = pnl / notional * 100 if notional > 0 else 0.0
        with Database() as db:
            db.close_trade(trade_id, exit_price, pnl, pnl_pct)

    async def _wait_execution_summary(
        self, symbol: str, order_id: str, requested_qty: float, timeout: float = 5.0
    ) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            latest = await asyncio.to_thread(
                self._fetch_execution_summary, symbol, order_id
            )
            if latest and latest["qty"] >= requested_qty - 1e-12:
                return latest
            await asyncio.sleep(0.25)
        return latest

    async def _cancel_entry_order(
        self, symbol: str, order_id: str, order_link_id: str
    ) -> bool:
        """Cancel maker remainder and prove a terminal state before market fallback."""
        terminal = {"cancelled", "canceled", "filled", "rejected", "deactivated"}
        cancel_error = None
        try:
            # ACK is asynchronous; it is not proof that leavesQty reached zero.
            await asyncio.to_thread(self.rest.cancel_order, order_id, symbol)
        except Exception as e:
            cancel_error = e

        for attempt in range(12):
            row = await asyncio.to_thread(
                self._lookup_order_by_link_id, symbol, order_link_id
            )
            if row:
                status = str(row.get("orderStatus") or row.get("status") or "").lower()
                try:
                    raw_leaves = row.get("leavesQty")
                    leaves = (
                        float(raw_leaves)
                        if raw_leaves not in (None, "")
                        else -1.0
                    )
                except (TypeError, ValueError):
                    leaves = -1.0
                if status in terminal or leaves == 0.0:
                    return True
            if attempt in {3, 7}:
                try:
                    await asyncio.to_thread(self.rest.cancel_order, order_id, symbol)
                    cancel_error = None
                except Exception as e:
                    cancel_error = e
            await asyncio.sleep(0.25)
        logger.error(
            f"Maker cancel не досяг terminal state: {order_link_id}; "
            f"last_error={cancel_error}"
        )
        return False

    async def _try_maker_entry(
        self,
        *,
        symbol: str,
        side: str,
        direction: str,
        qty: float,
        tp: float,
        sl: float,
        signal: dict,
        ob: Optional[OrderBookSnapshot],
    ) -> tuple[Optional[dict], Optional[dict]]:
        """PostOnly + TTL. Partial fill приймається; zero-fill може піти у market fallback."""
        if not USE_MAKER_ENTRY or ob is None or not ob.bids or not ob.asks:
            return None, None
        price = float(ob.bids[0][0]) if direction == "long" else float(ob.asks[0][0])
        link_id = self._order_link_id(signal, "-m")
        try:
            order = await asyncio.to_thread(
                self.rest.create_order,
                symbol=symbol,
                type="limit",
                side=side,
                amount=qty,
                price=price,
                params={
                    "positionIdx": 0,
                    "orderLinkId": link_id,
                    "timeInForce": "PostOnly",
                    "postOnly": True,
                    **self._full_tpsl_params(tp, sl),
                },
            )
        except Exception as e:
            transport_error = isinstance(
                e, (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable)
            )
            recovered = await asyncio.to_thread(
                self._lookup_order_by_link_id, symbol, link_id
            )
            if transport_error and not recovered:
                for _ in range(3):
                    await asyncio.sleep(0.5)
                    recovered = await asyncio.to_thread(
                        self._lookup_order_by_link_id, symbol, link_id
                    )
                    if recovered:
                        break
            if recovered:
                order = {
                    "id": recovered.get("orderId"),
                    "average": recovered.get("avgPrice") or None,
                    "filled": recovered.get("cumExecQty") or 0,
                    "info": recovered,
                }
                logger.warning(f"Maker submit відновлено за orderLinkId={link_id}")
            elif transport_error:
                self._trip_safety_latch(f"unknown maker submit state: {link_id}")
                await self._notify(
                    "send_alert",
                    f"🆘 *Невідомий стан maker entry* `{link_id}`. "
                    "Бот поставлено на паузу; market fallback заборонено.",
                )
                raise RuntimeError(f"unknown maker submit state: {link_id}")
            else:
                logger.warning(f"PostOnly entry явно відхилено: {e}")
                return None, None

        summary = await self._wait_execution_summary(
            symbol, str(order.get("id") or ""), qty, timeout=MAKER_ENTRY_TTL_SEC
        )
        if summary and summary["qty"] >= qty - 1e-12:
            self.state.exec_stats["maker_fills"] += 1
            return order, summary

        # TTL минув або fill частковий: спочатку скасовуємо залишок. Market
        # fallback дозволений лише після доведеного terminal/cancel стану.
        cancelled = await self._cancel_entry_order(
            symbol, str(order.get("id") or ""), link_id
        )
        if not cancelled:
            self._trip_safety_latch(f"unknown maker cancel state: {link_id}")
            await self._notify(
                "send_alert",
                f"🆘 *Не підтверджено скасування maker entry* `{link_id}`. "
                "Бот поставлено на паузу; перевір ордер/позицію на Bybit.",
            )
            raise RuntimeError(f"unknown maker cancel state: {link_id}")

        # Fill міг статись одночасно зі скасуванням — збираємо фінальний qty.
        summary = await self._wait_execution_summary(
            symbol, str(order.get("id") or ""), qty, timeout=1.0
        )
        if summary and summary["qty"] > 0:
            self.state.exec_stats["maker_fills"] += 1
            return order, summary
        self.state.exec_stats["maker_timeouts"] += 1
        return None, None

    async def _actionable_entry_price(
        self, symbol: str, direction: str
    ) -> tuple[Optional[float], str]:
        """Fresh executable side of market; signal close is never used for sizing."""
        with self._ob_lock:
            ob = self.state.ob_snapshots.get(symbol)
        if ob and ob.bids and ob.asks:
            age = time.monotonic() - ob.received_mono
            if 0 <= age <= 1.0:
                price = ob.asks[0][0] if direction == "long" else ob.bids[0][0]
                if float(price) > 0:
                    return float(price), "orderbook"

        try:
            ticker = await asyncio.to_thread(self._fetch_market_ticker, symbol)
            field = "ask" if direction == "long" else "bid"
            price = float(ticker.get(field) or ticker.get("last") or 0)
            if price > 0:
                return price, f"ticker.{field}"
        except Exception as e:
            logger.warning(f"{symbol}: не вдалося отримати actionable entry: {e}")
        return None, "unavailable"

    async def _execute_trade(self, signal: dict) -> None:
        """Serialize entry lifecycle so shutdown can await reconciliation safely."""
        async with self._execution_lock:
            if not self._running:
                return
            await self._execute_trade_locked(signal)

    async def _execute_trade_locked(self, signal: dict) -> None:
        symbol    = signal["symbol"]
        direction = signal["direction"]
        signal_entry = Decimal(str(signal["entry"]))
        tp        = Decimal(str(signal["tp"]))
        sl        = Decimal(str(signal["sl"]))

        actionable_price, pricing_source = await self._actionable_entry_price(
            symbol, direction
        )
        if actionable_price is None:
            self.state.exec_stats["calc_rejected"] += 1
            logger.warning(f"{symbol}: немає свіжої executable ціни — skip")
            return
        entry = Decimal(str(actionable_price))
        drift_bps = abs(entry - signal_entry) / signal_entry * Decimal("10000")
        if drift_bps > Decimal(str(MAX_ENTRY_DRIFT_BPS)):
            self.state.exec_stats["calc_rejected"] += 1
            logger.info(
                f"{symbol}: entry drift {drift_bps:.2f}bps > "
                f"{MAX_ENTRY_DRIFT_BPS:.2f}bps — stale setup skip"
            )
            return
        if direction == "long" and not (sl < entry < tp):
            self.state.exec_stats["calc_rejected"] += 1
            logger.info(f"{symbol}: actionable LONG entry уже поза SL/TP — skip")
            return
        if direction == "short" and not (tp < entry < sl):
            self.state.exec_stats["calc_rejected"] += 1
            logger.info(f"{symbol}: actionable SHORT entry уже поза TP/SL — skip")
            return
        signal["actionable_entry"] = float(entry)
        signal["entry_drift_bps"] = float(drift_bps)
        signal["pricing_source"] = pricing_source

        sig_min_rr = signal.get("min_rr")
        risk_entry = self._adverse_sizing_entry(
            signal_entry, entry, direction, MAX_ENTRY_DRIFT_BPS
        )
        if direction == "long" and not (sl < risk_entry < tp):
            self.state.exec_stats["calc_rejected"] += 1
            logger.info(f"{symbol}: worst-case LONG fill уже поза SL/TP — skip")
            return
        if direction == "short" and not (tp < risk_entry < sl):
            self.state.exec_stats["calc_rejected"] += 1
            logger.info(f"{symbol}: worst-case SHORT fill уже поза TP/SL — skip")
            return
        signal["risk_sizing_entry"] = float(risk_entry)
        pos = calculate_position(
            symbol      = symbol,
            deposit     = self.state.equity,
            risk_pct    = Decimal(str(RISK_PER_TRADE_PCT)),
            entry_price = risk_entry,
            stop_loss   = sl,
            take_profit = tp,
            min_rr      = Decimal(str(sig_min_rr)) if sig_min_rr is not None else None,
            max_notional = (self.state.equity * Decimal(str(MAX_NOTIONAL_EQUITY_MULT))
                            if MAX_NOTIONAL_EQUITY_MULT > 0 else None),
            max_leverage = Decimal(str(BYBIT_LEVERAGE)) if BYBIT_LEVERAGE > 0 else None,
        )

        if "error" in pos:
            self.state.exec_stats["calc_rejected"] += 1
            logger.error(f"Позиція неможлива: {pos['error']}")
            return

        if not pos["rr_ok"]:
            self.state.exec_stats["calc_rejected"] += 1
            logger.warning(
                f"R:R {pos['rr_ratio']} < floor {pos.get('rr_floor', MIN_RISK_REWARD)} "
                f"[{signal.get('strategy', signal.get('mode', '?'))}] — skip"
            )
            return

        qty  = float(pos["quantity"])
        side = "buy" if direction == "long" else "sell"

        logger.info(
            f"📤 Ордер: {side.upper()} {qty} {symbol} | "
            f"TP={float(tp):.4f} SL={float(sl):.4f} | "
            f"risk=${pos['risk_usdt']} RR={pos['rr_ratio']}"
        )

        # ── Вхід ────────────────────────────────────────────
        # Шлях 1 (основний): ОДИН маркет-ордер із прикріпленими TP/SL
        # (Bybit v5: takeProfit/stopLoss/tpslMode прямо у create order).
        # Це атомарно: або є позиція З захистом, або нічого.
        # Старий шлях (3 окремі ордери, SL через stopPrice+orderType="Stop")
        # Bybit v5 нерідко відхиляє (retCode 10001) → весь try падав у
        # except і угоди «зникали» мовчки.
        tp_side = "sell" if direction == "long" else "buy"
        order = None
        tpsl_attached = False
        order_link_id = self._order_link_id(signal)
        submit_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        with self._ob_lock:
            arrival_ob = self.state.ob_snapshots.get(symbol)
        arrival_price = None
        if arrival_ob and arrival_ob.bids and arrival_ob.asks:
            arrival_age = time.monotonic() - arrival_ob.received_mono
            if 0 <= arrival_age <= 1.0:
                arrival_price = (
                    float(arrival_ob.asks[0][0]) if direction == "long"
                    else float(arrival_ob.bids[0][0])
                )
        if arrival_price is None:
            arrival_price = float(entry)
        self.state.exec_stats["sent"] += 1
        pre_execution = None
        if USE_MAKER_ENTRY:
            order, pre_execution = await self._try_maker_entry(
                symbol=symbol, side=side, direction=direction, qty=qty,
                tp=float(tp), sl=float(sl), signal=signal, ob=arrival_ob,
            )
            if order is not None:
                order_link_id = self._order_link_id(signal, "-m")
                tpsl_attached = True
            elif not MAKER_FALLBACK_TO_MARKET:
                logger.info(f"{symbol}: maker TTL минув, market fallback вимкнений")
                return

        if order is None:
            try:
                order = await asyncio.to_thread(
                    self.rest.create_order,
                    symbol=symbol,
                    type="market",
                    side=side,
                    amount=qty,
                    params={
                        "positionIdx": 0,
                        "orderLinkId": order_link_id,
                        **self._full_tpsl_params(float(tp), float(sl)),
                    },
                )
                tpsl_attached = True
            except Exception as e:
                transport_error = isinstance(
                    e, (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable)
                )
                recovered = await asyncio.to_thread(
                    self._lookup_order_by_link_id, symbol, order_link_id
                )
                if recovered:
                    order = {
                        "id": recovered.get("orderId"),
                        "average": recovered.get("avgPrice") or None,
                        "filled": recovered.get("cumExecQty") or 0,
                        "info": recovered,
                    }
                    tpsl_attached = True
                    logger.warning(
                        f"Entry відновлено за orderLinkId={order_link_id} після помилки: {e}"
                    )
                elif transport_error:
                    for _ in range(3):
                        recovered = await asyncio.to_thread(
                            self._lookup_order_by_link_id, symbol, order_link_id
                        )
                        if recovered:
                            break
                        await asyncio.sleep(0.5)
                    if recovered:
                        order = {
                            "id": recovered.get("orderId"),
                            "average": recovered.get("avgPrice") or None,
                            "filled": recovered.get("cumExecQty") or 0,
                            "info": recovered,
                        }
                        tpsl_attached = True
                        logger.warning(
                            f"Submit response втрачено, ордер відновлено за "
                            f"orderLinkId={order_link_id}"
                        )
                    else:
                        self.state.exec_stats["last_reject"] = "unknown submit state"
                        self._trip_safety_latch(
                            f"unknown entry submit state: {order_link_id}"
                        )
                        logger.error(
                            f"Невідомий стан entry {order_link_id}; бот на паузі, "
                            "другий ордер НЕ надсилаю"
                        )
                        await self._notify(
                            "send_alert",
                            f"⚠️ *Невідомий стан entry-ордера* `{order_link_id}`. "
                            "Бот поставлено на паузу; перевір позицію на Bybit.",
                        )
                        return
                else:
                    logger.warning(
                        f"Атомарний вхід із TP/SL явно відхилено: {e} — "
                        "пробую роздільний шлях"
                    )

        # Шлях 2 (fallback): чистий вхід + окремі TP/SL з коректними v5-параметрами
        if order is None:
            fallback_link = self._order_link_id(signal, "-f")
            order_link_id = fallback_link
            try:
                order = await asyncio.to_thread(
                    self.rest.create_order,
                    symbol=symbol,
                    type="market",
                    side=side,
                    amount=qty,
                    params={
                        "positionIdx": 0,
                        "orderLinkId": fallback_link,
                    },
                )
            except Exception as e:
                if isinstance(e, (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable)):
                    recovered = None
                    for _ in range(4):
                        recovered = await asyncio.to_thread(
                            self._lookup_order_by_link_id, symbol, fallback_link
                        )
                        if recovered:
                            break
                        await asyncio.sleep(0.5)
                    if recovered:
                        order = {
                            "id": recovered.get("orderId"),
                            "average": recovered.get("avgPrice") or None,
                            "filled": recovered.get("cumExecQty") or 0,
                            "info": recovered,
                        }
                        logger.warning(
                            f"Fallback submit відновлено за orderLinkId={fallback_link}"
                        )
                    else:
                        self.state.exec_stats["last_reject"] = "unknown fallback submit state"
                        self._trip_safety_latch(
                            f"unknown fallback submit state: {fallback_link}"
                        )
                        await self._notify(
                            "send_alert",
                            f"⚠️ *Невідомий стан fallback entry* `{fallback_link}`. "
                            "Бот поставлено на паузу; перевір позицію на Bybit.",
                        )
                        return
                if order is not None:
                    pass
                else:
                    self.state.exec_stats["exchange_rejected"] += 1
                    self.state.exec_stats["last_reject"] = f"{type(e).__name__}: {e}"[:160]
                    logger.error(f"❌ ВХІДНИЙ ордер відхилено біржею: {type(e).__name__}: {e}")
                    # Тротлінг: та сама помилка повторюється кожен цикл (напр.
                    # retCode 10005 permission denied) → 1 алерт на 10 хв, не спам.
                    now_mono = time.monotonic()
                    if now_mono - self._last_reject_alert >= 600:
                        self._last_reject_alert = now_mono
                        await self._notify(
                            "send_alert",
                            f"❌ *Ордер відхилено біржею*\n"
                            f"{side.upper()} {qty} {symbol}\n`{e}`",
                        )
                    return

        try:
            execution = pre_execution or await self._wait_execution_summary(
                symbol, str(order.get("id") or ""), qty, timeout=5.0
            )
            execution_source = "executions"
            if execution:
                filled_qty = float(execution["qty"])
                real_entry = float(execution["vwap"])
                entry_fee = float(execution["fee"])
                maker_qty = float(execution["maker_qty"])
            else:
                # Авторитетний fallback — фактична активна позиція, не signal price.
                positions = await asyncio.to_thread(self.rest.fetch_positions, [symbol])
                active = [
                    p for p in positions if float(p.get("contracts", 0) or 0) > 0
                ]
                if not active:
                    raise RuntimeError("Ордер не має execution fills і активної позиції")
                p = active[0]
                info = p.get("info", {}) or {}
                filled_qty = float(p.get("contracts") or info.get("size") or 0)
                real_entry = float(p.get("entryPrice") or info.get("avgPrice") or 0)
                if filled_qty <= 0 or real_entry <= 0:
                    raise RuntimeError("Біржа не повернула фактичний qty/entry")
                entry_fee = 0.0
                maker_qty = 0.0
                execution_source = "position"

            requested_qty = qty
            qty = filled_qty
            fill_ratio = qty / requested_qty if requested_qty > 0 else 0.0
            signal_price = float(signal["entry"])
            sign = 1.0 if direction == "long" else -1.0
            slippage_signal_bps = sign * (real_entry - signal_price) / signal_price * 10_000
            slippage_arrival_bps = (
                sign * (real_entry - arrival_price) / arrival_price * 10_000
                if arrival_price else None
            )
            maker_pct = maker_qty / qty if qty > 0 else 0.0

            # Fill can differ from the quote used for sizing. Re-run the same
            # conservative NET-risk/RR model and fail closed if the actual fill
            # made the trade unsafe or stale.
            if direction == "long" and not (float(sl) < real_entry < float(tp)):
                raise RuntimeError("actual LONG fill уже поза SL/TP")
            if direction == "short" and not (float(tp) < real_entry < float(sl)):
                raise RuntimeError("actual SHORT fill уже поза TP/SL")
            if abs(real_entry - signal_price) / signal_price * 10_000 > MAX_ENTRY_DRIFT_BPS:
                raise RuntimeError("actual fill перевищив MAX_ENTRY_DRIFT_BPS")
            fill_eval = calculate_position(
                symbol=symbol,
                deposit=self.state.equity,
                risk_pct=Decimal(str(RISK_PER_TRADE_PCT)),
                entry_price=Decimal(str(real_entry)),
                stop_loss=sl,
                take_profit=tp,
                min_rr=Decimal(str(sig_min_rr)) if sig_min_rr is not None else None,
                max_notional=(self.state.equity * Decimal(str(MAX_NOTIONAL_EQUITY_MULT))
                              if MAX_NOTIONAL_EQUITY_MULT > 0 else None),
                max_leverage=(Decimal(str(BYBIT_LEVERAGE)) if BYBIT_LEVERAGE > 0 else None),
                entry_is_filled=True,
            )
            if "error" in fill_eval or not fill_eval.get("rr_ok", False):
                raise RuntimeError(
                    f"actual fill не проходить NET-risk/RR: "
                    f"{fill_eval.get('error') or fill_eval.get('rr_ratio')}"
                )
            if Decimal(str(qty)) > Decimal(str(fill_eval["quantity"])):
                raise RuntimeError("actual fill qty перевищує NET-risk budget")

            execution_meta = {
                "order_id": order.get("id"),
                "order_link_id": order_link_id,
                "requested_qty": requested_qty,
                "filled_qty": qty,
                "fill_ratio": fill_ratio,
                "signal_price": signal_price,
                "actionable_price": float(entry),
                "entry_drift_bps": float(drift_bps),
                "pricing_source": pricing_source,
                "arrival_price": arrival_price,
                "entry_vwap": real_entry,
                "entry_fee": entry_fee,
                "maker_pct": maker_pct,
                "source": execution_source,
                "submit_at_ms": submit_at_ms,
                "first_fill_ms": execution.get("first_fill_ms") if execution else None,
                "last_fill_ms": execution.get("last_fill_ms") if execution else None,
                "slippage_signal_bps": slippage_signal_bps,
                "slippage_arrival_bps": slippage_arrival_bps,
            }
            self.state.exec_stats["last_execution"] = execution_meta
            logger.info(
                f"✅ Ордер виконано: id={order.get('id')} | "
                f"filled={qty}/{requested_qty} | vwap={real_entry:.2f} | "
                f"slip={slippage_signal_bps:+.2f}bps | "
                f"tpsl_attached={tpsl_attached}"
            )

            if not tpsl_attached:
                try:
                    await asyncio.to_thread(
                        self._set_full_position_protection,
                        symbol,
                        float(tp),
                        float(sl),
                    )
                    tpsl_attached = True
                    logger.info("✅ Full position TP/SL встановлено через trading-stop")
                except Exception as protection_error:
                    logger.warning(
                        "Full position TP/SL не встановлено: "
                        f"{protection_error}; пробую окремі reduce-only ордери"
                    )

            if not tpsl_attached:
                # TP: reduceOnly limit
                await asyncio.to_thread(
                    self.rest.create_order,
                    symbol=symbol,
                    type="limit",
                    side=tp_side,
                    amount=qty,
                    price=float(tp),
                    params={"reduceOnly": True, "timeInForce": "GTC", "positionIdx": 0},
                )
                # SL: умовний маркет. triggerDirection обов'язковий у v5:
                # 2 = спрацьовує при ПАДІННІ ціни (SL лонга), 1 = при ЗРОСТАННІ (SL шорта)
                await asyncio.to_thread(
                    self.rest.create_order,
                    symbol=symbol,
                    type="market",
                    side=tp_side,
                    amount=qty,
                    params={
                        "reduceOnly":       True,
                        "triggerPrice":     float(sl),
                        "triggerDirection": 2 if direction == "long" else 1,
                        "positionIdx":      0,
                    },
                )

            logger.info(f"✅ TP={float(tp):.4f} SL={float(sl):.4f} встановлено")

            opened_at_ms = int(execution_meta.get("first_fill_ms") or submit_at_ms)
            opened_at = datetime.fromtimestamp(opened_at_ms / 1000, tz=timezone.utc)
            trade_record = {
                "symbol":      symbol,
                "direction":   direction,
                "entry":       real_entry,
                "qty":         qty,
                "requested_qty": requested_qty,
                "tp":          float(tp),
                "sl":          float(sl),
                "order_id":    order.get("id"),
                "opened_at":   opened_at,
                "risk_usdt":   float(pos["risk_usdt"]) * fill_ratio,
                "reward_usdt": float(pos["reward_usdt"]) * fill_ratio,
                "net_rr":      float(pos["rr_ratio"]),
                "execution":   execution_meta,
                "ob_imbalance": signal.get("ob_imbalance", 0),
                "mode":        signal.get("mode", "unknown"),
                "strategy":    signal.get("strategy", signal.get("mode", "unknown")),
                "cvd_ok":       signal.get("cvd_ok"),
                "volume_ok":    signal.get("volume_ok"),
                "ob_direction": signal.get("ob_direction"),
                "ob_confirmed": signal.get("ob_confirmed"),
                "ob_required":  signal.get("ob_required", False),
                "ob_stale":     signal.get("ob_stale", False),
                "signal_key":    signal.get("signal_key"),
                "trade_flow_direction": signal.get("trade_flow_direction"),
                "trade_flow_imbalance": signal.get("trade_flow_imbalance"),
                "trade_flow_notional": signal.get("trade_flow_notional"),
                # Індикатори для запису в БД
                "atr":         signal.get("atr"),
                "raw_rr":      signal.get("raw_rr"),
                "cvd_signal":  signal.get("cvd_signal"),
                "sweep_extreme": signal.get("sweep_extreme"),
                "range_high":  signal.get("range", {}).get("high"),
                "range_low":   signal.get("range", {}).get("low"),
                "bias_1h":     signal.get("bias_1h"),
                "of_delta":    signal.get("of_delta"),
                # Поля нових стратегій (None для sweep) — для Telegram-звіту
                "rsi":          signal.get("rsi"),
                "bb_percent_b": signal.get("bb_percent_b"),
                "bb_width_pct": signal.get("bb_width_pct"),
                "vwap_dev_pct": signal.get("vwap_dev_pct"),
                # FTA — проблемна зона старшого ТФ
                "fta_price":     signal.get("fta_price"),
                "fta_blocks_tp": signal.get("fta_blocks_tp", False),
                "fta_dist_pct":  signal.get("fta_dist_pct"),
            }

            self.state.open_trade      = trade_record
            self.state.last_trade_time = datetime.now(timezone.utc)
            self.state.daily_trades   += 1
            self.state.exec_stats["opened"] += 1

            try:
                await asyncio.to_thread(self._save_trade_open_db, trade_record)
            except Exception as e:
                logger.warning(f"DB trade-open log failed: {e}")

            # Push-сповіщення в Telegram (безпечне; нема нотифаєра → no-op)
            await self._notify("notify_trade_opened", trade_record)

            # TODO: log_trade_open(trade_record)  ← підключимо в наступному кроці

        except Exception as e:
            # Сюди потрапляємо ПІСЛЯ виконаного входу (TP/SL або запис впали).
            # Спершу створюємо authoritative/synthetic state, потім закриваємо
            # і проводимо звичайний closed-PnL accounting. Інакше emergency
            # fees/losses обходили daily limit, а прихована позиція дозволяла
            # наступний entry.
            logger.error(f"❌ Помилка після входу (TP/SL/запис): {e} — екстрено закриваю позицію")
            await self._notify(
                "send_alert",
                "⚠️ *Позиція відкрилась, але post-fill перевірка або "
                f"встановлення захисту не завершилися — закриваю!*\n`{e}`",
            )

            active = []
            position_query_ok = False
            for _ in range(5):
                try:
                    positions = await asyncio.to_thread(
                        self.rest.fetch_positions, [symbol]
                    )
                    position_query_ok = True
                    active = [
                        p for p in positions
                        if float(p.get("contracts", 0) or 0) > 0
                    ]
                    if active:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.4)

            known_entry = locals().get("real_entry")
            known_qty = locals().get("filled_qty")
            known_execution = locals().get("execution")
            if (not known_entry or not known_qty) and order is not None:
                retry_execution = await self._wait_execution_summary(
                    symbol, str(order.get("id") or ""), float(qty), timeout=2.0
                )
                if retry_execution:
                    known_entry = float(retry_execution["vwap"])
                    known_qty = float(retry_execution["qty"])
                    known_execution = retry_execution

            if active:
                p = active[0]
                info = p.get("info", {}) or {}
                known_entry = float(
                    p.get("entryPrice") or info.get("avgPrice") or known_entry or 0
                )
                known_qty = float(
                    p.get("contracts") or info.get("size") or known_qty or 0
                )

            if not position_query_ok or not known_entry or not known_qty:
                self._trip_safety_latch("post-entry position state unknown")
                await self._notify(
                    "send_alert",
                    f"🆘 *Не вдалося reconcile позицію {symbol} після entry-помилки.* "
                    "Safety latch активний; перевір Bybit і перезапусти сервіс.",
                )
                return

            opened_ms = int(
                (known_execution or {}).get("first_fill_ms") or submit_at_ms
            )
            emergency_trade = {
                "symbol": symbol,
                "direction": direction,
                "entry": float(known_entry),
                "qty": float(known_qty),
                "requested_qty": float(locals().get("requested_qty") or qty),
                "tp": float(tp),
                "sl": float(sl),
                "order_id": (order.get("id") if order else None) or order_link_id,
                "opened_at": datetime.fromtimestamp(opened_ms / 1000, tz=timezone.utc),
                "risk_usdt": float(pos.get("risk_usdt") or 0),
                "reward_usdt": float(pos.get("reward_usdt") or 0),
                "execution": locals().get("execution_meta"),
                "mode": signal.get("mode", "unknown"),
                "strategy": signal.get("strategy", signal.get("mode", "unknown")),
                "signal_key": signal.get("signal_key"),
                "raw_rr": signal.get("raw_rr"),
                "net_rr": float(pos.get("rr_ratio") or 0),
                "emergency_close": True,
                "emergency_reason": str(e)[:160],
            }
            self.state.open_trade = emergency_trade
            self.state.last_trade_time = datetime.now(timezone.utc)
            self.state.daily_trades += 1
            self.state.exec_stats["opened"] += 1
            try:
                await asyncio.to_thread(self._save_trade_open_db, emergency_trade)
            except Exception as db_error:
                logger.warning(f"Emergency DB open log failed: {db_error}")

            close_error = None
            if active:
                try:
                    await asyncio.to_thread(
                        self.rest.create_order,
                        symbol=symbol,
                        type="market",
                        side=tp_side,
                        amount=float(known_qty),
                        params={"reduceOnly": True, "positionIdx": 0},
                    )
                except Exception as e2:
                    close_error = e2

            flat = False
            for _ in range(10):
                try:
                    positions = await asyncio.to_thread(
                        self.rest.fetch_positions, [symbol]
                    )
                    still_active = [
                        p for p in positions
                        if float(p.get("contracts", 0) or 0) > 0
                    ]
                    if not still_active:
                        flat = True
                        break
                    flat = False
                except Exception:
                    pass
                await asyncio.sleep(0.5)

            if not flat:
                self._trip_safety_latch("emergency close not confirmed flat")
                logger.critical(
                    f"🆘 Emergency close НЕ підтверджено: {close_error or 'position active'}"
                )
                await self._notify(
                    "send_alert",
                    f"🆘 *ЗАКРИЙ ПОЗИЦІЮ {symbol} ВРУЧНУ НА BYBIT!* "
                    "Safety latch активний.",
                )
                return

            try:
                await asyncio.to_thread(self.rest.cancel_all_orders, symbol)
            except Exception as cancel_error:
                logger.warning(f"Emergency stale-order cleanup: {cancel_error}")
            logger.info("✅ Emergency позицію підтверджено закритою")
            await self._handle_closed_position()

    # ── Моніторинг позиції ────────────────────────────────────

    async def _monitor_position(self) -> None:
        if not self.state.open_trade:
            return

        symbol = self.state.open_trade["symbol"]
        try:
            positions = await asyncio.to_thread(self.rest.fetch_positions, [symbol])
            active    = [p for p in positions if float(p.get("contracts", 0)) > 0]
            if not active:
                await self._handle_closed_position()
        except Exception as e:
            logger.error(f"Помилка моніторингу позиції: {e}")

    async def _handle_closed_position(self) -> None:
        trade = self.state.open_trade
        if not trade:
            return

        try:
            opened_ms = int(trade["opened_at"].timestamp() * 1000)
            closed = None
            trade.setdefault("close_pending_since", datetime.now(timezone.utc))
            deadline = time.monotonic() + CLOSED_PNL_GRACE_SEC
            while True:
                closed = await asyncio.to_thread(
                    self._fetch_closed_pnl,
                    trade["symbol"],
                    opened_ms,
                    float(trade.get("entry") or 0),
                    float(trade.get("qty") or 0),
                )
                if closed is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(1.0, remaining))

            if closed is not None:
                pnl = float(closed["pnl"])
                exit_price = float(closed.get("avg_exit") or 0)
                trade["close_execution"] = closed
            else:
                # Execution history does not reliably expose reduceOnly or a
                # complete realized PnL. Keep the trade in closing-pending and
                # retry on the next monitor cycle; never invent or lose PnL.
                if not trade.get("close_pending_alerted"):
                    trade["close_pending_alerted"] = True
                    await self._notify(
                        "send_alert",
                        "⏳ Позиція вже закрита, але Bybit ще не повернув "
                        "authoritative closed-PnL. Бот блокує нові входи й повторить запит.",
                    )
                pending_for = (
                    datetime.now(timezone.utc) - trade["close_pending_since"]
                ).total_seconds()
                if pending_for >= 60 and not trade.get("close_pending_escalated"):
                    trade["close_pending_escalated"] = True
                    await self._notify(
                        "send_alert",
                        "🆘 Closed-PnL не з'явився понад 60 секунд. "
                        "Нові входи заблоковані; перевір історію Bybit/API.",
                    )
                return

            self.state.equity    += Decimal(str(pnl))
            self.state.daily_pnl += Decimal(str(pnl))

            # Ресинк капіталу з РЕАЛЬНИМ акаунтом після кожної закритої угоди —
            # внутрішня оцінка PnL не «розпливається» від фактичного балансу.
            if USE_REAL_BALANCE:
                real = await asyncio.to_thread(self._fetch_real_balance)
                if real is not None:
                    self.state.equity = Decimal(str(real))

            result = (
                "✅ PROFIT" if pnl > 0
                else "❌ LOSS" if pnl < 0
                else "➖ BREAKEVEN"
            )
            logger.info(
                f"{result} | {trade['direction'].upper()} {trade['symbol']} | "
                f"P&L=${pnl:.2f} | equity=${self.state.equity:.2f}"
            )

            if pnl < 0:
                self.state.loss_streak   += 1
                self.state.last_loss_time = datetime.now(timezone.utc)
            else:
                self.state.loss_streak = 0
                self.state.last_loss_time = None
            if pnl != 0:
                self.state.recent_outcomes.append(1 if pnl > 0 else 0)

            try:
                await asyncio.to_thread(
                    self._save_trade_close_db, trade, float(pnl), float(exit_price)
                )
            except Exception as e:
                logger.warning(f"DB trade-close log failed: {e}")

            # Push-сповіщення про закриття (безпечне; нема нотифаєра → no-op)
            await self._notify("notify_trade_closed", float(pnl), trade)

            # TODO: log_trade_close(trade["order_id"], pnl, result)

            self.state.open_trade = None

        except Exception as e:
            logger.error(f"Помилка обробки закритої позиції: {e}")
            # Fail closed: state лишається, тому новий entry неможливий до
            # успішного authoritative closed-PnL reconciliation.
            if trade is not None:
                trade.setdefault("close_pending_since", datetime.now(timezone.utc))

    # ── Допоміжні методи ──────────────────────────────────────

    def _ws_note(self, key: str, connected: Optional[bool] = None,
                 inc_connects: bool = False, inc_msgs: bool = False,
                 error: Optional[str] = None) -> None:
        """Оновлює стан WS-потоку для /diag (без винятків, без блокувань)."""
        st = self.state.ws_status.setdefault(
            key, {"connected": False, "connects": 0, "msgs": 0,
                  "last_error": None, "last_error_at": None},
        )
        if connected is not None:
            st["connected"] = connected
        if inc_connects:
            st["connects"] += 1
        if inc_msgs:
            st["msgs"] += 1
        if error is not None:
            st["last_error"] = error[:160]
            st["last_error_at"] = datetime.now(timezone.utc).strftime("%H:%M:%S")

    def _fetch_real_balance(self) -> Optional[float]:
        """Свіжий walletBalance USDT з акаунта (None — якщо API недоступний)."""
        try:
            if getattr(self, "_pybit", None) is None:
                return None
            resp = self._pybit.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            return float(resp["result"]["list"][0]["coin"][0]["walletBalance"])
        except Exception as e:
            logger.debug(f"Не вдалось оновити баланс з акаунта: {e}")
            return None

    def _last_candle_age_sec(self, symbol: str, tf: str) -> Optional[float]:
        """Вік останньої закритої свічки від її START."""
        buf = self.state.candles.get(f"{symbol}_{tf}")
        if not buf:
            return None
        last_ts_ms = float(buf[-1][0])
        return max(0.0, datetime.now(timezone.utc).timestamp() - last_ts_ms / 1000.0)

    def _closed_candle_lag_sec(
        self, symbol: str, tf: str, *, now_ts: float | None = None
    ) -> Optional[float]:
        """Відставання від останньої очікуваної закритої свічки.

        Вік від START природно росте від TF до 2×TF і не є затримкою.
        Перші 3с нового бару даємо біржі на confirm; далі останній START має
        дорівнювати START щойно закритого бару. 0с = дані актуальні.
        """
        buf = self.state.candles.get(f"{symbol}_{tf}")
        if not buf:
            return None
        tf_sec = self.TIMEFRAME_MS[tf] / 1000.0
        now_ts = datetime.now(timezone.utc).timestamp() if now_ts is None else now_ts
        current_start = int(now_ts // tf_sec) * tf_sec
        expected_start = current_start - tf_sec
        if now_ts - current_start <= self.CANDLE_CLOSE_GRACE_SEC:
            expected_start -= tf_sec
        actual_start = float(buf[-1][0]) / 1000.0
        return max(0.0, expected_start - actual_start)

    def _ws_age_sec(self, symbol: str, tf: str) -> Optional[float]:
        """Секунд від останнього live-пуша WS (здорово ≤3с)."""
        ts = self.state.ws_msg_at.get(f"{symbol}_{tf}")
        if ts is None:
            return None
        return max(0.0, datetime.now(timezone.utc).timestamp() - ts)

    def _backfill_candles(self, symbol: str, tf: str) -> None:
        """Докачує закриті свічки через REST (mainnet) і зливає в буфер по ts."""
        key = f"{symbol}_{tf}"
        try:
            raw = self._fetch_market_ohlcv(symbol, tf, 100)
        except Exception as e:
            logger.debug(f"Backfill {key} не вдався: {e}")
            return
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        tf_ms = self.TIMEFRAME_MS.get(tf)
        if tf_ms is None:
            return
        raw = [list(c) for c in raw if int(c[0]) + tf_ms <= now_ms]
        buf = self.state.candles.get(key)
        if buf is None:
            self.state.candles[key] = deque(raw, maxlen=self.CANDLE_BUFFER)
            return
        merged = {int(c[0]): list(c) for c in buf}
        for c in raw:
            merged[int(c[0])] = list(c)
        ordered = [merged[k] for k in sorted(merged)]
        self.state.candles[key] = deque(ordered[-300:], maxlen=self.CANDLE_BUFFER)
        logger.debug(f"Backfill {key}: {len(raw)} свічок злито, буфер {len(self.state.candles[key])}")

    CANDLE_CLOSE_GRACE_SEC = 3
    REFRESH_MIN_SEC = 3      # повторюємо REST на наступному циклі, доки дані старі
    ALERT_MIN_SEC   = 1800   # алерт у TG не частіше, ніж раз на 30 хв

    async def _ensure_fresh_data(self) -> None:
        """
        Якщо закрита свічка будь-якого потрібного TF не з'явилась за 3с після
        close — докачуємо її через REST. Якщо вона лишилась старою, фіксуємо
        live-помилку, а перевірка сигналу блокує нові входи.
        """
        stale: list[tuple[str, str, float | None]] = []
        for symbol in TRADING_PAIRS:
            for tf in self._required_timeframes():
                lag = self._closed_candle_lag_sec(symbol, tf)
                if lag is None or lag > 0:
                    stale.append((symbol, tf, lag))

        if not stale:
            self.state.candle_freshness_errors.clear()
            return

        now_mono = time.monotonic()
        if now_mono - self._last_data_refresh >= self.REFRESH_MIN_SEC:
            self._last_data_refresh = now_mono
            logger.warning(
                "🩹 Закриті свічки запізнились >3с: "
                + ", ".join(f"{symbol} {tf}" for symbol, tf, _ in stale)
                + " — докачую через REST"
            )
            await asyncio.gather(*(
                asyncio.to_thread(self._backfill_candles, symbol, tf)
                for symbol, tf, _ in stale
            ))

        errors = {}
        for symbol, tf, _ in stale:
            lag = self._closed_candle_lag_sec(symbol, tf)
            if lag is None or lag > 0:
                errors[f"{symbol}_{tf}"] = lag
        self.state.candle_freshness_errors = errors

        if not errors:
            return

        if now_mono - self._last_stale_alert >= self.ALERT_MIN_SEC:
            self._last_stale_alert = now_mono
            await self._notify(
                "send_alert",
                "❌ *LIVE-ПОМИЛКА: нова закрита свічка не з'явилась за 3с.*\n"
                "Нові входи заблоковані до відновлення WS/REST даних.",
            )

    async def _notify(self, method: str, *args) -> None:
        """
        Безпечний виклик нотифаєра (Telegram). Якщо нотифаєра нема або
        метод відсутній/кинув виняток — торгівля НЕ страждає.
        """
        n = self.notifier
        if n is None:
            return
        fn = getattr(n, method, None)
        if fn is None:
            return
        try:
            await fn(*args)
        except Exception as e:
            logger.warning(f"Notifier {method} помилка: {e}")

    def _get_df(
        self,
        symbol: str,
        tf: str,
        *,
        closed_only: bool = False,
    ) -> pd.DataFrame | None:
        """Повертає OHLCV; для сигналів ``closed_only=True`` прибирає repaint."""
        key = f"{symbol}_{tf}"
        buf = self.state.candles.get(key)
        if not buf or len(buf) < 10:
            return None

        rows = list(buf)
        if not closed_only:
            live = self.state.live_candles.get(key)
            if live is not None and int(live[0]) >= int(rows[-1][0]):
                rows.append(list(live))

        df = pd.DataFrame(
            rows,
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        # Дедуплікація: REST-початкові свічки і WS-confirm можуть дати
        # дубль останнього timestamp (forming vs closed). Лишаємо останній —
        # інакше BB/RSI/VWAP рахуються по «зайвій» свічці.
        df = df[~df.index.duplicated(keep="last")]
        if closed_only:
            tf_ms = self.TIMEFRAME_MS.get(tf)
            if tf_ms is None:
                logger.error(f"Невідомий timeframe для closed-bar filter: {tf}")
                return None
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            close_cutoff = pd.Timestamp(now_ms - tf_ms, unit="ms", tz="UTC")
            df = df[df.index <= close_cutoff]
            if len(df) < 10:
                return None
            df = df.tail(300)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        return df

    def _update_range_cache(self, symbol, df_1h, df_30m) -> None:
        """Оновлює кеш рейнджів раз на RANGE_UPDATE_MIN хвилин.

        ВАЖЛИВО: рейндж рахуємо з тими ж параметрами, що й generator.py
        (з SYMBOL_CONFIG), інакше кеш-рейндж (який generator використовує
        в першу чергу) не збігається з налаштуваннями і знецінює їх.
        """
        now = datetime.now(timezone.utc)
        if (self.state.range_updated_at is None or
                (now - self.state.range_updated_at).total_seconds() >= self.RANGE_UPDATE_MIN * 60):

            cfg           = SYMBOL_CONFIG.get(symbol, {})
            lookback      = int(cfg.get("range_lookback", 48))
            min_range_atr = float(cfg.get("min_range_atr", 1.5))
            max_range_atr = float(cfg.get("max_range_atr", 8.0))
            max_drift_atr = float(cfg.get("max_drift_atr", 2.5))

            if df_1h is not None:
                self.state.cached_range_1h = detect_active_range(
                    df_1h,
                    lookback=lookback,
                    min_range_atr=min_range_atr,
                    max_range_atr=max_range_atr,
                    max_drift_atr=max_drift_atr,
                )
            if df_30m is not None:
                self.state.cached_range_30m = detect_active_range(
                    df_30m,
                    lookback=min(lookback, 20),
                    min_range_atr=min_range_atr,
                    max_range_atr=max_range_atr,
                    max_drift_atr=max_drift_atr,
                )

            self.state.range_updated_at = now
            logger.debug(
                f"Рейндж оновлено | "
                f"1h={self.state.cached_range_1h is not None} "
                f"30m={self.state.cached_range_30m is not None}"
            )

    def _check_daily_reset(self, now: datetime) -> None:
        if now.date() != self.state.risk_day:
            logger.info(
                f"📅 Новий день | P&L: ${self.state.daily_pnl:.2f} | "
                f"угод: {self.state.daily_trades}"
            )
            self.state.daily_pnl    = Decimal("0")
            self.state.daily_trades = 0
            self.state.risk_day = now.date()

    def stop(self) -> None:
        self._running = False
        self._paused.set()
        logger.info("🛑 LiveTrader зупинено")

    def status(self) -> None:
        logger.info(f"\n{'='*50}")
        logger.info(f"📊 СТАТУС ТРЕЙДЕРА")
        logger.info(f"  Equity:        ${self.state.equity:.2f}")
        logger.info(f"  Daily P&L:     ${self.state.daily_pnl:.2f}")
        logger.info(f"  Угод сьогодні: {self.state.daily_trades}")
        logger.info(f"  Loss streak:   {self.state.loss_streak}")
        if self.state.open_trade:
            t = self.state.open_trade
            logger.info(f"  ВІДКРИТА: {t['direction'].upper()} {t['symbol']} "
                        f"entry={t['entry']:.4f} TP={t['tp']:.4f} SL={t['sl']:.4f}")
        else:
            logger.info(f"  Позицій немає")
        for symbol, ob in self.state.ob_snapshots.items():
            logger.info(f"  OB {symbol}: {ob.summary()}")
        logger.info(f"{'='*50}\n")


# ── Точка входу ───────────────────────────────────────────────

async def run_live_trader():
    trader = LiveTrader()
    try:
        await trader.run()
    except KeyboardInterrupt:
        logger.info("👋 Зупинено користувачем")
    finally:
        trader.stop()
