"""V3 configuration. Separate from V1: V3 chooses its own risk and policy."""
from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class V3Config:
    mode: str = "shadow"
    capital: float = 50.0
    risk: float = .0025
    allocation: float = .2
    leverage: int = 1
    breakout: int = 20
    trail_atr: float = 3.0
    time_stop_hours: int = 120
    cooldown_hours: int = 6
    fee: float = .0002
    execution_budget: float = .001
    day_loss: float = .01
    max_drawdown: float = .05
    shadow_slippage: float = .001
    poll_seconds: int = 5
    reconcile_seconds: int = 60
    site_url: str = "https://raricy.com"
    market_url: str = "https://data-api.binance.vision"
    database: str = "data/v3/shadow.sqlite3"
    session_file: str = "secrets/session.json"
    evidence_dir: str = "data/v3/evidence"
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_password_file: str = "secrets/smtp-password"
    smtp_security: str = "ssl"

    @classmethod
    def load(cls, path: str = "v3.toml") -> "V3Config":
        raw = tomllib.loads(Path(path).read_text()) if Path(path).exists() else {}
        cfg = cls(**raw)
        if cfg.mode not in {"shadow", "canary", "live"}:
            raise ValueError("V3 mode must be shadow, canary or live")
        if cfg.capital != 50:
            raise ValueError("V3 fixes capital=50")
        if not .001 <= cfg.risk <= .005:
            raise ValueError("V3 risk must be within 0.10%..0.50%")
        if not .05 <= cfg.allocation <= .5:
            raise ValueError("V3 allocation must be within 5%..50%")
        if cfg.leverage != 1:
            raise ValueError("V3 uses 1x; high leverage is structurally incompatible")
        if cfg.breakout not in (20, 55):
            raise ValueError("V3 breakout must be 20 or 55")
        if cfg.trail_atr not in (3, 4):
            raise ValueError("V3 trail_atr must be 3 or 4")
        if cfg.time_stop_hours not in (0, 120):
            raise ValueError("V3 time stop must be 120 hours or disabled")
        if cfg.cooldown_hours != 6:
            raise ValueError("V3 fixes a 6-hour cooldown")
        if cfg.fee != .0002 or cfg.day_loss != .01 or cfg.max_drawdown != .05:
            raise ValueError("V3 fee/risk rules are fixed")
        if cfg.site_url != "https://raricy.com":
            raise ValueError("credential destination must be https://raricy.com")
        if not cfg.market_url.startswith("https://"):
            raise ValueError("market URL must use HTTPS")
        return cfg

    @property
    def key(self):
        return (f"b{self.breakout}-t{self.trail_atr:g}-r{self.risk:g}"
                f"-s{self.time_stop_hours}-l{self.leverage}")

    def dict(self):
        return {k: getattr(self, k) for k in self.__dataclass_fields__}
