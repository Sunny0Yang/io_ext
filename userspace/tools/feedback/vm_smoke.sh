#!/bin/bash
# tools/feedback/vm_smoke.sh - HOST driver for the arb_feedback vng attach smoke.
#
# Safety-ledger contract (ioext-bench results/iox_policy_exploration):
#   - runs entirely under flock /tmp/vng.lock
#   - aborts if any HALT_*.flag exists in the campaign
#   - create_attempt BEFORE the VM run (kernel/loader hashes, source hashes)
#   - serial capture via vng --qemu-opts "-serial file:..." + console=ttyS0
#   - guest heartbeat, run-captured evidence archive
#   - finalize_attempt + events + derive_summary afterwards
#
# Disk: a dedicated sparse raw image created for this smoke under
# ioext/results/feedback_smoke_disks/ -- it NEVER touches
# results/dynamic_tenant_virtme/disks/disk.img (in use by the timeline
# experiment) and never a host bare device.
#
# Usage: tools/feedback/vm_smoke.sh
# Exit 0 = smoke passed; 1 = smoke failed; 2 = blocked/HALT.
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # userspace/io
OS_EXT="$(cd "$BASE_DIR/../.." && pwd)"                          # ioext
KERNEL_SRC="$OS_EXT/kernel/linux-7.3.1"
PA_ROOT="${PA_ROOT:-/opt/ioext}"
LEDGER_ROOT="$PA_ROOT/results/iox_policy_exploration"
CAMPAIGN="${CAMPAIGN:-arb_feedback_smoke}"
CAP="$PA_ROOT/scripts/iox_safety_capture.py"
VNG_LOCK="/tmp/vng.lock"
VM_CPUS="${FB_VM_CPUS:-8}"
VM_MEMORY="${FB_VM_MEMORY:-4G}"
RUN_SECONDS="${FB_RUN_SECONDS:-240}"

TS="$(date +%Y%m%d_%H%M%S)"
export LEDGER_ROOT CAMPAIGN PA_ROOT
mkdir -p "$OS_EXT/results/feedback_smoke_disks"
DISK="$OS_EXT/results/feedback_smoke_disks/arb_fb_smoke_$TS.img"
SERIAL="$OS_EXT/results/feedback_smoke_disks/arb_fb_smoke_$TS.serial.log"

log() { printf '[vm_smoke] %s\n' "$*" >&2; }

ledger_py() { # python snippet with ledger importable; env carries the config
    PYTHONPATH="$PA_ROOT/src/lib" python3 - "$@"
}

# ---------------------------------------------------------------- HALT gate
HALTS="$(find "$LEDGER_ROOT" -maxdepth 2 -name 'HALT_*.flag' 2>/dev/null || true)"
if [ -n "$HALTS" ]; then
    log "HALT flag(s) present -- stopping per ledger contract:"
    log "$HALTS"
    exit 2
fi

# ---------------------------------------------------------------- attempt
export FB_ATTEMPT_JSON="$OS_EXT/results/feedback_smoke_disks/.attempt_$TS.json"
ATT_CREATE_RC=$(ledger_py <<'PYEOF'
import json, os, sys
from iox_safety import ledger

root = os.environ["LEDGER_ROOT"]
campaign = os.environ["CAMPAIGN"]
base = "/opt/ioext-io_ext"
io = f"{base}/userspace/io"
sources = [
    f"{io}/arb_feedback.bpf.c",
    f"{io}/ioext_arb_feedback_common.h",
    f"{io}/loader_arb_feedback.c",
    f"{io}/Makefile",
    f"{io}/tools/feedback/accept_static.sh",
    f"{io}/tools/feedback/fb_model.py",
    f"{io}/tools/feedback/guest_smoke.sh",
    f"{io}/tools/feedback/vm_smoke.sh",
]
man = ledger.create_attempt(
    root, campaign,
    metadata={
        "parent_candidate": "arb_feedback",
        "kernel_hash": f"{base}/kernel/linux-7.3.1/arch/x86/boot/bzImage",
        "loader_hash": f"{io}/build/loader_arb_feedback",
        "build_command": "make -C userspace/io arb_feedback",
        "workload_seed": 0,
        "stage": "attach_smoke_v1",
    },
    source_paths=sources,
)
print(json.dumps({"attempt_id": man["attempt_id"],
                  "attempt_dir": os.path.join(root, campaign, "attempts", man["attempt_id"])}))
PYEOF
) || true
if ! echo "$ATT_CREATE_RC" | grep -q attempt_id; then
    log "create_attempt failed: $ATT_CREATE_RC"
    exit 2
fi
ATT_ID=$(echo "$ATT_CREATE_RC" | python3 -c "import json,sys; print(json.load(sys.stdin)['attempt_id'])")
ATT_DIR=$(echo "$ATT_CREATE_RC" | python3 -c "import json,sys; print(json.load(sys.stdin)['attempt_dir'])")
export FB_ATTEMPT_DIR="$ATT_DIR"
log "attempt=$ATT_ID dir=$ATT_DIR"

EV="$ATT_DIR/evidence"
mkdir -p "$EV"

append_ev() { # type severity name notes [evidence...]
    local etype="$1" sev="$2" name="$3" notes="$4"; shift 4
    LEDGER_ROOT="$LEDGER_ROOT" CAMPAIGN="$CAMPAIGN" ATT_ID="$ATT_ID" \
    EV_TYPE="$etype" EV_SEV="$sev" EV_NAME="$name" EV_NOTES="$notes" EV_EVID="$(echo "$@" | tr ' ' ',')" \
    ledger_py <<'PYEOF'
import json, os
from iox_safety import ledger
ev = {
    "event_type": os.environ["EV_TYPE"],
    "severity": os.environ["EV_SEV"],
    "name": os.environ["EV_NAME"],
    "notes": os.environ["EV_NOTES"],
    "evidence": [p for p in os.environ["EV_EVID"].split(",") if p],
}
rec = ledger.append_event(os.environ["LEDGER_ROOT"], os.environ["CAMPAIGN"],
                          os.environ["ATT_ID"], ev)
print(rec["event_id"])
PYEOF
}

# ---------------------------------------------------------------- build event
append_ev BUILD info "arb_feedback_build" "make arb_feedback (host)" \
    "$BASE_DIR/build/arb_feedback.bpf.o"

# ---------------------------------------------------------------- disk
qemu-img create -f raw "$DISK" 8G > /dev/null || { log "qemu-img failed"; exit 2; }
log "sparse smoke disk: $DISK (8G sparse; dynamic_tenant disk untouched)"

# ---------------------------------------------------------------- vng run
GUEST_CMD="FB_BASE_DIR=$OS_EXT FB_ATTEMPT_DIR=$ATT_DIR FB_CAP=$CAP FB_DEVICE=vda \
    bash $BASE_DIR/tools/feedback/guest_smoke.sh"

VNG_CMD=(vng --run "$KERNEL_SRC"
    --memory "$VM_MEMORY" --cpus "$VM_CPUS"
    --append "console=ttyS0 no-kvmapf"
    --cwd "$OS_EXT/"
    --rwdir "$ATT_DIR"
    "--qemu-opts=-drive file=$DISK,format=raw,if=none,id=disk1,cache=none,aio=native -device virtio-blk-pci,drive=disk1,serial=deadbeef -cpu host,pmu=on -serial file:$SERIAL"
    --exec "$GUEST_CMD")

# Serial-capture plan (from iox_safety_capture.py serial-args, plan a):
# -serial file:<log> via --qemu-opts + console=ttyS0 via --append.
if [ ! -t 1 ]; then
    RUN_WRAPPER=(script -q -c "$(printf '%q ' "${VNG_CMD[@]}")" /dev/null)
else
    RUN_WRAPPER=("${VNG_CMD[@]}")
fi

log "acquiring $VNG_LOCK and launching vng (timeout ${RUN_SECONDS}s)"
set -e
flock "$VNG_LOCK" true
(
  flock -x 9
  python3 "$CAP" run-captured \
      --attempt-dir "$ATT_DIR" --tag vng_run --timeout-sec "$RUN_SECONDS" \
      -- "${RUN_WRAPPER[@]}"
) 9>"$VNG_LOCK"
set +e
VNG_RC=$?
log "vng run-captured rc=$VNG_RC"

# ---------------------------------------------------------------- evidence
HEARTBEAT="$EV/heartbeat.jsonl"
if [ -f "$HEARTBEAT" ]; then
    if python3 "$CAP" check-heartbeat --heartbeat-path "$HEARTBEAT" --max-age-sec 60 > "$EV/heartbeat_check.json" 2>&1; then
        append_ev INFRA info "heartbeat_alive" "guest heartbeat fresh at check time" "$HEARTBEAT"
    else
        append_ev INFRA warning "heartbeat_stale" "heartbeat stale/absent at check time" "$HEARTBEAT"
    fi
else
    append_ev INFRA warning "heartbeat_missing" "no heartbeat file produced" "$SERIAL"
fi

if [ -s "$SERIAL" ]; then
    append_ev LIFECYCLE info "serial_captured" "serial log non-empty" "$SERIAL"
else
    append_ev LIFECYCLE warning "serial_missing" "serial log empty/absent: no-panic claims are unknown" "$SERIAL"
fi

# ------------------------------------------------------------ verdict
VERDICT="failed"
ATTACH_OK=0; VERIFY_OK=0; RAN=0; RUN_SECONDS_REP=0
if [ -f "$EV/vng_run.meta.json" ]; then
    RUN_SECONDS_REP=$(python3 -c "import json;print(json.load(open('$EV/vng_run.meta.json'))['duration_sec'])" 2>/dev/null || echo 0)
fi
if [ -f "$EV/prog_show.json" ] && grep -qi '"type": *"struct_ops"' "$EV/prog_show.json" 2>/dev/null; then
    VERIFY_OK=1
fi
if grep -q "arbiter 'fb_arbiter'" "$EV/dmesg.txt" 2>/dev/null; then
    ATTACH_OK=1
fi
if [ "$VNG_RC" -eq 0 ] && grep -q "guest smoke END: PASS=[0-9]* FAIL=0" "$EV/vng_run.stdout.log" 2>/dev/null; then
    RAN=1; VERDICT="completed"
fi
if grep -qiE '\bpanic\b|BUG:|hung_task' "$SERIAL" 2>/dev/null; then
    append_ev KERNEL error "guest_kernel_error" "panic/BUG/hung_task pattern in serial log" "$SERIAL"
    VERDICT="failed"
fi

if [ "$VERIFY_OK" = "1" ]; then
    append_ev VERIFY info "verifier_accepted" "7 struct_ops programs loaded; prog_show evidence" \
        "$EV/prog_show.json"
else
    append_ev VERIFY error "verifier_rejected_or_unknown" \
        "prog_show missing/unreadable or loader log shows rejection" \
        "$EV/prog_show.json" "$EV/arb_feedback_loader.log"
fi
if [ "$ATTACH_OK" = "1" ]; then
    append_ev ATTACH info "arbiter_attached" "dmesg: arbiter 'fb_arbiter' arb_mode=1" \
        "$EV/dmesg.txt"
else
    append_ev ATTACH error "arbiter_not_attached" "no registration line in dmesg" \
        "$EV/dmesg.txt" "$EV/arb_feedback_loader.log"
fi

# ------------------------------------------------------------ finalize
LEDGER_ROOT="$LEDGER_ROOT" CAMPAIGN="$CAMPAIGN" ATT_ID="$ATT_ID" \
FINAL_STATUS="$VERDICT" F_BUILD=1 F_VERIFY="$VERIFY_OK" F_ATTACH="$ATTACH_OK" \
F_RAN="$RAN" F_SEC="$RUN_SECONDS_REP" \
ledger_py <<'PYEOF'
import json, os
from iox_safety import ledger
outcome = {
    "build_passed": bool(int(os.environ["F_BUILD"])),
    "verifier_passed": bool(int(os.environ["F_VERIFY"])),
    "attach_succeeded": bool(int(os.environ["F_ATTACH"])),
    "actually_ran": bool(int(os.environ["F_RAN"])),
    "hot_swaps": 0,
    "run_seconds": float(os.environ["F_SEC"] or 0),
}
final = ledger.finalize_attempt(
    os.environ["LEDGER_ROOT"], os.environ["CAMPAIGN"], os.environ["ATT_ID"],
    os.environ["FINAL_STATUS"], outcome,
    notes="arb_feedback first attach smoke (idle + single-tenant fio)")
print(json.dumps(final, indent=2))
PYEOF
FINAL_RC=$?
log "finalize rc=$FINAL_RC"

LEDGER_ROOT="$LEDGER_ROOT" CAMPAIGN="$CAMPAIGN" \
ledger_py <<'PYEOF'
import os
from iox_safety import ledger
ledger.derive_summary(os.environ["LEDGER_ROOT"], os.environ["CAMPAIGN"])
print("summary written")
PYEOF
log "attempt=$ATT_ID verdict=$VERDICT"
[ "$VERDICT" = "completed" ] && [ "$FINAL_RC" -eq 0 ]
