from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import json
import logging
import signal as os_signal
import time

from .alerts import Alerts
from .client import APIError, Market, Site, SchemaError
from .engine import Engine, Frame, DAY
from .gates import check, write_report
from .backtest import code_fingerprint
from .storage import Store, instance_lock
from .strategy import HOUR, signal


def now_ms():
    return int(time.time() * 1000)


class Runner:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.site, self.market = Site(cfg), Market(cfg.market_url)
        self.alerts = Alerts(store, cfg)
        self.engine = Engine(store, cfg, self.site, self.alerts)
        if store.get("code_sha256") != code_fingerprint():
            self.engine.state.update(shadow_last_tick=None, shadow_continuous_ms=0)
            self.engine.save()
            store.put("code_sha256", code_fingerprint())
        self.sig = None
        self.candle_hour = -1
        self.stopping = False
        self.stop_since = None
        self.frame = Frame(now_ms(), 1, market_fresh=False)

    def stop(self, *_):
        self.stopping = True
        self.stop_since = self.stop_since or now_ms()

    def collect(self):
        start = now_ms()
        price, quote, fresh, external_ok = self.frame.price, None, False, False
        with ThreadPoolExecutor(max_workers=2) as pool:
            market = pool.submit(self.market.price, start)
            website = pool.submit(self.site.quote, start)
            try:
                price, stamp = market.result()
                fresh = external_ok = True
                self.alerts.set("market", "Upstream recovered", False)
            except Exception as exc:
                self.alerts.set("market", f"Upstream unavailable ({type(exc).__name__}); new entries blocked.")
            try:
                quote = website.result()
                self.alerts.set("site_quote", "Site quote recovered", False)
            except Exception as exc:
                self.alerts.set("site_quote", f"Site quote unavailable ({type(exc).__name__}); new entries blocked.")
        now = now_ms()
        if not fresh and quote and quote.fresh(now):
            price, fresh = quote.price, True
        if fresh and now - start > 15000:
            fresh = external_ok = False
        try:
            expiry = self.site.expiry()
        except (ValueError, KeyError, OSError):
            expiry = 0
        signal_time = (now // HOUR) * HOUR
        if now % HOUR >= 10000 and self.candle_hour != signal_time:
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    h = pool.submit(self.market.candles, "1h", now)
                    h4 = pool.submit(self.market.candles, "4h", now)
                    self.sig = signal(h.result(), h4.result())
                self.candle_hour = signal_time
                self.alerts.set("candles", "Candle feed recovered", False)
            except Exception as exc:
                self.sig = None
                self.alerts.set("candles", f"Closed candle validation failed ({type(exc).__name__}).")
        divergence = quote and fresh and abs(price / quote.price - 1) > .003
        self.alerts.set("divergence", "Site/upstream divergence > 0.3%; entries blocked.", bool(divergence))
        if now_ms() - start > 15000:
            fresh = False
        frame = Frame(now_ms(), price, quote, self.sig, expiry, fresh)
        frame.entry_enabled = external_ok and now_ms() - start <= 15000
        return frame

    def tick(self):
        self.frame = self.collect()
        if self.stopping:
            self.engine.shutdown(self.frame)
        else:
            self.engine.step(self.frame)
        s = self.engine.state
        good = (self.frame.entry_enabled and self.frame.market_fresh and self.frame.quote and self.frame.quote.fresh(self.frame.now) and
                self.sig is not None and self.frame.expires_at > self.frame.now + DAY and
                abs(self.frame.price / self.frame.quote.price - 1) <= .003)
        if not good and self.cfg.mode == "shadow":
            s["shadow_last_tick"] = None
            s["shadow_continuous_ms"] = 0
            self.engine.save()
        self.store.put("heartbeat", dict(at=now_ms(), mode=self.cfg.mode, healthy=bool(good),
                        halted=s["halt"], pending=bool(s["pending"]), position=bool(s["position"])))
        if self.cfg.mode == "canary" and s["position"] and not s["pending"]:
            self.engine.reconcile(self.frame.now)
            self.engine.sell_intent("canary_roundtrip", self.frame.now)
            self.engine.execute(self.frame)
        if self.cfg.mode == "canary" and s["canary_done"]:
            self.engine.reconcile(self.frame.now)
            write_report(self.cfg, "canary.json", dict(passed=not bool(s["halt"]), stake_units=10000))
            self.stopping = True
        return not (self.stopping and not s["position"] and not s["pending"])


def preflight(cfg):
    site = Site(cfg)
    now = now_ms()
    q = site.quote(now)
    snap = site.snapshot()
    if not q.fresh(now_ms()):
        raise ValueError("site quote stale")
    if site.expiry() <= now + DAY:
        raise ValueError("renew session before starting")
    market = Market(cfg.market_url)
    external, _ = market.price(now)
    if abs(external / q.price - 1) > .003:
        raise ValueError("upstream/site price mismatch")
    hourly = market.candles("1h", now)
    four = market.candles("4h", now)
    site_hourly = site.candles("1h")
    shared = {c.time: c for c in hourly}
    pairs = [(c, shared[c.time]) for c in site_hourly if c.time in shared]
    if len(pairs) < 100 or any(abs(a.close / b.close - 1) > .003 for a, b in pairs[-100:]):
        raise ValueError("site candle source mismatch")
    return dict(account_balance=snap["balance"] / 10000, positions=len(snap["positions"]),
                closed_hourly=len(hourly), closed_four_hourly=len(four), session_expires=site.expiry(),
                strategy_trend=signal(hourly, four).trend)


def run(cfg, canary=False):
    qualification_error = None
    if cfg.mode != "shadow":
        try:
            check(cfg, require_canary=cfg.mode == "live")
        except (ValueError, KeyError) as exc:
            qualification_error = str(exc)
    if (cfg.mode == "canary") != canary:
        raise ValueError("use canary command for canary mode; separate databases required")
    with instance_lock(cfg.database):
        store = Store(cfg.database)
        runner = Runner(cfg, store)
        try:
            os_signal.signal(os_signal.SIGTERM, runner.stop)
            os_signal.signal(os_signal.SIGINT, runner.stop)
            recovering = bool(runner.engine.state["position"] or runner.engine.state["pending"])
            if qualification_error:
                if not recovering:
                    raise ValueError(qualification_error)
                runner.engine.halt("activation_evidence_invalid_exit_only")
            if not recovering:
                preflight(cfg)
            else:
                logging.warning("Recovering persisted exposure; exits take priority over preflight")
            if canary and not recovering:
                runner.frame = runner.collect()
                runner.engine.reconcile(now_ms())
                if (not runner.engine.state["canary_done"] and not runner.engine.state["halt"] and
                        not runner.engine.state["position"] and not runner.engine.state["pending"]):
                    f = runner.frame
                    if (not f.market_fresh or not f.entry_enabled or f.quote is None or
                            not f.quote.fresh(f.now) or abs(f.price / f.quote.price - 1) > .003 or
                            f.expires_at - f.now <= DAY):
                        raise ValueError("canary requires fresh matching quotes and healthy session")
                    runner.engine.buy_intent(10000, .01, (now_ms() // HOUR - 1) * HOUR, now_ms())
                    runner.engine.execute(runner.frame)
            while True:
                start = time.monotonic()
                try:
                    if not runner.tick():
                        break
                except Exception as exc:
                    # Preserve state; do not log exception bodies from remote responses.
                    logging.error("tick failed: %s", type(exc).__name__)
                    runner.alerts.set("runtime", f"Runtime failure ({type(exc).__name__}); inspect status.")
                    runner.engine.sell_intent("runtime_failure", now_ms())
                    if isinstance(exc, SchemaError):
                        runner.engine.halt("schema_changed")
                delay = max(0, cfg.poll_seconds - (time.monotonic() - start))
                time.sleep(delay)
        finally:
            store.close()


def watchdog(cfg):
    # Separate process and DB so a wedged writer cannot silence the watchdog.
    monitor = Store(cfg.database + ".monitor")
    alerts = Alerts(monitor, cfg)
    while True:
        try:
            import sqlite3
            db = sqlite3.connect(f"file:{cfg.database}?mode=ro", uri=True, timeout=2)
            row = db.execute("SELECT value FROM kv WHERE key='heartbeat'").fetchone()
            db.close()
            heartbeat = json.loads(row[0]) if row else {}
            stale = now_ms() - heartbeat.get("at", 0) > 60000
        except Exception:
            stale = True
        alerts.set("trader_heartbeat", "Trader heartbeat missing >60s. Check host/session/open positions." if stale else
                   "Trader heartbeat recovered.", stale)
        time.sleep(15)
