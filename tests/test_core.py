import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from fishbot.client import APIError, SchemaError, Quote, parse_positions_page
from fishbot.config import Config
from fishbot.engine import Engine, Frame, DAY
from fishbot.math import settle, size
from fishbot.storage import Store, instance_lock
from fishbot.strategy import Candle, HOUR, Signal, atr, ema, signal, closed_candles

NOW = 1_790_800_000_000 // HOUR * HOUR + 10000


def frame(now=NOW, price=100, trend=True, breakout=True):
    sig = Signal(now // HOUR * HOUR - HOUR, 100, trend, breakout, False, .5, .01)
    return Frame(now, price, Quote(price, 0, False, now), sig)


class FakeSite:
    def __init__(self):
        self.balance = 1150000
        self.positions = []
        self.rows = []
        self.keys = {}
        self.closed = {}
        self.buy_calls = []
        self.sell_calls = []
        self.fail_buy = None
        self.fail_sell = None
        self.lose_buy = False
        self.lose_sell = False

    def snapshot(self, since_id=0):
        return dict(balance=self.balance, positions=list(self.positions), transactions=list(self.rows),
                    newest_id=max((r["id"] for r in self.rows), default=0))

    def buy(self, order):
        self.buy_calls.append(order["key"])
        if self.fail_buy:
            raise self.fail_buy
        if order["key"] not in self.keys:
            pid = "p" + str(len(self.keys) + 1)
            p = dict(id=pid, symbol="BTCUSDT", stake=order["stake"] / 10000, entry_price=100, leverage=1)
            self.keys[order["key"]] = {"position": p}
            self.positions.append(dict(p, entryPrice=100))
            self.balance -= order["stake"]
            self.rows.append(dict(id=len(self.rows) + 1, amount=-order["stake"] / 10000,
                                  type="market_buy", reference_id=pid))
        if self.lose_buy:
            self.lose_buy = False
            raise APIError(0)
        return self.keys[order["key"]]

    def sell(self, pid):
        self.sell_calls.append(pid)
        if self.fail_sell:
            raise self.fail_sell
        if pid not in self.closed:
            p = next(p for p in self.positions if p["id"] == pid)
            payout = settle(round(p["stake"] * 10000), 100, 99)
            self.balance += payout
            self.rows.append(dict(id=len(self.rows) + 1, amount=payout / 10000,
                                  type="market_sell", reference_id=pid))
            self.positions = [p for p in self.positions if p["id"] != pid]
            self.closed[pid] = dict(payout=payout / 10000, exit_price=99)
        if self.lose_sell:
            self.lose_sell = False
            raise APIError(0)
        return self.closed[pid]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "state.db")
        self.store = Store(self.path)
        self.cfg = Config(database=self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def live(self):
        site = FakeSite()
        engine = Engine(self.store, replace(self.cfg, mode="live"), site)
        return engine, site

    def test_settlement_no_move(self):
        self.assertEqual(settle(100000, 100, 100), 99980)
        self.assertEqual(settle(100000, 100, 100, 100), 98000)

    def test_liquidation_caps_loss(self):
        self.assertEqual(settle(10000, 100, 49, 2), 0)

    def test_precision_and_size(self):
        self.assertEqual(size(500000, 500000, .01), 100000)
        self.assertEqual(size(500000, 500000, .05), 0)
        self.assertEqual(size(1000, 1000, .01), 0)
        self.assertLessEqual(size(500000, 500000, .025) / 10000 * (.025 + .0012), .125)

    def test_buy_only_one_per_signal(self):
        e = Engine(self.store, self.cfg)
        e.step(frame())
        self.assertIsNotNone(e.state["position"])
        balance = e.state["cash"]
        e.step(frame(NOW + 5000))
        self.assertEqual(e.state["cash"], balance)

    def test_stale_quote_blocks_buy(self):
        e = Engine(self.store, self.cfg)
        f = frame()
        f.quote.age_ms = 41000
        e.step(f)
        self.assertIsNone(e.state["position"])

    def test_external_fallback_cannot_open(self):
        e = Engine(self.store, self.cfg)
        f = frame()
        f.entry_enabled = False
        e.step(f)
        self.assertIsNone(e.state["position"])

    def test_divergence_blocks_buy(self):
        e = Engine(self.store, self.cfg)
        f = frame()
        f.quote.price = 101
        e.step(f)
        self.assertIsNone(e.state["position"])

    def test_stop_with_site_quote_unavailable(self):
        e = Engine(self.store, self.cfg)
        e.step(frame())
        f = frame(NOW + 5000, 98)
        f.quote = None
        e.step(f)
        self.assertIsNone(e.state["position"])
        self.assertLess(e.state["cash"], 500000)

    def test_reopen_cooldown(self):
        e = Engine(self.store, self.cfg)
        e.step(frame())
        e.step(frame(NOW + 5000, 98))
        e.step(frame(NOW + 6 * HOUR))
        self.assertIsNone(e.state["position"])
        e.step(frame(NOW + 7 * HOUR))
        self.assertIsNotNone(e.state["position"])

    def test_drawdown_persistent_halt(self):
        e = Engine(self.store, self.cfg)
        e.state["cash"] = 475000
        e.step(frame(breakout=False))
        self.assertEqual(e.state["halt"], "max_drawdown")
        restored = Engine(self.store, self.cfg)
        self.assertEqual(restored.state["halt"], "max_drawdown")

    def test_daily_loss_resets_next_beijing_day(self):
        e = Engine(self.store, self.cfg)
        e.step(frame(breakout=False))
        e.state["cash"] = 494900
        e.step(frame(NOW + 5000, breakout=False))
        self.assertEqual(e.state["day_pause"], e.state["day"])
        e.step(frame(NOW + DAY, breakout=False))
        self.assertEqual(e.state["day_pause"], "")
        self.assertEqual(e.state["day_base"], 494900)

    def test_session_expiry_closes(self):
        e = Engine(self.store, self.cfg)
        e.step(frame())
        f = frame(NOW + 5000)
        f.expires_at = NOW + DAY
        e.step(f)
        self.assertIsNone(e.state["position"])

    def test_lost_buy_response_recovers_without_second_buy(self):
        e, site = self.live()
        site.lose_buy = True
        e.step(frame())
        self.assertIsNotNone(e.state["pending"])
        e = Engine(self.store, replace(self.cfg, mode="live"), site)
        e.step(frame(NOW + 5000))
        self.assertIsNone(e.state["pending"])
        self.assertEqual(e.state["position"]["id"], "p1")
        self.assertEqual(len(site.buy_calls), 1)
        self.assertEqual(e.state["cash"], 400000)

    def test_retry_uses_same_key(self):
        e, site = self.live()
        site.fail_buy = APIError(503)
        e.step(frame())
        key = e.state["pending"]["key"]
        site.fail_buy = None
        e.step(frame(NOW + 5000))
        self.assertEqual(site.buy_calls, [key, key])
        self.assertEqual(len(site.positions), 1)

    def test_stale_uncertain_buy_never_reissued(self):
        e, site = self.live()
        site.fail_buy = APIError(0)
        e.step(frame())
        site.fail_buy = None
        e.step(frame(NOW + 120000))
        self.assertEqual(len(site.buy_calls), 1)
        self.assertEqual(e.state["halt"], "ambiguous_buy")

    def test_definitively_rejected_buy_can_expire(self):
        e, site = self.live()
        site.fail_buy = APIError(503)
        e.step(frame())
        site.fail_buy = None
        e.step(frame(NOW + 120000))
        self.assertIsNone(e.state["pending"])
        self.assertEqual(e.state["halt"], "")
        self.assertEqual(len(site.buy_calls), 1)

    def test_sell_timeout_idempotent(self):
        e, site = self.live()
        e.step(frame())
        site.lose_sell = True
        e.step(frame(NOW + 5000, 98))
        self.assertEqual(e.state["pending"]["kind"], "sell")
        e.step(frame(NOW + 10000, 98))
        self.assertIsNone(e.state["position"])
        self.assertEqual(len([r for r in site.rows if r["type"] == "market_sell"]), 1)

    def test_401_preserves_exit_intent(self):
        e, site = self.live()
        e.step(frame())
        site.fail_sell = APIError(401)
        e.step(frame(NOW + 5000, 98))
        self.assertEqual(e.state["pending"]["kind"], "sell")
        self.assertIsNotNone(e.state["position"])
        site.fail_sell = None
        e.step(frame(NOW + 10000, 98))
        self.assertIsNone(e.state["position"])

    def test_rate_limit_retry_after(self):
        e, site = self.live()
        site.fail_buy = APIError(429, 30)
        e.step(frame())
        e.step(frame(NOW + 5000))
        self.assertEqual(len(site.buy_calls), 1)
        self.assertEqual(e.state["pending"]["next_try"], NOW + 30000)

    def test_excluded_deposit(self):
        e, site = self.live()
        e.reconcile(NOW)
        site.balance += 20000
        site.rows.append(dict(id=1, amount=2, type="checkin", reference_id=None))
        e.reconcile(NOW + 10000)
        self.assertEqual(e.state["cash"], 500000)

    def test_unknown_trade_fails_closed(self):
        e, site = self.live()
        e.reconcile(NOW)
        site.positions.append(dict(id="manual", symbol="BTCUSDT", stake=1, entryPrice=100, leverage=1))
        with self.assertRaises(SchemaError):
            e.reconcile(NOW + 10000)

    def test_ledger_mismatch(self):
        e, site = self.live()
        e.reconcile(NOW)
        site.balance -= 1
        with self.assertRaises(SchemaError):
            e.reconcile(NOW + 10000)

    def test_mode_db_mixing_blocked(self):
        Engine(self.store, self.cfg)
        with self.assertRaises(ValueError):
            Engine(self.store, replace(self.cfg, mode="live"))

    def test_single_instance(self):
        with instance_lock(self.path):
            with self.assertRaises(RuntimeError):
                with instance_lock(self.path):
                    pass

    def test_graceful_shutdown(self):
        e = Engine(self.store, self.cfg)
        e.step(frame())
        e.shutdown(frame(NOW + 5000))
        self.assertIsNone(e.state["position"])

    def test_schema_does_not_evaluate_script(self):
        with self.assertRaises(SchemaError):
            parse_positions_page('<script>throw new Error("not executable")</script>')

    def test_rsc_snapshot(self):
        props = dict(positions=[], balance=115.1809, feeRate=.0002, minStake=1, initialQuotes=[])
        chunk = "a:" + json.dumps(["$", "$L1", None, props]) + "\n"
        html = "<script>self.__next_f.push(" + json.dumps([1, chunk]) + ")</script>"
        self.assertEqual(parse_positions_page(html)["balance"], 1151809)


class IndicatorTests(unittest.TestCase):
    def test_ema_constant(self):
        self.assertEqual(ema([100] * 1000, 200), 100)

    def test_atr_wilder(self):
        rows = [Candle(i * HOUR, 100, 101, 99, 100) for i in range(20)]
        self.assertEqual(atr(rows), 2)

    def test_current_bar_excluded(self):
        rows = [Candle(i * HOUR, 100, 101, 99, 100) for i in range(1001)]
        actual = closed_candles(rows, HOUR, 1000 * HOUR + 10000)
        self.assertEqual(len(actual), 1000)

    def test_gap_rejected(self):
        rows = [Candle(i * HOUR, 100, 101, 99, 100) for i in range(1001)]
        del rows[3]
        with self.assertRaises(ValueError):
            closed_candles(rows, HOUR, 1001 * HOUR)

    def test_breakout_excludes_own_high(self):
        rows = [Candle(i * HOUR, 100, 101, 99, 100) for i in range(1000)]
        rows[-1] = Candle(rows[-1].time, 100, 150, 99, 102)
        four = [Candle(i * 4 * HOUR, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(1000)]
        sig = signal(rows, four)
        self.assertTrue(sig.breakout)
        self.assertTrue(sig.trend)

    def test_microsecond_archives(self):
        row = [1735689600000000, "100", "101", "99", "100", "10"]
        self.assertEqual(Candle.row(row).time, 1735689600000)
