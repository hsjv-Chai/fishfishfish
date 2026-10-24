"""Single-position V4 engine: slow trend hold with a wide protective stop."""
from fishbot.engine import DAY, Engine, Frame, position_direction, position_liquidation
from fishbot.strategy import HOUR


class V4Engine(Engine):
    def step(self, f):
        s, now = self.state, f.now
        if self.cfg.mode != "shadow":
            if now - self.last_reconcile >= self.cfg.reconcile_seconds * 1000 or not s["initialized"]:
                try:
                    self.reconcile(now)
                except Exception as exc:
                    self.alert("reconciliation", str(exc))
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
        day = self._day(now)
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
        fresh = (sig is not None and sig.time > s["last_signal"] and
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
        elif p and fresh:
            if position_direction(p) == "short":
                if sig.exit_short or not sig.down:
                    reason = "breakdown" if sig.exit_short else "trend_off"
            else:
                if sig.exit_long or not sig.up:
                    reason = "breakdown" if sig.exit_long else "trend_off"
        if p and reason:
            self.sell_intent(reason, now)
            self.execute(f)
        can_open = fresh and not reason and not s["position"] and not s["pending"]
        if can_open and self.cfg.mode != "canary":
            direction = sig.entry_direction
            ready_at = ((s["last_exit"] + HOUR - 1) // HOUR + self.cfg.cooldown_hours) * HOUR
            if (direction and self.entry_safe(f) and now >= ready_at and
                    now - (sig.time + HOUR) <= 60000):
                amount = min(int(equity * self.cfg.stake_fraction // 10000) * 10000, s["cash"])
                distance = min(max(self.cfg.stop_atr * sig.atr / sig.close, self.cfg.stop_floor),
                               self.cfg.stop_cap)
                if amount >= 10000:
                    self.buy_intent(amount, distance, sig.time, now,
                                    direction="long" if direction > 0 else "short",
                                    leverage=1)
                    self.execute(f)
        if fresh:
            s["last_signal"] = sig.time
        if self.cfg.mode == "shadow":
            prior = s["shadow_last_tick"]
            if prior is None or now - prior > 60000:
                s["shadow_started"], s["shadow_continuous_ms"] = now, 0
            else:
                s["shadow_continuous_ms"] += max(0, now - prior)
            s["shadow_last_tick"] = now
        self.save(now=now)

    @staticmethod
    def _day(now):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(now / 1000, ZoneInfo("Asia/Shanghai")).date().isoformat()
