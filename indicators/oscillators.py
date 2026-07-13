"""
ОСЦИЛЯТОРИ та СЕРЕДНІ — спільні індикатори для нових стратегій
================================================================
Цей модуль НЕ дублює entry.py / range_detector.py. Тут лежать
індикатори, які потрібні mean-reversion (BB+RSI) і VWAP стратегіям:

  rsi()              — Relative Strength Index (Wilder)
  bollinger_bands()  — Bollinger Bands (mid=SMA, upper/lower=±std·σ)
  ema()              — Exponential Moving Average
  vwap_rolling()     — ковзний VWAP (volume weighted average price)
  vwap_bands()       — VWAP ± k·σ (σ — зважене відхилення ціни від VWAP)
  adx()              — Average Directional Index (сила тренду, фільтр)

Усі функції повертають pd.Series/dict і НЕ модифікують вхідний df.
Кожна має fallback на коротких даних, щоб не кидати виняток у live-циклі.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────
# RSI — Relative Strength Index (метод Wilder)
# ─────────────────────────────────────────────────────────────

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    RSI за класичним методом Wilder (ewm з alpha=1/period).

    RSI < oversold  → перепроданість (mean-reversion LONG)
    RSI > overbought→ перекупленість  (mean-reversion SHORT)

    period=2 (Larry Connors RSI-2) → дуже чутливий, багато сигналів.
    period=14 → класичний, спокійніший.

    Повертає pd.Series у діапазоні 0..100. На коротких даних —
    заповнено 50 (нейтрально), щоб не блокувати і не хибити.
    """
    close = close.astype(float)
    if len(close) < period + 1:
        return pd.Series(50.0, index=close.index)

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))

    # avg_loss==0 → RSI=100 (тільки зростання); avg_gain==0 → RSI=0;
    # обидва нулі (повністю плаский ринок) → нейтральні 50.
    out = out.where(avg_loss != 0.0, 100.0)
    out.loc[(avg_gain == 0.0) & (avg_loss != 0.0)] = 0.0
    out.loc[(avg_gain == 0.0) & (avg_loss == 0.0)] = 50.0
    return out.fillna(50.0)


# ─────────────────────────────────────────────────────────────
# EMA
# ─────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int = 50) -> pd.Series:
    """Exponential Moving Average. Fallback — сам ряд при нестачі даних."""
    series = series.astype(float)
    if len(series) < 2:
        return series.copy()
    return series.ewm(span=period, adjust=False).mean()


# ─────────────────────────────────────────────────────────────
# Bollinger Bands
# ─────────────────────────────────────────────────────────────

def bollinger_bands(
    close: pd.Series,
    period: int = 20,
    std: float = 2.0,
) -> dict:
    """
    Bollinger Bands.
      mid   = SMA(close, period)
      upper = mid + std · σ
      lower = mid - std · σ

    Повертає dict з поточними (останніми) значеннями + Series:
      mid, upper, lower            — float (останні)
      width_pct                    — (upper-lower)/mid·100, ширина каналу
      percent_b                    — позиція ціни в каналі: 0=lower, 1=upper
      mid_s, upper_s, lower_s      — повні Series (для бектесту/логіки)

    width_pct малий → вузький канал (squeeze) → близький пробій.
    percent_b <= 0  → ціна на/під нижньою смугою (LONG mean-reversion).
    percent_b >= 1  → ціна на/над верхньою смугою (SHORT mean-reversion).
    """
    close = close.astype(float)
    n = len(close)
    if n < period:
        last = float(close.iloc[-1]) if n else 0.0
        return {
            "mid": last, "upper": last, "lower": last,
            "width_pct": 0.0, "percent_b": 0.5,
            "mid_s": close.copy(), "upper_s": close.copy(), "lower_s": close.copy(),
            "valid": False,
        }

    mid_s = close.rolling(period).mean()
    sigma = close.rolling(period).std(ddof=0)
    upper_s = mid_s + std * sigma
    lower_s = mid_s - std * sigma

    mid = float(mid_s.iloc[-1])
    upper = float(upper_s.iloc[-1])
    lower = float(lower_s.iloc[-1])
    price = float(close.iloc[-1])

    band = upper - lower
    width_pct = (band / mid * 100.0) if mid > 0 else 0.0
    percent_b = ((price - lower) / band) if band > 0 else 0.5

    return {
        "mid": mid, "upper": upper, "lower": lower,
        "width_pct": width_pct, "percent_b": percent_b,
        "mid_s": mid_s, "upper_s": upper_s, "lower_s": lower_s,
        "valid": True,
    }


# ─────────────────────────────────────────────────────────────
# VWAP — ковзний (rolling) Volume Weighted Average Price
# ─────────────────────────────────────────────────────────────

def vwap_rolling(df: pd.DataFrame, window: Optional[int] = None) -> pd.Series:
    """
    VWAP = Σ(typical_price · volume) / Σ(volume).

    typical_price = (high + low + close) / 3

    window=None  → кумулятивний VWAP від початку df (наближення "сесійного"
                   VWAP; для безперервного крипторинку беремо весь буфер).
    window=N     → ковзний VWAP за останні N свічок (стабільніше для скальпу,
                   бо не "тягне" старі дані нескінченно).

    Повертає pd.Series тієї ж довжини, що й df.
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    vol = df["volume"].astype(float).clip(lower=0.0)

    tp = (high + low + close) / 3.0
    pv = tp * vol

    if window is None or window <= 0:
        cum_v = vol.cumsum().replace(0.0, np.nan)
        return (pv.cumsum() / cum_v).fillna(tp)

    # min_periods=1 gives a causal cumulative warmup, then a true rolling
    # window, without changing the already calculated prefix later.
    roll_v = vol.rolling(window, min_periods=1).sum().replace(0.0, np.nan)
    return (pv.rolling(window, min_periods=1).sum() / roll_v).fillna(tp)


def vwap_bands(
    df: pd.DataFrame,
    window: Optional[int] = None,
    k: float = 2.0,
    anchor: Optional[str] = None,
) -> dict:
    """
    VWAP ± k·σ, де σ — зважене стандартне відхилення typical_price.

    ``anchor="session"`` скидає кумулятивні суми о 00:00 UTC. Без anchor
    використовується rolling/cumulative режим, сумісний зі старими викликами.

    Логіка σ:
      var = Eᵥ[tp²] - Eᵥ[tp]²               (volume-weighted variance)
      σ   = sqrt(var)

    Повертає dict з останніми значеннями + Series:
      vwap, upper, lower   — float (останні)
      sigma                — float, поточне відхилення
      dev_pct              — (price - vwap)/vwap·100, наскільки ціна далеко
      vwap_s, upper_s, lower_s — Series

    price < lower → ціна сильно нижче VWAP → LONG (повернення до VWAP)
    price > upper → ціна сильно вище VWAP → SHORT
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    vol = df["volume"].astype(float).clip(lower=0.0)

    tp = (high + low + close) / 3.0
    pv = tp * vol
    if anchor == "session":
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError("session VWAP потребує DatetimeIndex")
        session_index = df.index
        if session_index.tz is None:
            session_index = session_index.tz_localize("UTC")
        else:
            session_index = session_index.tz_convert("UTC")
        session = session_index.floor("D")
        cum_v = vol.groupby(session).cumsum().replace(0.0, np.nan)
        vwap_s = pv.groupby(session).cumsum() / cum_v
        second = ((tp ** 2) * vol).groupby(session).cumsum() / cum_v
        var_s = second - vwap_s ** 2
    else:
        vwap_s = vwap_rolling(df, window=window)
        if window is None or window <= 0:
            cum_v = vol.cumsum().replace(0.0, np.nan)
            second = ((tp ** 2) * vol).cumsum() / cum_v
            var_s = second - vwap_s ** 2
        else:
            roll_v = vol.rolling(window, min_periods=1).sum().replace(0.0, np.nan)
            second = (
                ((tp ** 2) * vol).rolling(window, min_periods=1).sum() / roll_v
            )
            var_s = second - vwap_s ** 2

    sigma_s = np.sqrt(var_s.clip(lower=0.0)).fillna(0.0)
    upper_s = vwap_s + k * sigma_s
    lower_s = vwap_s - k * sigma_s

    vwap = float(vwap_s.iloc[-1])
    price = float(close.iloc[-1])
    dev_pct = ((price - vwap) / vwap * 100.0) if vwap > 0 else 0.0

    return {
        "vwap": vwap,
        "upper": float(upper_s.iloc[-1]),
        "lower": float(lower_s.iloc[-1]),
        "sigma": float(sigma_s.iloc[-1]),
        "dev_pct": dev_pct,
        "vwap_s": vwap_s, "upper_s": upper_s, "lower_s": lower_s,
        "valid": vwap > 0,
    }


# ─────────────────────────────────────────────────────────────
# ADX — сила тренду (опційний фільтр)
# ─────────────────────────────────────────────────────────────

def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    ADX (Average Directional Index) — сила тренду 0..100.

      ADX < 20  → слабкий тренд / боковик  → ДОБРЕ для mean-reversion
      ADX > 25  → сильний тренд            → ризик для mean-reversion,
                                             добре для momentum/breakout

    Повертає causal pd.Series; ранні значення прогріваються поступово.
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100.0 * (plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0.0, np.nan))
    minus_di = 100.0 * (minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0.0, np.nan))

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1.0 / period, adjust=False).mean().fillna(0.0)
