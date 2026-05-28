#Usage:
    #python -m backtest.run
    #python -m backtest.run --symbol BTC/USDT:USDT
    #python -m backtest.run --symbol BTC/USDT:USDT --start 2026-04-01 --end 2026-05-01
    #python -m backtest.run --mode legacy --tf 1h


import argparse
from datetime import timedelta

import pandas as pd
from loguru import logger

from backtest.range_engine import RangeDeviationBacktestEngine

from storage.database import Database


SYMBOLS = ["BTC/USDT:USDT", "SOL/USDT:USDT"]
TIMEFRAMES = ["1m", "5m", "15m", "1h"]
MTF_TIMEFRAMES = ["1m", "5m", "15m", "1h"]
WARMUP_DAYS = 10


def _parse_utc(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None

    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")

    return ts.tz_convert("UTC")


def _load_frame(
    db: Database,
    symbol: str,
    timeframe: str,
    start: pd.Timestamp | None,
    end: pd.Timestamp | None,
) -> pd.DataFrame:
    load_start = start - timedelta(days=WARMUP_DAYS) if start is not None else None

    df = db.load_ohlcv(
        exchange="bybit",
        symbol=symbol,
        timeframe=timeframe,
        start_date=load_start.to_pydatetime() if load_start is not None else None,
    )

    if df.empty:
        return df

    if end is not None:
        df = df[df.index <= end]

    return df




def run_mtf_backtest(
    symbol: str,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> None:
    logger.info(f"\n{'=' * 70}")
    logger.info(f"Multi-timeframe backtest: {symbol} | 1h -> 15m -> 5m -> 1m")
    logger.info(f"{'=' * 70}")

    data: dict[str, pd.DataFrame] = {}

    with Database() as db:
        for timeframe in MTF_TIMEFRAMES:
            df = _load_frame(db, symbol, timeframe, start, end)

            if df.empty:
                logger.error(
                    f"No data for {symbol} {timeframe}. Run: python main.py --fetch"
                )
                return

            data[timeframe] = df
            logger.info(
                f"{timeframe:<3} {len(df):>8,} candles | "
                f"{df.index[0]} -> {df.index[-1]}"
            )

    engine = RangeDeviationBacktestEngine()
    results = engine.run(data, symbol=symbol, start_date=start, end_date=end)

    if results:
        engine.print_report(results)
        curve = results.get("equity_curve")
        if curve is not None and len(curve) > 0:
            logger.info(f"Equity curve: ${curve.iloc[0]:.2f} -> ${curve.iloc[-1]:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest runner")
    parser.add_argument("--symbol", default="BTC/USDT:USDT", choices=SYMBOLS)
    parser.add_argument("--tf", default="1h", choices=TIMEFRAMES)
    parser.add_argument("--mode", default="mtf", choices=["mtf", "legacy"])
    parser.add_argument("--start", help="UTC start, example: 2026-04-01")
    parser.add_argument("--end", help="UTC end, example: 2026-05-01")

    args = parser.parse_args()

    start = _parse_utc(args.start)
    end = _parse_utc(args.end)

   
    run_mtf_backtest(args.symbol, start, end)


if __name__ == "__main__":
    main()