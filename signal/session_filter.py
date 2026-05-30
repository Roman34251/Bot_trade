"""
ФІЛЬТР ТОРГОВИХ СЕСІЙ
========================
Торгуємо тільки в двох сесіях де боковик найстабільніший:

  Азійська сесія:       02:00 - 09:00 UTC
  Лондон pre-market:    06:00 - 09:00 UTC (перекривається з Азією)

ЧОМУ САМЕ ЦІ СЕСІЇ для рейндж скальпінгу:

  Азійська (02-09 UTC):
    - Низький обсяг → ціна бовтається в боковику
    - BTC і SOL рідко роблять великі направлені рухи
    - Ідеально для рейндж стратегії
    - Менше false breakout від великих гравців

  Лондон pre-market (06-09 UTC):
    - Інституції готуються до відкриття
    - Часто "підчищають" ліквідність перед відкриттям Лондона
    - Девіації з поверненням = наша стратегія

УНИКАЄМО:
  Нью-Йорк відкриття (13:30-16:00 UTC):
    - Висока волатильність → false breakout без повернення
    - Великі направлені рухи → рейндж ламається

  Лондон відкриття (08:00-10:00 UTC):
    - Різкі рухи при відкритті → небезпечно для рейнджу
    - Виключення: якщо рейндж дуже щільний (BB width < 1%)

UTC+3 для Львова (твій часовий пояс):
  Азійська:      05:00 - 12:00 за київським часом
  Лондон pre:    09:00 - 12:00 за київським часом
"""

from datetime import datetime, timezone, time
from loguru import logger


# ── Визначення сесій (UTC) ─────────────────────────────────

SESSIONS = {
    "asian": {
        "start": time(2, 0),    # 02:00 UTC
        "end":   time(9, 0),    # 09:00 UTC
        "description": "Азійська сесія (05:00-12:00 Київ)",
        "quality": "high",      # найкраща для рейнджу
    },
    "london_pre": {
        "start": time(6, 0),    # 06:00 UTC
        "end":   time(8, 0),    # 08:00 UTC (до відкриття Лондона)
        "description": "Лондон pre-market (09:00-11:00 Київ)",
        "quality": "medium",
    },
    "london_open": {
        "start": time(8, 0),    # 08:00 UTC — небезпечна зона
        "end":   time(10, 0),   # 10:00 UTC
        "description": "Лондон відкриття — ПРОПУСКАЄМО",
        "quality": "dangerous",
    },
    "new_york": {
        "start": time(13, 30),  # 13:30 UTC
        "end":   time(20, 0),   # 20:00 UTC
        "description": "Нью-Йорк — ПРОПУСКАЄМО (висока волатильність)",
        "quality": "dangerous",
    },
}

# Дозволені сесії для торгівлі
ALLOWED_SESSIONS = {"asian", "london_pre"}


def get_current_session(dt: datetime | None = None) -> str:
    """
    Повертає назву поточної сесії або 'off_hours'.

    dt: datetime в UTC. Якщо None — використовує поточний час.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)

    current_time = dt.time()

    for session_name, session in SESSIONS.items():
        start = session["start"]
        end   = session["end"]

        # Перевірка діапазону (не переходить опівніч)
        if start <= current_time < end:
            return session_name

    return "off_hours"


def is_trading_allowed(dt: datetime | None = None,
                       strict: bool = True) -> dict:
    """
    Перевіряє чи дозволена торгівля в поточний час.

    strict=True:  торгуємо тільки в asian + london_pre
    strict=False: торгуємо у всіх сесіях крім dangerous

    Повертає dict:
      allowed      — True/False
      session      — назва сесії
      reason       — пояснення
      next_open    — коли наступне відкрите вікно (UTC)
    """
    if dt is None:
        dt = datetime.now(timezone.utc)

    session = get_current_session(dt)

    if session in ALLOWED_SESSIONS:
        info = SESSIONS[session]
        return {
            "allowed": True,
            "session": session,
            "reason":  info["description"],
            "quality": info["quality"],
        }

    if session == "off_hours":
        reason = "Поза торговими сесіями"
    elif session == "london_open":
        reason = "Лондон відкриття — висока волатильність"
    elif session == "new_york":
        reason = "Нью-Йорк сесія — направлені рухи"
    else:
        reason = f"Сесія {session} не дозволена"

    return {
        "allowed":  False,
        "session":  session,
        "reason":   reason,
        "quality":  SESSIONS.get(session, {}).get("quality", "unknown"),
    }


def session_filter(dt: datetime | None = None) -> bool:
    """
    Простий bool фільтр для використання в engine.

    Використання:
      if not session_filter():
          return None  # не торгуємо
    """
    result = is_trading_allowed(dt)
    if not result["allowed"]:
        logger.debug(f"⏰ Торгівля заборонена: {result['reason']}")
    return result["allowed"]


def get_session_info(dt: datetime | None = None) -> str:
    """Повертає читабельний рядок про поточну сесію."""
    if dt is None:
        dt = datetime.now(timezone.utc)

    result  = is_trading_allowed(dt)
    kyiv_h  = (dt.hour + 3) % 24   # UTC+3
    kyiv_str = f"{kyiv_h:02d}:{dt.minute:02d}"

    status = "✅ ТОРГУЄМО" if result["allowed"] else "⛔ ПРОПУСКАЄМО"
    return (f"{status} | {result['session']} | "
            f"UTC {dt.strftime('%H:%M')} / Київ {kyiv_str} | "
            f"{result['reason']}")