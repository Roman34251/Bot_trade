"""
ВИЗНАЧЕННЯ РЕЙНДЖУ — старший ТФ (1h / 30m)
============================================
Зміни відносно попередньої версії:
  - calculate_atr:      додано fallback-логіку (як в generator.py _safe_atr)
  - detect_bb_squeeze:  без змін (стабільна)
  - detect_range:       додано підтримку min_range_atr / max_range_atr /
                        max_drift_atr — параметри з _detect_active_range()
                        в generator.py; при squeeze_pct=None — пропускає
                        BB-фільтр і використовує тільки ATR-межі
  - detect_active_range: НОВА функція — пряма копія логіки
                        _detect_active_range() з generator.py але як
                        публічний API щоб dual_df.py міг її використати

Структура залежностей:
  generator.py  → _detect_active_range() (приватна, всередині файлу)
                  + calculate_atr() звідси для _safe_atr fallback
  dual_df.py    → detect_range() або detect_active_range() звідси
                  + calculate_atr() звідси
  signal_engine → detect_range() звідси (BB-based, старий шлях)
"""

from __future__ import annotations

from typing import Optional
import pandas as pd
from .atr import calculate_atr as _atr_series


# ─────────────────────────────────────────────────────────────
# ATR
# ─────────────────────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    Обгортка над atr.py — повертає float (останнє значення ATR).

    Зміни:
      Додано повний fallback-ланцюжок (аналог _safe_atr у generator.py):
        1. Спробувати через _atr_series (atr.py)
        2. Якщо не вдалось — ручний розрахунок TR
        3. Якщо даних замало — mean(high - low)

      Це важливо для dual_df.py де calculate_atr викликається першим
      і повинен повернути валідне число навіть на коротких df.

    Чому float а не Series:
      generate_scalp_signal(), generate_dual_tf_signal() і
      calculate_levels() потребують одне поточне число.
    """
    if len(df) < 2:
        return float((df["high"] - df["low"]).mean())

    # Спроба 1: через atr.py
    try:
        if len(df) >= period + 1:
            series = _atr_series(df, period)
            val    = series.iloc[-1]
            if pd.notna(val) and float(val) > 0:
                return float(val)
    except Exception:
        pass

    # Спроба 2: ручний ATR
    try:
        high       = df["high"].astype(float)
        low        = df["low"].astype(float)
        prev_close = df["close"].astype(float).shift(1)
        tr = pd.concat(
            [
                (high - low),
                (high - prev_close).abs(),
                (low  - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        val = tr.rolling(min(period, len(df))).mean().iloc[-1]
        if pd.notna(val) and float(val) > 0:
            return float(val)
    except Exception:
        pass

    # Спроба 3: fallback на mean HL
    return float((df["high"] - df["low"]).mean())


# ─────────────────────────────────────────────────────────────
# BB Squeeze
# ─────────────────────────────────────────────────────────────

def detect_bb_squeeze(
    df: pd.DataFrame,
    length: int = 20,
    std: float = 2.0,
    squeeze_pct: float = 2.0,
) -> dict:
    """
    Bollinger Bands squeeze.
    Ширина < squeeze_pct% відносно mid → боковик підтверджено.

    Використовується в:
      detect_range()   — для BB-based виявлення рейнджу
      signal_engine.py — перевірка squeeze активний (шар 1)
    """
    if len(df) < length:
        return {
            "is_squeeze": False,
            "width_pct":  999.0,
            "upper":      0.0,
            "lower":      0.0,
            "mid":        0.0,
        }

    mid   = df["close"].rolling(length).mean()
    sigma = df["close"].rolling(length).std()
    upper = mid + std * sigma
    lower = mid - std * sigma

    u = float(upper.iloc[-1])
    l = float(lower.iloc[-1])
    m = float(mid.iloc[-1])

    if m == 0 or pd.isna(m):
        return {
            "is_squeeze": False,
            "width_pct":  999.0,
            "upper":      u,
            "lower":      l,
            "mid":        m,
        }

    width_pct = (u - l) / m * 100
    return {
        "upper":      u,
        "lower":      l,
        "mid":        m,
        "width_pct":  width_pct,
        "is_squeeze": width_pct < squeeze_pct,
    }


# ─────────────────────────────────────────────────────────────
# detect_range — BB-based (для signal_engine.py)
# ─────────────────────────────────────────────────────────────

def detect_range(
    df_slow: pd.DataFrame,
    min_candles: int = 20,
    squeeze_pct: float = 2.0,
) -> Optional[dict]:
    """
    Визначає рейндж на старшому ТФ через BB squeeze.
    Повертає None якщо рейнджу немає.

    Використовується в:
      signal_engine.py — шар 1 (squeeze active)
      dual_df.py       — як fallback якщо cached_range = None
                         (поряд з detect_active_range)

    dict ключі:
      high, low, mid  — межі рейнджу
      size            — розмір в $
      size_pct        — розмір в %
      bb_upper, bb_lower, bb_width — параметри BB
    """
    if len(df_slow) < min_candles:
        return None

    recent = df_slow.iloc[-min_candles:]
    bb     = detect_bb_squeeze(recent, squeeze_pct=squeeze_pct)

    if not bb["is_squeeze"]:
        return None

    rng_h    = float(recent["high"].max())
    rng_l    = float(recent["low"].min())
    mid      = (rng_h + rng_l) / 2
    size     = rng_h - rng_l
    size_pct = size / mid * 100 if mid > 0 else 0

    return {
        "high":     rng_h,
        "low":      rng_l,
        "mid":      mid,
        "size":     size,
        "size_pct": size_pct,
        "bb_upper": bb["upper"],
        "bb_lower": bb["lower"],
        "bb_width": bb["width_pct"],
    }


# ─────────────────────────────────────────────────────────────
# detect_active_range — ATR-based (для generator.py / dual_df.py)
# ─────────────────────────────────────────────────────────────

def detect_active_range(
    df: pd.DataFrame,
    lookback: int = 48,
    atr: Optional[float] = None,
    atr_period: int = 14,
    min_range_atr: float = 1.5,
    max_range_atr: float = 8.0,
    max_drift_atr: float = 2.5,
) -> Optional[dict]:
    """
    ATR-based виявлення активного рейнджу.
    Публічний аналог _detect_active_range() з generator.py.

    Параметри:
      lookback      — скільки свічок аналізувати (48 для 1h = 2 дні)
      atr           — якщо вже розраховано, передати сюди
      min_range_atr — мінімальний розмір рейнджу в ATR (замалий = шум)
      max_range_atr — максимальний розмір рейнджу в ATR (завеликий = тренд)
      max_drift_atr — максимальний дрейф ціни всередині рейнджу

    Чому окремо від detect_range:
      detect_range() використовує BB squeeze → потребує щільної консолідації
      detect_active_range() використовує ATR межі → гнучкіший, підходить
      для generator.py де рейндж може бути ширшим але структурованим.

    Використовується в:
      generator.py  — _detect_active_range() (приватна копія всередині)
      dual_df.py    — може викликати напряму якщо cached_range = None

    Повертає dict з тими ж ключами що й detect_range() для сумісності:
      high, low, mid, size + atr, lookback (додаткові поля)
    """
    if len(df) < lookback + 1:
        return None

    # Розраховуємо ATR якщо не переданий
    if atr is None or atr <= 0:
        atr = calculate_atr(df, period=atr_period)
    if atr <= 0:
        return None

    window = df.tail(lookback).copy()
    if window.empty:
        return None

    range_high = float(window["high"].max())
    range_low  = float(window["low"].min())
    range_size = range_high - range_low

    # Перевірка ширини рейнджу
    if not (min_range_atr * atr <= range_size <= max_range_atr * atr):
        return None

    # Перевірка дрейфу (якщо ціна сильно зсунулась — це тренд, не рейндж)
    first_close = float(window["close"].iloc[0])
    last_close  = float(window["close"].iloc[-1])
    drift       = abs(last_close - first_close)

    if drift > max_drift_atr * atr:
        return None

    mid = (range_high + range_low) / 2.0

    return {
        "high":     range_high,
        "low":      range_low,
        "mid":      mid,
        "size":     range_size,
        "size_pct": range_size / mid * 100 if mid > 0 else 0.0,
        "atr":      atr,
        "lookback": float(lookback),
    }


# ─────────────────────────────────────────────────────────────
# normalize_cached_range — утиліта для роботи з кешем
# ─────────────────────────────────────────────────────────────

def normalize_cached_range(
    cached_range: dict | None,
    atr: float,
) -> Optional[dict]:
    """
    Нормалізує і валідує кешований рейндж.

    Публічний аналог _normalize_cached_range() з generator.py.
    Виніс сюди щоб dual_df.py міг використовувати без дублювання коду.

    Перевіряє:
      - наявність ключів high / low
      - high > low
      - заповнює відсутні поля (mid, size, atr)
    """
    if not cached_range:
        return None
    if "high" not in cached_range or "low" not in cached_range:
        return None

    high = float(cached_range["high"])
    low  = float(cached_range["low"])
    if high <= low:
        return None

    size = high - low
    mid  = float(cached_range.get("mid", (high + low) / 2.0))

    return {
        "high":     high,
        "low":      low,
        "mid":      mid,
        "size":     size,
        "size_pct": size / mid * 100 if mid > 0 else 0.0,
        "atr":      float(cached_range.get("atr", atr)),
        "lookback": float(cached_range.get("lookback", 0)),
    }