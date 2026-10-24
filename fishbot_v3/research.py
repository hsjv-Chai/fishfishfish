"""Pre-registered 16-policy research: first development window selects, later data only evaluates."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json

from fishbot.history import add_months, month_ms, read_minutes
from fishbot_v3.backtest import code_fingerprint, simulate
from fishbot_v3.config import V3Config

GRID = tuple(replace(V3Config(), breakout=b, trail_atr=t, risk=r, time_stop_hours=s)
             for b in (20, 55) for t in (3, 4) for r in (.0025, .0035) for s in (120, 0))
FOLD_STARTS = ("2024-09", "2024-12", "2025-03", "2025-06")
COSTS = (0, .0005, .001, .002)


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".new")
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False, ensure_ascii=False))
    tmp.replace(path)


def _summary(metrics, halt):
    keys = ("net_return", "max_drawdown", "trades", "boundary_closures", "exposure",
            "final_fish", "daily_sharpe", "win_rate", "profit_factor", "average_profit")
    return {k: metrics.get(k) for k in keys} | {"halt": halt}


def _run(minutes, start, end, cfg, cost, carry=None):
    metrics, trades, curve, halt, state = simulate(minutes, start, end, cfg, cost, carry)
    return _summary(metrics, halt), state


def run(minutes, output="reports/v3", data_path="data/history/minutes.csv"):
    root = Path(output)
    if root.exists():
        raise ValueError("use a new V3 experiment directory")
    root.mkdir(parents=True)
    data_sha = hashlib.sha256(Path(data_path).read_bytes()).hexdigest()
    created = datetime.now(timezone.utc).isoformat()
    experiment = dict(schema=3, created_at=created, code_sha256=code_fingerprint(),
                      data_sha256=data_sha, grid=[c.dict() for c in GRID],
                      selection_stage="first development window only",
                      costs=list(COSTS), folds=[], selected=None, status="research_running",
                      live_qualified=False)
    _atomic(root / "research.json", experiment)
    trials = root / "trials.jsonl"
    development = month_ms(FOLD_STARTS[0]), month_ms(add_months(FOLD_STARTS[0], 12))
    scored = []
    for cfg in GRID:
        s, _ = _run(minutes, *development, cfg, .001)
        s.update(policy=cfg.key)
        scored.append((cfg, s))
        with trials.open("a") as f:
            f.write(json.dumps(dict(stage="development", policy=cfg.dict(), metrics=s,
                                    code_sha256=experiment["code_sha256"]), allow_nan=False) + "\n")
    eligible = [x for x in scored if x[1]["max_drawdown"] is not None and
                x[1]["max_drawdown"] <= .04 and (x[1]["trades"] or 0) >= 30]
    pool = eligible or scored
    selected, selected_dev = max(pool, key=lambda x: ((x[1]["net_return"] or -1e9),
                                                      -(x[1]["max_drawdown"] or 1),
                                                      x[0].key))
    experiment.update(selected=selected.dict() | {"key": selected.key},
                      selected_development=selected_dev,
                      development_eligible=bool(eligible))
    _atomic(root / "research.json", experiment)

    folds = []
    carries = {cost: None for cost in COSTS}
    for start in FOLD_STARTS:
        eval_start = add_months(start, 12)
        eval_end = add_months(eval_start, 3)
        row = dict(train=[start, eval_start], assessment=[eval_start, eval_end], costs={})
        for cost in COSTS:
            m, carries[cost] = _run(minutes, month_ms(eval_start), month_ms(eval_end),
                                    selected, cost, carries[cost])
            row["costs"][str(cost)] = m
            with trials.open("a") as f:
                f.write(json.dumps(dict(stage="fold", train=row["train"], assessment=row["assessment"],
                                        cost=cost, policy=selected.dict(), metrics=m,
                                        code_sha256=experiment["code_sha256"]), allow_nan=False) + "\n")
        folds.append(row)
    experiment["folds"] = folds

    test = {}
    for cost in COSTS:
        test[str(cost)], _ = _run(minutes, month_ms("2026-03"), month_ms("2026-09"), selected, cost)
        with trials.open("a") as f:
            f.write(json.dumps(dict(stage="test", cost=cost, policy=selected.dict(),
                                    metrics=test[str(cost)], code_sha256=experiment["code_sha256"]),
                               allow_nan=False) + "\n")
    experiment["test"] = test
    fold_pass = sum(1 for row in folds if (row["costs"]["0.001"]["net_return"] or -1) > 0)
    test_ok = all(test[c]["net_return"] is not None and test[c]["net_return"] > 0 and
                  test[c]["max_drawdown"] is not None and test[c]["max_drawdown"] <= .05 and
                  (test[c]["trades"] or 0) >= 30 for c in ("0", "0.001"))
    passed = bool(eligible) and test_ok and fold_pass >= 3 and not any(
        row["costs"]["0.001"]["halt"] for row in folds)
    experiment.update(status="qualified_for_shadow" if passed else "not_qualified",
                      fold_positive_at_10bp=fold_pass, test_passed=test_ok,
                      passed=passed, completed_at=datetime.now(timezone.utc).isoformat())
    _atomic(root / "research.json", experiment)
    _report(root, experiment, scored, folds, test)
    return experiment


def _pct(x):
    return "—" if x is None else f"{x * 100:.2f}%"


def _report(root, experiment, scored, folds, test):
    lines = ["# V3 对称趋势研究报告", "",
             f"生成时间：{experiment['created_at']}", "",
             f"状态：**{experiment['status']}**", "",
             "## 预注册参数", "",
             f"共 {len(GRID)} 组：突破 20/55，跟踪 ATR 3/4，风险 0.25%/0.35%，时间止损 120h/关闭。",
             "只用第一个开发窗口选择参数；后续窗口和测试段不重新选择。", "",
             "## 开发窗口（2024-09～2025-09，0.10% 偏差）", "",
             "| 参数 | 净收益 | 最大回撤 | 交易数 |",
             "|---|---:|---:|---:|"]
    for cfg, s in scored:
        mark = " **选中**" if cfg.key == experiment["selected"].get("key") else ""
        lines.append(f"| {cfg.key}{mark} | {_pct(s['net_return'])} | {_pct(s['max_drawdown'])} | {s['trades']} |")
    lines += ["", "## 4 个滚动评估窗口（选中参数，0.10% 偏差）", "",
              "| 评估窗口 | 净收益 | 最大回撤 | 交易数 | 停机 |",
              "|---|---:|---:|---:|---|"]
    for row in folds:
        m = row["costs"]["0.001"]
        lines.append(f"| {row['assessment'][0]}～{row['assessment'][1]} | {_pct(m['net_return'])} | "
                     f"{_pct(m['max_drawdown'])} | {m['trades']} | {m['halt'] or '无'} |")
    lines += ["", "## 测试段（2026-03～2026-09）", "",
              "| 每边额外偏差 | 净收益 | 最大回撤 | 交易数 | 停机 |",
              "|---|---:|---:|---:|---|"]
    for cost in ("0", "0.0005", "0.001", "0.002"):
        m = test[cost]
        lines.append(f"| {cost} | {_pct(m['net_return'])} | {_pct(m['max_drawdown'])} | {m['trades']} | {m['halt'] or '无'} |")
    lines += ["", "## 判定", "",
              f"- 开发段是否有满足回撤/样本要求的参数：{experiment['development_eligible']}",
              f"- 滚动窗口 0.10% 偏差为正的窗口数：{experiment['fold_positive_at_10bp']}/4",
              f"- 测试段是否通过：{experiment['test_passed']}",
              f"- 最终：**{experiment['status']}**", "",
              "限制：1m OHLC 无法还原 5 秒价格路径；所有历史数据已被查看，不是独立样本外；"
              "最终证据必须来自规则冻结后的前瞻影子。", ""]
    (Path(root) / "报告.md").write_text("\n".join(lines))
