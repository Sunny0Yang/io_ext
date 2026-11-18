#!/bin/bash
# tests/q_ddl_ci.sh — one-button host-side CI loop for q_ddl.
#
# Boots the test VM with the Tier-2 rig and runs
# tests/q_ddl_accept.sh inside it. Exit 0 = PASS.
#
# The rig (q_mq_deadline / arb_wdrr standard, userspace/ARB_POLICY_DEV_GUIDE.md):
#   - OSEXT_VM_DISK=virtio: guest device vda, shallow queue (~256 tags)
#   - OSEXT_VM_BPS_THROTTLE=200MB/s: saturated device, so the scheduler's
#     lane/precedence decisions — not the submitter — determine dispatch order
#   - OSEXT_VM_DISK_IMG: MANDATORY unique path — sibling agents boot VMs
#     concurrently and QEMU takes a write lock on the shared test_disk.img
#
# Prereq: loader_q_ddl built (userspace/build/loader_q_ddl). This script
# deliberately does NOT build anything — sibling agents run concurrent
# builds; the loader is a prereq, not a side effect.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export OSEXT_VM_DISK="${OSEXT_VM_DISK:-virtio}"
export OSEXT_VM_BPS_THROTTLE="${OSEXT_VM_BPS_THROTTLE:-200000000}"
export TARGET_DEVICE="${TARGET_DEVICE:-vda}"
export OSEXT_VM_DISK_IMG="${OSEXT_VM_DISK_IMG:-/tmp/opencode/q_ddl_ci_disk.img}"
mkdir -p "$(dirname "$OSEXT_VM_DISK_IMG")"

if [ ! -x "$BASE_DIR/userspace/build/loader_q_ddl" ]; then
    echo "[q_ddl_ci] loader_q_ddl missing at userspace/build/ — build it first (./scripts/make.sh q_ddl); this CI never builds (concurrent-agent constraint)"
    exit 1
fi

if [ "${SKIP_VM_BOOT:-0}" = "1" ]; then
	exec "$BASE_DIR/tests/q_ddl_accept.sh"
fi

cd "$BASE_DIR"

# 2 sibling agents may boot VMs concurrently; on boot failure (QEMU image
# lock, resource exhaustion) sleep and retry, then give up as BLOCKED-VM.
run_vm() {
	if command -v script >/dev/null 2>&1 && [ ! -t 0 ]; then
		script -q -c "./scripts/run.sh --exec 'cd $BASE_DIR && TARGET_DEVICE=$TARGET_DEVICE IOEXT_DESTRUCTIVE_TEST=1 tests/q_ddl_accept.sh'" /dev/null
	else
		./scripts/run.sh --exec "cd $BASE_DIR && TARGET_DEVICE=$TARGET_DEVICE IOEXT_DESTRUCTIVE_TEST=1 tests/q_ddl_accept.sh"
	fi
}

attempt=1
while [ $attempt -le 3 ]; do
	echo "[q_ddl_ci] VM boot attempt $attempt/3 (disk_img=$OSEXT_VM_DISK_IMG)"
	if run_vm; then
		exit 0
	fi
	rc=$?
	echo "[q_ddl_ci] VM run attempt $attempt failed (rc=$rc)"
	if [ "$attempt" = "3" ]; then
		echo "[q_ddl_ci] BLOCKED-VM: 3 boot attempts failed"
		exit 1
	fi
	sleep 45
	attempt=$((attempt + 1))
done
exit 1