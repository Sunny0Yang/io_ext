#!/bin/bash
# tests/full_suite/run_on_virtme.sh
#
# 宿主机入口: 构建 -> 启动 virtme-ng VM -> 执行 full_suite -> 回收报告
#
# 用法:
#   ./tests/full_suite/run_on_virtme.sh                          # 全量 quick
#   ./tests/full_suite/run_on_virtme.sh --skip-build              # 跳过构建
#   ./tests/full_suite/run_on_virtme.sh --mode full               # 完整模式
#   ./tests/full_suite/run_on_virtme.sh --mode deterministic-only # 仅确定性
#   ./tests/full_suite/run_on_virtme.sh --mode lifecycle-only     # 仅生命周期
#   ./tests/full_suite/run_on_virtme.sh --policy q_mq_deadline    # 单策略
#
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
KERNEL_SRC="${KERNEL_SRC:-$BASE_DIR/kernel/linux-7.3.1}"
IO_DIR="$BASE_DIR/userspace"
DISK_IMG="${OSEXT_VM_DISK_IMG:-$BASE_DIR/test_disk.img}"
VM_MEMORY="${OSEXT_VM_MEMORY:-4G}"
VM_CPUS="${OSEXT_VM_CPUS:-16}"
SSH_PORT="${OSEXT_VM_SSH_PORT:-10023}"
QEMU_DISK="${QEMU_DISK:-nvme}"
TARGET_DEVICE="${TARGET_DEVICE:-nvme0n1}"
MODE="quick"
SKIP_BUILD=0
POLICY_OVERRIDE=""
EXTRA_ARGS=()

usage() {
    cat <<EOF
Usage: $0 [options]

Options:
  --skip-build          跳过 kernel + loader 构建
  --mode MODE           quick (默认) | full | deterministic-only | lifecycle-only | merge-only
  --policy POLICY       仅测试指定策略 (如 q_mq_deadline)
  --ssh-port PORT       SSH 端口 (默认 10023)
  -h, --help            显示帮助
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --skip-build)   SKIP_BUILD=1; shift ;;
        --mode)         MODE="${2:?}"; shift 2 ;;
        --policy)       POLICY_OVERRIDE="${2:?}"; shift 2 ;;
        --ssh-port)     SSH_PORT="${2:?}"; shift 2 ;;
        -h|--help)      usage; exit 0 ;;
        *)              EXTRA_ARGS+=("$1"); shift ;;
    esac
done

find_free_port() {
    local port=$1
    while ss -tuln 2>/dev/null | grep -q ":$port "; do
        port=$((port + 1))
    done
    printf '%s\n' "$port"
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || { printf 'missing: %s\n' "$1" >&2; exit 1; }
}

build_all() {
    printf '=== [1] Build kernel + loaders ===\n'
    if [ ! -f "$KERNEL_SRC/.config" ]; then
        printf 'WARN: no kernel .config; run scripts/seed-kernel-config.sh first\n' >&2
        exit 1
    fi
    # 构建 kernel + 全部 io 策略
    "$BASE_DIR/scripts/make.sh"
}

build_io_only() {
    printf '=== [1] Build io loaders (skip kernel) ===\n'
    make -C "$IO_DIR" KERNEL_SRC="$KERNEL_SRC" all
}

# ── main ──
require_cmd vng
require_cmd qemu-img
[ -d "$KERNEL_SRC" ] || { printf 'kernel tree missing: %s\n' "$KERNEL_SRC" >&2; exit 1; }

if [ "$SKIP_BUILD" -eq 0 ]; then
    # 检测 kernel 是否已构建; 若未构建则全量, 否则仅 io
    if [ -f "$KERNEL_SRC/vmlinux" ] && [ -f "$KERNEL_SRC/arch/x86/boot/bzImage" ]; then
        build_io_only
    else
        build_all
    fi
fi

if [ ! -f "$DISK_IMG" ]; then
    printf '=== creating 10G test disk %s ===\n' "$DISK_IMG"
    qemu-img create -f raw "$DISK_IMG" 10G
fi

port="$(find_free_port "$SSH_PORT")"

# QEMU 磁盘参数
qemu_args="-drive file=$DISK_IMG,format=raw,if=none,id=disk1,cache=none,aio=native"
if [ "$QEMU_DISK" = "nvme" ]; then
    qemu_args="$qemu_args -device nvme,drive=disk1,serial=deadbeef,num_queues=64"
    target_dev="${TARGET_DEVICE:-nvme0n1}"
else
    qemu_args="$qemu_args -device virtio-blk-pci,drive=disk1,serial=deadbeef"
    target_dev="${TARGET_DEVICE:-vda}"
fi
qemu_args="$qemu_args -cpu host,pmu=on"

# VM 内执行的命令
test_cmd="cd $BASE_DIR"
test_cmd+=" && export TARGET_DEVICE=$target_dev"
test_cmd+=" && export FULL_SUITE_MODE=$MODE"
test_cmd+=" && export IOEXT_DESTRUCTIVE_TEST=1"
test_cmd+=" && export ENABLE_COVERAGE=${ENABLE_COVERAGE:-1}"
if [ -n "$POLICY_OVERRIDE" ]; then
    test_cmd+=" && export POLICY_OVERRIDE=$POLICY_OVERRIDE"
fi
test_cmd+=" && bash tests/full_suite/full_suite.sh"
if [ ${#EXTRA_ARGS[@]} -gt 0 ]; then
    test_cmd+=" ${EXTRA_ARGS[*]}"
fi

printf '=== starting virtme-ng on port %s (mode=%s) ===\n' "$port" "$MODE"

vng_cmd=(
    vng --run "$KERNEL_SRC"
    --memory "$VM_MEMORY"
    --cpus "$VM_CPUS"
    --append "no-kvmapf"
    --cwd "$BASE_DIR/"
    --rwdir "$BASE_DIR/results"
    --qemu-opts="$qemu_args"
    --exec "$test_cmd"
)

# 非 TTY 环境用 script 分配 PTY
if [ -t 1 ]; then
    "${vng_cmd[@]}"
else
    require_cmd script
    script -q -c "$(printf '%q ' "${vng_cmd[@]}")" /dev/null
fi

printf '=== full_suite complete; results in %s/results/full_suite/ ===\n' "$BASE_DIR"