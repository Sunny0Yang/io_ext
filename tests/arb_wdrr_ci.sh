#!/bin/bash
# tests/arb_wdrr_ci.sh — one-button host-side CI loop for arb_wdrr.
#
# Boots the test VM with the rig the acceptance test needs and runs
# tests/arb_wdrr_accept.sh inside it. Exit 0 = PASS.
#
# The rig (see userspace/ARB_POLICY_DEV_GUIDE.md):
#   - OSEXT_VM_DISK=virtio: shallow device queue (~256 tags)
#   - accept script raises nr_requests to 1023 so the scheduler pipeline
#     exceeds the device queue and the elevator actually holds a backlog
#   - OSEXT_VM_BPS_THROTTLE=200MB/s: saturated device, so the arbiter's
#     share decisions — not the submitter — determine each cgroup's rate
#
# Prereq: ./scripts/make.sh arb_wdrr q_fifo (artifacts must be current).
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export OSEXT_VM_DISK="${OSEXT_VM_DISK:-virtio}"
export OSEXT_VM_BPS_THROTTLE="${OSEXT_VM_BPS_THROTTLE:-200000000}"
export TARGET_DEVICE="${TARGET_DEVICE:-vda}"

if [ "${SKIP_VM_BOOT:-0}" = "1" ]; then
	exec "$BASE_DIR/tests/arb_wdrr_accept.sh"
fi

cd "$BASE_DIR"
if command -v script >/dev/null 2>&1 && [ ! -t 0 ]; then
	script -q -c "./scripts/run.sh --exec 'cd $BASE_DIR && TARGET_DEVICE=$TARGET_DEVICE tests/arb_wdrr_accept.sh'" /dev/null
else
	./scripts/run.sh --exec "cd $BASE_DIR && TARGET_DEVICE=$TARGET_DEVICE tests/arb_wdrr_accept.sh"
fi