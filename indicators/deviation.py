"""
DEVIATION DETECTOR — ядро стратегії
=====================================
Що таке девіація:
  Ціна виходить за межу рейнджу (liquidity grab) але
  закривається ВСЕРЕДИНІ — це хибний пробій.

Чому це важливо:
  Великі гравці навмисно виштовхують ціну за межі рейнджу
  щоб зібрати стопи роздрібних трейдерів і ліквідність.
  Після цього ціна різко повертається всередину рейнджу.
  МИ заходимо в цей зворотній рух.

Умови девіації (всі мають бути True):
  1. Ринок у squeeze (рейндж підтверджений)
  2. Свічка пробила межу рейнджу на > 0.3 ATR (фетиль назовні)
  3. Свічка ЗАКРИЛАСЬ всередині рейнджу (не справжній пробій)
  4. Об'єм < 1.5x середнього (великий об'єм = реальний пробій)

Параметри:
  DEVIATION_ATR_MULT = 0.3   мін. розмір фетиля девіації в ATR
  VOLUME_RATIO_MAX   = 1.5   макс. об'єм відносно середнього
  SL_ATR_BUFFER      = 0.3   буфер SL за фетилем
  TP_RANGE_PCT       = 0.70  TP = 70% до протилежної межі
"""

import pandas as pd
import numpy as np

DEVIATION_ATR_MULT = 0.3
VOLUME_RATIO_MAX   = 1.5
SL_ATR_BUFFER      = 0.3
TP_RANGE_PCT       = 0.70


def detect_deviation(df: pd.DataFrame) -> pd.DataFrame:
    """
    Виявляє девіації (liquidity grab) на межах рейнджу.

    Вимагає що df вже має колонки від bollinger.py і atr.py:
        squeeze, range_high, range_low, atr_fast

    Додає колонки:
        dev_bearish  — девіація вгору (шорт сигнал)
        dev_bullish  — девіація вниз (лонг сигнал)
        dev_signal   — 'long', 'short', None
        sl_long      — рівень SL для лонгу
        tp_long      — рівень TP для лонгу
        sl_short     — рівень SL для шорту
        tp_short     — рівень TP для шорту
    """
    df = df.copy()

    avg_volume = df["volume"].rolling(20).mean()
    atr        = df["atr_fast"]

    # ─── Девіація ВГОРУ (bearish) → шорт ───────────────────────
    # Умова: фетиль пробив range_high але свічка закрилась нижче
    df["dev_bearish"] = (
        df["squeeze"] &                                             # рейндж активний
        (df["high"] > df["range_high"] + DEVIATION_ATR_MULT * atr) & # фетиль за межею
        (df["close"] < df["range_high"]) &                          # закрились всередині
        (df["volume"] < avg_volume * VOLUME_RATIO_MAX)              # не великий об'єм
    )

    # ─── Девіація ВНИЗ (bullish) → лонг ────────────────────────
    df["dev_bullish"] = (
        df["squeeze"] &
        (df["low"] < df["range_low"] - DEVIATION_ATR_MULT * atr) &
        (df["close"] > df["range_low"]) &
        (df["volume"] < avg_volume * VOLUME_RATIO_MAX)
    )

    # ─── Читабельний сигнал ─────────────────────────────────────
    df["dev_signal"] = None
    df.loc[df["dev_bullish"],  "dev_signal"] = "long"
    df.loc[df["dev_bearish"],  "dev_signal"] = "short"

    # ─── SL і TP для лонгу ──────────────────────────────────────
    # SL: нижче найнижчого фетиля девіації + буфер
    # TP: 70% відстані від входу до верхньої межі рейнджу
    df["sl_long"] = df["low"] - SL_ATR_BUFFER * atr
    df["tp_long"] = df["close"] + TP_RANGE_PCT * (df["range_high"] - df["close"])

    # ─── SL і TP для шорту ──────────────────────────────────────
    df["sl_short"] = df["high"] + SL_ATR_BUFFER * atr
    df["tp_short"] = df["close"] - TP_RANGE_PCT * (df["close"] - df["range_low"])

    return df


def calculate_rr(
    entry: float,
    sl: float,
    tp: float,
    quantity: float = 1.0,
    fee: float = 0.00055,
) -> dict:
    """
    Розраховує реальний R:R з урахуванням комісій Bybit.

    Маркет ордер = taker fee 0.055% на ВХІД і на ВИХІД.
    Це критично для скальпінгу: 0.055% × 2 = 0.11% з кожної угоди.

    Повертає dict з:
        gross_rr  — R:R без комісій
        net_rr    — R:R після комісій (реальний)
        viable    — True якщо net_rr >= 1.5
        fee_usdt  — загальна комісія в USDT
    """
    gross_profit = abs(tp - entry) * quantity
    gross_loss   = abs(sl - entry) * quantity

    if gross_loss == 0:
        return {"gross_rr": 0, "net_rr": 0, "viable": False, "fee_usdt": 0}

    # Комісія: taker на вхід + taker на вихід (обидва маркет)
    fee_in  = entry * quantity * fee
    fee_out_tp = tp * quantity * fee
    fee_out_sl = sl * quantity * fee

    net_profit = gross_profit - fee_in - fee_out_tp
    net_loss   = gross_loss   + fee_in + fee_out_sl

    net_rr = net_profit / net_loss if net_loss > 0 else 0

    return {
        "gross_rr":  round(gross_profit / gross_loss, 2),
        "net_rr":    round(net_rr, 2),
        "net_profit": round(net_profit, 4),
        "net_loss":   round(net_loss, 4),
        "fee_usdt":  round(fee_in + fee_out_tp, 4),
        "viable":    net_rr >= 1.5,
    }
