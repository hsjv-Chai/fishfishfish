from pathlib import Path
import hashlib
import json
import time

from .backtest import code_fingerprint
from .engine import DAY


def test_fingerprint():
    digest = hashlib.sha256()
    for p in sorted((Path(__file__).parent.parent / "tests").glob("test*.py")):
        digest.update(p.name.encode())
        digest.update(p.read_bytes())
    return digest.hexdigest()


def load_report(directory, name):
    path = Path(directory) / name
    if not path.exists():
        raise ValueError(f"missing evidence: {name}")
    data = json.loads(path.read_text())
    if data.get("code_sha256") != code_fingerprint():
        raise ValueError(f"evidence belongs to another code version: {name}")
    return data


def check(config, require_canary=True):
    d = config.evidence_dir
    report = load_report(d, "backtest.json")
    # Recompute acceptance; do not trust just a passed flag.
    test = report["partitions"]["test"]
    for level in ("0", "0.001"):
        m = test[level]
        if not (m["net_return"] > 0 and m["max_drawdown"] <= .05 and m["trades"] >= 30):
            raise ValueError("out-of-sample backtest not qualified")
    drills = load_report(d, "drills.json")
    if not drills.get("passed") or drills.get("tests_sha256") != test_fingerprint():
        raise ValueError("fault drills missing/outdated/failed")
    shadow = load_report(d, "shadow.json")
    if shadow.get("continuous_ms", 0) < 14 * DAY or shadow.get("halt") or not shadow.get("reconciled"):
        raise ValueError("14-day uninterrupted verified shadow run not complete")
    if time.time() - shadow.get("created_at", 0) > 86400:
        raise ValueError("shadow qualification must be refreshed within 24 hours")
    if require_canary:
        canary = load_report(d, "canary.json")
        if not canary.get("passed") or canary.get("stake_units") != 10000:
            raise ValueError("one-fish round-trip reconciliation not complete")
    if not config.smtp_host or not config.smtp_to:
        raise ValueError("email alerts required for canary/live mode")
    mail = load_report(d, "email.json")
    if not mail.get("sent") or time.time() - mail.get("created_at", 0) > 86400:
        raise ValueError("run email-test within 24 hours before activation")


def write_report(config, name, data):
    directory = Path(config.evidence_dir)
    directory.mkdir(parents=True, exist_ok=True)
    value = dict(data, code_sha256=code_fingerprint(), created_at=time.time())
    path = directory / name
    tmp = path.with_suffix(".new")
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False))
    tmp.replace(path)
    return value
