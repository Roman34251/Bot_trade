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

        # ── Sweep як STATE, а не миттєвий збіг ──────────────────
        # Раніше sweep+BOS+order-flow мали збігтись на ОДНІЙ останній 1m
        # свічці одночасно → майже ніколи не траплялось → 0 угод.
        # Тепер sweep — це "setup", що живе sweep_window свічок; вхід
        # потребує reclaim + МІНІМУМ min_confirmations підтверджень.
        "sweep_window":        12,    # скільки 1m свічок sweep лишається валідним
        "min_confirmations":   1,     # скільки з (BOS / order-flow / momentum) треба

        # ── Order Flow — тепер М'ЯКЕ підтвердження (не hard-filter) ──
        "order_flow_lookback": 3,
        "use_order_flow_filter": False,   # було True → різало половину сигналів

        # ── CVD / Volume — тимчасово НЕ блокують сигнал
        "use_cvd_filter":     False,
        "use_volume_filter":  False,

        # Але логувати їх треба, щоб потім порівняти статистику
        "log_cvd":            True,
        "log_volume":         True,

        "cvd_lookback":       3,
        "volume_mult":        1.05,
        "volume_lookback":    20,

        # Власний поріг RR для sweep (gross, до комісій). Широкий 1h-range
        # дає велику ціль → RR високий, тримаємо запас на комісії.
        "min_rr":             1.6,

        # ════════════════════════════════════════════════════════
        # СТРАТЕГІЯ B — Mean-Reversion (Bollinger Bands + RSI)
        # Окрема, незалежна стратегія. Працює в боковику: ціна
        # торкається смуги BB + RSI в екстремумі → вхід на повернення
        # до середини каналу (SMA20). Дає БАГАТО угод.
        # ════════════════════════════════════════════════════════
        "meanrev": {
            "enabled":        True,
            "tf":             "5m",    # сигнальний ТФ (ширші смуги → TP покриває комісії)
            "bb_period":      20,
            "bb_std":         2.0,
            "rsi_period":     14,
            "rsi_oversold":   35,      # м'якше за класичні 30 → більше сигналів
            "rsi_overbought": 65,      # м'якше за класичні 70
            "require_rsi":    True,    # BB-торкання має підтверджуватись RSI
            "tp_target":      "mid",   # ціль = середня смуга BB (= середнє)
            "sl_atr_buffer":  0.5,     # SL за смугою + 0.5·ATR
            "min_width_pct":  0.18,    # знижено з 0.30: на вихідних low-vol стискав канал
                                       #   <0.30% і РІЗАВ УСІ сигнали (причина 0 угод за вихідні)
            "use_adx_filter": False,   # поки вимкнено → більше угод; вмикати при тюнінгу
            "adx_max":        35,      # якщо use_adx_filter: пропускати при ADX>35 (тренд)
            "min_sl_pct":     0.0018,  # ВЛАСНИЙ мін. SL (захист маржі), НЕ глобальний 0.5%
            # min_rr НИЗЬКИЙ свідомо: mean-reversion = високий win-rate / низький RR.
            # При маркет-ордерах і комісії ~0.17% net RR виходить ≈0.5-0.8.
            # Це стартове значення «РОЗТОРГУВАТИ + зібрати статистику на demo».
            # Підняти win-rate далі: maker/limit входи, ТФ 15m, суворіший RSI/ADX.
            "min_rr":         0.5,
        },

        # ════════════════════════════════════════════════════════
        # СТРАТЕГІЯ C — VWAP σ-band reversion
        # Окрема, незалежна стратегія. Ціна відходить на k·σ від VWAP
        # → вхід на повернення до VWAP (інституційний бенчмарк).
        # ════════════════════════════════════════════════════════
        "vwap": {
            "enabled":        True,
            "tf":             "5m",
            "window":         96,      # ковзний VWAP ≈ 8h на 5m (стабільніше за сесійний)
            "k_band":         1.8,     # вхід коли ціна за межами VWAP±1.8σ (знижено для угод)
            "require_rsi":    False,   # VWAP-девіації достатньо; RSI лише як бонус
            "rsi_period":     14,
            "rsi_oversold":   42,      # дуже м'яко (майже не блокує)
            "rsi_overbought": 58,
            "sl_k":           3.5,     # SL на рівні VWAP±3.5σ (за межею входу)
            "tp_target":      "vwap",  # ціль = сам VWAP
            "min_dev_pct":    0.12,    # мін. девіація від VWAP (знижено: у будні vol вище)
            "min_sl_pct":     0.002,   # власний мін. SL (захист маржі)
            "min_rr":         0.6,     # VWAP-девіація дає кращий RR за BB; поріг вищий
        },

        # ════════════════════════════════════════════════════════
        # СТРАТЕГІЯ D — TREND-FOLLOWING (EMA-stack pullback)
        # Найнадійніша проста трендова стратегія (за дослідженням).
        # Торгує ЛИШЕ за трендом 1h, вхід на відкаті до EMA20..EMA50 на 5m.
        # ВАЖЛИВО: BTC зараз у сильному НИЗХІДНОМУ тренді → ця стратегія
        # шортить ралі = найякісніше джерело угод саме зараз.
        # ════════════════════════════════════════════════════════
        "trend": {
            "enabled":        True,
            "trend_tf":       "1h",    # ТФ тренду ("що")
            "entry_tf":       "5m",    # ТФ входу ("коли")
            "ema_fast":       20,
            "ema_mid":        50,
            "ema_slow":       200,
            "use_ema200_filter": True, # вимагати price/EMA50 по той бік EMA200
            "ema_slope_lookback": 5,   # EMA50 має рости/падати за 5 барів
            "adx_period":     14,
            "adx_min":        18,      # <18 = боковик, не торгуємо (м'якше за 20 → більше угод)
            "atr_period":     14,
            "pullback_lookback": 6,    # у скількох останніх 5m барах шукати торкання EMA20
            "max_pullback_below_ema_atr": 0.5,  # відкид, якщо структуру зламано
            "max_extension_atr": 1.2,  # не входити, якщо ціна задерта від EMA20
            "swing_lookback": 10,      # свінг для структурного SL
            "sl_buffer_atr":  0.3,     # SL трохи за свінгом/EMA50
            "tp_r":           2.2,     # TP = 2.2R (gross; після комісій net ≈1.5)
            "use_rsi_confirm": False,  # поки вимкнено → більше угод
            "rsi_period":     14,
            "min_sl_pct":     0.0018,  # захист маржі
            # min_rr = NET-поріг. 2.2R gross дає ≈1.5 net (комісія 0.17% з'їдає
            # частину). 1.4 net із трендовим фільтром (win 45-55%) = сильний +EV.
            "min_rr":         1.4,
        },
    }

}


# ═══════════════════════════════════════════════════════════════
# STRATEGY SELECTION — які стратегії активні і в якому порядку
# ═══════════════════════════════════════════════════════════════

# Майстер-перемикачі (env може вимкнути будь-яку без зміни коду).
USE_SWEEP_STRATEGY   = os.getenv("USE_SWEEP_STRATEGY",   "true").lower() == "true"
USE_MEANREV_STRATEGY = os.getenv("USE_MEANREV_STRATEGY", "true").lower() == "true"
USE_VWAP_STRATEGY    = os.getenv("USE_VWAP_STRATEGY",    "true").lower() == "true"
USE_TREND_STRATEGY   = os.getenv("USE_TREND_STRATEGY",   "true").lower() == "true"

# Порядок перевірки в live_trade._check_signal. Перша стратегія, що дала
# валідний сигнал — виконується. TREND першим: він торгує ЗА трендом (зараз
# BTC падає → шорти з трендом = найякісніші угоди). Далі mean-reversion/vwap
# для частоти, sweep останнім.
STRATEGY_PRIORITY = os.getenv(
    "STRATEGY_PRIORITY", "trend,vwap,meanrev,sweep"
).split(",")


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

# Основний множник ATR для порогу девіації
# dual_df.py: low < range_low - atr × DEVIATION_ATR_MULT = devіація вниз
DEVIATION_ATR_MULT         = float(os.getenv("DEVIATION_ATR_MULT",      0.3))
DEVIATION_VOLUME_LOOKBACK  = int(os.getenv("DEVIATION_VOLUME_LOOKBACK", 20))
DEVIATION_VOLUME_MAX_RATIO = float(os.getenv("DEVIATION_VOLUME_MAX_RATIO", 1.5))
MAX_DEVIATION_AGE_MIN      = int(os.getenv("MAX_DEVIATION_AGE_MIN",     180))

# Сумісність зі старим кодом
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

# dual_tf тимчасово вимикаємо, бо будемо змінювати стратегію
USE_DUAL_TF_STRATEGY = os.getenv("USE_DUAL_TF_STRATEGY", "false").lower() == "true"

# Order Book робимо не обов'язковим direction-фільтром
USE_ORDER_BOOK_CONFIRMATION = os.getenv("USE_ORDER_BOOK_CONFIRMATION", "false").lower() == "true"

# Але стіну проти входу краще залишити як hard-filter
USE_ORDER_BOOK_WALL_FILTER = os.getenv("USE_ORDER_BOOK_WALL_FILTER", "false").lower() == "false"



# ═══════════════════════════════════════════════════════════════
# CVD / ORDER FLOW
# ════════════════════════════════════

CVD_LOOKBACK            = int(os.getenv("CVD_LOOKBACK",   3))
OF_DELTA_LOOKBACK       = int(os.getenv("OF_DELTA_LOOKBACK", 3))
USE_CVD_CONFIRMATION    = os.getenv("USE_CVD_CONFIRMATION", "false").lower() == "true"


# ═══════════════════════════════════════════════════════════════
# BOS — Break of Structure
# ═══════════════════════════════════════════════════════════════

# USE_BOS_CONFIRMATION — вмикає BOS в старій range стратегії
# В generator.py та dual_df.py BOS завжди обов'язковий
USE_BOS_CONFIRMATION  = os.getenv("USE_BOS_CONFIRMATION", "false").lower() == "true"
BOS_VOLUME_MULT       = float(os.getenv("BOS_VOLUME_MULT",      1.2))

# ENTRY_SWING_LOOKBACK — structure_lookback для detect_bos в entry.py
# generator.py бере з SYMBOL_CONFIG["structure_lookback"] (дефолт 5)
# dual_df.py використовує дефолт з entry.py (5)
# старий range стратегія бере цю константу
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

# TP_RANGE_PCT — для signal_engine (mode="range_pct")
# TP = range_low + range_size × TP_RANGE_PCT (0.70 = 70% рейнджу)
TP_RANGE_PCT          = float(os.getenv("TP_RANGE_PCT",       0.70))

# SL_ATR_BUFFER — для signal_engine і calculate_levels(mode="range_pct")
# SL = deviation_extreme ± atr × SL_ATR_BUFFER
SL_ATR_BUFFER         = float(os.getenv("SL_ATR_BUFFER",      0.3))
SL_ATR_MULT_RANGE     = SL_ATR_BUFFER   # аліас для сумісності

# Для dual_df (mode="midpoint") — stop_pad береться з SYMBOL_CONFIG
# або з дефолту в entry.py (0.12 ATR). Окрема константа тут для ясності:
SL_ATR_STOP_PAD       = float(os.getenv("SL_ATR_STOP_PAD",    0.12))

# Глобальний мінімальний стоп у % від ціни (fallback, якщо в SYMBOL_CONFIG
# не заданий min_sl_distance_pct). 0.5% тримає комісію в межах ~1/3 ризику
# і не дає позиції роздутись понад маржу.
MIN_SL_DISTANCE_PCT   = float(os.getenv("MIN_SL_DISTANCE_PCT",  0.005))
SL_ZONE_BUFFER_PCT    = float(os.getenv("SL_ZONE_BUFFER_PCT",   0.0005))


# ═══════════════════════════════════════════════════════════════
# COOLDOWN / LIMITS
# ═══════════════════════════════════════════════════════════════

# Кулдаун між угодами трактується в live_trade як ХВИЛИНИ. 2 хв — активний
# скальп, але без подвійних входів на тій самій свічці.
MIN_CANDLES_BETWEEN_TRADES = int(os.getenv("MIN_CANDLES_BETWEEN_TRADES", 2))
# Підняті ліміти: мета зараз — розторгувати бота і зібрати статистику.
# Звужуватимемо назад під час тюнінгу win-rate.
MAX_TRADES_PER_HOUR        = int(os.getenv("MAX_TRADES_PER_HOUR",        6))
MAX_TRADES_PER_DAY         = int(os.getenv("MAX_TRADES_PER_DAY",        30))
COOLDOWN_AFTER_LOSS_MIN    = int(os.getenv("COOLDOWN_AFTER_LOSS_MIN",    8))
MAX_CONSECUTIVE_LOSSES     = int(os.getenv("MAX_CONSECUTIVE_LOSSES",     4))
COOLDOWN_AFTER_SERIES_MIN  = int(os.getenv("COOLDOWN_AFTER_SERIES_MIN", 45))

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