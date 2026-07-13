"""
СТРАТЕГІЯ B — MEAN-REVERSION (Bollinger Bands + RSI)
=====================================================
Незалежна стратегія (НЕ підтвердження для sweep). Працює в боковику.

Ідея (як торгують скальпери-mean-reversion, Connors RSI-2 / BB-fade):
  - Ціна проколола НИЖНЮ смугу BB + RSI у перепроданості + свічка ЗАКРИЛАСЬ
        назад у канал (розворотна) → LONG, ціль = середня смуга BB
  - Ціна проколола ВЕРХНЮ смугу BB + RSI у перекупленості + свічка ЗАКРИЛАСЬ
        назад у канал (розворотна) → SHORT, ціль = середня смуга BB

════════════════════════════════════════════════════════════════════
ЩО РЕАЛІЗОВАНО ЗАРАЗ (2026-07-08):
  1. Смуги Боллінджера (20, 2σ) + RSI-екстремум (пороги з .env).
  2. Фільтр ширини каналу (вузький канал → TP менший за комісії → skip).
  3. Опційний ADX-фільтр сили тренду.
  4. ⭐ ПІДТВЕРДЖЕННЯ РОЗВОРОТУ (reclaim): свічка мусить ЗАКРИТИСЬ назад
     усередину каналу і бути розворотною (бичача для LONG / ведмежа для
     SHORT). Це прибирає «ловлю ножів» — вхід, поки ціна ще провалюється.
  5. ⭐ HTF-ТРЕНДОВИЙ ФІЛЬТР (1h EMA20/EMA50 + нахил): НЕ купуємо проти
     сильного падіння і НЕ шортимо проти сильного росту. У боковику
     (тренду нема) — обидва напрямки дозволені.

ЧОМУ ЦІ ФІЛЬТРИ (з дослідження):
  Дослідження BB+RSI mean-reversion одностайне: БЕЗ фільтрів win-rate ≈45%
  (трендові дні дають катастрофічні серії збитків), З фільтрами (ADX +
  розворотна свічка + узгодження зі старшим ТФ) — 58-65%. Саме тому в нашій
  статистиці всі 3 LONG-и на перепроданості в падінні 07-08.07 програли:
  це були входи проти тренду без підтвердження розвороту.

ЩО ЩЕ МОЖНА ПОКРАЩИТИ (черга апгрейдів):
  • Лімітні (maker) входи прямо на смузі: −0.035% комісії + без сліпеджу
    → RR помітно вгору (потребує wait/cancel логіки у виконавці).
  • Ширший сигнальний ТФ (15m) — менше шуму, чистіші торкання смуг.
  • Time-stop: закривати угоду, якщо за N свічок ціна не пішла до mid.
  • RSI-«гачок» (curl-up) увімкнено за замовчуванням: RSI має вже почати
    розвертатись після недавнього екстремуму, а не просто бути в зоні.

Вхід: MARKET (на закритті сигнальної свічки). Сигнал-дикт сумісний з
generate_scalp_signal → live_trade._execute_trade працює без змін.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
from loguru import logger

from config.settings import SYMBOL_CONFIG
from indicators.range_detector import calculate_atr
from indicators.oscillators import rsi, bollinger_bands, adx, ema


def _validate(df: Optional[pd.DataFrame], need: int) -> bool:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False
    if len(df) < need:
        return False
    required = {"open", "high", "low", "close", "volume"}
    return required.issubset(df.columns)


def _rsi_reversal_hook(
    values: pd.Series,
    direction: str,
    threshold: float,
    lookback: int = 3,
    min_delta: float = 0.0,
) -> bool:
    """Підтверджує, що RSI вже повертається з недавнього екстремуму.

    Функція дивиться лише на передані (тобто вже закриті) бари. Для LONG
    у попередньому вікні мусив бути RSI <= oversold, а останній RSI має
    зрости щонайменше на ``min_delta``. Для SHORT умови дзеркальні.
    """
    lookback = max(1, int(lookback))
    clean = values.astype(float).dropna()
    if len(clean) < lookback + 1:
        return False

    prior = clean.iloc[-(lookback + 1):-1]
    current = float(clean.iloc[-1])
    previous = float(clean.iloc[-2])

    if direction == "long":
        touched_extreme = float(prior.min()) <= threshold
        return touched_extreme and current >= previous + min_delta
    if direction == "short":
        touched_extreme = float(prior.max()) >= threshold
        return touched_extreme and current <= previous - min_delta
    return False


def htf_trend_direction(
    dfs: dict,
    tf: str = "1h",
    ema_fast: int = 20,
    ema_slow: int = 50,
    slope_lb: int = 5,
) -> Optional[str]:
    """
    Напрямок тренду на старшому ТФ (за останньою свічкою).
      'up'   — EMA_fast > EMA_slow, ціна > EMA_slow, EMA_slow росте
      'down' — дзеркально
      None   — боковик / немає чіткого тренду (контр-тренд дозволено в обидва боки)

    Використовується як «anti-falling-knife» фільтр: не торгувати
    mean-reversion ПРОТИ сильного тренду старшого ТФ.
    """
    df = dfs.get(tf)
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < ema_slow + slope_lb + 2:
        return None

    close = df["close"].astype(float)
    ef = ema(close, ema_fast)
    es = ema(close, ema_slow)

    f = float(ef.iloc[-1])
    s = float(es.iloc[-1])
    s_prev = float(es.iloc[-1 - slope_lb])
    price = float(close.iloc[-1])

    if f > s and price > s and s > s_prev:
        return "up"
    if f < s and price < s and s < s_prev:
        return "down"
    return None


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
    use_adx = bool(mr.get("use_adx_filter", True))
    adx_max = float(mr.get("adx_max", 35))
    min_rr = float(mr.get("min_rr", 0.85))
    min_sl_pct = float(mr.get("min_sl_pct", 0.003))
    # ⭐ нові фільтри якості (2026-07-08)
    require_reclaim = bool(mr.get("require_reclaim", True))
    require_rsi_hook = bool(mr.get("require_rsi_hook", True))
    rsi_hook_lookback = max(1, int(mr.get("rsi_hook_lookback", 3)))
    # ``rsi_hook_min_delta`` лишаємо як сумісний alias для ранніх конфігів.
    rsi_hook_min_delta = max(
        0.0,
        float(mr.get("rsi_hook_delta", mr.get("rsi_hook_min_delta", 0.0))),
    )
    use_trend_filter = bool(mr.get("use_trend_filter", True))
    trend_filter_tf = str(mr.get("trend_filter_tf", "1h"))

    need = max(bb_period, rsi_period) + rsi_hook_lookback + 2
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

    # Один широкий бар, який проколов обидві смуги, не дає однозначного
    # reversion-напрямку. Старий ``if/elif`` довільно надавав перевагу LONG.
    if long_touch and short_touch:
        logger.debug(f"{symbol} [meanrev]: прокол обох BB-смуг — skip")
        return None

    if require_rsi and require_rsi_hook:
        long_rsi_ok = _rsi_reversal_hook(
            rsi_series, "long", rsi_os, rsi_hook_lookback, rsi_hook_min_delta
        )
        short_rsi_ok = _rsi_reversal_hook(
            rsi_series, "short", rsi_ob, rsi_hook_lookback, rsi_hook_min_delta
        )
    else:
        long_rsi_ok = rsi_val <= rsi_os
        short_rsi_ok = rsi_val >= rsi_ob

    direction = None
    if long_touch and (not require_rsi or long_rsi_ok):
        direction = "long"
    elif short_touch and (not require_rsi or short_rsi_ok):
        direction = "short"

    if direction is None:
        return None

    o = float(last["open"])

    # ── ⭐ Підтвердження розвороту (reclaim) ─────────────────────
    # Ключовий анти-«ніж»-фільтр: свічка має ЗАКРИТИСЬ назад у канал
    # (за смугу вона лише «проколола» тінню) І бути розворотною за
    # тілом. Без цього ми входимо, поки ціна ще провалюється далі.
    if require_reclaim:
        if direction == "long" and not (price > lower and price > o):
            logger.debug(f"{symbol} [meanrev]: LONG без reclaim у канал — skip (ніж)")
            return None
        if direction == "short" and not (price < upper and price < o):
            logger.debug(f"{symbol} [meanrev]: SHORT без reclaim у канал — skip (ніж)")
            return None

    # ── ⭐ HTF-трендовий фільтр (не торгувати проти сильного тренду) ─
    if use_trend_filter:
        trend = htf_trend_direction(dfs, tf=trend_filter_tf)
        if direction == "long" and trend == "down":
            logger.debug(f"{symbol} [meanrev]: LONG проти 1h-падіння — skip")
            return None
        if direction == "short" and trend == "up":
            logger.debug(f"{symbol} [meanrev]: SHORT проти 1h-росту — skip")
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
