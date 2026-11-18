#!/bin/bash
# 宿主机运行：构建 userspace + 启动 virtme-ng + 执行 YCSB 全策略测试
#
# 用法:
#   ./tests/run_on_virtme.sh
#   ./tests/run_on_virtme.sh --stable-only
#   ./tests/run_on_virtme.sh --policy q_fifo
#   ./tests/run_on_virtme.sh --skip-build --ssh-port 10023
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
KERNEL_SRC="${KERNEL_SRC:-$BASE_DIR/kernel/linux-7.3.1}"
IO_DIR="$BASE_DIR/userspace"
YCSB_HOST="${YCSB_HOST:-/opt/ioext/src/vm/assets/ycsb/ycsb_runtime}"
YCSB_GUEST_DIR="${YCSB_GUEST_DIR:-tests/ycsb_runtime}"
DISK_IMG="${OSEXT_VM_DISK_IMG:-$BASE_DIR/test_disk.img}"
VM_MEMORY="${OSEXT_VM_MEMORY:-4G}"
VM_CPUS="${OSEXT_VM_CPUS:-16}"
SSH_PORT="${OSEXT_VM_SSH_PORT:-10023}"
# 当前 io_ext 内核若未启用 NVME，使用 virtio-blk（vda）；重建内核后可 QEMU_DISK=nvme
QEMU_DISK="${QEMU_DISK:-virtio}"
TARGET_DEVICE="${TARGET_DEVICE:-}"

SKIP_BUILD=0
TEST_ARGS=()
CUSTOM_EXEC=""

usage() {
	cat <<'EOF'
Usage: run_on_virtme.sh [options] [-- <test-args>]

Options:
  --skip-build        跳过 userspace 构建
  --ssh-port PORT     SSH 端口（默认 10023，自动递增找空闲）
  --exec CMD          在 VM 内执行自定义命令（替代默认 run_all_policies_ycsb.sh）
  -h, --help

传递给 run_all_policies_ycsb.sh 的参数示例:
  --stable-only | --hot-switch-only | --policy q_fifo
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
	--skip-build)
		SKIP_BUILD=1
		shift
		;;
	--ssh-port)
		SSH_PORT="${2:-}"
		[ -n "$SSH_PORT" ] || { echo "missing port" >&2; exit 1; }
		shift 2
		;;
	--exec)
		CUSTOM_EXEC="${2:-}"
		[ -n "$CUSTOM_EXEC" ] || { echo "missing exec cmd" >&2; exit 1; }
		shift 2
		;;
	-h | --help)
		usage
		exit 0
		;;
	--)
		shift
		TEST_ARGS=("$@")
		break
		;;
	--stable-only | --hot-switch-only | --policy)
		TEST_ARGS+=("$1")
		[ "$1" = "--policy" ] && { TEST_ARGS+=("${2:-}"); shift; }
		shift
		;;
	*)
		TEST_ARGS+=("$1")
		shift
		;;
	esac
done

find_free_port() {
	local port=$1
	while ss -tuln | grep -q ":$port "; do
		port=$((port + 1))
	done
	printf '%s\n' "$port"
}

require_cmd() {
	command -v "$1" >/dev/null 2>&1 || {
		printf 'missing command: %s\n' "$1" >&2
		exit 1
	}
}

build_userspace() {
	local libbpf bpftool

	libbpf="$KERNEL_SRC/tools/lib/bpf/libbpf.a"
	bpftool="$KERNEL_SRC/tools/bpf/bpftool/bpftool"

	if [ ! -f "$libbpf" ]; then
		printf '=== building libbpf ===\n'
		make -C "$KERNEL_SRC/tools/lib/bpf" -j"$(nproc)"
	fi
	if [ ! -x "$bpftool" ]; then
		printf '=== building bpftool ===\n'
		make -C "$KERNEL_SRC/tools/bpf/bpftool" -j"$(nproc)"
	fi

	printf '=== building io-ext userspace loaders ===\n'
	make -C "$IO_DIR" KERNEL_SRC="$KERNEL_SRC"
}

main() {
	local port test_cmd qemu_args vng_cmd run_wrapper

	require_cmd vng
	require_cmd qemu-img
	[ -d "$KERNEL_SRC" ] || {
		printf 'kernel tree missing: %s\n' "$KERNEL_SRC" >&2
		exit 1
	}
	[ -x "$YCSB_HOST/bin/ycsb" ] || {
		printf 'YCSB runtime missing: %s/bin/ycsb\n' "$YCSB_HOST" >&2
		exit 1
	}

	if [ "$SKIP_BUILD" -eq 0 ]; then
		build_userspace
	fi

	if [ ! -f "$DISK_IMG" ]; then
		printf '=== creating 10G test disk %s ===\n' "$DISK_IMG"
		qemu-img create -f raw "$DISK_IMG" 10G
	fi

	port="$(find_free_port "$SSH_PORT")"
	qemu_args="-drive file=$DISK_IMG,format=raw,if=none,id=disk1,cache=none,aio=native"
	if [ "$QEMU_DISK" = "nvme" ]; then
		qemu_args="$qemu_args -device nvme,drive=disk1,serial=deadbeef,max_ioqpairs=64"
		target_dev="${TARGET_DEVICE:-nvme0n1}"
	else
		qemu_args="$qemu_args -device virtio-blk-pci,drive=disk1,serial=deadbeef"
		target_dev="${TARGET_DEVICE:-vda}"
	fi
	qemu_args="$qemu_args -cpu host,pmu=on"

	if [ -n "$CUSTOM_EXEC" ]; then
		test_cmd="$CUSTOM_EXEC"
	else
		test_cmd="cd $BASE_DIR && TARGET_DEVICE=$target_dev YCSB_DIR=$BASE_DIR/$YCSB_GUEST_DIR"
		if [ -n "${IO_TEST_POLICIES_OVERRIDE:-}" ]; then
			test_cmd+=" IO_TEST_POLICIES_OVERRIDE=$(printf '%q' "$IO_TEST_POLICIES_OVERRIDE")"
		fi
		test_cmd+=" ./tests/run_all_policies_ycsb.sh"
		if [ ${#TEST_ARGS[@]} -gt 0 ]; then
			test_cmd+=" ${TEST_ARGS[*]}"
		fi
	fi

	printf '=== starting virtme-ng on SSH port %s ===\n' "$port"
	vng_cmd=(
		vng --run "$KERNEL_SRC"
		--memory "$VM_MEMORY"
		--cpus "$VM_CPUS"
		--append no-kvmapf
		--cwd "$BASE_DIR/"
		--rwdir="$BASE_DIR/results"
		--rwdir="$BASE_DIR/$YCSB_GUEST_DIR=$YCSB_HOST"
		--qemu-opts="$qemu_args"
		--exec "$test_cmd"
	)

	# vng 需要有效 PTY；非交互环境用 script 分配伪终端
	if [ -t 1 ]; then
		run_wrapper=("${vng_cmd[@]}")
	else
		require_cmd script
		run_wrapper=(script -q -c "$(printf '%q ' "${vng_cmd[@]}")" /dev/null)
	fi

	"${run_wrapper[@]}"
}

main "$@"
