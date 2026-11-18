#!/bin/bash
# tests/q_kyber_ci.sh — one-button host-side CI loop for q_kyber.
#
# Boots the test VM with the rig the acceptance test needs and runs
# tests/q_kyber_accept.sh inside it. Exit 0 = all strict cases PASS.
#
# Rig (mirrors tests/arb_wdrr_ci.sh; see userspace/README.md §11):
#   - OSEXT_VM_DISK=virtio: guest device vda with a shallow hardware queue
#   - accept script raises nr_requests to 1023 so the scheduler pipeline
#     exceeds the device queue and the elevator actually holds a backlog
#   - OSEXT_VM_BPS_THROTTLE=200MB/s: saturated device so dispatch decisions
#     come from the scheduler, not the submitter
#   - OSEXT_VM_DISK_IMG=/tmp/opencode/q_kyber_ci_disk.img (mandatory):
#     sibling policies boot their own VMs concurrently and QEMU write-locks
#     the default test_disk.img
#
# Prereq: userspace/build/loader_q_kyber built (scripts/make.sh q_kyber).
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export OSEXT_VM_DISK="${OSEXT_VM_DISK:-virtio}"
export OSEXT_VM_BPS_THROTTLE="${OSEXT_VM_BPS_THROTTLE:-200000000}"
export TARGET_DEVICE="${TARGET_DEVICE:-vda}"
export OSEXT_VM_DISK_IMG="${OSEXT_VM_DISK_IMG:-/tmp/opencode/q_kyber_ci_disk.img}"
mkdir -p "$(dirname "$OSEXT_VM_DISK_IMG")"

if [ "${SKIP_VM_BOOT:-0}" = "1" ]; then
    exec "$BASE_DIR/tests/q_kyber_accept.sh"
fi

cd "$BASE_DIR"

run_vm() {
    if command -v script >/dev/null 2>&1 && [ ! -t 0 ]; then
        script -q -c "./scripts/run.sh --exec 'cd $BASE_DIR && TARGET_DEVICE=vda IOEXT_DESTRUCTIVE_TEST=1 tests/q_kyber_accept.sh'" /dev/null
    else
        ./scripts/run.sh --exec "cd $BASE_DIR && TARGET_DEVICE=vda IOEXT_DESTRUCTIVE_TEST=1 tests/q_kyber_accept.sh"
    fi
}

# vng --exec does not propagate the guest command's exit code, so the real
# verdict is read back from the acceptance runner's summary.json (the VM
# shares $BASE_DIR/results with the host).
verdict_from_summary() {
    local latest
    latest=$(ls -t "$BASE_DIR"/results/q_kyber_accept_*/summary.json 2>/dev/null | head -1)
    [ -z "$latest" ] && return 1
    python3 - "$latest" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
bad = [k for k, v in d.get('verdicts', {}).items()
       if not (v == 'PASS' or v.startswith('FLAKE-'))]
if bad:
    print('[q_kyber_ci] strict failures: ' + ', '.join(bad), file=sys.stderr)
    sys.exit(1)
PY
}

for attempt in 1 2 3; do
    if ! run_vm; then
        echo "[q_kyber_ci] VM attempt $attempt/3 failed to boot (sibling VM contention?); retrying in 45s..." >&2
        sleep 45
        continue
    fi
    # VM ran; judge by the recorded summary (no retry on honest case FAIL).
    if verdict_from_summary; then
        exit 0
    fi
    exit 1
done
echo "[q_kyber_ci] giving up after 3 attempts" >&2
exit 2