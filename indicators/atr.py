"""
ATR — Average True Range
=========================
Міра волатильності ринку за N свічок.

Використання в стратегії:
  ATR(1h, 14) → визначає розмір рейнджу та SL буфер
  ATR × 0.15  = sweep_buffer (мін. розмір sweep за межу рейнджу)
  ATR × 0.12  = stop_pad (буфер SL за фетилем)

Формула:
  True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
  ATR = EWM(True Range, span=period)
"""

import pandas as pd


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Параметри:
        df     — DataFrame з колонками high, low, close
        period — кількість свічок (default 14)

    Повертає: Series з ATR значеннями
    """
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    close = df["close"].astype(float)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    return tr.ewm(span=period, adjust=False).mean()