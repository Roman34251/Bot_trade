import unittest

import pandas as pd

from indicators.oscillators import vwap_bands


def frame(values, start="2026-07-01 00:00:00+00:00", freq="5min"):
    idx = pd.date_range(start, periods=len(values), freq=freq)
    return pd.DataFrame(
        {
            "open": values,
            "high": values,
            "low": values,
            "close": values,
            "volume": [1.0] * len(values),
        },
        index=idx,
    )


class VwapBandsTests(unittest.TestCase):
    def test_rolling_variance_is_around_current_weighted_mean(self):
        result = vwap_bands(frame([10.0, 20.0]), window=2, k=2.0)
        self.assertAlmostEqual(result["vwap"], 15.0)
        self.assertAlmostEqual(result["sigma"], 5.0)
        self.assertAlmostEqual(result["upper"], 25.0)
        self.assertAlmostEqual(result["lower"], 5.0)

    def test_session_anchor_resets_at_utc_midnight(self):
        first = frame([10.0, 20.0], start="2026-07-01 23:50:00+00:00")
        second = frame([100.0, 100.0], start="2026-07-02 00:00:00+00:00")
        df = pd.concat([first, second])
        result = vwap_bands(df, window=None, k=2.0, anchor="session")
        self.assertAlmostEqual(result["vwap"], 100.0)
        self.assertAlmostEqual(result["sigma"], 0.0)

    def test_session_anchor_requires_datetime_index(self):
        df = frame([10.0, 20.0]).reset_index(drop=True)
        with self.assertRaises(ValueError):
            vwap_bands(df, anchor="session")


if __name__ == "__main__":
    unittest.main()
