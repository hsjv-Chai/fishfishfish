"""Chronological minute simulation. An hourly signal fills at next minute open.

We wait one full minute after hourly close (conservative approximation of +10s).
An intraminute stop fills at min(open, stop) minus adverse execution slippage.
Trailing thresholds calculated from an hour are not used inside that same hour.
"""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
import hashlib
import json
import math
import statistics

from .client import Quote
from .config import Config
from .engine import Engine, Frame
from .history import month_ms, add_months, read_minutes
from .math import settle
from .strategy import Candle, HOUR, signal
from .storage import initial_state


class MemoryStore:
    def __init__(self):
        self.kv = {}
        self.trades = []
        self.curve = []

    def get(self, key, default=None):
        return self.kv.get(key, default)

    def put(self, key, value):
        self.kv[key] = value

    def save(self, state, kind=None, data=None, now=None):
        self.kv["state"] = state
        if kind == "sell_filled":
            self.trades.append({"at": now, **data})

    def event(self, *args):
        pass

    def equity(self, at, value, exposed):
        # Last value in each minute is kept by simulator, not every intrabar substep.
        pass


def aggregate(chunk, width):
    if len(chunk) != width // 60000 or chunk[0].time % width:
        raise ValueError("incomplete aggregate bar")
    return Candle(chunk[0].time, chunk[0].open, max(c.high for c in chunk),
                  min(c.low for c in chunk), chunk[-1].close, sum(c.volume for c in chunk))


def metrics(curve, trades, start_units=500000):
    if not curve:
        raise ValueError("empty evaluation interval")
    peak, drawdown = start_units, 0
    daily = {}
    for t, value, exposed in curve:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
        day = t // (24 * HOUR)
        daily[day] = value
    values = [start_units, *daily.values()]
    returns = [b / a - 1 for a, b in zip(values, values[1:]) if a > 0]
    sd = statistics.stdev(returns) if len(returns) > 1 else 0
    return dict(net_return=curve[-1][1] / start_units - 1, max_drawdown=drawdown,
                trades=sum(t.get("reason") != "evaluation_end" for t in trades),
                boundary_closures=sum(t.get("reason") == "evaluation_end" for t in trades),
                daily_sharpe=(statistics.mean(returns) / sd * math.sqrt(365) if sd else None),
                exposure=sum(bool(r[2]) for r in curve) / len(curve), final_fish=curve[-1][1] / 10000)


def simulate(minutes, start, end, slippage):
    hourly, four = deque(maxlen=1000), deque(maxlen=1000)
    hour_chunk, four_chunk = [], []
    store = MemoryStore()
    cfg = replace(Config(), shadow_slippage=slippage)
    engine = Engine(store, cfg)
    sig = None
    curve = []
    first_price, last_price = None, None
    last_frame = None
    for c in minutes:
        if c.time >= end:
            break
        if c.time >= start:
            if len(hourly) < 1000 or len(four) < 1000:
                raise ValueError("evaluation needs 1000 completed 4h bars of preceding warmup")
            first_price = first_price or c.open
            last_price = c.close
            # Indicators are computed after previous hour, execution delayed to :01.
            ready_sig = sig if c.time % HOUR >= 60000 else None
            frame = Frame(c.time, c.open, Quote(c.open, 0, False, c.time), ready_sig)
            engine.step(frame)
            # Intrabar low checks only an already established stop. No use of this minute's high.
            p = engine.state["position"]
            if p:
                eq_limit = max(engine.state["high_water"] * .95, engine.state["day_base"] * .99)
                cash = engine.state["cash"]
                # For 1x equity=cash+floor(stake*price/entry*(1-fee)).
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
            sig = signal(list(hourly), list(four))
    if not curve or curve[0][0] != start or curve[-1][0] + 60000 != end:
        raise ValueError("requested evaluation interval incomplete")
    if engine.state["position"]:
        engine.sell_intent("evaluation_end", last_frame.now)
        engine.execute(last_frame)
        curve[-1] = (curve[-1][0], engine.state["cash"], False)
    return metrics(curve, store.trades), store.trades, curve, engine.state["halt"]


def baselines(minutes, start, end, slippage):
    rows = [c for c in minutes if start <= c.time < end]
    if not rows:
        raise ValueError("no baseline data")
    result = {}
    for name, fraction in [("cash", 0), ("btc_buy_hold", 1), ("btc20_cash80", .2)]:
        stake = round(500000 * fraction)
        cash = 500000 - stake
        entry = rows[0].open * (1 + slippage)
        curve = [(c.time, cash + (settle(stake, entry, c.close * (1 - slippage)) if stake else 0), bool(stake)) for c in rows]
        result[name] = metrics(curve, [{"baseline": True}] if stake else [])
    return result


def code_fingerprint():
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for p in sorted(root.glob("*.py")):
        digest.update(p.name.encode())
        digest.update(p.read_bytes())
    return digest.hexdigest()


def run_backtest(csv_path, start_month, output):
    start = month_ms(start_month)
    boundaries = [start, month_ms(add_months(start_month, 12)),
                  month_ms(add_months(start_month, 18)), month_ms(add_months(start_month, 24))]
    minutes = list(read_minutes(csv_path))
    if minutes[0].time > start - 1000 * 4 * HOUR or minutes[-1].time + 60000 < boundaries[-1]:
        raise ValueError("need 24 full months plus at least 167 days of prior warmup")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    report = dict(code_sha256=code_fingerprint(), data_sha256=hashlib.sha256(Path(csv_path).read_bytes()).hexdigest(),
                  evaluation_start=start_month, partitions={}, limitations=[
                      "1-minute OHLC cannot reconstruct 5-second paths; fills approximated conservatively",
                      "intra-minute highs are not sampled for account high-water; live drawdown can differ",
                      "fixed strategy; no optimization on validation or test partition",
                      "each partition starts with 50 fish; no carry-over positions; preceding data only warms indicators"])
    for i, name in enumerate(("development", "validation", "test")):
        report["partitions"][name] = {}
        for slip in (0, .0005, .001, .002):
            import logging
            logging.info("backtest %s slip=%.4f", name, slip)
            summary, trades, curve, halt = simulate(minutes, boundaries[i], boundaries[i + 1], slip)
            report["partitions"][name][str(slip)] = {**summary, "halt": halt,
                    "baselines": baselines(minutes, boundaries[i], boundaries[i + 1], slip)}
            (output / f"{name}-{slip}-trades.json").write_text(json.dumps(trades, indent=2))
    test = report["partitions"]["test"]
    report["passed"] = (test["0"]["net_return"] > 0 and test["0"]["max_drawdown"] <= .05 and
                        test["0"]["trades"] >= 30 and test["0.001"]["net_return"] > 0 and
                        test["0.001"]["max_drawdown"] <= .05 and test["0.001"]["trades"] >= 30)
    report["status"] = "passed" if report["passed"] else "not_qualified"
    (output / "backtest.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    return report
