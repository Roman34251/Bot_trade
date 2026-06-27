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
import json
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    DEPOSIT_USDT, RISK_PER_TRADE_PCT,
    MIN_RISK_REWARD, BYBIT_TAKER_FEE,
    COOLDOWN_AFTER_LOSS_MIN, MIN_CANDLES_BETWEEN_TRADES,
    OB_IMBALANCE_LONG_MIN, OB_IMBALANCE_SHORT_MAX,
    OB_MAX_AGE_SECONDS, OB_WALL_THRESHOLD_MULT, OB_WALL_BLOCK_PCT,
    USE_DUAL_TF_STRATEGY,
    USE_ORDER_BOOK_CONFIRMATION,
    USE_ORDER_BOOK_WALL_FILTER,
    SYMBOL_CONFIG,
    USE_SWEEP_STRATEGY, USE_MEANREV_STRATEGY, USE_VWAP_STRATEGY,
    STRATEGY_PRIORITY,
)
from indicators.range_detector import detect_active_range, calculate_atr
from signals.generator import generate_scalp_signal
from signals.mean_reversion import generate_meanrev_signal
from signals.vwap_strategy import generate_vwap_signal
from signals.dual_tf import generate_dual_tf_signal
from signals.calculator import calculate_position, check_daily_limits
# DB імпорт — підключимо в наступному кроці
# from db.trade_logger import log_trade_open, log_trade_close


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
    loss_streak:      int     = 0
    last_trade_time:  Optional[datetime] = None
    last_loss_time:   Optional[datetime] = None

    # Кешовані рейнджі (оновлення раз на RANGE_UPDATE_MIN хвилин)
    cached_range_1h:  Optional[dict] = None
    cached_range_30m: Optional[dict] = None
    range_updated_at: Optional[datetime] = None

    ob_snapshots:         dict = field(default_factory=dict)   # symbol → OBSnapshot
    ob_imbalance_history: dict = field(default_factory=dict)   # symbol → deque

    candles: dict = field(default_factory=dict)  # "BTC/USDT:USDT_1h" → deque(200)


# ── Головний клас ─────────────────────────────────────────────

class LiveTrader:

    CANDLE_BUFFER    = 200
    RANGE_UPDATE_MIN = 60

    def __init__(self):
        self._running     = False
        self._ob_lock     = threading.Lock()
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

        self.state = LiveState(
            equity  = Decimal(str(self._real_balance)),
            deposit = Decimal(str(self._real_balance)),
        )

        mode = "DEMO" if BYBIT_DEMO else "LIVE 🔴"
        logger.info(f"🤖 LiveTrader | режим={mode}")
        logger.info(f"   Депозит: ${self._real_balance:.2f} | Ризик: {RISK_PER_TRADE_PCT*100:.1f}%/угоду")
        logger.info(f"   Пари: {TRADING_PAIRS}")

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
            resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            usdt = float(resp["result"]["list"][0]["coin"][0]["walletBalance"])
            logger.info(f"✅ Bybit підключено | USDT баланс: ${usdt:.2f}")
            self._real_balance = usdt
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

        await self._load_initial_candles()

        tasks = []
        for symbol in TRADING_PAIRS:
            tasks.append(self._stream_orderbook(symbol))
            # Раніше стрімився ТІЛЬКИ 1m → 1h/5m/30m після старту "застигали"
            # (рейндж рахувався з протухлого вікна, 5m CVD/volume були мертві).
            for tf in ("1h", "30m", "5m", "1m"):
                tasks.append(self._stream_candles(symbol, tf))
        tasks.append(self._trading_loop())

        logger.info("▶ Всі потоки запущено")
        await asyncio.gather(*tasks)

    async def _load_initial_candles(self) -> None:
        logger.info("📊 Завантаження початкових свічок...")
        for symbol in TRADING_PAIRS:
            for tf in ["1h", "30m", "5m", "1m"]:
                try:
                    raw = self.market.fetch_ohlcv(symbol, tf, limit=self.CANDLE_BUFFER)
                    self.state.candles[f"{symbol}_{tf}"] = deque(raw, maxlen=self.CANDLE_BUFFER)
                    logger.info(f"   {symbol} {tf}: {len(raw)} свічок")
                except Exception as e:
                    logger.error(f"Помилка завантаження {symbol} {tf}: {e}")
        logger.info("✅ Початкові свічки завантажено")


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
        sub_msg   = json.dumps({"op": "subscribe", "args": [f"orderbook.50.{ws_symbol}"]})
 
        full_bids: dict = {}
        full_asks: dict = {}
 
        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval = None,
                    ping_timeout  = None,
                    close_timeout = 10,
                    open_timeout  = 10,
                ) as ws:
                    await ws.send(sub_msg)
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
 
                        while self._running:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                            except asyncio.TimeoutError:
                                continue
 
                            data     = json.loads(raw)
                            msg_type = data.get("type")
                            topic    = data.get("topic", "")
                            ob_data  = data.get("data", {})
 
                            if data.get("op") == "pong":
                                continue
 
                            if not topic.startswith("orderbook"):
                                continue
 
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
 
                                logger.debug(f"{symbol} OB [{msg_type}]: {snapshot.summary()}")
 
                    finally:
                        keepalive.cancel()
 
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"OB WebSocket помилка {symbol}: {e} — перепідключення...")
                full_bids.clear()
                full_asks.clear()
                await asyncio.sleep(5)
 
    async def _stream_candles(self, symbol: str, tf: str) -> None:
        ws_symbol = symbol.replace("/", "").replace(":USDT", "")
        url       = "wss://stream.bybit.com/v5/public/linear"
        sub_msg   = json.dumps({"op": "subscribe", "args": [f"kline.{tf}.{ws_symbol}"]})
        key       = f"{symbol}_{tf}"
 
        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval = None,
                    ping_timeout  = None,
                    close_timeout = 10,
                    open_timeout  = 10,
                ) as ws:
                    await ws.send(sub_msg)
 
                    keepalive = asyncio.create_task(
                        self._bybit_keepalive(ws, f"Candles {symbol} {tf}")
                    )
 
                    try:
                        first_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        first     = json.loads(first_raw)
 
                        if first.get("op") == "subscribe" and first.get("success"):
                            logger.info(f"✅ Candles WebSocket: {symbol} {tf}")
                        elif first.get("op") == "pong":
                            logger.debug(f"Candles {symbol}: pong")
                        else:
                            logger.info(
                                f"✅ Candles WebSocket: {symbol} {tf} "
                                f"| first={first.get('op') or first.get('type')}"
                            )
 
                        while self._running:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                            except asyncio.TimeoutError:
                                continue
 
                            data = json.loads(raw)
 
                            if data.get("op") == "pong":
                                continue
 
                            if data.get("topic", "").startswith("kline"):
                                for candle in data.get("data", []):
                                    if candle.get("confirm", False):
                                        c = [
                                            int(candle["start"]),
                                            float(candle["open"]),
                                            float(candle["high"]),
                                            float(candle["low"]),
                                            float(candle["close"]),
                                            float(candle["volume"]),
                                        ]
                                        if key in self.state.candles:
                                            self.state.candles[key].append(c)
                                            logger.debug(
                                                f"Нова {symbol} {tf}: close={c[4]:.4f} vol={c[5]:.2f}"
                                            )
 
                    finally:
                        keepalive.cancel()
 
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Candles WebSocket помилка {symbol} {tf}: {e}")
                await asyncio.sleep(5)
    # ── Торговий цикл ─────────────────────────────────────────

    async def _trading_loop(self) -> None:
        logger.info("🔄 Торговий цикл запущено")

        while self._running:
            try:
                await self._paused.wait()
                if not self._running:
                    break

                now = datetime.now(timezone.utc)
                self._check_daily_reset(now)

                # Моніторинг відкритої позиції
                if self.state.open_trade:
                    await self._monitor_position()
                    await asyncio.sleep(10)
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

                # Перевіряємо сигнали
                for symbol in TRADING_PAIRS:
                    signal = await self._check_signal(symbol)
                    if signal:
                        await self._execute_trade(signal)
                        break

                await asyncio.sleep(15)

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
        df_1h  = self._get_df(symbol, "1h")
        df_30m = self._get_df(symbol, "30m")
        df_5m  = self._get_df(symbol, "5m")
        df_1m  = self._get_df(symbol, "1m")

        if df_1m is None or len(df_1m) < 25:
            return None

        self._update_range_cache(symbol, df_1h, df_30m)

        dfs = {"1h": df_1h, "30m": df_30m, "5m": df_5m, "1m": df_1m}

        # ── Диспетчер стратегій за пріоритетом ──────────────────
        # Перша стратегія зі STRATEGY_PRIORITY, що дала валідний сигнал —
        # перемагає. Усі три незалежні: sweep / meanrev / vwap.
        signal = None
        for name in STRATEGY_PRIORITY:
            name = name.strip().lower()
            if name == "sweep" and USE_SWEEP_STRATEGY:
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

        if ob is None:
            logger.debug(f"{symbol}: немає order book даних — продовжуємо без OB")
            signal["ob_imbalance"] = None
            signal["ob_bid_total"] = None
            signal["ob_ask_total"] = None
            return signal

        ob_age = (datetime.now(timezone.utc) - ob.timestamp).total_seconds()

        if ob_age > OB_MAX_AGE_SECONDS:
            logger.debug(
                f"{symbol}: order book застарів ({ob_age:.1f}с) — продовжуємо без OB"
            )
            signal["ob_imbalance"] = ob.imbalance
            signal["ob_bid_total"] = ob.bid_total
            signal["ob_ask_total"] = ob.ask_total
            signal["ob_stale"] = True
            return signal

        ob_direction = self._get_ob_signal(symbol)

        # Hard-filter тільки для великої стіни проти входу
        if USE_ORDER_BOOK_WALL_FILTER and ob.has_wall_against(direction, signal["entry"]):
            logger.info(f"{symbol}: велика стіна проти {direction.upper()} — skip")
            return None

        # Direction-фільтр вмикається тільки якщо явно треба
        if USE_ORDER_BOOK_CONFIRMATION and ob_direction != direction:
            logger.debug(
                f"{symbol}: OB hard-filter не підтверджує {direction.upper()} "
                f"(OB: {ob_direction}) | {ob.summary()}"
            )
            return None

        signal["ob_imbalance"] = ob.imbalance
        signal["ob_bid_total"] = ob.bid_total
        signal["ob_ask_total"] = ob.ask_total
        signal["ob_direction"] = ob_direction
        signal["ob_confirmed"] = ob_direction == direction

        logger.info(
            f"✅ SIGNAL ACCEPTED {direction.upper()} {symbol} | "
            f"OB={ob_direction} imbalance={ob.imbalance:+.1f}% | "
            f"hard_ob={USE_ORDER_BOOK_CONFIRMATION}"
        )

        return signal

    def _get_ob_signal(self, symbol: str) -> Optional[str]:
        """
        Напрямок за середнім + медіаною останніх 10 imbalance значень.
        Захист від шуму одиночного великого ордера.
        """
        import statistics

        with self._ob_lock:
            ob      = self.state.ob_snapshots.get(symbol)
            history = list(self.state.ob_imbalance_history.get(symbol, []))

        if ob is None:
            return None

        ob_age = (datetime.now(timezone.utc) - ob.timestamp).total_seconds()
        if ob_age > OB_MAX_AGE_SECONDS:
            return None

        if len(history) < 5:
            return None

        recent  = history[-10:]
        avg     = sum(recent) / len(recent)
        median  = statistics.median(recent)
        current = ob.imbalance

        logger.debug(
            f"{symbol} OB: current={current:+.1f}% avg={avg:+.1f}% median={median:+.1f}%"
        )

        if current > OB_IMBALANCE_LONG_MIN and avg > OB_IMBALANCE_LONG_MIN * 0.7 and median > 0:
            return "long"
        if current < OB_IMBALANCE_SHORT_MAX and avg < OB_IMBALANCE_SHORT_MAX * 0.7 and median < 0:
            return "short"

        return None

    # ── Виконання ордера ──────────────────────────────────────

    async def _execute_trade(self, signal: dict) -> None:
        symbol    = signal["symbol"]
        direction = signal["direction"]
        entry     = Decimal(str(signal["entry"]))
        tp        = Decimal(str(signal["tp"]))
        sl        = Decimal(str(signal["sl"]))

        sig_min_rr = signal.get("min_rr")
        pos = calculate_position(
            symbol      = symbol,
            deposit     = self.state.equity,
            risk_pct    = Decimal(str(RISK_PER_TRADE_PCT)),
            entry_price = entry,
            stop_loss   = sl,
            take_profit = tp,
            min_rr      = Decimal(str(sig_min_rr)) if sig_min_rr is not None else None,
        )

        if "error" in pos:
            logger.error(f"Позиція неможлива: {pos['error']}")
            return

        if not pos["rr_ok"]:
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

        try:
            order = self.rest.create_order(
                symbol = symbol,
                type   = "market",
                side   = side,
                amount = qty,
                params = {"positionIdx": 0},
            )
            logger.info(
                f"✅ Ордер виконано: id={order['id']} | "
                f"filled={order.get('filled', qty)} | "
                f"price={order.get('average', 'market')}"
            )

            real_entry = float(order.get("average", signal["entry"]))
            tp_side    = "sell" if direction == "long" else "buy"

            # Take Profit
            self.rest.create_order(
                symbol = symbol, type = "limit", side = tp_side, amount = qty,
                price  = float(tp),
                params = {"reduceOnly": True, "timeInForce": "GTC"},
            )

            # Stop Loss
            self.rest.create_order(
                symbol = symbol, type = "market", side = tp_side, amount = qty,
                params = {
                    "reduceOnly":   True,
                    "stopPrice":    float(sl),
                    "triggerPrice": float(sl),
                    "orderType":    "Stop",
                },
            )

            logger.info(f"✅ TP={float(tp):.4f} SL={float(sl):.4f} встановлено")

            trade_record = {
                "symbol":      symbol,
                "direction":   direction,
                "entry":       real_entry,
                "qty":         qty,
                "tp":          float(tp),
                "sl":          float(sl),
                "order_id":    order["id"],
                "opened_at":   datetime.now(timezone.utc),
                "risk_usdt":   float(pos["risk_usdt"]),
                "reward_usdt": float(pos["reward_usdt"]),
                "ob_imbalance": signal.get("ob_imbalance", 0),
                "mode":        signal.get("mode", "unknown"),
                "strategy":    signal.get("strategy", signal.get("mode", "unknown")),
                "cvd_ok":       signal.get("cvd_ok"),
                "volume_ok":    signal.get("volume_ok"),
                "ob_direction": signal.get("ob_direction"),
                "ob_confirmed": signal.get("ob_confirmed"),
                "ob_stale":     signal.get("ob_stale", False),
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
            }

            self.state.open_trade      = trade_record
            self.state.last_trade_time = datetime.now(timezone.utc)
            self.state.daily_trades   += 1

            # Push-сповіщення в Telegram (безпечне; нема нотифаєра → no-op)
            await self._notify("notify_trade_opened", trade_record)

            # TODO: log_trade_open(trade_record)  ← підключимо в наступному кроці

        except Exception as e:
            logger.error(f"❌ Помилка виконання ордера: {e}")

    # ── Моніторинг позиції ────────────────────────────────────

    async def _monitor_position(self) -> None:
        if not self.state.open_trade:
            return

        symbol = self.state.open_trade["symbol"]
        try:
            positions = self.rest.fetch_positions([symbol])
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
            history = self.rest.fetch_my_trades(symbol=trade["symbol"], limit=5)
            closing = [
                t for t in history
                if t["timestamp"] > int(trade["opened_at"].timestamp() * 1000)
                and t.get("reduceOnly", False)
            ]
            pnl = sum(float(t.get("info", {}).get("realizedPnl", 0)) for t in closing)

            if pnl == 0:
                current = float(self.rest.fetch_ticker(trade["symbol"])["last"])
                pnl = (
                    (current - trade["entry"]) * trade["qty"]
                    if trade["direction"] == "long"
                    else (trade["entry"] - current) * trade["qty"]
                )
                pnl -= trade["entry"] * trade["qty"] * float(BYBIT_TAKER_FEE) * 2

            self.state.equity    += Decimal(str(pnl))
            self.state.daily_pnl += Decimal(str(pnl))

            result = "✅ PROFIT" if pnl > 0 else "❌ LOSS"
            logger.info(
                f"{result} | {trade['direction'].upper()} {trade['symbol']} | "
                f"P&L=${pnl:.2f} | equity=${self.state.equity:.2f}"
            )

            if pnl < 0:
                self.state.loss_streak   += 1
                self.state.last_loss_time = datetime.now(timezone.utc)
            else:
                self.state.loss_streak = 0

            # Push-сповіщення про закриття (безпечне; нема нотифаєра → no-op)
            await self._notify("notify_trade_closed", float(pnl), trade)

            # TODO: log_trade_close(trade["order_id"], pnl, result)

            self.state.open_trade = None

        except Exception as e:
            logger.error(f"Помилка обробки закритої позиції: {e}")
            self.state.open_trade = None

    # ── Допоміжні методи ──────────────────────────────────────

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

    def _get_df(self, symbol: str, tf: str) -> pd.DataFrame | None:
        key = f"{symbol}_{tf}"
        buf = self.state.candles.get(key)
        if not buf or len(buf) < 10:
            return None

        df = pd.DataFrame(
            list(buf),
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        # Дедуплікація: REST-початкові свічки і WS-confirm можуть дати
        # дубль останнього timestamp (forming vs closed). Лишаємо останній —
        # інакше BB/RSI/VWAP рахуються по «зайвій» свічці.
        df = df[~df.index.duplicated(keep="last")]
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
        if (self.state.last_trade_time and
                now.date() > self.state.last_trade_time.date()):
            logger.info(
                f"📅 Новий день | P&L: ${self.state.daily_pnl:.2f} | "
                f"угод: {self.state.daily_trades}"
            )
            self.state.daily_pnl    = Decimal("0")
            self.state.daily_trades = 0

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
        trader.stop()
        logger.info("👋 Зупинено користувачем")