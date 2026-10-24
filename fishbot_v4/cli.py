import argparse
import json
import subprocess
import sys

from fishbot_v4.config import V4Config


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fishbot_v4")
    parser.add_argument("--config", default="v4.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    research = sub.add_parser("research")
    research.add_argument("--data", default="data/history/minutes.csv")
    research.add_argument("--output", default="reports/v3")
    backtest = sub.add_parser("backtest")
    backtest.add_argument("--data", default="data/history/minutes.csv")
    backtest.add_argument("--output", default="reports/v4-backtest")
    sub.add_parser("drills")
    args = parser.parse_args(argv)
    cfg = V4Config.load(args.config)
    if args.command == "research":
        from fishbot_v4.research import run
        from fishbot.history import read_minutes
        print(json.dumps(run(list(read_minutes(args.data)), args.output, args.data),
                         ensure_ascii=False, indent=2))
    elif args.command == "backtest":
        from fishbot_v4.backtest import evaluate
        from fishbot.history import read_minutes
        from pathlib import Path
        import hashlib
        data = list(read_minutes(args.data))
        result = evaluate(data, "2024-09", "2026-09", cfg,
                          hashlib.sha256(Path(args.data).read_bytes()).hexdigest())
        Path(args.output).mkdir(parents=True, exist_ok=True)
        (Path(args.output) / "backtest.json").write_text(json.dumps(result, indent=2, allow_nan=False))
        print(json.dumps(dict(status=result["status"], test=result["partitions"]["test"]),
                         ensure_ascii=False, indent=2))
    elif args.command == "drills":
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests_v4", "-v"],
                                capture_output=True, text=True)
        print(result.stdout + result.stderr)
        return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
