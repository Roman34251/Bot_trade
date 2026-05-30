"""
ВИЗНАЧЕННЯ РЕЙНДЖУ — старший ТФ (1h / 30m)
=============================================
Рейндж визначаємо ОДИН РАЗ на годину і кешуємо.
НЕ перераховуємо на кожній 1m свічці — це дорого і непотрібно.

Індикатори:
  Bollinger Bands — звуження ширини = консолідація
  ATR             — волатильність для SL і підтвердження девіації
"""

import pandas as pd
import numpy as np




# ── Fallback реалізації якщо pandas_ta не встановлено ─────

def _atr_manual(high: pd.Series, low: pd.Series, close: pd.Series,
                period: int) -> pd.Series:
    """True Range і ATR без pandas_ta."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def _bbands_manual(close: pd.Series, length: int = 20,
                   std: float = 2.0) -> dict:
    """Bollinger Bands без pandas_ta."""
    mid   = close.rolling(length).mean()
    sigma = close.rolling(length).std()
    return {
        "upper": mid + std * sigma,
        "lower": mid - std * sigma,
        "mid":   mid,
    }


def _stoch_manual(high: pd.Series, low: pd.Series, close: pd.Series,
                  k: int = 5, d: int = 3, smooth_k: int = 3) -> dict:
    """Stochastic без pandas_ta."""
    lowest_low   = low.rolling(k).min()
    highest_high = high.rolling(k).max()
    raw_k = 100 * (close - lowest_low) / (highest_high - lowest_low + 1e-9)
    smooth_K = raw_k.rolling(smooth_k).mean()
    smooth_D = smooth_K.rolling(d).mean()
    return {"k": smooth_K, "d": smooth_D}


# ── Основні функції ────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    ATR = середній True Range за N свічок.

    Використання:
      На 1h period=14 → для визначення розміру рейнджу і SL
      На 1m period=7  → для точного SL маркет ордера

    Орієнтири:
      BTC 1h ATR: ~$200-500 (залежить від волатильності)
      SOL 1h ATR: ~$1.5-4

    Девіація = вихід за межу рейнджу більше ніж на 0.3 ATR.
    SL = екстремум девіації ± 0.3 ATR буфер.
    """
    if len(df) < period + 1:
        # Недостатньо даних — повертаємо простий range
        return float((df['high'] - df['low']).mean())

    atr_series = _atr_manual(
        df['high'],
        df['low'],
        df['close'],
        period
    )

    val = atr_series.iloc[-1]
    return float(val) if not pd.isna(val) else float((df['high'] - df['low']).mean())


def detect_bb_squeeze(df: pd.DataFrame,
                      length: int = 20, std: float = 2.0) -> dict:
    """
    Bollinger Bands squeeze = ціна стиснута = очікуємо девіацію.

    Ширина BB в % від середини:
      < 2.0% = сильне стиснення → рейндж підтверджено
      > 3.0% = розширення → можливий тренд

    Розраховуємо на 1h або 30m свічках.
    НЕ викликаємо на 1m — занадто шумно.
    """
    if len(df) < length:
        return {"is_squeeze": False, "width_pct": 999.0,
                "upper": 0.0, "lower": 0.0, "mid": 0.0}

    bb = _bbands_manual(
        df['close'],
        length,
        std
    )

    upper = float(bb["upper"].iloc[-1])
    lower = float(bb["lower"].iloc[-1])
    mid = float(bb["mid"].iloc[-1])

    if mid == 0 or pd.isna(mid):
        return {"is_squeeze": False, "width_pct": 999.0,
                "upper": upper, "lower": lower, "mid": mid}

    width_pct = (upper - lower) / mid * 100

    return {
        "upper":      upper,
        "lower":      lower,
        "mid":        mid,
        "width_pct":  width_pct,
        "is_squeeze": width_pct < 2.0,   # < 2% = боковик
    }


def detect_range(df_slow: pd.DataFrame,
                 min_candles: int = 20,
                 squeeze_pct: float = 2.0) -> dict | None:
    """
    Визначає рейндж на старшому ТФ (1h або 30m).

    Алгоритм:
      1. BB squeeze (ширина < squeeze_pct%)
      2. High і Low останніх min_candles свічок = межі рейнджу

    Кешування:
      Результат зберігати в engine і оновлювати раз на годину.
      НЕ викликати на кожній 1m свічці.

    Повертає None якщо рейнджу немає (тренд або недостатньо даних).

    Структура результату:
      high     — верхня межа рейнджу (опір)
      low      — нижня межа рейнджу (підтримка)
      mid      — середина рейнджу
      size     — розмір рейнджу в $
      size_pct — розмір рейнджу в % від mid
      bb_*     — параметри BB
      candles  — кількість свічок в рейнджі
    """
    if len(df_slow) < min_candles:
        return None

    recent = df_slow.iloc[-min_candles:]
    bb     = detect_bb_squeeze(recent, squeeze_pct=squeeze_pct)

    if not bb["is_squeeze"]:
        return None

    rng_h     = float(recent['high'].max())
    rng_l     = float(recent['low'].min())
    mid       = (rng_h + rng_l) / 2
    size      = rng_h - rng_l
    size_pct  = size / mid * 100 if mid > 0 else 0

    return {
        "high":      rng_h,
        "low":       rng_l,
        "mid":       mid,
        "size":      size,
        "size_pct":  size_pct,
        "bb_upper":  bb["upper"],
        "bb_lower":  bb["lower"],
        "bb_width":  bb["width_pct"],
        "candles":   min_candles,
    }