"""
ATR — Average True Range
=========================
Що це: міра волатильності ринку за N свічок.

Навіщо в скальпінгу:
  - ATR(1h) → визначає розмір рейнджу (великий чи малий боковик)
  - ATR(1m) → розраховує точний SL за фетилем девіації
  - ATR(1m) × 0.3 = буфер SL щоб не зачепило стопи достроково

Формула:
  True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
  ATR = середнє True Range за N свічок
"""

import pandas as pd
import numpy as np


def calculate_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """
    Розраховує ATR для DataFrame з колонками high, low, close.

    Параметри:
        df     — DataFrame з OHLCV
        period — кількість свічок (14 для 1h, 7 для 1m)

    Повертає: Series з ATR значеннями (та сама довжина що df)
    """
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    # True Range — найбільший з трьох варіантів руху
    tr = pd.concat([
        high - low,                        # діапазон свічки
        (high - close.shift(1)).abs(),     # від попереднього закриття вгору
        (low  - close.shift(1)).abs(),     # від попереднього закриття вниз
    ], axis=1).max(axis=1)

    # Exponential moving average для більшої ваги свіжих даних
    atr = tr.ewm(span=period, adjust=False).mean()

    return atr


def add_atr(df: pd.DataFrame, period_slow: int = 14, period_fast: int = 7) -> pd.DataFrame:
    """
    Додає ATR колонки до DataFrame.

    atr_slow — ATR(14) на 1h → визначає розмір рейнджу
    atr_fast — ATR(7)  на 1m → точний SL для скальпінгу

    Після виклику df матиме нові колонки:
        atr_slow, atr_fast
    """
    df = df.copy()
    df["atr_slow"] = calculate_atr(df, period_slow)
    df["atr_fast"] = calculate_atr(df, period_fast)
    return df
