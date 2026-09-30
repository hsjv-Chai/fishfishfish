from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from fishbot.alerts import Alerts
from fishbot.backtest import metrics, aggregate, MemoryStore
from fishbot.client import APIError, Site, SchemaError, Quote, fish_units
from fishbot.config import Config, private_file
from fishbot.engine import Engine, Frame, DAY
from fishbot.gates import check, write_report, test_fingerprint
from fishbot.runtime import Runner
from fishbot.storage import Store
from fishbot.strategy import Candle, HOUR, Signal


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(database=str(root / "db"), evidence_dir=str(root / "evidence"))
        self.store = Store(self.cfg.database)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_alert_dedup_recovery_and_hourly_repeat(self):
        alerts = Alerts(self.store, replace(self.cfg, smtp_host="example", smtp_to="user@example.test"))
        with patch.object(alerts, "send") as send:
            alerts.set("down", "down", now=100)
            alerts.set("down", "down", now=105)
            self.assertEqual(send.call_count, 1)
            alerts.set("down", "down", now=3700)
            alerts.set("down", "up", active=False, now=3705)
            alerts.set("down", "up", active=False, now=3710)
            self.assertEqual(send.call_count, 3)

    def test_email_failure_retry_bounded(self):
        alerts = Alerts(self.store, replace(self.cfg, smtp_host="example", smtp_to="user@example.test"))
        with patch.object(alerts, "send", side_effect=OSError("private content never logged")) as send:
            alerts.set("down", "down", now=100)
            alerts.set("down", "down", now=105)
            alerts.set("down", "down", now=161)
            self.assertEqual(send.call_count, 2)

    def test_missing_evidence_blocks_live(self):
        with self.assertRaisesRegex(ValueError, "missing evidence"):
            check(self.cfg)

    def test_insufficient_trades_cannot_be_overridden_by_passed(self):
        stats = dict(net_return=.1, max_drawdown=.01, trades=26)
        write_report(self.cfg, "backtest.json", dict(passed=True, partitions={"test": {"0": stats, "0.001": stats}}))
        with self.assertRaisesRegex(ValueError, "not qualified"):
            check(self.cfg)

    def test_code_change_invalidates_evidence(self):
        write_report(self.cfg, "backtest.json", {})
        with patch("fishbot.gates.code_fingerprint", return_value="changed"):
            with self.assertRaisesRegex(ValueError, "another code version"):
                check(self.cfg)

    def test_private_secret_permissions(self):
        path = Path(self.tmp.name) / "secret"
        path.write_text("dummy")
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            private_file(str(path))
        path.chmod(0o600)
        self.assertEqual(private_file(str(path)), path)

    def test_precision_schema_guard(self):
        self.assertEqual(fish_units(115.1809), 1151809)
        with self.assertRaises(SchemaError):
            fish_units(1.00001)

    def test_redirect_becomes_auth_error_without_following(self):
        site = Site(self.cfg)
        with patch.object(site, "session", return_value={"token": "dummy"}), patch.object(
                site.http, "request", side_effect=APIError(307)):
            with self.assertRaises(APIError) as error:
                site.call("/fish/trade", raw=True)
            self.assertEqual(error.exception.code, 401)

    def test_qualifying_days_reset_on_gap(self):
        store = MemoryStore()
        e = Engine(store, self.cfg)
        t = 1000 * HOUR
        e.step(Frame(t, 100, Quote(100, 0, False, t)))
        e.step(Frame(t + 5000, 100, Quote(100, 0, False, t + 5000)))
        self.assertEqual(e.state["shadow_continuous_ms"], 5000)
        e.step(Frame(t + 120000, 100, Quote(100, 0, False, t + 120000)))
        self.assertEqual(e.state["shadow_continuous_ms"], 0)

    def test_trailing_stop_does_not_move_down(self):
        e = Engine(self.store, self.cfg)
        e.buy_intent(100000, .02, 1000 * HOUR, 1001 * HOUR + 10000)
        e.execute(Frame(1001 * HOUR + 10000, 100))
        original = e.state["position"]["stop"]
        t = 1002 * HOUR + 10000
        sig = Signal(1001 * HOUR, 100, True, False, False, 5, .1)
        e.step(Frame(t, 100, Quote(100, 0, False, t), sig))
        self.assertEqual(e.state["position"]["stop"], original)

    def test_metrics_exclude_forced_boundary_trade(self):
        curve = [(0, 500000, False), (60000, 505000, False)]
        stats = metrics(curve, [{"reason": "stop"}, {"reason": "evaluation_end"}])
        self.assertEqual(stats["trades"], 1)
        self.assertEqual(stats["boundary_closures"], 1)

    def test_minute_aggregation_uses_only_supplied_period(self):
        rows = [Candle(i * 60000, 100, 102, 99, 101, 2) for i in range(60)]
        c = aggregate(rows, HOUR)
        self.assertEqual((c.open, c.high, c.low, c.close, c.volume), (100, 102, 99, 101, 120))
        with self.assertRaises(ValueError):
            aggregate(rows[:-1], HOUR)

    def test_reconciliation_read_failure_does_not_block_exit(self):
        from test_core import FakeSite, frame, NOW
        site = FakeSite()
        e = Engine(self.store, replace(self.cfg, mode="live"), site)
        e.step(frame())
        with patch.object(site, "snapshot", side_effect=APIError(503)):
            e.step(frame(NOW + 60000, 98))
        self.assertIsNone(e.state["position"])

    def test_all_price_loss_creates_exit_intent(self):
        from test_core import FakeSite, frame, NOW
        site = FakeSite()
        e = Engine(self.store, replace(self.cfg, mode="live"), site)
        e.step(frame())
        f = frame(NOW + 5000)
        f.market_fresh = False
        e.step(f)
        self.assertIsNone(e.state["position"])

    def test_gap_stop_uses_observed_price_not_stop_line(self):
        e = Engine(self.store, self.cfg)
        from test_core import frame, NOW
        e.step(frame())
        e.step(frame(NOW + 5000, 95))
        row = self.store.db.execute("select data from events where kind='sell_filled'").fetchone()
        self.assertAlmostEqual(json.loads(row[0])["price"], 95 * .999)

    def test_missing_state_does_not_adopt_existing_position(self):
        from test_core import FakeSite, NOW
        site = FakeSite()
        site.positions = [{"id": "old"}]
        e = Engine(self.store, replace(self.cfg, mode="live"), site)
        with self.assertRaisesRegex(SchemaError, "no existing positions"):
            e.reconcile(NOW)


if __name__ == "__main__":
    unittest.main()
