import os
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════
# TRADING PAIRS
# ═══════════════════════════════════════════════════════════════

TRADING_PAIRS = [
    "BTC/USDT:USDT",
    #"SOL/USDT:USDT",
]

# SYMBOL_CONFIG — тільки BTC (як запитано)
SYMBOL_CONFIG = {
    "BTC/USDT:USDT": {
        "display":      "BTC",
        "min_qty":      0.001,
        "qty_step":     0.001,
        "price_step":   0.1,
        "slippage":     0.0003,
        "bybit_symbol": "BTCUSDT",

        # ── Main strategy: Liquidity Sweep / Market Maker Range
        # Range detection — ТЕПЕР реально застосовується: generator.py і
        # live_trade.py читають ці значення з SYMBOL_CONFIG (раніше ігнорувались
        # і використовувались хардкод-дефолти 1.5 / 8.0 / 2.5 → рейндж майже
        # ніколи не знаходився, бо реальний BTC range 3-6% > 8×ATR).
        "range_lookback":     30,     # ~1.25 доби 1h — свіжіший рейндж
        "min_range_atr":      0.8,    # дозволяємо трохи вужчі рейнджі
        "max_range_atr":      14.0,   # ГОЛОВНИЙ анблокер: ширший BTC range
        "max_drift_atr":      5.0,    # не відсікати помірний боковик з дрейфом

        "sweep_buffer_atr":   0.10,   # легше зловити sweep за межу
        "stop_pad_atr":       0.15,   # ширший буфер SL → менший вплив комісій

        "structure_lookback": 3,      # м'який BOS/MSS

        # min_rr ТУТ = GROSS RR у generator (ДО комісій). Виконавець
        # (calculator.calculate_position) окремо рахує NET RR після комісій
        # і порівнює з MIN_RISK_REWARD. На BTC round-trip комісія+сліпедж
        # ~0.17% notional, тож gross мусить мати запас (див. розрахунок нижче).
        "min_rr":             2.0,

        # Мінімальна дистанція SL у % від ціни. Захищає від двох речей одразу:
        #   1) комісія (0.17%) з'їдає угоду з надто тісним стопом
        #   2) тісний стоп → величезна позиція → перевищення маржі → ордер
        #      відхиляється біржею (тому угоди й "не з'являлись")
        # None → береться глобальний MIN_SL_DISTANCE_PCT.
        "min_sl_distance_pct": 0.005,  # ~0.5% (≈$475 на BTC ~95k)

        # ── Order Flow — основний підтверджуючий фільтр
        "order_flow_lookback": 3,
        "use_order_flow_filter": True,

        # ── CVD / Volume — тимчасово НЕ блокують сигнал
        "use_cvd_filter":     False,
        "use_volume_filter":  False,

        # Але логувати їх треба, щоб потім порівняти статистику
        "log_cvd":            True,
        "log_volume":         True,

        "cvd_lookback":       3,
        "volume_mult":        1.05,
        "volume_lookback":    20,
    }

}


# ═══════════════════════════════════════════════════════════════
# TIMEFRAMES
# ═══════════════════════════════════════════════════════════════

TIMEFRAMES = {
    "trend":   "1h",
    "dev":     "15m",
    "confirm": "5m",
    "entry":   "1m",
}

BYBIT_HISTORY_MONTHS = {
    "1m":  2,
    "5m":  6,
    "15m": 12,
    "1h":  24,
}


# ═══════════════════════════════════════════════════════════════
# BYBIT API
# ═══════════════════════════════════════════════════════════════

BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY",    "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_DEMO_KEY   = os.getenv("BYBIT_DEMO_KEY",   "")
BYBIT_DEMO_SECRET = os.getenv("BYBIT_DEMO_SECRET", "")
BYBIT_DEMO       = os.getenv("BYBIT_DEMO", "false").lower() == "true"

ACTIVE_API_KEY    = BYBIT_DEMO_KEY    if BYBIT_DEMO else BYBIT_API_KEY
ACTIVE_API_SECRET = BYBIT_DEMO_SECRET if BYBIT_DEMO else BYBIT_API_SECRET

TRADING_MODE = os.getenv("TRADING_MODE", "backtest")


# ═══════════════════════════════════════════════════════════════
# FEES / SLIPPAGE
# ═══════════════════════════════════════════════════════════════

BYBIT_TAKER_FEE = float(os.getenv("BYBIT_TAKER_FEE", 0.00055))
BYBIT_MAKER_FEE = float(os.getenv("BYBIT_MAKER_FEE", 0.0001))

MARKET_SLIPPAGE = {
    "BTC/USDT:USDT": float(os.getenv("BTC_MARKET_SLIPPAGE", 0.0003)),
    "SOL/USDT:USDT": float(os.getenv("SOL_MARKET_SLIPPAGE", 0.0005)),
}

BREAKEVEN_PCT = {
    "BTC/USDT:USDT": 0.0017,
    #"SOL/USDT:USDT": 0.0021,
}


# ═══════════════════════════════════════════════════════════════
# ACCOUNT / RISK
# ═══════════════════════════════════════════════════════════════

DEPOSIT_USDT          = float(os.getenv("DEPOSIT_USDT",          500))
RISK_PER_TRADE_PCT    = float(os.getenv("RISK_PER_TRADE_PCT",    0.01))
RISK_PER_TRADE_USDT   = DEPOSIT_USDT * RISK_PER_TRADE_PCT

MAX_DAILY_LOSS_PCT    = float(os.getenv("MAX_DAILY_LOSS_PCT",    0.03))
MAX_DAILY_LOSS_USDT   = DEPOSIT_USDT * MAX_DAILY_LOSS_PCT

# MIN_RISK_REWARD — це NET RR (ПІСЛЯ комісій), який перевіряє виконавець
# (calculator.calculate_position.rr_ok). Тримаємо нижче за generator min_rr
# (=2.0 gross), бо комісії з'їдають частину RR. 1.2 net ≈ 2.0 gross при стопі 0.5%.
MIN_RISK_REWARD       = float(os.getenv("MIN_RISK_REWARD",       1.2))
MAX_OPEN_POSITIONS    = int(os.getenv("MAX_OPEN_POSITIONS",      2))
MAX_LEVERAGE          = float(os.getenv("MAX_LEVERAGE",          10))


# ═══════════════════════════════════════════════════════════════
# TREND / EMA FILTER
# ═══════════════════════════════════════════════════════════════

USE_EMA_TREND_FILTER      = os.getenv("USE_EMA_TREND_FILTER", "true").lower() == "true"
TREND_EMA_PERIOD          = int(os.getenv("TREND_EMA_PERIOD",          50))
TREND_EMA_SLOPE_CANDLES   = int(os.getenv("TREND_EMA_SLOPE_CANDLES",   5))
TREND_EMA_SLOPE_THRESHOLD = float(os.getenv("TREND_EMA_SLOPE_THRESHOLD", 0.15))


# ═══════════════════════════════════════════════════════════════
# RANGE DETECTION
# ═══════════════════════════════════════════════════════════════

RANGE_LOOKBACK_CANDLES      = int(os.getenv("RANGE_LOOKBACK_CANDLES",      96))
RANGE_MIN_TOUCHES_EACH_SIDE = int(os.getenv("RANGE_MIN_TOUCHES_EACH_SIDE", 2))
RANGE_TOUCH_ATR_MULT        = float(os.getenv("RANGE_TOUCH_ATR_MULT",       0.6))
RANGE_MIN_WIDTH_PCT         = float(os.getenv("RANGE_MIN_WIDTH_PCT",        0.008))
RANGE_MIN_CANDLES           = int(os.getenv("RANGE_MIN_CANDLES",           20))


# ═══════════════════════════════════════════════════════════════
# BOLLINGER BANDS (для старих модулів / signal_engine)
# ═══════════════════════════════════════════════════════════════

BB_PERIOD                  = int(os.getenv("BB_PERIOD",   20))
BB_STD                     = float(os.getenv("BB_STD",    2.0))
BB_SQUEEZE_PCT             = float(os.getenv("BB_SQUEEZE_PCT", 2.0))
TREND_BB_WIDTH_PCT         = float(os.getenv("TREND_BB_WIDTH_PCT",         3.5))
TREND_BB_BREAKOUT_CANDLES  = int(os.getenv("TREND_BB_BREAKOUT_CANDLES",    2))
TREND_RESUME_WAIT_CANDLES  = int(os.getenv("TREND_RESUME_WAIT_CANDLES",    5))


# ═══════════════════════════════════════════════════════════════
# DEVIATION
# ═══════════════════════════════════════════════════════════════

DEVIATION_ATR_MULT         = float(os.getenv("DEVIATION_ATR_MULT",      0.3))
DEVIATION_VOLUME_LOOKBACK  = int(os.getenv("DEVIATION_VOLUME_LOOKBACK", 20))
DEVIATION_VOLUME_MAX_RATIO = float(os.getenv("DEVIATION_VOLUME_MAX_RATIO", 1.5))
MAX_DEVIATION_AGE_MIN      = int(os.getenv("MAX_DEVIATION_AGE_MIN",     180))

DEVIATION_VOLUME_MODE      = os.getenv("DEVIATION_VOLUME_MODE", "weak")
WEAK_BREAKOUT_VOLUME_MAX   = float(os.getenv("WEAK_BREAKOUT_VOLUME_MAX", 1.5))
CLIMAX_VOLUME_MIN          = float(os.getenv("CLIMAX_VOLUME_MIN",        2.0))
VOLUME_RATIO_MAX           = DEVIATION_VOLUME_MAX_RATIO


# ═══════════════════════════════════════════════════════════════
# ATR
# ═══════════════════════════════════════════════════════════════

ATR_PERIOD      = int(os.getenv("ATR_PERIOD",      14))
ATR_PERIOD_FAST = int(os.getenv("ATR_PERIOD_FAST",  7))


# ═══════════════════════════════════════════════════════════════
# STRATEGY SWITCHES
# ═══════════════════════════════════════════════════════════════

USE_DUAL_TF_STRATEGY = os.getenv("USE_DUAL_TF_STRATEGY", "false").lower() == "true"
USE_ORDER_BOOK_CONFIRMATION = os.getenv("USE_ORDER_BOOK_CONFIRMATION", "false").lower() == "true"
USE_ORDER_BOOK_WALL_FILTER = os.getenv("USE_ORDER_BOOK_WALL_FILTER", "true").lower() == "true"


# ═══════════════════════════════════════════════════════════════
# CVD / ORDER FLOW
# ═══════════════════════════════════════════════════════════════

CVD_LOOKBACK            = int(os.getenv("CVD_LOOKBACK",   3))
OF_DELTA_LOOKBACK       = int(os.getenv("OF_DELTA_LOOKBACK", 3))
USE_CVD_CONFIRMATION    = os.getenv("USE_CVD_CONFIRMATION", "false").lower() == "true"


# ═══════════════════════════════════════════════════════════════
# BOS — Break of Structure
# ═══════════════════════════════════════════════════════════════

USE_BOS_CONFIRMATION  = os.getenv("USE_BOS_CONFIRMATION", "false").lower() == "true"
BOS_VOLUME_MULT       = float(os.getenv("BOS_VOLUME_MULT",      1.2))
ENTRY_SWING_LOOKBACK  = int(os.getenv("ENTRY_SWING_LOOKBACK",   3))


# ═══════════════════════════════════════════════════════════════
# 1m ENTRY CONFIRMATION
# ═══════════════════════════════════════════════════════════════

USE_1M_VOLUME_CONFIRMATION = os.getenv("USE_1M_VOLUME_CONFIRMATION", "true").lower() == "true"
ENTRY_VOLUME_LOOKBACK      = int(os.getenv("ENTRY_VOLUME_LOOKBACK",    20))
ENTRY_VOLUME_MIN_RATIO     = float(os.getenv("ENTRY_VOLUME_MIN_RATIO", 0.8))


# ═══════════════════════════════════════════════════════════════
# TP / SL
# ═══════════════════════════════════════════════════════════════

TP_RANGE_PCT          = float(os.getenv("TP_RANGE_PCT",       0.70))
SL_ATR_BUFFER         = float(os.getenv("SL_ATR_BUFFER",      0.3))
SL_ATR_MULT_RANGE     = SL_ATR_BUFFER
SL_ATR_STOP_PAD       = float(os.getenv("SL_ATR_STOP_PAD",    0.12))

# Глобальний мінімальний стоп у % від ціни (fallback, якщо в SYMBOL_CONFIG
# не заданий min_sl_distance_pct). 0.5% тримає комісію в межах ~1/3 ризику
# і не дає позиції роздутись понад маржу.
MIN_SL_DISTANCE_PCT   = float(os.getenv("MIN_SL_DISTANCE_PCT",  0.005))
SL_ZONE_BUFFER_PCT    = float(os.getenv("SL_ZONE_BUFFER_PCT",   0.0005))


# ═══════════════════════════════════════════════════════════════
# COOLDOWN / LIMITS
# ═══════════════════════════════════════════════════════════════

MIN_CANDLES_BETWEEN_TRADES = int(os.getenv("MIN_CANDLES_BETWEEN_TRADES", 3))
MAX_TRADES_PER_HOUR        = int(os.getenv("MAX_TRADES_PER_HOUR",        4))
MAX_TRADES_PER_DAY         = int(os.getenv("MAX_TRADES_PER_DAY",        12))
COOLDOWN_AFTER_LOSS_MIN    = int(os.getenv("COOLDOWN_AFTER_LOSS_MIN",   10))
MAX_CONSECUTIVE_LOSSES     = int(os.getenv("MAX_CONSECUTIVE_LOSSES",     3))
COOLDOWN_AFTER_SERIES_MIN  = int(os.getenv("COOLDOWN_AFTER_SERIES_MIN", 60))

MAX_CONFIRMATION_AGE_MIN   = int(os.getenv("MAX_CONFIRMATION_AGE_MIN",  90))
MAX_SIGNAL_AGE_MIN         = int(os.getenv("MAX_SIGNAL_AGE_MIN",        60))


# ═══════════════════════════════════════════════════════════════
# ORDER BOOK
# ═══════════════════════════════════════════════════════════════

OB_IMBALANCE_LONG_MIN  = float(os.getenv("OB_IMBALANCE_LONG_MIN",   12.0))
OB_IMBALANCE_SHORT_MAX = float(os.getenv("OB_IMBALANCE_SHORT_MAX", -12.0))
OB_MAX_AGE_SECONDS     = int(os.getenv("OB_MAX_AGE_SECONDS",         30))
OB_WALL_THRESHOLD_MULT = float(os.getenv("OB_WALL_THRESHOLD_MULT",    3.0))
OB_WALL_BLOCK_PCT      = float(os.getenv("OB_WALL_BLOCK_PCT",        0.003))


# ═══════════════════════════════════════════════════════════════
# DATA COLLECTION
# ═══════════════════════════════════════════════════════════════

CANDLES_PER_REQUEST = int(os.getenv("CANDLES_PER_REQUEST", 1000))


# ═══════════════════════════════════════════════════════════════
# STRATEGY MODE
# ═══════════════════════════════════════════════════════════════

TRADING_MODE_STRATEGY = os.getenv("TRADING_MODE_STRATEGY", "range")
DEVIATION_TF          = os.getenv("DEVIATION_TF",           "15m")
ENTRY_TF              = os.getenv("ENTRY_TF",               "1m")
STOCH_CONFIRM_TF      = os.getenv("STOCH_CONFIRM_TF",       "5m")


# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", 5432)),
    "database": os.getenv("DB_NAME",     "quant_bot"),
    "user":     os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
}
"""
if not DB_CONFIG["password"]:
    raise ValueError("DB_PASSWORD is not set. Check your .env file.")
"""


# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

LOG_LEVEL     = os.getenv("LOG_LEVEL",     "DEBUG")
LOG_FILE      = os.getenv("LOG_FILE",      "logs/bot.log")
LOG_ROTATION  = os.getenv("LOG_ROTATION",  "1 day")
LOG_RETENTION = os.getenv("LOG_RETENTION", "30 days")