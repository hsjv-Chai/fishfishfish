"""Chronological minute simulation for V4. Signals fill at the next minute open."""
from collections import deque
from dataclasses import replace
from pathlib import Path
import copy
import hashlib
import json
import math

from fishbot.backtest import MemoryStore, aggregate, baselines, metrics
from fishbot.client import Quote
from fishbot.engine import Frame, position_direction
from fishbot.history import add_months, month_ms
from fishbot.strategy import HOUR, Candle
from fishbot_v4.config import V4Config
from fishbot_v4.engine import V4Engine
from fishbot_v4.strategy import signal


def code_fingerprint():
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for p in sorted(root.glob("*.py")):
        digest.update(p.name.encode())
        digest.update(p.read_bytes())
    shared = Path(__file__).parent.parent / "fishbot"
    for name in ("client.py", "engine.py", "math.py", "strategy.py", "storage.py", "alerts.py"):
        p = shared / name
        digest.update(("fishbot/" + name).encode())
        digest.update(p.read_bytes())
    return digest.hexdigest()


def simulate(minutes, start, end, cfg, slippage, carry=None):
    cfg = replace(cfg, shadow_slippage=slippage)
    hourly, four = deque(maxlen=1000), deque(maxlen=1000)
    hour_chunk, four_chunk = [], []
    store = MemoryStore()
    engine = V4Engine(store, cfg)
    if carry is not None:
        if carry.get("position") or carry.get("pending"):
            raise ValueError("carry must be fully settled")
        engine.state = copy.deepcopy(carry)
    start_units = carry["cash"] if carry is not None else round(cfg.capital * 10000)
    sig = None
    curve = []
    last_frame = None
    for c in minutes:
        if c.time >= end:
            break
        if c.time >= start:
            if len(hourly) < 1000 or len(four) < 1000:
                raise ValueError("evaluation needs 1000 completed 4h bars of preceding warmup")
            ready_sig = sig if c.time % HOUR >= 60000 else None
            frame = Frame(c.time, c.open, Quote(c.open, 0, False, c.time), ready_sig)
            engine.step(frame)
            p = engine.state["position"]
            if p:
                eq_limit = max(engine.state["high_water"] * .95, engine.state["day_base"] * .99)
                cash = engine.state["cash"]
                if position_direction(p) == "short":
                    notional = p["stake"] * p["leverage"]
                    ratio = (cash + p["stake"] + notional - eq_limit) / (notional * (1 + .0002))
                    risk_price = max(0, p["entry"] * ratio)
                    trigger = min(p["stop"], risk_price)
                    if c.high >= trigger:
                        exit_base = max(c.open, trigger)
                        engine.step(Frame(c.time + 30000, exit_base,
                                          Quote(exit_base, 0, False, c.time + 30000), None))
                else:
                    risk_price = max(0, (eq_limit - cash) * p["entry"] / (p["stake"] * (1 - .0002)))
                    trigger = max(p["stop"], risk_price)
                    if c.low <= trigger:
                        exit_base = min(c.open, trigger)
                        engine.step(Frame(c.time + 30000, exit_base,
                                          Quote(exit_base, 0, False, c.time + 30000), None))
            engine.step(Frame(c.time + 59999, c.close, Quote(c.close, 0, False, c.time + 59999), None))
            curve.append((c.time, engine.equity(c.close), bool(engine.state["position"])))
            last_frame = Frame(c.time + 59999, c.close)
        hour_chunk.append(c)
        four_chunk.append(c)
        if (c.time + 60000) % HOUR == 0:
            hourly.append(aggregate(hour_chunk, HOUR))
            hour_chunk = []
        if (c.time + 60000) % (4 * HOUR) == 0:
            four.append(aggregate(four_chunk, 4 * HOUR))
            four_chunk = []
        if (c.time + 60000) % HOUR == 0 and len(hourly) >= 1000 and len(four) >= 1000:
            sig = signal(list(hourly), list(four), cfg.breakout, cfg.exit_breakout)
    if not curve or curve[0][0] != start or curve[-1][0] + 60000 != end:
        raise ValueError("requested evaluation interval incomplete")
    if engine.state["position"]:
        engine.sell_intent("evaluation_end", last_frame.now)
        engine.execute(last_frame)
        curve[-1] = (curve[-1][0], engine.state["cash"], False)
    result = metrics(curve, store.trades, start_units)
    if carry is not None:
        peak = max(carry.get("high_water", start_units), start_units)
        for _, value, _ in curve:
            peak = max(peak, value)
            result["max_drawdown"] = max(result["max_drawdown"], 1 - value / peak)
    return result, store.trades, curve, engine.state["halt"], engine.state


def evaluate(minutes, start_month, end_month, cfg, data_sha256):
    start, end = month_ms(start_month), month_ms(end_month)
    result = dict(partitions={}, code_sha256=code_fingerprint(), data_sha256=data_sha256,
                  policy=cfg.dict(), limitations=[
                      "1-minute OHLC cannot reconstruct 5-second paths; stops are conservative approximations",
                      "intra-minute highs are not sampled for the high-water mark",
                      "V4 is a frozen research policy; the test segment is not pristine out-of-sample",
                  ])
    for name, a, b in (("development", start_month, add_months(start_month, 12)),
                       ("validation", add_months(start_month, 12), add_months(start_month, 18)),
                       ("test", add_months(start_month, 18), add_months(start_month, 24))):
        result["partitions"][name] = {}
        for slip in (0, .0005, .001, .002):
            summary, trades, curve, halt, _ = simulate(minutes, month_ms(a), month_ms(b), cfg, slip)
            result["partitions"][name][str(slip)] = {
                **summary, "halt": halt,
                "baselines": baselines(minutes, month_ms(a), month_ms(b), slip),
            }
    test = result["partitions"]["test"]
    result["passed"] = (test["0"]["net_return"] > 0 and test["0"]["max_drawdown"] <= .05 and
                        test["0"]["trades"] >= 30 and test["0.001"]["net_return"] > 0 and
                        test["0.001"]["max_drawdown"] <= .05 and test["0.001"]["trades"] >= 30)
    result["status"] = "passed" if result["passed"] else "not_qualified"
    return result
