import os
from dotenv import load_dotenv

load_dotenv()

# Trading pairs
TRADING_PAIRS = [
    "BTC/USDT:USDT",
    "SOL/USDT:USDT",
]

SYMBOL_CONFIG = {
    "BTC/USDT:USDT": {
        "display": "BTC",
        "min_qty": 0.001,
        "qty_step": 0.001,
        "price_step": 0.1,
        "slippage": 0.0003,
        "bybit_symbol": "BTCUSDT",
    },
    "SOL/USDT:USDT": {
        "display": "SOL",
        "min_qty": 0.1,
        "qty_step": 0.1,
        "price_step": 0.001,
        "slippage": 0.0005,
        "bybit_symbol": "SOLUSDT",
    },
}

# Timeframes
TIMEFRAMES = {
    "trend": "1h",
    "dev": "15m",
    "confirm": "5m",
    "entry": "1m",
}

BYBIT_HISTORY_MONTHS = {
    "1m": 2,
    "5m": 6,
    "15m": 12,
    "1h": 24,
}

# Bybit
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET_KEY = os.getenv("BYBIT_TESTNET_KEY", "")
BYBIT_TESTNET_SECRET = os.getenv("BYBIT_TESTNET_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

ACTIVE_API_KEY = BYBIT_TESTNET_KEY if BYBIT_TESTNET else BYBIT_API_KEY
ACTIVE_API_SECRET = BYBIT_TESTNET_SECRET if BYBIT_TESTNET else BYBIT_API_SECRET

TRADING_MODE = os.getenv("TRADING_MODE", "backtest")

# Fees / slippage
BYBIT_TAKER_FEE = float(os.getenv("BYBIT_TAKER_FEE", 0.00055))
BYBIT_MAKER_FEE = float(os.getenv("BYBIT_MAKER_FEE", 0.0001))

MARKET_SLIPPAGE = {
    "BTC/USDT:USDT": float(os.getenv("BTC_MARKET_SLIPPAGE", 0.0003)),
    "SOL/USDT:USDT": float(os.getenv("SOL_MARKET_SLIPPAGE", 0.0005)),
}

BREAKEVEN_PCT = {
    "BTC/USDT:USDT": 0.0017,
    "SOL/USDT:USDT": 0.0021,
}

# Account / risk
DEPOSIT_USDT = float(os.getenv("DEPOSIT_USDT", 500))
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", 0.005))
RISK_PER_TRADE_USDT = DEPOSIT_USDT * RISK_PER_TRADE_PCT

MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", 0.03))
MAX_DAILY_LOSS_USDT = DEPOSIT_USDT * MAX_DAILY_LOSS_PCT

MIN_RISK_REWARD = float(os.getenv("MIN_RISK_REWARD", 1.5))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", 1))
MAX_LEVERAGE = float(os.getenv("MAX_LEVERAGE", 10))

# Trend / EMA filter
USE_EMA_TREND_FILTER = os.getenv("USE_EMA_TREND_FILTER", "true").lower() == "true"
TREND_EMA_PERIOD = int(os.getenv("TREND_EMA_PERIOD", 50))
TREND_EMA_SLOPE_CANDLES = int(os.getenv("TREND_EMA_SLOPE_CANDLES", 5))
TREND_EMA_SLOPE_THRESHOLD = float(os.getenv("TREND_EMA_SLOPE_THRESHOLD", 0.15))

# Range detection
RANGE_LOOKBACK_CANDLES = int(os.getenv("RANGE_LOOKBACK_CANDLES", 96))
RANGE_MIN_TOUCHES_EACH_SIDE = int(os.getenv("RANGE_MIN_TOUCHES_EACH_SIDE", 2))
RANGE_TOUCH_ATR_MULT = float(os.getenv("RANGE_TOUCH_ATR_MULT", 0.6))
RANGE_MIN_WIDTH_PCT = float(os.getenv("RANGE_MIN_WIDTH_PCT", 0.008))
RANGE_MIN_CANDLES = int(os.getenv("RANGE_MIN_CANDLES", 20))

# Bollinger values kept for old modules / future filters
BB_PERIOD = int(os.getenv("BB_PERIOD", 20))
BB_STD = float(os.getenv("BB_STD", 2.0))
BB_SQUEEZE_PCT = float(os.getenv("BB_SQUEEZE_PCT", 2.0))
TREND_BB_WIDTH_PCT = float(os.getenv("TREND_BB_WIDTH_PCT", 3.5))
TREND_BB_BREAKOUT_CANDLES = int(os.getenv("TREND_BB_BREAKOUT_CANDLES", 2))
TREND_RESUME_WAIT_CANDLES = int(os.getenv("TREND_RESUME_WAIT_CANDLES", 5))

# Deviation
DEVIATION_ATR_MULT = float(os.getenv("DEVIATION_ATR_MULT", 0.3))
DEVIATION_VOLUME_LOOKBACK = int(os.getenv("DEVIATION_VOLUME_LOOKBACK", 20))
DEVIATION_VOLUME_MAX_RATIO = float(os.getenv("DEVIATION_VOLUME_MAX_RATIO", 1.5))
MAX_DEVIATION_AGE_MIN = int(os.getenv("MAX_DEVIATION_AGE_MIN", 180))

# Kept for compatibility with older code
DEVIATION_VOLUME_MODE = os.getenv("DEVIATION_VOLUME_MODE", "weak")
WEAK_BREAKOUT_VOLUME_MAX = float(os.getenv("WEAK_BREAKOUT_VOLUME_MAX", 1.5))
CLIMAX_VOLUME_MIN = float(os.getenv("CLIMAX_VOLUME_MIN", 2.0))
VOLUME_RATIO_MAX = DEVIATION_VOLUME_MAX_RATIO

# ATR
ATR_PERIOD = int(os.getenv("ATR_PERIOD", 14))
ATR_PERIOD_FAST = int(os.getenv("ATR_PERIOD_FAST", 7))

# Stochastic
STOCH_K = int(os.getenv("STOCH_K", 5))
STOCH_D = int(os.getenv("STOCH_D", 3))
STOCH_SMOOTH = int(os.getenv("STOCH_SMOOTH", 3))

STOCH_OVERSOLD = float(os.getenv("STOCH_OVERSOLD", 30))
STOCH_OVERBOUGHT = float(os.getenv("STOCH_OVERBOUGHT", 70))

STOCH_LONG_MAX = float(os.getenv("STOCH_LONG_MAX", 30))
STOCH_SHORT_MIN = float(os.getenv("STOCH_SHORT_MIN", 70))

USE_STOCH_CROSS = os.getenv("USE_STOCH_CROSS", "true").lower() == "true"
STOCH_LONG_CROSS_MAX = float(os.getenv("STOCH_LONG_CROSS_MAX", 35))
STOCH_SHORT_CROSS_MIN = float(os.getenv("STOCH_SHORT_CROSS_MIN", 65))

# CVD kept off for candle-only range strategy
CVD_LOOKBACK = int(os.getenv("CVD_LOOKBACK", 3))
OF_DELTA_LOOKBACK = int(os.getenv("OF_DELTA_LOOKBACK", 3))
USE_CVD_CONFIRMATION = os.getenv("USE_CVD_CONFIRMATION", "false").lower() == "true"

# BOS kept off for range strategy
USE_BOS_CONFIRMATION = os.getenv("USE_BOS_CONFIRMATION", "false").lower() == "true"
BOS_VOLUME_MULT = float(os.getenv("BOS_VOLUME_MULT", 1.2))
ENTRY_SWING_LOOKBACK = int(os.getenv("ENTRY_SWING_LOOKBACK", 3))

# 1m entry confirmation
USE_1M_VOLUME_CONFIRMATION = os.getenv("USE_1M_VOLUME_CONFIRMATION", "true").lower() == "true"
ENTRY_VOLUME_LOOKBACK = int(os.getenv("ENTRY_VOLUME_LOOKBACK", 20))
ENTRY_VOLUME_MIN_RATIO = float(os.getenv("ENTRY_VOLUME_MIN_RATIO", 0.8))

# TP / SL
TP_RANGE_PCT = float(os.getenv("TP_RANGE_PCT", 0.70))
SL_ATR_BUFFER = float(os.getenv("SL_ATR_BUFFER", 0.3))
SL_ATR_MULT_RANGE = SL_ATR_BUFFER

MIN_SL_DISTANCE_PCT = float(os.getenv("MIN_SL_DISTANCE_PCT", 0.001))
SL_ZONE_BUFFER_PCT = float(os.getenv("SL_ZONE_BUFFER_PCT", 0.0005))

# Cooldown / limits
MIN_CANDLES_BETWEEN_TRADES = int(os.getenv("MIN_CANDLES_BETWEEN_TRADES", 3))
MAX_TRADES_PER_HOUR = int(os.getenv("MAX_TRADES_PER_HOUR", 4))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", 12))
COOLDOWN_AFTER_LOSS_MIN = int(os.getenv("COOLDOWN_AFTER_LOSS_MIN", 10))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", 3))
COOLDOWN_AFTER_SERIES_MIN = int(os.getenv("COOLDOWN_AFTER_SERIES_MIN", 60))

MAX_CONFIRMATION_AGE_MIN = int(os.getenv("MAX_CONFIRMATION_AGE_MIN", 90))
MAX_SIGNAL_AGE_MIN = int(os.getenv("MAX_SIGNAL_AGE_MIN", 60))

# Data collection
CANDLES_PER_REQUEST = int(os.getenv("CANDLES_PER_REQUEST", 1000))

# Strategy mode
TRADING_MODE_STRATEGY = os.getenv("TRADING_MODE_STRATEGY", "range")
DEVIATION_TF = os.getenv("DEVIATION_TF", "15m")
ENTRY_TF = os.getenv("ENTRY_TF", "1m")
STOCH_CONFIRM_TF = os.getenv("STOCH_CONFIRM_TF", "5m")

# Database
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "database": os.getenv("DB_NAME", "quant_bot"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
}

if not DB_CONFIG["password"]:
    raise ValueError("DB_PASSWORD is not set. Check your .env file.")

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "logs/bot.log")
LOG_ROTATION = os.getenv("LOG_ROTATION", "1 day")
LOG_RETENTION = os.getenv("LOG_RETENTION", "30 days")