"""
SIGNAL ENGINE — зведення всіх індикаторів в один сигнал
=========================================================
Приймає рішення тільки коли ВСІ умови збігаються:

  Шар 1: BB Squeeze активний (ринок у рейнджі)
  Шар 2: Девіація виявлена (liquidity grab)
  Шар 3: CVD підтверджує розворот тиску
  Шар 4: Stochastic у правильній зоні + перетин
  Шар 5: R:R >= 1.5 після комісій

Якщо хоч один шар не підтверджує → сигнал = None → не торгуємо.
"""

import pandas as pd
import numpy as np
from loguru import logger

from indicators.atr        import add_atr
from indicators.bollinger  import add_bollinger
from indicators.stochastic import calculate_stochastic
from indicators.cvd        import calculate_cvd
from indicators.deviation  import detect_deviation, calculate_rr


def run_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Послідовно застосовує всі індикатори до DataFrame.

    Порядок важливий: ATR і BB потрібні для deviation.

    Вхід:  df з колонками open, high, low, close, volume
    Вихід: df з усіма індикаторними колонками
    """
    df = add_atr(df)           # atr_slow, atr_fast
    df = add_bollinger(df)     # bb_*, squeeze, range_*
    df = calculate_stochastic(df)  # stoch_k, stoch_d, signals
    df = calculate_cvd(df)     # cvd, delta, signals
    df = detect_deviation(df)  # dev_*, sl_*, tp_*
    return df


def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Генерує фінальний торговий сигнал на основі всіх шарів.

    Додає колонки:
        signal       — 'long', 'short', або None
        signal_score — кількість підтверджень (1-5)
        sl           — рівень стоп-лосу
        tp           — рівень тейк-профіту
        rr_net       — чистий R:R після комісій
    """
    df = df.copy()
    df["signal"]       = None
    df["signal_score"] = 0
    df["sl"]           = np.nan
    df["tp"]           = np.nan
    df["rr_net"]       = np.nan

    for i in range(len(df)):
        row = df.iloc[i]

        # ─── ЛОНГ сигнал ──────────────────────────────────────
        if _check_long(row, df, i):
            rr = calculate_rr(
                entry=row["close"],
                sl=row["sl_long"],
                tp=row["tp_long"],
            )
            if rr["viable"]:
                df.at[df.index[i], "signal"]       = "long"
                df.at[df.index[i], "signal_score"] = _score_long(row)
                df.at[df.index[i], "sl"]           = row["sl_long"]
                df.at[df.index[i], "tp"]           = row["tp_long"]
                df.at[df.index[i], "rr_net"]       = rr["net_rr"]

        # ─── ШОРТ сигнал ──────────────────────────────────────
        elif _check_short(row, df, i):
            rr = calculate_rr(
                entry=row["close"],
                sl=row["sl_short"],
                tp=row["tp_short"],
            )
            if rr["viable"]:
                df.at[df.index[i], "signal"]       = "short"
                df.at[df.index[i], "signal_score"] = _score_short(row)
                df.at[df.index[i], "sl"]           = row["sl_short"]
                df.at[df.index[i], "tp"]           = row["tp_short"]
                df.at[df.index[i], "rr_net"]       = rr["net_rr"]

    total = (df["signal"].notna()).sum()
    logger.info(f"Сигналів згенеровано: {total} з {len(df)} свічок")
    return df


def _check_long(row: pd.Series, df: pd.DataFrame, i: int) -> bool:
    """
    Перевіряє всі 4 умови для лонгу:
    1. Squeeze активний
    2. Бичача девіація (ціна пробила range_low і повернулась)
    3. CVD підтверджує (accumulation або bullish)
    4. Stochastic в зоні oversold з перетином
    """
    try:
        return (
            bool(row.get("squeeze", False)) and          # шар 1: рейндж
            bool(row.get("dev_bullish", False)) and      # шар 2: девіація
            row.get("cvd_signal") in ("accumulation", "bullish") and  # шар 3: CVD
            bool(row.get("stoch_long", False))           # шар 4: Stochastic
        )
    except Exception:
        return False


def _check_short(row: pd.Series, df: pd.DataFrame, i: int) -> bool:
    """Перевіряє всі 4 умови для шорту."""
    try:
        return (
            bool(row.get("squeeze", False)) and
            bool(row.get("dev_bearish", False)) and
            row.get("cvd_signal") in ("distribution", "bearish") and
            bool(row.get("stoch_short", False))
        )
    except Exception:
        return False


def _score_long(row: pd.Series) -> int:
    """Рахує силу лонг сигналу (1-5). Більше = сильніший сигнал."""
    score = 0
    if row.get("squeeze"):                                    score += 1
    if row.get("dev_bullish"):                                score += 1
    if row.get("cvd_signal") == "accumulation":               score += 2
    elif row.get("cvd_signal") == "bullish":                  score += 1
    if row.get("stoch_long") and row.get("stoch_zone") == "oversold": score += 1
    return score


def _score_short(row: pd.Series) -> int:
    """Рахує силу шорт сигналу (1-5)."""
    score = 0
    if row.get("squeeze"):                                    score += 1
    if row.get("dev_bearish"):                                score += 1
    if row.get("cvd_signal") == "distribution":               score += 2
    elif row.get("cvd_signal") == "bearish":                  score += 1
    if row.get("stoch_short") and row.get("stoch_zone") == "overbought": score += 1
    return score
