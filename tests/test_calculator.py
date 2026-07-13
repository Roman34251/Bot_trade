"""Unit tests for fee-aware position sizing."""

import unittest
from decimal import Decimal
import sys
import types
from unittest.mock import patch

# Keep this unit test runnable in the minimal bundled Python runtime.  The
# calculator only needs a logger and these scalar settings at import time.
fake_loguru = types.ModuleType("loguru")
fake_loguru.logger = types.SimpleNamespace(debug=lambda *args, **kwargs: None)
sys.modules.setdefault("loguru", fake_loguru)

fake_settings = types.ModuleType("config.settings")
fake_settings.MIN_RISK_REWARD = 1.2
fake_settings.MAX_DAILY_LOSS_PCT = 0.03
fake_settings.MAX_TRADES_PER_DAY = 30
fake_settings.MAX_CONSECUTIVE_LOSSES = 4
fake_settings.BYBIT_TAKER_FEE = 0.00055
fake_settings.BYBIT_MAKER_FEE = 0.00020
fake_settings.BTC_SLIPPAGE_PCT = 0.00015
fake_settings.SOL_SLIPPAGE_PCT = 0.00030
sys.modules.setdefault("config.settings", fake_settings)

from signals import calculator


D = Decimal
SYMBOL = "BTC/USDT:USDT"


class CalculatePositionTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(calculator, "BYBIT_TAKER", D("0.00055")),
            patch.object(calculator, "BYBIT_MAKER", D("0.00020")),
            patch.dict(calculator.SLIPPAGE, {SYMBOL: D("0.00015")}),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def _calculate(self, **overrides):
        params = {
            "symbol": SYMBOL,
            "deposit": D("1000"),
            "risk_pct": D("0.01"),
            "entry_price": D("100000"),
            "stop_loss": D("99000"),
            "take_profit": D("102000"),
            "min_rr": D("0"),
        }
        params.update(overrides)
        return calculator.calculate_position(**params)

    def _exact_stop_loss(self, result, *, is_long):
        quantity = result["quantity"]
        entry = result["entry_price"]
        stop = result["stop_loss"]
        slip = calculator.SLIPPAGE[SYMBOL]
        if is_long:
            real_entry = entry * (D("1") + slip)
            real_stop = stop * (D("1") - slip)
            gross_loss = (real_entry - real_stop) * quantity
        else:
            real_entry = entry * (D("1") - slip)
            real_stop = stop * (D("1") + slip)
            gross_loss = (real_stop - real_entry) * quantity
        return (
            gross_loss
            + quantity * real_entry * calculator.BYBIT_TAKER
            + quantity * real_stop * calculator.BYBIT_TAKER
        )

    def test_long_quantity_uses_full_net_stop_loss(self):
        result = self._calculate()

        self.assertNotIn("error", result)
        self.assertEqual(result["quantity"], D("0.008"))
        exact_loss = self._exact_stop_loss(result, is_long=True)
        self.assertLessEqual(exact_loss, D("10"))
        self.assertEqual(result["risk_budget"], D("10.00"))
        # Gross-only sizing would have returned 0.010 BTC and breached budget.
        self.assertLess(result["quantity"], D("0.010"))

    def test_short_quantity_uses_full_net_stop_loss(self):
        result = self._calculate(
            stop_loss=D("101000"),
            take_profit=D("98000"),
        )

        self.assertNotIn("error", result)
        self.assertEqual(result["quantity"], D("0.008"))
        exact_loss = self._exact_stop_loss(result, is_long=False)
        self.assertLessEqual(exact_loss, D("10"))

    def test_max_notional_caps_quantity_after_risk_sizing(self):
        result = self._calculate(
            risk_pct=D("0.10"),
            stop_loss=D("99900"),
            max_notional=D("5000"),
        )

        self.assertNotIn("error", result)
        self.assertEqual(result["quantity"], D("0.050"))
        self.assertLessEqual(result["position_value"], D("5000"))
        self.assertEqual(result["max_notional"], D("5000"))

    def test_max_leverage_caps_notional_to_deposit_multiple(self):
        result = self._calculate(
            risk_pct=D("0.10"),
            stop_loss=D("99900"),
            max_leverage=D("2"),
        )

        self.assertNotIn("error", result)
        self.assertEqual(result["quantity"], D("0.020"))
        self.assertLessEqual(result["position_value"], D("2000"))
        self.assertEqual(result["max_notional"], D("2000"))

    def test_stricter_of_notional_and_leverage_caps_wins(self):
        result = self._calculate(
            risk_pct=D("0.10"),
            stop_loss=D("99900"),
            max_notional=D("5000"),
            max_leverage=D("2"),
        )

        self.assertNotIn("error", result)
        self.assertEqual(result["quantity"], D("0.020"))
        self.assertEqual(result["max_notional"], D("2000"))

    def test_legacy_call_signature_remains_supported(self):
        result = calculator.calculate_position(
            SYMBOL,
            D("1000"),
            D("0.01"),
            D("100000"),
            D("99000"),
            D("102000"),
            D("0"),
        )

        self.assertNotIn("error", result)
        self.assertLessEqual(
            self._exact_stop_loss(result, is_long=True),
            D("10"),
        )

    def test_invalid_exposure_caps_fail_closed(self):
        self.assertIn("error", self._calculate(max_notional=D("0")))
        self.assertIn("error", self._calculate(max_leverage=D("0")))

    def test_daily_loss_limit_blocks_at_exact_boundary(self):
        result = calculator.check_daily_limits(
            daily_pnl=D("-30"),
            trade_count=1,
            loss_streak=0,
            deposit=D("1000"),
        )
        self.assertFalse(result["can_trade"])

    def test_actual_fill_recheck_does_not_apply_entry_slippage_twice(self):
        initial = self._calculate()
        modeled_fill = D("100000") * (D("1") + calculator.SLIPPAGE[SYMBOL])
        recheck = self._calculate(
            entry_price=modeled_fill,
            entry_is_filled=True,
        )
        self.assertNotIn("error", recheck)
        self.assertGreaterEqual(recheck["quantity"], initial["quantity"])


if __name__ == "__main__":
    unittest.main()
