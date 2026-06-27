"""
СТРАТЕГІЯ B — MEAN-REVERSION (Bollinger Bands + RSI)
=====================================================
Незалежна стратегія (НЕ підтвердження для sweep). Працює в боковику.

Ідея (як торгують скальпери-mean-reversion, Connors RSI-2 / BB-fade):
  - Ціна торкнулась/проколола НИЖНЮ смугу BB + RSI у перепроданості
        → LONG, ціль = середня смуга BB (середнє, до якого ціна вертається)
  - Ціна торкнулась/проколола ВЕРХНЮ смугу BB + RSI у перекупленості
        → SHORT, ціль = середня смуга BB

Чому це дає БАГАТО угод:
  торкання смуг BB на 5m трапляється кілька разів на день у кожен бік,
  на відміну від sweep-сетапу, який вимагає рідкісного збігу умов.

Економіка (ВАЖЛИВО):
  mean-reversion свідомо має НИЗЬКИЙ RR (≈0.8-1.0), але високий win-rate.
  Калькулятор отримує власний min_rr цієї стратегії (settings: meanrev.min_rr),
  а не загальний поріг sweep. При маркет-ордерах комісія ~0.17% round-trip
  з'їдає частину прибутку — головний важіль покращення далі: лімітні (maker)
  входи на смузі або ширший ТФ (15m). Поки що — маркет, щоб збігатись з
  поточним виконавцем і ПОЧАТИ торгувати/збирати статистику.

Вхід: MARKET (на закритті сигнальної свічки). Сигнал-дикт сумісний з
generate_scalp_signal → live_trade._execute_trade працює без змін.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
from loguru import logger

from config.settings import SYMBOL_CONFIG
from indicators.range_detector import calculate_atr
from indicators.oscillators import rsi, bollinger_bands, adx


def _validate(df: Optional[pd.DataFrame], need: int) -> bool:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False
    if len(df) < need:
        return False
    required = {"open", "high", "low", "close", "volume"}
    return required.issubset(df.columns)


def generate_meanrev_signal(dfs: dict, symbol: str) -> Optional[dict]:
    """
    dfs — словник timeframe→DataFrame: {"1h":.., "30m":.., "5m":.., "1m":..}.
    Стратегія сама обирає свій сигнальний ТФ з конфігу (meanrev.tf).

    Повертає сигнал-дикт або None.
    """
    cfg = SYMBOL_CONFIG.get(symbol, {})
    mr = cfg.get("meanrev", {})
    if not mr.get("enabled", False):
        return None

    tf = mr.get("tf", "5m")
    df = dfs.get(tf)

    bb_period = int(mr.get("bb_period", 20))
    bb_std = float(mr.get("bb_std", 2.0))
    rsi_period = int(mr.get("rsi_period", 14))
    rsi_os = float(mr.get("rsi_oversold", 35))
    rsi_ob = float(mr.get("rsi_overbought", 65))
    require_rsi = bool(mr.get("require_rsi", True))
    tp_target = str(mr.get("tp_target", "mid"))
    sl_atr_buffer = float(mr.get("sl_atr_buffer", 0.6))
    min_width_pct = float(mr.get("min_width_pct", 0.25))
    use_adx = bool(mr.get("use_adx_filter", False))
    adx_max = float(mr.get("adx_max", 35))
    min_rr = float(mr.get("min_rr", 0.85))
    min_sl_pct = float(mr.get("min_sl_pct", 0.003))

    need = max(bb_period, rsi_period) + 5
    if not _validate(df, need):
        logger.debug(f"{symbol} [meanrev]: недостатньо даних {tf}")
        return None

    close = df["close"].astype(float)
    last = df.iloc[-1]
    price = float(last["close"])
    low = float(last["low"])
    high = float(last["high"])

    atr = calculate_atr(df, period=14)
    if atr <= 0:
        return None

    bb = bollinger_bands(close, period=bb_period, std=bb_std)
    if not bb["valid"]:
        return None

    # Надто вузький канал → TP (до середини) менший за комісії → пропуск
    if bb["width_pct"] < min_width_pct:
        logger.debug(
            f"{symbol} [meanrev]: канал вузький {bb['width_pct']:.2f}%"
            f" < {min_width_pct}% — skip"
        )
        return None

    rsi_series = rsi(close, period=rsi_period)
    rsi_val = float(rsi_series.iloc[-1])

    # Опційний фільтр сили тренду: в сильному тренді mean-reversion небезпечна
    if use_adx:
        adx_val = float(adx(df).iloc[-1])
        if adx_val > adx_max:
            logger.debug(f"{symbol} [meanrev]: ADX {adx_val:.1f} > {adx_max} (тренд) — skip")
            return None

    mid = bb["mid"]
    lower = bb["lower"]
    upper = bb["upper"]

    # ── Визначення напрямку ──────────────────────────────────
    long_touch = low <= lower or price <= lower
    short_touch = high >= upper or price >= upper

    direction = None
    if long_touch and (not require_rsi or rsi_val <= rsi_os):
        direction = "long"
    elif short_touch and (not require_rsi or rsi_val >= rsi_ob):
        direction = "short"

    if direction is None:
        return None

    # ── Рівні TP / SL ────────────────────────────────────────
    # min_sl_pct тут — ВЛАСНИЙ поріг стратегії (НЕ глобальний 0.5%): він
    # лише гарантує МІНІМАЛЬНУ дистанцію SL (захист від роздування позиції
    # над маржею), але не роздуває SL до 0.5%, що вбивало б RR mean-reversion.
    if direction == "long":
        tp = mid if tp_target == "mid" else upper
        sl = min(low, lower) - atr * sl_atr_buffer
        sl = min(sl, price * (1.0 - min_sl_pct))   # не ближче за поріг
        if sl >= price or tp <= price:
            return None
        rr = (tp - price) / (price - sl)
    else:
        tp = mid if tp_target == "mid" else lower
        sl = max(high, upper) + atr * sl_atr_buffer
        sl = max(sl, price * (1.0 + min_sl_pct))   # не ближче за поріг
        if sl <= price or tp >= price:
            return None
        rr = (price - tp) / (sl - price)

    if rr < min_rr:
        logger.debug(
            f"{symbol} [meanrev]: RR {rr:.2f} < {min_rr} "
            f"({direction} entry={price:.2f} tp={tp:.2f} sl={sl:.2f})"
        )
        return None

    logger.info(
        f"🎯 MEANREV {direction.upper()} {symbol} | entry={price:.2f} "
        f"TP={tp:.2f} SL={sl:.2f} RR={rr:.2f} | "
        f"RSI={rsi_val:.1f} %b={bb['percent_b']:.2f} width={bb['width_pct']:.2f}%"
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
        "mode": "meanrev",
        "strategy": "meanrev",
        "rsi": rsi_val,
        "bb_percent_b": bb["percent_b"],
        "bb_width_pct": bb["width_pct"],
    }
