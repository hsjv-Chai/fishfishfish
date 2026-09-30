from dataclasses import dataclass
import math

HOUR = 3_600_000


@dataclass(frozen=True)
class Candle:
    time: int  # UTC open time in milliseconds
    open: float
    high: float
    low: float
    close: float
    volume: float = 0

    @classmethod
    def row(cls, row):
        t = int(row[0])
        if t > 10**14:  # Binance public archives switched to microseconds in 2025
            t //= 1000
        return cls(t, *(float(v) for v in row[1:6]))


def closed_candles(rows, interval: int, now: int, minimum: int = 1000):
    result = [c for c in rows if c.time + interval <= now]
    if len(result) < minimum:
        raise ValueError(f"need {minimum} closed candles, got {len(result)}")
    for i, c in enumerate(result):
        if (c.time % interval or not all(math.isfinite(v) and v > 0 for v in
                (c.open, c.high, c.low, c.close)) or c.volume < 0 or
                not math.isfinite(c.volume) or
                not (c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high)):
            raise ValueError("invalid OHLC candle")
        if i and c.time != result[i - 1].time + interval:
            raise ValueError("missing/duplicate/out-of-order candle")
    if result[-1].time + interval != (now // interval) * interval:
        raise ValueError("candles have not reached latest closed interval")
    return result


def ema(values, period):
    if len(values) < period:
        raise ValueError("insufficient EMA warmup")
    value = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for x in values[period:]:
        value += alpha * (x - value)
    return value


def atr(candles, period=14):
    if len(candles) < period + 1:
        raise ValueError("insufficient ATR warmup")
    ranges = [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
              for a, b in zip(candles, candles[1:])]
    value = sum(ranges[:period]) / period
    for x in ranges[period:]:
        value = ((period - 1) * value + x) / period
    return value


@dataclass(frozen=True)
class Signal:
    time: int
    close: float
    trend: bool
    breakout: bool
    breakdown: bool
    atr: float
    distance: float


def signal(hourly, four_hourly):
    if len(hourly) < 1000 or len(four_hourly) < 1000:
        raise ValueError("1000 closed candles per interval required")
    closes = [c.close for c in four_hourly]
    trend = closes[-1] > ema(closes, 200) and ema(closes, 50) > ema(closes, 200)
    last = hourly[-1]
    a = atr(hourly)
    return Signal(last.time, last.close, trend,
                  last.close > max(c.high for c in hourly[-21:-1]),
                  last.close < min(c.low for c in hourly[-11:-1]),
                  a, max(2 * a / last.close, .01))
