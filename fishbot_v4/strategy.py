"""Closed-candle slow trend signal: 4h regime + 55-bar entry and exit."""
from dataclasses import dataclass

from fishbot.strategy import atr, ema


@dataclass(frozen=True)
class Signal:
    time: int
    close: float
    atr: float
    distance: float
    up: bool
    down: bool
    breakout: bool
    breakdown: bool
    exit_long: bool
    exit_short: bool

    @property
    def entry_direction(self):
        if self.up and self.breakout:
            return 1
        if self.down and self.breakdown:
            return -1
        return 0


def signal(hourly, four_hourly, breakout=55, exit_breakout=55):
    if breakout != 55 or exit_breakout != 55:
        raise ValueError("V4 freezes 55-bar entry/exit")
    if len(hourly) < 1000 or len(four_hourly) < 1000:
        raise ValueError("1000 closed candles per interval required")
    closes = [c.close for c in four_hourly]
    e50, e200 = ema(closes, 50), ema(closes, 200)
    up = closes[-1] > e200 and e50 > e200
    down = closes[-1] < e200 and e50 < e200
    last = hourly[-1]
    a = atr(hourly)
    return Signal(
        last.time,
        last.close,
        a,
        max(2 * a / last.close, .01),
        up,
        down,
        last.close > max(c.high for c in hourly[-breakout - 1:-1]),
        last.close < min(c.low for c in hourly[-breakout - 1:-1]),
        last.close < min(c.low for c in hourly[-exit_breakout - 1:-1]),
        last.close > max(c.high for c in hourly[-exit_breakout - 1:-1]),
    )
