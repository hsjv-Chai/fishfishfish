import math
from decimal import Decimal, ROUND_DOWN


def units(amount: float) -> int:
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("invalid fish amount")
    return int((Decimal(str(amount)) * 10000).to_integral_value(rounding=ROUND_DOWN))


def settle(stake_units: int, entry: float, exit_price: float,
           leverage: int = 1, fee: float = .0002) -> int:
    """Match the website's IEEE-754 arithmetic and final floor, in storage units."""
    if stake_units < 0 or leverage not in (1, 2, 3, 5, 10, 20, 100):
        raise ValueError("invalid stake/leverage")
    if not all(math.isfinite(x) and x > 0 for x in (entry, exit_price)):
        raise ValueError("invalid price")
    ratio = exit_price / entry
    notional = stake_units * leverage
    return max(0, math.floor(stake_units + notional * (ratio - 1) - notional * ratio * fee))


def size(equity_units: int, cash_units: int, distance: float) -> int:
    if not .01 <= distance <= .04:
        return 0
    fish = min(.0025 * (equity_units / 10000) / (distance + .0002 + .001),
               .2 * (equity_units / 10000), cash_units / 10000)
    n = units(fish)
    return n if n >= 10000 else 0
