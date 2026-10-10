#!/usr/bin/env python3
"""把 benchmark 报告换算成三个分数：单核、多核、性价比（100 = 参考机）。

benchmark.py 每次运行结束时直接 import 本模块打分；也可单独对已有报告打分：
  python scripts/score.py results/benchmark-xxx.json --price 4.2
  python scripts/score.py --make-baseline results/a.json results/b.json ... --ref-price 4.2

价格统一折算为「美元/月」。
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ("replay_only", "scheduled_market_orders", "passive_limit_orders", "bar_ema_cross")
PHASES = {"single": "single_core_results", "multi": "all_core_results"}
SPREAD_LIMIT = 0.2
STEAL_LIMIT_PERCENT = 1.0
SINGLE_CORE_MIN_PERCENT = 90.0


class ScoreError(Exception):
    """无法打分或无法确定价格；调用方决定是退出还是跳过。"""


def eps(report: Mapping[str, Any], phase: str, scenario: str) -> float:
    return report[PHASES[phase]][scenario]["run_only"]["events_per_second"]


def geomean_score(ratios) -> float:
    ratios = list(ratios)
    return 100 * math.exp(sum(math.log(r) for r in ratios) / len(ratios))


def make_baseline(paths, ref_price: float | None, out_dir: Path | None = None) -> Path:
    reports = [json.loads(Path(p).read_text()) for p in paths]
    versions = {r["benchmark_version"] for r in reports}
    commits = {r["source_commit"] for r in reports}
    if len(versions) != 1 or len(commits) != 1:
        raise ScoreError(f"报告的 benchmark_version / source_commit 不一致: {sorted(versions)} {sorted(commits)}")
    baseline = {
        "benchmark_version": versions.pop(),
        "source_commit": commits.pop(),
        "ref_monthly_price": ref_price,
        "events_per_second": {
            phase: {s: statistics.median(eps(r, phase, s) for r in reports) for s in SCENARIOS}
            for phase in PHASES
        },
    }
    out = (out_dir or ROOT / "baselines") / f"v{baseline['benchmark_version']}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(baseline, indent=2) + "\n")
    return out


def load_baseline(version: int, base_dir: Path | None = None) -> dict[str, Any]:
    path = (base_dir or ROOT / "baselines") / f"v{version}.json"
    if not path.exists():
        raise ScoreError(f"缺少参考机文件 baselines/{path.name}，先用 --make-baseline 生成")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ScoreError(f"参考机文件 {path.name} 无法解析: {error}") from error


def quality_flags(report: Mapping[str, Any]) -> list[str]:
    """质量标记：只提示，不改分数。"""
    out = []
    for name, key in (("单核", "single_core_resources"), ("全核", "all_core_resources")):
        steal = report[key]["system_cpu_steal_percent"]
        if steal > STEAL_LIMIT_PERCENT:
            out.append(f"{name} CPU steal {steal:.1f}% > {STEAL_LIMIT_PERCENT:g}%")
    if report["single_core_resources"]["cpu_percent_of_one_core"] < SINGLE_CORE_MIN_PERCENT:
        out.append(f"单核占用 < {SINGLE_CORE_MIN_PERCENT:g}%")
    for phase, key in PHASES.items():
        for scenario in SCENARIOS:
            run = report[key][scenario]["run_only"]
            samples = run.get("samples_events_per_second") or [1000 / m for m in run["samples_ms"]]
            spread = (max(samples) - min(samples)) / statistics.median(samples)
            if spread > SPREAD_LIMIT:
                out.append(f"{phase}/{scenario} sample 极差 {spread:.0%} > {SPREAD_LIMIT:.0%}")
    return out


def compute_scores(
    report: Mapping[str, Any], baseline: Mapping[str, Any], monthly_price_usd: float | None = None
) -> dict[str, Any]:
    if baseline["source_commit"] != report["source_commit"]:
        raise ScoreError("报告与参考机的 source_commit 不一致，不能比较")
    if baseline["benchmark_version"] != report["benchmark_version"]:
        raise ScoreError("报告与参考机的 benchmark_version 不一致，不能比较")
    ratios = {
        phase: {s: eps(report, phase, s) / baseline["events_per_second"][phase][s] for s in SCENARIOS}
        for phase in PHASES
    }
    multi = geomean_score(ratios["multi"].values())
    ref_price = baseline.get("ref_monthly_price")
    value = multi * ref_price / monthly_price_usd if monthly_price_usd and ref_price else None
    return {
        "single": geomean_score(ratios["single"].values()),
        "multi": multi,
        "value": value,
        "ratios": ratios,
        "flags": quality_flags(report),
    }


def dumps_report(report: Mapping[str, Any]) -> str:
    """报告 JSON。文件开头是单核、多核、性价比，其余字段保持字母序。"""
    rest = {key: report[key] for key in report if key != "scores"}
    rest_text = json.dumps(rest, indent=2, sort_keys=True)
    scores = report.get("scores")
    if not scores:
        return rest_text + "\n"
    ordered = {
        "single": scores["single"],
        "multi": scores["multi"],
        "value": scores["value"],
        "ratios": {
            phase: {scenario: scores["ratios"][phase][scenario] for scenario in sorted(scores["ratios"][phase])}
            for phase in ("single", "multi")
        },
        "flags": list(scores["flags"]),
    }
    lines = json.dumps(ordered, indent=2).splitlines()
    embedded = lines[0] + "\n" + "\n".join("  " + line for line in lines[1:])
    return '{\n  "scores": ' + embedded + ",\n" + rest_text[2:] + "\n"


def format_scores(scores: Mapping[str, Any], *, price_note: str = "") -> str:
    lines = ["分数（100 = 参考机）", f"  单核   {scores['single']:6.1f}", f"  多核   {scores['multi']:6.1f}"]
    if scores["value"] is not None:
        lines.append(f"  性价比 {scores['value']:6.1f}")
    else:
        lines.append(f"  性价比    --   {price_note or '(未提供价格)'}")
    lines.append("  场景比值（单核 / 多核）")
    for s in SCENARIOS:
        lines.append(f"    {s:<24}{scores['ratios']['single'][s] * 100:6.1f} / {scores['ratios']['multi'][s] * 100:6.1f}")
    lines.extend(f"  低置信: {flag}" for flag in scores["flags"])
    return "\n".join(lines)


def score_report(
    report: Mapping[str, Any], monthly_price_usd: float | None, price_note: str = ""
) -> tuple[dict[str, Any] | None, str]:
    """返回 (scores, 要打印的文本)。无法打分时 scores 为 None，文本是跳过原因，绝不抛异常。"""
    try:
        scores = compute_scores(report, load_baseline(report["benchmark_version"]), monthly_price_usd)
    except (ScoreError, KeyError) as error:
        return None, f"已跳过打分：{error}"
    return scores, format_scores(scores, price_note=price_note)


def main() -> None:
    from price import interactive_ask, parse_amount, resolve_monthly_price

    ap = argparse.ArgumentParser()
    ap.add_argument("reports", nargs="+")
    ap.add_argument("--price", type=float, help="本机月费，单位美元；省略则按环境变量/本机记录解析")
    ap.add_argument("--make-baseline", action="store_true", help="用给定报告的中位数生成参考机")
    ap.add_argument("--ref-price", type=float, help="参考机月费（美元），配合 --make-baseline")
    args = ap.parse_args()
    try:
        if args.make_baseline:
            print(f"参考机已写入 {make_baseline(args.reports, args.ref_price).relative_to(ROOT)}")
            return
        if len(args.reports) != 1:
            ap.error("打分只接受一份报告")
        report = json.loads(Path(args.reports[0]).read_text())
        if args.price is not None:
            price, note = parse_amount(str(args.price)), ""
        else:
            price, note = resolve_monthly_price(ask=interactive_ask())
        scores = compute_scores(report, load_baseline(report["benchmark_version"]), price)
        print(format_scores(scores, price_note=note))
    except ScoreError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()

