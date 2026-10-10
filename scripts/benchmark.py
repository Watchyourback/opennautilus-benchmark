#!/usr/bin/env python3
"""Cross-server NautilusTrader canonical Python benchmark (executes backtests)."""

from __future__ import annotations

import csv
import hashlib
import os
import platform
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import nautilus_trader
from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import LoggerConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model import (
    AccountType,
    AggregationSource,
    Bar,
    BarAggregation,
    BarSpecification,
    BarType,
    BookType,
    CryptoPerpetual,
    Currency,
    InstrumentId,
    Money,
    OmsType,
    OrderSide,
    OrderStatus,
    Price,
    PriceType,
    Quantity,
    QuoteTick,
    StrategyId,
    Symbol,
    Venue,
)
from nautilus_trader.trading import Strategy, StrategyConfig

from price import interactive_ask, resolve_monthly_price
from score import dumps_report, score_report
from system_info import host_info, public_host_label, resource_delta, resource_snapshot

PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_PATH = PROJECT_DIR / "data" / "btc-perp-20211231-20220201_1m.csv"
RESULTS_DIR = PROJECT_DIR / "results"

EXPECTED_NAUTILUS_VERSION = "2.0.0rc6"
EXPECTED_DATA_SHA256 = "65ab55cd5f3531aa64f429772a38331fe82a92973cbb32a79b243073236c8da3"
SOURCE_COMMIT = "7b766f8825b2539c5b2ac1375e9d97b41c509edb"
DATA_ROWS = 10_000
DATA_EVENTS = DATA_ROWS * 2
SCHEDULED_ACTIONS = 64
TRADE_SIZE = "0.010"
EMA_FAST_PERIOD = 10
EMA_SLOW_PERIOD = 20
# Cross-server measurement contract. Change only by creating a new benchmark version.
BENCHMARK_VERSION = 3
WARMUP_SECONDS_PER_SCENARIO = 2.0
SAMPLES = 9
BATCH_SIZE = 5
SCENARIOS = ("replay_only", "scheduled_market_orders", "passive_limit_orders", "bar_ema_cross")
SCOPES = ("run_only", "load_build_run")
EXPECTED = {
    "replay_only": dict(iterations=20_000, execution_events=0, orders=0, filled=0, canceled=0, positions=0),
    "scheduled_market_orders": dict(iterations=20_000, execution_events=128, orders=64, filled=64, canceled=0, positions=32),
    "passive_limit_orders": dict(iterations=20_000, execution_events=192, orders=64, filled=0, canceled=64, positions=0),
    "bar_ema_cross": dict(iterations=20_000, execution_events=900, orders=450, filled=450, canceled=0, positions=225),
}
@dataclass(frozen=True)
class Fixture:
    instrument: CryptoPerpetual
    bar_type: BarType
    data: list[Any]
    timestamps: list[int]
def canonical_instrument() -> CryptoPerpetual:
    """Mirror the Rust canonical ETHUSDT stub with its documented BTC mutations."""
    usdt = Currency.from_str("USDT")
    return CryptoPerpetual(
        instrument_id=InstrumentId(Symbol("BTCUSDT-PERP"), Venue("BINANCE")),
        raw_symbol=Symbol("BTCUSDT"),
        base_currency=Currency.from_str("BTC"),
        quote_currency=usdt,
        settlement_currency=usdt,
        is_inverse=False,
        price_precision=2,
        size_precision=3,
        price_increment=Price.from_str("0.01"),
        size_increment=Quantity.from_str("0.001"),
        ts_event=0,
        ts_init=0,
        max_quantity=Quantity.from_str("10000.000"),
        min_quantity=Quantity.from_str("0.001"),
        min_notional=Money.from_str("10.00 USDT"),
        max_price=Price.from_str("1000000.00"),
        min_price=Price.from_str("1.00"),
        margin_init=Decimal("1.0"),
        margin_maint=Decimal("0.35"),
    )
def timestamp_ns(value: str) -> int:
    parsed = datetime.fromisoformat(value).replace(tzinfo=UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = parsed - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000
def load_fixture() -> Fixture:
    instrument = canonical_instrument()
    bar_type = BarType(
        instrument.id,
        BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    data: list[Any] = []
    timestamps: list[int] = []
    with DATA_PATH.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["timestamp", "open", "high", "low", "close", "volume"]:
            raise RuntimeError(f"Unexpected CSV header: {reader.fieldnames}")
        for index, row in enumerate(reader):
            if index == DATA_ROWS:
                break
            ts = timestamp_ns(row["timestamp"])
            close = Price.from_raw(Price.from_str(row["close"]).raw, instrument.price_precision)
            volume = Quantity.from_decimal_dp(Decimal(row["volume"]), instrument.size_precision)
            quote = QuoteTick(instrument.id, close, close, volume, volume, ts, ts)
            bar = Bar(
                bar_type,
                Price.from_raw(Price.from_str(row["open"]).raw, instrument.price_precision),
                Price.from_raw(Price.from_str(row["high"]).raw, instrument.price_precision),
                Price.from_raw(Price.from_str(row["low"]).raw, instrument.price_precision),
                close,
                volume,
                ts,
                ts,
            )
            data.extend((quote, bar))
            timestamps.append(ts)
    if len(timestamps) != DATA_ROWS:
        raise RuntimeError(f"Expected {DATA_ROWS} data rows, found {len(timestamps)}")
    return Fixture(instrument, bar_type, data, timestamps)
class ScheduledOrders(Strategy):
    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

    def configure(self, fixture: Fixture, passive: bool) -> None:
        stride = len(fixture.timestamps) // (SCHEDULED_ACTIONS + 1)
        self.instrument_id = fixture.instrument.id
        self.times = [fixture.timestamps[index * stride] for index in range(1, SCHEDULED_ACTIONS + 1)]
        self.passive = passive
        self.submitted = 0

    def on_start(self) -> None:
        for index, alert_time in enumerate(self.times):
            self.clock.set_time_alert_ns(f"canonical-order-{index}", alert_time)

    def on_time_event(self, _event: Any) -> None:
        side = OrderSide.BUY if self.submitted % 2 == 0 else OrderSide.SELL
        quantity = Quantity.from_str(TRADE_SIZE)
        if self.passive:
            price = Price.from_str("30000.00" if side == OrderSide.BUY else "70000.00")
            order = self.order_factory.limit(self.instrument_id, side, quantity, price)
        else:
            order = self.order_factory.market(self.instrument_id, side, quantity)
        self.submitted += 1
        self.submit_order(order)

    def on_stop(self) -> None:
        if self.passive:
            self.cancel_all_orders(self.instrument_id)
class BarEmaCross(Strategy):
    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

    def configure(self, fixture: Fixture) -> None:
        self.bar_type = fixture.bar_type
        self.instrument_id = fixture.instrument.id
        self.fast = ExponentialMovingAverage(EMA_FAST_PERIOD, PriceType.LAST)
        self.slow = ExponentialMovingAverage(EMA_SLOW_PERIOD, PriceType.LAST)
        self.previous_fast_above: bool | None = None

    def on_start(self) -> None:
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar) -> None:
        self.fast.handle_bar(bar)
        self.slow.handle_bar(bar)
        if not self.fast.initialized or not self.slow.initialized:
            return
        fast_above = self.fast.value > self.slow.value
        if self.previous_fast_above is not None and fast_above != self.previous_fast_above:
            side = OrderSide.BUY if fast_above else OrderSide.SELL
            order = self.order_factory.market(self.instrument_id, side, Quantity.from_str(TRADE_SIZE))
            self.submit_order(order)
        self.previous_fast_above = fast_above
def build_engine(scenario: str, fixture: Fixture) -> BacktestEngine:
    engine = BacktestEngine(
        BacktestEngineConfig(
            logging=LoggerConfig.from_spec("bypass_logging"),
            bypass_logging=True,
            run_analysis=False,
        )
    )
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money.from_str("1000000 USDT")],
        book_type=BookType.L1_MBP,
        queue_position=True,
        fee_model=MakerTakerFeeModel(Decimal("0.0002"), Decimal("0.0004")),
    )
    engine.add_instrument(fixture.instrument)
    if scenario == "scheduled_market_orders":
        strategy = ScheduledOrders(
            StrategyConfig(strategy_id=StrategyId("CANONICAL-MARKET-001"), order_id_tag="001")
        )
        strategy.configure(fixture, passive=False)
        engine.add_strategy(strategy)
    elif scenario == "passive_limit_orders":
        strategy = ScheduledOrders(
            StrategyConfig(strategy_id=StrategyId("CANONICAL-PASSIVE-001"), order_id_tag="001")
        )
        strategy.configure(fixture, passive=True)
        engine.add_strategy(strategy)
    elif scenario == "bar_ema_cross":
        strategy = BarEmaCross(
            StrategyConfig(strategy_id=StrategyId("CANONICAL-BAR-EMA-001"), order_id_tag="001")
        )
        strategy.configure(fixture)
        engine.add_strategy(strategy)
    elif scenario != "replay_only":
        raise ValueError(f"Unknown scenario: {scenario}")
    engine.add_data(fixture.data, validate=True, sort=True)
    return engine
def verify_result(engine: BacktestEngine, scenario: str) -> dict[str, int]:
    result = engine.get_result()
    orders = engine.cache.orders()
    actual = {
        "iterations": result.iterations,
        "execution_events": result.total_events,
        "orders": result.total_orders,
        "filled": sum(order.status == OrderStatus.FILLED for order in orders),
        "canceled": sum(order.status == OrderStatus.CANCELED for order in orders),
        "positions": result.total_positions,
    }
    if actual != EXPECTED[scenario]:
        raise RuntimeError(f"{scenario} result mismatch: expected {EXPECTED[scenario]}, got {actual}")
    return actual
def execute_once(scenario: str, scope: str, preloaded: Fixture) -> float:
    engine: BacktestEngine | None = None
    try:
        if scope == "run_only":
            engine = build_engine(scenario, preloaded)
            started = time.perf_counter_ns()
            engine.run()
            elapsed = time.perf_counter_ns() - started
        elif scope == "load_build_run":
            started = time.perf_counter_ns()
            fixture = load_fixture()
            engine = build_engine(scenario, fixture)
            engine.run()
            elapsed = time.perf_counter_ns() - started
        else:
            raise ValueError(f"Unknown timing scope: {scope}")
        verify_result(engine, scenario)
        return elapsed / 1_000_000_000
    finally:
        if engine is not None:
            engine.dispose()


def preflight() -> Fixture:
    if nautilus_trader.__version__ != EXPECTED_NAUTILUS_VERSION:
        raise RuntimeError(
            f"Expected NautilusTrader {EXPECTED_NAUTILUS_VERSION}, found {nautilus_trader.__version__}"
        )
    digest = hashlib.sha256(DATA_PATH.read_bytes()).hexdigest()
    if digest != EXPECTED_DATA_SHA256:
        raise RuntimeError(f"Dataset SHA256 mismatch: expected {EXPECTED_DATA_SHA256}, found {digest}")
    fixture = load_fixture()
    for scenario in SCENARIOS:
        engine = build_engine(scenario, fixture)
        try:
            engine.run()
            verify_result(engine, scenario)
        finally:
            engine.dispose()
    return fixture
def warm_up(fixture: Fixture) -> None:
    for scenario in SCENARIOS:
        deadline = time.monotonic() + WARMUP_SECONDS_PER_SCENARIO
        index = 0
        while time.monotonic() < deadline:
            execute_once(scenario, SCOPES[index % len(SCOPES)], fixture)
            index += 1
def summarize(samples: dict[str, dict[str, list[float]]]) -> dict[str, Any]:
    summary = {}
    for scenario in SCENARIOS:
        summary[scenario] = {}
        for scope in SCOPES:
            values = samples[scenario][scope]
            median = statistics.median(values)
            summary[scenario][scope] = {
                "median_ms": median * 1_000,
                "min_ms": min(values) * 1_000,
                "max_ms": max(values) * 1_000,
                "events_per_second": DATA_EVENTS / median,
                "samples_ms": [value * 1_000 for value in values],
            }
    return summary


def main() -> None:
    host_label = public_host_label()
    # 价格询问放在测量之前，之后可无人值守；价格只存本机，不写入公开报告。
    monthly_price, price_note = resolve_monthly_price(ask=interactive_ask())
    print("[阶段 1/5] 正在校验环境、数据和回测结果...")
    fixture = preflight()
    print("[阶段 2/5] 正在预热四个回测场景...")
    warm_up(fixture)

    print("[阶段 3/5] 正在运行单核性能测试...")
    samples = {scenario: {scope: [] for scope in SCOPES} for scenario in SCENARIOS}
    single_resources_before = resource_snapshot()
    started_utc = datetime.now(UTC)
    for round_index in range(SAMPLES):
        ordered = SCENARIOS[round_index % len(SCENARIOS) :] + SCENARIOS[: round_index % len(SCENARIOS)]
        scopes = SCOPES if round_index % 2 == 0 else tuple(reversed(SCOPES))
        for scenario in ordered:
            for scope in scopes:
                batch = [execute_once(scenario, scope, fixture) for _ in range(BATCH_SIZE)]
                samples[scenario][scope].append(statistics.mean(batch))
    single_core_resource_delta = resource_delta(single_resources_before, resource_snapshot())

    single_results = summarize(samples)
    worker_count = os.cpu_count() or 1
    print(f"[阶段 4/5] 正在运行全核性能测试（{worker_count} 个进程）...")
    from parallel_benchmark import measure_parallel, print_report, resource_summary
    single_core_resources = resource_summary(single_core_resource_delta, include_rss=True)
    all_core_resources_before = resource_snapshot(children=True)
    all_core_results = measure_parallel(
        worker_count=worker_count,
        single_results=single_results,
        scenarios=SCENARIOS,
        scopes=SCOPES,
        sample_count=SAMPLES,
        batch_size=BATCH_SIZE,
        data_events=DATA_EVENTS,
    )
    all_core_resources = resource_summary(
        resource_delta(all_core_resources_before, resource_snapshot(children=True)),
        include_rss=False,
    )
    finished_utc = datetime.now(UTC)
    print("[阶段 5/5] 正在计算分数并生成 JSON 报告...")
    output = {
        "benchmark_version": BENCHMARK_VERSION,
        "started_utc": started_utc.isoformat(),
        "finished_utc": finished_utc.isoformat(),
        "duration_seconds": (finished_utc - started_utc).total_seconds(),
        "source_commit": SOURCE_COMMIT,
        "dataset": {"path": DATA_PATH.relative_to(PROJECT_DIR).as_posix(), "sha256": EXPECTED_DATA_SHA256, "rows": DATA_ROWS},
        "contract": {
            "data_events": DATA_EVENTS,
            "warmup_seconds_per_scenario": WARMUP_SECONDS_PER_SCENARIO,
            "samples": SAMPLES,
            "batch_size": BATCH_SIZE,
            "single_core_workers": 1,
            "all_core_workers": worker_count,
            "expected_results": EXPECTED,
        },
        "host": host_info(host_label, platform.python_version(), nautilus_trader.__version__),
        "single_core_resources": single_core_resources,
        "all_core_resources": all_core_resources,
        "single_core_results": single_results,
        "all_core_results": all_core_results,
    }
    scores, score_text = score_report(output, monthly_price, price_note)
    if scores is not None:
        output["scores"] = scores
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"benchmark-{host_label}-{started_utc.strftime('%Y%m%dT%H%M%SZ')}.json"
    output_path = RESULTS_DIR / filename
    output_path.write_text(dumps_report(output), encoding="utf-8")

    print_report(
        single_results,
        all_core_results,
        single_core_resources,
        all_core_resources,
        SCENARIOS,
        SCOPES,
    )
    print(f"\n{score_text}")
    print(f"\n报告：{output_path}")


if __name__ == "__main__":
    main()
