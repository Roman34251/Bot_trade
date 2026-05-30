"""
СИГНАЛИ ВХОДУ — молодший ТФ (5m / 1m)
========================================
Всі індикатори тут швидкі — реагують за 3-5 свічок.
Це критично для скальпінгу маркет ордерами.

Порядок перевірки (від найшвидшого):
  1. Order Flow Delta  — поточна свічка
  2. CVD розворот      — 3 свічки
  3. Stochastic        — 5 свічок
  4. Об'єм девіації    — 20 свічок (avg)
  5. BOS               — 2 свічки (поточна + попередня)
"""

import pandas as pd
import numpy as np

try:
    import pandas_ta as ta
    HAS_PANDAS_TA = True
except ImportError:
    HAS_PANDAS_TA = False


def _stoch_manual(high: pd.Series, low: pd.Series, close: pd.Series,
                  k: int = 5, d: int = 3, smooth_k: int = 3) -> dict:
    lowest_low   = low.rolling(k).min()
    highest_high = high.rolling(k).max()
    raw_k   = 100 * (close - lowest_low) / (highest_high - lowest_low + 1e-9)
    k_line  = raw_k.rolling(smooth_k).mean()
    d_line  = k_line.rolling(d).mean()
    return {"k": k_line, "d": d_line}


# ── Stochastic (5,3,3) ────────────────────────────────────

def stochastic_signal(df: pd.DataFrame,
                      k: int = 5, d: int = 3, smooth_k: int = 3) -> dict:
    """
    Stochastic замість RSI для скальпінгу.

    ЧОМУ Stochastic швидший за RSI:
      RSI(14) потребує 14 свічок → на 1m це 14 хвилин запізнення
      Stoch(5,3,3) реагує за 5 свічок → 5 хвилин на 1m

    Сигнали:
      %K < 20 = перепроданість → лонг зона після девіації вниз
      %K > 80 = перекупленість → шорт зона після девіації вгору
      Crossover K/D = підтвердження розвороту

    На 5m свічках (рекомендовано для фільтру):
      Stoch < 20 + crossover вгору = підтверджений лонг сигнал

    На 1m свічках (для BOS підтвердження):
      Stoch < 20 = достатньо (crossover може не встигнути)
    """
    if len(df) < k + d + smooth_k:
        return {"k": 50.0, "d": 50.0, "oversold": False,
                "overbought": False, "bullish_cross": False, "bearish_cross": False}

    if HAS_PANDAS_TA:
        stoch  = ta.stoch(df['high'], df['low'], df['close'],
                          k=k, d=d, smooth_k=smooth_k)
        k_col  = f'STOCHk_{k}_{d}_{smooth_k}'
        d_col  = f'STOCHd_{k}_{d}_{smooth_k}'
        k_val  = float(stoch[k_col].iloc[-1])
        d_val  = float(stoch[d_col].iloc[-1])
        k_prev = float(stoch[k_col].iloc[-2])
        d_prev = float(stoch[d_col].iloc[-2])
    else:
        st     = _stoch_manual(df['high'], df['low'], df['close'], k, d, smooth_k)
        k_val  = float(st["k"].iloc[-1])
        d_val  = float(st["d"].iloc[-1])
        k_prev = float(st["k"].iloc[-2])
        d_prev = float(st["d"].iloc[-2])

    # NaN перевірка
    if any(pd.isna(v) for v in [k_val, d_val, k_prev, d_prev]):
        return {"k": 50.0, "d": 50.0, "oversold": False,
                "overbought": False, "bullish_cross": False, "bearish_cross": False}

    bullish_cross = (k_prev < d_prev) and (k_val > d_val)
    bearish_cross = (k_prev > d_prev) and (k_val < d_val)

    return {
        "k":             k_val,
        "d":             d_val,
        "oversold":      k_val < 20,         # лонг зона
        "overbought":    k_val > 80,         # шорт зона
        "bullish_cross": bullish_cross,      # K перетнув D знизу вгору
        "bearish_cross": bearish_cross,      # K перетнув D зверху вниз
    }


# ── CVD — Cumulative Volume Delta ─────────────────────────

def calculate_cvd(df: pd.DataFrame) -> pd.Series:
    """
    CVD = накопичена дельта об'єму.

    Апроксимація (без trades даних):
      Зелена свічка (close > open) → весь об'єм = купівлі
      Червона свічка               → весь об'єм = продажі

    Реагує на КОЖНУ свічку → ідеально для скальпінгу.

    Для точнішого CVD потрібні trades дані (не OHLCV).
    В бектесті використовуємо цю апроксимацію.
    """
    delta = np.where(df['close'] > df['open'], df['volume'], -df['volume'])
    return pd.Series(delta, index=df.index).cumsum()


def cvd_reversal(cvd: pd.Series, lookback: int = 3) -> str:
    """
    Визначає розворот CVD за останні N свічок.

    'bullish' = CVD 3 свічки підряд росте
                → покупці повертаються після девіації вниз
                → сигнал для лонгу

    'bearish' = CVD 3 свічки підряд падає
                → продавці заходять після девіації вгору
                → сигнал для шорту

    'none'    = немає чіткого сигналу → пропускаємо

    lookback=3 достатньо для скальпінгу.
    Більше = повільніше реагує.
    """
    if len(cvd) < lookback:
        return "none"

    r = cvd.iloc[-lookback:]
    vals = [float(r.iloc[i]) for i in range(lookback)]

    if all(vals[i] > vals[i-1] for i in range(1, lookback)):
        return "bullish"
    if all(vals[i] < vals[i-1] for i in range(1, lookback)):
        return "bearish"
    return "none"


# ── Order Flow Delta ──────────────────────────────────────

def order_flow_delta(df: pd.DataFrame, lookback: int = 3) -> dict:
    """
    Апроксимація тиску покупців/продавців (формула Kaufman).

    Логіка:
      Якщо ціна закрилась ближче до хаю → більшість об'єму = купівлі
      Якщо ціна закрилась ближче до лоу → більшість об'єму = продажі

    Buy vol  = volume × (close - low) / (high - low)
    Sell vol = volume × (high - close) / (high - low)
    Delta    = buy_vol - sell_vol

    lookback=3 для скальпінгу (швидка реакція).
    Позитивна delta = покупці домінують → лонг сигнал.
    Негативна delta = продавці домінують → шорт сигнал.

    Найшвидший індикатор — рахуємо першим.
    """
    if len(df) < lookback:
        return {"delta": 0.0, "is_bullish": False, "is_bearish": False,
                "delta_per_candle": 0.0}

    recent = df.iloc[-lookback:]
    hl     = (recent['high'] - recent['low']).replace(0, 1e-9)
    buy_v  = recent['volume'] * (recent['close'] - recent['low'])  / hl
    sell_v = recent['volume'] * (recent['high']  - recent['close']) / hl
    delta  = float((buy_v - sell_v).sum())

    return {
        "delta":            delta,
        "is_bullish":       delta > 0,
        "is_bearish":       delta < 0,
        "delta_per_candle": delta / lookback,
    }


# ── Об'єм на девіації ─────────────────────────────────────

def volume_on_deviation(df: pd.DataFrame,
                        avg_period: int = 20,
                        false_breakout_ratio: float = 1.5) -> dict:
    """
    Перевіряє чи є девіація false breakout (пастка).

    False breakout (торгуємо — ціна повернеться):
      Об'єм девіаційної свічки < 1.5x середнього об'єму
      Причина: великих гравців нема, тільки стопи підбирають

    Реальний пробій (пропускаємо — ціна може продовжити):
      Об'єм > 2.0x середнього об'єму
      Причина: великі гравці проштовхують ціну далі

    Перевіряємо останню свічку df.iloc[-1].
    """
    if len(df) < avg_period:
        return {"volume_ratio": 1.0, "is_false_breakout": True,
                "avg_volume": 0.0, "dev_volume": 0.0}

    avg_vol = float(df['volume'].iloc[-avg_period:].mean())
    dev_vol = float(df['volume'].iloc[-1])
    ratio   = dev_vol / avg_vol if avg_vol > 0 else 1.0

    return {
        "volume_ratio":      ratio,
        "is_false_breakout": ratio < false_breakout_ratio,  # < 1.5 = пастка
        "avg_volume":        avg_vol,
        "dev_volume":        dev_vol,
    }


# ── BOS — Break of Structure ──────────────────────────────

def detect_bos(df_fast: pd.DataFrame, direction: str,
               volume_mult: float = 1.2) -> bool:
    """
    BOS = перша свічка що підтверджує розворот після девіації.
    Це тригер для маркет ордера.

    Обидві умови обов'язкові (BOS варіант В):

    ЛОНГ (після девіації вниз):
      ✅ Зелена свічка (close > open)
      ✅ close > попереднього high (пробила локальний опір)
      ✅ Об'єм > 1.2x середнього (підтверджений рух)

    ШОРТ (після девіації вгору):
      ✅ Червона свічка (close < open)
      ✅ close < попереднього low (пробила локальну підтримку)
      ✅ Об'єм > 1.2x середнього

    Після BOS → ОДРАЗУ маркет ордер.
    НЕ чекаємо закриття наступної свічки.

    df_fast = 1m свічки для Режиму А.
    """
    if len(df_fast) < 21:   # потрібно мінімум для avg_volume
        return False

    last    = df_fast.iloc[-1]
    prev    = df_fast.iloc[-2]
    avg_vol = float(df_fast['volume'].iloc[-20:].mean())
    good_vol = float(last['volume']) > avg_vol * volume_mult

    if direction == "long":
        green   = float(last['close']) > float(last['open'])
        above   = float(last['close']) > float(prev['high'])
        return green and above and good_vol

    if direction == "short":
        red     = float(last['close']) < float(last['open'])
        below   = float(last['close']) < float(prev['low'])
        return red and below and good_vol

    return False


# ── Рівні TP / SL ─────────────────────────────────────────

def calculate_levels(range_data: dict, direction: str,
                     deviation_extreme: float, atr: float) -> dict:
    """
    Розраховує TP і SL за правилами стратегії.

    ЛОНГ (після девіації вниз):
      TP = range_low + range_size × 0.70
           → 70% шляху до верхньої межі
           → потрапляє на фетиль верхнього хаю рейнджу
      SL = deviation_extreme - atr × 0.3
           → за фетилем девіації (нижче низу девіаційної свічки)

    ШОРТ (після девіації вгору):
      TP = range_high - range_size × 0.70
           → 70% шляху до нижньої межі
           → потрапляє на фетиль нижнього лоу рейнджу
      SL = deviation_extreme + atr × 0.3
           → за фетилем девіації (вище хаю девіаційної свічки)

    ВАЖЛИВО:
      Ці рівні — до slippage.
      Реальний R:R рахуй через calculate_position() в risk модулі.
    """
    h = range_data["high"]
    l = range_data["low"]
    s = range_data["size"]

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