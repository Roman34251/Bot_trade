"""
STOCHASTIC OSCILLATOR
======================
Що це: показує де зараз ціна відносно діапазону
       за останні N свічок (0–100).

Чому Stochastic а не RSI для скальпінгу:
  - RSI реагує повільніше (стандартний period=14)
  - Stochastic з K=5 реагує за 5 свічок = ідеально для 1m-5m
  - %K і %D перетини дають чіткі сигнали входу в зонах OB/OS

Параметри (зі скілу):
  STOCH_K       = 5    свічок (швидка лінія)
  STOCH_D       = 3    smoothing D (повільна, сигнальна)
  STOCH_SMOOTH  = 3    smoothing K
  STOCH_OVERSOLD   = 20  < 20 = зона перепроданості (лонг)
  STOCH_OVERBOUGHT = 80  > 80 = зона перекупленості (шорт)

Читання сигналу:
  - %K перетинає %D знизу вгору в зоні < 30 → лонг сигнал
  - %K перетинає %D зверху вниз в зоні > 70 → шорт сигнал
"""

import pandas as pd
import numpy as np

STOCH_K          = 5
STOCH_D          = 3
STOCH_SMOOTH     = 3
STOCH_OVERSOLD   = 20
STOCH_OVERBOUGHT = 80


def calculate_stochastic(df: pd.DataFrame) -> pd.DataFrame:
    """
    Розраховує Stochastic %K і %D.

    Додає колонки:
        stoch_k      — швидка лінія (0–100)
        stoch_d      — повільна/сигнальна лінія (0–100)
        stoch_long   — True коли K перетинає D вгору в зоні OS
        stoch_short  — True коли K перетинає D вниз в зоні OB
        stoch_zone   — 'oversold', 'overbought', 'neutral'
    """
    df = df.copy()

    low_min  = df["low"].rolling(STOCH_K).min()
    high_max = df["high"].rolling(STOCH_K).max()

    # Сирий %K: де ціна в діапазоні (0 = на мінімумі, 100 = на максимумі)
    # +1e-10 захист від ділення на нуль коли high == low (flat ринок)
    stoch_k_raw = (
        100 * (df["close"] - low_min) / (high_max - low_min + 1e-10)
    )

    # Згладжений %K
    df["stoch_k"] = stoch_k_raw.rolling(STOCH_SMOOTH).mean()

    # %D = середнє %K за 3 свічки (сигнальна лінія)
    df["stoch_d"] = df["stoch_k"].rolling(STOCH_D).mean()

    # ─── Сигнали перетину ──────────────────────────────────────
    k_prev = df["stoch_k"].shift(1)
    d_prev = df["stoch_d"].shift(1)

    # Лонг: K перетинає D знизу вгору + знаходимось в зоні перепроданості
    df["stoch_long"] = (
        (df["stoch_k"] > df["stoch_d"]) &   # K вище D зараз
        (k_prev <= d_prev) &                  # K був нижче D раніше
        (df["stoch_k"] < STOCH_OVERSOLD + 10) # перетин в зоні OS (±10)
    )

    # Шорт: K перетинає D зверху вниз + знаходимось в зоні перекупленості
    df["stoch_short"] = (
        (df["stoch_k"] < df["stoch_d"]) &
        (k_prev >= d_prev) &
        (df["stoch_k"] > STOCH_OVERBOUGHT - 10)
    )

    # Зона для читабельності в логах
    df["stoch_zone"] = "neutral"
    df.loc[df["stoch_k"] < STOCH_OVERSOLD,   "stoch_zone"] = "oversold"
    df.loc[df["stoch_k"] > STOCH_OVERBOUGHT, "stoch_zone"] = "overbought"

    return df
