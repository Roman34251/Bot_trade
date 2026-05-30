"""
ГЕНЕРАТОР СИГНАЛІВ — Range Scalping
======================================
Об'єднує всі індикатори в один сигнал входу.
Всі умови мають бути True — інакше None (пропускаємо).

Режим А: 1h рейндж → 15m девіація → 5m фільтр → 1m BOS
Режим Б: 30m рейндж → 5m девіація → 1m BOS
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
    DEVIATION_ATR_MULT, SYMBOL_CONFIG,
    STOCH_K, STOCH_D, STOCH_SMOOTH,
    CVD_LOOKBACK, OF_DELTA_LOOKBACK,
)


def generate_scalp_signal(
    df_1h:  pd.DataFrame,
    df_5m:  pd.DataFrame,
    df_1m:  pd.DataFrame,
    symbol: str,
    cached_range: dict | None = None,
    mode: str = "A",
) -> dict | None:
    """
    Головна функція генерації сигналу.

    Порядок перевірки (швидкий → повільний):
      1. Рейндж є?           (BB на 1h — кешується)
      2. Девіація є?         (вихід за межу > 0.3 ATR)
      3. False breakout?     (об'єм < 1.5x)
      4. Order Flow delta?   (3 свічки 1m — найшвидший)
      5. CVD розворот?       (3 свічки 5m)
      6. Stochastic зона?    (5 свічок 5m)
      7. BOS на 1m?          → MARKET ORDER

    Повертає dict з сигналом або None якщо умови не виконані.

    cached_range: передавай збережений рейндж щоб не рахувати щохвилини.
    """
    symbol_cfg = SYMBOL_CONFIG.get(symbol, {})

    # ── 1. Рейндж ──────────────────────────────────────────
    # Кешуємо і оновлюємо раз на 1h, не щохвилини
    range_data = cached_range
    if range_data is None:
        df_slow = df_1h if mode == "A" else df_5m
        range_data = detect_range(df_slow)

    if range_data is None:
        logger.debug(f"{symbol}: немає рейнджу (BB не стиснутий)")
        return None

    # ── 2. Девіація ────────────────────────────────────────
    atr      = calculate_atr(df_1h)
    last_1m  = df_1m.iloc[-1]
    dev_thresh = atr * DEVIATION_ATR_MULT   # 0.3 × ATR

    long_dev  = float(last_1m['low'])  < range_data["low"]  - dev_thresh
    short_dev = float(last_1m['high']) > range_data["high"] + dev_thresh

    if not long_dev and not short_dev:
        return None   # немає девіації — не торгуємо

    direction   = "long" if long_dev else "short"
    dev_extreme = float(last_1m['low']) if direction == "long" \
                  else float(last_1m['high'])

    logger.debug(f"{symbol}: девіація {direction.upper()} | "
                 f"extreme={dev_extreme:.2f}")

    # ── 3. False breakout (об'єм) ──────────────────────────
    vol_check = volume_on_deviation(df_1m)
    if not vol_check["is_false_breakout"]:
        logger.debug(f"{symbol}: великий об'єм на девіації "
                     f"({vol_check['volume_ratio']:.1f}x) — можливий пробій")
        return None

    # ── 4. Order Flow Delta (1m — найшвидший) ──────────────
    of = order_flow_delta(df_1m, lookback=OF_DELTA_LOOKBACK)
    if direction == "long"  and not of["is_bullish"]:
        logger.debug(f"{symbol}: OF delta негативна для лонгу")
        return None
    if direction == "short" and not of["is_bearish"]:
        logger.debug(f"{symbol}: OF delta позитивна для шорту")
        return None

    # ── 5. CVD розворот (5m) ───────────────────────────────
    cvd    = calculate_cvd(df_5m)
    cvd_sig = cvd_reversal(cvd, lookback=CVD_LOOKBACK)
    if direction == "long"  and cvd_sig != "bullish":
        logger.debug(f"{symbol}: CVD не бичачий ({cvd_sig})")
        return None
    if direction == "short" and cvd_sig != "bearish":
        logger.debug(f"{symbol}: CVD не ведмежий ({cvd_sig})")
        return None

    # ── 6. Stochastic зона (5m) ────────────────────────────
    stoch = stochastic_signal(df_5m, k=STOCH_K, d=STOCH_D, smooth_k=STOCH_SMOOTH)
    if direction == "long"  and not stoch["oversold"]:
        logger.debug(f"{symbol}: Stoch %K={stoch['k']:.1f} не в зоні лонгу (<20)")
        return None
    if direction == "short" and not stoch["overbought"]:
        logger.debug(f"{symbol}: Stoch %K={stoch['k']:.1f} не в зоні шорту (>80)")
        return None

    # ── 7. BOS на 1m → тригер маркет ордера ───────────────
    if not detect_bos(df_1m, direction):
        logger.debug(f"{symbol}: BOS не підтверджено на 1m")
        return None

    # ── Розрахунок рівнів ──────────────────────────────────
    levels = calculate_levels(range_data, direction, dev_extreme, atr)
    entry  = float(last_1m['close'])

    # Мінімальна перевірка R:R (до slippage)
    tp_dist = abs(levels["tp"] - entry)
    sl_dist = abs(levels["sl"] - entry)
    raw_rr  = tp_dist / sl_dist if sl_dist > 0 else 0

    if raw_rr < 1.5:   # мінімум до слипажу; після буде ~2.0
        logger.debug(f"{symbol}: R:R {raw_rr:.2f} замалий (до slippage)")
        return None

    logger.info(
        f"🎯 СИГНАЛ {direction.upper()} {symbol} | "
        f"entry={entry:.2f} TP={levels['tp']:.2f} SL={levels['sl']:.2f} | "
        f"RR={raw_rr:.2f} Stoch={stoch['k']:.1f} CVD={cvd_sig}"
    )

    return {
        "symbol":         symbol,
        "direction":      direction,
        "entry":          entry,
        "tp":             levels["tp"],
        "sl":             levels["sl"],
        "atr":            atr,
        "raw_rr":         raw_rr,
        "stoch_k":        stoch["k"],
        "stoch_d":        stoch["d"],
        "cvd_signal":     cvd_sig,
        "of_delta":       of["delta"],
        "vol_ratio":      vol_check["volume_ratio"],
        "dev_extreme":    dev_extreme,
        "range":          range_data,
        "order_type":     "MARKET",
        "mode":           mode,
    }