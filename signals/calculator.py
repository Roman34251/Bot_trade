"""
КАЛЬКУЛЯТОР РИЗИКУ — маркет ордери з slippage
===============================================
Завжди використовуй Decimal для цін і комісій.
float дає похибку при розрахунку комісій → хибний P&L.

Схема витрат на одну угоду (маркет ордер Bybit):
  Вхід:  taker 0.055% + slippage BTC 0.03% / SOL 0.05%
  Вихід: taker 0.055% + slippage BTC 0.03% / SOL 0.05%
  Разом: BTC ~0.17% / SOL ~0.21% від розміру позиції

  При $25 позиції BTC: $25 × 0.17% = $0.043 за угоду
  Мінімальний рух для беззбитковості BTC: 0.17% від ціни
  При BTC $95,000: мінімум $161.5 руху ціни
"""

from decimal import Decimal, ROUND_DOWN
from loguru import logger

from config.settings import (
    MIN_RISK_REWARD,
    MAX_DAILY_LOSS_PCT,
    MAX_TRADES_PER_DAY,
    MAX_CONSECUTIVE_LOSSES,
)


# ── Константи ─────────────────────────────────────────────

BYBIT_TAKER = Decimal("0.00055")   # 0.055% — маркет завжди taker

SLIPPAGE = {
    "BTC/USDT:USDT": Decimal("0.0003"),   # 0.03%
    "SOL/USDT:USDT": Decimal("0.0005"),   # 0.05%
}

SYMBOL_CFG = {
    "BTC/USDT:USDT": {
        "min_qty":  Decimal("0.001"),
        "qty_step": Decimal("0.001"),
        "sl_atr_mult": Decimal("1.5"),
    },
    "SOL/USDT:USDT": {
        "min_qty":  Decimal("0.1"),
        "qty_step": Decimal("0.1"),
        "sl_atr_mult": Decimal("2.0"),
    },
}

# NET RR поріг виконавця. Раніше тут був хардкод 2.0, через що ОДНА ця
# перевірка тихо відсікала майже всі сигнали (generator віддає gross RR ~1.5-3,
# а після комісій net падає нижче 2.0). Тепер береться з settings.MIN_RISK_REWARD.
MIN_RR = Decimal(str(MIN_RISK_REWARD))


def calculate_position(
    symbol:      str,
    deposit:     Decimal,
    risk_pct:    Decimal,
    entry_price: Decimal,
    stop_loss:   Decimal,
    take_profit: Decimal,
    min_rr:      Decimal | None = None,
) -> dict:
    """
    Розраховує розмір позиції і реальний P&L з комісіями.

    min_rr — поріг NET RR для цієї КОНКРЕТНОЇ угоди. Кожна стратегія має
    свій профіль: sweep/breakout очікує RR≥1.5, а mean-reversion свідомо
    торгує з RR≈0.8-1.0 але високим win-rate. Якщо None — береться
    глобальний MIN_RR (= settings.MIN_RISK_REWARD).

    Алгоритм:
      1. risk_usdt = deposit × risk_pct          ($500 × 1% = $5)
      2. sl_distance = |entry - stop_loss|
      3. qty = risk_usdt / sl_distance            (округлення вниз)
      4. real_entry = entry ± slippage            (маркет гірша ціна)
      5. real_exit  = tp/sl ∓ slippage            (маркет гірша ціна)
      6. fee_in  = qty × real_entry × 0.055%
      7. fee_out = qty × real_exit  × 0.055%
      8. net_profit = gross_profit - fee_in - fee_out
      9. net_loss   = gross_loss   + fee_in + fee_out
     10. rr = net_profit / net_loss

    Повертає dict або {"error": "..."} якщо позиція замала.
    """
    cfg      = SYMBOL_CFG[symbol]
    slip     = SLIPPAGE[symbol]
    risk_usd = deposit * risk_pct
    rr_floor = Decimal(str(min_rr)) if min_rr is not None else MIN_RR

    sl_dist = abs(entry_price - stop_loss)
    if sl_dist == 0:
        return {"error": "SL дорівнює ціні входу"}

    # Розмір позиції в монетах (округлюємо вниз до qty_step)
    raw_qty  = risk_usd / sl_dist
    step     = cfg["qty_step"]
    quantity = (raw_qty // step) * step

    if quantity < cfg["min_qty"]:
        return {
            "error": (f"Позиція {quantity} менша за мінімум "
                      f"{cfg['min_qty']}. "
                      f"Збільш ризик або зменш SL дистанцію.")
        }

    pos_value = quantity * entry_price

    # Реальні ціни з slippage (маркет ордер — завжди гірша ціна)
    is_long = entry_price < take_profit

    if is_long:
        real_entry    = entry_price  * (1 + slip)   # купуємо дорожче
        real_exit_tp  = take_profit  * (1 - slip)   # продаємо дешевше
        real_exit_sl  = stop_loss    * (1 - slip)
    else:
        real_entry    = entry_price  * (1 - slip)   # продаємо дешевше
        real_exit_tp  = take_profit  * (1 + slip)   # купуємо дорожче
        real_exit_sl  = stop_loss    * (1 + slip)

    # Комісії (taker обидва боки)
    fee_in     = quantity * real_entry   * BYBIT_TAKER
    fee_out_tp = quantity * real_exit_tp * BYBIT_TAKER
    fee_out_sl = quantity * real_exit_sl * BYBIT_TAKER

    # Gross P&L
    if is_long:
        gross_profit = (real_exit_tp - real_entry) * quantity
        gross_loss   = (real_entry   - real_exit_sl) * quantity
    else:
        gross_profit = (real_entry   - real_exit_tp) * quantity
        gross_loss   = (real_exit_sl - real_entry)   * quantity

    net_profit = gross_profit - fee_in - fee_out_tp
    net_loss   = gross_loss   + fee_in + fee_out_sl

    rr = net_profit / net_loss if net_loss > 0 else Decimal("0")

    # Мінімальний рух для беззбитку
    total_cost_pct  = (BYBIT_TAKER + slip) * 2
    breakeven_price = entry_price * total_cost_pct

    result = {
        "symbol":          symbol,
        "quantity":        quantity,
        "position_value":  pos_value.quantize(Decimal("0.01")),
        "entry_price":     entry_price,
        "stop_loss":       stop_loss,
        "take_profit":     take_profit,
        "real_entry":      real_entry.quantize(Decimal("0.01")),
        "fee_total":       (fee_in + fee_out_tp).quantize(Decimal("0.0001")),
        "slippage_cost":   (pos_value * slip * 2).quantize(Decimal("0.0001")),
        "risk_usdt":       net_loss.quantize(Decimal("0.01")),
        "reward_usdt":     net_profit.quantize(Decimal("0.01")),
        "rr_ratio":        rr.quantize(Decimal("0.01")),
        "rr_ok":           rr >= rr_floor,
        "rr_floor":        rr_floor,
        "breakeven_move":  breakeven_price.quantize(Decimal("0.01")),
        "order_type":      "MARKET",
    }

    logger.debug(
        f"Position {symbol}: qty={quantity} pos=${pos_value:.2f} | "
        f"risk=${net_loss:.2f} reward=${net_profit:.2f} RR={rr:.2f} "
        f"(floor {rr_floor}) {'✅' if rr >= rr_floor else '❌'}"
    )

    return result


def check_daily_limits(
    daily_pnl:    Decimal,
    trade_count:  int,
    loss_streak:  int,
    deposit:      Decimal,
) -> dict:
    """
    Перевіряє чи можна відкривати нову угоду.

    Ліміти:
      daily_loss > 3% депозиту → стоп на день
      trade_count > 12         → стоп на день
      loss_streak >= 3         → пауза 60 хвилин
    """
    max_daily_loss = deposit * Decimal(str(MAX_DAILY_LOSS_PCT))
    blocked        = False
    reasons        = []

    if daily_pnl < -max_daily_loss:
        blocked = True
        reasons.append(f"Денний збиток ${abs(daily_pnl):.2f} > ліміт ${max_daily_loss:.2f}")

    if trade_count >= MAX_TRADES_PER_DAY:
        blocked = True
        reasons.append(f"Максимум угод на день ({trade_count}/{MAX_TRADES_PER_DAY})")

    if loss_streak >= MAX_CONSECUTIVE_LOSSES:
        blocked = True
        reasons.append(f"Серія збитків {loss_streak}/{MAX_CONSECUTIVE_LOSSES} — пауза")

    return {
        "can_trade": not blocked,
        "reasons":   reasons,
        "daily_pnl": daily_pnl,
        "trade_count": trade_count,
        "loss_streak": loss_streak,
    }