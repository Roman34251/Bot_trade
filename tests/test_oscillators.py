import unittest

import numpy as np
import pandas as pd

from indicators.oscillators import adx, rsi, vwap_bands


class OscillatorEdgeCaseTests(unittest.TestCase):
    @staticmethod
    def _frame(length: int) -> pd.DataFrame:
        idx = pd.date_range("2026-01-01", periods=length, freq="5min", tz="UTC")
        base = 100.0 + np.sin(np.arange(length) / 4.0) + np.arange(length) * 0.03
        return pd.DataFrame(
            {
                "open": base - 0.1,
                "high": base + 0.5,
                "low": base - 0.5,
                "close": base,
                "volume": 1.0 + (np.arange(length) % 7),
            },
            index=idx,
        )

    def test_flat_rsi_is_neutral(self):
        close = pd.Series([100.0] * 30)
        self.assertEqual(float(rsi(close, 14).iloc[-1]), 50.0)

    def test_rolling_vwap_prefix_is_stable(self):
        full = self._frame(80)
        prefix = full.iloc[:35]
        from_prefix = vwap_bands(prefix, window=20, k=2.0)["vwap_s"]
        from_full = vwap_bands(full, window=20, k=2.0)["vwap_s"].iloc[:35]
        pd.testing.assert_series_equal(from_prefix, from_full)

    def test_adx_prefix_is_stable_during_warmup(self):
        full = self._frame(60)
        prefix = full.iloc[:20]
        pd.testing.assert_series_equal(adx(prefix, 14), adx(full, 14).iloc[:20])


if __name__ == "__main__":
    unittest.main()
