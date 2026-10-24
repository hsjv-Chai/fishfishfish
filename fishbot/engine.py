"""Persistent state machine; trade intent is committed before any POST."""
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
import uuid

from .client import APIError, SchemaError, fish_units
from .math import settle, size, liquidation_price
from .storage import initial_state
from .strategy import HOUR, Signal

DAY = 24 * HOUR


def position_direction(position):
    return position.get("direction", "long")


def position_leverage(position):
    return position.get("leverage", 1)


def position_liquidation(position):
    return position.get("liquidation", 0) or 0


@dataclass
class Frame:
    now: int
    price: float
    quote: object = None
    signal: Signal | None = None
    expires_at: int = 2**62
    market_fresh: bool = True
    entry_enabled: bool = True


class Engine:
    def __init__(self, store, config, site=None, alerts=None):
        self.store, self.cfg, self.site, self.alerts = store, config, site, alerts
        self.state = store.get("state", initial_state(config.capital))
        mode = store.get("mode")
        if mode and mode != config.mode:
            raise ValueError("each mode must use its own database")
        store.put("mode", config.mode)
        # Databases created before direction/leverage support contain long-only,
        # 1x positions. Normalise in memory; the next save persists the new shape.
        if self.state.get("position"):
            self.state["position"].setdefault("direction", "long")
            self.state["position"].setdefault("leverage", 1)
            self.state["position"].setdefault("liquidation", 0.0)
        if self.state.get("pending"):
            self.state["pending"].setdefault("direction", "long")
            self.state["pending"].setdefault("leverage", 1)
        self.last_reconcile = 0

    def save(self, kind=None, data=None, now=None):
        self.store.save(self.state, kind, data, now)

    def alert(self, key, text, active=True):
        if self.alerts:
            self.alerts.set(key, text, active)

    def halt(self, reason):
        self.state["halt"] = reason
        self.save("halt", {"reason": reason})
        self.alert(reason, "Trading paused: " + reason)

    def equity(self, price):
        p = self.state["position"]
        return self.state["cash"] + (settle(p["stake"], p["entry"], price,
                                            position_leverage(p),
                                            direction=position_direction(p)) if p else 0)

    def reconcile(self, now):
        s = self.state
        snap = self.site.snapshot(s["ledger_id"])
        positions = snap["positions"]
        if not s["initialized"]:
            if positions:
                raise SchemaError("initial account must have no existing positions")
            if snap["balance"] < round(self.cfg.capital * 10000):
                raise SchemaError("account balance below strategy budget")
            s.update(initialized=True, ledger_id=snap["newest_id"], account_balance=snap["balance"])
            self.save("account_baseline", {"balance": snap["balance"]}, now)
            self.last_reconcile = now
            return
        rows = [r for r in snap["transactions"] if r["id"] > s["ledger_id"]]
        change = sum(round(r["amount"] * 10000) for r in rows)
        if s["account_balance"] + change != snap["balance"]:
            raise SchemaError("account balance does not match ledger")
        order = s["pending"]
        if order and order["kind"] == "buy":
            new_buys = [r for r in rows if r["type"] == "market_buy" and r["reference_id"] not in s["known_ids"]]
            if len(new_buys) == 1:
                matching = [p for p in positions if p["id"] == new_buys[0]["reference_id"] and
                            p["symbol"] == "BTCUSDT" and
                            p.get("direction", "long") == order.get("direction", "long") and
                            p.get("leverage", 1) == order.get("leverage", 1) and
                            fish_units(p["stake"]) == order["stake"] and
                            round(new_buys[0]["amount"] * 10000) == -order["stake"]]
                if len(matching) == 1:
                    p = matching[0]
                    self.apply_buy(p["id"], p["entryPrice"], order, now, recovered=True,
                                   liquidation=p.get("liquidationPrice"))
                else:
                    raise SchemaError("ambiguous buy requires manual reconciliation")
            elif len(new_buys) > 1:
                raise SchemaError("multiple unknown buys")
        order = s["pending"]
        if order and order["kind"] == "sell":
            sells = [r for r in rows if r["type"] == "market_sell" and r["reference_id"] == order["position_id"]]
            if len(sells) == 1:
                # POST replay is safe and supplies the authoritative exit price.
                receipt = self.site.sell(order["position_id"])
                self.apply_sell(fish_units(receipt["payout"]), receipt["exit_price"], now,
                                liquidated=bool(receipt.get("liquidated")))
        # A liquidation closes the position without a market_sell ledger row. If a
        # durable sell intent exists, replaying sell is intended and returns the
        # authoritative liquidated result; otherwise the position mismatch still
        # fails closed below.
        if (s["pending"] and s["pending"]["kind"] == "sell" and s["position"] and
                s["position"]["id"] not in {p["id"] for p in positions}):
            receipt = self.site.sell(s["pending"]["position_id"])
            self.apply_sell(fish_units(receipt["payout"]), receipt["exit_price"], now,
                            liquidated=bool(receipt.get("liquidated")))
        expected = {s["position"]["id"]} if s["position"] else set()
        actual = {p["id"] for p in positions}
        if actual != expected:
            raise SchemaError("unknown or externally closed position")
        if s["position"]:
            p, remote = s["position"], positions[0]
            if (fish_units(remote["stake"]) != p["stake"] or remote["entryPrice"] != p["entry"] or
                    remote["symbol"] != "BTCUSDT" or
                    remote.get("direction", "long") != position_direction(p) or
                    remote.get("leverage", 1) != position_leverage(p)):
                raise SchemaError("position changed")
        for row in rows:
            if row["type"] in {"market_buy", "market_sell"} and row["reference_id"] not in s["known_ids"]:
                raise SchemaError("unmanaged trade in ledger")
            if row["type"] not in {"market_buy", "market_sell"}:
                self.store.event("external_cashflow_excluded", {"id": row["id"], "amount": row["amount"]}, now)
        if snap["balance"] < s["cash"]:
            raise SchemaError("account withdrawal consumed reserved strategy cash")
        s.update(account_balance=snap["balance"], ledger_id=snap["newest_id"])
        self.save("reconciled", {"ledger_id": s["ledger_id"]}, now)
        self.last_reconcile = now

    def apply_buy(self, pid, price, order, now, recovered=False, liquidation=None):
        s = self.state
        if s["position"]:
            raise SchemaError("unexpected second position")
        direction = order.get("direction", "long")
        leverage = order.get("leverage", 1)
        if liquidation is None:
            liquidation = order.get("liquidation") or liquidation_price(price, leverage, direction)
        s["cash"] -= order["stake"]
        s["position"] = dict(id=pid, stake=order["stake"], entry=price,
                             stop=price * (1 - order["distance"]) if direction == "long"
                             else price * (1 + order["distance"]),
                             peak=price, opened=now, entry_signal=order["signal_time"],
                             direction=direction, leverage=leverage, liquidation=liquidation)
        s["known_ids"].append(pid)
        s["pending"] = None
        self.save("buy_filled", {"id": pid, "price": price, "stake": order["stake"],
                                 "direction": direction, "leverage": leverage,
                                 "liquidation": liquidation, "recovered": recovered}, now)

    def apply_sell(self, payout, price, now, liquidated=False):
        s, p = self.state, self.state["position"]
        if not p:
            raise SchemaError("unexpected sell receipt")
        direction, leverage = position_direction(p), position_leverage(p)
        expected = settle(p["stake"], p["entry"], price, leverage, direction=direction)
        # Apply authoritative cash before surfacing any math disagreement.
        s["cash"] += payout
        reason = s["pending"].get("reason") if s["pending"] else (
            "liquidated" if liquidated else "unknown")
        s.update(position=None, pending=None, last_exit=now)
        self.save("sell_filled", {"id": p["id"], "price": price, "payout": payout,
                                  "profit": payout - p["stake"], "reason": reason,
                                  "direction": direction, "leverage": leverage,
                                  "liquidated": bool(liquidated)}, now)
        if payout != expected:
            self.halt("settlement_mismatch")
        if self.cfg.mode == "canary":
            s["canary_done"] = True
            self.save("canary_closed", {"settlement_match": payout == expected}, now)

    def buy_intent(self, stake, distance, signal_time, now, direction="long", leverage=1, liquidation=0):
        if direction not in {"long", "short"} or isinstance(leverage, bool) or not isinstance(leverage, int) or not 1 <= leverage <= 100:
            raise ValueError("invalid direction/leverage")
        self.state["pending"] = dict(kind="buy", key="f-" + uuid.uuid4().hex, stake=stake,
                                     distance=distance, signal_time=signal_time, created=now,
                                     direction=direction, leverage=leverage, liquidation=liquidation,
                                     attempts=0, next_try=now, sent=False, uncertain=False)
        self.save("buy_intent", self.state["pending"], now)

    def sell_intent(self, reason, now):
        s = self.state
        if s["position"] and not s["pending"]:
            s["pending"] = dict(kind="sell", position_id=s["position"]["id"], reason=reason,
                                attempts=0, next_try=now, created=now)
            self.save("sell_intent", s["pending"], now)

    def execute(self, frame):
        s, now = self.state, frame.now
        order = s["pending"]
        if not order or now < order["next_try"]:
            return
        if order["kind"] == "buy" and order["sent"] and self.cfg.mode != "shadow":
            # Read back state before any retry. A missing response is not a rejection.
            self.reconcile(now)
            order = s["pending"]
            if not order:
                return
            if now - order["created"] > 60000 or s["halt"] or not self.entry_safe(frame):
                if order.get("uncertain", True):
                    self.halt("ambiguous_buy")
                    order["next_try"] = now + 60000
                    self.save()
                else:
                    s["pending"] = None
                    self.save("buy_expired_unfilled", {"key": order["key"]}, now)
                return
        if order["kind"] == "buy" and s["halt"]:
            return
        order["attempts"] += 1
        order["sent"] = True
        # A process crash after committing this record must be treated as uncertain.
        previously_uncertain = order.get("uncertain", False)
        if order["kind"] == "buy":
            order["uncertain"] = True
        self.save("order_attempt", {"kind": order["kind"], "attempt": order["attempts"]}, now)
        try:
            if self.cfg.mode == "shadow":
                if not frame.market_fresh:
                    raise APIError(503)
                if order["kind"] == "buy":
                    direction = order.get("direction", "long")
                    slip = self.cfg.shadow_slippage
                    fill = frame.price * (1 - slip if direction == "short" else 1 + slip)
                    self.apply_buy("shadow-" + order["key"], fill, order, now)
                else:
                    p = s["position"]
                    direction = position_direction(p)
                    slip = self.cfg.shadow_slippage
                    price = frame.price * (1 - slip if direction == "long" else 1 + slip)
                    self.apply_sell(settle(p["stake"], p["entry"], price,
                                           position_leverage(p), direction=direction), price, now)
            elif order["kind"] == "buy":
                data = self.site.buy(order)
                position = data["position"]
                self.apply_buy(position["id"], position["entry_price"], order, now,
                               liquidation=position.get("liquidation_price"))
            else:
                data = self.site.sell(order["position_id"])
                self.apply_sell(fish_units(data["payout"]), data["exit_price"], now,
                                liquidated=bool(data.get("liquidated")))
            self.alert("order_failure", "Order execution recovered", False)
        except APIError as exc:
            if order["kind"] == "buy" and exc.code in (400, 401, 403, 429, 503) and not isinstance(exc, SchemaError):
                order["uncertain"] = previously_uncertain
            order["next_try"] = now + max(5, min(300, 5 * 2 ** min(order["attempts"] - 1, 6)), exc.retry_after) * 1000
            self.save("order_failed", {"kind": order["kind"], "status": exc.code}, now)
            self.alert("order_failure", f"{order['kind']} pending; status={exc.code}. Position may remain open.")
            if exc.code in (401, 403):
                self.alert("session", "Login/permission failure; re-authenticate to restore exits.")
            if isinstance(exc, SchemaError) or exc.code in (400, 404):
                self.halt("order_rejected_or_schema_changed")
                if order["kind"] == "buy" and not order.get("uncertain"):
                    s["pending"] = None
                    self.save()

    def entry_safe(self, f):
        return (f.entry_enabled and f.market_fresh and f.quote is not None and f.quote.fresh(f.now) and
                abs(f.price / f.quote.price - 1) <= .003 and
                f.expires_at - f.now > DAY and
                f.signal is not None and abs(f.price / f.signal.close - 1) <= .003)

    def step(self, f):
        s, now = self.state, f.now
        if self.cfg.mode != "shadow":
            if now - self.last_reconcile >= self.cfg.reconcile_seconds * 1000 or not s["initialized"]:
                try:
                    self.reconcile(now)
                except APIError as exc:
                    self.alert("reconciliation", str(exc))
                    # A failed read must not block an already durable exit intent.
                    if isinstance(exc, SchemaError):
                        self.halt("reconciliation_mismatch")
                    if s["position"]:
                        self.sell_intent("reconciliation_failure", now)
                        self.execute(f)
                    return
                self.alert("reconciliation", "Account reconciled", False)
        if s["pending"]:
            self.execute(f)
            if s["pending"]:
                return
        if not f.market_fresh:
            if s["position"]:
                self.sell_intent("all_prices_unavailable", now)
                self.execute(f)
            return
        equity = self.equity(f.price)
        day = datetime.fromtimestamp(now / 1000, ZoneInfo("Asia/Shanghai")).date().isoformat()
        if s["day"] != day:
            s.update(day=day, day_base=equity, day_pause="")
        s["high_water"] = max(s["high_water"], equity)
        self.store.equity(now, equity, bool(s["position"]))
        if equity <= s["high_water"] * (1 - self.cfg.max_drawdown):
            s["halt"] = "max_drawdown"
            self.alert("max_drawdown", "5% high-water drawdown reached; manual review required.")
        if equity <= s["day_base"] * (1 - self.cfg.day_loss):
            s["day_pause"] = day
            self.alert("daily_loss", "1% daily loss reached; paused until next Beijing day.")
        remaining = f.expires_at - now
        self.alert("session_expiry", "Session expires within 72 hours; renew it.", remaining <= 3 * DAY)
        sig = f.signal
        fresh_signal = (sig is not None and sig.time > s["last_signal"] and
                        now >= sig.time + HOUR + 10000)
        p = s["position"]
        reason = None
        if s["halt"]:
            reason = s["halt"]
        elif s["day_pause"] == day:
            reason = "daily_loss"
        elif remaining <= DAY:
            reason = "session_expiring"
        elif p and ((f.price >= p["stop"]) if position_direction(p) == "short" else (f.price <= p["stop"])):
            reason = "stop"
        elif p and position_liquidation(p) > 0 and (
                (f.price >= position_liquidation(p)) if position_direction(p) == "short"
                else (f.price <= position_liquidation(p))):
            reason = "liquidation"
        elif p and fresh_signal:
            # Include only candles closing after actual entry, never pre-entry highs.
            if position_direction(p) == "short":
                if sig.time + HOUR > p["opened"]:
                    p["peak"] = min(p["peak"], sig.close)
                    p["stop"] = min(p["stop"], p["peak"] + 3 * sig.atr)
                if f.price >= p["stop"]:
                    reason = "trailing_stop"
            else:
                if sig.time + HOUR > p["opened"]:
                    p["peak"] = max(p["peak"], sig.close)
                    p["stop"] = max(p["stop"], p["peak"] - 3 * sig.atr)
                if sig.breakdown or not sig.trend:
                    reason = "breakdown" if sig.breakdown else "trend_off"
                elif f.price <= p["stop"]:
                    reason = "trailing_stop"
        if p and reason:
            self.sell_intent(reason, now)
            self.execute(f)
        can_open = fresh_signal and not reason and not s["position"] and not s["pending"]
        if can_open and self.cfg.mode != "canary":
            # The first partial hour after exit does not count as a complete cooldown bar.
            ready_at = ((s["last_exit"] + HOUR - 1) // HOUR + 6) * HOUR
            if (sig.trend and sig.breakout and self.entry_safe(f) and now >= ready_at and
                    now - (sig.time + HOUR) <= 60000):
                amount = size(equity, s["cash"], sig.distance)
                if amount:
                    self.buy_intent(amount, sig.distance, sig.time, now)
                    self.execute(f)
        if fresh_signal:
            s["last_signal"] = sig.time
        if self.cfg.mode == "shadow":
            prior = s["shadow_last_tick"]
            if prior is None or now - prior > 60000:
                s["shadow_started"], s["shadow_continuous_ms"] = now, 0
            else:
                s["shadow_continuous_ms"] += max(0, now - prior)
            s["shadow_last_tick"] = now
        self.save(now=now)

    def shutdown(self, frame):
        if self.state["pending"] and self.state["pending"]["kind"] == "buy":
            self.halt("shutdown_with_uncertain_buy")
        self.sell_intent("shutdown", frame.now)
        self.execute(frame)
