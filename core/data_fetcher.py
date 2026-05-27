"""
ЗАВАНТАЖЕННЯ ДАНИХ — Bybit Futures
=====================================
Пари:       BTC/USDT:USDT, SOL/USDT:USDT (perpetual ф'ючерси)
Таймфрейми: 1m, 5m, 15m, 30m, 1h
Без API ключів — публічні дані.

ЛІМІТИ BYBIT (зберігання свічок):
  1m  → ~2 місяці
  5m  → ~6 місяців
  15m → ~12 місяців
  30m → ~12 місяців
  1h  → ~12 місяців
"""

import ccxt
import time
from datetime import datetime, timezone, timedelta
from typing import Dict
from tqdm import tqdm
from loguru import logger

from config.settings import (
    TRADING_PAIRS, TIMEFRAMES, BYBIT_HISTORY_MONTHS,
    CANDLES_PER_REQUEST,
)


class DataFetcher:

    PAUSE = 0.3  # пауза між запитами (сек) — Bybit rate limit

    def __init__(self):
        # Публічне підключення — без ключів, тільки для даних
        self.exchange = ccxt.bybit({
            "enableRateLimit": True,
            "options": {
                "defaultType": "linear",  # USDT perpetual ф'ючерси
            },
        })
        logger.info("✅ CCXT підключено: Bybit Futures (публічний API)")
        logger.info(f"   Пари: {TRADING_PAIRS}")

    def fetch_all(self, db) -> None:
        """
        Завантажує дані для всіх пар і таймфреймів.
        Враховує ліміти зберігання Bybit для кожного ТФ.
        Продовжує з останньої збереженої дати.
        """
        now    = datetime.now(timezone.utc)
        combos = []

        for symbol in TRADING_PAIRS:
            for tf_key, tf in TIMEFRAMES.items():
                months = BYBIT_HISTORY_MONTHS[tf]
                start  = now - timedelta(days=months * 30)
                combos.append((symbol, tf, start))

        logger.info(f"📥 Починаємо збір: {len(combos)} комбінацій")
        logger.info("   ⚠️  1m дані: тільки ~2 місяці (ліміт Bybit)\n")

        for symbol, timeframe, default_start in tqdm(combos, desc="Збір даних"):
            last  = db.get_last_candle_time("bybit", symbol, timeframe)
            since = last if last else default_start

            logger.info(
                f"\n▶ BYBIT {symbol} {timeframe} "
                f"(з {since.strftime('%Y-%m-%d %H:%M')})"
            )

            count = self._fetch_paginated(
                db=db,
                symbol=symbol,
                timeframe=timeframe,
                since=since,
            )

            total = db.get_candle_count("bybit", symbol, timeframe)
            db.update_fetch_progress(
                exchange="bybit", symbol=symbol, timeframe=timeframe,
                last_fetched_at=datetime.now(timezone.utc),
                total_candles=total, is_complete=True,
            )
            logger.info(f"   ✅ +{count:,} нових | всього: {total:,}")

        logger.info("\n🎉 Збір даних завершено!")
        db.print_summary()

    def update(self, db) -> None:
        """Оновлює тільки нові свічки. Запускати регулярно."""
        logger.info("🔄 Оновлення актуальних даних (Bybit)...")
        for symbol in TRADING_PAIRS:
            for tf in TIMEFRAMES.values():
                last = db.get_last_candle_time("bybit", symbol, tf)
                if last:
                    count = self._fetch_paginated(db, symbol, tf, last)
                    if count > 0:
                        logger.info(f"   ↑ {symbol} {tf}: +{count} свічок")
        logger.info("✅ Оновлення завершено")

    def _fetch_paginated(
        self,
        db,
        symbol: str,
        timeframe: str,
        since: datetime,
    ) -> int:
        """
        Пагінація свічок з Bybit.
        Повертає кількість нових збережених свічок.
        """
        since_ms  = int(since.timestamp() * 1000)
        now_ms    = int(datetime.now(timezone.utc).timestamp() * 1000)
        tf_ms     = self._tf_to_ms(timeframe)
        total_new = 0

        while since_ms < now_ms:
            try:
                raw = self.exchange.fetch_ohlcv(
                    symbol=symbol,
                    timeframe=timeframe,
                    since=since_ms,
                    limit=CANDLES_PER_REQUEST,
                )
            except ccxt.NetworkError as e:
                logger.warning(f"Мережева помилка, чекаємо 15с: {e}")
                time.sleep(15)
                continue
            except ccxt.ExchangeError as e:
                logger.error(f"Помилка Bybit {symbol} {timeframe}: {e}")
                break
            except Exception as e:
                logger.error(f"Невідома помилка: {e}")
                break

            if not raw:
                break

            # Відкидаємо поточну незакриту свічку
            candles = [
                {
                    "timestamp": c[0], "open": c[1], "high": c[2],
                    "low": c[3],       "close": c[4], "volume": c[5],
                }
                for c in raw
                if c[0] < now_ms - tf_ms
            ]

            if candles:
                saved      = db.save_ohlcv_batch("bybit", symbol, timeframe, candles)
                total_new += saved

            since_ms = raw[-1][0] + 1
            time.sleep(self.PAUSE)

        return total_new

    @staticmethod
    def _tf_to_ms(tf: str) -> int:
        units = {
            "m": 60_000,
            "h": 3_600_000,
            "d": 86_400_000,
            "w": 604_800_000,
        }
        return int(tf[:-1]) * units[tf[-1]]