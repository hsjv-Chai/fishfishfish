"""V4 single-policy research: slow trend hold, fixed exposure, wide ATR stop."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json

from fishbot.history import add_months, month_ms, read_minutes
from fishbot_v4.backtest import code_fingerprint, simulate
from fishbot_v4.config import V4Config

FOLD_STARTS = ("2024-09", "2024-12", "2025-03", "2025-06")
COSTS = (0, .0005, .001, .002)


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".new")
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False, ensure_ascii=False))
    tmp.replace(path)


def _summary(metrics, halt, trades):
    natural = [t for t in trades if t.get("reason") != "evaluation_end"]
    wins = [t["profit"] for t in natural if t["profit"] > 0]
    losses = [t["profit"] for t in natural if t["profit"] < 0]
    top = max(wins) / 10000 if wins else 0
    net = metrics.get("net_return") or 0
    return dict(net_return=net, max_drawdown=metrics.get("max_drawdown"),
                trades=metrics.get("trades"), boundary_closures=metrics.get("boundary_closures"),
                exposure=metrics.get("exposure"), final_fish=metrics.get("final_fish"),
                win_rate=(len(wins) / len(natural)) if natural else None,
                profit_factor=(sum(wins) / -sum(losses)) if losses else None,
                top_trade_fish=top, top_trade_share=(top / (net * 50)) if net > 0 else None,
                halt=halt)


def _run(minutes, start, end, cfg, cost, carry=None):
    metrics, trades, curve, halt, state = simulate(minutes, start, end, cfg, cost, carry)
    return _summary(metrics, halt, trades), state


def run(minutes, output="reports/v4", data_path="data/history/minutes.csv"):
    root = Path(output)
    if root.exists():
        raise ValueError("use a new V4 experiment directory")
    root.mkdir(parents=True)
    cfg = V4Config()
    data_sha = hashlib.sha256(Path(data_path).read_bytes()).hexdigest()
    experiment = dict(schema=4, created_at=datetime.now(timezone.utc).isoformat(),
                      code_sha256=code_fingerprint(), data_sha256=data_sha,
                      policy=cfg.dict(), policy_key=cfg.key,
                      status="research_running", live_qualified=False,
                      note="Slow-exit hypothesis was generated after viewing V3; treat history as exploratory and require forward data")
    _atomic(root / "research.json", experiment)
    trials = root / "trials.jsonl"

    dev = {}
    for cost in COSTS:
        m, _ = _run(minutes, month_ms("2024-09"), month_ms("2025-09"), cfg, cost)
        dev[str(cost)] = m
        with trials.open("a") as f:
            f.write(json.dumps(dict(stage="development", cost=cost, policy=cfg.key, metrics=m,
                                    code_sha256=experiment["code_sha256"]), allow_nan=False) + "\n")
    experiment["development"] = dev

    folds = []
    carries = {cost: None for cost in COSTS}
    for start in FOLD_STARTS:
        eval_start = add_months(start, 12)
        eval_end = add_months(eval_start, 3)
        row = dict(train=[start, eval_start], assessment=[eval_start, eval_end], costs={})
        for cost in COSTS:
            m, carries[cost] = _run(minutes, month_ms(eval_start), month_ms(eval_end),
                                    cfg, cost, carries[cost])
            row["costs"][str(cost)] = m
            with trials.open("a") as f:
                f.write(json.dumps(dict(stage="fold", train=row["train"], assessment=row["assessment"],
                                        cost=cost, policy=cfg.key, metrics=m,
                                        code_sha256=experiment["code_sha256"]), allow_nan=False) + "\n")
        folds.append(row)
    experiment["folds"] = folds

    test = {}
    for cost in COSTS:
        test[str(cost)], _ = _run(minutes, month_ms("2026-03"), month_ms("2026-09"), cfg, cost)
        with trials.open("a") as f:
            f.write(json.dumps(dict(stage="test", cost=cost, policy=cfg.key,
                                    metrics=test[str(cost)], code_sha256=experiment["code_sha256"]),
                               allow_nan=False) + "\n")
    experiment["test"] = test

    fold_pass = sum(1 for row in folds if (row["costs"]["0.001"]["net_return"] or -1) > 0)
    test_ok = all(test[c]["net_return"] is not None and test[c]["net_return"] > 0 and
                  test[c]["max_drawdown"] is not None and test[c]["max_drawdown"] <= .05 and
                  (test[c]["trades"] or 0) >= 30 for c in ("0", "0.001"))
    dev_ok = (dev["0.001"]["net_return"] or -1) > 0
    passed = bool(dev_ok and test_ok and fold_pass >= 3 and not any(
        row["costs"]["0.001"]["halt"] for row in folds))
    experiment.update(fold_positive_at_10bp=fold_pass, test_passed=test_ok,
                      development_passed=dev_ok,
                      status="qualified_for_forward_shadow" if passed else "not_qualified",
                      passed=passed, completed_at=datetime.now(timezone.utc).isoformat())
    _atomic(root / "research.json", experiment)
    _report(root, experiment, dev, folds, test)
    return experiment


def _pct(x):
    return "—" if x is None else f"{x * 100:.2f}%"


def _report(root, experiment, dev, folds, test):
    lines = ["# V4 慢趋势持有研究报告", "",
             f"生成时间：{experiment['created_at']}", "",
             f"状态：**{experiment['status']}**", "",
             "规则：4h 趋势 + 1h 55 根突破；55 根反向/趋势反转离场；2×ATR14 初始止损（1%～4%）；"
             "固定 20% 净值投入；1x；6 小时冷却；日亏 1%；回撤 5%。", "",
             f"参数：`{experiment['policy_key']}`", "",
             "## 开发 2024-09～2025-09", "",
             "| 每边偏差 | 净收益 | 最大回撤 | 交易数 | 胜率 | PF | 最大单笔/条 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for cost in ("0", "0.0005", "0.001", "0.002"):
        m = dev[cost]
        lines.append(f"| {cost} | {_pct(m['net_return'])} | {_pct(m['max_drawdown'])} | {m['trades']} | "
                     f"{_pct(m['win_rate'])} | {m['profit_factor'] or 0:.2f} | {m['top_trade_fish']:.4f} |")
    lines += ["", "## 4 个滚动窗口（0.001 偏差，继承状态）", "",
              "| 评估窗口 | 净收益 | 最大回撤 | 交易数 | 停机 |",
              "|---|---:|---:|---:|---|"]
    for row in folds:
        m = row["costs"]["0.001"]
        lines.append(f"| {row['assessment'][0]}～{row['assessment'][1]} | {_pct(m['net_return'])} | "
                     f"{_pct(m['max_drawdown'])} | {m['trades']} | {m['halt'] or '无'} |")
    lines += ["", "## 测试 2026-03～2026-09", "",
              "| 每边偏差 | 净收益 | 最大回撤 | 交易数 | 胜率 | PF | 最大单笔/条 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for cost in ("0", "0.0005", "0.001", "0.002"):
        m = test[cost]
        lines.append(f"| {cost} | {_pct(m['net_return'])} | {_pct(m['max_drawdown'])} | {m['trades']} | "
                     f"{_pct(m['win_rate'])} | {m['profit_factor'] or 0:.2f} | {m['top_trade_fish']:.4f} |")
    lines += ["", "## 判定", "",
              f"- 开发 0.10% 为正：{experiment['development_passed']}",
              f"- 滚动窗口为正：{experiment['fold_positive_at_10bp']}/4",
              f"- 测试（0 与 0.10%，≥30 笔）：{experiment['test_passed']}",
              f"- 最终：**{experiment['status']}**", "",
              "注意：本假设是在查看 V3 结果后提出的，历史测试段不是独立样本外；"
              "只有 2026-10-23 之后的前瞻影子才能验证。", ""]
    (Path(root) / "报告.md").write_text("\n".join(lines))
