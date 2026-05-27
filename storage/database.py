"""
МЕНЕДЖЕР БАЗИ ДАНИХ
"""

import psycopg2
import psycopg2.extras
import pandas as pd
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from loguru import logger

from config.settings import DB_CONFIG
from storage.schema import CREATE_TABLES_SQL


class Database:

    def __init__(self):
        self.conn   = None
        self.cursor = None

    def connect(self) -> None:
        self.conn = psycopg2.connect(
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"],
            database=DB_CONFIG["database"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            cursor_factory=psycopg2.extras.RealDictCursor,
            options="-c client_encoding=UTF8",
        )
        self.conn.autocommit = False
        self.cursor = self.conn.cursor()
        logger.info("✅ З'єднано з PostgreSQL")
    

    def close(self) -> None:
        if self.cursor: self.cursor.close()
        if self.conn:   self.conn.close()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
            logger.error(f"❌ Rollback через помилку: {exc_val}")
        self.close()

    def initialize_tables(self) -> None:
        """Створює всі таблиці при першому запуску."""
        self.cursor.execute(CREATE_TABLES_SQL)
        self.conn.commit()
        logger.info("✅ Таблиці ініціалізовані")

    # ── OHLCV ──────────────────────────────────────────────

    def save_ohlcv_batch(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        candles: List[Dict],
    ) -> int:
        if not candles:
            return 0

        rows = [
            (
                exchange, symbol, timeframe,
                datetime.fromtimestamp(c["timestamp"] / 1000, tz=timezone.utc),
                str(c["open"]), str(c["high"]),
                str(c["low"]),  str(c["close"]),
                str(c["volume"]),
            )
            for c in candles
        ]

        psycopg2.extras.execute_values(
            self.cursor,
            """
            INSERT INTO ohlcv
                (exchange, symbol, timeframe, timestamp,
                 open, high, low, close, volume)
            VALUES %s
            ON CONFLICT (exchange, symbol, timeframe, timestamp)
            DO NOTHING
            """,
            rows,
            template="(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        )
        self.conn.commit()
        return self.cursor.rowcount

    def load_ohlcv(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        limit: Optional[int] = None,
        start_date: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """Завантажує OHLCV як pandas DataFrame."""
        q = """
            SELECT timestamp, open, high, low, close, volume
            FROM ohlcv
            WHERE exchange=%s AND symbol=%s AND timeframe=%s
        """
        params: list = [exchange, symbol, timeframe]

        if start_date:
            q += " AND timestamp >= %s"
            params.append(start_date)

        q += " ORDER BY timestamp ASC"
        if limit:
            q += f" LIMIT {limit}"

        self.cursor.execute(q, params)
        rows = self.cursor.fetchall()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df.set_index("timestamp", inplace=True)
        return df

    def get_last_candle_time(
        self, exchange: str, symbol: str, timeframe: str
    ) -> Optional[datetime]:
        self.cursor.execute(
            "SELECT MAX(timestamp) as t FROM ohlcv "
            "WHERE exchange=%s AND symbol=%s AND timeframe=%s",
            (exchange, symbol, timeframe),
        )
        r = self.cursor.fetchone()
        return r["t"] if r and r["t"] else None

    def get_candle_count(
        self, exchange: str, symbol: str, timeframe: str
    ) -> int:
        self.cursor.execute(
            "SELECT COUNT(*) as cnt FROM ohlcv "
            "WHERE exchange=%s AND symbol=%s AND timeframe=%s",
            (exchange, symbol, timeframe),
        )
        return self.cursor.fetchone()["cnt"]

    # ── ПРОГРЕС ЗАВАНТАЖЕННЯ ───────────────────────────────

    def update_fetch_progress(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        last_fetched_at: datetime,
        total_candles: int,
        is_complete: bool = False,
    ) -> None:
        self.cursor.execute(
            """
            INSERT INTO fetch_progress
                (exchange, symbol, timeframe,
                 last_fetched_at, total_candles, is_complete)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (exchange, symbol, timeframe) DO UPDATE SET
                last_fetched_at = EXCLUDED.last_fetched_at,
                total_candles   = EXCLUDED.total_candles,
                is_complete     = EXCLUDED.is_complete
            """,
            (exchange, symbol, timeframe,
             last_fetched_at, total_candles, is_complete),
        )
        self.conn.commit()

    def get_fetch_status(self) -> List[Dict]:
        self.cursor.execute("""
            SELECT exchange, symbol, timeframe,
                   last_fetched_at, total_candles, is_complete
            FROM fetch_progress
            ORDER BY exchange, symbol, timeframe
        """)
        return self.cursor.fetchall()

    # ── ІНДИКАТОРИ ─────────────────────────────────────────

    def save_indicators(self, data: Dict[str, Any]) -> None:
        self.cursor.execute(
            """
            INSERT INTO indicators
                (exchange, symbol, timeframe, timestamp,
                 vwap_session, vwap_weekly, vwap_bias,
                 poc, vah, val,
                 cvd, cvd_signal,
                 composite_signal, signal_strength)
            VALUES
                (%(exchange)s, %(symbol)s, %(timeframe)s, %(timestamp)s,
                 %(vwap_session)s, %(vwap_weekly)s, %(vwap_bias)s,
                 %(poc)s, %(vah)s, %(val)s,
                 %(cvd)s, %(cvd_signal)s,
                 %(composite_signal)s, %(signal_strength)s)
            ON CONFLICT (exchange, symbol, timeframe, timestamp) DO UPDATE SET
                vwap_session     = EXCLUDED.vwap_session,
                vwap_weekly      = EXCLUDED.vwap_weekly,
                vwap_bias        = EXCLUDED.vwap_bias,
                poc              = EXCLUDED.poc,
                vah              = EXCLUDED.vah,
                val              = EXCLUDED.val,
                cvd              = EXCLUDED.cvd,
                cvd_signal       = EXCLUDED.cvd_signal,
                composite_signal = EXCLUDED.composite_signal,
                signal_strength  = EXCLUDED.signal_strength
            """,
            data,
        )
        self.conn.commit()

    # ── УГОДИ ──────────────────────────────────────────────

    def save_trade(self, trade: Dict[str, Any]) -> None:
        self.cursor.execute(
            """
            INSERT INTO trades
                (trade_id, exchange, symbol, direction, mode,
                 entry_price, stop_loss, take_profit,
                 quantity, entry_reason, status, opened_at)
            VALUES
                (%(trade_id)s, %(exchange)s, %(symbol)s,
                 %(direction)s, %(mode)s,
                 %(entry_price)s, %(stop_loss)s, %(take_profit)s,
                 %(quantity)s, %(entry_reason)s,
                 %(status)s, %(opened_at)s)
            ON CONFLICT (trade_id) DO NOTHING
            """,
            trade,
        )
        self.conn.commit()

    def close_trade(
        self,
        trade_id: str,
        exit_price: float,
        pnl_usdt: float,
        pnl_pct: float,
        status: str = "closed",
    ) -> None:
        self.cursor.execute(
            """
            UPDATE trades SET
                exit_price = %s,
                pnl_usdt   = %s,
                pnl_pct    = %s,
                status     = %s,
                closed_at  = NOW()
            WHERE trade_id = %s
            """,
            (exit_price, pnl_usdt, pnl_pct, status, trade_id),
        )
        self.conn.commit()

    def get_daily_pnl(self, date_str: str) -> float:
        """Повертає P&L за конкретний день."""
        self.cursor.execute(
            """
            SELECT COALESCE(SUM(pnl_usdt), 0) as total
            FROM trades
            WHERE DATE(closed_at) = %s AND status = 'closed'
            """,
            (date_str,),
        )
        return float(self.cursor.fetchone()["total"])

    def print_summary(self) -> None:
        """Виводить зведену таблицю зібраних даних."""
        rows = self.get_fetch_status()
        if not rows:
            logger.warning("Даних ще немає. Запусти data_fetcher.")
            return

        print("\n" + "=" * 68)
        print(f"  {'Біржа':<10} {'Пара':<12} {'ТФ':<6} "
              f"{'Свічок':>8}  {'Остання дата':<20} {'✓'}")
        print("-" * 68)
        for s in rows:
            dt = (s["last_fetched_at"].strftime("%Y-%m-%d %H:%M")
                  if s["last_fetched_at"] else "—")
            ok = "✅" if s["is_complete"] else "⏳"
            print(f"  {s['exchange']:<10} {s['symbol']:<12} "
                  f"{s['timeframe']:<6} {s['total_candles']:>8,}  "
                  f"{dt:<20} {ok}")
        print("=" * 68 + "\n")
