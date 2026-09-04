"""Run independent canonical backtests on all logical CPUs."""

from __future__ import annotations

import multiprocessing
import statistics
from concurrent.futures import ProcessPoolExecutor
from typing import Any

_FIXTURE: Any = None


def resource_summary(resources: dict[str, Any], *, include_rss: bool) -> dict[str, Any]:
    summary = {
        "measurement_wall_seconds": resources["measurement_wall_seconds"],
        "cpu_seconds": resources["process_cpu_seconds"],
        "cpu_percent_of_one_core": resources["process_cpu_percent_of_one_core"],
        "system_cpu_steal_percent": resources["system_cpu_steal_percent"],
        "memory_available_before_kib": resources["memory_available_before_kib"],
        "memory_available_after_kib": resources["memory_available_after_kib"],
    }
    if include_rss:
        summary["peak_process_rss_kib"] = resources["peak_process_rss_kib"]
    return summary


def _worker_init() -> None:
    global _FIXTURE
    import benchmark

    _FIXTURE = benchmark.load_fixture()


def _run_batch(task: tuple[str, str, int]) -> float:
    import benchmark

    scenario, scope, batch_size = task
    if _FIXTURE is None:
        raise RuntimeError("Worker fixture is not initialized")
    return sum(benchmark.execute_once(scenario, scope, _FIXTURE) for _ in range(batch_size))


def measure_parallel(
    *,
    worker_count: int,
    single_results: dict[str, Any],
    scenarios: tuple[str, ...],
    scopes: tuple[str, ...],
    sample_count: int,
    batch_size: int,
    data_events: int,
) -> dict[str, Any]:
    samples = {scenario: {scope: [] for scope in scopes} for scenario in scenarios}
    context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(
        max_workers=worker_count,
        mp_context=context,
        initializer=_worker_init,
    ) as executor:
        for round_index in range(sample_count):
            ordered = scenarios[round_index % len(scenarios) :] + scenarios[: round_index % len(scenarios)]
            ordered_scopes = scopes if round_index % 2 == 0 else tuple(reversed(scopes))
            for scenario in ordered:
                for scope in ordered_scopes:
                    task = (scenario, scope, batch_size)
                    worker_seconds = list(executor.map(_run_batch, [task] * worker_count))
                    samples[scenario][scope].append(
                        worker_count * batch_size * data_events / max(worker_seconds)
                    )

    results = {}
    for scenario in scenarios:
        results[scenario] = {}
        for scope in scopes:
            rates = samples[scenario][scope]
            throughput = statistics.median(rates)
            speedup = throughput / single_results[scenario][scope]["events_per_second"]
            results[scenario][scope] = {
                "workers": worker_count,
                "events_per_second": throughput,
                "speedup": speedup,
                "efficiency_percent": 100.0 * speedup / worker_count,
                "samples_events_per_second": rates,
            }
    return results


def print_summary(results: dict[str, Any], scenarios: tuple[str, ...], scopes: tuple[str, ...]) -> None:
    print(f"\n全核测试结果\n{'场景':28} {'范围':16} {'事件/秒':>18} {'相对单核':>10} {'并行效率':>11}")
    for scenario in scenarios:
        for scope in scopes:
            row = results[scenario][scope]
            print(
                f"{scenario:28} {scope:16} {row['events_per_second']:18,.0f} "
                f"{row['speedup']:8.2f}x {row['efficiency_percent']:10.1f}%"
            )


def print_report(
    single_results: dict[str, Any],
    all_core_results: dict[str, Any],
    single_resources: dict[str, Any],
    all_core_resources: dict[str, Any],
    scenarios: tuple[str, ...],
    scopes: tuple[str, ...],
) -> None:
    print(f"\n单核测试结果\n{'场景':28} {'范围':16} {'中位耗时 ms':>12} {'事件/秒':>12}")
    for scenario in scenarios:
        for scope in scopes:
            row = single_results[scenario][scope]
            print(f"{scenario:28} {scope:16} {row['median_ms']:12.3f} {row['events_per_second']:12,.0f}")
    print_summary(all_core_results, scenarios, scopes)
    print("\n单核阶段资源")
    print(f"CPU 使用率：{single_resources['cpu_percent_of_one_core']:.1f}%（单核为 100%）")
    print(f"主进程峰值 RSS：{single_resources['peak_process_rss_kib'] / 1024:.1f} MiB")
    print(f"系统 CPU steal：{single_resources['system_cpu_steal_percent']:.3f}%")
    memory_delta_mib = (
        all_core_resources["memory_available_after_kib"]
        - all_core_resources["memory_available_before_kib"]
    ) / 1024
    print("\n全核阶段资源")
    print(f"CPU 使用率：{all_core_resources['cpu_percent_of_one_core']:.1f}%（单核为 100%）")
    print(f"系统 CPU steal：{all_core_resources['system_cpu_steal_percent']:.3f}%")
    print(f"系统可用内存变化：{memory_delta_mib:+.1f} MiB")
