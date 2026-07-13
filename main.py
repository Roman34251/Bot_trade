"""
ГОЛОВНИЙ ФАЙЛ ЗАПУСКУ
======================
  python main.py --setup   → ініціалізувати базу даних
  python main.py --status  → статус позицій на Bybit
  python main.py --demo    → запустити на demo рахунку Bybit
  python main.py --live    → запустити на live рахунку (обережно!)
"""

import sys
import asyncio
import argparse
from loguru import logger

from config.settings import (
    LOG_LEVEL, LOG_FILE, LOG_ROTATION, LOG_RETENTION,
    BYBIT_DEMO, ACTIVE_API_KEY, ACTIVE_API_SECRET,
)
from storage.database import Database


def setup_logging() -> None:
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
        "logs/bot_{time:YYYY-MM-DD}.log",  # ← змінити тільки це
        level="DEBUG",
        format="{time:YYYY-MM-DD} | {message}",
        rotation="00:00",                  # ← і це
        retention=LOG_RETENTION,
        encoding="utf-8",
    )


def cmd_setup() -> None:
    logger.info("🔧 Ініціалізація бази даних...")
    with Database() as db:
        db.initialize_tables()
    logger.info("✅ База даних готова!")


def cmd_status() -> None:
    """Показує поточні позиції на Bybit."""
    if not ACTIVE_API_KEY:
        logger.warning("API ключі не встановлені")
        return

    try:
        import ccxt

        exchange = ccxt.bybit({
            "apiKey": ACTIVE_API_KEY,
            "secret": ACTIVE_API_SECRET,
            "options": {"defaultType": "linear"},
        })

        if BYBIT_DEMO:
            exchange.urls["api"] = {
                "public":  "https://api-demo.bybit.com",
                "private": "https://api-demo.bybit.com",
            }

        balance   = exchange.fetch_balance()
        usdt      = balance.get("USDT", {}).get("free", 0)
        positions = exchange.fetch_positions()
        active    = [p for p in positions if float(p.get("contracts", 0)) > 0]

        mode = "DEMO" if BYBIT_DEMO else "LIVE"
        logger.info(f"\n📊 Bybit {mode} | USDT: ${usdt:.2f}")

        if active:
            logger.info("Відкриті позиції:")
            for p in active:
                logger.info(
                    f"  {p.get('side','').upper()} {p.get('symbol','')} | "
                    f"size={p.get('contracts',0)} | "
                    f"PnL=${float(p.get('unrealizedPnl', 0)):.2f}"
                )
        else:
            logger.info("Відкритих позицій немає")

    except Exception as e:
        logger.warning(f"Не вдалось отримати позиції: {e}")


def cmd_demo() -> None:
    """Запускає бота на demo рахунку Bybit."""
    if not BYBIT_DEMO:
        logger.error(
            "❌ Відмовлено: --demo, але BYBIT_DEMO=false. "
            "Зміни .env на BYBIT_DEMO=true."
        )
        return

    if not ACTIVE_API_KEY or not ACTIVE_API_SECRET:
        logger.error("❌ API ключі не встановлені!")
        logger.error("   Заповни BYBIT_DEMO_KEY і BYBIT_DEMO_SECRET в .env")
        return

    logger.info("🟡 Запуск в DEMO режимі (api-demo.bybit.com)")
    logger.info("   Зупинити: Ctrl+C\n")

    from core.live_trade import run_live_trader
    asyncio.run(run_live_trader())


def cmd_live() -> None:
    """Запускає бота на live рахунку."""
    if BYBIT_DEMO:
        logger.error("❌ BYBIT_DEMO=true в .env — встанови false для live торгівлі")
        return

    if not ACTIVE_API_KEY or not ACTIVE_API_SECRET:
        logger.error("❌ API ключі не встановлені!")
        return

    logger.warning("⚠️  LIVE РЕЖИМ — використовуються РЕАЛЬНІ ГРОШІ!")
    confirm = input("Введи 'LIVE' для підтвердження: ")
    if confirm != "LIVE":
        logger.info("Скасовано")
        return

    from core.live_trade import run_live_trader
    asyncio.run(run_live_trader())


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="Range Scalping Bot — Bybit Futures")
    parser.add_argument("--setup",  action="store_true", help="Ініціалізувати БД")
    parser.add_argument("--status", action="store_true", help="Статус позицій на Bybit")
    parser.add_argument("--demo",   action="store_true", help="Запустити demo торгівлю")
    parser.add_argument("--live",   action="store_true", help="Запустити live торгівлю")

    args = parser.parse_args()

    if args.setup:
        cmd_setup()
    elif args.status:
        cmd_status()
    elif args.demo:
        cmd_demo()
    elif args.live:
        cmd_live()
    else:
        print("\n📋 Використання:")
        print("  python main.py --setup    # Перший запуск: створити таблиці")
        print("  python main.py --status   # Статус позицій на Bybit")
        print("  python main.py --demo     # Запустити demo торгівлю")
        print("  python main.py --live     # Запустити live торгівлю\n")


if __name__ == "__main__":
    main()
