#!/bin/bash
# tests/q_wedf_ci.sh — one-button host-side CI loop for q_wedf.
#
# Boots the test VM with the rig the acceptance test needs and runs
# tests/q_wedf_accept.sh inside it. Exit 0 = PASS.
#
# The rig:
#   - OSEXT_VM_DISK=virtio: shallow device queue (~256 tags), guest device vda
#   - accept script raises nr_requests to 1023 so the scheduler pipeline
#     exceeds the device queue and the elevator actually holds a backlog
#   - OSEXT_VM_BPS_THROTTLE=200MB/s: saturated device, so the deadline/EDF
#     ordering decisions — not the submitter — shape dispatch
#   - OSEXT_VM_DISK_IMG=/tmp/opencode/q_wedf_ci_disk.img: PRIVATE disk image
#     (mandatory: sibling agents boot VMs concurrently and QEMU write-locks
#     the default test_disk.img)
#
# Prereq: loader_q_wedf built (built single-target here if missing; no source
# edits involved).
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export OSEXT_VM_DISK="${OSEXT_VM_DISK:-virtio}"
export OSEXT_VM_BPS_THROTTLE="${OSEXT_VM_BPS_THROTTLE:-200000000}"
export TARGET_DEVICE="${TARGET_DEVICE:-vda}"
export OSEXT_VM_DISK_IMG="${OSEXT_VM_DISK_IMG:-/tmp/opencode/q_wedf_ci_disk.img}"

mkdir -p "$(dirname "$OSEXT_VM_DISK_IMG")"

if [ ! -x "$BASE_DIR/userspace/build/loader_q_wedf" ]; then
    echo "[q_wedf_ci] loader_q_wedf missing — building single target"
    make -C "$BASE_DIR/userspace" KERNEL_SRC="$BASE_DIR/kernel/linux-7.3.1" loader_q_wedf
fi

if [ "${SKIP_VM_BOOT:-0}" = "1" ]; then
    exec "$BASE_DIR/tests/q_wedf_accept.sh"
fi

cd "$BASE_DIR"
if command -v script >/dev/null 2>&1 && [ ! -t 0 ]; then
    script -q -c "./scripts/run.sh --exec 'cd $BASE_DIR && TARGET_DEVICE=vda IOEXT_DESTRUCTIVE_TEST=1 tests/q_wedf_accept.sh'" /dev/null
else
    ./scripts/run.sh --exec "cd $BASE_DIR && TARGET_DEVICE=vda IOEXT_DESTRUCTIVE_TEST=1 tests/q_wedf_accept.sh"
fi