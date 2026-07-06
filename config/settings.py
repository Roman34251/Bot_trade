"""
НАЛАШТУВАННЯ БОТА
==================
Структура (після повного аудиту 2026-07-07):

  1) РОБОЧА ЗОНА — кожен параметр тут РЕАЛЬНО читається живим кодом
     (live_trade → trend/vwap/meanrev/sweep → calculator).
     Все тюниться через .env БЕЗ зміни коду: os.getenv(...) всюди.

  2) АРХІВ (в кінці файлу, закоментовано) — 50+ параметрів старих
     стратегій (deviation/signal_engine/BB-squeeze/стохастик), які
     НІДЕ не імпортуються. Лишені як довідка, щоб не вводили в оману.

ПРОТОКОЛ ТЮНІНГУ (домовленість із власником):
  після кожних ~5 закритих угод переглядаємо статистику і ЖОРСТКІШАЄМО
  пороги через .env (драбина кроків — див. CLAUDE.md). Пріоритет ручок:
  RSI-пороги → ширина BB → k VWAP → ADX → min_rr → OB-фільтри.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() == "true"


# ═══════════════════════════════════════════════════════════════
# TRADING PAIRS
# ═══════════════════════════════════════════════════════════════

TRADING_PAIRS = [
    "BTC/USDT:USDT",
    #"SOL/USDT:USDT",
]

# SYMBOL_CONFIG — тільки BTC. Ключові «ручки» стратегій читаються з .env
# (назви змінних — у коментарях), щоб тюнити без зміни коду.
SYMBOL_CONFIG = {
    "BTC/USDT:USDT": {
        "display":      "BTC",

        # ── Main strategy: Liquidity Sweep / Market Maker Range ──
        "range_lookback":     30,
        "min_range_atr":      0.8,
        "max_range_atr":      14.0,
        "max_drift_atr":      5.0,

        "sweep_buffer_atr":   0.10,
        "stop_pad_atr":       0.15,
        "structure_lookback": 3,

        # Sweep як setup-state (вікно) + scored-підтвердження
        "sweep_window":        12,
        "min_confirmations":   1,

        # Order Flow — м'яке підтвердження (не hard-filter)
        "order_flow_lookback": 3,
        "use_order_flow_filter": False,

        # CVD / Volume — лише логування
        "use_cvd_filter":     False,
        "use_volume_filter":  False,
        "log_cvd":            True,
        "log_volume":         True,
        "cvd_lookback":       3,
        "volume_mult":        1.05,
        "volume_lookback":    20,

        # gross RR порог sweep-генератора (.env: SWEEP_MIN_RR)
        "min_rr":             float(os.getenv("SWEEP_MIN_RR", 1.6)),

        # Мін. дистанція SL (% від ціни) для sweep. None → MIN_SL_DISTANCE_PCT
        "min_sl_distance_pct": 0.005,

        # ════════════════════════════════════════════════════════
        # СТРАТЕГІЯ B — Mean-Reversion (BB + RSI), 5m
        # ════════════════════════════════════════════════════════
        "meanrev": {
            "enabled":        _env_bool("MEANREV_ENABLED", "true"),
            "tf":             "5m",
            "bb_period":      20,
            "bb_std":         2.0,
            "rsi_period":     14,
            # .env: MEANREV_RSI_OS / MEANREV_RSI_OB (жорсткіше = 30/70)
            "rsi_oversold":   float(os.getenv("MEANREV_RSI_OS", 35)),
            "rsi_overbought": float(os.getenv("MEANREV_RSI_OB", 65)),
            "require_rsi":    True,
            "tp_target":      "mid",
            "sl_atr_buffer":  0.5,
            # .env: MEANREV_MIN_WIDTH_PCT (жорсткіше = більше)
            "min_width_pct":  float(os.getenv("MEANREV_MIN_WIDTH_PCT", 0.18)),
            # .env: MEANREV_USE_ADX=true — вимикати mean-reversion у тренді
            "use_adx_filter": _env_bool("MEANREV_USE_ADX", "false"),
            "adx_max":        float(os.getenv("MEANREV_ADX_MAX", 35)),
            "min_sl_pct":     0.0018,
            # .env: MEANREV_MIN_RR (NET після комісій; жорсткіше = більше)
            "min_rr":         float(os.getenv("MEANREV_MIN_RR", 0.5)),
        },

        # ════════════════════════════════════════════════════════
        # СТРАТЕГІЯ C — VWAP σ-band reversion, 5m
        # ════════════════════════════════════════════════════════
        "vwap": {
            "enabled":        _env_bool("VWAP_ENABLED", "true"),
            "tf":             "5m",
            "window":         int(os.getenv("VWAP_WINDOW", 96)),
            # .env: VWAP_K_BAND (жорсткіше = більше, класика 2.0)
            "k_band":         float(os.getenv("VWAP_K_BAND", 1.8)),
            "require_rsi":    False,
            "rsi_period":     14,
            "rsi_oversold":   42,
            "rsi_overbought": 58,
            "sl_k":           3.5,
            "tp_target":      "vwap",
            # .env: VWAP_MIN_DEV_PCT (жорсткіше = більше)
            "min_dev_pct":    float(os.getenv("VWAP_MIN_DEV_PCT", 0.12)),
            "min_sl_pct":     0.002,
            # .env: VWAP_MIN_RR
            "min_rr":         float(os.getenv("VWAP_MIN_RR", 0.6)),
        },

        # ════════════════════════════════════════════════════════
        # СТРАТЕГІЯ D — Trend-following (EMA-stack pullback), 1h→5m
        # ════════════════════════════════════════════════════════
        "trend": {
            "enabled":        _env_bool("TREND_ENABLED", "true"),
            "trend_tf":       "1h",
            # .env: TREND_ENTRY_TF (5m активніше / 15m чистіше)
            "entry_tf":       os.getenv("TREND_ENTRY_TF", "5m"),
            "ema_fast":       20,
            "ema_mid":        50,
            "ema_slow":       200,
            "use_ema200_filter": True,
            "ema_slope_lookback": 5,
            "adx_period":     14,
            # .env: TREND_ADX_MIN (жорсткіше = більше; 18 м'яко, 22 суворо)
            "adx_min":        float(os.getenv("TREND_ADX_MIN", 18)),
            "atr_period":     14,
            "pullback_lookback": 6,
            "max_pullback_below_ema_atr": 0.5,
            "max_extension_atr": 1.2,
            "swing_lookback": 10,
            "sl_buffer_atr":  0.3,
            # .env: TREND_TP_R (ціль у R; gross)
            "tp_r":           float(os.getenv("TREND_TP_R", 2.2)),
            "use_rsi_confirm": _env_bool("TREND_USE_RSI", "false"),
            "rsi_period":     14,
            "min_sl_pct":     0.0018,
            # .env: TREND_MIN_RR (NET після комісій)
            "min_rr":         float(os.getenv("TREND_MIN_RR", 1.4)),
        },
    }

}


# ═══════════════════════════════════════════════════════════════
# STRATEGY SELECTION
# ═══════════════════════════════════════════════════════════════

USE_SWEEP_STRATEGY   = _env_bool("USE_SWEEP_STRATEGY",   "true")
USE_MEANREV_STRATEGY = _env_bool("USE_MEANREV_STRATEGY", "true")
USE_VWAP_STRATEGY    = _env_bool("USE_VWAP_STRATEGY",    "true")
USE_TREND_STRATEGY   = _env_bool("USE_TREND_STRATEGY",   "true")

# Легасі-фолбек (адаптер dual_tf); трендова стратегія працює НЕ через нього
USE_DUAL_TF_STRATEGY = _env_bool("USE_DUAL_TF_STRATEGY", "false")

# Перша стратегія зі списку, що дала сигнал — виконується
STRATEGY_PRIORITY = os.getenv(
    "STRATEGY_PRIORITY", "trend,vwap,meanrev,sweep"
).split(",")


# ═══════════════════════════════════════════════════════════════
# BYBIT API
# ═══════════════════════════════════════════════════════════════

BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY",    "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_DEMO_KEY   = os.getenv("BYBIT_DEMO_KEY",   "")
BYBIT_DEMO_SECRET = os.getenv("BYBIT_DEMO_SECRET", "")
BYBIT_DEMO       = _env_bool("BYBIT_DEMO", "false")

ACTIVE_API_KEY    = BYBIT_DEMO_KEY    if BYBIT_DEMO else BYBIT_API_KEY
ACTIVE_API_SECRET = BYBIT_DEMO_SECRET if BYBIT_DEMO else BYBIT_API_SECRET


# ═══════════════════════════════════════════════════════════════
# FEES / SLIPPAGE (використовуються калькулятором позиції!)
# ═══════════════════════════════════════════════════════════════

# Bybit USDT-перпи, стандартний тариф: taker 0.055%, maker 0.02%.
# Вхід і SL — маркет (taker+сліпедж); TP — ЛІМІТНИЙ (maker, без сліпеджу).
BYBIT_TAKER_FEE = float(os.getenv("BYBIT_TAKER_FEE", 0.00055))
BYBIT_MAKER_FEE = float(os.getenv("BYBIT_MAKER_FEE", 0.0002))

# Модель сліпеджу маркет-ордера (частка від ціни, в ОДИН бік).
# 0.015% — консервативно для BTCUSDT (реально ~0.005-0.01% на 1-2 BTC);
# стара вшита 0.03% завищувала витрати вдвічі-втричі і різала угоди.
# Після 10+ угод відкалібруємо за фактичними виконаннями (real_entry).
BTC_SLIPPAGE_PCT = float(os.getenv("BTC_SLIPPAGE_PCT", 0.00015))
SOL_SLIPPAGE_PCT = float(os.getenv("SOL_SLIPPAGE_PCT", 0.0003))


# ═══════════════════════════════════════════════════════════════
# ACCOUNT / RISK
# ═══════════════════════════════════════════════════════════════

# Капітал тягнеться з акаунта через API (walletBalance USDT).
# DEPOSIT_USDT — fallback, якщо API недоступний, або USE_REAL_BALANCE=false.
USE_REAL_BALANCE      = _env_bool("USE_REAL_BALANCE", "true")
DEPOSIT_USDT          = float(os.getenv("DEPOSIT_USDT",          500))
RISK_PER_TRADE_PCT    = float(os.getenv("RISK_PER_TRADE_PCT",    0.01))

MAX_DAILY_LOSS_PCT    = float(os.getenv("MAX_DAILY_LOSS_PCT",    0.03))

# Fallback NET-RR поріг виконавця. Кожна стратегія несе ВЛАСНИЙ min_rr
# (trend 1.4 / sweep 1.6 / vwap 0.6 / meanrev 0.5) — цей діє лише для
# сигналів без власного порогу.
MIN_RISK_REWARD       = float(os.getenv("MIN_RISK_REWARD",       1.2))


# ═══════════════════════════════════════════════════════════════
# COOLDOWN / LIMITS
# ═══════════════════════════════════════════════════════════════

# Хвилин між угодами (трактується як ХВИЛИНИ у live_trade)
MIN_CANDLES_BETWEEN_TRADES = int(os.getenv("MIN_CANDLES_BETWEEN_TRADES", 2))
MAX_TRADES_PER_DAY         = int(os.getenv("MAX_TRADES_PER_DAY",        30))
COOLDOWN_AFTER_LOSS_MIN    = int(os.getenv("COOLDOWN_AFTER_LOSS_MIN",    8))
MAX_CONSECUTIVE_LOSSES     = int(os.getenv("MAX_CONSECUTIVE_LOSSES",     4))
# Пауза після серії збитків, ПІСЛЯ якої серія скидається і торгівля
# відновлюється (інакше був вічний деддок: не торгуєш → не виграєш →
# streak не скидається)
COOLDOWN_AFTER_SERIES_MIN  = int(os.getenv("COOLDOWN_AFTER_SERIES_MIN", 45))


# ═══════════════════════════════════════════════════════════════
# ORDER BOOK
# ═══════════════════════════════════════════════════════════════

# Фільтри OB (обидва зараз вимкнені — вмикати на етапі жорсткішання):
USE_ORDER_BOOK_CONFIRMATION = _env_bool("USE_ORDER_BOOK_CONFIRMATION", "false")
USE_ORDER_BOOK_WALL_FILTER  = _env_bool("USE_ORDER_BOOK_WALL_FILTER",  "false")

# Калібрування під BTC (топ-25 рівнів стакана):
#   imbalance ±12% — шум, ±20% — значущий перекіс
#   стіна 3× середнього — постійне явище (різало все); 8× — реальна стіна
#   зона 0.3% від 63k = $190 (щось є завжди); 0.05% ≈ $30 = впритул
OB_IMBALANCE_LONG_MIN  = float(os.getenv("OB_IMBALANCE_LONG_MIN",   20.0))
OB_IMBALANCE_SHORT_MAX = float(os.getenv("OB_IMBALANCE_SHORT_MAX", -20.0))
OB_MAX_AGE_SECONDS     = int(os.getenv("OB_MAX_AGE_SECONDS",         10))
OB_WALL_THRESHOLD_MULT = float(os.getenv("OB_WALL_THRESHOLD_MULT",    8.0))
OB_WALL_BLOCK_PCT      = float(os.getenv("OB_WALL_BLOCK_PCT",        0.0005))


# ═══════════════════════════════════════════════════════════════
# ДОПОМІЖНІ (sweep-генератор)
# ═══════════════════════════════════════════════════════════════

CVD_LOOKBACK          = int(os.getenv("CVD_LOOKBACK", 3))
# Глобальний мін. SL % (fallback, якщо в SYMBOL_CONFIG немає власного)
MIN_SL_DISTANCE_PCT   = float(os.getenv("MIN_SL_DISTANCE_PCT", 0.005))


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


# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

LOG_LEVEL     = os.getenv("LOG_LEVEL",     "DEBUG")
LOG_FILE      = os.getenv("LOG_FILE",      "logs/bot.log")
LOG_ROTATION  = os.getenv("LOG_ROTATION",  "1 day")
LOG_RETENTION = os.getenv("LOG_RETENTION", "30 days")


# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
#   АРХІВ — НЕ ПРАЦЮЄ (аудит 2026-07-07: ніде не імпортується)
# ═══════════════════════════════════════════════════════════════
# Параметри старих стратегій (deviation / signal_engine / BB-squeeze /
# стохастик / старий EMA-фільтр). Живий код їх НЕ читає. Розкоментовувати
# безглуздо, поки не з'явиться код, що їх використовує.
#
# --- таймфрейми/історія (live_trade має власний список 1h/30m/5m/1m):
# TIMEFRAMES = {"trend": "1h", "dev": "15m", "confirm": "5m", "entry": "1m"}
# BYBIT_HISTORY_MONTHS = {"1m": 2, "5m": 6, "15m": 12, "1h": 24}
# TRADING_MODE = os.getenv("TRADING_MODE", "backtest")
#
# --- комісії/сліпедж (calculator.py має ВЛАСНІ хардкоди BYBIT_TAKER/SLIPPAGE):
# BYBIT_MAKER_FEE = float(os.getenv("BYBIT_MAKER_FEE", 0.0001))
# MARKET_SLIPPAGE = {"BTC/USDT:USDT": 0.0003, "SOL/USDT:USDT": 0.0005}
# BREAKEVEN_PCT   = {"BTC/USDT:USDT": 0.0017}
#
# --- ризик (обчислювались, але ніде не читались / НЕ РЕАЛІЗОВАНІ):
# RISK_PER_TRADE_USDT = DEPOSIT_USDT * RISK_PER_TRADE_PCT
# MAX_DAILY_LOSS_USDT = DEPOSIT_USDT * MAX_DAILY_LOSS_PCT
# MAX_OPEN_POSITIONS  = 2     # бот тримає РІВНО 1 позицію (state.open_trade)
# MAX_LEVERAGE        = 10    # НІКОЛИ не відправляється на біржу!
# MAX_TRADES_PER_HOUR = 6     # ніде не перевіряється (діє лише PER_DAY)
#
# --- старий EMA-трендфільтр (новий тренд живе в SYMBOL_CONFIG["trend"]):
# USE_EMA_TREND_FILTER = true; TREND_EMA_PERIOD = 50
# TREND_EMA_SLOPE_CANDLES = 5; TREND_EMA_SLOPE_THRESHOLD = 0.15
#
# --- старий range-детектор (BB-based detect_range, не в живому шляху):
# RANGE_LOOKBACK_CANDLES = 96; RANGE_MIN_TOUCHES_EACH_SIDE = 2
# RANGE_TOUCH_ATR_MULT = 0.6; RANGE_MIN_WIDTH_PCT = 0.008; RANGE_MIN_CANDLES = 20
#
# --- Bollinger для старих модулів (meanrev має власні bb_period/bb_std):
# BB_PERIOD = 20; BB_STD = 2.0; BB_SQUEEZE_PCT = 2.0
# TREND_BB_WIDTH_PCT = 3.5; TREND_BB_BREAKOUT_CANDLES = 2; TREND_RESUME_WAIT_CANDLES = 5
#
# --- deviation-стратегія (indicators/deviation.py має ВЛАСНІ локальні копії):
# DEVIATION_ATR_MULT = 0.3; DEVIATION_VOLUME_LOOKBACK = 20
# DEVIATION_VOLUME_MAX_RATIO = 1.5; MAX_DEVIATION_AGE_MIN = 180
# DEVIATION_VOLUME_MODE = "weak"; WEAK_BREAKOUT_VOLUME_MAX = 1.5
# CLIMAX_VOLUME_MIN = 2.0; VOLUME_RATIO_MAX = 1.5
#
# --- ATR-періоди (індикатори використовують власні дефолти period=14):
# ATR_PERIOD = 14; ATR_PERIOD_FAST = 7
#
# --- OF/CVD/BOS підтвердження старого шляху:
# OF_DELTA_LOOKBACK = 3; USE_CVD_CONFIRMATION = false
# USE_BOS_CONFIRMATION = false; BOS_VOLUME_MULT = 1.2; ENTRY_SWING_LOOKBACK = 3
#
# --- 1m volume-підтвердження (ніде не перевіряється):
# USE_1M_VOLUME_CONFIRMATION = true; ENTRY_VOLUME_LOOKBACK = 20; ENTRY_VOLUME_MIN_RATIO = 0.8
#
# --- TP/SL старих режимів (entry.calculate_levels — поза живим шляхом):
# TP_RANGE_PCT = 0.70; SL_ATR_BUFFER = 0.3; SL_ATR_MULT_RANGE = SL_ATR_BUFFER
# SL_ATR_STOP_PAD = 0.12; SL_ZONE_BUFFER_PCT = 0.0005
#
# --- застарілі ліміти часу сигналів:
# MAX_CONFIRMATION_AGE_MIN = 90; MAX_SIGNAL_AGE_MIN = 60
#
# --- режим старого signal_engine:
# TRADING_MODE_STRATEGY = "range"; DEVIATION_TF = "15m"; ENTRY_TF = "1m"; STOCH_CONFIRM_TF = "5m"
# CANDLES_PER_REQUEST = 1000
