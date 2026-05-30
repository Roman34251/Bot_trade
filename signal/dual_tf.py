"""
ПАРАЛЕЛЬНИЙ РЕЙНДЖ 30m З ФІЛЬТРОМ 1h BIAS
===========================================
Додатковий режим що збільшує частоту угод БЕЗ зниження якості.

Логіка:
  1h  → визначає bias (напрямок ринку)
         bias = "bullish" якщо ціна в верхній половині 1h рейнджу
         bias = "bearish" якщо ціна в нижній половині
         bias = "neutral" якщо немає чіткого рейнджу на 1h

  30m → шукає рейндж і девіації всередині 1h рейнджу
         торгуємо тільки в напрямку 1h bias

ЧОМУ ЦЕ БЕЗПЕЧНО:
  30m рейндж = менший діапазон = більше сигналів
  1h bias фільтр = відсіює сигнали проти тренду
  Разом: більше угод з кращою якістю

ПРИКЛАД:
  1h рейндж: $94,000 - $96,000
  1h bias: bullish (ціна у верхній половині $95,000-$96,000)
  30m девіація вниз → ЛОНГ ✅ (в напрямку 1h bias)
  30m девіація вгору → ШОРТ ❌ (проти 1h bias — пропускаємо)
"""

from __future__ import annotations
import pandas as pd
from loguru import logger

from indicators.range_detector import detect_range, calculate_atr
from indicators.entry import (
    stochastic_signal, calculate_cvd, cvd_reversal,
    order_flow_delta, volume_on_deviation,
    detect_bos, calculate_levels,
)
from config.settings import (
    DEVIATION_ATR_MULT,
    STOCH_K, STOCH_D, STOCH_SMOOTH,
    CVD_LOOKBACK, OF_DELTA_LOOKBACK,
)


def get_1h_bias(df_1h: pd.DataFrame,
                cached_1h_range: dict | None = None) -> str:
    """
    Визначає bias (напрямок) на 1h таймфреймі.

    Логіка:
      Якщо є 1h рейндж:
        Поточна ціна > середини рейнджу → bullish bias
        Поточна ціна < середини рейнджу → bearish bias

      Якщо немає 1h рейнджу (тренд):
        EMA20 напрямок → bullish або bearish

    Повертає: "bullish", "bearish", "neutral"
    """
    current_price = float(df_1h['close'].iloc[-1])

    # Спробуємо визначити через рейндж
    range_1h = cached_1h_range or detect_range(df_1h)

    if range_1h:
        mid = range_1h["mid"]
        if current_price > mid * 1.002:   # + 0.2% буфер
            return "bullish"
        if current_price < mid * 0.998:   # - 0.2% буфер
            return "bearish"
        return "neutral"

    # Якщо рейнджу немає — дивимось на EMA20 напрямок
    ema = df_1h['close'].ewm(span=20, adjust=False).mean()
    ema_now  = float(ema.iloc[-1])
    ema_prev = float(ema.iloc[-3])   # 3 свічки тому

    if ema_now > ema_prev * 1.001:
        return "bullish"
    if ema_now < ema_prev * 0.999:
        return "bearish"
    return "neutral"


def generate_dual_tf_signal(
    df_1h:          pd.DataFrame,
    df_30m:         pd.DataFrame,
    df_5m:          pd.DataFrame,
    df_1m:          pd.DataFrame,
    symbol:         str,
    cached_1h_range: dict | None = None,
    cached_30m_range: dict | None = None,
) -> dict | None:
    """
    Генерує сигнал з 30m рейнджу відфільтрований 1h bias.

    Умови входу (всі мають бути True):
      ✅ 1h bias визначено (не neutral)
      ✅ 30m рейндж є (BB squeeze)
      ✅ Девіація в протилежному до bias напрямку
         (девіація вниз + bullish bias = лонг проти руху вниз)
      ✅ False breakout (об'єм)
      ✅ Order Flow підтверджує
      ✅ CVD розворот
      ✅ Stochastic в зоні
      ✅ BOS на 1m

    Повертає dict або None.
    """

    # ── 1. 1h bias ─────────────────────────────────────────
    bias = get_1h_bias(df_1h, cached_1h_range)

    if bias == "neutral":
        logger.debug(f"{symbol} [30m]: 1h bias neutral — пропускаємо")
        return None

    # ── 2. 30m рейндж ──────────────────────────────────────
    range_30m = cached_30m_range or detect_range(df_30m, min_candles=10)

    if range_30m is None:
        logger.debug(f"{symbol} [30m]: немає рейнджу на 30m")
        return None

    # ── 3. Девіація на 30m ─────────────────────────────────
    atr_1h     = calculate_atr(df_1h)
    last_1m    = df_1m.iloc[-1]
    dev_thresh = atr_1h * DEVIATION_ATR_MULT

    long_dev  = float(last_1m['low'])  < range_30m["low"]  - dev_thresh
    short_dev = float(last_1m['high']) > range_30m["high"] + dev_thresh

    if not long_dev and not short_dev:
        return None

    direction   = "long" if long_dev else "short"
    dev_extreme = float(last_1m['low']) if direction == "long" \
                  else float(last_1m['high'])

    # ── 4. Фільтр bias — ключова перевірка ─────────────────
    # Лонг тільки при bullish bias (ціна у верхній половині 1h)
    # Шорт тільки при bearish bias (ціна в нижній половині 1h)
    if direction == "long"  and bias != "bullish":
        logger.debug(f"{symbol} [30m]: лонг сигнал але bias={bias} — пропускаємо")
        return None
    if direction == "short" and bias != "bearish":
        logger.debug(f"{symbol} [30m]: шорт сигнал але bias={bias} — пропускаємо")
        return None

    logger.debug(f"{symbol} [30m]: девіація {direction.upper()} | bias={bias} ✅")

    # ── 5. False breakout ──────────────────────────────────
    vol = volume_on_deviation(df_1m)
    if not vol["is_false_breakout"]:
        logger.debug(f"{symbol} [30m]: великий об'єм — можливий пробій")
        return None

    # ── 6. Order Flow (1m) ─────────────────────────────────
    of = order_flow_delta(df_1m, lookback=OF_DELTA_LOOKBACK)
    if direction == "long"  and not of["is_bullish"]: return None
    if direction == "short" and not of["is_bearish"]: return None

    # ── 7. CVD (5m) ────────────────────────────────────────
    cvd_sig = cvd_reversal(calculate_cvd(df_5m), lookback=CVD_LOOKBACK)
    if direction == "long"  and cvd_sig != "bullish": return None
    if direction == "short" and cvd_sig != "bearish": return None

    # ── 8. Stochastic (5m) ────────────────────────────────
    stoch = stochastic_signal(df_5m, k=STOCH_K, d=STOCH_D, smooth_k=STOCH_SMOOTH)
    if direction == "long"  and not stoch["oversold"]:   return None
    if direction == "short" and not stoch["overbought"]: return None

    # ── 9. BOS (1m) → маркет ордер ────────────────────────
    if not detect_bos(df_1m, direction):
        return None

    # ── Рівні ─────────────────────────────────────────────
    levels = calculate_levels(range_30m, direction, dev_extreme, atr_1h)
    entry  = float(last_1m['close'])

    tp_dist = abs(levels["tp"] - entry)
    sl_dist = abs(levels["sl"] - entry)
    raw_rr  = tp_dist / sl_dist if sl_dist > 0 else 0

    if raw_rr < 1.5:
        return None

    logger.info(
        f"🎯 [30m+1h] {direction.upper()} {symbol} | bias={bias} | "
        f"entry={entry:.2f} TP={levels['tp']:.2f} SL={levels['sl']:.2f} | "
        f"RR={raw_rr:.2f}"
    )

    return {
        "symbol":         symbol,
        "direction":      direction,
        "entry":          entry,
        "tp":             levels["tp"],
        "sl":             levels["sl"],
        "atr":            atr_1h,
        "raw_rr":         raw_rr,
        "stoch_k":        stoch["k"],
        "cvd_signal":     cvd_sig,
        "of_delta":       of["delta"],
        "vol_ratio":      vol["volume_ratio"],
        "dev_extreme":    dev_extreme,
        "range":          range_30m,
        "bias_1h":        bias,
        "order_type":     "MARKET",
        "mode":           "dual_30m_1h",
    }