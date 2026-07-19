"""
ГЕНЕРАТОР СИГНАЛІВ — Liquidity Sweep / Market Maker Range Strategy
===================================================================

Логіка (sweep-and-reverse): маркет-мейкери «знімають» ліквідність за межею
рейнджу (стопи роздрібу), після чого ціна розвертається назад. Ми ловимо
саме розворот після зняття.

1) Знаходимо активний 1h range
2) Чекаємо sweep нижньої/верхньої межі range (у вікні sweep_window)
3) Чекаємо reclaim всередину range (на останній свічці)
4) Щонайменше два незалежні підтвердження: BOS / order-flow / momentum
5) CVD / volume — лог (опційно hard-filter)
6) Рахуємо SL/TP і перевіряємо RR
7) ⭐ Order Book як ПІДТВЕРДЖЕННЯ напрямку — застосовується у live_trade
   (SWEEP_USE_OB_CONFIRM): дисбаланс стакана має бути в бік розвороту.

════════════════════════════════════════════════════════════════════
ЩО РЕАЛІЗОВАНО ЗАРАЗ (2026-07-08):
  • Детекція рейнджу за ATR + sweep за межу + reclaim.
  • Щонайменше 2 незалежні підтвердження (BOS/OF/MOM); BOS і MOM однієї
    свічки не рахуються двічі.
  • ⭐ Order Book confirmation РОЗБЛОКОВАНО саме для sweep: угода
    проходить, лише якщо дисбаланс стакана підтверджує розворот
    (bid-перекіс для LONG / ask-перекіс для SHORT). Це головний
    «підпис» справжнього sweep — після зняття ліквідності пасивні
    заявки з'являються у бік розвороту (абсорбція).

ЩО ЩЕ МОЖНА ПОКРАЩИТИ:
  • CVD-дивергенція на екстремумі sweep (новий low/high ціни без нового
    low/high CVD = абсорбція) як обов'язкове підтвердження.
  • Прив'язка до мапи ліквідацій / великих OB-стін як цілей sweep.
  • Time-stop: якщо reclaim не тримається N свічок — вихід у беззбиток.

Режими:
- A: conservative = 1h range + 5m confirmation + 1m trigger
- B: aggressive   = 1h range + 1m confirmation + 1m trigger
"""

from __future__ import annotations

from typing import Optional, Dict, Any, Tuple

import pandas as pd
from loguru import logger

from indicators.entry import calculate_cvd, cvd_reversal ,order_flow_delta
from indicators.range_detector import calculate_atr
from config.settings import CVD_LOOKBACK, SYMBOL_CONFIG, MIN_SL_DISTANCE_PCT


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def _validate_df(df: pd.DataFrame, name: str) -> bool:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        logger.debug(f"{name}: dataframe is empty or invalid")
        return False
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        logger.debug(f"{name}: missing columns {sorted(missing)}")
        return False
    return True


def _safe_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    Розрахунок ATR із fallback-логікою.
    """
    try:
        atr = float(calculate_atr(df))
        if atr > 0:
            return atr
    except Exception as exc:
        logger.debug(f"ATR calc failed via calculate_atr(): {exc}")

    # Fallback ATR
    if len(df) < period + 1:
        return 0.0

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.rolling(period).mean().iloc[-1]
    return float(atr) if pd.notna(atr) else 0.0


def _sma(series: pd.Series, period: int) -> float:
    if series is None or len(series) < period:
        return 0.0
    value = series.astype(float).rolling(period).mean().iloc[-1]
    return float(value) if pd.notna(value) else 0.0


def _detect_active_range(
    df_1h: pd.DataFrame,
    lookback: int,
    atr: float,
    min_range_atr: float = 1.5,
    max_range_atr: float = 8.0,
    max_drift_atr: float = 2.5,
) -> Optional[Dict[str, float]]:
    """
    Виявляємо робочий range по 1h:
    - high / low за lookback свічок
    - range не повинен бути занадто вузький або занадто широкий
    - drift ціни всередині range має бути помірним
    """
    if len(df_1h) < lookback + 1:
        return None

    window = df_1h.tail(lookback).copy()
    if window.empty:
        return None

    range_high = float(window["high"].max())
    range_low = float(window["low"].min())
    range_size = range_high - range_low
    if atr <= 0:
        return None

    if not (min_range_atr * atr <= range_size <= max_range_atr * atr):
        return None

    first_close = float(window["close"].iloc[0])
    last_close = float(window["close"].iloc[-1])
    drift = abs(last_close - first_close)

    if drift > max_drift_atr * atr:
        return None

    return {
        "high": range_high,
        "low": range_low,
        "mid": (range_high + range_low) / 2.0,
        "size": range_size,
        "atr": atr,
        "lookback": float(lookback),
    }


def _normalize_cached_range(cached_range: dict | None, atr: float) -> Optional[Dict[str, float]]:
    if not cached_range:
        return None
    if "high" not in cached_range or "low" not in cached_range:
        return None

    high = float(cached_range["high"])
    low = float(cached_range["low"])
    if high <= low:
        return None

    return {
        "high": high,
        "low": low,
        "mid": float(cached_range.get("mid", (high + low) / 2.0)),
        "size": float(cached_range.get("size", high - low)),
        "atr": float(cached_range.get("atr", atr)),
        "lookback": float(cached_range.get("lookback", 0)),
    }


def _detect_sweep(
    df: pd.DataFrame,
    direction: str,
    range_low: float,
    range_high: float,
    atr: float,
    sweep_buffer_atr: float,
    lookback_bars: int = 3,
) -> Tuple[bool, float]:
    """
    Sweep = пробій межі range з поверненням назад всередину.
    Повертає:
    - bool: чи був sweep
    - float: extreme свічки sweep
    """
    if len(df) < lookback_bars:
        return False, 0.0

    window = df.tail(lookback_bars).copy()
    last = window.iloc[-1]

    buffer_value = atr * sweep_buffer_atr

    if direction == "long":
        extreme = float(window["low"].min())
        swept = extreme < (range_low - buffer_value)
        reclaimed = float(last["close"]) > range_low
        return bool(swept and reclaimed), extreme

    if direction == "short":
        extreme = float(window["high"].max())
        swept = extreme > (range_high + buffer_value)
        reclaimed = float(last["close"]) < range_high
        return bool(swept and reclaimed), extreme

    return False, 0.0


def _detect_bos(
    df: pd.DataFrame,
    direction: str,
    structure_lookback: int = 5,
) -> bool:
    """
    Простий BOS/MSS-фільтр:
    - LONG: остання свічка закривається вище локального high
    - SHORT: остання свічка закривається нижче локального low
    """
    if len(df) < structure_lookback + 1:
        return False

    window = df.tail(structure_lookback + 1).copy()
    ref = window.iloc[:-1]
    last = window.iloc[-1]

    if direction == "long":
        break_level = float(ref["high"].max())
        return float(last["close"]) > break_level and float(last["close"]) > float(last["open"])

    if direction == "short":
        break_level = float(ref["low"].min())
        return float(last["close"]) < break_level and float(last["close"]) < float(last["open"])

    return False


def _volume_confirm(
    df: pd.DataFrame,
    lookback: int = 20,
    multiplier: float = 1.2,
    direction: str | None = None,
) -> bool:
    """
    Підтвердження об'ємом:
    - останній volume > SMA(volume, lookback) * multiplier
    - опційно: свічка має бути в напрямку угоди
    """
    if len(df) < lookback + 1:
        return False

    last = df.iloc[-1]
    vol_sma = _sma(df["volume"], lookback)
    if vol_sma <= 0:
        return False

    if float(last["volume"]) <= vol_sma * multiplier:
        return False

    if direction == "long" and float(last["close"]) <= float(last["open"]):
        return False
    if direction == "short" and float(last["close"]) >= float(last["open"]):
        return False

    return True


def _cvd_confirm(
    df: pd.DataFrame,
    direction: str,
    lookback: int = 5,
) -> Tuple[bool, str]:
    """
    Підтвердження CVD:
    - використовуємо і категорію від cvd_reversal, і нахил CVD
    """
    if len(df) < lookback + 2:
        return False, "unknown"

    try:
        cvd = calculate_cvd(df)
    except Exception as exc:
        logger.debug(f"CVD calc failed: {exc}")
        return False, "unknown"

    signal = "unknown"
    try:
        signal = cvd_reversal(cvd, lookback=lookback)
    except Exception as exc:
        logger.debug(f"CVD reversal check failed: {exc}")

    slope_ok = False
    try:
        if isinstance(cvd, pd.DataFrame):
            s = cvd.iloc[:, 0].dropna()
        elif isinstance(cvd, pd.Series):
            s = cvd.dropna()
        else:
            s = pd.Series(cvd).dropna()

        if len(s) >= lookback + 1:
            slope = float(s.iloc[-1] - s.iloc[-lookback])
            slope_ok = slope > 0 if direction == "long" else slope < 0
    except Exception as exc:
        logger.debug(f"CVD slope check failed: {exc}")

    bullish_like = {"bullish", "accumulation", "up", "buy"}
    bearish_like = {"bearish", "distribution", "down", "sell"}

    if direction == "long":
        ok = (signal in bullish_like) or slope_ok
        return ok, signal

    if direction == "short":
        ok = (signal in bearish_like) or slope_ok
        return ok, signal

    return False, signal


def _build_trade_levels(
    direction: str,
    entry: float,
    range_low: float,
    range_high: float,
    sweep_extreme: float,
    atr: float,
    min_rr: float,
    stop_pad_atr: float,
    min_sl_pct: float = 0.0,
) -> Optional[Dict[str, float]]:
    """
    Стратегія TP:
    - LONG: спершу midpoint, якщо RR не проходить — range high
    - SHORT: спершу midpoint, якщо RR не проходить — range low

    min_sl_pct — мінімальна дистанція SL у % від ціни. Якщо природний SL
    (за sweep + буфер) ближче — розширюємо стоп до цього порогу. Це не дає
    комісії з'їсти угоду і не дає позиції роздутись понад доступну маржу.
    """
    if atr <= 0 or entry <= 0:
        return None

    mid = (range_high + range_low) / 2.0
    stop_pad = atr * stop_pad_atr

    if direction == "long":
        sl = min(sweep_extreme, range_low) - stop_pad
        if min_sl_pct > 0.0:
            sl = min(sl, entry * (1.0 - min_sl_pct))   # не ближче за поріг
        if sl >= entry:
            return None

        candidates = [mid, range_high]
        for tp in candidates:
            if tp <= entry:
                continue
            rr = (tp - entry) / (entry - sl) if (entry - sl) > 0 else 0.0
            if rr >= min_rr:
                return {
                    "tp": float(tp),
                    "sl": float(sl),
                    "raw_rr": float(rr),
                }

        return None

    if direction == "short":
        sl = max(sweep_extreme, range_high) + stop_pad
        if min_sl_pct > 0.0:
            sl = max(sl, entry * (1.0 + min_sl_pct))   # не ближче за поріг
        if sl <= entry:
            return None

        candidates = [mid, range_low]
        for tp in candidates:
            if tp >= entry:
                continue
            rr = (entry - tp) / (sl - entry) if (sl - entry) > 0 else 0.0
            if rr >= min_rr:
                return {
                    "tp": float(tp),
                    "sl": float(sl),
                    "raw_rr": float(rr),
                }

        return None

    return None


# ------------------------------------------------------------
# Main generator
# ------------------------------------------------------------

def generate_scalp_signal(
    df_1h: pd.DataFrame,
    df_5m: pd.DataFrame,
    df_1m: pd.DataFrame,
    symbol: str,
    cached_range: dict | None = None,
    mode: str = "A",
) -> Optional[dict]:
    """
    Генерація сигналу під BTC/SOL range-liquidity strategy.

    mode A:
        - 1h range
        - 5m confirmation
        - 1m trigger

    mode B:
        - 1h range
        - 1m confirmation
        - 1m trigger
    """
    if not _validate_df(df_1h, "df_1h"):
        return None
    if not _validate_df(df_5m, "df_5m"):
        return None
    if not _validate_df(df_1m, "df_1m"):
        return None

    symbol_cfg = SYMBOL_CONFIG.get(symbol, {})

    # Налаштування з можливістю перевизначення через config
    range_lookback = int(symbol_cfg.get("range_lookback", 48 if mode.upper() == "A" else 24))
    sweep_buffer_atr = float(symbol_cfg.get("sweep_buffer_atr", 0.15))
    stop_pad_atr = float(symbol_cfg.get("stop_pad_atr", 0.12))
    volume_mult = float(symbol_cfg.get("volume_mult", 1.20))
    volume_lookback = int(symbol_cfg.get("volume_lookback", 20))
    structure_lookback = int(symbol_cfg.get("structure_lookback", 5))
    min_rr = float(symbol_cfg.get("min_rr", 1.50))
    cvd_lookback = int(symbol_cfg.get("cvd_lookback", CVD_LOOKBACK))

    # Sweep як setup-state: шукаємо sweep у вікні останніх sweep_window 1m
    # свічок (а не лише на 3), вхід — за reclaim + min_confirmations
    # підтверджень. Раніше потрібен був збіг sweep+BOS+order-flow на ОДНІЙ
    # свічці → майже ніколи → 0 угод.
    sweep_window = int(symbol_cfg.get("sweep_window", 12))
    # Два незалежні класи підтвердження — незнижуваний quality floor.
    min_confirmations = max(2, int(symbol_cfg.get("min_confirmations", 2)))

    # Range-параметри — раніше не передавались у _detect_active_range,
    # тому хардкод-дефолти (1.5 / 8.0 / 2.5) ігнорували SYMBOL_CONFIG.
    min_range_atr = float(symbol_cfg.get("min_range_atr", 1.5))
    max_range_atr = float(symbol_cfg.get("max_range_atr", 8.0))
    max_drift_atr = float(symbol_cfg.get("max_drift_atr", 2.5))

    # Мінімальна дистанція SL (% від ціни) — захист від комісій і перевищення маржі.
    _min_sl_cfg = symbol_cfg.get("min_sl_distance_pct", None)
    min_sl_pct = float(_min_sl_cfg) if _min_sl_cfg is not None else float(MIN_SL_DISTANCE_PCT)

    trigger_df = df_1m
    confirm_df = df_5m if mode.upper() == "A" else df_1m

    # ── 1) ATR ──────────────────────────────────────────────
    atr = _safe_atr(df_1h, period=14)
    if atr <= 0:
        logger.debug(f"{symbol}: ATR invalid")
        return None

    # ── 2) Range ────────────────────────────────────────────
    range_data = _normalize_cached_range(cached_range, atr)
    if range_data is None:
        range_data = _detect_active_range(
            df_1h=df_1h,
            lookback=range_lookback,
            atr=atr,
            min_range_atr=min_range_atr,
            max_range_atr=max_range_atr,
            max_drift_atr=max_drift_atr,
        )

    if range_data is None:
        logger.debug(f"{symbol}: active range not found")
        return None

    range_high = float(range_data["high"])
    range_low = float(range_data["low"])
    range_mid = float(range_data["mid"])

    # ── 3) Sweep (у вікні sweep_window, reclaim на останній свічці) ──
    long_sweep, long_extreme = _detect_sweep(
        trigger_df, "long", range_low, range_high, atr, sweep_buffer_atr,
        lookback_bars=sweep_window,
    )
    short_sweep, short_extreme = _detect_sweep(
        trigger_df, "short", range_low, range_high, atr, sweep_buffer_atr,
        lookback_bars=sweep_window,
    )

    if long_sweep and short_sweep:
        logger.debug(f"{symbol}: simultaneous long and short sweep -> skip")
        return None

    if not long_sweep and not short_sweep:
        logger.debug(f"{symbol}: no liquidity sweep")
        return None

    direction = "long" if long_sweep else "short"
    sweep_extreme = long_extreme if direction == "long" else short_extreme

    logger.debug(
        f"{symbol}: sweep detected | direction={direction.upper()} "
        f"extreme={sweep_extreme:.2f} range_low={range_low:.2f} range_high={range_high:.2f}"
    )

    # ── 4) Підтвердження входу (SCORED, не жорсткий ланцюг) ──
    # Раніше BOS і order-flow були окремими hard-фільтрами, і обидва мали
    # спрацювати на тій самій останній свічці → майже ніколи. Тепер це
    # незалежні "голоси": достатньо min_confirmations з них.
    order_flow_lookback = int(symbol_cfg.get("order_flow_lookback", 3))
    use_order_flow_filter = bool(symbol_cfg.get("use_order_flow_filter", False))

    of = order_flow_delta(trigger_df, lookback=order_flow_lookback)

    bos_ok = _detect_bos(trigger_df, direction, structure_lookback=structure_lookback)
    of_ok = of["is_bullish"] if direction == "long" else of["is_bearish"]

    last_c = trigger_df.iloc[-1]
    if direction == "long":
        momentum_ok = float(last_c["close"]) > float(last_c["open"])
    else:
        momentum_ok = float(last_c["close"]) < float(last_c["open"])

    confirmations = []
    if bos_ok:
        confirmations.append("BOS")
    if of_ok:
        confirmations.append("OF")
    # BOS уже включає directional candle, тому MOM у такому разі є тим самим
    # price-action фактом, а не другим незалежним підтвердженням.
    if momentum_ok and not bos_ok:
        confirmations.append("MOM")

    # Якщо order-flow явно вмикають як обов'язковий — лишаємо hard-filter
    if use_order_flow_filter and not of_ok:
        logger.debug(f"{symbol}: Order Flow hard-filter не підтримує {direction.upper()}")
        return None

    if len(confirmations) < min_confirmations:
        logger.debug(
            f"{symbol}: замало підтверджень {confirmations} "
            f"({len(confirmations)}/{min_confirmations}) — skip"
        )
        return None

    # ── 6) CVD / Volume — тільки лог, не hard-filter ─────────
    use_cvd_filter = bool(symbol_cfg.get("use_cvd_filter", False))
    use_volume_filter = bool(symbol_cfg.get("use_volume_filter", False))
    log_cvd = bool(symbol_cfg.get("log_cvd", True))
    log_volume = bool(symbol_cfg.get("log_volume", True))

    cvd_ok = None
    cvd_sig = "not_checked"

    if log_cvd or use_cvd_filter:
        cvd_ok, cvd_sig = _cvd_confirm(
            confirm_df,
            direction,
            lookback=cvd_lookback,
        )

        if use_cvd_filter and not cvd_ok:
            logger.debug(
                f"{symbol}: CVD hard-filter не підтримує "
                f"{direction.upper()} ({cvd_sig})"
            )
            return None

    volume_ok = None

    if log_volume or use_volume_filter:
        volume_ok = _volume_confirm(
            confirm_df,
            lookback=volume_lookback,
            multiplier=volume_mult,
            direction=direction,
        )

        if use_volume_filter and not volume_ok:
            logger.debug(f"{symbol}: Volume hard-filter failed")
            return None

    # ── 7) Price / levels ───────────────────────────────────
    entry = float(trigger_df.iloc[-1]["close"])

    levels = _build_trade_levels(
        direction=direction,
        entry=entry,
        range_low=range_low,
        range_high=range_high,
        sweep_extreme=sweep_extreme,
        atr=atr,
        min_rr=min_rr,
        stop_pad_atr=stop_pad_atr,
        min_sl_pct=min_sl_pct,
    )

    if levels is None:
        logger.debug(f"{symbol}: RR filter failed or invalid SL/TP")
        return None

    raw_rr = float(levels["raw_rr"])
    tp = float(levels["tp"])
    sl = float(levels["sl"])

    logger.info(
        f"🎯 SWEEP {direction.upper()} {symbol} | "
        f"entry={entry:.2f} TP={tp:.2f} SL={sl:.2f} "
        f"RR={raw_rr:.2f} | confirms={confirmations} | "
        f"OF_delta={of['delta']:.0f} | "
        f"CVD={cvd_sig} ok={cvd_ok} | "
        f"VOL_ok={volume_ok} | mode={mode}"
    )

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "atr": atr,
        "raw_rr": raw_rr,
        "min_rr": min_rr,
        "strategy": "sweep",
        "confirmations": confirmations,
        "of_delta": of["delta"],
        "cvd_signal": cvd_sig,
        "cvd_ok": cvd_ok,
        "volume_ok": volume_ok,
        "sweep_extreme": sweep_extreme,
        "range": range_data,
        "order_type": "MARKET",
        "mode": mode,
    }
