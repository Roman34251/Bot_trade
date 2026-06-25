"""
СИГНАЛИ ВХОДУ — молодший ТФ (1m / 5m)
========================================
Що містить цей файл:
  - stochastic_signal     — Stochastic (5,3,3) для скальпінгу
  - calculate_cvd         — Cumulative Volume Delta (pd.Series)
  - cvd_reversal          — визначення розвороту CVD
  - order_flow_delta      — тиск покупців/продавців (Kaufman)
  - volume_on_deviation   — перевірка false/real breakout
  - detect_bos            — Break of Structure (тригер входу)
  - build_trade_levels    — розрахунок TP/SL (логіка з generator.py)
  - calculate_levels      — спрощений TP/SL для signal_engine / dual_df

Порядок перевірки (від найшвидшого):
  1. Order Flow Delta   — поточна свічка
  2. CVD розворот       — N свічок
  3. Stochastic         — 5 свічок
  4. Об'єм девіації     — 20 свічок (avg)
  5. BOS                — structure_lookback + 1 свічок
"""

from __future__ import annotations

from typing import Optional
import pandas as pd
import numpy as np




# ─────────────────────────────────────────────────────────────
# CVD — Cumulative Volume Delta
# ─────────────────────────────────────────────────────────────

def calculate_cvd(df: pd.DataFrame) -> pd.Series:
    """
    CVD = накопичена дельта об'єму.

    Апроксимація (без tick-даних):
      Зелена свічка (close > open) → весь об'єм = купівлі.
      Червона свічка               → весь об'єм = продажі.

    Повертає pd.Series (index = index df) — потрібно для
    _cvd_confirm() в generator.py (slope через iloc).
    """
    delta = np.where(df["close"] > df["open"], df["volume"], -df["volume"])
    return pd.Series(delta, index=df.index).cumsum()


def cvd_reversal(cvd: pd.Series, lookback: int = 3) -> str:
    """
    Визначає розворот CVD за останні lookback свічок.

    'bullish'  — CVD N свічок підряд росте.
    'bearish'  — CVD N свічок підряд падає.
    'none'     — немає чіткого сигналу.
    """
    if len(cvd) < lookback:
        return "none"

    r    = cvd.iloc[-lookback:]
    vals = [float(r.iloc[i]) for i in range(lookback)]

    if all(vals[i] > vals[i - 1] for i in range(1, lookback)):
        return "bullish"
    if all(vals[i] < vals[i - 1] for i in range(1, lookback)):
        return "bearish"
    return "none"


# ─────────────────────────────────────────────────────────────
# Order Flow Delta
# ─────────────────────────────────────────────────────────────

def order_flow_delta(df: pd.DataFrame, lookback: int = 3) -> dict:
    """
    Апроксимація тиску покупців/продавців (формула Kaufman).

    Buy vol  = volume × (close - low)  / (high - low)
    Sell vol = volume × (high - close) / (high - low)
    Delta    = buy_vol - sell_vol

    Позитивна delta → покупці домінують → лонг сигнал.
    Негативна delta → продавці домінують → шорт сигнал.

    lookback береться з OF_DELTA_LOOKBACK в settings
    (dual_df.py передає його явно).
    """
    if len(df) < lookback:
        return {
            "delta": 0.0,
            "is_bullish": False,
            "is_bearish": False,
            "delta_per_candle": 0.0,
        }

    recent = df.iloc[-lookback:]
    hl     = (recent["high"] - recent["low"]).replace(0, 1e-9)
    buy_v  = recent["volume"] * (recent["close"] - recent["low"])  / hl
    sell_v = recent["volume"] * (recent["high"]  - recent["close"]) / hl
    delta  = float((buy_v - sell_v).sum())

    return {
        "delta":            delta,
        "is_bullish":       delta > 0,
        "is_bearish":       delta < 0,
        "delta_per_candle": delta / lookback,
    }




# ─────────────────────────────────────────────────────────────
# BOS — Break of Structure
# ─────────────────────────────────────────────────────────────

def detect_bos(
    df_fast: pd.DataFrame,
    direction: str,
    volume_mult: float = 1.2,
    structure_lookback: int = 5,
) -> bool:
    """
    BOS = перша свічка що підтверджує розворот після девіації.
    Тригер для маркет ордера.

    Логіка з generator.py (_detect_bos):
      structure_lookback=5 — перевіряємо пробій max/min за 5 свічок
      (не тільки попередньої — це запобігає хибним BOS на шумі).

    ЛОНГ (після девіації вниз):
      ✅ Зелена свічка (close > open)
      ✅ close > max(high) за structure_lookback свічок
      ✅ Об'єм > volume_mult × avg(20)

    ШОРТ (після девіації вгору):
      ✅ Червона свічка (close < open)
      ✅ close < min(low) за structure_lookback свічок
      ✅ Об'єм > volume_mult × avg(20)

    Після BOS → ОДРАЗУ маркет ордер. НЕ чекаємо наступну свічку.
    Якщо structure_lookback=1 → порівняння тільки з попередньою свічкою.
    """
    min_required = max(structure_lookback + 1, 21)
    if len(df_fast) < min_required:
        return False

    last     = df_fast.iloc[-1]
    ref      = df_fast.iloc[-(structure_lookback + 1):-1]
    avg_vol  = float(df_fast["volume"].iloc[-20:].mean())
    good_vol = float(last["volume"]) > avg_vol * volume_mult

    if direction == "long":
        green = float(last["close"]) > float(last["open"])
        above = float(last["close"]) > float(ref["high"].max())
        return green and above and good_vol

    if direction == "short":
        red   = float(last["close"]) < float(last["open"])
        below = float(last["close"]) < float(ref["low"].min())
        return red and below and good_vol

    return False


# ─────────────────────────────────────────────────────────────
# build_trade_levels — логіка з generator.py
# ─────────────────────────────────────────────────────────────

def build_trade_levels(
    direction: str,
    entry: float,
    range_low: float,
    range_high: float,
    sweep_extreme: float,
    atr: float,
    min_rr: float = 1.5,
    stop_pad_atr: float = 0.12,
) -> Optional[dict]:
    """
    Розраховує TP і SL за логікою generator.py (_build_trade_levels).

    Стратегія TP (midpoint-first):
      ЛОНГ: спочатку пробуємо TP = midpoint рейнджу.
            Якщо RR < min_rr → пробуємо TP = range_high.
            Якщо знову < min_rr → повертаємо None (угода не торгується).

      ШОРТ: спочатку пробуємо TP = midpoint.
            Якщо RR < min_rr → пробуємо TP = range_low.

    SL:
      ЛОНГ: SL = min(sweep_extreme, range_low) - atr × stop_pad_atr
            (за найнижчою точкою девіації + буфер)
      ШОРТ: SL = max(sweep_extreme, range_high) + atr × stop_pad_atr

    ЧОМУ midpoint-first (а не range_pct=70%):
      При range_pct=70% TP = low + size×0.7.
      На вузькому рейнджі це може бути дуже близько до entry,
      що дає поганий RR. midpoint-first гарантує:
        1. Спочатку перевіряємо реалістичний TP (середина рейнджу).
        2. Якщо RR не проходить — беремо повний рейндж як ціль.
        3. Якщо і це не дає мінімальний RR → пропускаємо угоду.

    Повертає dict або None якщо RR < min_rr за будь-якого TP.
    """
    if atr <= 0 or entry <= 0:
        return None

    mid       = (range_high + range_low) / 2.0
    stop_pad  = atr * stop_pad_atr

    if direction == "long":
        sl = min(sweep_extreme, range_low) - stop_pad
        if sl >= entry:
            return None

        for tp in [mid, range_high]:
            if tp <= entry:
                continue
            rr = (tp - entry) / (entry - sl)
            if rr >= min_rr:
                return {
                    "tp":     float(tp),
                    "sl":     float(sl),
                    "raw_rr": float(rr),
                    "tp_target": "midpoint" if tp == mid else "range_high",
                }
        return None

    if direction == "short":
        sl = max(sweep_extreme, range_high) + stop_pad
        if sl <= entry:
            return None

        for tp in [mid, range_low]:
            if tp >= entry:
                continue
            rr = (entry - tp) / (sl - entry)
            if rr >= min_rr:
                return {
                    "tp":     float(tp),
                    "sl":     float(sl),
                    "raw_rr": float(rr),
                    "tp_target": "midpoint" if tp == mid else "range_low",
                }
        return None

    return None


# ─────────────────────────────────────────────────────────────
# calculate_levels — спрощений TP/SL для signal_engine / dual_df
# ─────────────────────────────────────────────────────────────

def calculate_levels(
    range_data: dict,
    direction: str,
    deviation_extreme: float,
    atr: float,
    mode: str = "range_pct",
) -> dict:
    """
    Спрощений розрахунок TP і SL.

    mode="range_pct" (для signal_engine.py):
      TP ставиться на 70% шляху від краю рейнджу до протилежного.
      SL ставиться за фетилем девіаційної свічки + 0.3 ATR буфер.

        ЛОНГ:  TP = range_low  + range_size × TP_RANGE_PCT
               SL = deviation_extreme - atr × SL_ATR_BUFFER
        ШОРТ:  TP = range_high - range_size × TP_RANGE_PCT
               SL = deviation_extreme + atr × SL_ATR_BUFFER

      TP_RANGE_PCT = 0.70 і SL_ATR_BUFFER = 0.3 відповідають
      значенням з settings.py (TP_RANGE_PCT, SL_ATR_BUFFER).

    mode="midpoint" (для dual_df.py):
      TP = midpoint рейнджу (30m середина).
      SL = за фетилем девіації + 0.12 ATR буфер.

        ЛОНГ:  TP = mid
               SL = min(deviation_extreme, range_low) - atr × 0.12
        ШОРТ:  TP = mid
               SL = max(deviation_extreme, range_high) + atr × 0.12

      ЧОМУ midpoint для dual_df:
        dual_df стратегія — це range reversion: ціна вийшла за межу
        30m рейнджу (девіація), ми чекаємо повернення назад в рейндж.
        Логічна ціль — середина рейнджу (midpoint), а не 70% від краю,
        бо ціна повертається в центр консолідації, де знаходиться
        найбільша ліквідність і де маркет-мейкери закривають позиції.
        range_pct=70% на вузькому рейнджі дає гірший RR ніж midpoint.

    Повертає dict з tp, sl, tp_distance, sl_distance.
    """
    h = range_data["high"]
    l = range_data["low"]
    s = range_data["size"]
    m = range_data.get("mid", (h + l) / 2.0)

    if mode == "midpoint":
        stop_pad = atr * 0.12
        if direction == "long":
            tp = m
            sl = min(deviation_extreme, l) - stop_pad
        else:
            tp = m
            sl = max(deviation_extreme, h) + stop_pad
    else:
        # range_pct (дефолт, для signal_engine)
        if direction == "long":
            tp = l + s * 0.70
            sl = deviation_extreme - atr * 0.3
        else:
            tp = h - s * 0.70
            sl = deviation_extreme + atr * 0.3

    return {
        "tp":          tp,
        "sl":          sl,
        "tp_distance": abs(tp - deviation_extreme),
        "sl_distance": abs(sl - deviation_extreme),
    }