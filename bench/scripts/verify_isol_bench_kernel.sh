#!/bin/bash
set -euo pipefail

TARGET_DEVICE="${TARGET_DEVICE:-nvme0n1}"
CGROUP_ROOT="${CGROUP_ROOT:-/sys/fs/cgroup}"
CHECK_CGROUP="$CGROUP_ROOT/ioext_bench_kernel_check"

mountpoint -q "$CGROUP_ROOT" || sudo mount -t cgroup2 none "$CGROUP_ROOT"
printf '%s\n' +io | sudo tee "$CGROUP_ROOT/cgroup.subtree_control" >/dev/null
sudo mkdir -p "$CHECK_CGROUP"

cleanup() {
    sudo rmdir "$CHECK_CGROUP" 2>/dev/null || true
}
trap cleanup EXIT

test -f "$CHECK_CGROUP/io.max" || { echo "missing $CHECK_CGROUP/io.max" >&2; exit 1; }

SCHEDULER_PATH="/sys/block/$TARGET_DEVICE/queue/scheduler"
SCHEDULERS=$(<"$SCHEDULER_PATH")
case " $SCHEDULERS " in
    *" bfq "*) ;;
    *) echo "bfq is not registered: $SCHEDULERS" >&2; exit 1 ;;
esac

ORIGINAL=$(printf '%s\n' "$SCHEDULERS" | sed -n 's/.*\[\([^]]*\)\].*/\1/p')
printf '%s\n' bfq | sudo tee "$SCHEDULER_PATH" >/dev/null
ACTIVE=$(<"$SCHEDULER_PATH")
case "$ACTIVE" in
    *"[bfq]"*) ;;
    *) echo "failed to activate bfq: $ACTIVE" >&2; exit 1 ;;
esac

if [ -n "$ORIGINAL" ]; then
    printf '%s\n' "$ORIGINAL" | sudo tee "$SCHEDULER_PATH" >/dev/null
fi

printf 'io.max=present scheduler_before=%s scheduler_bfq=%s\n' "$SCHEDULERS" "$ACTIVE"
