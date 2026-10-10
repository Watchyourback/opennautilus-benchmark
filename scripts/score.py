#!/usr/bin/env python3
"""把 benchmark 报告换算成三个分数：单核、多核、性价比（100 = 参考机）。

benchmark.py 每次运行结束时直接 import 本模块打分；也可单独对已有报告打分：
  python scripts/score.py results/benchmark-xxx.json --price 4.2
  python scripts/score.py --make-baseline results/a.json results/b.json ... --ref-price 4.2

价格统一折算为「美元/月」。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ("replay_only", "scheduled_market_orders", "passive_limit_orders", "bar_ema_cross")
PHASES = {"single": "single_core_results", "multi": "all_core_results"}
LOCAL_RECORD = ROOT / ".benchmark-local.json"
RATE_URL = "https://api.frankfurter.dev/v1/latest?base={currency}&symbols=USD"
CURRENCY_ALIASES = {"RMB": "CNY", "元": "CNY", "$": "USD", "€": "EUR", "美元": "USD", "欧元": "EUR", "人民币": "CNY"}
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


# ---------- 价格：环境变量 → 本机记录 → 交互询问 ----------

def parse_amount(raw: str) -> float:
    try:
        value = float(raw.strip().replace(",", ""))
    except ValueError:
        raise ScoreError(f"价格不是数字: {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise ScoreError(f"价格必须大于 0: {raw!r}")
    return value


def normalize_currency(raw: str) -> str:
    code = CURRENCY_ALIASES.get(raw.strip(), raw.strip().upper())
    code = CURRENCY_ALIASES.get(code, code)
    if len(code) != 3 or not code.isalpha():
        raise ScoreError(f"无法识别的币种: {raw!r}（请用 USD / EUR / CNY 这类三字母代码，RMB 也可以）")
    return code


def fetch_usd_rate(currency: str, timeout: float = 10.0) -> tuple[float, str]:
    """联网查 1 单位币种 = ? 美元，返回 (汇率, 汇率日期)。数据源 Frankfurter（欧洲央行日汇率）。"""
    if currency == "USD":
        return 1.0, "-"
    try:
        # 默认的 Python-urllib User-Agent 会被该服务 403 拒绝，需要自定义。
        request = urllib.request.Request(
            RATE_URL.format(currency=currency), headers={"User-Agent": "opennautilus-benchmark/1.0"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        return float(payload["rates"]["USD"]), str(payload["date"])
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as error:
        raise ScoreError(f"获取 {currency}→USD 汇率失败: {error}") from error


def to_monthly_usd(
    amount: float, currency: str, period: str, fetch_rate: Callable[[str], tuple[float, str]] = fetch_usd_rate
) -> tuple[float, dict[str, Any]]:
    rate, rate_date = fetch_rate(currency)
    monthly = amount * rate / (12 if period == "y" else 1)
    return monthly, {"amount": amount, "currency": currency, "period": period, "rate": rate, "rate_date": rate_date}


def machine_fingerprint() -> str:
    """机器指纹的哈希；整目录复制到另一台服务器后指纹会变，价格记录随之失效。"""
    try:
        raw = Path("/etc/machine-id").read_text().strip()
    except OSError:
        raw = ""
    return hashlib.sha256((raw or platform.node()).encode()).hexdigest()


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        record = json.loads(path.read_text())
        if record["fingerprint"] == machine_fingerprint() and float(record["monthly_price_usd"]) > 0:
            return record
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _write_record(path: Path, monthly: float, source: dict[str, Any]) -> None:
    record = {"fingerprint": machine_fingerprint(), "monthly_price_usd": monthly, "input": source}
    path.write_text(json.dumps(record, indent=2) + "\n")


def _ask_price(
    ask: Callable[[str], str], fetch_rate: Callable[[str], tuple[float, str]]
) -> tuple[float, dict[str, Any]] | None:
    print("这台机器还没有价格记录，用于计算性价比（直接回车可跳过）。")
    while True:
        period = ask("计价方式 [m=月价 / y=年价]（默认 m）: ").strip().lower() or "m"
        if period in ("m", "y"):
            break
        print("请输入 m 或 y。")
    while True:
        try:
            currency = normalize_currency(ask("币种 USD / EUR / CNY(RMB) 等（默认 USD）: ") or "USD")
            break
        except ScoreError as error:
            print(error)
    while True:
        raw = ask(f"{'年' if period == 'y' else '月'}价金额（{currency}）: ").strip()
        if not raw:
            return None
        try:
            amount = parse_amount(raw)
            break
        except ScoreError as error:
            print(error)
    try:
        return to_monthly_usd(amount, currency, period, fetch_rate)
    except ScoreError as error:
        print(error)
        print("改为直接输入美元金额。")
    while True:
        raw = ask(f"{'年' if period == 'y' else '月'}价金额（USD）: ").strip()
        if not raw:
            return None
        try:
            return to_monthly_usd(parse_amount(raw), "USD", period, fetch_rate)
        except ScoreError as error:
            print(error)


def resolve_monthly_price(
    env: Mapping[str, str] = os.environ,
    record_path: Path = LOCAL_RECORD,
    *,
    ask: Callable[[str], str] | None = None,
    fetch_rate: Callable[[str], tuple[float, str]] = fetch_usd_rate,
) -> tuple[float | None, str]:
    """返回 (美元/月 或 None, 备注)。ask 为 None 表示非交互，不会询问。永不抛异常。"""
    monthly_raw, yearly_raw = env.get("BENCHMARK_MONTHLY_PRICE"), env.get("BENCHMARK_YEARLY_PRICE")
    if monthly_raw and yearly_raw:
        return None, "(BENCHMARK_MONTHLY_PRICE 与 BENCHMARK_YEARLY_PRICE 只能设置一个)"
    if monthly_raw or yearly_raw:
        try:
            currency = normalize_currency(env.get("BENCHMARK_PRICE_CURRENCY") or "USD")
            amount = parse_amount(monthly_raw or yearly_raw)
            return to_monthly_usd(amount, currency, "m" if monthly_raw else "y", fetch_rate)[0], ""
        except ScoreError as error:
            return None, f"({error})"

    record = _read_record(record_path)
    if record:
        return float(record["monthly_price_usd"]), ""
    if ask is None:
        return None, "(本机无价格记录；设置 BENCHMARK_MONTHLY_PRICE 或 BENCHMARK_YEARLY_PRICE 后重跑)"
    try:
        answer = _ask_price(ask, fetch_rate)
    except (EOFError, KeyboardInterrupt):
        print()
        return None, "(已跳过价格输入)"
    if answer is None:
        return None, "(已跳过价格输入)"
    monthly, source = answer
    try:
        _write_record(record_path, monthly, source)
    except OSError as error:
        print(f"无法保存价格记录（{error}），本次仍会使用。")
    return monthly, ""


def interactive_ask() -> Callable[[str], str] | None:
    return input if sys.stdin.isatty() and sys.stdout.isatty() else None


# ---------- 命令行 ----------

def main() -> None:
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
