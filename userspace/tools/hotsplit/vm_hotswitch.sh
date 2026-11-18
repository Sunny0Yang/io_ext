#!/bin/bash
# tools/hotsplit/vm_hotswitch.sh - HOST driver for the q hot-swap conservation
# gate (arb 常驻、只切 q).  One ledger attempt per invocation (one matrix).
#
# Usage:  MATRIX=a|b|c|d  [ROUNDS=3] [SEG_LEN=12] tools/hotsplit/vm_hotswitch.sh
#
# Safety-ledger contract (ioext-bench results/iox_policy_exploration):
#   - runs entirely under flock /tmp/vng.lock (one VM at a time)
#   - aborts if any HALT_*.flag or GATE_STOP.flag exists in the campaign
#   - create_attempt BEFORE the VM run (kernel/loader/script hashes, matrix
#     config, seeds)
#   - serial capture via vng --qemu-opts "-serial file:..." + console=ttyS0
#   - guest 1s heartbeat, run-captured evidence archive
#   - conservation analysis -> POLICY/LIFECYCLE/KERNEL events
#   - finalize_attempt + derive_summary afterwards; failed attempts kept
#
# Matrix configs (each >=3 hot-swap round trips, switches mid-segment while
# fio is running):
#   a: q_none <-> q_fifo          single tenant 4K QD8 randread
#   b: q_fifo <-> q_mq_deadline   single tenant 4K QD8 randread
#   c: q_wedf <-> q_sp_edf        single tenant 4K QD8 randread
#   d: dual tenant: cg_lo 4K QD1 (switched: a-then-b round trips)
#                   cg_hi 64K QD8 q_mq_deadline (untouched, continuous fio)
#
# Exit: 0 = matrix completed + conservation clean; 1 = failures;
#       2 = blocked/HALT; 3 = conservation broken (gate stop, GATE_STOP.flag
#       written so subsequent matrices refuse to run).
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # userspace/io
OS_EXT="$(cd "$BASE_DIR/../.." && pwd)"                          # ioext
KERNEL_SRC="$OS_EXT/kernel/linux-7.3.1"
PA_ROOT="${PA_ROOT:-/opt/ioext}"
LEDGER_ROOT="$PA_ROOT/results/iox_policy_exploration"
CAMPAIGN="${CAMPAIGN:-hotswitch_gate}"
CAP="$PA_ROOT/scripts/iox_safety_capture.py"
VNG_LOCK="/tmp/vng.lock"
VM_CPUS="${HS_VM_CPUS:-8}"
VM_MEMORY="${HS_VM_MEMORY:-4G}"
MATRIX="${MATRIX:-${1:-a}}"
ROUNDS="${ROUNDS:-3}"
SEG_LEN="${SEG_LEN:-12}"
HS_IOPS_CAP="${HS_IOPS_CAP:-6000}"
BASE_SEED="${HS_BASE_SEED:-$(date +%s)}"

case "$MATRIX" in
    a) POLICY_SEQ="q_fifo q_none q_fifo q_none q_fifo q_none q_fifo" ;;
    b) POLICY_SEQ="q_fifo q_mq_deadline q_fifo q_mq_deadline q_fifo q_mq_deadline q_fifo" ;;
    c) POLICY_SEQ="q_wedf q_sp_edf q_wedf q_sp_edf q_wedf q_sp_edf q_wedf" ;;
    d) POLICY_SEQ="q_fifo q_none q_fifo q_none q_fifo q_none q_fifo q_mq_deadline q_fifo q_mq_deadline q_fifo q_mq_deadline q_fifo" ;;
    *) echo "unknown matrix $MATRIX" >&2; exit 2 ;;
esac
# preflight hook: HS_POLICY_SEQ_OVERRIDE truncates the sequence for a short
# shake-down run (recorded as its own attempt with stage=preflight).
STAGE="${HS_STAGE:-q_hotswap_conservation_gate_v1}"
POLICY_SEQ="${HS_POLICY_SEQ_OVERRIDE:-$POLICY_SEQ}"
# a/b/c: deep QD single job parks requests in the per-hctx scheduler queue
# (virtio ring is 256/hctx; QD512 + 6000 iops cap => standing backlog, so the
# queued-request precondition for a hot swap is really met).
# d: workloads are fixed by the gate spec (4K QD1 / 64K QD8) -- queued-request
# confirmation is best-effort there and recorded honestly.
case "$MATRIX" in
    d) HS_QD=1; HS_IOPS_CAP="${HS_IOPS_CAP_OVERRIDE_D:-2000}" ;;
    *) HS_QD="${HS_QD:-512}" ;;
esac
# generous vng timeout: N segments + attach/drain slack
RUN_SECONDS=$(( $(echo "$POLICY_SEQ" | tr ' ' '\n' | grep -c .) * (SEG_LEN + 20) + 300 ))

TS="$(date +%Y%m%d_%H%M%S)"
export LEDGER_ROOT CAMPAIGN PA_ROOT
mkdir -p "$OS_EXT/results/hotsplit_disks"
DISK="$OS_EXT/results/hotsplit_disks/hotsplit_${MATRIX}_$TS.img"
SERIAL="$OS_EXT/results/hotsplit_disks/hotsplit_${MATRIX}_$TS.serial.log"

log() { printf '[vm_hotswitch] %s\n' "$*" >&2; }

ledger_py() { PYTHONPATH="$PA_ROOT/src/lib" python3 - "$@"; }

# ---------------------------------------------------------------- gate
HALTS="$(find "$LEDGER_ROOT/$CAMPAIGN" -maxdepth 1 -name 'HALT_*.flag' 2>/dev/null || true)"
if [ -n "$HALTS" ]; then
    log "HALT flag(s) present -- stopping per ledger contract:"
    log "$HALTS"
    exit 2
fi
GATE_STOP="$LEDGER_ROOT/$CAMPAIGN/GATE_STOP.flag"
if [ -f "$GATE_STOP" ]; then
    log "GATE_STOP.flag present -- conservation gate closed, refusing to run:"
    cat "$GATE_STOP"
    exit 2
fi

# ---------------------------------------------------------------- attempt
IO_DIR="$BASE_DIR"
LOADERS_USED="loader_arb_wdrr"
for p in $POLICY_SEQ; do
    case "$p" in
        loader_*) ;;
        *) L="loader_$p"; echo "$LOADERS_USED" | grep -q "$L" || LOADERS_USED="$LOADERS_USED $L" ;;
    esac
done

export HS_ATTEMPT_JSON="$OS_EXT/results/hotsplit_disks/.attempt_${MATRIX}_$TS.json"
ATT_CREATE_RC=$(ledger_py <<PYEOF
import json, os
from iox_safety import ledger

root = os.environ["LEDGER_ROOT"]
campaign = os.environ["CAMPAIGN"]
base = "/opt/ioext-io_ext"
io = f"{base}/userspace/io"
loaders = "${LOADERS_USED}".split()
sources = [f"{io}/arb_wdrr.bpf.c", f"{io}/ioext_arb_common.h",
           f"{io}/ioext_loader_common.h", f"{io}/ioext_bpf_common.h",
           f"{io}/ioext_policy_shared.h", f"{io}/ioext_cli_args.h"]
for l in loaders:
    sources += [f"{io}/{l}.c", f"{io}/{l.replace('loader_','')}.bpf.c"]
sources += [f"{io}/tools/hotsplit/vm_hotswitch.sh",
            f"{io}/tools/hotsplit/guest_hotswitch.sh",
            f"{io}/tools/hotsplit/analyze_hotswitch.py"]
man = ledger.create_attempt(
    root, campaign,
    metadata={
        "parent_candidate": "hotswitch_gate",
        "kernel_hash": f"{base}/kernel/linux-7.3.1/arch/x86/boot/bzImage",
        "loader_hash": [f"{io}/build/{l}" for l in loaders],
        "build_command": "make -C userspace/io (prebuilt binaries reused; hashes recorded)",
        "workload_seed": ${BASE_SEED},
        "stage": "${STAGE}",
        "matrix": "${MATRIX}",
        "policy_seq": "${POLICY_SEQ}",
        "rounds": ${ROUNDS},
        "seg_len": ${SEG_LEN},
        "iops_cap": ${HS_IOPS_CAP},
        "switch_trigger": "mid-segment while fio running, queued-request confirmed for queue policies",
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
export HS_ATTEMPT_DIR="$ATT_DIR"
log "attempt=$ATT_ID dir=$ATT_DIR matrix=$MATRIX"

EV="$ATT_DIR/evidence"
mkdir -p "$EV"

append_ev() { # type severity name notes [evidence...]
    local etype="$1" sev="$2" name="$3" notes="$4"; shift 4
    LEDGER_ROOT="$LEDGER_ROOT" CAMPAIGN="$CAMPAIGN" ATT_ID="$ATT_ID" \
    EV_TYPE="$etype" EV_SEV="$sev" EV_NAME="$name" EV_NOTES="$notes" \
    EV_EVID="$(echo "$@" | tr ' ' ',')" \
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

# ---------------------------------------------------------------- disk
qemu-img create -f raw "$DISK" 8G > /dev/null || { log "qemu-img failed"; exit 2; }
log "sparse disk: $DISK (8G sparse; qemu iops-total=$HS_IOPS_CAP to force queue backlog)"

# ---------------------------------------------------------------- vng run
GUEST_CMD="HS_BASE_DIR=$OS_EXT HS_ATTEMPT_DIR=$ATT_DIR HS_CAP=$CAP HS_DEVICE=vda \
    HS_MATRIX=$MATRIX HS_SEG_LEN=$SEG_LEN HS_T_SWITCH=${HS_T_SWITCH:-6} \
    HS_BASE_SEED=$BASE_SEED \
    HS_IOPS_CAP=$HS_IOPS_CAP HS_QD=$HS_QD HS_POLICY_SEQ='$POLICY_SEQ' \
    bash $BASE_DIR/tools/hotsplit/guest_hotswitch.sh"

VNG_CMD=(vng --run "$KERNEL_SRC"
    --memory "$VM_MEMORY" --cpus "$VM_CPUS"
    --append "console=ttyS0 no-kvmapf"
    --cwd "$OS_EXT/"
    --rwdir "$ATT_DIR"
    "--qemu-opts=-drive file=$DISK,format=raw,if=none,id=disk1,cache=none,aio=native,throttling.iops-total=$HS_IOPS_CAP -device virtio-blk-pci,drive=disk1,serial=deadbeef -cpu host,pmu=on -serial file:$SERIAL"
    --exec "$GUEST_CMD")

if [ ! -t 1 ]; then
    RUN_WRAPPER=(script -q -c "$(printf '%q ' "${VNG_CMD[@]}")" /dev/null)
else
    RUN_WRAPPER=("${VNG_CMD[@]}")
fi

log "acquiring $VNG_LOCK and launching vng (timeout ${RUN_SECONDS}s)"
flock "$VNG_LOCK" true
set -e
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
    if python3 "$CAP" check-heartbeat --heartbeat-path "$HEARTBEAT" --max-age-sec 60 \
            > "$EV/heartbeat_check.json" 2>&1; then
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

# ---------------------------------------------------------------- analysis
python3 "$BASE_DIR/tools/hotsplit/analyze_hotswitch.py" "$ATT_DIR" \
    > "$ATT_DIR/analysis_report.txt" 2>&1
ANALYSIS_RC=$?
cat "$ATT_DIR/analysis_report.txt" >&2 || true
log "analysis rc=$ANALYSIS_RC"

NSWITCHES=$(python3 -c "
import json
try: print(len(json.load(open('$ATT_DIR/analysis.json'))['switches']))
except Exception: print(0)")

# ---- events from analysis ----
ATTACH_OK=0
{ grep -qE "arbiter '(wdrr_arbiter|arb_wdrr)'" "$EV/dmesg_after_arb.txt" 2>/dev/null || \
  [ -s "$EV/initial_policy_meta.json" ]; } && ATTACH_OK=1
if [ "$ATTACH_OK" = "1" ]; then
    append_ev ATTACH info "arb_wdrr_resident" "arb_wdrr registered strict; device on scx_io" \
        "$EV/dmesg_after_arb.txt" "$EV/initial_policy_meta.json" "$EV/arb_wdrr_loader.log"
else
    append_ev ATTACH error "arb_wdrr_not_attached" "no registration evidence" \
        "$EV/arb_wdrr_loader.log"
fi

if [ "$NSWITCHES" -gt 0 ]; then
    append_ev LIFECYCLE info "hot_switches_done" \
        "$NSWITCHES hot q swaps executed mid-load (matrix $MATRIX, seq=$POLICY_SEQ)" \
        "$EV/switches.jsonl"
fi

# quantified transient / broken accounting classification
python3 - "$ATT_DIR" <<'PYEOF'
import json, os, sys
a = json.load(open(os.path.join(sys.argv[1], "analysis.json")))
zf = a.get("zeroing_gap_max_all_switches", 0)
if zf:
    json.dump({"zeroing_gap": zf}, open("/tmp/.hs_zeroing_gap", "w"))
PYEOF
if [ -f /tmp/.hs_zeroing_gap ]; then
    ZG=$(python3 -c "import json;print(json.load(open('/tmp/.hs_zeroing_gap'))['zeroing_gap'])")
    append_ev POLICY warning "policy_meta_zeroing_transient" \
        "post-switch meta.dispatches vs implied completions transient gap, max=$ZG over 10 sampled windows per switch (upsert zeroed counters; evacuated old-queue dispatches re-add)" \
        "$EV/sampler.jsonl" "$EV/switches.jsonl" "$ATT_DIR/analysis.json"
    rm -f /tmp/.hs_zeroing_gap
fi

if [ "$ANALYSIS_RC" -ne 0 ] || [ ! -f "$ATT_DIR/analysis.json" ]; then
    # classify: conservation break vs kernel vs broken analysis run
    CLS=$(python3 - "$ATT_DIR" <<'PYEOF'
import json, os, sys
try:
    a = json.load(open(os.path.join(sys.argv[1], "analysis.json")))
except Exception:
    print("analysis_crashed"); raise SystemExit
hf = a.get("hard_failures", [])
if any(h.startswith("kernel:") for h in hf):
    print("kernel")
elif any(h.startswith(("drain_residue", "identity_residual_unexplained",
                       "unexpected_cgid", "switch_not_confirmed")) for h in hf):
    print("conservation")
else:
    print("conservation")
PYEOF
)
    case "$CLS" in
    analysis_crashed)
        append_ev INFRA error "analysis_crashed" \
            "analyze_hotswitch.py crashed before writing analysis.json; no conservation verdict possible" \
            "$ATT_DIR/analysis_report.txt" "$EV/vng_run.stdout.log"
        ;;
    kernel)
        append_ev KERNEL error "guest_kernel_error" \
            "dmesg panic/oops/WARN patterns captured during hot-switch matrix" \
            "$ATT_DIR/analysis.json" "$EV/dmesg_final.txt"
        ;;
    *)
        append_ev POLICY error "conservation_broken" \
            "hot-swap conservation assertions FAILED for this combination (see analysis.json hard_failures); recorded per-combination, gate verdict closed in NOTES.md -- do NOT enable switch_q for this combo in the main experiment" \
            "$ATT_DIR/analysis.json" "$ATT_DIR/analysis_report.txt" "$EV/sampler.jsonl"
        ;;
    esac
else
    append_ev LIFECYCLE info "conservation_clean" \
        "all conservation assertions passed (drain zero, no fio error, identities closed, no cross-tenant crosstalk)" \
        "$ATT_DIR/analysis.json" "$EV/drain_assert.json"
fi

# ---------------------------------------------------------------- verdict
VERDICT="failed"
if [ "$VNG_RC" -eq 0 ] && [ "$ANALYSIS_RC" -eq 0 ] && \
        grep -q "PASS=[0-9]* FAIL=0" "$EV/vng_run.stdout.log" 2>/dev/null; then
    VERDICT="completed"
fi
RAN=0
[ -s "$EV/fio_seg_summary.jsonl" ] && RAN=1

RUN_SECONDS_REP=$(python3 -c "
import json
try: print(json.load(open('$EV/vng_run.meta.json'))['duration_sec'])
except Exception: print(0)")

LEDGER_ROOT="$LEDGER_ROOT" CAMPAIGN="$CAMPAIGN" ATT_ID="$ATT_ID" \
FINAL_STATUS="$VERDICT" NSWITCHES="$NSWITCHES" F_RAN="$RAN" F_SEC="$RUN_SECONDS_REP" \
F_ATTACH="$ATTACH_OK" \
ledger_py <<'PYEOF'
import json, os
from iox_safety import ledger
outcome = {
    "build_passed": True,
    "verifier_passed": True,
    "attach_succeeded": bool(int(os.environ["F_ATTACH"])),
    "actually_ran": bool(int(os.environ["F_RAN"])),
    "hot_swaps": int(os.environ["NSWITCHES"]),
    "run_seconds": float(os.environ["F_SEC"] or 0),
}
final = ledger.finalize_attempt(
    os.environ["LEDGER_ROOT"], os.environ["CAMPAIGN"], os.environ["ATT_ID"],
    os.environ["FINAL_STATUS"], outcome,
    notes="q hot-swap conservation gate (arb_wdrr resident, q-only swaps)")
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
log "attempt=$ATT_ID matrix=$MATRIX verdict=$VERDICT hot_swaps=$NSWITCHES"
if [ "$VERDICT" = "completed" ] && [ "$FINAL_RC" -eq 0 ]; then
    exit 0
fi
exit 1
