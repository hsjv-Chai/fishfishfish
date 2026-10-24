import tempfile
import unittest
from pathlib import Path

from dataclasses import replace
from fishbot.backtest import MemoryStore
from fishbot.client import Quote
from fishbot.engine import Frame
from fishbot.strategy import Candle, HOUR
from fishbot_v4.backtest import code_fingerprint
from fishbot_v4.config import V4Config
from fishbot_v4.engine import V4Engine
from fishbot_v4.strategy import Signal, signal

T = 1000 * HOUR + 10_000


def hourly(up=True, last_close=None):
    rows = [Candle(i * HOUR, 100, 100.1, 99.9, 100) for i in range(1000)]
    rows[-1] = Candle(999 * HOUR, 100, 101, 99, last_close if last_close is not None else (101.1 if up else 98.9))
    return rows


def four(up=True):
    values = [100 + i * .1 for i in range(1000)] if up else [200 - i * .1 for i in range(1000)]
    return [Candle(i * 4 * HOUR, v, v + .1, v - .1, v) for i, v in enumerate(values)]


def sig(up=True):
    return Signal(T // HOUR * HOUR - HOUR, 100, .5, .01, up, not up, up, not up,
                  not up, up)


class SignalTests(unittest.TestCase):
    def test_long_and_short(self):
        long_signal = signal(hourly(True, 101.1), four(True))
        short_signal = signal(hourly(False, 98.9), four(False))
        self.assertEqual(long_signal.entry_direction, 1)
        self.assertEqual(short_signal.entry_direction, -1)
        self.assertTrue(long_signal.exit_short)
        self.assertTrue(short_signal.exit_long)

    def test_warmup_and_window(self):
        with self.assertRaises(ValueError):
            signal(hourly()[:999], four())
        with self.assertRaises(ValueError):
            signal(hourly(), four(), 20, 55)


class EngineTests(unittest.TestCase):
    def test_fixed_twenty_percent_entry(self):
        engine = V4Engine(MemoryStore(), replace(V4Config(), shadow_slippage=0))
        engine.step(Frame(T, 100, Quote(100, 0, False, T), sig()))
        position = engine.state["position"]
        self.assertEqual(position["stake"], 100000)
        self.assertEqual(position["direction"], "long")
        self.assertEqual(position["stop"], 99)

    def test_short_shape_and_stop(self):
        engine = V4Engine(MemoryStore(), replace(V4Config(), shadow_slippage=0))
        engine.step(Frame(T, 100, Quote(100, 0, False, T), sig(False)))
        p = engine.state["position"]
        self.assertEqual((p["direction"], p["stake"], p["stop"]), ("short", 100000, 101))
        engine.step(Frame(T + 20_000, 101.5, Quote(101.5, 0, False, T + 20_000)))
        self.assertIsNone(engine.state["position"])

    def test_slow_exit_keeps_position_through_ten_bar_pullback(self):
        engine = V4Engine(MemoryStore(), replace(V4Config(), shadow_slippage=0))
        engine.step(Frame(T, 100, Quote(100, 0, False, T), sig()))
        # A 10-bar breakdown is no longer an exit in V4; only 55-bar/trend.
        exit_signal = Signal(T // HOUR * HOUR - HOUR, 100, .5, .01, True, False,
                             False, False, False, True)
        engine.step(Frame(T + 20_000, 100, Quote(100, 0, False, T + 20_000), exit_signal))
        self.assertIsNotNone(engine.state["position"])

    def test_fingerprint(self):
        self.assertEqual(len(code_fingerprint()), 64)


class ConfigTests(unittest.TestCase):
    def test_default_key(self):
        self.assertEqual(V4Config().key, "b55-x55-s2-f0.2-l1")
        self.assertEqual(V4Config().stake_fraction, .2)

    def test_frozen_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v4.toml"
            path.write_text("breakout = 20\n")
            with self.assertRaises(ValueError):
                V4Config.load(str(path))


if __name__ == "__main__":
    unittest.main()
