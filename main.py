"""
ГОЛОВНИЙ ФАЙЛ ЗАПУСКУ
======================
Запускай цей файл щоб:
  python main.py --setup   → ініціалізувати базу даних
  python main.py --fetch   → завантажити 5 років даних
  python main.py --update  → оновити до поточного моменту
  python main.py --status  → показати статус зібраних даних
"""

import sys
import argparse
from loguru import logger

from config.settings import LOG_LEVEL, LOG_FILE, LOG_ROTATION, LOG_RETENTION
from storage.database import Database
from core.data_fetcher import DataFetcher


def setup_logging() -> None:
    """Налаштовує loguru: консоль + файл."""
    logger.remove()  # прибираємо дефолтний хендлер

    # Консоль — кольоровий вивід
    logger.add(
        sys.stdout,
        level=LOG_LEVEL,
        format="<green>{time:HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{message}</cyan>",
        colorize=True,
    )

    # Файл — детальні логи з ротацією
    logger.add(
        LOG_FILE,
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
        rotation=LOG_ROTATION,
        retention=LOG_RETENTION,
        encoding="utf-8",
    )


def cmd_setup() -> None:
    """Ініціалізує таблиці в базі даних."""
    logger.info("🔧 Ініціалізація бази даних...")
    with Database() as db:
        db.initialize_tables()
    logger.info("✅ База даних готова!")
    logger.info("   Наступний крок: python main.py --fetch")


def cmd_fetch() -> None:
    """Завантажує 5 років OHLCV даних."""
    logger.info("📥 Запуск збору даних (це може зайняти 10-30 хвилин)...")
    logger.info("   Не закривай термінал. Дані зберігаються в PostgreSQL.")
    logger.info("   Якщо перерветься — запусти знову, продовжить з місця зупинки.\n")

    fetcher = DataFetcher()
    with Database() as db:
        fetcher.fetch_all(db)


def cmd_update() -> None:
    """Оновлює дані до поточного моменту."""
    fetcher = DataFetcher()
    with Database() as db:
        fetcher.update(db)


def cmd_status() -> None:
    """Показує статус зібраних даних."""
    with Database() as db:
        db.print_summary()


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Quant Bot — інструмент управління даними"
    )
    parser.add_argument("--setup",  action="store_true", help="Ініціалізувати БД")
    parser.add_argument("--fetch",  action="store_true", help="Завантажити 5 років даних")
    parser.add_argument("--update", action="store_true", help="Оновити актуальні дані")
    parser.add_argument("--status", action="store_true", help="Показати статус даних")

    args = parser.parse_args()

    if args.setup:
        cmd_setup()
    elif args.fetch:
        cmd_fetch()
    elif args.update:
        cmd_update()
    elif args.status:
        cmd_status()
    else:
        print("\n📋 Використання:")
        print("  python main.py --setup   # Перший запуск: створити таблиці")
        print("  python main.py --fetch   # Завантажити 5 років даних")
        print("  python main.py --update  # Оновити до поточного моменту")
        print("  python main.py --status  # Показати скільки даних є\n")


if __name__ == "__main__":
    main()
