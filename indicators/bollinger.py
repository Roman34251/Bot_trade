"""
BOLLINGER BANDS + RANGE DETECTOR
==================================
Що це: BB — канал навколо середньої ціни.
       Ширина каналу = волатильність.

Навіщо в нашій стратегії:
  - Стиснення BB (squeeze) = ринок у рейнджі (боковику)
  - Коли BB width < 2% протягом 20+ свічок → рейндж підтверджено
  - Верхня і нижня межа BB = межі рейнджу для торгівлі

Параметри (зі скілу):
  BB_PERIOD = 20    свічок
  BB_STD    = 2.0   стандартних відхилення
  BB_SQUEEZE_PCT  = 2.0%  поріг стиснення
  RANGE_MIN_CANDLES = 20  мін. свічок у стисненні
"""

import pandas as pd
import numpy as np

# Параметри з конфігу
BB_PERIOD          = 20
BB_STD             = 2.0
BB_SQUEEZE_PCT     = 2.0
RANGE_MIN_CANDLES  = 20


def calculate_bb(df: pd.DataFrame) -> pd.DataFrame:
    """
    Розраховує Bollinger Bands.

    Додає колонки:
        bb_mid   — середня (SMA 20)
        bb_upper — верхня межа (mid + 2σ)
        bb_lower — нижня межа (mid − 2σ)
        bb_width — ширина у % від середньої (міра волатильності)
    """
    df = df.copy()

    df["bb_mid"]   = df["close"].rolling(BB_PERIOD).mean()
    bb_std         = df["close"].rolling(BB_PERIOD).std()
    df["bb_upper"] = df["bb_mid"] + BB_STD * bb_std
    df["bb_lower"] = df["bb_mid"] - BB_STD * bb_std

    # BB Width у відсотках
    # Чим менше — тим вужчий канал = менша волатильність = рейндж
    df["bb_width"] = (
        (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"] * 100
    )

    return df


def detect_squeeze(df: pd.DataFrame) -> pd.DataFrame:
    """
    Визначає чи ринок у рейнджі (squeeze).

    Squeeze = BB width був < 2% протягом останніх 20 свічок.
    Якщо True → ринок консолідується → чекаємо девіацію.

    Додає колонки:
        squeeze      — True якщо зараз у стисненні
        range_high   — верхня межа рейнджу (max BB upper за період)
        range_low    — нижня межа рейнджу (min BB lower за період)
        range_size   — розмір рейнджу в USDT
        range_size_pct — розмір рейнджу у %
    """
    df = df.copy()

    # Squeeze: максимальна ширина за N свічок < порогу
    df["squeeze"] = (
        df["bb_width"]
        .rolling(RANGE_MIN_CANDLES)
        .max()
        < BB_SQUEEZE_PCT
    )

    # Межі рейнджу фіксуємо коли squeeze активний
    # Беремо max/min BB за весь період стиснення
    df["range_high"] = df["bb_upper"].rolling(RANGE_MIN_CANDLES).max()
    df["range_low"]  = df["bb_lower"].rolling(RANGE_MIN_CANDLES).min()

    # Розмір рейнджу (нам потрібно знати куди ставити TP = 70%)
    df["range_size"]     = df["range_high"] - df["range_low"]
    df["range_size_pct"] = df["range_size"] / df["range_low"] * 100

    return df


def add_bollinger(df: pd.DataFrame) -> pd.DataFrame:
    """
    Головна функція: BB + Squeeze в один виклик.

    Використання:
        df = add_bollinger(df)
        # тепер df має: bb_mid, bb_upper, bb_lower,
        #               bb_width, squeeze, range_high, range_low
    """
    df = calculate_bb(df)
    df = detect_squeeze(df)
    return df
