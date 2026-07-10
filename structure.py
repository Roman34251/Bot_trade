"""
СТРУКТУРА РИНКУ / FTA — First Trouble Area (проблемні зони на старшому ТФ)
=========================================================================
FTA (First Trouble Area) — концепт зі smart-money / ICT: це НАЙБЛИЖЧА
протилежна зона (свінг-хай для лонга / свінг-лоу для шорта) на СТАРШОМУ ТФ,
яку ціна мусить пробити ПЕРШ НІЖ дійде до нашого TP.

Навіщо це боту:
  Якщо між входом і ціллю (TP) стоїть велика зустрічна зона старшого ТФ —
  імовірність, що ціна легко долетить до TP, НИЖЧА (там сидить зустрічна
  ліквідність / реакція). Такі угоди — «проти найближчої перешкоди».
  Бот має це БАЧИТИ: або пропускати такий вхід, або хоча б позначати в
  сповіщенні, що TP «за перешкодою».

Що рахуємо:
  1) Свінги (fractal pivots) на HTF: локальні максимуми/мінімуми.
  2) FTA у напрямку угоди:
       LONG  → найближчий свінг-ХАЙ ВИЩЕ входу (опір).
       SHORT → найближчий свінг-ЛОУ НИЖЧЕ входу (підтримка).
  3) blocks_tp = чи стоїть ця зона МІЖ входом і TP (тобто TP «за нею»).
"""

from __future__ import annotations

from typing import Optional, List, Tuple

import pandas as pd


def find_swings(
    df: pd.DataFrame,
    lookback: int = 3,
) -> Tuple[List[float], List[float]]:
    """
    Fractal-свінги: точка i — свінг-хай, якщо high[i] — максимум у вікні
    [i-lookback, i+lookback]; аналогічно свінг-лоу для low[i].

    Останні `lookback` свічок пропускаємо — вони ще НЕ підтверджені
    (справа бракує свічок, щоб пік вважався сформованим).

    Повертає (swing_highs, swing_lows) — списки цін.
    """
    n = len(df)
    if n < 2 * lookback + 1:
        return [], []

    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values

    swing_highs: List[float] = []
    swing_lows: List[float] = []

    for i in range(lookback, n - lookback):
        window_h = highs[i - lookback : i + lookback + 1]
        window_l = lows[i - lookback : i + lookback + 1]
        if highs[i] == window_h.max():
            swing_highs.append(float(highs[i]))
        if lows[i] == window_l.min():
            swing_lows.append(float(lows[i]))

    return swing_highs, swing_lows


def first_trouble_area(
    df_htf: pd.DataFrame,
    direction: str,
    entry: float,
    tp: float,
    lookback: int = 3,
    buffer_pct: float = 0.0005,
) -> Optional[dict]:
    """
    Знаходить найближчу проблемну зону (FTA) на старшому ТФ у напрямку угоди.

    Параметри:
      df_htf     — DataFrame старшого ТФ (напр. 1h) з OHLC.
      direction  — 'long' / 'short'.
      entry, tp  — ціни входу і цілі.
      lookback   — глибина fractal-свінгів.
      buffer_pct — ігноруємо рівні впритул до входу (шум), напр. 0.05%.

    Повертає dict:
      {
        "fta": float | None,        # ціна найближчої зустрічної зони
        "blocks_tp": bool,          # чи TP «за» цією зоною
        "dist_pct": float | None,   # відстань входу до FTA, %
      }
    або None, якщо даних замало.
    """
    if df_htf is None or len(df_htf) < 2 * lookback + 2:
        return None
    if entry <= 0:
        return None

    swing_highs, swing_lows = find_swings(df_htf, lookback=lookback)

    fta: Optional[float] = None

    if direction == "long":
        # Опір: найнижчий свінг-хай, що ВИЩЕ входу (перша перешкода зверху)
        above = [h for h in swing_highs if h > entry * (1.0 + buffer_pct)]
        if above:
            fta = min(above)
        blocks = fta is not None and fta < tp  # TP вище перешкоди → «за нею»
    elif direction == "short":
        # Підтримка: найвищий свінг-лоу, що НИЖЧЕ входу (перша перешкода знизу)
        below = [l for l in swing_lows if l < entry * (1.0 - buffer_pct)]
        if below:
            fta = max(below)
        blocks = fta is not None and fta > tp  # TP нижче перешкоди → «за нею»
    else:
        return None

    dist_pct = (abs(fta - entry) / entry * 100.0) if fta is not None else None

    return {
        "fta": fta,
        "blocks_tp": bool(blocks),
        "dist_pct": dist_pct,
    }
