"""
КАЛЬКУЛЯТОР РИЗИКУ — NET-ризик з комісіями та slippage.

Вхід і захисний SL моделюються як taker-виконання зі slippage у
гірший бік. Limit-TP не має slippage, але для fail-safe RR комісія
моделюється як taker: triggered limit може одразу забрати ліквідність.
Фактичний maker-fill лише покращить результат. Усі грошові
розрахунки виконуються через Decimal.
"""

from decimal import Decimal, ROUND_DOWN
from loguru import logger

from config.settings import (
    MIN_RISK_REWARD,
    MAX_DAILY_LOSS_PCT,
    MAX_TRADES_PER_DAY,
    MAX_CONSECUTIVE_LOSSES,
    BYBIT_TAKER_FEE,
    BYBIT_MAKER_FEE,
    BTC_SLIPPAGE_PCT,
    SOL_SLIPPAGE_PCT,
)


# ── Константи (тепер із settings/.env, не хардкоди) ────────

BYBIT_TAKER = Decimal(str(BYBIT_TAKER_FEE))   # вхід і SL — маркет (taker)
BYBIT_MAKER = Decimal(str(BYBIT_MAKER_FEE))   # telemetry/reference

SLIPPAGE = {
    "BTC/USDT:USDT": Decimal(str(BTC_SLIPPAGE_PCT)),
    "SOL/USDT:USDT": Decimal(str(SOL_SLIPPAGE_PCT)),
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
    *,
    max_notional: Decimal | None = None,
    max_leverage: Decimal | None = None,
    entry_is_filled: bool = False,
) -> dict:
    """
    Розраховує розмір позиції і реальний P&L з комісіями.

    min_rr — поріг NET RR для цієї КОНКРЕТНОЇ угоди. Кожна стратегія має
    свій профіль: sweep/breakout очікує RR≥1.5, а mean-reversion свідомо
    торгує з RR≈0.8-1.0 але високим win-rate. Якщо None — береться
    глобальний MIN_RR (= settings.MIN_RISK_REWARD).

    Розмір позиції рахується від NET-збитку на одиницю активу:
    рух до SL після slippage + taker-комісія входу + taker-комісія
    SL. Отримана quantity округлюється вниз до qty_step, тому
    фактичний NET-збиток не може перевищити ``deposit * risk_pct``.

    max_notional обмежує номінал позиції, а max_leverage — номінал
    до ``deposit * max_leverage``. Обидва аргументи опційні; за замовчуванням
    поведінка викликів залишається сумісною.

    entry_is_filled=True використовується лише для post-fill safety recheck:
    ``entry_price`` тоді вже є фактичним fill, тому повторний entry-slippage
    не додається. Entry fee та adverse slippage захисного SL лишаються.

    Повертає dict або {"error": "..."} якщо позиція замала.
    """
    cfg      = SYMBOL_CFG[symbol]
    slip     = SLIPPAGE[symbol]
    risk_usd = deposit * risk_pct
    rr_floor = Decimal(str(min_rr)) if min_rr is not None else MIN_RR

    if deposit <= 0 or risk_pct <= 0 or risk_usd <= 0:
        return {"error": "Депозит і risk_pct мають бути додатними"}
    if entry_price <= 0 or stop_loss <= 0 or take_profit <= 0:
        return {"error": "Ціни entry, SL і TP мають бути додатними"}

    # Реальні ціни. Вхід і SL — маркет (сліпедж у гірший бік).
    # TP — ЛІМІТНИЙ ордер: виконується рівно за своєю ціною, БЕЗ сліпеджу.
    is_long = entry_price < take_profit

    if is_long and stop_loss >= entry_price:
        return {"error": "Для LONG стоп має бути нижче ціни входу"}
    if not is_long and (take_profit >= entry_price or stop_loss <= entry_price):
        return {"error": "Для SHORT TP має бути нижче, а SL — вище ціни входу"}

    if is_long:
        real_entry    = entry_price if entry_is_filled else entry_price * (1 + slip)
        real_exit_tp  = take_profit                 # ліміт = точна ціна
        real_exit_sl  = stop_loss    * (1 - slip)
    else:
        real_entry    = entry_price if entry_is_filled else entry_price * (1 - slip)
        real_exit_tp  = take_profit                 # ліміт = точна ціна
        real_exit_sl  = stop_loss    * (1 + slip)

    # NET-збиток на 1 монету вже містить slippage в цінах
    # виконання і обидві taker-комісії. Саме від нього, а не від
    # gross SL-дистанції, рахуємо безпечну quantity.
    if is_long:
        gross_loss_per_unit = real_entry - real_exit_sl
    else:
        gross_loss_per_unit = real_exit_sl - real_entry
    loss_per_unit = (
        gross_loss_per_unit
        + real_entry * BYBIT_TAKER
        + real_exit_sl * BYBIT_TAKER
    )
    if loss_per_unit <= 0:
        return {"error": "Неможливо розрахувати додатний NET-ризик"}

    raw_qty = risk_usd / loss_per_unit

    # Опційні жорсткі обмеження експозиції. Ліміт leverage
    # перетворюється на максимальний номінал від поточного deposit.
    notional_cap = None
    if max_notional is not None:
        max_notional = Decimal(str(max_notional))
        if max_notional <= 0:
            return {"error": "max_notional має бути додатним"}
        notional_cap = max_notional
    if max_leverage is not None:
        max_leverage = Decimal(str(max_leverage))
        if max_leverage <= 0:
            return {"error": "max_leverage має бути додатним"}
        leverage_cap = deposit * max_leverage
        notional_cap = (
            leverage_cap if notional_cap is None
            else min(notional_cap, leverage_cap)
        )
    if notional_cap is not None:
        raw_qty = min(raw_qty, notional_cap / entry_price)

    # Округляємо тільки вниз до кроку біржі.
    step = cfg["qty_step"]
    quantity = (raw_qty / step).to_integral_value(rounding=ROUND_DOWN) * step

    # Захисна перевірка інваріанта після біржового floor. Decimal-математика
    # має завжди пройти цю умову; додатковий крок захищає від
    # майбутніх змін точності/округлення.
    if quantity * loss_per_unit > risk_usd:
        quantity -= step

    if quantity < cfg["min_qty"]:
        return {
            "error": (f"Позиція {max(quantity, Decimal('0'))} менша за мінімум "
                      f"{cfg['min_qty']} після NET-risk/notional обмежень.")
        }

    pos_value = quantity * entry_price

    # Комісії: вхід/SL taker. Triggered limit-TP консервативно теж taker:
    # біржа не гарантує maker liquidity для такого виконання.
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

    if net_loss > risk_usd:
        # Fail closed: ніколи не повертаємо quantity, що перевищує
        # заданий власником risk budget.
        return {"error": "NET-ризик після округлення перевищує risk budget"}

    rr = net_profit / net_loss if net_loss > 0 else Decimal("0")

    # Мінімальний рух для беззбитку (вхід taker+slip, TP worst-case taker)
    total_cost_pct  = BYBIT_TAKER + BYBIT_TAKER + (
        Decimal("0") if entry_is_filled else slip
    )
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
        "slippage_cost":   (
            pos_value * slip * (Decimal("1") if entry_is_filled else Decimal("2"))
        ).quantize(Decimal("0.0001")),
        "risk_usdt":       net_loss.quantize(Decimal("0.01")),
        "reward_usdt":     net_profit.quantize(Decimal("0.01")),
        "rr_ratio":        rr.quantize(Decimal("0.01")),
        "rr_ok":           rr >= rr_floor,
        "rr_floor":        rr_floor,
        "risk_budget":     risk_usd,
        "max_notional":    notional_cap,
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

    if daily_pnl <= -max_daily_loss:
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
