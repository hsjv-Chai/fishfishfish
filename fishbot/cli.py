import argparse
from dataclasses import replace
from datetime import datetime, timezone
import getpass
import json
import logging
from pathlib import Path
import subprocess
import sys
import time

from .config import Config
from .storage import Store, instance_lock
from .gates import check, write_report, test_fingerprint
from .client import APIError


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fishbot")
    parser.add_argument("--config", default="config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("login", "doctor", "status", "run", "watchdog", "canary", "gates", "email-test", "qualify-shadow", "drills"):
        sub.add_parser(name)
    download = sub.add_parser("download")
    download.add_argument("--start", default="2024-03")
    download.add_argument("--end", default="2026-08")
    download.add_argument("--directory", default="data/history")
    backtest = sub.add_parser("backtest")
    backtest.add_argument("--data", default="data/history/minutes.csv")
    backtest.add_argument("--start", default="2024-09")
    backtest.add_argument("--output", default="data/evidence")
    resume = sub.add_parser("resume")
    resume.add_argument("--reason", required=True)
    resume.add_argument("--reset-high-water", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.load(args.config)
    try:
        if args.command == "login":
            from .client import login
            username = input("raricy.com username: ")
            expires = login(cfg, username, getpass.getpass("Password (not stored): "))
            print("Session saved with mode 600. Expires:", datetime.fromtimestamp(expires, timezone.utc).isoformat())
        elif args.command == "download":
            from .history import download
            print(download(args.start, args.end, args.directory))
        elif args.command == "backtest":
            from .backtest import run_backtest
            result = run_backtest(args.data, args.start, args.output)
            print(json.dumps({"status": result["status"], "test": result["partitions"]["test"]}, indent=2))
        elif args.command == "doctor":
            from .runtime import preflight
            print(json.dumps(preflight(cfg), indent=2))
        elif args.command in {"run", "canary"}:
            from .runtime import run
            run(cfg, canary=args.command == "canary")
        elif args.command == "watchdog":
            from .runtime import watchdog
            watchdog(cfg)
        elif args.command == "gates":
            check(cfg)
            print("All activation gates passed.")
        elif args.command == "drills":
            command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
            result = subprocess.run(command, capture_output=True, text=True)
            print(result.stdout + result.stderr)
            import re
            match = re.search(r"Ran (\d+) tests?", result.stderr)
            count = int(match[1]) if match else 0
            passed = result.returncode == 0 and count >= 20
            write_report(cfg, "drills.json", dict(passed=passed, count=count, tests_sha256=test_fingerprint()))
            if not passed:
                return 1
        elif args.command == "email-test":
            from .alerts import Alerts
            if not cfg.smtp_to or not cfg.smtp_host:
                raise ValueError("configure SMTP first")
            store = Store(cfg.database)
            try:
                Alerts(store, cfg).send("[Fishbot] Delivery test", "Fishbot email delivery test. No credentials included.")
                write_report(cfg, "email.json", dict(sent=True))
            finally:
                store.close()
        else:
            if args.command == "status":
                store = Store(cfg.database)
                try:
                    print(json.dumps(dict(mode=store.get("mode"), state=store.get("state"), heartbeat=store.get("heartbeat")), indent=2))
                finally:
                    store.close()
            else:
                with instance_lock(cfg.database):
                    store = Store(cfg.database)
                    try:
                        s = store.get("state")
                        if not s:
                            raise ValueError("no strategy state")
                        if args.command == "resume":
                            if s["position"] or s["pending"]:
                                raise ValueError("resolve orders and flatten before resuming")
                            if s["halt"] == "max_drawdown" and not args.reset_high_water:
                                raise ValueError("drawdown reset requires --reset-high-water after review")
                            if args.reset_high_water:
                                s["high_water"] = s["cash"]
                            s["halt"] = ""
                            # The day's loss pause is deliberately retained.
                            store.save(s, "manual_resume", {"reason": args.reason, "high_water_reset": args.reset_high_water})
                        elif args.command == "qualify-shadow":
                            from .backtest import code_fingerprint
                            from .runtime import preflight
                            heartbeat = store.get("heartbeat", {})
                            if store.get("mode") != "shadow" or store.get("code_sha256") != code_fingerprint():
                                raise ValueError("shadow DB/code mismatch")
                            if time.time() * 1000 - heartbeat.get("at", 0) > 60000 or not heartbeat.get("healthy"):
                                raise ValueError("shadow heartbeat must be healthy and <=60 seconds old")
                            result = preflight(cfg)
                            write_report(cfg, "shadow.json", dict(continuous_ms=s["shadow_continuous_ms"],
                                          halt=s["halt"], reconciled=result["positions"] == 0))
                    finally:
                        store.close()
    except (ValueError, RuntimeError, KeyError, OSError, APIError) as exc:
        logging.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
