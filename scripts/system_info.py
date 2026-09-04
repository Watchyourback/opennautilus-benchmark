"""Low-overhead Linux host and process resource snapshots."""

from __future__ import annotations

import os
import platform
import re
import resource
import time
from pathlib import Path
from typing import Any


CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def public_host_label() -> str:
    label = os.environ.get("BENCHMARK_HOST_LABEL", "anonymous-host")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", label):
        raise RuntimeError(
            "BENCHMARK_HOST_LABEL must use 1-63 lowercase letters, digits, or hyphens"
        )
    return label


def _cpu_times() -> dict[str, int]:
    values = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    return dict(zip(CPU_FIELDS, values, strict=False))


def _memory() -> dict[str, int]:
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.strip().split()[0])
    return values


def host_info(host_label: str, python_version: str, nautilus_version: str) -> dict[str, Any]:
    cpuinfo = Path("/proc/cpuinfo").read_text(errors="replace")
    cpu_model = next(
        (line.split(":", 1)[1].strip() for line in cpuinfo.splitlines() if line.startswith("model name")),
        "unknown",
    )
    memory = _memory()
    return {
        "hostname": host_label,
        "platform": platform.platform(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "cpu_model": cpu_model,
        "logical_cpus": os.cpu_count(),
        "virtualized": "hypervisor" in cpuinfo.lower(),
        "load_average": list(os.getloadavg()),
        "memory_total_kib": memory["MemTotal"],
        "python": python_version,
        "nautilus_trader": nautilus_version,
    }


def resource_snapshot(*, children: bool = False) -> dict[str, Any]:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN if children else resource.RUSAGE_SELF)
    return {
        "wall_ns": time.perf_counter_ns(),
        "process_cpu_seconds": usage.ru_utime + usage.ru_stime,
        "peak_process_rss_kib": usage.ru_maxrss,
        "memory_available_kib": _memory()["MemAvailable"],
        "cpu_times": _cpu_times(),
    }


def resource_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    wall_seconds = (after["wall_ns"] - before["wall_ns"]) / 1_000_000_000
    process_cpu_seconds = after["process_cpu_seconds"] - before["process_cpu_seconds"]
    cpu_delta = {
        name: after["cpu_times"][name] - before["cpu_times"][name] for name in CPU_FIELDS
    }
    total = sum(cpu_delta.values())
    percentages = {
        f"system_cpu_{name}_percent": 100.0 * value / total if total else 0.0
        for name, value in cpu_delta.items()
    }
    percentages["system_cpu_busy_percent"] = 100.0 * (
        total - cpu_delta["idle"] - cpu_delta["iowait"]
    ) / total if total else 0.0
    return {
        "measurement_wall_seconds": wall_seconds,
        "process_cpu_seconds": process_cpu_seconds,
        "process_cpu_percent_of_one_core": 100.0 * process_cpu_seconds / wall_seconds,
        "peak_process_rss_kib": after["peak_process_rss_kib"],
        "memory_available_before_kib": before["memory_available_kib"],
        "memory_available_after_kib": after["memory_available_kib"],
        **percentages,
    }
