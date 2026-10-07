# NautilusTrader canonical benchmark environment

## 当前阶段

第一阶段环境准备已完成；第二阶段已于 2026-08-13 首次执行统一 Python benchmark。

本项目用于在多台服务器上，以完全一致的 NautilusTrader 官方 canonical workload 比较 CPU / 服务器的单回测性能和整机并行回测吞吐。

## 固定环境

| 项目 | 固定值 |
| --- | --- |
| Python | `3.12.13` |
| NautilusTrader | `2.0.0rc3.dev20260811` |
| 分支类型 | 官方 nightly（不是同日的 develop `+run` 构建） |
| 官方 nightly commit | `34014ab94b96d3b227ec793e76999e259ca32e52` |
| 安装来源 | `https://packages.nautechsystems.io/simple` |
| Linux x86_64 wheel | `nautilus_trader-2.0.0rc3.dev20260811-cp312-cp312-manylinux_2_34_x86_64.whl` |
| Wheel SHA256（官方 index） | `95c9a12943a4fdb76d54f1d73e48b570372d60172cc5f5c6b0e6306dbb50428d` |
| Wheel 策略 | `--only-binary=:all:`；不允许源码构建 |
| Conda environment | `nautilus-benchmark` |
| Conda prefix | 由本机 Conda 管理，不依赖固定绝对路径 |

官方 nightly 文档支持 Python 3.12–3.14；这里选择 Python 3.12 作为各服务器的统一版本。使用 Miniforge 具名环境，不使用 base 或系统 Python：

```bash
conda activate nautilus-benchmark
```

## 固定数据

| 项目 | 固定值 |
| --- | --- |
| 本地文件 | `data/btc-perp-20211231-20220201_1m.csv` |
| 官方仓库位置 | `test_data/btc-perp-20211231-20220201_1m.csv` |
| 来源 commit | `34014ab94b96d3b227ec793e76999e259ca32e52` |
| 文件大小 | `3,220,342 bytes` |
| 文件行数 | `45,032`（含 1 行 header） |
| SHA256 | `65ab55cd5f3531aa64f429772a38331fe82a92973cbb32a79b243073236c8da3` |

CSV 是官方仓库原始文件，未修改。canonical workload 只读取 header 后的前 `10,000` 行。

## 官方 canonical benchmark 定义

固定 nightly commit 中的源码位置：

- 注册入口：`crates/backtest/benches/engine.rs`
- workload 定义：`crates/backtest/benches/engine/canonical.rs`
- 官方说明与基线：`crates/backtest/benches/BENCHMARKS.md`
- 本地官方说明副本：`references/official/crates/backtest/benches/BENCHMARKS.md`
  - 来源 commit：`34014ab94b96d3b227ec793e76999e259ca32e52`
  - SHA256：`173ce39b52bc76e9cdb28099bfd8c0a39e012cd8cffae158f00c255793f71f12`

当前官方定义（2026-08-10 引入的 canonical 四场景矩阵）与旧的单一案例相比，明确统一为每场景：

- 输入 CSV 前 `10,000` 行；每行生成一个零 spread `QuoteTick`（bid/ask 均为 close）和一个 1-minute `Bar`。
- Quote 数量 `10,000`，Bar 数量 `10,000`，data events 总数 `20,000`。
- Instrument：`BTCUSDT-PERP.BINANCE`，raw symbol `BTCUSDT`，base currency `BTC`。
- Venue：`BINANCE`；Netting OMS、Margin account、L1 MBP book、初始余额 `1,000,000 USDT`、queue position enabled。
- Bar type：`BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL`。
- bypass logging、analysis disabled、一个 simulated venue；四场景使用完全相同的数据流。

| Workload | 定义 | 预期结果 |
| --- | --- | --- |
| Replay only | 只重放 20,000 data events，不挂策略 | 0 orders / 0 fills / 0 positions；0 execution events |
| Scheduled market orders | 在 64 个均匀分布的时间点交替提交 Buy/Sell market order，每笔 `0.010` | 64 submitted / 64 fills / 32 positions；128 execution events |
| Passive limit orders | 同样 64 个时间点交替 Buy/Sell，每笔 `0.010`；Buy price `30,000.00`，Sell price `70,000.00`；停止时全部撤销 | 64 submitted / 0 fills / 64 canceled；192 execution events |
| Bar EMA cross | 对 bar close 计算 EMA fast=`10`、slow=`20`；状态发生上穿时 Buy、下穿时 Sell market order，每笔 `0.010` | 450 submitted / 450 fills / 225 positions；900 execution events |

所有场景的固定事件数、订单结果和参数都应在第二阶段的 Python 实现中保持一致。官方 Rust benchmark 的 `run_preloaded` 与 `load_build_run` 是两种计时边界；第二阶段需先明确 Python benchmark 采用哪一个边界，再跨服务器统一执行。

## 环境锁定文件

- `requirements/pip-freeze.txt`：当前环境的原始 `pip freeze` 输出。
- `requirements/conda-explicit.txt`：Linux x86_64 的完整 Conda 显式包列表。
- `requirements/environment.yml`：便于在其他服务器创建同版本环境，并从官方 index 安装固定 wheel。

同架构 Linux 服务器优先使用 `conda-explicit.txt` 重建 Conda 层，再安装固定 NautilusTrader wheel；跨架构服务器使用 `environment.yml`，但必须保持 Python、NautilusTrader、数据和 benchmark 参数一致。

## 新服务器：一键准备（不执行 benchmark）

将整个 `nautilus-benchmark/` 目录复制到新服务器的 workspace。服务器需要为 Linux x86_64、glibc 2.34+；无需预先安装 Miniforge/Conda。从项目根目录执行：

```bash
bash scripts/setup.sh
```

setup 优先复用 PATH 或常见安装目录中的 Conda（包括 `~/miniforge3`、`~/miniconda3`）。未检测到时，自动从官方 GitHub release 下载固定的 Miniforge `26.7.2-0`，核验脚本内锁定的 SHA256，再无交互安装到 `~/miniforge3`。下载需要 `curl` 或 `wget`，并能访问 GitHub、conda-forge 和 NautilusTrader 官方 index。安装包暂存在项目 `tmp/`，成功或失败后自动清理；不会覆盖已有但不可用的安装目录。脚本不修改 shell 配置，结束时会打印当前终端所需的 `source .../etc/profile.d/conda.sh` 与 `conda activate` 命令。

`data/`、`logs/`、空的 `results/` 目录和固定官方 CSV 都是项目基准内容，必须随项目完整复制。历史 benchmark JSON 不纳入仓库；新结果仅保存在本地 `results/`。setup 检查 Linux x86_64 与 glibc 2.34+，准备 Conda，创建或核验具名环境 `nautilus-benchmark`，然后通过 `pip --only-binary` 从官方 index 安装锁定的 NautilusTrader wheel，并校验 Python、NautilusTrader 与 CSV SHA256。项目依赖只安装在具名环境中。缺少项目文件或数据时直接停止；它不补建基准目录、不下载数据，也**不会**调用 `benchmark.py` 或执行任何 backtest。

若目标服务器已存在同名环境，脚本只校验其版本；不匹配时会停止，避免覆盖未知环境。确认可以替换后再手动执行 `conda env remove -n nautilus-benchmark` 并重新运行 setup。

自动安装流程的离线回归检查：`python3 -m unittest discover -s tests -v`，覆盖首次安装、已有环境复用、下载或安装失败、校验失败和安装目录保护。

## 运行 benchmark

```bash
conda activate nautilus-benchmark
export BENCHMARK_HOST_LABEL=example-4c8g
python scripts/benchmark.py
```

`BENCHMARK_HOST_LABEL` 是写入公开报告和文件名的机器标签，只允许小写字母、数字和连字符；不要填写真实 hostname 或云服务器实例编号。未设置时使用 `anonymous-host`。报告中的数据路径固定写为项目相对路径，不记录用户目录。

脚本依次显示中文阶段：结果校验、预热、单核测试、全核测试、生成报告。终端会显示两组性能和各自资源统计，完整报告写入同一个 `results/benchmark-<host-label>-<UTC timestamp>.json`。

## 目录

```text
nautilus-benchmark/
├── data/         # 官方固定 CSV
├── scripts/      # 一键 setup、benchmark 与资源记录入口
├── results/      # 第二阶段本地输出（JSON 不纳入 Git）
├── logs/         # 当前服务器基本信息
├── requirements/ # 环境锁定文件
└── README.md
```

## 第二阶段 benchmark

第二阶段运行 `python scripts/benchmark.py`。脚本已固定数据校验、结果正确性检查、warm-up、9 个 samples、每 sample 5 次 backtest、交错 workload 顺序、median、events/sec、CPU steal 记录和 JSON 输出。它同时报告仅计 `engine.run()` 的 `run_only`，以及包含 CSV 加载、对象构造和 engine 搭建的 `load_build_run`。

JSON 中的 `single_core_results` 是单核性能，`all_core_results` 是全核性能，资源分别记录在 `single_core_resources` 和 `all_core_resources`。两阶段的 `cpu_percent_of_one_core` 使用相同口径：一个逻辑核心满载为 100%，所以单核测试应接近 100%，4 核全核测试应接近 400%。CPU 时间只在每个阶段开始和结束时采集一次。

全核结果中的 `speedup = 全核 events/s ÷ 单核 events/s`；`efficiency_percent = speedup ÷ 全核进程数 × 100%`。例如 4 个进程达到 3.4 倍加速，并行效率就是 `3.4 ÷ 4 × 100% = 85%`。

历史 benchmark 已执行；结果 JSON 不随公开仓库发布。

## License 与第三方内容

本项目采用 LGPL-3.0。固定数据集和官方 benchmark 文档来自 NautilusTrader；来源、固定 commit、校验值和许可说明见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
