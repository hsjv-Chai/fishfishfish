import math
from decimal import Decimal, ROUND_DOWN

DIRECTIONS = ("long", "short")
MIN_LEVERAGE = 1
MAX_LEVERAGE = 100


def units(amount: float) -> int:
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("invalid fish amount")
    return int((Decimal(str(amount)) * 10000).to_integral_value(rounding=ROUND_DOWN))


def _leverage(value) -> int:
    # The site accepts every integer in 1..100, not a fixed whitelist.
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_LEVERAGE <= value <= MAX_LEVERAGE:
        raise ValueError("invalid leverage")
    return value


def settle(stake_units: int, entry: float, exit_price: float,
           leverage: int = 1, fee: float = .0002, direction: str = "long") -> int:
    """Match the website's direction-aware IEEE-754 arithmetic and final floor."""
    leverage = _leverage(leverage)
    if direction not in DIRECTIONS:
        raise ValueError("invalid direction")
    if stake_units < 0:
        raise ValueError("invalid stake")
    if not all(math.isfinite(x) and x > 0 for x in (entry, exit_price)):
        raise ValueError("invalid price")
    ratio = exit_price / entry
    notional = stake_units * leverage
    move = 1 - ratio if direction == "short" else ratio - 1
    return max(0, math.floor(stake_units + notional * move - notional * ratio * fee))


def liquidation_price(entry: float, leverage: int = 1, direction: str = "long") -> float:
    """Price at which the position's margin reaches zero.

    A 1x long has no reachable liquidation price (0); every short does, including
    1x (2x entry). The website stores this fixed value at entry.
    """
    leverage = _leverage(leverage)
    if direction not in DIRECTIONS:
        raise ValueError("invalid direction")
    if not math.isfinite(entry) or entry <= 0:
        raise ValueError("invalid price")
    if direction == "short":
        return entry * (1 + 1 / leverage)
    if leverage == 1:
        return 0.0
    return entry * (1 - 1 / leverage)


def size(equity_units: int, cash_units: int, distance: float, leverage: int = 1,
         risk: float = .0025, allocation: float = .2) -> int:
    if not .01 <= distance <= .04:
        return 0
    leverage = _leverage(leverage)
    # Leverage multiplies both the stop loss and the closing fee on notional.
    fish = min(risk * (equity_units / 10000) / (leverage * (distance + .0002 + .001)),
               allocation * (equity_units / 10000), cash_units / 10000)
    n = units(fish)
    return n if n >= 10000 else 0
