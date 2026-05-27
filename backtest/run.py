"""
ЗАПУСК БЕКТЕСТУ
================
Завантажує дані з PostgreSQL і запускає бектест.

Використання:
    python -m backtest.run
    python -m backtest.run --symbol BTC/USDT:USDT --tf 1h
    python -m backtest.run --symbol SOL/USDT:USDT --tf 15m
"""

import argparse
from loguru import logger

from storage.database import Database
from backtest.engine  import BacktestEngine

SYMBOLS    = ["BTC/USDT:USDT", "SOL/USDT:USDT"]
TIMEFRAMES = ["1h", "15m", "5m"]


def run_backtest(symbol: str, timeframe: str) -> None:
    logger.info(f"\n{'='*55}")
    logger.info(f"Запуск бектесту: {symbol} {timeframe}")
    logger.info(f"{'='*55}")

    with Database() as db:
        count = db.get_candle_count("bybit", symbol, timeframe)
        if count == 0:
            logger.error(
                f"❌ Немає даних для {symbol} {timeframe}. "
                f"Запусти: python main.py --fetch"
            )
            return

        logger.info(f"📦 Завантажуємо {count:,} свічок...")
        df = db.load_ohlcv(
            exchange  = "bybit",
            symbol    = symbol,
            timeframe = timeframe,
        )

    if df.empty:
        logger.error("DataFrame порожній")
        return

    logger.info(f"✅ Дані завантажено: {len(df):,} рядків")

    engine = BacktestEngine()
    results = engine.run(df, symbol=symbol)

    if results:
        engine.print_report(results)

        # Зберігаємо equity curve для аналізу
        if "equity_curve" in results:
            curve = results["equity_curve"]
            logger.info(
                f"📈 Equity curve: "
                f"від ${curve.iloc[0]:.2f} до ${curve.iloc[-1]:.2f}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Запуск бектесту")
    parser.add_argument(
        "--symbol", default="BTC/USDT:USDT",
        choices=SYMBOLS,
        help="Торгова пара"
    )
    parser.add_argument(
        "--tf", default="1h",
        choices=TIMEFRAMES,
        help="Таймфрейм"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Запустити бектест для всіх пар і таймфреймів"
    )
    args = parser.parse_args()

    if args.all:
        for sym in SYMBOLS:
            for tf in TIMEFRAMES:
                run_backtest(sym, tf)
    else:
        run_backtest(args.symbol, args.tf)


if __name__ == "__main__":
    main()
