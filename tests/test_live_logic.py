"""Pure live-pipeline safety tests without network/client dependencies."""

import sys
import asyncio
import threading
import time
import types
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd


# The desktop test runtime intentionally does not bundle exchange clients.
fake_dotenv = types.ModuleType("dotenv")
fake_dotenv.load_dotenv = lambda *args, **kwargs: None
sys.modules["dotenv"] = fake_dotenv

if "loguru" not in sys.modules:
    fake_loguru = types.ModuleType("loguru")
    fake_loguru.logger = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        critical=lambda *args, **kwargs: None,
    )
    sys.modules["loguru"] = fake_loguru
else:
    existing_logger = sys.modules["loguru"].logger
    for method_name in ("debug", "info", "warning", "error", "critical"):
        if not hasattr(existing_logger, method_name):
            setattr(existing_logger, method_name, lambda *args, **kwargs: None)

# A previous unit module may have installed a scalar-only settings stub.
sys.modules.pop("config.settings", None)


class _ExchangeError(Exception):
    pass


fake_ccxt = types.ModuleType("ccxt")
fake_ccxt.__path__ = []
fake_ccxt.NetworkError = type("NetworkError", (_ExchangeError,), {})
fake_ccxt.RequestTimeout = type("RequestTimeout", (fake_ccxt.NetworkError,), {})
fake_ccxt.ExchangeNotAvailable = type(
    "ExchangeNotAvailable", (fake_ccxt.NetworkError,), {}
)
fake_ccxt.bybit = type("bybit", (), {})
fake_ccxt_pro = types.ModuleType("ccxt.pro")
fake_ccxt_pro.bybit = type("bybit", (), {})
fake_ccxt.pro = fake_ccxt_pro
sys.modules["ccxt"] = fake_ccxt
sys.modules["ccxt.pro"] = fake_ccxt_pro

fake_websockets = types.ModuleType("websockets")
fake_websockets.connect = None
sys.modules["websockets"] = fake_websockets

fake_database = types.ModuleType("storage.database")
fake_database.Database = object
sys.modules["storage.database"] = fake_database

from core.live_trade import LiveState, LiveTrader, OrderBookSnapshot


SYMBOL = "BTC/USDT:USDT"


class LiveGateTests(unittest.TestCase):
    def setUp(self):
        self.trader = object.__new__(LiveTrader)
        self.trader._ob_lock = threading.Lock()
        self.trader._paused = asyncio.Event()
        self.trader._paused.set()
        self.trader.state = LiveState(equity=Decimal("1000"), deposit=Decimal("1000"))

    def test_orderbook_and_flow_can_be_scoped_after_setup_detection(self):
        now = time.monotonic()
        rows = []
        for offset in (1.25, 1.0, 0.75, 0.5, 0.25, 0.05):
            snapshot = OrderBookSnapshot(
                symbol=SYMBOL,
                timestamp=datetime.now(timezone.utc),
                bids=[[100.0, 10.0]],
                asks=[[100.1, 6.0]],
                received_mono=now - offset,
            )
            snapshot.analyze()
            rows.append(snapshot)
        self.trader.state.ob_snapshot_history[SYMBOL] = rows

        self.assertEqual(self.trader._get_ob_signal(SYMBOL), "long")
        self.assertIsNone(
            self.trader._get_ob_signal(SYMBOL, since_mono=now - 0.2)
        )

        self.trader.state.trade_flow_history[SYMBOL] = [
            (now - 0.50, 1_000.0, 30_000.0),
            (now - 0.20, 1_001.0, 30_000.0),
            (now - 0.05, 1_002.0, 30_000.0),
        ]
        direction, meta = self.trader._get_trade_flow_signal(SYMBOL)
        self.assertEqual(direction, "long")
        self.assertGreater(meta["total"], 50_000)
        direction_after, _ = self.trader._get_trade_flow_signal(
            SYMBOL, since_mono=now - 0.01
        )
        self.assertIsNone(direction_after)

    def test_signal_dedup_is_scoped_by_symbol_and_strategy(self):
        frame = pd.DataFrame(
            {"close": [100.0]}, index=[pd.Timestamp("2026-01-01", tz="UTC")]
        )
        dfs = {"5m": frame}
        signal = {
            "symbol": SYMBOL,
            "strategy": "trend",
            "direction": "long",
        }
        self.assertIsNotNone(self.trader._deduplicate_signal(dict(signal), dfs))
        self.assertIsNone(self.trader._deduplicate_signal(dict(signal), dfs))

    def test_daily_loss_streak_is_restored_after_restart(self):
        rows = [
            {"updatedTime": "1000", "closedPnl": "2.0"},
            {"updatedTime": "2000", "closedPnl": "-1.0"},
            {"updatedTime": "3000", "closedPnl": "-0.5"},
        ]
        self.trader._restore_daily_risk_state(rows)
        self.assertEqual(self.trader.state.daily_pnl, Decimal("0.5"))
        self.assertEqual(self.trader.state.daily_trades, 3)
        self.assertEqual(self.trader.state.loss_streak, 2)
        self.assertIsNotNone(self.trader.state.last_loss_time)
        self.trader._restore_recent_outcomes(rows)
        self.assertEqual(list(self.trader.state.recent_outcomes), [1, 0, 0])
        self.trader._restore_daily_risk_state([])
        self.assertEqual(self.trader.state.loss_streak, 2)

    def test_closed_pnl_aggregates_partial_closes(self):
        rows = [
            {
                "updatedTime": "2000",
                "avgEntryPrice": "100",
                "avgExitPrice": "102",
                "qty": "1.0",
                "closedSize": "0.4",
                "closedPnl": "0.7",
                "openFee": "0.01",
                "closeFee": "0.01",
            },
            {
                "updatedTime": "3000",
                "avgEntryPrice": "100",
                "avgExitPrice": "103",
                "qty": "1.0",
                "closedSize": "0.6",
                "closedPnl": "1.7",
                "openFee": "0.02",
                "closeFee": "0.02",
            },
        ]
        self.trader._pybit = types.SimpleNamespace(
            get_closed_pnl=lambda **kwargs: {"result": {"list": rows}}
        )
        result = self.trader._fetch_closed_pnl(
            SYMBOL, opened_at_ms=1000, expected_entry=100.0, expected_qty=1.0
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["qty"], 1.0)
        self.assertAlmostEqual(result["pnl"], 2.4)
        self.assertAlmostEqual(result["avg_exit"], 102.6)

    def test_sizing_uses_worst_allowed_fill(self):
        signal_entry = Decimal("62500")
        self.assertEqual(
            self.trader._adverse_sizing_entry(
                signal_entry, Decimal("62520"), "long", 12
            ),
            Decimal("62575.0000"),
        )
        self.assertEqual(
            self.trader._adverse_sizing_entry(
                signal_entry, Decimal("62480"), "short", 12
            ),
            Decimal("62425.0000"),
        )

    def test_full_tpsl_uses_market_orders(self):
        self.assertEqual(
            self.trader._full_tpsl_params(110.0, 95.0),
            {
                "takeProfit": 110.0,
                "stopLoss": 95.0,
                "tpslMode": "Full",
                "tpOrderType": "Market",
                "slOrderType": "Market",
            },
        )

    def test_safety_latch_cannot_look_like_normal_pause(self):
        self.trader._trip_safety_latch("unknown submit")
        self.assertFalse(self.trader._paused.is_set())
        self.assertEqual(self.trader.state.safety_latch_reason, "unknown submit")

    def test_maker_cancel_waits_for_terminal_status_after_ack(self):
        self.trader.rest = types.SimpleNamespace(
            cancel_order=lambda *args, **kwargs: {"id": "1"}
        )
        states = iter([
            {"orderStatus": "New", "leavesQty": "1"},
            {"orderStatus": "PartiallyFilled", "leavesQty": "0.5"},
            {"orderStatus": "Cancelled", "leavesQty": "0"},
        ])
        calls = []

        def lookup(*args):
            calls.append(1)
            return next(states)

        self.trader._lookup_order_by_link_id = lookup
        result = asyncio.run(
            self.trader._cancel_entry_order(SYMBOL, "order-1", "link-1")
        )
        self.assertTrue(result)
        self.assertEqual(len(calls), 3)

    def test_daily_reset_happens_only_once_per_utc_day(self):
        self.trader.state.risk_day = date(2026, 1, 1)
        self.trader.state.daily_pnl = Decimal("-5")
        self.trader.state.daily_trades = 2
        now = datetime(2026, 1, 2, 0, 1, tzinfo=timezone.utc)
        self.trader._check_daily_reset(now)
        self.assertEqual(self.trader.state.daily_pnl, Decimal("0"))
        self.trader.state.daily_pnl = Decimal("3")
        self.trader._check_daily_reset(now)
        self.assertEqual(self.trader.state.daily_pnl, Decimal("3"))

    def test_closed_candle_freshness_uses_expected_bar_not_age_from_start(self):
        # At :28 the latest closed 1m candle is naturally 88s old from START,
        # but it is current and therefore has zero lag.
        now_ts = 1_800_000_028.0
        current_start = int(now_ts // 60) * 60
        self.trader.state.candles[f"{SYMBOL}_1m"] = [
            [(current_start - 60) * 1000, 1, 1, 1, 1, 1]
        ]
        self.assertEqual(
            self.trader._closed_candle_lag_sec(SYMBOL, "1m", now_ts=now_ts),
            0.0,
        )

        # One missing bar is reported as a full 60s lag.
        self.trader.state.candles[f"{SYMBOL}_1m"] = [
            [(current_start - 120) * 1000, 1, 1, 1, 1, 1]
        ]
        self.assertEqual(
            self.trader._closed_candle_lag_sec(SYMBOL, "1m", now_ts=now_ts),
            60.0,
        )

        # During the first 3s after close, the previous bar is still allowed.
        boundary_ts = current_start + 2
        self.assertEqual(
            self.trader._closed_candle_lag_sec(SYMBOL, "1m", now_ts=boundary_ts),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
