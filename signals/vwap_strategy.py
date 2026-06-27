"""
СТРАТЕГІЯ C — VWAP σ-BAND REVERSION
====================================
Незалежна стратегія. VWAP (Volume Weighted Average Price) — це
інституційний бенчмарк: фонди намагаються виконувати ордери біля VWAP,
тож ціна статистично тяжіє назад до VWAP після різких відхилень.

Ідея (як торгують VWAP-скальпери):
  - Ціна впала на > k·σ НИЖЧЕ VWAP  → LONG, ціль = VWAP
  - Ціна злетіла на > k·σ ВИЩЕ VWAP → SHORT, ціль = VWAP
  SL ставимо за межею входу (на k_sl·σ), щоб дати місце на overshoot.

Відмінність від mean-reversion (BB):
  BB-канал будується від СЕРЕДНЬОЇ ціни (SMA), VWAP — від ЦІНИ·ОБ'ЄМ.
  VWAP сильніше реагує на сплески об'єму (саме там, де "застрягли" великі
  гравці) → інша точка входу. Тому це ОКРЕМА стратегія, а не дубль BB.

Економіка та сама, що й у mean-reversion: низький RR, високий win-rate,
власний min_rr (settings: vwap.min_rr). Маркет-вхід (поки що).
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
from loguru import logger

from config.settings import SYMBOL_CONFIG
from indicators.range_detector import calculate_atr
from indicators.oscillators import rsi, vwap_bands


def _validate(df: Optional[pd.DataFrame], need: int) -> bool:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False
    if len(df) < need:
        return False
    required = {"open", "high", "low", "close", "volume"}
    return required.issubset(df.columns)


def generate_vwap_signal(dfs: dict, symbol: str) -> Optional[dict]:
    """
    dfs — словник timeframe→DataFrame. Стратегія обирає свій ТФ (vwap.tf).
    Повертає сигнал-дикт або None.
    """
    cfg = SYMBOL_CONFIG.get(symbol, {})
    vc = cfg.get("vwap", {})
    if not vc.get("enabled", False):
        return None

    tf = vc.get("tf", "5m")
    df = dfs.get(tf)

    window = vc.get("window", 96)
    window = int(window) if window else None
    k_band = float(vc.get("k_band", 2.0))
    require_rsi = bool(vc.get("require_rsi", False))
    rsi_period = int(vc.get("rsi_period", 14))
    rsi_os = float(vc.get("rsi_oversold", 42))
    rsi_ob = float(vc.get("rsi_overbought", 58))
    sl_k = float(vc.get("sl_k", 3.5))
    tp_target = str(vc.get("tp_target", "vwap"))
    min_dev_pct = float(vc.get("min_dev_pct", 0.20))
    min_rr = float(vc.get("min_rr", 0.85))
    min_sl_pct = float(vc.get("min_sl_pct", 0.003))

    need = (window or 30) + 5
    if not _validate(df, need):
        logger.debug(f"{symbol} [vwap]: недостатньо даних {tf}")
        return None

    close = df["close"].astype(float)
    price = float(close.iloc[-1])

    atr = calculate_atr(df, period=14)
    if atr <= 0:
        return None

    vb = vwap_bands(df, window=window, k=k_band)
    if not vb["valid"] or vb["sigma"] <= 0:
        return None

    vwap = vb["vwap"]
    upper = vb["upper"]
    lower = vb["lower"]
    sigma = vb["sigma"]
    dev_pct = vb["dev_pct"]

    # Девіація замала → ціль (VWAP) ближче за комісії → пропуск
    if abs(dev_pct) < min_dev_pct:
        return None

    rsi_val = float(rsi(close, period=rsi_period).iloc[-1]) if require_rsi else None

    direction = None
    if price <= lower and (not require_rsi or rsi_val <= rsi_os):
        direction = "long"
    elif price >= upper and (not require_rsi or rsi_val >= rsi_ob):
        direction = "short"

    if direction is None:
        return None

    # ── Рівні TP / SL ────────────────────────────────────────
    # min_sl_pct — власний поріг стратегії (мінімальна дистанція SL для
    # захисту маржі), не глобальний 0.5%.
    if direction == "long":
        tp = vwap if tp_target == "vwap" else upper
        sl = vwap - sl_k * sigma
        sl = min(sl, price * (1.0 - min_sl_pct))
        if sl >= price or tp <= price:
            return None
        rr = (tp - price) / (price - sl)
    else:
        tp = vwap if tp_target == "vwap" else lower
        sl = vwap + sl_k * sigma
        sl = max(sl, price * (1.0 + min_sl_pct))
        if sl <= price or tp >= price:
            return None
        rr = (price - tp) / (sl - price)

    if rr < min_rr:
        logger.debug(
            f"{symbol} [vwap]: RR {rr:.2f} < {min_rr} "
            f"({direction} entry={price:.2f} tp={tp:.2f} sl={sl:.2f})"
        )
        return None

    logger.info(
        f"🎯 VWAP {direction.upper()} {symbol} | entry={price:.2f} "
        f"TP={tp:.2f} SL={sl:.2f} RR={rr:.2f} | "
        f"VWAP={vwap:.2f} dev={dev_pct:+.2f}% σ={sigma:.2f}"
    )

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": price,
        "tp": float(tp),
        "sl": float(sl),
        "atr": atr,
        "raw_rr": float(rr),
        "min_rr": min_rr,
        "order_type": "MARKET",
        "mode": "vwap",
        "strategy": "vwap",
        "vwap": vwap,
        "vwap_dev_pct": dev_pct,
        "vwap_sigma": sigma,
    }
