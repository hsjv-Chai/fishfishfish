import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fishbot.backtest import MemoryStore
from fishbot.client import Quote
from fishbot.engine import Frame
from fishbot.strategy import Candle, HOUR
from fishbot_v3.backtest import code_fingerprint
from fishbot_v3.config import V3Config
from fishbot_v3.engine import V3Engine
from fishbot_v3.strategy import signal

T = 1000 * HOUR + 10_000


def hourly(up=True, last_close=None):
    rows = [Candle(i * HOUR, 100, 100.1, 99.9, 100) for i in range(1000)]
    rows[-1] = Candle(999 * HOUR, 100, 101 if up else 101, 99, last_close if last_close is not None else (101.1 if up else 98.9))
    return rows


def four(up=True):
    rows = [Candle(i * 4 * HOUR, 0, 0, 0, 0) for i in range(1000)]
    values = [100 + i * .1 for i in range(1000)] if up else [200 - i * .1 for i in range(1000)]
    return [Candle(i * 4 * HOUR, v, v + .1, v - .1, v) for i, v in enumerate(values)]


class SignalTests(unittest.TestCase):
    def test_long_and_short_entry(self):
        long_signal = signal(hourly(True, 101.1), four(True), 20)
        short_signal = signal(hourly(False, 98.9), four(False), 20)
        self.assertTrue(long_signal.up)
        self.assertTrue(long_signal.breakout)
        self.assertEqual(long_signal.entry_direction, 1)
        self.assertLessEqual(long_signal.distance, .04)
        self.assertTrue(short_signal.down)
        self.assertTrue(short_signal.breakdown)
        self.assertEqual(short_signal.entry_direction, -1)

    def test_exit_flags_use_ten_bars(self):
        s = signal(hourly(True, 101.1), four(True), 20)
        self.assertFalse(s.exit_long)
        self.assertTrue(s.exit_short)

    def test_warmup_required(self):
        with self.assertRaises(ValueError):
            signal(hourly()[:999], four(), 20)

    def test_breakout_must_exclude_current(self):
        rows = hourly(True, 100.05)
        s = signal(rows, four(True), 20)
        self.assertFalse(s.breakout)


class EngineTests(unittest.TestCase):
    def engine(self, cfg=None):
        store = MemoryStore()
        return V3Engine(store, cfg or replace(V3Config(), shadow_slippage=0))

    def enter_short(self):
        engine = self.engine()
        engine.buy_intent(100000, .01, T - HOUR, T, direction="short", leverage=1)
        engine.execute(Frame(T, 100, Quote(100, 0, False, T)))
        return engine

    def test_short_position_shape_and_equity(self):
        engine = self.enter_short()
        p = engine.state["position"]
        self.assertEqual((p["direction"], p["leverage"], p["stop"]), ("short", 1, 101))
        self.assertEqual(engine.equity(99), 500980)

    def test_short_stop_exits(self):
        engine = self.enter_short()
        engine.step(Frame(T + 20_000, 101.5, Quote(101.5, 0, False, T + 20_000)))
        self.assertIsNone(engine.state["position"])
        self.assertEqual(engine.state["halt"], "")

    def test_time_stop(self):
        engine = self.engine(replace(V3Config(), shadow_slippage=0, time_stop_hours=1))
        engine.buy_intent(100000, .01, T - HOUR, T)
        engine.execute(Frame(T, 100, Quote(100, 0, False, T)))
        engine.step(Frame(T + HOUR, 100, Quote(100, 0, False, T + HOUR)))
        self.assertIsNone(engine.state["position"])

    def test_invalid_order_shape(self):
        engine = self.engine()
        with self.assertRaises(ValueError):
            engine.buy_intent(100000, .01, T - HOUR, T, direction="up")


class ConfigTests(unittest.TestCase):
    def test_defaults_and_key(self):
        cfg = V3Config()
        self.assertEqual(cfg.leverage, 1)
        self.assertEqual(cfg.key, "b20-t3-r0.0025-s120-l1")
        self.assertEqual(len({replace(cfg, breakout=b, trail_atr=t).key
                              for b in (20, 55) for t in (3, 4)}), 4)

    def test_fingerprint_includes_v3(self):
        self.assertEqual(len(code_fingerprint()), 64)


if __name__ == "__main__":
    unittest.main()
