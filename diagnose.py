"""
ДІАГНОСТИКА «ЧОМУ НЕМАЄ УГОД»
==============================
Запуск на твоєму ПК (де є ccxt і доступ до Bybit):

    python diagnose.py

Скрипт:
  1) тягне ЖИВІ свічки BTC з Bybit (public REST, без ключів);
  2) друкує поточні значення індикаторів ПРОТИ порогів кожної стратегії;
  3) запускає всі стратегії один раз і показує спрацювала / ні + причину
     (вмикає DEBUG-логи — там кожна стратегія пише, чому пропускає сигнал);
  4) для сигналів, що спрацювали, рахує позицію з комісіями і показує net RR
     та чи проходить rr_ok (тобто чи реально відкрилася б угода).

Так ти за 10 секунд побачиш, де саме «затик»:
  - якщо стратегії НЕ дають сигналів → ринок не дає сетапів АБО пороги жорсткі;
  - якщо сигнали Є, а угод у боті немає → проблема у виконанні ордера
    (OB-фільтр стіни / create_order / маржа) → шукай у логах бота помилки.
"""

import sys
from decimal import Decimal

import pandas as pd
from loguru import logger

# показуємо DEBUG — саме там стратегії друкують причину пропуску
logger.remove()
logger.add(sys.stdout, level="DEBUG",
           format="<level>{level: <7}</level> | {message}")

try:
    import ccxt
except ImportError:
    print("❌ ccxt не встановлено. pip install ccxt")
    sys.exit(1)

from config.settings import (
    SYMBOL_CONFIG, DEPOSIT_USDT, RISK_PER_TRADE_PCT, FTA_TF,
)
try:
    from config.settings import STRATEGY_PRIORITY
except Exception:
    STRATEGY_PRIORITY = ["meanrev", "vwap", "sweep"]

from signals.generator import generate_scalp_signal
from signals.calculator import calculate_position
from indicators.range_detector import calculate_atr
from indicators.oscillators import bollinger_bands, rsi, vwap_bands, ema, adx

# нові стратегії можуть бути ще не задеплоєні — імпортуємо безпечно
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

SYMBOL = "BTC/USDT:USDT"

TF_DURATIONS = {
    "1m": pd.Timedelta(minutes=1),
    "3m": pd.Timedelta(minutes=3),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
    "2h": pd.Timedelta(hours=2),
    "4h": pd.Timedelta(hours=4),
    "1d": pd.Timedelta(days=1),
}


def fetch(ex, tf, limit=250):
    raw = ex.fetch_ohlcv(SYMBOL, tf, limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    duration = TF_DURATIONS.get(tf)
    if duration is None:
        raise ValueError(f"Невідомий timeframe: {tf}")
    now = pd.Timestamp.now(tz="UTC")
    # Bybit REST зазвичай повертає forming candle останнім рядком. Діагностика
    # має бути causal-equivalent live, тому використовує лише вже закриті.
    return df.loc[(df.index + duration) <= now]


def line(txt=""):
    print(txt)


def run_calc(sig):
    pos = calculate_position(
        symbol=SYMBOL, deposit=Decimal(str(DEPOSIT_USDT)),
        risk_pct=Decimal(str(RISK_PER_TRADE_PCT)),
        entry_price=Decimal(str(sig["entry"])),
        stop_loss=Decimal(str(sig["sl"])),
        take_profit=Decimal(str(sig["tp"])),
        min_rr=Decimal(str(sig["min_rr"])) if sig.get("min_rr") is not None else None,
    )
    return pos


def _truncate(dfs, decision_time):
    """Каузальний зріз: включає лише свічки, закриті до decision_time."""
    out = {}
    for tf, df in dfs.items():
        if df is None:
            out[tf] = None
            continue
        duration = TF_DURATIONS.get(tf)
        if duration is None:
            out[tf] = df.loc[df.index < decision_time]
        else:
            out[tf] = df.loc[(df.index + duration) <= decision_time]
    return out


def _run_all(dfs, symbol):
    """Повертає dict strat->signal (None якщо не спрацювала)."""
    res = {}
    if generate_trend_signal:
        try: res["trend"] = generate_trend_signal(dfs, symbol)
        except Exception: res["trend"] = None
    if generate_vwap_signal:
        try: res["vwap"] = generate_vwap_signal(dfs, symbol)
        except Exception: res["vwap"] = None
    if generate_meanrev_signal:
        try: res["meanrev"] = generate_meanrev_signal(dfs, symbol)
        except Exception: res["meanrev"] = None
    try:
        res["sweep"] = generate_scalp_signal(df_1h=dfs.get("1h"), df_5m=dfs.get("5m"),
                                              df_1m=dfs.get("1m"), symbol=symbol,
                                              cached_range=None, mode="A")
    except Exception:
        res["sweep"] = None
    return res


def scan_history(dfs, symbol, scan_bars=180):
    """
    Каузальний діагностичний scan останніх scan_bars 5m-свічок. Це НЕ
    win-rate/backtest: тут немає виходів, one-position, OB та cooldown.
    """
    df5 = dfs.get("5m")
    if df5 is None or len(df5) < scan_bars + 10:
        line("   (замало 5m історії для реплею)")
        return
    fired = {}
    execd = {}
    idxs = df5.index[-scan_bars:]
    logger.remove()
    logger.add(sys.stdout, level="WARNING")   # тиша під час реплею
    for cutoff in idxs:
        sub = _truncate(dfs, cutoff + pd.Timedelta(minutes=5))
        for name, sig in _run_all(sub, symbol).items():
            if sig is None:
                continue
            fired[name] = fired.get(name, 0) + 1
            pos = run_calc(sig)
            if "error" not in pos and pos.get("rr_ok"):
                execd[name] = execd.get(name, 0) + 1
    logger.remove()
    logger.add(sys.stdout, level="DEBUG", format="<level>{level: <7}</level> | {message}")

    line(f"\n   За останні {scan_bars} 5m-свічок (~{scan_bars*5/60:.0f} год):")
    names = sorted(set(list(fired) + ["trend", "vwap", "meanrev", "sweep"]))
    for n in names:
        line(f"     {n:<8}: сигналів {fired.get(n,0):>3}  |  net-RR пройшли {execd.get(n,0):>3}")
    total_exec = sum(execd.values())
    return total_exec


def test_websocket(seconds: int = 12) -> None:
    """
    Прямий тест WS до Bybit З ЦЬОГО СЕРВЕРА: підключаємось до
    stream.bybit.com, підписуємось на kline.1.BTCUSDT і рахуємо пуші.
    Якщо пуші йдуть — мережа ОК і проблему треба шукати в боті (/diag →
    'WS-потоки' покаже точну помилку). Якщо ні — мережа сервера ріже WS.
    """
    import asyncio
    import json as _json
    try:
        import websockets
        line(f"   websockets version: {getattr(websockets, '__version__', '?')}")
    except ImportError:
        line("   ❌ websockets не встановлено:")
        line("      python3.11 -m pip install 'websockets>=12,<13'")
        return

    async def _run():
        url = "wss://stream.bybit.com/v5/public/linear"
        total = kline = 0
        try:
            ws = await asyncio.wait_for(
                websockets.connect(url, ping_interval=None, ping_timeout=None),
                timeout=15,
            )
        except Exception as e:
            line(f"   ❌ WS НЕ ПІДКЛЮЧАЄТЬСЯ: {type(e).__name__}: {e}")
            line("      → мережа сервера блокує WebSocket до stream.bybit.com")
            line("      → перевір: curl -sI https://stream.bybit.com | head -1")
            return
        try:
            await ws.send(_json.dumps({"op": "subscribe", "args": ["kline.1.BTCUSDT"]}))
            loop = asyncio.get_event_loop()
            end = loop.time() + seconds
            while loop.time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                total += 1
                if "kline" in raw:
                    kline += 1
        finally:
            try:
                await ws.close()
            except Exception:
                pass
        if kline > 0:
            line(f"   ✅ WS ЖИВИЙ: за {seconds}с прийшло {kline} kline-пушів (всього {total} msg)")
            line("      → мережа ОК. Якщо бот все одно морозить дані —")
            line("        дивись /diag розділ 'WS-потоки': там текст помилки бота")
        else:
            line(f"   ⚠️ WS підключився, але kline-пушів НЕМА (msg={total})")
            line("      → підписка не працює; надішли цей вивід у чат")

    asyncio.run(_run())


def main():
    scan_bars = 180
    if len(sys.argv) > 1:
        try: scan_bars = int(sys.argv[1])
        except ValueError: pass

    ex = ccxt.bybit({"enableRateLimit": True,
                     "options": {"defaultType": "linear", "defaultSubType": "linear"}})
    line("📡 Тягну живі свічки BTC з Bybit...")
    symbol_cfg = SYMBOL_CONFIG.get(SYMBOL, {})
    required_tfs = {"1h", "30m", "5m", "1m", FTA_TF}
    required_tfs.update({
        str(symbol_cfg.get("trend", {}).get("trend_tf", "1h")),
        str(symbol_cfg.get("trend", {}).get("entry_tf", "5m")),
        str(symbol_cfg.get("vwap", {}).get("tf", "5m")),
        str(symbol_cfg.get("meanrev", {}).get("tf", "5m")),
    })
    limits = {
        "1d": 300, "4h": 300, "2h": 300, "1h": 300,
        "30m": 400, "15m": 500, "5m": 600, "3m": 800, "1m": 1000,
    }
    tf_order = {tf: i for i, tf in enumerate(TF_DURATIONS)}
    dfs = {}
    for tf in sorted(required_tfs, key=lambda x: tf_order.get(x, 999)):
        try:
            dfs[tf] = fetch(ex, tf, limit=limits.get(tf, 500))
            line(f"   {tf}: {len(dfs[tf])} свічок, остання ціна {dfs[tf]['close'].iloc[-1]:.1f}")
        except Exception as e:
            line(f"   ❌ {tf}: {e}")
            dfs[tf] = None

    line("\n" + "=" * 64)
    line("ТЕСТ WEBSOCKET (чи пропускає мережа сервера live-потік):")
    line("=" * 64)
    test_websocket(seconds=12)

    cfg = SYMBOL_CONFIG.get(SYMBOL, {})
    price = dfs["5m"]["close"].iloc[-1]

    line("\n" + "=" * 64)
    line(f"РИНОК ЗАРАЗ: BTC = {price:.1f}")
    line(f"STRATEGY_PRIORITY = {STRATEGY_PRIORITY}")
    line("=" * 64)

    # ── MEAN-REVERSION діагностика ──────────────────────────
    mr = cfg.get("meanrev", {})
    if mr.get("enabled"):
        df = dfs.get(mr.get("tf", "5m"))
        bb = bollinger_bands(df["close"], int(mr.get("bb_period", 20)), float(mr.get("bb_std", 2.0)))
        rv = float(rsi(df["close"], int(mr.get("rsi_period", 14))).iloc[-1])
        line("\n── MEANREV (BB+RSI) ──")
        line(f"   BB width = {bb['width_pct']:.3f}%  (потрібно ≥ {mr.get('min_width_pct')})  "
             f"{'✅' if bb['width_pct'] >= float(mr.get('min_width_pct', 0)) else '❌ канал вузький'}")
        line(f"   %b = {bb['percent_b']:.2f}  (≤0 = торкнув низ / ≥1 = торкнув верх)")
        line(f"   RSI = {rv:.1f}  (OS {mr.get('rsi_oversold')} / OB {mr.get('rsi_overbought')})")

    # ── VWAP діагностика ────────────────────────────────────
    vw = cfg.get("vwap", {})
    if vw.get("enabled"):
        df = dfs.get(vw.get("tf", "5m"))
        win = vw.get("window", 96)
        win = int(win) if win else None
        vw_mode = str(vw.get("mode", "session")).lower()
        vb = vwap_bands(
            df,
            window=win if vw_mode == "rolling" else None,
            k=float(vw.get("k_band", 2.0)),
            anchor="session" if vw_mode == "session" else None,
        )
        line(f"\n── VWAP (σ-bands, {vw_mode}) ──")
        need = len(df) if df is not None else 0
        line(f"   свічок {need} (потрібно ≥ {(win or 30)+5})")
        line(f"   VWAP = {vb['vwap']:.1f}  дев = {vb['dev_pct']:+.3f}%  "
             f"(потрібно |дев| ≥ {vw.get('min_dev_pct')})  "
             f"{'✅' if abs(vb['dev_pct']) >= float(vw.get('min_dev_pct', 0)) else '❌ замала девіація'}")
        line(f"   смуги: [{vb['lower']:.0f} .. {vb['upper']:.0f}]  ціна {price:.0f}")

    # ── TREND діагностика ───────────────────────────────────
    tc = cfg.get("trend", {})
    if tc.get("enabled") and dfs.get("1h") is not None:
        d1 = dfs["1h"]
        c = d1["close"]
        ef, em, es = ema(c, 20).iloc[-1], ema(c, 50).iloc[-1], ema(c, 200).iloc[-1]
        adx_v = float(adx(d1, 14).iloc[-1])
        line("\n── TREND (EMA-stack 1h) ──")
        line(f"   свічок 1h = {len(d1)} (потрібно ≥ ~210 для EMA200)")
        line(f"   EMA20={ef:.0f}  EMA50={em:.0f}  EMA200={es:.0f}  ціна={c.iloc[-1]:.0f}")
        line(f"   ADX = {adx_v:.1f}  (потрібно ≥ {tc.get('adx_min')})  "
             f"{'✅ тренд' if adx_v >= float(tc.get('adx_min', 20)) else '❌ боковик'}")
        updn = "LONG-gate" if (ef > em > es and c.iloc[-1] > em) else (
               "SHORT-gate" if (ef < em < es and c.iloc[-1] < em) else "немає gate (боковик)")
        line(f"   напрямок тренду: {updn}")

    # ── ЗАПУСК СТРАТЕГІЙ (як у боті) ────────────────────────
    line("\n" + "=" * 64)
    line("ЗАПУСК СТРАТЕГІЙ (DEBUG-причини нижче кожної):")
    line("=" * 64)

    runners = {
        "trend":   (lambda: generate_trend_signal(dfs, SYMBOL)) if generate_trend_signal else None,
        "vwap":    (lambda: generate_vwap_signal(dfs, SYMBOL)) if generate_vwap_signal else None,
        "meanrev": (lambda: generate_meanrev_signal(dfs, SYMBOL)) if generate_meanrev_signal else None,
        "sweep":   (lambda: generate_scalp_signal(df_1h=dfs["1h"], df_5m=dfs["5m"],
                                                   df_1m=dfs["1m"], symbol=SYMBOL,
                                                   cached_range=None, mode="A")),
    }

    any_signal = False
    for name in STRATEGY_PRIORITY:
        name = name.strip().lower()
        fn = runners.get(name)
        line(f"\n▶ {name.upper()}")
        if fn is None:
            line("   (стратегія недоступна в цій версії коду)")
            continue
        try:
            sig = fn()
        except Exception as e:
            line(f"   ❌ виняток: {e}")
            continue
        if sig is None:
            line("   → сигналу немає (причина в DEBUG-рядку вище)")
            continue
        any_signal = True
        pos = run_calc(sig)
        if "error" in pos:
            line(f"   ⚠️ сигнал Є, але позиція неможлива: {pos['error']}")
        else:
            ok = pos["rr_ok"]
            line(f"   {'✅ УГОДА ВІДКРИЛАСЬ БИ' if ok else '❌ відсічено net RR'}: "
                 f"{sig['direction'].upper()} entry={sig['entry']:.1f} "
                 f"TP={sig['tp']:.1f} SL={sig['sl']:.1f} | net RR={pos['rr_ratio']} "
                 f"(поріг {pos.get('rr_floor')})")

    # ── ІСТОРИЧНИЙ РЕПЛЕЙ (головна діагностика) ─────────────
    line("\n" + "=" * 64)
    line("РЕПЛЕЙ ІСТОРІЇ — скільки сетапів дав ринок нещодавно:")
    line("=" * 64)
    total_exec = scan_history(dfs, SYMBOL, scan_bars=scan_bars)

    line("\n" + "=" * 64)
    if total_exec and total_exec > 0:
        line(f"ВИСНОВОК: ринок ДАВАВ ~{total_exec} відкриваних сетапів за вікно.")
        line("Отже сигнали Є, а угод у боті немає → проблема у ВИКОНАННІ:")
        line("  1) OB-фільтр стіни ріже вхід  → лог 'велика стіна проти'")
        line("  2) помилка ордера / маржі      → лог 'Помилка виконання ордера'")
        line("  3) позиція замала              → лог 'Позиція неможлива'")
        line("  4) бот на паузі / не той код   → перевір, що задеплоєна нова версія")
        line("Грепни лог: grep -E 'стіна|Помилка викон|Позиція немож|floor' logs/*.log")
    elif any_signal:
        line("ВИСНОВОК: зараз сигнал Є, але за вікно реплею відкриваних мало.")
        line("Ринок на межі порогів. Дивись ❌ вище і лог бота на виконання.")
    else:
        line("ВИСНОВОК: ринок майже НЕ давав сетапів (тихо / жорсткі пороги).")
        line("Це і є причина 0 угод. Дивись ❌ вище — які саме пороги не дотягли,")
        line("і пом'якш їх у settings (min_width_pct / k_band / min_dev_pct / adx_min).")
    line("=" * 64)


if __name__ == "__main__":
    main()
