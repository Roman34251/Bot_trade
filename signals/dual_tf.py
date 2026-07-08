"""
СТРАТЕГІЯ D — TREND-FOLLOWING (Dual-Timeframe EMA-stack pullback)
=================================================================
Колишній dual_tf, тепер — повноцінна трендова стратегія.

Дослідження (quantpedia, tradeciety, altrady, stockcharts та ін.) одностайно
обрало EMA-stack pullback як найнадійніший простий трендовий підхід із
високим win-rate. Суть:

  1) ТРЕНД на старшому ТФ (1h) — "що": торгуємо ЛИШЕ за трендом.
       LONG-gate:  EMA20 > EMA50 > EMA200, ціна > EMA50, EMA50 росте, ADX>min
       SHORT-gate: дзеркально
  2) ВХІД на молодшому ТФ (5m) — "коли": лише на ВІДкаті до зони EMA20..EMA50
       + підтверджуюча свічка (close назад за EMA20 у бік тренду).
       НЕ ловимо ножі: відкид, якщо ціна зламала структуру (нижче EMA50−0.5·ATR).
  3) ВИХІД: структурний SL (за свінгом / EMA50), TP = tp_r·R (за замовч. 2R).

Чому високий win-rate (з дослідження):
  - HTF-gate прибирає контртрендові угоди (головний важіль win-rate);
  - вхід на відкаті дає тісний SL біля структури → асиметрія на користь виграшу;
  - RR=2 потребує лише ~36-40% виграшних, а трендовий фільтр дає 45-55%.

Примітка: partial-TP(2R)+перенесення в BE+трейлінг — наступний апгрейд
(потребує доопрацювання виконавця _monitor_position). Поки — один TP на 2R,
що вже дає позитивне матсподівання з трендовим фільтром.

════════════════════════════════════════════════════════════════════
ЩО РЕАЛІЗОВАНО ЗАРАЗ:
  • HTF-gate на 1h: EMA20>EMA50>EMA200 + нахил EMA50 + ADX>min.
  • Вхід на 5m на відкаті в зону EMA20..EMA50 + підтверджуюча свічка.
  • Захист від «ножа»: skip, якщо структура зламана (нижче EMA50−0.5·ATR).
  • Захист від «задертості»: skip, якщо ціна вже далеко від EMA20.
  • Структурний SL (свінг/EMA50) + TP = tp_r·R (за замовч. 2.2R).

ЩО ЩЕ МОЖНА ПОКРАЩИТИ:
  • Partial-TP 50% на 2R + перенесення в беззбиток + ATR-трейлінг.
  • ADX має РОСТИ (adx[-1] > adx[-2]) — трендова сила має посилюватись.
  • Вхід на 15m (TREND_ENTRY_TF=15m) — менше шуму, чистіші відкати.
  • MTF-momentum: RSI/MACD на 1h у бік тренду як додаткове підтвердження.

Інтерфейс: generate_trend_signal(dfs, symbol) — як у meanrev/vwap.
generate_dual_tf_signal(...) лишено як тонкий адаптер для сумісності.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
from loguru import logger

from config.settings import SYMBOL_CONFIG
from indicators.range_detector import calculate_atr
from indicators.oscillators import ema, adx, rsi


def _validate(df: Optional[pd.DataFrame], need: int) -> bool:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False
    if len(df) < need:
        return False
    required = {"open", "high", "low", "close", "volume"}
    return required.issubset(df.columns)


def _trend_gate(df_1h: pd.DataFrame, cfg: dict) -> Optional[str]:
    """
    Визначає напрямок тренду на 1h за останньою ЗАКРИТОЮ свічкою.
    Повертає 'long' / 'short' / None (немає чіткого тренду → не торгуємо).
    """
    ema_fast = int(cfg.get("ema_fast", 20))
    ema_mid = int(cfg.get("ema_mid", 50))
    ema_slow = int(cfg.get("ema_slow", 200))
    slope_lb = int(cfg.get("ema_slope_lookback", 5))
    adx_min = float(cfg.get("adx_min", 20))
    use_ema_slow = bool(cfg.get("use_ema200_filter", True))

    need = (ema_slow if use_ema_slow else ema_mid) + slope_lb + 2
    if len(df_1h) < need:
        return None

    close = df_1h["close"].astype(float)
    e_fast = ema(close, ema_fast)
    e_mid = ema(close, ema_mid)
    e_slow = ema(close, ema_slow)

    f = float(e_fast.iloc[-1])
    m = float(e_mid.iloc[-1])
    s = float(e_slow.iloc[-1])
    price = float(close.iloc[-1])
    mid_now = float(e_mid.iloc[-1])
    mid_prev = float(e_mid.iloc[-1 - slope_lb])

    adx_val = float(adx(df_1h, period=int(cfg.get("adx_period", 14))).iloc[-1])
    if adx_val < adx_min:
        return None  # боковик / слабкий тренд → стоп

    long_ok = (f > m) and (price > m) and (mid_now > mid_prev)
    short_ok = (f < m) and (price < m) and (mid_now < mid_prev)
    if use_ema_slow:
        long_ok = long_ok and (m > s) and (price > s)
        short_ok = short_ok and (m < s) and (price < s)

    if long_ok and not short_ok:
        return "long"
    if short_ok and not long_ok:
        return "short"
    return None


def generate_trend_signal(dfs: dict, symbol: str) -> Optional[dict]:
    """
    Трендова стратегія (EMA-stack pullback). dfs — словник tf→DataFrame.
    Повертає сигнал-дикт сумісний з live_trade._execute_trade або None.
    """
    cfg = SYMBOL_CONFIG.get(symbol, {})
    tc = cfg.get("trend", {})
    if not tc.get("enabled", False):
        return None

    trend_tf = tc.get("trend_tf", "1h")
    entry_tf = tc.get("entry_tf", "5m")
    df_1h = dfs.get(trend_tf)
    df_e = dfs.get(entry_tf)

    ema_fast = int(tc.get("ema_fast", 20))
    ema_mid = int(tc.get("ema_mid", 50))
    pullback_lb = int(tc.get("pullback_lookback", 6))
    swing_lb = int(tc.get("swing_lookback", 10))
    max_break_atr = float(tc.get("max_pullback_below_ema_atr", 0.5))
    extension_atr = float(tc.get("max_extension_atr", 1.2))
    sl_buffer_atr = float(tc.get("sl_buffer_atr", 0.3))
    tp_r = float(tc.get("tp_r", 2.0))
    use_rsi = bool(tc.get("use_rsi_confirm", False))
    rsi_period = int(tc.get("rsi_period", 14))
    min_rr = float(tc.get("min_rr", 1.7))
    min_sl_pct = float(tc.get("min_sl_pct", 0.0018))

    if not _validate(df_1h, ema_mid + 10) or not _validate(df_e, ema_mid + pullback_lb + 2):
        return None

    # ── 1) Трендовий gate на 1h ─────────────────────────────
    direction = _trend_gate(df_1h, tc)
    if direction is None:
        logger.debug(f"{symbol} [trend]: немає трендового gate (боковик/слабкий ADX)")
        return None

    # ── 2) Вхід на 5m: відкат у зону EMA20..EMA50 + підтвердження ──
    close_e = df_e["close"].astype(float)
    e_fast = ema(close_e, ema_fast)
    e_mid = ema(close_e, ema_mid)
    atr_e = calculate_atr(df_e, period=int(tc.get("atr_period", 14)))
    if atr_e <= 0:
        return None

    last = df_e.iloc[-1]
    price = float(last["close"])
    o = float(last["open"])
    ef = float(e_fast.iloc[-1])
    em = float(e_mid.iloc[-1])

    lows = df_e["low"].astype(float).iloc[-pullback_lb:]
    highs = df_e["high"].astype(float).iloc[-pullback_lb:]
    ef_win = e_fast.iloc[-pullback_lb:]

    rsi_val = float(rsi(close_e, period=rsi_period).iloc[-1]) if use_rsi else None

    if direction == "long":
        dipped = bool((lows <= ef_win).any())            # торкнувся зони EMA20
        confirm = price > ef and price > o               # close назад над EMA20 + бичача
        not_broken = price > (em - max_break_atr * atr_e)  # структура ціла
        not_extended = price < (ef + extension_atr * atr_e)  # не задерта
        rsi_ok = (rsi_val is None) or (rsi_val > 45)
        if not (dipped and confirm and not_broken and not_extended and rsi_ok):
            return None

        swing_low = float(df_e["low"].astype(float).iloc[-swing_lb:].min())
        sl = min(swing_low, em) - sl_buffer_atr * atr_e
        sl = min(sl, price * (1.0 - min_sl_pct))
        if sl >= price:
            return None
        risk = price - sl
        tp = price + tp_r * risk
        rr = (tp - price) / risk
    else:
        popped = bool((highs >= ef_win).any())           # торкнувся зони EMA20 зверху
        confirm = price < ef and price < o               # close назад під EMA20 + ведмежа
        not_broken = price < (em + max_break_atr * atr_e)
        not_extended = price > (ef - extension_atr * atr_e)
        rsi_ok = (rsi_val is None) or (rsi_val < 55)
        if not (popped and confirm and not_broken and not_extended and rsi_ok):
            return None

        swing_high = float(df_e["high"].astype(float).iloc[-swing_lb:].max())
        sl = max(swing_high, em) + sl_buffer_atr * atr_e
        sl = max(sl, price * (1.0 + min_sl_pct))
        if sl <= price:
            return None
        risk = sl - price
        tp = price - tp_r * risk
        rr = (price - tp) / risk

    if rr < min_rr or tp <= 0:
        return None

    logger.info(
        f"🎯 TREND {direction.upper()} {symbol} | entry={price:.2f} "
        f"TP={tp:.2f} SL={sl:.2f} RR={rr:.2f} | "
        f"EMA20={ef:.1f} EMA50={em:.1f} ATR={atr_e:.1f}"
        + (f" RSI={rsi_val:.1f}" if rsi_val is not None else "")
    )

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": price,
        "tp": float(tp),
        "sl": float(sl),
        "atr": atr_e,
        "raw_rr": float(rr),
        "min_rr": min_rr,
        "order_type": "MARKET",
        "mode": "trend",
        "strategy": "trend",
        "rsi": rsi_val,
    }


def generate_dual_tf_signal(
    df_1h: pd.DataFrame,
    df_30m: pd.DataFrame,
    df_5m: pd.DataFrame,
    df_1m: pd.DataFrame,
    symbol: str,
    cached_1h_range: dict | None = None,
    cached_30m_range: dict | None = None,
) -> Optional[dict]:
    """
    Адаптер сумісності зі старим викликом у live_trade. Просто збирає dfs і
    делегує в generate_trend_signal.
    """
    dfs = {"1h": df_1h, "30m": df_30m, "5m": df_5m, "1m": df_1m}
    return generate_trend_signal(dfs, symbol)
