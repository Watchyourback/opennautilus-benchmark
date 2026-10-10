# 官方 Threadripper 9980X benchmark 存档

检索与存档日期：2026-10-07。CPU 为 AMD Ryzen Threadripper **9980X，64 cores / 128 threads**。

官方记录分为原生 Rust canonical 四场景测试与 Python v1/v2 的 16 场景对比。以下表格摘录官方结果；本机对照列来自本地 rc6 报告，不属于官方数据。

## 来源与完整性

归档使用官方 rc6 commit `7b766f8825b2539c5b2ac1375e9d97b41c509edb` 的原始文件，未修改内容。另核查检索时 develop HEAD `a109026ca6782f7f8cd48fec3f4b630e217261d1`：其中 `BENCHMARKS.md` 与 `v1-v2-results.json` 的 SHA256 和本存档一致。因此这些历史测试记录仍是检索时官方文档提供的数据，不是 2026-10-07 重新测量的结果。

| 本地原始文件 | 官方不可变来源 | SHA256 |
| --- | --- | --- |
| [BENCHMARKS.md](crates/backtest/benches/BENCHMARKS.md) | [官方四场景、优化记录及 Python 版本对比](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/crates/backtest/benches/BENCHMARKS.md) | `08d164bf11620c07a69f6c3f172924161f120ed45d2562a5c154da0f90588344` |
| [v1-v2-results.json](crates/backtest/benches/v1-v2-results.json) | [官方 Python 版本对比原始结果](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/crates/backtest/benches/v1-v2-results.json) | `4ff2ae000c93333f70d0769f90ce0323710dd7ac7b389e7f21e28cdd183fac45` |
| [BENCHMARKING.md](BENCHMARKING.md) | [官方 benchmark 方法与降噪说明](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/BENCHMARKING.md) | `350657d5822b268d6cbbfbe0aa717a21e13b942ede3a8d302b184d9fa93c7177` |

## 原生 Rust canonical：2026-09-04

来源是官方文档的 `2026-09-04: Typed batch input`。这是文档中较新的完整四场景计时表；另有 2026-08-10 初始基线和若干只测部分场景的优化记录，不应混合为同一次测试。

这里选择 candidate executable 的 **legacy** 输入，最接近本项目交错 QuoteTick / Bar 的输入方式。它不是 rc6 release wheel 测试；源码身份为 baseline revision `ec1894d6fab4fb3768caadbcd6ab6956e0568f13` 加 candidate patch，measured source tree `1635c1b51a7dac1ae93810bce550273bb8b938fd`。

| 场景 | 官方仅 `run()` 中位耗时 | 官方加载、构建及运行中位耗时 |
| --- | ---: | ---: |
| Replay only | 14.631 ms | 21.166 ms |
| Scheduled market orders | 16.111 ms | 22.893 ms |
| Passive limit orders | 22.127 ms | 28.470 ms |
| Bar EMA cross | 22.040 ms | 29.282 ms |

每场景为同一 CSV 的前 10,000 行，生成 10,000 QuoteTicks 和 10,000 Bars，共 20,000 data events。订单结果为 0 / 64 / 64 / 450 submitted，market / EMA 分别 64 / 450 fills，passive 为 64 cancels。

官方文档的原生基线环境与该小节的测量方法：

- 原生基线主机为 Ubuntu 24.04.4 LTS、Linux `7.0.0-28-generic`，governor 为 `powersave`；线程未绑核，SMT / boost enabled，进程禁用 ASLR。
- 初始基线使用 Rust / Cargo `1.97.1`、LLVM `22.1.6`；8 月优化记录使用 Rust / Cargo `1.98.0`、LLVM `22.1.8`。两者均为标准精度、`bench-lto`（fat LTO、one codegen unit）。9 月 4 日小节没有单独重列 governor 与完整 toolchain，不应将这些环境描述当作该次运行的独立原始 host record。
- 9 月 4 日小节明确记录：测试期间没有 Cargo 工作。
- 每 case 预热 3 秒、测量目标 5 秒、50 samples；共 3 rounds，取三轮中位数的中位数。
- Candidate executable SHA256：`b1f2e5dc7cea1b562c599ac3e6abbc8f1c3cb77b585fe00a80761e50848291a1`。

### 本机 rc6 参考对照

本机报告：[benchmark-anonymous-host-20261007T141312Z.json](../../results/benchmark-anonymous-host-20261007T141312Z.json)，9950X、2 logical CPUs、Python `3.12.13`、NautilusTrader `2.0.0rc6`、benchmark version `3`。

| 场景 | 本机单核仅运行 | 相对上述官方耗时 | 本机加载、构建及运行 |
| --- | ---: | ---: | ---: |
| Replay only | 15.701 ms | +7.3% | 54.404 ms |
| Scheduled market orders | 19.818 ms | +23.0% | 58.414 ms |
| Passive limit orders | 28.360 ms | +28.2% | 68.450 ms |
| Bar EMA cross | 33.171 ms | +50.5% | 72.222 ms |

上述差值只描述两份记录的耗时差，不能作为 CPU 排名：官方为 Rust 原生标准精度的 LTO executable，本机为 Python / PyO3 release wheel，源码版本、构建、采样方式及主机控制也不同。全流程还包含不同语言的数据加载与对象构建开销。官方表不是 128 线程整机吞吐，不能用它推算全核加速比或并行效率。本次检索未找到与本项目完全同口径的 rc6 9980X 四场景及全核结果。

## Python v1 / v2：2026-08-27

[原始 JSON](crates/backtest/benches/v1-v2-results.json) 的记录范围为 `2026-08-27T22:56:07.081215+00:00` 至 `2026-08-27T23:01:30.730694+00:00`。它包含 16 场景 × 2 计时边界 × 2 版本 × 5 sessions，共 **320 条计时记录**。这 16 场景与本项目 canonical 四场景矩阵不同。

| 项目 | v1 | v2 |
| --- | --- | --- |
| 包版本 | `1.231.0` | `2.0.0rc4` |
| Backend | Cython | Rust / PyO3 |
| Source commit | `27a8e54e7ac3c57d6cbf8891f0283dfbaee97317` | `908c571caec0af086c1d1a8edbcf7bcbb07d6621` |
| Python | `3.12.3` | `3.12.3` |
| 精度 | High precision | High precision |

两者都在同一台 9980X 上运行，governor 为 `performance`，ASLR disabled。每 case 每 session 预热一次、计时一次，交错版本顺序，取五次 session 的中位数。`run_preloaded` 只计 engine.run，`load_build_run` 包括 fixture generation、engine construction、data load 和 run。

官方结论：v2 在 **30 / 32** 个场景与边界组合中更快。4,000-fill accumulating-position 场景在两个边界下更慢；其余不能概括为所有 workload 都提速。8 个组合的 spread 超过 25%，其中一个达到 272.9%，接近的性能差需要查看原始样本。

选取官方记录中的几个代表结果如下，单位 ms：

| 场景 | v1 仅运行 | v2 仅运行 | v1 全流程 | v2 全流程 |
| --- | ---: | ---: | ---: | ---: |
| quote_trade_replay_medium | 10.863 | 2.443 | 26.984 | 12.023 |
| alternating_market_medium | 42.676 | 3.605 | 49.978 | 9.595 |
| passive_cancel | 18.658 | 4.898 | 35.270 | 15.667 |
| accumulating_market_large | 363.737 | 596.268 | 393.466 | 596.190 |

JSON 保存完整 samples、fingerprints、identities、summaries 与测量前后 host state，便于重新分析。原文的 driver SHA256 为实际测量时的身份记录，不应拿当前上游脚本替代后声称完全复现了原始实验。
