"""
КОНФІГУРАЦІЯ — Range Scalping Bot
====================================
Біржа:      Bybit Futures (perpetual)
Стратегія:  скальпінг бокового руху маркет ордерами
Пари:       BTC/USDT:USDT і SOL/USDT:USDT (ф'ючерси)
Таймфрейми: 1m, 5m, 15m, 30m, 1h
Депозит:    $500 → ризик $5/угоду (1%)

═══════════════════════════════════════════════════════
 ЯК ПІДКЛЮЧИТИСЬ ДО BYBIT (API ключі):
═══════════════════════════════════════════════════════

 КРОК 1 — Отримай API ключі:
   bybit.com → правий кут → API → Create New Key
   Тип: "API Transaction"
   Дозволи:
     ✅ Contract - Orders (для ф'ючерсів)
     ✅ Contract - Positions
     ❌ Transfer  (НЕ дозволяй)
     ❌ Withdrawal (НЕ дозволяй)
   IP whitelist: додай свій IP для безпеки

 КРОК 2 — Testnet (тест без реальних грошей):
   testnet.bybit.com → той самий процес
   Встав ключі в .env як BYBIT_TESTNET_KEY / SECRET
   В .env: BYBIT_TESTNET=true

 КРОК 3 — .env файл:
   BYBIT_API_KEY=твій_ключ
   BYBIT_API_SECRET=твій_секрет
   BYBIT_TESTNET_KEY=testnet_ключ
   BYBIT_TESTNET_SECRET=testnet_секрет
   BYBIT_TESTNET=false  (true = testnet, false = live)

 ВАЖЛИВО:
   Для збору даних API ключі НЕ потрібні (публічний API)
   Для торгівлі — потрібні
   Маркет ордери = завжди Taker = 0.055% комісія
═══════════════════════════════════════════════════════
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── ТОРГОВІ ПАРИ (Bybit perpetual ф'ючерси) ──────────────
#
# CCXT формат для Bybit perpetual: "BASE/USDT:USDT"
# Bybit API назви: BTCUSDT, SOLUSDT (linear perpetual)
#
# Спот "BTC/USDT" ≠ Ф'ючерс "BTC/USDT:USDT" — різні ціни і графіки!
TRADING_PAIRS = [
    "BTC/USDT:USDT",   # BTC perpetual ф'ючерс
    "SOL/USDT:USDT",   # SOL perpetual ф'ючерс
]

# ─── ТАЙМФРЕЙМИ ───────────────────────────────────────────
#
# Режим А: 1h рейндж → 15m девіація → 5m фільтр → 1m BOS вхід
# Режим Б: 30m рейндж → 5m девіація → 1m BOS вхід
#
# Bybit зберігає 1m свічки: ~2 місяці
# Для бектесту на 1m використовуємо наявні дані
TIMEFRAMES = {
    "1m":  "1m",    # BOS підтвердження + маркет ордер
    "5m":  "5m",    # Stochastic, CVD, Order Flow фільтр
    "15m": "15m",   # девіація (Режим А)
    "30m": "30m",   # рейндж (Режим Б)
    "1h":  "1h",    # рейндж (Режим А) + ATR
}

TRADING_MODE_STRATEGY = os.getenv("TRADING_MODE_STRATEGY", "A")

# ─── BYBIT ПІДКЛЮЧЕННЯ ────────────────────────────────────
BYBIT_API_KEY       = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET    = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET_KEY   = os.getenv("BYBIT_TESTNET_KEY", "")
BYBIT_TESTNET_SECRET = os.getenv("BYBIT_TESTNET_SECRET", "")
BYBIT_TESTNET       = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

# Активні ключі (автоматично testnet або live)
ACTIVE_API_KEY    = BYBIT_TESTNET_KEY    if BYBIT_TESTNET else BYBIT_API_KEY
ACTIVE_API_SECRET = BYBIT_TESTNET_SECRET if BYBIT_TESTNET else BYBIT_API_SECRET

# ─── РЕЖИМ РОБОТИ ─────────────────────────────────────────
TRADING_MODE = "backtest"   # backtest → testnet → live

# ─── КОМІСІЇ ──────────────────────────────────────────────
#
# Маркет ордери = ЗАВЖДИ Taker = 0.055%
# Немає винятків — скальпінг маркет ордерами завжди taker
BYBIT_TAKER_FEE = 0.00055   # 0.055%
BYBIT_MAKER_FEE = 0.0001    # 0.01% (не використовуємо, але для довідки)

# Slippage маркет ордерів (додаткові витрати понад комісію)
MARKET_SLIPPAGE = {
    "BTC/USDT:USDT": 0.0003,   # 0.03% (~$28 при BTC $95,000)
    "SOL/USDT:USDT": 0.0005,   # 0.05% (менша ліквідність ніж BTC)
}

# Мінімальний рух для беззбитковості (fee × 2 + slippage × 2):
# BTC: (0.055% + 0.03%) × 2 = 0.17% від ціни
# SOL: (0.055% + 0.05%) × 2 = 0.21% від ціни
BREAKEVEN_PCT = {
    "BTC/USDT:USDT": 0.0017,
    "SOL/USDT:USDT": 0.0021,
}

# ─── ФІНАНСОВІ ПАРАМЕТРИ ($500 депозит) ───────────────────
DEPOSIT_USDT            = float(os.getenv("DEPOSIT_USDT", 500))
RISK_PER_TRADE_PCT      = float(os.getenv("RISK_PER_TRADE_PCT", 0.01))
RISK_PER_TRADE_USDT     = DEPOSIT_USDT * RISK_PER_TRADE_PCT   # $5

MAX_DAILY_LOSS_PCT      = float(os.getenv("MAX_DAILY_LOSS_PCT", 0.03))
MAX_DAILY_LOSS_USDT     = DEPOSIT_USDT * MAX_DAILY_LOSS_PCT    # $15

MIN_RISK_REWARD         = 2.0    # мін R:R після комісій і slippage
MAX_OPEN_POSITIONS      = 1      # одна позиція одночасно (MVP)

# ─── НАЛАШТУВАННЯ ПАР ─────────────────────────────────────
#
# BTC: вузький рейндж, висока ліквідність, менший slippage
# SOL: різкі сплески, менша ліквідність, ширший SL
SYMBOL_CONFIG = {
    "BTC/USDT:USDT": {
        "display":       "BTC",
        "min_qty":       0.001,    # мінімальний ордер
        "qty_step":      0.001,    # крок кількості
        "price_step":    0.1,      # tickSize (мін. крок ціни)
        "sl_atr_mult":   1.5,      # SL = 1.5 × ATR від межі рейнджу
        "slippage":      0.0003,   # 0.03% slippage маркет ордера
        "bybit_symbol":  "BTCUSDT",  # назва на Bybit API
    },
    "SOL/USDT:USDT": {
        "display":       "SOL",
        "min_qty":       0.1,
        "qty_step":      0.1,
        "price_step":    0.001,
        "sl_atr_mult":   2.0,      # ширший SL через волатильність SOL
        "slippage":      0.0005,   # 0.05%
        "bybit_symbol":  "SOLUSDT",
    },
}

# ─── ПАРАМЕТРИ РЕЙНДЖУ (визначення боковика) ──────────────
RANGE_MIN_CANDLES   = 20    # мін. свічок консолідації для рейнджу
BB_SQUEEZE_PCT      = 2.0   # BB width < 2% = стиснення
BB_PERIOD           = 20
BB_STD              = 2.0
DEVIATION_ATR_MULT  = 0.3   # вихід за межу > 0.3 ATR = девіація
VOLUME_RATIO_MAX    = 1.5   # об'єм девіації < 1.5x avg = false breakout

# ─── ПАРАМЕТРИ ІНДИКАТОРІВ ────────────────────────────────
ATR_PERIOD          = 14    # ATR на 1h для рейнджу
ATR_PERIOD_FAST     = 7     # ATR на 1m для точного SL

# Stochastic (замість RSI — швидший для скальпінгу)
STOCH_K             = 5     # period (5 свічок замість 14 у RSI)
STOCH_D             = 3     # smoothing D
STOCH_SMOOTH        = 3     # smoothing K
STOCH_OVERSOLD      = 20    # < 20 = лонг зона
STOCH_OVERBOUGHT    = 80    # > 80 = шорт зона

CVD_LOOKBACK        = 3     # свічок для CVD розвороту
OF_DELTA_LOOKBACK   = 3     # свічок для Order Flow (3 для скальпінгу)
BOS_VOLUME_MULT     = 1.2   # BOS об'єм > 1.2x avg

# TP/SL
TP_RANGE_PCT        = 0.70  # 70% до протилежної межі
SL_ATR_BUFFER       = 0.3   # буфер за фетилем девіації

# ─── COOLDOWN МІЖ УГОДАМИ (захист від overtrading) ────────
MIN_CANDLES_BETWEEN_TRADES = 3     # мін. 3 хвилини між угодами (1m)
MAX_TRADES_PER_HOUR        = 4     # не більше 4 угод/год
MAX_TRADES_PER_DAY         = 12    # не більше 12 угод/день
COOLDOWN_AFTER_LOSS_MIN    = 10    # 10 хвилин після збитку
MAX_CONSECUTIVE_LOSSES     = 3     # 3 збитки підряд → пауза 1 год
COOLDOWN_AFTER_SERIES_MIN  = 60    # 60 хвилин після серії збитків

# ─── ЗБІР ДАНИХ ───────────────────────────────────────────
HISTORY_YEARS           = 1
CANDLES_PER_REQUEST     = 1000

# Ліміти зберігання свічок на Bybit
BYBIT_HISTORY_MONTHS = {
    "1m":  2,    # тільки 2 місяці 1m свічок
    "5m":  6,
    "15m": 12,
    "30m": 12,
    "1h":  12,
}

# ─── БАЗА ДАНИХ ───────────────────────────────────────────
DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 5432)),
    "database": os.getenv("DB_NAME", "quant_bot"),
    "user":     os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
}

if not DB_CONFIG["password"]:
    raise ValueError("DB_PASSWORD не встановлено! Перевір .env файл")

# ─── ЛОГУВАННЯ ────────────────────────────────────────────
LOG_LEVEL     = "INFO"
LOG_FILE      = "logs/bot.log"
LOG_ROTATION  = "1 day"
LOG_RETENTION = "30 days"