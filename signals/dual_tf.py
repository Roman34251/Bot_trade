"""
DUAL TF — DISABLED / UNDER REWORK
=================================

Цей модуль тимчасово вимкнений.

Причина:
  Стара dual_tf стратегія дублювала generator.py, бо теж торгувала
  range liquidity sweep. Зараз основна стратегія — generator.py.

План:
  Пізніше переписати dual_tf у відмінну допоміжну стратегію:
  trend breakout continuation.

Поки функція generate_dual_tf_signal() повертає None,
щоб live_trade.py міг запускатися без ImportError.
"""

from __future__ import annotations

from typing import Optional
import pandas as pd
from loguru import logger


def generate_dual_tf_signal(
    df_1h: pd.DataFrame,
    df_30m: pd.DataFrame,
    df_5m: pd.DataFrame,
    df_1m: pd.DataFrame,
    symbol: str,
    cached_1h_range: dict | None = None,
    cached_30m_range: dict | None = None,
) -> Optional[dict]:
    """
    Тимчасово вимкнена dual_tf стратегія.

    Повертає None, щоб бот використовував тільки generator.py.
    """
    logger.debug(f"{symbol} [dual_tf]: disabled / under rework — skip")
    return None