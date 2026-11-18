#!/bin/bash
# run_on_virtme.sh - host-side entry: build + boot VM + run merge suite
set -euo pipefail

BASE_DIR="${BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
cd "$BASE_DIR"

KERNEL_SRC="${KERNEL_SRC:-$BASE_DIR/kernel/linux-7.3.1}"
DISK_IMG="${OSEXT_VM_DISK_IMG:-$BASE_DIR/test_disk.img}"
VM_MEMORY="${OSEXT_VM_MEMORY:-4G}"
VM_CPUS="${OSEXT_VM_CPUS:-64}"
SSH_PORT="${OSEXT_VM_SSH_PORT:-10023}"
FIO_RUNTIME="${FIO_RUNTIME:-30}"
SKIP_BUILD="${SKIP_BUILD:-0}"
MERGE_PHASE="${MERGE_PHASE:-all}"

usage() {
    cat <<'EOF'
Usage: run_on_virtme.sh [options]

Options:
  --skip-build       Skip kernel/userspace build
  --fio-runtime SEC  Per-fio runtime (default 30)
  --phase N          Run only phase N (1|2|3|all, default all)
  -h, --help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
    --skip-build) SKIP_BUILD=1; shift ;;
    --fio-runtime)
        FIO_RUNTIME="${2:-}"
        shift 2
        ;;
    --phase)
        MERGE_PHASE="${2:-all}"
        shift 2
        ;;
    -h | --help)
        usage
        exit 0
        ;;
    *)
        echo "unknown arg: $1" >&2
        usage
        exit 1
        ;;
    esac
done

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing command: $1" >&2
        exit 1
    }
}

if [ "$SKIP_BUILD" -eq 0 ]; then
    echo "=== [1] Build (merge-test kernel config + io policies) ==="
    KERNEL_CONFIG="$BASE_DIR/kernel/config/config-7.0-osext-merge-test" \
        IOEXT_DEBUG=1 \
        ./scripts/make.sh q_merge_probe q_kyber q_mq_deadline
else
    echo "=== [1] Skipping build (--skip-build) ==="
fi

echo "=== [2] Boot VM and run merge suite ==="
# virtme-ng --cwd mounts $BASE_DIR at the same absolute path inside the guest.
RUN_CMD=$(cat <<EOF
set -euo pipefail
cd '$BASE_DIR'
export BASE_DIR='$BASE_DIR'
export PATH='$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool:$PATH'
if ! command -v fio >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq fio bpftrace 2>/dev/null || true
fi
scripts/vm_init.sh
export FIO_RUNTIME='$FIO_RUNTIME'
export MERGE_PHASE='$MERGE_PHASE'
bash tests/merge/run_merge_suite.sh
EOF
)

require_cmd vng
require_cmd qemu-img
require_cmd script
[ -d "$KERNEL_SRC" ] || {
    echo "kernel tree missing: $KERNEL_SRC" >&2
    exit 1
}
[ -f "$DISK_IMG" ] || qemu-img create -f raw "$DISK_IMG" 10G

QEMU_ARGS="-drive file=$DISK_IMG,format=raw,if=none,id=disk1,cache=none,aio=native"
QEMU_ARGS="$QEMU_ARGS -device nvme,drive=disk1,serial=deadbeef,num_queues=64 -cpu host,pmu=on"

VNG_CMD=(
    vng --run "$KERNEL_SRC"
    --memory "$VM_MEMORY"
    --cpus "$VM_CPUS"
    --append no-kvmapf
    --cwd "$BASE_DIR/"
    --rwdir="$BASE_DIR/results"
    --qemu-opts="$QEMU_ARGS"
    --exec "$RUN_CMD"
)

echo "=== starting virtme-ng (FIO_RUNTIME=${FIO_RUNTIME}s, phase=${MERGE_PHASE}) ==="
if [ -t 1 ]; then
    "${VNG_CMD[@]}"
else
    script -q -c "$(printf '%q ' "${VNG_CMD[@]}")" /dev/null
fi

echo "=== [3] Generate report ==="
RESULT_DIR="$BASE_DIR/results/io_merge_suite"
LATEST=$(ls -dt "$RESULT_DIR"/suite_* 2>/dev/null | head -1)
if [ -n "$LATEST" ] && [ -d "$LATEST" ]; then
    python3 "$BASE_DIR/tests/merge/summarize_merge_results.py" "$LATEST"
    echo "Report: $LATEST/MERGE_REPORT.md"
    echo "Suite:  $LATEST"
else
    echo "No suite results found in $RESULT_DIR" >&2
    exit 1
fi
