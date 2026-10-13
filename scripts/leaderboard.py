#!/usr/bin/env python3
"""本地排行榜：读取 results/*.json，按多核分排名（100 = 参考机）。

  python scripts/leaderboard.py                 # 终端表格
  python scripts/leaderboard.py --html tmp/leaderboard.html
  python scripts/leaderboard.py --upload          # 经本机 wrangler 写入私人 Worker 的 KV

分数一律由原始 events/s 与 baselines/ 重新计算，不采信报告里已写入的 scores。
只有与参考机 benchmark_version / source_commit 一致的报告才入榜。
输出只含机器标签、CPU 型号、逻辑核数和分数；不含价格、hostname 以外的路径或指纹。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from score import ROOT, ScoreError, compute_scores, load_baseline

TEMPLATE = Path(__file__).with_name("leaderboard.html")
COLUMNS = ("排名", "机器标签", "CPU", "逻辑核", "单核", "多核", "时间(UTC)", "低置信")


def load_entries(results_dir: Path, version: int | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """返回 (按多核分降序的条目, 跳过说明)。version 省略时取目录中最新的 benchmark_version。"""
    reports = []
    for path in sorted(results_dir.glob("benchmark-*.json")):
        try:
            reports.append((path, json.loads(path.read_text())))
        except json.JSONDecodeError as error:
            reports.append((path, error))
    versions = [r["benchmark_version"] for _, r in reports if isinstance(r, dict) and "benchmark_version" in r]
    version = version if version is not None else max(versions, default=None)
    if version is None:
        return [], ["没有可读的报告"]
    baseline = load_baseline(version)
    entries, skipped = [], []
    for path, report in reports:
        try:
            if isinstance(report, Exception):
                raise ScoreError(f"无法解析: {report}")
            scores = compute_scores(report, baseline)
            host = report["host"]
        except (ScoreError, KeyError) as error:
            skipped.append(f"{path.name}: {error!r}" if isinstance(error, KeyError) else f"{path.name}: {error}")
            continue
        entries.append({
            "id": report["started_utc"],
            "version": report["benchmark_version"],
            "label": host["hostname"],
            "cpu": host["cpu_model"],
            "cpus": host["logical_cpus"],
            "single": scores["single"],
            "multi": scores["multi"],
            "when": report["started_utc"][:16].replace("T", " "),
            "flags": len(scores["flags"]),
        })
    entries.sort(key=lambda e: e["multi"], reverse=True)
    return entries, skipped


def rows(entries: list[dict[str, Any]]) -> list[tuple[str, ...]]:
    return [
        (str(i), e["label"], e["cpu"], str(e["cpus"]), f"{e['single']:.1f}", f"{e['multi']:.1f}", e["when"],
         f"{e['flags']} 项" if e["flags"] else "")
        for i, e in enumerate(entries, 1)
    ]


def render_text(entries: list[dict[str, Any]]) -> str:
    table = [COLUMNS, *rows(entries)]
    widths = [max(len(r[c]) for r in table) for c in range(len(COLUMNS))]
    return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip() for r in table)


def render_html(entries: list[dict[str, Any]]) -> str:
    data = json.dumps(entries, ensure_ascii=False).replace("<", "\\u003c")
    return TEMPLATE.read_text().replace("__ENTRIES__", data)


def upload(entries: list[dict[str, Any]]) -> None:
    """整份榜单写入 Worker 的 KV 键 runs（覆盖，幂等）；使用本机已登录的 wrangler。"""
    tmp = ROOT / "tmp"
    tmp.mkdir(exist_ok=True)
    payload = tmp / "runs.json"
    payload.write_text(json.dumps(entries, ensure_ascii=False))
    try:
        subprocess.run(
            ["npx", "wrangler", "kv", "key", "put", "runs", "--path", str(payload), "--binding", "RESULTS", "--remote"],
            cwd=ROOT / "worker", check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"上传失败: {error}")
    finally:
        payload.unlink(missing_ok=True)
    print(f"已上传 {len(entries)} 条")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, default=ROOT / "results")
    ap.add_argument("--version", type=int, help="benchmark_version，默认取最新")
    ap.add_argument("--html", type=Path, help="同时写出静态 HTML（建议放 tmp/，已被 Git 忽略）")
    ap.add_argument("--upload", action="store_true", help="把入榜条目（仅摘要）写入 Worker 的 KV")
    args = ap.parse_args()
    try:
        entries, skipped = load_entries(args.results, args.version)
    except ScoreError as error:
        sys.exit(str(error))
    print(render_text(entries) if entries else "没有可入榜的报告")
    if skipped:
        print(f"\n已跳过 {len(skipped)} 份（版本或 commit 与参考机不一致，或格式不符）", file=sys.stderr)
    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(render_html(entries))
        print(f"HTML 已写入 {args.html}")
    if args.upload:
        upload(entries)


if __name__ == "__main__":
    main()
