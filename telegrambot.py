"""
TELEGRAM BOT — керування Bot_trade
=====================================
Запуск разом з трейдером:
  python telegrambot.py --demo
  python telegrambot.py --live

Або окремо (тільки моніторинг без торгівлі):
  python telegrambot.py --monitor

Потрібні змінні в .env:
  TELEGRAM_BOT_TOKEN=your_token
  TELEGRAM_ADMIN_ID=your_chat_id   ← тільки ти можеш керувати
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from typing import Any
import os as _os
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger
import pandas as pd

from config.settings import BYBIT_DEMO
from core.live_trade import LiveTrader
from config.settings import LOG_LEVEL, LOG_ROTATION, LOG_RETENTION

# ── для /diag (діагностика в Telegram) ────────────────────
from decimal import Decimal
from config.settings import (
    SYMBOL_CONFIG, STRATEGY_PRIORITY, DEPOSIT_USDT, RISK_PER_TRADE_PCT,
    USE_ORDER_BOOK_WALL_FILTER, USE_ORDER_BOOK_CONFIRMATION,
)
from signals.generator import generate_scalp_signal
from signals.calculator import calculate_position
from indicators.oscillators import bollinger_bands, rsi, vwap_bands, ema, adx
try:
    from signals.mean_reversion import generate_meanrev_signal
except Exception:
    generate_meanrev_signal = None
try:
    from signals.vwap_strategy import generate_vwap_signal
except Exception:
    generate_vwap_signal = None
try:
    from signals.dual_tf import generate_trend_signal
except Exception:
    generate_trend_signal = None

_DIAG_SYMBOL = "BTC/USDT:USDT"

_DIAG_BARS = max(12, int(float(_os.getenv("DIAG_HOURS", "4")) * 12))
# ── Конфіг ────────────────────────────────────────────────

def _load_tg_config() -> tuple[str, int]:
    """Завантажує токен і admin ID з .env."""
    import os
    from dotenv import load_dotenv

    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    admin_raw = os.getenv("TELEGRAM_ADMIN_ID", "0").strip()

    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не знайдено в .env")

    try:
        admin_id = int(admin_raw)
    except ValueError:
        raise RuntimeError(
            f"TELEGRAM_ADMIN_ID має бути додатним числом, отримано {admin_raw!r}"
        )

    if admin_id <= 0:
        raise RuntimeError(
            "TELEGRAM_ADMIN_ID не встановлено: керування торгівлею fail-closed"
        )

    return token, admin_id

# -------Логування---------

def setup_logging() -> None:
    """Ініціалізує логування в файл і stdout."""
    import os
    os.makedirs("logs", exist_ok=True)   # створює папку logs якщо нема
 
    logger.remove()
    logger.add(
        sys.stdout,
        level=LOG_LEVEL,
        format="<green>{time:HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{message}</cyan>",
        colorize=True,
    )
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
        rotation="00:00",
        retention=LOG_RETENTION,
        encoding="utf-8",
    )
    logger.info("📝 Логування ініціалізовано")
 
# ── Клавіатури ────────────────────────────────────────────

def kb_main() -> InlineKeyboardMarkup:
    """Головне меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Статус", callback_data="status"),
            InlineKeyboardButton(text="💰 P&L", callback_data="pnl"),
        ],
        [
            InlineKeyboardButton(text="📈 Позиція", callback_data="position"),
            InlineKeyboardButton(text="📡 Order Book", callback_data="orderbook"),
        ],
        [
            InlineKeyboardButton(text="🔬 Діагностика", callback_data="diag"),
        ],
        [
            InlineKeyboardButton(text="⏸ Пауза", callback_data="pause"),
            InlineKeyboardButton(text="▶️ Продовжити", callback_data="resume"),
        ],
        [
            InlineKeyboardButton(text="🛑 СТОП (екстрений)", callback_data="emergency_stop"),
        ],
    ])


def kb_confirm_stop() -> InlineKeyboardMarkup:
    """Підтвердження екстреного стопу."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Так, зупинити", callback_data="confirm_stop"),
            InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_stop"),
        ],
    ])


def kb_back() -> InlineKeyboardMarkup:
    """Кнопка назад до головного меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Головне меню", callback_data="main_menu")],
    ])


# ── Форматування повідомлень ──────────────────────────────

def fmt_num(value: Any, digits: int = 2, signed: bool = False) -> str:
    """
    Безпечне форматування чисел для Telegram.
    Не падає, якщо value=None або не число.
    """
    if value is None:
        return "n/a"

    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"

    if signed:
        return f"{number:+.{digits}f}"

    return f"{number:.{digits}f}"


def fmt_bool(value: Any) -> str:
    """Форматування optional-фільтрів: CVD / Volume / OB."""
    if value is True:
        return "✅"
    if value is False:
        return "❌"
    return "➖"


def fmt_ob(value: Any) -> str:
    """OB може бути None, якщо order book став optional."""
    if value is None:
        return "n/a"

    formatted = fmt_num(value, digits=1, signed=True)
    if formatted == "n/a":
        return "n/a"

    return f"{formatted}%"


def fmt_price(value: Any, digits: int = 4) -> str:
    """Ціна з безпечним fallback."""
    return fmt_num(value, digits=digits)


def fmt_pct_distance(target: Any, entry: Any) -> str:
    """Відстань від entry до TP/SL у відсотках."""
    try:
        target_f = float(target)
        entry_f = float(entry)
        if entry_f == 0:
            return "n/a"
        return f"{abs(target_f - entry_f) / entry_f * 100:.2f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return "n/a"


def fmt_filters(t: dict) -> list[str]:
    """
    Рядки 'фільтрів входу' залежно від стратегії угоди.
    Для sweep — OF/CVD/Volume/OB. Для meanrev — RSI/BB. Для vwap — VWAP/RSI.
    Так звіт не показує 'n/a' там, де фільтр просто не застосовується.
    """
    strat = (t.get("strategy") or t.get("mode") or "").lower()
    if strat == "meanrev":
        lines = [
            f"  RSI: `{fmt_num(t.get('rsi'), 1)}`",
            f"  BB %b: `{fmt_num(t.get('bb_percent_b'), 2)}` | ширина: `{fmt_num(t.get('bb_width_pct'), 2)}%`",
        ]
    elif strat == "vwap":
        lines = [
            f"  VWAP дев.: `{fmt_num(t.get('vwap_dev_pct'), 2, signed=True)}%`",
            f"  RSI: `{fmt_num(t.get('rsi'), 1)}`",
        ]
    else:
        lines = [
            f"  OF delta: `{fmt_num(t.get('of_delta'), 0, signed=True)}`",
            f"  CVD: `{t.get('cvd_signal', 'n/a')}` | ok: {fmt_bool(t.get('cvd_ok'))}",
            f"  Volume ok: {fmt_bool(t.get('volume_ok'))}",
            f"  OB: `{fmt_ob(t.get('ob_imbalance'))}` | confirmed: {fmt_bool(t.get('ob_confirmed'))}",
        ]

    # FTA — проблемна зона старшого ТФ (показуємо, лише якщо порахована)
    fta = t.get("fta_price")
    if fta is not None:
        flag = "⚠️ TP за зоною" if t.get("fta_blocks_tp") else "✅ вільно до TP"
        lines.append(
            f"  FTA(HTF): `{fmt_num(fta, 1)}` ({fmt_num(t.get('fta_dist_pct'), 2)}%) — {flag}"
        )
    return lines


def fmt_trader_state(trader: LiveTrader) -> str:
    """
    Коректний стан:
    - _running False → не запущений / зупинений
    - _paused.clear() → пауза
    - _paused.set() → активний
    """
    if not getattr(trader, "_running", False):
        return "⏹ Не запущений"

    latch = getattr(trader.state, "safety_latch_reason", None)
    if latch:
        return f"🆘 Safety latch: {latch}"

    paused_event = getattr(trader, "_paused", None)
    if paused_event is not None and not paused_event.is_set():
        return "⏸ На паузі"

    return "✅ Активний"


def fmt_status(trader: LiveTrader) -> str:
    s = trader.state
    mode = "🟡 DEMO" if BYBIT_DEMO else "🔴 LIVE"
    running = fmt_trader_state(trader)

    recent = list(getattr(s, "recent_outcomes", []))[-10:]
    recent_wr = f"{sum(recent) / len(recent) * 100:.1f}%" if recent else "н/д"

    lines = [
        "*📊 СТАТУС ТРЕЙДЕРА*",
        "",
        f"Режим: {mode}",
        f"Стан: {running}",
        "",
        f"💵 Equity: `${fmt_num(s.equity, 2)}`",
        f"📅 Денний P&L: `${fmt_num(s.daily_pnl, 2, signed=True)}`",
        f"🔢 Угод сьогодні: `{s.daily_trades}`",
        f"📉 Loss streak: `{s.loss_streak}`",
        f"🎯 Win-rate останніх `{len(recent)}`: `{recent_wr}`",
    ]

    if s.open_trade:
        t = s.open_trade
        dur = datetime.now(timezone.utc) - t["opened_at"]
        mins = int(dur.total_seconds() / 60)

        lines += [
            "",
            (
                "*⏳ ПОЗИЦІЯ ЗАКРИТА — ОЧІКУЮ CLOSED-PNL:*"
                if t.get("close_pending_since") else "*📈 ВІДКРИТА ПОЗИЦІЯ:*"
            ),
            f"  {t['direction'].upper()} `{t['symbol']}`",
            f"  Entry: `{fmt_price(t.get('entry'), 2)}`",
            f"  TP: `{fmt_price(t.get('tp'), 2)}` | SL: `{fmt_price(t.get('sl'), 2)}`",
            f"  R:R: `{fmt_num(t.get('raw_rr'), 2)}`",
            f"  Ризик: `${fmt_num(t.get('risk_usdt'), 2)}` → Мета: `${fmt_num(t.get('reward_usdt'), 2)}`",
            "",
            f"*Стратегія: `{t.get('strategy', t.get('mode', '?'))}`*",
            "*Фільтри входу:*",
            *fmt_filters(t),
            "",
            f"  Відкрита: `{mins} хв тому`",
        ]
    else:
        lines.append("\n_Відкритих позицій немає_")

    lines.append(
        f"\n_Оновлено: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC_"
    )

    return "\n".join(lines)


def fmt_pnl(trader: LiveTrader) -> str:
    s = trader.state
    deposit = float(s.deposit)
    equity = float(s.equity)
    daily = float(s.daily_pnl)
    total = equity - deposit
    total_pct = (total / deposit * 100) if deposit > 0 else 0
    daily_pct = (daily / deposit * 100) if deposit > 0 else 0
    recent = list(getattr(s, "recent_outcomes", []))[-10:]
    recent_wr = f"{sum(recent) / len(recent) * 100:.1f}%" if recent else "н/д"

    emoji_total = "📈" if total >= 0 else "📉"
    emoji_daily = "✅" if daily >= 0 else "❌"

    return "\n".join([
        "*💰 P&L ЗВІТ*",
        "",
        f"Депозит: `${deposit:.2f}`",
        f"Equity:  `${equity:.2f}`",
        "",
        f"{emoji_total} Загальний: `${total:+.2f}` ({total_pct:+.2f}%)",
        f"{emoji_daily} Сьогодні:  `${daily:+.2f}` ({daily_pct:+.2f}%)",
        "",
        f"Угод сьогодні: `{s.daily_trades}`",
        f"Loss streak:   `{s.loss_streak}`",
        f"Win-rate {len(recent)}: `{recent_wr}`",
        "",
        f"_Оновлено: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC_",
    ])


def fmt_position(trader: LiveTrader) -> str:
    s = trader.state

    if not s.open_trade:
        return "📈 *ПОЗИЦІЯ*\n\n_Зараз немає відкритих позицій_"

    t = s.open_trade
    dur = datetime.now(timezone.utc) - t["opened_at"]
    mins = int(dur.total_seconds() / 60)

    direction_emoji = "🟢 LONG" if t["direction"] == "long" else "🔴 SHORT"

    return "\n".join([
        "*📈 ВІДКРИТА ПОЗИЦІЯ*",
        "",
        f"{direction_emoji} `{t['symbol']}`",
        "",
        f"Entry:  `{fmt_price(t.get('entry'), 4)}`",
        f"TP:     `{fmt_price(t.get('tp'), 4)}` (+{fmt_pct_distance(t.get('tp'), t.get('entry'))})",
        f"SL:     `{fmt_price(t.get('sl'), 4)}` (-{fmt_pct_distance(t.get('sl'), t.get('entry'))})",
        f"R:R:    `{fmt_num(t.get('raw_rr'), 2)}`",
        "",
        f"Розмір: `{t.get('qty', 'n/a')}`",
        f"Ризик:  `${fmt_num(t.get('risk_usdt'), 2)}`",
        f"Мета:   `${fmt_num(t.get('reward_usdt'), 2)}`",
        "",
        f"*Стратегія: `{t.get('strategy', t.get('mode', '?'))}`*",
        "*Фільтри входу:*",
        *fmt_filters(t),
        "",
        f"Відкрита: `{mins} хв тому`",
        f"ID: `{t.get('order_id', '?')}`",
    ])


def fmt_orderbook(trader: LiveTrader) -> str:
    s = trader.state

    if not s.ob_snapshots:
        return "📡 *ORDER BOOK*\n\n_Дані ще не завантажені (зачекай 5–10 сек)_"

    lines = ["*📡 ORDER BOOK*", ""]

    for symbol, ob in s.ob_snapshots.items():
        age = (datetime.now(timezone.utc) - ob.timestamp).total_seconds()
        age_str = f"{age:.1f}с тому"

        imbalance = float(getattr(ob, "imbalance", 0.0))
        imb_emoji = "🟢" if imbalance > 20 else ("🔴" if imbalance < -20 else "⚪️")

        lines += [
            f"*{symbol}* _{age_str}_",
            f"  {imb_emoji} Дисбаланс: `{fmt_ob(imbalance)}`",
            f"  Bid: `{fmt_num(getattr(ob, 'bid_total', 0), 2)}` | Ask: `{fmt_num(getattr(ob, 'ask_total', 0), 2)}`",
        ]

        if getattr(ob, "bid_walls", None):
            walls = ", ".join(f"${p:.0f}" for p, _ in ob.bid_walls[:3])
            lines.append(f"  🟩 Bid стіни: {walls}")

        if getattr(ob, "ask_walls", None):
            walls = ", ".join(f"${p:.0f}" for p, _ in ob.ask_walls[:3])
            lines.append(f"  🟥 Ask стіни: {walls}")

        lines.append("")

    return "\n".join(lines)


# ── Діагностика (та сама логіка, що diagnose.py, але на ЖИВИХ ─────
#    свічках бота — показує рівно те, що «бачить» трейдер) ──────────

def _diag_calc(sig):
    return calculate_position(
        symbol=_DIAG_SYMBOL, deposit=Decimal(str(DEPOSIT_USDT)),
        risk_pct=Decimal(str(RISK_PER_TRADE_PCT)),
        entry_price=Decimal(str(sig["entry"])),
        stop_loss=Decimal(str(sig["sl"])),
        take_profit=Decimal(str(sig["tp"])),
        min_rr=Decimal(str(sig["min_rr"])) if sig.get("min_rr") is not None else None,
    )


def _diag_run_all(dfs):
    res = {}
    if generate_trend_signal:
        try: res["trend"] = generate_trend_signal(dfs, _DIAG_SYMBOL)
        except Exception: res["trend"] = None
    if generate_vwap_signal:
        try: res["vwap"] = generate_vwap_signal(dfs, _DIAG_SYMBOL)
        except Exception: res["vwap"] = None
    if generate_meanrev_signal:
        try: res["meanrev"] = generate_meanrev_signal(dfs, _DIAG_SYMBOL)
        except Exception: res["meanrev"] = None
    try:
        res["sweep"] = generate_scalp_signal(df_1h=dfs.get("1h"), df_5m=dfs.get("5m"),
                                              df_1m=dfs.get("1m"), symbol=_DIAG_SYMBOL,
                                              cached_range=None, mode="A")
    except Exception:
        res["sweep"] = None
    return res


def fmt_diagnostic(trader: LiveTrader, scan_bars: int = _DIAG_BARS) -> str:
    """
    Знімок індикаторів проти порогів + короткий реплей історії на ЖИВИХ
    свічках бота. Викликається з Telegram (/diag або кнопка).
    ВАЖЛИВО: синхронна і трохи важка (реплей) — виклик робимо в executor,
    щоб не блокувати торговий цикл.
    """
    get = trader._get_df
    dfs = {
        tf: get(_DIAG_SYMBOL, tf, closed_only=True)
        for tf in trader._required_timeframes()
    }
    df5 = dfs.get("5m")
    if df5 is None or len(df5) < 30:
        return "*🔬 ДІАГНОСТИКА*\n\n_Дані ще вантажаться (5m свічок мало). Зачекай кілька хв._"

    cfg = SYMBOL_CONFIG.get(_DIAG_SYMBOL, {})
    price = float(df5["close"].iloc[-1])
    L = ["*🔬 ДІАГНОСТИКА*", f"BTC = `{price:.1f}`  |  пріоритет `{','.join(STRATEGY_PRIORITY)}`", ""]

    # ── Свіжість даних (КРИТИЧНО: ціна вище — з буферів бота; якщо вони
    #    замерзли, все нижче — про СТАРИЙ ринок) ─────────────────────────
    def _age_str(sec: float) -> str:
        if sec < 120:
            return f"{int(sec)}с"
        if sec < 7200:
            return f"{int(sec/60)}хв"
        return f"{sec/3600:.1f}год"

    now_ts = datetime.now(timezone.utc).timestamp()

    # LIVE-потік: епоха останнього WS-пуша поточної свічки (здорово ≤3с)
    ws_at = getattr(trader.state, "ws_msg_at", {}) or {}
    live_ts = ws_at.get(f"{_DIAG_SYMBOL}_1m")
    live_age = None if live_ts is None else max(0.0, now_ts - live_ts)
    if live_age is not None and live_age <= 30:
        L.append(f"live-потік: `{_age_str(live_age)}` ✅ (ціна вище — реального часу)")
        live_dead = False
    else:
        L.append(f"live-потік: `{'нема' if live_age is None else _age_str(live_age)}` ❌")
        live_dead = True

    # Закриті свічки (вік від СТАРТУ останньої закритої: 1m здорово 60-125с)
    limits = {"1m": 240, "5m": 900, "1h": 7500}
    ages, closed_stale = [], False
    for tf in ["1m", "5m", "1h"]:
        d = dfs.get(tf)
        if d is None or len(d) == 0:
            ages.append(f"{tf} `нема` ❌")
            closed_stale = True
            continue
        sec = max(0.0, now_ts - d.index[-1].timestamp())
        bad = sec > limits[tf]
        closed_stale = closed_stale or bad
        ages.append(f"{tf} `{_age_str(sec)}`" + (" ❌" if bad else ""))
    L.append("закриті свічки: " + " | ".join(ages))

    if live_dead and closed_stale:
        L += ["", "🛑 *ДАНІ ЗАМЕРЗЛИ — бот бачить СТАРУ ціну!*",
              "_WS мертвий або бот давно не перезапускався._",
              "_Перезапусти бота і перевір /diag ще раз._", ""]
    elif live_dead:
        L += ["", "🩹 _live-потік мовчить — працюю по закритих свічках (REST)._", ""]
    else:
        L.append("")

    # ── Стан WS-потоків: ТОЧНА причина, якщо щось не підключається ──
    ws_status = getattr(trader.state, "ws_status", {}) or {}
    if ws_status:
        L.append("*WS-потоки (conn/msgs):*")
        for k in sorted(ws_status):
            st = ws_status[k]
            name = k.split("_")[-1]
            icon = "🟢" if st.get("connected") else "🔴"
            L.append(f"  {icon} `{name:<3}` {st.get('connects', 0)}/{st.get('msgs', 0)}")
            if not st.get("connected") and st.get("last_error"):
                L.append(f"      ⚠ `{st['last_error'][:90]}` о {st.get('last_error_at', '?')}")
        L.append("")
    else:
        L += ["*WS-потоки:* `порожньо` — жоден потік ще не стартував ❗", ""]

    # ── ВИКОНАННЯ: де вмирають сигнали (лічильники з моменту старту) ──
    # Саме ця секція відповідає на «сетапи є, а угод нема — що міша?»
    paused_ev = getattr(trader, "_paused", None)
    is_paused = paused_ev is not None and not paused_ev.is_set()
    L.append(
        f"*Виконання:* пауза: {'⏸ ТАК ← УВІМКНИ ▶️!' if is_paused else 'ні'} | "
        f"фільтр стіни: `{'ON' if USE_ORDER_BOOK_WALL_FILTER else 'off'}` | "
        f"OB-напрям: `{'ON' if USE_ORDER_BOOK_CONFIRMATION else 'off'}`"
    )
    es = getattr(trader.state, "exec_stats", None)
    if es:
        L.append(
            f"  сигналів `{es.get('signals', 0)}` → зарізано: "
            f"стіна `{es.get('wall_blocked', 0)}` · OB-напрям `{es.get('obdir_blocked', 0)}` · "
            f"flow `{es.get('flow_blocked', 0)}` · dedup `{es.get('dedup_blocked', 0)}` · "
            f"калькулятор `{es.get('calc_rejected', 0)}`"
        )
        L.append(
            f"  надіслано на біржу `{es.get('sent', 0)}` → відхилено `{es.get('exchange_rejected', 0)}` "
            f"→ *відкрито `{es.get('opened', 0)}`*"
        )
        if es.get("last_reject"):
            L.append(f"  ⚠ остання відмова біржі: `{str(es['last_reject'])[:90]}`")
        execution = es.get("last_execution")
        if execution:
            slip = execution.get("slippage_signal_bps")
            maker_pct = float(execution.get("maker_pct") or 0.0) * 100
            slip_text = f"{float(slip):+.2f} bps" if slip is not None else "н/д"
            L.append(
                f"  останнє виконання: `{execution.get('filled_qty', 0)}` / "
                f"`{execution.get('requested_qty', 0)}` @ `{float(execution.get('entry_vwap') or 0):.2f}` · "
                f"slippage `{slip_text}` · maker `{maker_pct:.0f}%`"
            )
    L.append("")

    mr = cfg.get("meanrev", {})
    if mr.get("enabled"):
        bb = bollinger_bands(df5["close"], int(mr.get("bb_period", 20)), float(mr.get("bb_std", 2.0)))
        rv = float(rsi(df5["close"], int(mr.get("rsi_period", 14))).iloc[-1])
        ok = "✅" if bb["width_pct"] >= float(mr.get("min_width_pct", 0)) else "❌"
        L.append(f"*MEANREV* {ok}  BB `{bb['width_pct']:.2f}%`≥{mr.get('min_width_pct')} "
                 f"%b `{bb['percent_b']:.2f}` RSI `{rv:.0f}`")

    vw = cfg.get("vwap", {})
    if vw.get("enabled"):
        win = vw.get("window", 96); win = int(win) if win else None
        vw_mode = str(vw.get("mode", "session")).lower()
        vb = vwap_bands(
            df5,
            window=win if vw_mode == "rolling" else None,
            k=float(vw.get("k_band", 2.0)),
            anchor="session" if vw_mode == "session" else None,
        )
        ok = "✅" if abs(vb["dev_pct"]) >= float(vw.get("min_dev_pct", 0)) else "❌"
        L.append(f"*VWAP {vw_mode}* {ok}  дев `{vb['dev_pct']:+.2f}%`≥{vw.get('min_dev_pct')} "
                 f"смуги `{vb['lower']:.0f}..{vb['upper']:.0f}`")

    tc = cfg.get("trend", {})
    d1 = dfs.get("1h")
    if tc.get("enabled") and d1 is not None and len(d1) > 210:
        c = d1["close"]
        ef, em, es = ema(c, 20).iloc[-1], ema(c, 50).iloc[-1], ema(c, 200).iloc[-1]
        av = float(adx(d1, 14).iloc[-1])
        gate = ("LONG" if (ef > em > es and c.iloc[-1] > em)
                else "SHORT" if (ef < em < es and c.iloc[-1] < em) else "боковик")
        ok = "✅" if av >= float(tc.get("adx_min", 20)) else "❌"
        L.append(f"*TREND* ADX `{av:.0f}`≥{tc.get('adx_min')} {ok}  gate `{gate}`")

    # спрацьовує зараз?
    now = _diag_run_all(dfs)
    fired_now = [n for n, s in now.items() if s is not None]
    L += ["", f"*зараз дають сигнал:* {', '.join(fired_now) if fired_now else '— нема'}"]

    # короткий реплей історії
    n = min(scan_bars, len(df5) - 10)
    fired, execd = {}, {}
    if n > 20:
        for cutoff in df5.index[-n:]:
            decision = cutoff + pd.Timedelta(minutes=5)
            durations = {
                "1m": pd.Timedelta(minutes=1), "5m": pd.Timedelta(minutes=5),
                "15m": pd.Timedelta(minutes=15), "30m": pd.Timedelta(minutes=30),
                "1h": pd.Timedelta(hours=1),
            }
            sub = {
                tf: (
                    None if d is None else
                    d.loc[(d.index + durations.get(tf, pd.Timedelta(0))) <= decision]
                )
                for tf, d in dfs.items()
            }
            for name, sig in _diag_run_all(sub).items():
                if sig is None:
                    continue
                fired[name] = fired.get(name, 0) + 1
                pos = _diag_calc(sig)
                if "error" not in pos and pos.get("rr_ok"):
                    execd[name] = execd.get(name, 0) + 1
        L += ["", f"*каузальний scan ~{n*5/60:.0f} год (сигнали / net-RR):*"]
        for name in ["trend", "vwap", "meanrev", "sweep"]:
            L.append(f"  `{name:<8}` {fired.get(name,0):>3} / {execd.get(name,0):>3}")
        total = sum(execd.values())
        if total > 0:
            L += ["", f"_Net-RR пройшли ~{total} кандидатів; це не backtest/win-rate._",
                  "_(фільтр стіни / помилка ордера / бот не перезапущено)._"]
        else:
            L += ["", "_Сетапів мало — тихий ринок або жорсткі пороги._"]

    return "\n".join(L)


# ── Telegram Bot ──────────────────────────────────────────

class TradingBot:
    def __init__(self, token: str, admin_id: int, trader: LiveTrader):
        self.bot = Bot(token=token, default=DefaultBotProperties(parse_mode="Markdown"))
        self.dp = Dispatcher()
        self.admin_id = admin_id
        self.trader = trader

        self._register_handlers()

    def _is_admin(self, user_id: int) -> bool:
        """Перевіряє чи є користувач адміном."""
        return user_id == self.admin_id

    def _register_handlers(self) -> None:
        dp = self.dp

        # ── Команди ───────────────────────────────────────
        dp.message.register(self.cmd_start, Command("start"))
        dp.message.register(self.cmd_menu, Command("menu"))
        dp.message.register(self.cmd_status, Command("status"))
        dp.message.register(self.cmd_diag, Command("diag"))
        dp.message.register(self.cmd_stop, Command("stop"))

        # ── Inline кнопки ─────────────────────────────────
        dp.callback_query.register(self.cb_status, F.data == "status")
        dp.callback_query.register(self.cb_pnl, F.data == "pnl")
        dp.callback_query.register(self.cb_position, F.data == "position")
        dp.callback_query.register(self.cb_orderbook, F.data == "orderbook")
        dp.callback_query.register(self.cb_diag, F.data == "diag")
        dp.callback_query.register(self.cb_pause, F.data == "pause")
        dp.callback_query.register(self.cb_resume, F.data == "resume")
        dp.callback_query.register(self.cb_emg_stop, F.data == "emergency_stop")
        dp.callback_query.register(self.cb_confirm_stop, F.data == "confirm_stop")
        dp.callback_query.register(self.cb_cancel_stop, F.data == "cancel_stop")
        dp.callback_query.register(self.cb_main_menu, F.data == "main_menu")

    # ── Команди ───────────────────────────────────────────

    async def cmd_start(self, msg: Message) -> None:
        if not self._is_admin(msg.from_user.id):
            await msg.answer("⛔ Доступ заборонено")
            return

        mode = "🟡 DEMO" if BYBIT_DEMO else "🔴 LIVE"
        await msg.answer(
            f"*🤖 Bot\\_trade запущено*\n\n"
            f"Режим: {mode}\n"
            f"Використовуй кнопки нижче для керування:\n",
            reply_markup=kb_main(),
        )

    async def cmd_menu(self, msg: Message) -> None:
        if not self._is_admin(msg.from_user.id):
            return
        await msg.answer("*📋 Головне меню*", reply_markup=kb_main())

    async def cmd_status(self, msg: Message) -> None:
        if not self._is_admin(msg.from_user.id):
            return
        await msg.answer(fmt_status(self.trader), reply_markup=kb_back())

    async def cmd_diag(self, msg: Message) -> None:
        if not self._is_admin(msg.from_user.id):
            return
        await msg.answer("🔬 Рахую діагностику (реплей історії)...")
        text = await self._diagnostic_text()
        await msg.answer(text, reply_markup=kb_back())

    async def cmd_stop(self, msg: Message) -> None:
        if not self._is_admin(msg.from_user.id):
            return
        await msg.answer(
            "⚠️ *Екстрений стоп*\nЗупинити бота і закрити всі позиції?",
            reply_markup=kb_confirm_stop(),
        )

    # ── Callback handlers ─────────────────────────────────

    async def cb_status(self, cb: CallbackQuery) -> None:
        if not self._is_admin(cb.from_user.id):
            await cb.answer("⛔ Доступ заборонено", show_alert=True)
            return
        await self._edit(cb, fmt_status(self.trader), kb_back())
        await cb.answer()

    async def cb_pnl(self, cb: CallbackQuery) -> None:
        if not self._is_admin(cb.from_user.id):
            await cb.answer("⛔", show_alert=True)
            return
        await self._edit(cb, fmt_pnl(self.trader), kb_back())
        await cb.answer()

    async def cb_position(self, cb: CallbackQuery) -> None:
        if not self._is_admin(cb.from_user.id):
            await cb.answer("⛔", show_alert=True)
            return
        await self._edit(cb, fmt_position(self.trader), kb_back())
        await cb.answer()

    async def cb_diag(self, cb: CallbackQuery) -> None:
        if not self._is_admin(cb.from_user.id):
            await cb.answer("⛔", show_alert=True)
            return
        await cb.answer("🔬 Рахую... (кілька сек)")
        text = await self._diagnostic_text()
        await self._edit(cb, text, kb_back())

    async def cb_orderbook(self, cb: CallbackQuery) -> None:
        if not self._is_admin(cb.from_user.id):
            await cb.answer("⛔", show_alert=True)
            return
        await self._edit(cb, fmt_orderbook(self.trader), kb_back())
        await cb.answer()

    async def cb_pause(self, cb: CallbackQuery) -> None:
        if not self._is_admin(cb.from_user.id):
            await cb.answer("⛔", show_alert=True)
            return

        if not self.trader._paused.is_set():
            await cb.answer("⏸ Вже на паузі", show_alert=True)
            return

        self.trader._paused.clear()
        logger.warning("⏸ Бот поставлено на паузу через Telegram")
        await cb.answer("⏸ Пауза активована", show_alert=True)
        await self._edit(cb, fmt_status(self.trader), kb_main())

    async def cb_resume(self, cb: CallbackQuery) -> None:
        if not self._is_admin(cb.from_user.id):
            await cb.answer("⛔", show_alert=True)
            return

        if not self.trader._running:
            await cb.answer(
                "🛑 Трейдер зупинений; потрібен restart сервісу",
                show_alert=True,
            )
            return

        latch = getattr(self.trader.state, "safety_latch_reason", None)
        if latch:
            await cb.answer(
                "🆘 Safety latch не можна зняти кнопкою. Перевір Bybit і "
                "перезапусти сервіс.",
                show_alert=True,
            )
            return

        if self.trader._paused.is_set():
            await cb.answer("▶️ Вже активний", show_alert=True)
            return

        self.trader._paused.set()
        logger.info("▶️ Бот відновлено через Telegram")
        await cb.answer("▶️ Торгівлю відновлено!", show_alert=True)
        await self._edit(cb, fmt_status(self.trader), kb_main())

    async def cb_emg_stop(self, cb: CallbackQuery) -> None:
        if not self._is_admin(cb.from_user.id):
            await cb.answer("⛔", show_alert=True)
            return

        await self._edit(
            cb,
            "🛑 *ЕКСТРЕНИЙ СТОП*\n\n"
            "Зупинити бота і закрити всі позиції?\n\n"
            "⚠️ Ця дія незворотна!",
            kb_confirm_stop(),
        )
        await cb.answer()

    async def cb_confirm_stop(self, cb: CallbackQuery) -> None:
        if not self._is_admin(cb.from_user.id):
            await cb.answer("⛔", show_alert=True)
            return

        self.trader.stop()

        # Дочікуємось завершення in-flight entry lifecycle. Інакше market
        # entry може підтвердитись уже після нашої фінальної flat-перевірки.
        async with self.trader._execution_lock:
            pass

        # Джерело істини — біржа, не in-memory state: невідомий submit або
        # restart могли залишити реальну позицію, якої state ще не бачить.
        try:
            from config.settings import TRADING_PAIRS

            closed_symbols = []
            close_errors = []
            async def _active_positions():
                rows = await asyncio.to_thread(
                    self.trader.rest.fetch_positions, TRADING_PAIRS
                )
                return [
                    p for p in rows if float(p.get("contracts", 0) or 0) > 0
                ]

            async def _close(rows):
                for p in rows:
                    symbol = p.get("symbol")
                    qty = float(p.get("contracts", 0) or 0)
                    pside = str(p.get("side") or "").lower()
                    side = "sell" if pside in {"long", "buy"} else "buy"
                    try:
                        await asyncio.to_thread(
                            self.trader.rest.create_order,
                            symbol=symbol,
                            type="market",
                            side=side,
                            amount=qty,
                            params={"reduceOnly": True, "positionIdx": 0},
                        )
                        closed_symbols.append(str(symbol))
                    except Exception as e:
                        close_errors.append(f"{symbol}: {e}")

            # Close visible positions first, then cancel every resting/conditional
            # order. Repeat once to catch an in-flight entry whose response was lost.
            await _close(await _active_positions())
            cancel_errors = []
            for symbol in TRADING_PAIRS:
                try:
                    await asyncio.to_thread(self.trader.rest.cancel_all_orders, symbol)
                except Exception as e:
                    cancel_errors.append(f"{symbol}: {e}")
            await asyncio.sleep(1.0)
            await _close(await _active_positions())
            for symbol in TRADING_PAIRS:
                try:
                    await asyncio.to_thread(self.trader.rest.cancel_all_orders, symbol)
                except Exception as e:
                    cancel_errors.append(f"{symbol}: {e}")

            await asyncio.sleep(0.5)
            remaining = await _active_positions()
            open_orders = []
            for symbol in TRADING_PAIRS:
                open_orders.extend(
                    await asyncio.to_thread(self.trader.rest.fetch_open_orders, symbol)
                )
            if remaining or open_orders or cancel_errors or close_errors:
                details = []
                if remaining:
                    details.append(
                        "позиції=" + ",".join(str(p.get("symbol")) for p in remaining)
                    )
                if open_orders:
                    details.append(f"open_orders={len(open_orders)}")
                if cancel_errors:
                    details.append("cancel_errors=" + " | ".join(cancel_errors))
                if close_errors:
                    details.append("close_errors=" + " | ".join(close_errors))
                raise RuntimeError("; ".join(details))

            closed_text = (
                "✅ Закрито: " + ", ".join(f"`{s}`" for s in closed_symbols)
                if closed_symbols else "✅ Відкритих позицій на біржі не було"
            )
            await self._edit(
                cb,
                "🛑 *СТОП ВИКОНАНО*\n\n"
                f"{closed_text}\n"
                "✅ Бот зупинено\n\n"
                "Для перезапуску: `python telegrambot.py --demo`",
                None,
            )
        except Exception as e:
            await self._edit(
                cb,
                f"🛑 Бот зупинено\n"
                f"⚠️ Не підтверджено закриття всіх позицій: `{e}`\n\n"
                "Перевір і закрий вручну на Bybit!",
                None,
            )

        await cb.answer("🛑 Стоп виконано", show_alert=True)

    async def cb_cancel_stop(self, cb: CallbackQuery) -> None:
        await self._edit(cb, fmt_status(self.trader), kb_main())
        await cb.answer("✅ Скасовано")

    async def cb_main_menu(self, cb: CallbackQuery) -> None:
        await self._edit(cb, "*📋 Головне меню*", kb_main())
        await cb.answer()

    # ── Авто-сповіщення ───────────────────────────────────

    async def send_alert(self, text: str) -> None:
        """
        Відправляє сповіщення адміну. Стійке до:
          • спецсимволів Markdown → фолбек на plain text;
          • тимчасових мережевих збоїв Telegram → до 3 спроб із паузою.
        Помилку логуємо ГУЧНО (з admin_id), щоб «сповіщення не приходять»
        було видно в journalctl, а не гасилось тихо.
        """
        if self.admin_id == 0:
            logger.warning("send_alert: TELEGRAM_ADMIN_ID=0 — нема кому слати алерт!")
            return

        for attempt in range(1, 4):
            try:
                await self.bot.send_message(self.admin_id, text, parse_mode="Markdown")
                return
            except TelegramBadRequest as e:
                # Markdown зламався через спецсимволи — шлемо plain text.
                logger.warning(f"Telegram Markdown помилка, plain text: {e}")
                try:
                    await self.bot.send_message(self.admin_id, text, parse_mode=None)
                    return
                except Exception as e2:
                    logger.error(f"send_alert plain-text теж впав: {e2}")
                    return
            except Exception as e:
                logger.error(
                    f"send_alert спроба {attempt}/3 до admin_id={self.admin_id} "
                    f"впала: {type(e).__name__}: {e}"
                )
                if attempt < 3:
                    await asyncio.sleep(2 * attempt)

        logger.critical(
            f"🆘 send_alert: НЕ вдалось доставити алерт адміну {self.admin_id} "
            f"після 3 спроб — перевір токен/мережу/чи натиснув /start у бота"
        )

    async def notify_trade_opened(self, signal: dict) -> None:
        direction_emoji = "🟢" if signal["direction"] == "long" else "🔴"
        strat = signal.get("strategy", signal.get("mode", "unknown"))
        filters = "\n".join(line.strip() for line in fmt_filters(signal))

        await self.send_alert(
            f"{direction_emoji} *ВІДКРИТО УГОДУ*\n\n"
            f"`{signal['symbol']}` {signal['direction'].upper()}\n"
            f"Стратегія: `{strat}`\n\n"
            f"Entry: `{fmt_price(signal.get('entry'), 4)}`\n"
            f"TP:    `{fmt_price(signal.get('tp'), 4)}`\n"
            f"SL:    `{fmt_price(signal.get('sl'), 4)}`\n"
            f"R:R:   `{fmt_num(signal.get('raw_rr'), 2)}`\n\n"
            f"*Фільтри:*\n{filters}"
        )

    async def notify_trade_closed(self, pnl: float, trade: dict) -> None:
        emoji = "✅ PROFIT" if pnl > 0 else "❌ LOSS" if pnl < 0 else "➖ BREAKEVEN"
        await self.send_alert(
            f"{emoji}\n\n"
            f"`{trade['symbol']}` {trade['direction'].upper()}\n"
            f"P&L: `${fmt_num(pnl, 2, signed=True)}`\n"
            f"Entry: `{fmt_price(trade.get('entry'), 4)}`\n"
            f"Equity: `${fmt_num(self.trader.state.equity, 2)}`"
        )

    async def notify_daily_limit(self, reason: str) -> None:
        await self.send_alert(f"🚨 *ДЕННИЙ ЛІМІТ*\n\n{reason}")

    # ── Допоміжні ─────────────────────────────────────────

    async def _edit(self, cb: CallbackQuery, text: str, markup) -> None:
        """Редагує повідомлення або відправляє нове, якщо редагування не вдалося."""
        try:
            await cb.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return

            logger.warning(f"Telegram edit Markdown помилка, пробую plain text: {e}")
            try:
                await cb.message.edit_text(text, reply_markup=markup, parse_mode=None)
            except Exception:
                await cb.message.answer(text, reply_markup=markup, parse_mode=None)
        except Exception:
            await cb.message.answer(text, reply_markup=markup, parse_mode=None)

    async def _diagnostic_text(self) -> str:
        """
        Рахує fmt_diagnostic у thread-executor, щоб важкий реплей не блокував
        asyncio-цикл (а отже і торгівлю).
        """
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, fmt_diagnostic, self.trader)
        except Exception as e:
            return f"🔬 Діагностика впала: `{e}`"

    async def run(self) -> None:
        """Запускає polling."""
        logger.info("🤖 Telegram бот запущено")
        await self.dp.start_polling(self.bot)


# ── Точка входу ───────────────────────────────────────────

async def run_bot_with_trader(mode: str = "demo") -> None:
    """
    Запускає трейдер + Telegram бот паралельно.
    """
    from config.settings import TRADING_PAIRS

    token, admin_id = _load_tg_config()
    trader = LiveTrader()
    tg_bot = TradingBot(token=token, admin_id=admin_id, trader=trader)

    # ВАЖЛИВО: даємо трейдеру посилання на нотифаєр, інакше push-алерти
    # (відкриття/закриття угоди) НЕ надсилаються — вони саме тут і вмикаються.
    trader.notifier = tg_bot

    # Відправляємо стартове повідомлення
    if admin_id:
        mode_str = "🟡 DEMO" if BYBIT_DEMO else "🔴 LIVE"
        await tg_bot.send_alert(
            f"🤖 *Bot\\_trade запущено*\n\n"
            f"Режим: {mode_str}\n"
            f"Пари: {', '.join(TRADING_PAIRS)}\n\n"
            f"Натисни /menu для керування"
        )

    # Запускаємо трейдер і бот паралельно
    await asyncio.gather(
        trader.run(),
        tg_bot.run(),
    )


async def run_monitor_only() -> None:
    """
    Тільки Telegram бот без запуску торгівлі.
    Увага: цей режим створює новий LiveTrader зі своїм станом.
    Для повного моніторингу вже запущеного процесу потрібен shared state / БД / API.
    """
    token, admin_id = _load_tg_config()

    trader = LiveTrader()
    tg_bot = TradingBot(token=token, admin_id=admin_id, trader=trader)

    logger.info("👁 Режим моніторингу (без торгівлі)")
    await tg_bot.run()


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Telegram Bot для Bot_trade")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="Demo + Telegram")
    mode.add_argument("--live", action="store_true", help="Live + Telegram")
    mode.add_argument("--monitor", action="store_true", help="Тільки моніторинг")
    args = parser.parse_args()

    if args.monitor:
        asyncio.run(run_monitor_only())
    elif args.demo:
        if not BYBIT_DEMO:
            logger.error(
                "❌ Відмовлено: --demo, але BYBIT_DEMO=false. "
                "Live credentials не будуть використані."
            )
            return
        asyncio.run(run_bot_with_trader())
    elif args.live:
        if BYBIT_DEMO:
            logger.error("❌ Відмовлено: --live, але BYBIT_DEMO=true")
            return
        if _os.getenv("CONFIRM_LIVE_TRADING", "").strip() != "LIVE":
            logger.error(
                "❌ Live-запуск заблоковано. Для свідомого дозволу встанови "
                "CONFIRM_LIVE_TRADING=LIVE."
            )
            return
        asyncio.run(run_bot_with_trader())
    else:
        print("\n📋 Використання:")
        print("  python telegrambot.py --demo     # Demo торгівля + Telegram")
        print("  python telegrambot.py --live     # Live торгівля + Telegram")
        print("  python telegrambot.py --monitor  # Тільки моніторинг\n")


if __name__ == "__main__":
    main()
