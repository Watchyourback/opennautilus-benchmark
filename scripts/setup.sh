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
MINIFORGE_VERSION=26.7.2-0
MINIFORGE_SHA256=281b0ac7d550802efc81af633225a5e6116d29ae72f3ab4eae7168c3931a4c05
CONDA_BIN=

fail() {
    printf '\n[失败] %s\n' "$*" >&2
    exit 1
}

check_host() {
    [ "$(uname -s)" = Linux ] || fail "Only Linux is supported."
    [ "$(uname -m)" = x86_64 ] || fail "Only x86_64 is supported."
    command -v ldd >/dev/null || fail "ldd is required to check glibc."
    local glibc_version
    glibc_version=$(ldd --version | sed -n '1p' | grep -oE '[0-9]+\.[0-9]+' | tail -n 1)
    [ -n "$glibc_version" ] || fail "Could not determine glibc version."
    [ "$(printf '%s\n' 2.34 "$glibc_version" | sort -V | head -n 1)" = 2.34 ] || \
        fail "glibc $glibc_version is below the required 2.34."
    printf '[阶段 1/4 完成] 主机检查通过：Linux x86_64，glibc %s\n' "$glibc_version"
}

install_miniforge() (
    local prefix=$1 installer_dir installer url actual_sha256
    [ ! -e "$prefix" ] && [ ! -L "$prefix" ] || \
        fail "Miniforge installation path already exists but has no usable Conda: $prefix. Inspect it before retrying."
    command -v sha256sum >/dev/null || fail "sha256sum is required to verify Miniforge."
    if ! command -v curl >/dev/null && ! command -v wget >/dev/null; then
        fail "curl or wget is required to download Miniforge."
    fi
    mkdir -p "$PROJECT_DIR/tmp"
    installer_dir=$(mktemp -d "$PROJECT_DIR/tmp/miniforge-install.XXXXXX")
    trap 'rm -rf -- "$installer_dir"' EXIT
    installer="$installer_dir/Miniforge3-$MINIFORGE_VERSION-Linux-x86_64.sh"
    url="https://github.com/conda-forge/miniforge/releases/download/$MINIFORGE_VERSION/$(basename -- "$installer")"
    printf '[阶段 2/4] 未检测到 Conda，正在下载并安装 Miniforge %s 到 %s...\n' "$MINIFORGE_VERSION" "$prefix"
    if command -v curl >/dev/null; then
        curl --fail --location --show-error --retry 3 --connect-timeout 20 \
            --output "$installer" "$url" || fail "Miniforge download failed: $url. Check network access and rerun."
    else
        wget --tries=3 --timeout=20 --output-document="$installer" "$url" || \
            fail "Miniforge download failed: $url. Check network access and rerun."
    fi
    actual_sha256=$(sha256sum "$installer" | awk '{print $1}')
    [ "$actual_sha256" = "$MINIFORGE_SHA256" ] || \
        fail "Miniforge SHA256 mismatch: expected $MINIFORGE_SHA256, found $actual_sha256. Refusing to install."
    bash "$installer" -b -p "$prefix" || \
        fail "Miniforge installation failed: $prefix. Inspect any partial installation before retrying."
    [ -x "$prefix/bin/conda" ] || fail "Miniforge did not install a usable Conda: $prefix/bin/conda."
)

find_or_install_conda() {
    local candidate
    CONDA_BIN=$(command -v conda || true)
    if [ -z "$CONDA_BIN" ]; then
        for candidate in \
            "$HOME/miniforge3/bin/conda" \
            "$HOME/miniconda3/bin/conda" \
            "$HOME/mambaforge/bin/conda" \
            "$HOME/anaconda3/bin/conda" \
            /opt/conda/bin/conda
        do
            if [ -x "$candidate" ]; then
                CONDA_BIN=$candidate
                break
            fi
        done
    fi
    if [ -z "$CONDA_BIN" ]; then
        install_miniforge "$HOME/miniforge3"
        CONDA_BIN="$HOME/miniforge3/bin/conda"
    fi
}

create_or_check_environment() {
    find_or_install_conda

    if ! "$CONDA_BIN" run --no-capture-output -n "$ENV_NAME" python --version >/dev/null 2>&1; then
        printf '[阶段 2/4] 正在创建 Conda 环境 %q...\n' "$ENV_NAME"
        "$CONDA_BIN" env create -f "$PROJECT_DIR/requirements/environment.yml"
        printf '[阶段 2/4 完成] Conda 环境已创建。\n'
    else
        printf '[阶段 2/4 完成] Conda 环境 %q 已存在，继续核验版本。\n' "$ENV_NAME"
    fi

    "$CONDA_BIN" run --no-capture-output -n "$ENV_NAME" python -c "
import sys
expected_python = '$PYTHON_VERSION'
actual_python = '.'.join(map(str, sys.version_info[:3]))
if actual_python != expected_python:
    raise SystemExit(f'Python version mismatch: expected {expected_python}, found {actual_python}')
print(f'[阶段 2/4 完成] Python 版本正确：{actual_python}')
"

    printf '[阶段 3/4] 正在从 NautilusTrader 官方 nightly index 安装/核验固定 wheel...\n'
    "$CONDA_BIN" run --no-capture-output -n "$ENV_NAME" python -m pip install \
        --disable-pip-version-check \
        --only-binary=:all: \
        --index-url "$PACKAGE_INDEX" \
        "nautilus_trader==$NAUTILUS_VERSION"

    "$CONDA_BIN" run --no-capture-output -n "$ENV_NAME" python -c "
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
    local conda_base
    printf 'NautilusTrader benchmark 环境准备（不会执行 benchmark/backtest）\n\n'
    check_host
    create_or_check_environment
    check_project_baseline
    conda_base=$("$CONDA_BIN" info --base)
    printf '\n[成功] 环境准备完成。未执行 benchmark 或 backtest。\n'
    printf '仅在准备进入第二阶段时，再单独执行：\n'
    printf '  source %q\n' "$conda_base/etc/profile.d/conda.sh"
    printf '  conda activate %s\n' "$ENV_NAME"
    printf '  cd %q\n' "$PROJECT_DIR"
    printf '  python scripts/benchmark.py\n'
    printf 'Benchmark executed: NO\n'
}

main "$@"
