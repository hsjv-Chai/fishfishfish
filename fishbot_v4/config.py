"""V4 configuration: slow trend hold with a wide stop and fixed exposure."""
from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class V4Config:
    mode: str = "shadow"
    capital: float = 50.0
    stake_fraction: float = .2
    leverage: int = 1
    breakout: int = 55
    exit_breakout: int = 55
    stop_atr: float = 2.0
    stop_floor: float = .01
    stop_cap: float = .04
    cooldown_hours: int = 6
    fee: float = .0002
    day_loss: float = .01
    max_drawdown: float = .05
    shadow_slippage: float = .001
    poll_seconds: int = 5
    reconcile_seconds: int = 60
    site_url: str = "https://raricy.com"
    market_url: str = "https://data-api.binance.vision"
    database: str = "data/v4/shadow.sqlite3"
    session_file: str = "secrets/session.json"
    evidence_dir: str = "data/v4/evidence"
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_password_file: str = "secrets/smtp-password"
    smtp_security: str = "ssl"

    @classmethod
    def load(cls, path: str = "v4.toml") -> "V4Config":
        raw = tomllib.loads(Path(path).read_text()) if Path(path).exists() else {}
        cfg = cls(**raw)
        if cfg.mode not in {"shadow", "canary", "live"}:
            raise ValueError("V4 mode must be shadow, canary or live")
        if cfg.capital != 50:
            raise ValueError("V4 fixes capital=50")
        if not .05 <= cfg.stake_fraction <= .3:
            raise ValueError("V4 stake fraction must be within 5%..30%")
        if cfg.leverage != 1:
            raise ValueError("V4 uses 1x")
        if cfg.breakout != 55 or cfg.exit_breakout != 55:
            raise ValueError("V4 freezes the 55-bar entry and exit")
        if cfg.stop_atr != 2 or cfg.stop_floor != .01 or cfg.stop_cap != .04:
            raise ValueError("V4 freezes the ATR stop")
        if cfg.cooldown_hours != 6:
            raise ValueError("V4 fixes a 6-hour cooldown")
        if cfg.fee != .0002 or cfg.day_loss != .01 or cfg.max_drawdown != .05:
            raise ValueError("V4 fee/risk rules are fixed")
        if cfg.site_url != "https://raricy.com":
            raise ValueError("credential destination must be https://raricy.com")
        if not cfg.market_url.startswith("https://"):
            raise ValueError("market URL must use HTTPS")
        return cfg

    @property
    def key(self):
        return f"b{self.breakout}-x{self.exit_breakout}-s{self.stop_atr:g}-f{self.stake_fraction:g}-l1"

    def dict(self):
        return {k: getattr(self, k) for k in self.__dataclass_fields__}
