import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fishbot.client import SchemaError, Site, parse_positions_page
from fishbot.config import Config
from fishbot.engine import Engine, Frame
from fishbot.client import Quote
from fishbot.math import liquidation_price, settle, size
from fishbot.storage import Store

HOUR = 3_600_000
T = 1000 * HOUR + 10_000


def page(positions, balance=69.4375):
    props = dict(positions=positions, balance=balance, feeRate=.0002, minStake=1, initialQuotes=[])
    chunk = "a:" + json.dumps(["$", "$L1", None, props]) + "\n"
    return "<script>self.__next_f.push(" + json.dumps([1, chunk]) + ")</script>"


def position(direction="long", leverage=1, entry=80000, stake=1, liquidation=None, id="p1"):
    if liquidation is None:
        if leverage <= 0:
            liquidation = 0
        elif direction == "long":
            liquidation = 0 if leverage == 1 else entry * (1 - 1 / leverage)
        else:
            liquidation = entry * (1 + 1 / leverage)
    return dict(id=id, symbol="BTCUSDT", direction=direction, stake=stake,
                entryPrice=entry, leverage=leverage, liquidationPrice=liquidation)


class MathTests(unittest.TestCase):
    def test_short_and_leverage_settlement(self):
        # 10x short, entry 100, exit 90: 100000 + 1,000,000*0.1 - 180 fee.
        self.assertEqual(settle(100000, 100, 90, 10, direction="short"), 199819)
        self.assertEqual(settle(100000, 100, 90, 10), 0)
        self.assertEqual(settle(100000, 100, 110, 1, direction="short"), 89977)
        self.assertEqual(settle(100000, 100, 110, 1), 109978)

    def test_liquidation_prices(self):
        self.assertEqual(liquidation_price(100, 1, "long"), 0)
        self.assertEqual(liquidation_price(100, 50, "long"), 98)
        self.assertEqual(liquidation_price(100, 1, "short"), 200)
        self.assertEqual(liquidation_price(100, 50, "short"), 102)
        self.assertEqual(settle(100000, 100, 98, 50), 0)
        self.assertEqual(settle(100000, 100, 102, 50, direction="short"), 0)

    def test_size_scales_with_leverage(self):
        self.assertEqual(size(500000, 500000, .01), 100000)
        self.assertGreater(size(500000, 500000, .01, 10), 0)
        self.assertLess(size(500000, 500000, .01, 10), size(500000, 500000, .01))

    def test_invalid_direction_and_leverage(self):
        with self.assertRaises(ValueError):
            settle(100000, 100, 90, 0)
        with self.assertRaises(ValueError):
            settle(100000, 100, 90, 50, direction="up")
        with self.assertRaises(ValueError):
            size(500000, 500000, .01, 101)


class ParserTests(unittest.TestCase):
    def test_custom_leverage_and_short_are_preserved(self):
        values = parse_positions_page(page([position("short", 50, id="p1"), position("long", 25, id="p2")]))["positions"]
        self.assertEqual(values[0]["direction"], "short")
        self.assertEqual(values[0]["leverage"], 50)
        self.assertEqual(values[0]["liquidationPrice"], 81600)
        self.assertEqual(values[1]["direction"], "long")
        self.assertEqual(values[1]["leverage"], 25)

    def test_1x_short_is_not_flattened(self):
        values = parse_positions_page(page([position("short", 1)]))["positions"]
        self.assertEqual(values[0]["direction"], "short")
        self.assertEqual(values[0]["leverage"], 1)
        self.assertEqual(values[0]["liquidationPrice"], 160000)

    def test_invalid_direction_and_leverage_rejected(self):
        for value in (position("up", 1), position("long", 0), position("long", 101),
                      position("long", 1.5), position("long", True)):
            with self.assertRaises(SchemaError):
                parse_positions_page(page([value]))

    def test_missing_direction_rejected(self):
        value = position("long", 1)
        del value["direction"]
        with self.assertRaises(SchemaError):
            parse_positions_page(page([value]))


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.site = Site(Config())

    def test_buy_sends_direction_and_custom_leverage(self):
        order = dict(stake=10000, distance=.01, key="f-" + "a" * 32, direction="short", leverage=50)
        response = {"position": dict(id="p1", symbol="BTCUSDT", direction="short", stake=1,
                                     entry_price=80000, leverage=50, liquidation_price=81600)}
        with patch.object(self.site, "call", return_value=response) as call:
            self.site.buy(order)
        body = call.call_args.args[1]
        self.assertEqual((body["direction"], body["leverage"], body["amount"]), ("short", 50, 1))

    def test_buy_receipt_shape_mismatch_is_rejected(self):
        order = dict(stake=10000, distance=.01, key="f-" + "a" * 32, direction="short", leverage=50)
        response = {"position": dict(id="p1", symbol="BTCUSDT", direction="long", stake=1,
                                     entry_price=80000, leverage=50, liquidation_price=78400)}
        with patch.object(self.site, "call", return_value=response):
            with self.assertRaises(SchemaError):
                self.site.buy(order)

    def test_sell_accepts_liquidated_short(self):
        response = dict(position_id="p1", symbol="BTCUSDT", direction="short", payout=0,
                        exit_price=81600, liquidated=True)
        with patch.object(self.site, "call", return_value=response):
            self.assertTrue(self.site.sell("p1")["liquidated"])

    def test_sell_requires_direction(self):
        response = dict(position_id="p1", symbol="BTCUSDT", payout=0, exit_price=81600, liquidated=True)
        with patch.object(self.site, "call", return_value=response):
            with self.assertRaises(SchemaError):
                self.site.sell("p1")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "state.db")
        self.store = Store(self.path)
        self.engine = Engine(self.store, replace(Config(database=self.path), shadow_slippage=0))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enter_short(self, leverage=10):
        self.engine.buy_intent(100000, .01, T - HOUR, T, direction="short", leverage=leverage)
        self.engine.execute(Frame(T, 100, Quote(100, 0, False, T)))
        return self.engine.state["position"]

    def test_short_position_shape_and_equity(self):
        p = self.enter_short()
        self.assertEqual((p["direction"], p["leverage"], p["stop"]), ("short", 10, 101))
        self.assertAlmostEqual(p["liquidation"], 110)
        self.assertEqual(self.engine.equity(90), 599819)

    def test_short_stop_exits_and_keeps_settlement(self):
        self.enter_short()
        self.engine.step(Frame(T + 20_000, 101.5, Quote(101.5, 0, False, T + 20_000)))
        self.assertIsNone(self.engine.state["position"])
        self.assertEqual(self.engine.state["halt"], "")
        row = self.store.db.execute("select data from events where kind='sell_filled'").fetchone()
        self.assertEqual(json.loads(row[0])["direction"], "short")
        self.assertEqual(json.loads(row[0])["leverage"], 10)

    def test_liquidated_sell_closes_without_ledger_cash(self):
        self.enter_short()
        self.engine.sell_intent("site_liquidation", T + 20_000)
        self.engine.apply_sell(0, 110, T + 30_000, liquidated=True)
        self.assertIsNone(self.engine.state["position"])
        self.assertEqual(self.engine.state["halt"], "")
        row = self.store.db.execute("select data from events where kind='sell_filled'").fetchone()
        self.assertTrue(json.loads(row[0])["liquidated"])

    def test_buy_intent_rejects_invalid_shape(self):
        with self.assertRaises(ValueError):
            self.engine.buy_intent(100000, .01, T - HOUR, T, direction="short", leverage=101)
        with self.assertRaises(ValueError):
            self.engine.buy_intent(100000, .01, T - HOUR, T, direction="up")


if __name__ == "__main__":
    unittest.main()
