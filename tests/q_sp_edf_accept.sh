#!/bin/bash
# tests/q_sp_edf_accept.sh — q_sp_edf closed-loop acceptance test (VM-side).
#
# Mirrors the q_mq_deadline Tier-2 standard (tests/full_suite/EVOLUTION_PLAN.md
# §1.2 G1-G4) using the shared full_suite helpers:
#   gate 0  reference model self-test (tests/q_sp_edf_model.py), like
#           tests/arb_wdrr_accept.sh gate 0
#   per-case flow mirrors tests/full_suite/deterministic/run_dt_mqdd_only.sh
#           run_one: fs_set_scx_scheduler + nr_requests=1023 (elevator
#           pipeline must exceed device tags), fs_start_loader/fs_wait_attached,
#           bpftrace capture, trace_driver async batch, TRACE_PLAY marker grep,
#           loader stop, dmesg error check
#   G1      python3 check_inflight.py --min-inflight 2 (shared, per-case)
#   G2/G3/G4 own computation in tests/q_sp_edf_gates.py: the shared
#           compare_expect.py dispatch_with_choice field is dead (never
#           incremented) and INSERT tracepoints never fire for scx_io-managed
#           requests (blk-mq.c elevator insert path has no
#           trace_block_rq_insert), so G2 uses the ISSUE/COMPLETE
#           inserted-but-not-completed proxy and G4 uses window issue_count
#   verdict compare_expect.py exit status (0=PASS 1=FAIL 2=BLOCKED), case
#           PASS only when G1-G4 AND compare all pass
#
# 5 attempts per case with fs_cleanup_ioext between attempts (mirror
# run_dt_mqdd_only.sh / fs_run_case_with_retry). FAIL is recorded honestly
# with root-cause evidence under results/q_sp_edf_accept_<ts>/.
#
# Run inside the virtme-ng VM (root):
#   TARGET_DEVICE=vda IOEXT_DESTRUCTIVE_TEST=1 tests/q_sp_edf_accept.sh
# Exit: 0 = all cases PASS, 1 = any FAIL/BLOCKED
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FS_DIR="$BASE_DIR/tests/full_suite"
DT_DIR="$FS_DIR/deterministic"
export TARGET_DEVICE="${TARGET_DEVICE:-vda}"
export IOEXT_DESTRUCTIVE_TEST=1
export POLICY_OVERRIDE=q_sp_edf

source "$FS_DIR/lib_test.sh"
set +e

MODEL="$BASE_DIR/tests/q_sp_edf_model.py"
GATES_PY="$BASE_DIR/tests/q_sp_edf_gates.py"
TRACE_DRIVER="$DT_DIR/trace_driver"
BPFTRACE_SCRIPT="$DT_DIR/bpftrace/trace_dispatch.bt"
COMPARE_PY="$DT_DIR/compare_expect.py"
CHECK_INFLIGHT_PY="$DT_DIR/check_inflight.py"
LOADER="$FS_LOADER_DIR/loader_q_sp_edf"
BT="$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
ROOT_CG_ID="$(stat -c %i /sys/fs/cgroup 2>/dev/null || echo 0)"

RESULTS_DIR="${RESULTS_DIR:-$BASE_DIR/results/q_sp_edf_accept_$(date +%Y%m%d_%H%M%S)}"
R_FILE="$RESULTS_DIR/results.txt"
mkdir -p "$RESULTS_DIR"
: > "$R_FILE"

PASS=0; FAIL=0; BLOCKED=0
declare -a CASE_SUMMARY

log()      { printf '[q_sp_edf_accept] %s\n' "$*" >&2; echo "[q_sp_edf_accept] $*" >> "$R_FILE"; }
pass()     { log "PASS: $*"; PASS=$((PASS+1)); }
fail()     { log "FAIL: $*"; FAIL=$((FAIL+1)); }
blocked()  { log "BLOCKED: $*"; BLOCKED=$((BLOCKED+1)); }

final_summary() {
    {
        echo ""
        echo "## Summary"
        echo "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED"
        echo "Date: $(date)"
        echo "RESULTS_DIR=$RESULTS_DIR"
    } >> "$R_FILE"
    tail -6 "$R_FILE"
}
trap final_summary EXIT

log "=== q_sp_edf acceptance START ==="
log "BASE_DIR=$BASE_DIR device=${TARGET_DEVICE} results=$RESULTS_DIR"
log "  rig: nr_requests=$(cat /sys/block/$TARGET_DEVICE/queue/nr_requests 2>/dev/null) tags=$(cat /sys/block/$TARGET_DEVICE/mq/0/nr_tags 2>/dev/null)"

# ---------------------------------------------------------------- gate 0
log "--- gate 0: reference model self-test ---"
if timeout 120 python3 "$MODEL" > "$RESULTS_DIR/model.txt" 2>&1; then
    pass "model_self_test"
else
    fail "model_self_test" "$(tail -2 "$RESULTS_DIR/model.txt" | tr '\n' ' ')"
    log "=== q_sp_edf acceptance END: FAIL (model) ==="
    exit 1
fi

# ---------------------------------------------------------------- env
fs_require_vm
[ -x "$LOADER" ]          || { blocked "env" "missing loader: $LOADER"; exit 1; }
[ -f "$BPFTRACE_SCRIPT" ] || { blocked "env" "missing bpftrace script"; exit 1; }
command -v bpftrace >/dev/null 2>&1 || { blocked "env" "bpftrace not installed"; exit 1; }
if [ ! -x "$TRACE_DRIVER" ]; then
    TRACE_DRIVER="/tmp/opencode/q_sp_edf_trace_driver"
    if [ ! -x "$TRACE_DRIVER" ]; then
        gcc -O2 -o "$TRACE_DRIVER" "$DT_DIR/trace_driver.c" 2>>"$RESULTS_DIR/env.txt" \
            || { blocked "env" "trace_driver build failed"; exit 1; }
        log "trace_driver built to $TRACE_DRIVER (shared binary missing)"
    fi
fi
fs_detect_device
fs_require_destructive_guard
fs_ensure_bpffs
fs_setup_cgroup_io
fs_cleanup_ioext
log "env OK: device=$FS_DEV_PATH loader=$LOADER trace_driver=$TRACE_DRIVER"

# trace_driver args: mirror run_deterministic.sh trace_driver_args (tier>=2
# honors the trace JSON submit_mode/batch_size; tier<2 stays sync)
trace_driver_args() {
    local trace_file="$1" tier="$2"
    if [ "$tier" -ge 2 ]; then
        local mode batch
        mode=$(python3 -c "import json;d=json.load(open('$trace_file'));print(d.get('defaults',{}).get('submit_mode','sync'))" 2>/dev/null || echo sync)
        if [ "$mode" = "async_batch" ] || [ "$mode" = "async" ]; then
            batch=$(python3 -c "import json;d=json.load(open('$trace_file'));print(d.get('defaults',{}).get('batch_size',8))" 2>/dev/null || echo 8)
            echo "--mode async --batch $batch"
            return
        fi
    fi
    echo ""
}

wait_policy_registered() {
    # CONFIG_IOSCHED_EXT_DEBUG is not set in this kernel, so the
    # "policy 'q_sp_edf' attached" dmesg line is no_printk and never appears
    # (iosched_ext.c:31-37); fs_wait_attached's dmesg grep therefore always
    # misses. The strongest attach evidence is the cgroup entry appearing in
    # the pinned policy_meta map (same probe arb_wdrr_accept.sh uses).
    local cg_id="$1" timeout="$2" i
    for ((i = 1; i <= timeout; i++)); do
        if ! fs_loader_alive; then return 1; fi
        if "$BT" -j map dump pinned /sys/fs/bpf/ioext_policy_meta \
                > "$RESULTS_DIR/.meta_probe.json" 2>/dev/null; then
            if [ "$(python3 -c "
import json
try: entries = json.load(open('$RESULTS_DIR/.meta_probe.json'))
except Exception: entries = []
ids = set()
for e in entries:
    k = e.get('key')
    if isinstance(k, dict):
        ids.add(k.get('cgroup_id'))
    elif isinstance(k, list):
        kid = 0
        for i, b in enumerate(k[:8]):
            kid |= int(b, 0) << (8 * i)
        ids.add(kid)
print(1 if $cg_id in ids else 0)
" 2>/dev/null)" = "1" ]; then
                return 0
            fi
        fi
        sleep 1
    done
    return 1
}

# run_one <case_id> <trace> <expect> <tier> <attempt>
# returns 0 = case PASS; writes per-attempt evidence into
# $RESULTS_DIR/<case_id>/attempt_<n>/ and appends the verdict to the case's
# attempts.jsonl
run_one() {
    local case_id="$1" trace_file="$2" expect_file="$3" tier="$4" attempt="$5"
    local case_dir="$RESULTS_DIR/$case_id"
    local att_dir="$case_dir/attempt_$attempt"
    mkdir -p "$att_dir"
    local loader_log="$att_dir/loader.log"
    local bpftrace_log="$att_dir/bpftrace.log"
    local trace_out="$att_dir/trace_output.txt"
    local compare_out="$att_dir/compare.json"
    local inflight_out="$att_dir/inflight.json"
    local gates_out="$att_dir/gates.json"
    local verdict_out="$att_dir/verdict.json"
    local reasons=()

    local mark
    mark="$(fs_dmesg_mark "spedf_${case_id}_attempt${attempt}")"

    fs_set_scx_scheduler || { reasons+=("scx_io switch failed"); }

    if [ ${#reasons[@]} -eq 0 ]; then
        # elevator pipeline must exceed device tags, otherwise blk-mq drains
        # the elevator straight into the device (see arb_wdrr_accept.sh
        # set_scx_io): requests must be allocated on sched tags
        # (RQF_USE_SCHED) so they go through the scx_io insert hook
        echo 1023 > "/sys/block/$FS_DEVICE/queue/nr_requests" 2>/dev/null || \
            reasons+=("nr_requests=1023 failed")
    fi

    if [ ${#reasons[@]} -eq 0 ]; then
        fs_start_loader q_sp_edf "$loader_log"
        local attach_ok=0
        if [ -x "$BT" ]; then
            wait_policy_registered "$ROOT_CG_ID" 15 && attach_ok=1
        else
            fs_wait_attached q_sp_edf "$FS_LOADER_PID" 15 && attach_ok=1
        fi
        if [ "$attach_ok" != "1" ]; then
            reasons+=("policy not registered in policy_meta in 15s (see loader.log)")
            cat "$loader_log" > "$att_dir/attach_failure_loader.log" 2>/dev/null
            fs_stop_loader "$FS_LOADER_PID"
        fi
    fi

    local bpf_pid=""
    if [ ${#reasons[@]} -eq 0 ]; then
        bpftrace "$BPFTRACE_SCRIPT" > "$bpftrace_log" 2>&1 &
        bpf_pid=$!
        # attach-wait (Phase-3 port): cold VM 首次 bpftrace attach 可 >1s,
        # 不等待则 trace 在探针就绪前播完 (T1 attempt-1 FAIL 的根因)。
        local aw=0
        until grep -q 'Attaching' "$bpftrace_log" 2>/dev/null; do
            sleep 0.2
            aw=$((aw + 1))
            [ "$aw" -ge 30 ] && { log "bpftrace attach-wait timeout"; break; }
        done
        sleep 1

        local driver_args
        driver_args="$(trace_driver_args "$trace_file" "$tier")"
        log "[$case_id attempt $attempt] trace_driver args: ${driver_args:-<sync>}"
        if [ -n "$driver_args" ]; then
            "$TRACE_DRIVER" "$FS_DEV_PATH" "$trace_file" $driver_args > "$trace_out" 2>&1
        else
            "$TRACE_DRIVER" "$FS_DEV_PATH" "$trace_file" > "$trace_out" 2>&1
        fi
        local trace_rc=$?
        [ "$trace_rc" -ne 0 ] && reasons+=("trace_driver exited $trace_rc")

        sleep 1
        kill -INT "$bpf_pid" 2>/dev/null
        wait "$bpf_pid" 2>/dev/null
        bpf_pid=""
        grep -E 'TRACE_PLAY_(START|END)' "$trace_out" >> "$bpftrace_log" 2>/dev/null

        fs_stop_loader "$FS_LOADER_PID"

        if fs_dmesg_has_errors "$mark"; then
            fs_dmesg_snapshot "$mark" > "$att_dir/dmesg_errors.txt" 2>/dev/null
            reasons+=("dmesg errors since case mark (see dmesg_errors.txt)")
        fi
        if [ ! -s "$bpftrace_log" ]; then
            reasons+=("bpftrace log empty/missing")
        fi
    fi

    local g1_pass=false g1_detail=""
    local gates_pass=true gates_detail=""
    local compare_status="NOT_RUN"
    local compare_exit=-1

    if [ ${#reasons[@]} -eq 0 ] && [ "$tier" -ge 2 ]; then
        # G1: scheduler actually held >=2 candidates (shared gate)
        if python3 "$CHECK_INFLIGHT_PY" --bpftrace-log "$bpftrace_log" \
                --trace "$trace_file" \
                --out "$inflight_out" --min-inflight 2 > "$att_dir/inflight_stdout.txt" 2>&1; then
            g1_pass=true
            g1_detail="$(python3 -c "import json;d=json.load(open('$inflight_out'));print('max_inflight=%s'%d.get('max_inflight'))" 2>/dev/null)"
        else
            g1_pass=false
            g1_detail="$(python3 -c "import json;d=json.load(open('$inflight_out'));print('max_inflight=%s reason=%s'%(d.get('max_inflight'),d.get('reason',''))" 2>/dev/null)"
            reasons+=("G1 FAIL scheduler_inactive ($g1_detail)")
        fi
        # G2/G3/G4: own computation (see q_sp_edf_gates.py docstring)
        if python3 "$GATES_PY" --bpftrace-log "$bpftrace_log" \
                --trace "$trace_file" --out "$gates_out" > "$att_dir/gates_stdout.txt" 2>&1; then
            gates_pass=true
        else
            gates_pass=false
            gates_detail="$(python3 -c "import json;d=json.load(open('$gates_out'));errs=[k for k,v in d.items() if k.endswith('_pass') and not v];print('failed gates: %s; g3_errors=%s'%(errs,d.get('g3_errors')))" 2>/dev/null)"
            reasons+=("G2/G3/G4 FAIL ($gates_detail)")
        fi
    fi

    if [ -s "$bpftrace_log" ]; then
        python3 "$COMPARE_PY" --bpftrace-log "$bpftrace_log" \
            --expect "$expect_file" --trace "$trace_file" \
            --policy q_sp_edf --tier "$tier" \
            --out "$compare_out" > "$att_dir/compare_stdout.txt" 2>&1
        compare_exit=$?
        compare_status="$(python3 -c "import json;print(json.load(open('$compare_out')).get('status','FAIL'))" 2>/dev/null || echo FAIL)"
        if [ "$compare_status" != "PASS" ]; then
            reasons+=("compare status=$compare_status: $(python3 -c "import json;print(json.load(open('$compare_out')).get('errors',[]))" 2>/dev/null)")
        fi
    fi

    local verdict="FAIL"
    [ ${#reasons[@]} -eq 0 ] && verdict="PASS"
    python3 - "$case_id" "$attempt" "$verdict" "$g1_pass" "$gates_pass" \
            "$compare_status" "$compare_exit" "$att_dir" "${reasons[*]-}" <<'PYEOF'
import json, sys
case_id, attempt, verdict, g1, gates, cstatus, cexit, att, reasons = sys.argv[1:10]
v = {
    "case_id": case_id, "attempt": int(attempt), "verdict": verdict,
    "g1_pass": g1 == "true", "g2_g3_g4_pass": gates == "true",
    "compare_status": cstatus, "compare_exit": int(cexit),
    "reasons": reasons.split("; ") if reasons else [],
    "evidence_dir": att,
}
json.dump(v, open(f"{att}/verdict.json", "w"), indent=2)
with open(f"{att}/../attempts.jsonl", "a") as f:
    f.write(json.dumps(v) + "\n")
PYEOF
    log "[$case_id attempt $attempt] verdict=$verdict reasons=[${reasons[*]-}] g1=$g1_pass gates=$gates_pass compare=$compare_status"
    [ "$verdict" = "PASS" ]
}

# case_id|trace|expect|tier  (DT_CASES mapping preserved:
#   q_sp_edf-DT-M01=spedf_deadline_5  M02=spedf_tie_prio_5  M03=spedf_large_small_8)
CASES=(
    "spedf_t1_counts|$DT_DIR/traces/micro/spedf_deadline_5.json|$DT_DIR/expect/partial/spedf_t1_counts.json|1"
    "spedf_m01_deadline_5|$DT_DIR/traces/micro/spedf_deadline_5.json|$DT_DIR/expect/golden/spedf_deadline_5.json|2"
    "spedf_m02_tie_prio_5|$DT_DIR/traces/micro/spedf_tie_prio_5.json|$DT_DIR/expect/golden/spedf_tie_prio_5.json|2"
    "spedf_m03_large_small_8|$DT_DIR/traces/micro/spedf_large_small_8.json|$DT_DIR/expect/golden/spedf_large_small_8.json|2"
    "spedf_m04_burst_18|$DT_DIR/traces/micro/spedf_burst_18.json|$DT_DIR/expect/golden/spedf_burst_18.json|2"
)

MAX_ATTEMPTS=5
for entry in "${CASES[@]}"; do
    IFS='|' read -r case_id trace expect tier <<< "$entry"
    log "=== case $case_id (tier $tier) ==="
    if [ ! -f "$trace" ]; then
        blocked "$case_id" "trace missing: $trace"
        CASE_SUMMARY+=("$case_id|BLOCKED|trace_missing")
        continue
    fi
    if [ ! -f "$expect" ]; then
        blocked "$case_id" "expect missing: $expect"
        CASE_SUMMARY+=("$case_id|BLOCKED|expect_missing")
        continue
    fi

    got_pass=0
    last_reasons=""
    semantic_seen=0
    for attempt in 1 2 3 4 5; do
        if run_one "$case_id" "$trace" "$expect" "$tier" "$attempt"; then
            pass "$case_id (attempt=$attempt)"
            CASE_SUMMARY+=("$case_id|PASS|attempt=$attempt")
            got_pass=1
            break
        fi
        # a compare.json on this attempt means the case reached semantic
        # assertion (FAIL-class, not env BLOCKED-class)
        [ -f "$RESULTS_DIR/$case_id/attempt_$attempt/compare.json" ] && semantic_seen=1
        last_reasons="$(python3 -c "import json;print('; '.join(json.load(open('$RESULTS_DIR/$case_id/attempt_$attempt/verdict.json'))['reasons']))" 2>/dev/null)"
        fs_cleanup_ioext
        sleep 1
    done

    if [ "$got_pass" != "1" ]; then
        if [ "$semantic_seen" = "1" ]; then
            fail "$case_id" "5 attempts, last: $last_reasons"
            CASE_SUMMARY+=("$case_id|FAIL|$last_reasons")
        else
            blocked "$case_id" "5 attempts, never reached assertion, last: $last_reasons"
            CASE_SUMMARY+=("$case_id|BLOCKED|$last_reasons")
        fi
    fi
    fs_cleanup_ioext
    sleep 1
done

fs_cleanup_ioext
log "=== q_sp_edf acceptance END ==="

{
    echo "{"
    printf '"cases": [\n'
    first=1
    for e in "${CASE_SUMMARY[@]}"; do
        IFS='|' read -r cid st detail <<< "$e"
        [ $first = 0 ] && printf ',\n'
        printf '  {"case_id": "%s", "status": "%s", "detail": "%s"}' "$cid" "$st" "${detail//\"/\\\"}"
        first=0
    done
    printf '\n],\n'
    printf '"pass": %d, "fail": %d, "blocked": %d}\n' "$PASS" "$FAIL" "$BLOCKED"
} > "$RESULTS_DIR/summary.json"

if [ "$FAIL" -gt 0 ] || [ "$BLOCKED" -gt 0 ]; then
    exit 1
fi
exit 0