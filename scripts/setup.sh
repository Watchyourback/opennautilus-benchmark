#!/usr/bin/env bash
# Prepare the fixed NautilusTrader benchmark environment. Never runs a backtest.
set -euo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ENV_NAME=nautilus-benchmark
PYTHON_VERSION=3.12.13
NAUTILUS_VERSION=2.0.0rc3.dev20260811
DATA_FILE="$PROJECT_DIR/data/btc-perp-20211231-20220201_1m.csv"
DATA_SHA256=65ab55cd5f3531aa64f429772a38331fe82a92973cbb32a79b243073236c8da3
PACKAGE_INDEX=https://packages.nautechsystems.io/simple

fail() {
    printf '\n[失败] %s\n' "$*" >&2
    exit 1
}

check_host() {
    [ "$(uname -s)" = Linux ] || fail "Only Linux is supported."
    [ "$(uname -m)" = x86_64 ] || fail "Only x86_64 is supported."
    command -v ldd >/dev/null || fail "ldd is required to check glibc."
    local glibc_version
    glibc_version=$(ldd --version | head -n 1 | grep -oE '[0-9]+\.[0-9]+' | tail -n 1)
    [ -n "$glibc_version" ] || fail "Could not determine glibc version."
    [ "$(printf '%s\n' 2.34 "$glibc_version" | sort -V | head -n 1)" = 2.34 ] || \
        fail "glibc $glibc_version is below the required 2.34."
    printf '[阶段 1/4 完成] 主机检查通过：Linux x86_64，glibc %s\n' "$glibc_version"
}

create_or_check_environment() {
    local conda_bin
    conda_bin=$(command -v conda || true)
    if [ -z "$conda_bin" ]; then
        for candidate in \
            "$HOME/miniforge3/bin/conda" \
            "$HOME/mambaforge/bin/conda" \
            "$HOME/anaconda3/bin/conda" \
            /opt/conda/bin/conda
        do
            if [ -x "$candidate" ]; then
                conda_bin=$candidate
                break
            fi
        done
    fi
    [ -n "$conda_bin" ] || fail "Conda/Miniforge was not found in PATH. Install it, then rerun this script."

    if ! "$conda_bin" run --no-capture-output -n "$ENV_NAME" python --version >/dev/null 2>&1; then
        printf '[阶段 2/4] 正在创建 Conda 环境 %q...\n' "$ENV_NAME"
        "$conda_bin" env create -f "$PROJECT_DIR/requirements/environment.yml"
        printf '[阶段 2/4 完成] Conda 环境已创建。\n'
    else
        printf '[阶段 2/4 完成] Conda 环境 %q 已存在，继续核验版本。\n' "$ENV_NAME"
    fi

    "$conda_bin" run --no-capture-output -n "$ENV_NAME" python -c "
import sys
expected_python = '$PYTHON_VERSION'
actual_python = '.'.join(map(str, sys.version_info[:3]))
if actual_python != expected_python:
    raise SystemExit(f'Python version mismatch: expected {expected_python}, found {actual_python}')
print(f'[阶段 2/4 完成] Python 版本正确：{actual_python}')
"

    printf '[阶段 3/4] 正在从 NautilusTrader 官方 nightly index 安装/核验固定 wheel...\n'
    "$conda_bin" run --no-capture-output -n "$ENV_NAME" python -m pip install \
        --disable-pip-version-check \
        --only-binary=:all: \
        --index-url "$PACKAGE_INDEX" \
        "nautilus_trader==$NAUTILUS_VERSION"

    "$conda_bin" run --no-capture-output -n "$ENV_NAME" python -c "
import nautilus_trader
expected_nautilus = '$NAUTILUS_VERSION'
if nautilus_trader.__version__ != expected_nautilus:
    raise SystemExit(f'NautilusTrader version mismatch: expected {expected_nautilus}, found {nautilus_trader.__version__}')
print(f'[阶段 3/4 完成] NautilusTrader 版本正确：{nautilus_trader.__version__}')
"
}

check_project_baseline() {
    [ -f "$DATA_FILE" ] || fail "Fixed dataset is missing: $DATA_FILE. Copy the complete project baseline, including data/."
    [ -d "$PROJECT_DIR/results" ] || fail "Project results/ directory is missing. Copy the complete project baseline."
    local actual_sha256
    actual_sha256=$(sha256sum "$DATA_FILE" | awk '{print $1}')
    [ "$actual_sha256" = "$DATA_SHA256" ] || \
        fail "Dataset SHA256 mismatch: expected $DATA_SHA256, found $actual_sha256. Refusing to run."
    printf '[阶段 4/4 完成] 固定数据校验通过：SHA256=%s，大小=%s bytes\n' \
        "$actual_sha256" "$(stat -c '%s' "$DATA_FILE")"
}

main() {
    printf 'NautilusTrader benchmark 环境准备（不会执行 benchmark/backtest）\n\n'
    check_host
    create_or_check_environment
    check_project_baseline
    printf '\n[成功] 环境准备完成。未执行 benchmark 或 backtest。\n'
    printf '仅在准备进入第二阶段时，再单独执行：\n'
    printf '  conda activate %s\n' "$ENV_NAME"
    printf '  cd %q\n' "$PROJECT_DIR"
    printf '  python scripts/benchmark.py\n'
    printf 'Benchmark executed: NO\n'
}

main "$@"
