from dataclasses import dataclass
from pathlib import Path
import os
import tomllib


@dataclass(frozen=True)
class Config:
    mode: str = "shadow"
    capital: float = 50.0
    site_url: str = "https://raricy.com"
    market_url: str = "https://data-api.binance.vision"
    database: str = "data/shadow.sqlite3"
    session_file: str = "secrets/session.json"
    poll_seconds: int = 5
    risk: float = 0.0025
    allocation: float = 0.20
    fee: float = 0.0002
    execution_budget: float = 0.001
    day_loss: float = 0.01
    max_drawdown: float = 0.05
    shadow_slippage: float = 0.001
    reconcile_seconds: int = 60
    evidence_dir: str = "data/evidence"
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_password_file: str = "secrets/smtp-password"
    smtp_security: str = "ssl"

    @classmethod
    def load(cls, path: str = "config.toml") -> "Config":
        raw = tomllib.loads(Path(path).read_text()) if Path(path).exists() else {}
        cfg = cls(**raw)
        if cfg.mode not in {"shadow", "canary", "live"}:
            raise ValueError("mode must be shadow, canary or live")
        if cfg.capital != 50 or cfg.poll_seconds != 5:
            raise ValueError("v1 fixes capital=50 and poll_seconds=5")
        if (cfg.risk, cfg.allocation, cfg.fee, cfg.execution_budget,
                cfg.day_loss, cfg.max_drawdown) != (.0025, .2, .0002, .001, .01, .05):
            raise ValueError("v1 risk rules are fixed; changes require revalidation")
        if cfg.smtp_security not in {"ssl", "starttls"}:
            raise ValueError("SMTP requires TLS")
        if cfg.site_url != "https://raricy.com":
            raise ValueError("credential destination must be https://raricy.com")
        if not cfg.market_url.startswith("https://"):
            raise ValueError("market URL must use HTTPS")
        return cfg


def private_file(path: str) -> Path:
    p = Path(path)
    if not p.is_file() or p.is_symlink():
        raise ValueError(f"missing regular secret file: {p}")
    if os.name == "posix" and p.stat().st_mode & 0o077:
        raise ValueError(f"secret file must have mode 600: {p}")
    return p
