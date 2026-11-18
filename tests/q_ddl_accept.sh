#!/bin/bash
# tests/q_ddl_accept.sh — q_ddl closed-loop acceptance test (VM-side).
#
# Mirrors tests/q_sp_edf_accept.sh (the proven Phase-1 triad structure),
# whose per-case flow mirrors tests/full_suite/deterministic/
# run_dt_mqdd_only.sh run_one:
#   gate 0  reference model self-test (tests/q_ddl_model.py), like
#           tests/arb_wdrr_accept.sh gate 0
#   per-case: fs_set_scx_scheduler + nr_requests=1023 (elevator pipeline
#           must exceed device tags), fs_start_loader/fs_wait_attached —
#           attach probed via the pinned ioext_policy_meta map
#           (wait_policy_registered, ported from q_sp_edf_accept.sh;
#           CONFIG_IOSCHED_EXT_DEBUG is off so the dmesg attached line is
#           no_printk), bpftrace capture with the 'Attaching' attach-wait
#           loop (cold VM first attach can take >1s — proven flake source),
#           trace_driver async batch, TRACE_PLAY marker grep, loader stop,
#           dmesg error check
#   G1      python3 check_inflight.py --min-inflight 2 (shared, per-case)
#   G2/G3/G4 own computation in tests/q_ddl_gates.py (dual-method G2,
#           window purity, coverage — see its docstring)
#   verdict compare_expect.py exit status (0=PASS 1=FAIL 2=BLOCKED), case
#           PASS only when G1-G4 AND compare all pass
#   counter_evidence step: a copy of the M01 golden mutated (first two
#           expected entries swapped — inverts the BULK lane arrival order)
#           must FAIL (exit 1) against the captured passing-attempt log;
#           preserved under $RESULTS_DIR/counter_evidence/
#   bypass_gap package: per-attempt policy_meta probe (dispatches==0 after
#           fully-ISSUEd traces == every request bypassed q_ddl, because
#           note_dispatch fires only on the accept path) + code-proof that
#           state_map is an orphaned lookup-only HASH map. M02
#           (ddl_cost_class_5) is the registered bypass detector: its
#           golden is the only one that differs from submission order, so
#           under the bypass bug it FAILs while M01/M03 pass vacuously.
#
# q_ddl rig notes: loader_q_ddl takes NO tunables (loader_q_ddl.c:16-17,
# common args only) and q_ddl has NO stats map — no loader args, no stats
# dumps. All burst/expire constants are BPF-builtins (q_ddl.bpf.c:29-35).
#
# 5 attempts per case with fs_cleanup_ioext between attempts (mirror
# run_dt_mqdd_only.sh / fs_run_case_with_retry). FAIL is recorded honestly
# with root-cause evidence under results/q_ddl_accept_<ts>/.
#
# Run inside the virtme-ng VM (root):
#   TARGET_DEVICE=vda IOEXT_DESTRUCTIVE_TEST=1 tests/q_ddl_accept.sh
# Exit: 0 = all cases PASS, 1 = any FAIL/BLOCKED
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FS_DIR="$BASE_DIR/tests/full_suite"
DT_DIR="$FS_DIR/deterministic"
export TARGET_DEVICE="${TARGET_DEVICE:-vda}"
export IOEXT_DESTRUCTIVE_TEST=1
export POLICY_OVERRIDE=q_ddl

source "$FS_DIR/lib_test.sh"
set +e

MODEL="$BASE_DIR/tests/q_ddl_model.py"
GATES_PY="$BASE_DIR/tests/q_ddl_gates.py"
TRACE_DRIVER="$DT_DIR/trace_driver"
BPFTRACE_SCRIPT="$DT_DIR/bpftrace/trace_dispatch.bt"
COMPARE_PY="$DT_DIR/compare_expect.py"
CHECK_INFLIGHT_PY="$DT_DIR/check_inflight.py"
LOADER="$FS_LOADER_DIR/loader_q_ddl"
BT="$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
ROOT_CG_ID="$(stat -c %i /sys/fs/cgroup 2>/dev/null || echo 0)"

RESULTS_DIR="${RESULTS_DIR:-$BASE_DIR/results/q_ddl_accept_$(date +%Y%m%d_%H%M%S)}"
R_FILE="$RESULTS_DIR/results.txt"
mkdir -p "$RESULTS_DIR"
: > "$R_FILE"

PASS=0; FAIL=0; BLOCKED=0
declare -a CASE_SUMMARY

log()      { printf '[q_ddl_accept] %s\n' "$*" >&2; echo "[q_ddl_accept] $*" >> "$R_FILE"; }
pass()     { log "PASS: $*"; PASS=$((PASS+1)); }
fail()     { log "FAIL: $*"; FAIL=$((FAIL+1)); }
blocked()  { log "BLOCKED: $*"; BLOCKED=$((BLOCK+1)); }

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

log "=== q_ddl acceptance START ==="
log "BASE_DIR=$BASE_DIR device=${TARGET_DEVICE} results=$RESULTS_DIR"
log "  rig: nr_requests=$(cat /sys/block/$TARGET_DEVICE/queue/nr_requests 2>/dev/null) tags=$(cat /sys/block/$TARGET_DEVICE/mq/0/nr_tags 2>/dev/null)"

# ---------------------------------------------------------------- gate 0
log "--- gate 0: reference model self-test ---"
if timeout 120 python3 "$MODEL" > "$RESULTS_DIR/model.txt" 2>&1; then
    pass "model_self_test"
else
    fail "model_self_test: $(tail -2 "$RESULTS_DIR/model.txt" | tr '\n' ' ')"
    log "=== q_ddl acceptance END: FAIL (model) ==="
    exit 1
fi

# ---------------------------------------------------------------- env
fs_require_vm
[ -x "$LOADER" ]          || { blocked "env: missing loader: $LOADER"; exit 1; }
[ -f "$BPFTRACE_SCRIPT" ] || { blocked "env: missing bpftrace script"; exit 1; }
command -v bpftrace >/dev/null 2>&1 || { blocked "env: bpftrace not installed"; exit 1; }
if [ ! -x "$TRACE_DRIVER" ]; then
    TRACE_DRIVER="/tmp/opencode/q_ddl_trace_driver"
    if [ ! -x "$TRACE_DRIVER" ]; then
        gcc -O2 -o "$TRACE_DRIVER" "$DT_DIR/trace_driver.c" 2>>"$RESULTS_DIR/env.txt" \
            || { blocked "env: trace_driver build failed"; exit 1; }
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
    # "policy 'q_ddl' attached" dmesg line is no_printk and never appears
    # (iosched_ext.c:31-37); fs_wait_attached's dmesg grep therefore always
    # misses. The strongest attach evidence is the cgroup entry appearing in
    # the pinned policy_meta map (probe ported from q_sp_edf_accept.sh).
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
    local meta_dispatches="-" meta_qdepth="-"
    local reasons=()

    local mark
    mark="$(fs_dmesg_mark "qddl_${case_id}_attempt${attempt}")"

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
        # loader_q_ddl: NO tunables (loader_q_ddl.c:16-17 common args only)
        fs_start_loader q_ddl "$loader_log"
        local attach_ok=0
        if [ -x "$BT" ]; then
            wait_policy_registered "$ROOT_CG_ID" 15 && attach_ok=1
        else
            fs_wait_attached q_ddl "$FS_LOADER_PID" 15 && attach_ok=1
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
        # attach-wait (ported from run_dt_mqdd_only.sh / q_sp_edf_accept.sh):
        # cold VM first bpftrace attach can take >1s; without waiting, the
        # trace plays before the probes are ready (proven flake source).
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

        # bypass probe (loader still alive, maps pinned): policy_meta
        # dispatches/queue_depth after the trace. note_dispatch fires ONLY
        # on the accept path (q_ddl.bpf.c:310-311), so dispatches==0 after
        # n_reqs issued requests is direct evidence that insert_request
        # returned -1 (bypass) for all of them. Recorded per attempt in
        # verdict.json; the bypass_gap package aggregates it.
        local meta_dispatches="-" meta_qdepth="-"
        if [ -x "$BT" ] && "$BT" -j map dump pinned /sys/fs/bpf/ioext_policy_meta \
                > "$att_dir/policy_meta_post_trace.json" 2>/dev/null; then
            # decode struct iosched_policy_meta (ioext_policy_shared.h:97-106):
            # u64 queue_depth@0, u64 dispatches@8, ... — bpftool -j emits the
            # value as a little-endian hex-byte list
            meta_dispatches="$(python3 -c "
import json
try: entries = json.load(open('$att_dir/policy_meta_post_trace.json'))
except Exception: entries = []
vals = {}
for e in entries if isinstance(entries, list) else []:
    v = e.get('value', {})
    if isinstance(v, dict):
        vals.update(v)
    elif isinstance(v, list):
        def le64(off):
            b = [int(x, 0) for x in v[off:off + 8]]
            return sum(x << (8 * i) for i, x in enumerate(b))
        vals['queue_depth'] = le64(0)
        vals['dispatches'] = le64(8)
print(vals.get('dispatches', 'unparsed'))
" 2>/dev/null)"
            meta_qdepth="$(python3 -c "
import json
try: entries = json.load(open('$att_dir/policy_meta_post_trace.json'))
except Exception: entries = []
vals = {}
for e in entries if isinstance(entries, list) else []:
    v = e.get('value', {})
    if isinstance(v, dict):
        vals.update(v)
    elif isinstance(v, list):
        def le64(off):
            b = [int(x, 0) for x in v[off:off + 8]]
            return sum(x << (8 * i) for i, x in enumerate(b))
        vals['queue_depth'] = le64(0)
        vals['dispatches'] = le64(8)
print(vals.get('queue_depth', 'unparsed'))
" 2>/dev/null)"
            log "[$case_id attempt $attempt] policy_meta post-trace: dispatches=$meta_dispatches queue_depth=$meta_qdepth"
        fi

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
        # G1: scheduler actually held >=2 candidates (shared gate; exit 0
        # needs G1+G2, post Phase-2 dispatch_with_choice is INSERT-based)
        if python3 "$CHECK_INFLIGHT_PY" --bpftrace-log "$bpftrace_log" \
                --trace "$trace_file" \
                --out "$inflight_out" --min-inflight 2 > "$att_dir/inflight_stdout.txt" 2>&1; then
            g1_pass=true
            g1_detail="$(python3 -c "import json;d=json.load(open('$inflight_out'));print('max_inflight=%s dwc=%s'%(d.get('max_inflight'),d.get('dispatch_with_choice')))" 2>/dev/null)"
        else
            g1_pass=false
            g1_detail="$(python3 -c "import json;d=json.load(open('$inflight_out'));print('max_inflight=%s reason=%s'%(d.get('max_inflight'),d.get('reason','')))" 2>/dev/null)"
            reasons+=("G1 FAIL scheduler_inactive ($g1_detail)")
        fi
        # G2/G3/G4: own computation (see q_ddl_gates.py docstring)
        if python3 "$GATES_PY" --bpftrace-log "$bpftrace_log" \
                --trace "$trace_file" --out "$gates_out" > "$att_dir/gates_stdout.txt" 2>&1; then
            gates_pass=true
            gates_detail="$(python3 -c "import json;d=json.load(open('$gates_out'));print('g2=%s(%s) g3=%s g4=%s ins=%s iss=%s'%(d['g2_pass'],d['g2_method'],d['g3_pass'],d['g4_pass'],d['window_insert_count'],d['window_issue_count']))" 2>/dev/null)"
        else
            gates_pass=false
            gates_detail="$(python3 -c "import json;d=json.load(open('$gates_out'));errs=[k for k,v in d.items() if k.endswith('_pass') and not v];print('failed gates: %s; g3_errors=%s'%(errs,d.get('g3_errors')))" 2>/dev/null)"
            reasons+=("G2/G3/G4 FAIL ($gates_detail)")
        fi
    fi

    if [ -s "$bpftrace_log" ]; then
        python3 "$COMPARE_PY" --bpftrace-log "$bpftrace_log" \
            --expect "$expect_file" --trace "$trace_file" \
            --policy q_ddl --tier "$tier" \
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
            "$compare_status" "$compare_exit" "$att_dir" "${reasons[*]-}" \
            "$meta_dispatches" "$meta_qdepth" <<'PYEOF'
import json, sys
(case_id, attempt, verdict, g1, gates, cstatus, cexit, att, reasons,
 meta_dispatches, meta_qdepth) = sys.argv[1:13]
v = {
    "case_id": case_id, "attempt": int(attempt), "verdict": verdict,
    "g1_pass": g1 == "true", "g2_g3_g4_pass": gates == "true",
    "compare_status": cstatus, "compare_exit": int(cexit),
    "reasons": reasons.split("; ") if reasons else [],
    "policy_meta_dispatches": meta_dispatches,
    "policy_meta_queue_depth": meta_qdepth,
    "evidence_dir": att,
}
json.dump(v, open(f"{att}/verdict.json", "w"), indent=2)
with open(f"{att}/../attempts.jsonl", "a") as f:
    f.write(json.dumps(v) + "\n")
PYEOF
    log "[$case_id attempt $attempt] verdict=$verdict reasons=[${reasons[*]-}] g1=($g1_detail) gates=($gates_detail) compare=$compare_status"
    [ "$verdict" = "PASS" ]
}

# case_id|trace|expect|tier  (loader_q_ddl takes NO tunables — no extra args
# exist to pass; all q_ddl constants are BPF-builtins, q_ddl.bpf.c:29-35)
CASES=(
    "ddl_t1_counts|$DT_DIR/traces/micro/ddl_bulk_first_5.json|$DT_DIR/expect/partial/ddl_t1_counts.json|1"
    "ddl_m01_bulk_first_5|$DT_DIR/traces/micro/ddl_bulk_first_5.json|$DT_DIR/expect/golden/ddl_bulk_first_5.json|2"
    "ddl_m02_cost_class_5|$DT_DIR/traces/micro/ddl_cost_class_5.json|$DT_DIR/expect/golden/ddl_cost_class_5.json|2"
    "ddl_m03_lat_fifo_desc_5|$DT_DIR/traces/micro/ddl_lat_fifo_desc_5.json|$DT_DIR/expect/golden/ddl_lat_fifo_desc_5.json|2"
)

MAX_ATTEMPTS=5
for entry in "${CASES[@]}"; do
    IFS='|' read -r case_id trace expect tier <<< "$entry"
    log "=== case $case_id (tier $tier) ==="
    if [ ! -f "$trace" ]; then
        blocked "$case_id: trace missing: $trace"
        CASE_SUMMARY+=("$case_id|BLOCKED|trace_missing")
        continue
    fi
    if [ ! -f "$expect" ]; then
        blocked "$case_id: expect missing: $expect"
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
            fail "$case_id: 5 attempts, last: $last_reasons"
            CASE_SUMMARY+=("$case_id|FAIL|$last_reasons")
        else
            blocked "$case_id: 5 attempts, never reached assertion, last: $last_reasons"
            CASE_SUMMARY+=("$case_id|BLOCKED|$last_reasons")
        fi
    fi
    fs_cleanup_ioext
    sleep 1
done

# ------------------------------------------- bypass-gap evidence package
# VM finding (2026-09-01, first q_ddl DT run): M02's golden (BULK lane
# before LAT lane for interleaved arrivals) FAILed 5/5 with the exact
# submission order observed, while M01/M03 (whose goldens coincide with
# submission order) passed. Root cause chain, recorded here per playbook
# §12 discipline (never launder an oracle to match a broken policy):
log "--- bypass-gap evidence package ---"
BG="$RESULTS_DIR/bypass_gap"
mkdir -p "$BG"
{
    echo "== grep -n 'bpf_map_update_elem' userspace/q_ddl.bpf.c =="
    grep -n "bpf_map_update_elem" "$BASE_DIR/userspace/q_ddl.bpf.c"
    rc=$?
    [ "$rc" -ne 0 ] && echo "(no matches — q_ddl NEVER creates state_map entries; ddl_state_lookup is lookup-only at :88-97)"
    echo
    echo "== state_map declaration (q_ddl.bpf.c:79-84) — HASH keyed by (cgroup_id,hctx_idx) =="
    sed -n '79,84p' "$BASE_DIR/userspace/q_ddl.bpf.c"
    echo
    echo "== working siblings use auto-zero ARRAY state maps (lookups cannot miss): =="
    grep -n -A1 "__uint(type, BPF_MAP_TYPE_ARRAY)" \
        "$BASE_DIR/userspace/q_sp_edf.bpf.c" | head -3
    grep -n -A1 "__uint(type, BPF_MAP_TYPE_ARRAY)" \
        "$BASE_DIR/userspace/q_wedf.bpf.c" | head -3
    echo
    echo "== insert_request lookup-miss path (q_ddl.bpf.c:260-264) returns -1 =="
    sed -n '260,264p' "$BASE_DIR/userspace/q_ddl.bpf.c"
    echo
    echo "Consequence: ddl_state_lookup ALWAYS returns NULL (hash map, zero"
    echo "entries, no creator) -> insert_request returns -1 for EVERY request"
    echo "-> kernel bypasses q_ddl entirely -> the LAT/BULK lanes and the"
    echo "5-rule precedence are never exercised. The 2026-08-19 policy_smoke"
    echo "PASS only proved attach + IO liveness (bypass passes IO through)."
    echo
    echo "Runtime proof: policy_meta.dispatches==0 after each trace although"
    echo "all requests ISSUEd (note_dispatch fires only on the accept path,"
    echo "q_ddl.bpf.c:310-311). See dispatch_probe.txt and per-attempt"
    echo "policy_meta_post_trace.json / verdict.json."
} > "$BG/code_state_map_orphaned.txt" 2>&1

{
    echo "per-attempt policy_meta dispatches/queue_depth (post-trace, loader alive):"
    for att in "$RESULTS_DIR"/*/attempt_*/verdict.json; do
        [ -f "$att" ] || continue
        python3 -c "
import json
v = json.load(open('$att'))
print('  %s a%s: verdict=%s compare=%s meta_dispatches=%s meta_queue_depth=%s' % (
    v['case_id'], v['attempt'], v['verdict'], v['compare_status'],
    v.get('policy_meta_dispatches'), v.get('policy_meta_queue_depth')))
" 2>/dev/null
    done
    echo
    echo "A WORKING policy shows dispatches>=n_reqs (5) after each trace;"
    echo "q_ddl shows 0 == every request bypassed."
} > "$BG/dispatch_probe.txt" 2>&1

{
    echo "bypass_gap verdict: q_ddl structurally bypasses ALL requests as built."
    echo "M02 (ddl_cost_class_5) is the ONLY registered case whose golden"
    echo "differs from submission order, so it is the only one that can"
    echo "detect the bypass; its FAIL 5/5 (identical order every attempt) is"
    echo "the finding, not a flake. M01/M03 mechanical PASSes are vacuous as"
    echo "lane-semantics proofs under this root cause (their goldens coincide"
    echo "with submission order) — they retain anti-C-LOOK discriminating"
    echo "power against a WORKING scheduler only."
    echo
    echo "Proposed fix (out of this harness's ownership; needs q_ddl.bpf.c edit +"
    echo "loader rebuild via ./scripts/make.sh q_ddl — no builds allowed here):"
    echo "  Option 1 (mirror the working siblings, minimal): state_map becomes"
    echo "    BPF_MAP_TYPE_ARRAY keyed by __u32 hctx_idx (max_entries 64),"
    echo "    ddl_state_lookup drops the cgroup_id component — array lookups"
    echo "    auto-return zero-initialized state, no creator needed."
    echo "  Option 2 (preserve POL-08 per-cgroup isolation): keep the HASH map"
    echo "    but seed entries from the LOADER (bpf_map_update_elem from"
    echo "    userspace is legal for spin-lock-valued maps; from BPF progs it"
    echo "    is verifier-rejected) for (cgroup_id, hctx 0..nr_hw_queues)."
    echo "After the fix the SAME goldens/ci.sh must go green with zero"
    echo "harness changes (they are the model-derived intended oracle)."
} > "$BG/verdict_and_fix.txt"

record_bypass_blocked() {
    log "BYPASS-GAP (expected until q_ddl.bpf.c is fixed): $*"
    echo "BYPASS-GAP (expected until q_ddl.bpf.c is fixed): $*" >> "$R_FILE"
}
if grep -q "FAIL" "$R_FILE" 2>/dev/null; then
    record_bypass_blocked "M02-class FAILs recorded — see $BG/"
fi

# ---------------------------------------------------- counter evidence
# Mutate a copy of the M01 golden (swap the first two expected entries —
# inverts the BULK lane arrival FIFO) and prove compare_expect.py FAILs
# (exit 1) against the captured passing-attempt log. A compare that cannot
# fail is worthless as an oracle (playbook §12: 反证真跑).
log "--- counter evidence: mutated M01 golden must FAIL ---"
CE_DIR="$RESULTS_DIR/counter_evidence"
mkdir -p "$CE_DIR"
ce_done=0
for cid in ddl_m01_bulk_first_5; do
    # find the passing attempt's captured log
    ce_log=""
    if [ -f "$RESULTS_DIR/$cid/attempts.jsonl" ]; then
        ce_att="$(python3 -c "
import json
att = None
for line in open('$RESULTS_DIR/$cid/attempts.jsonl'):
    v = json.loads(line)
    if v['verdict'] == 'PASS':
        att = v['attempt']
print(att or '')
" 2>/dev/null)"
        if [ -n "$ce_att" ] && [ -s "$RESULTS_DIR/$cid/attempt_$ce_att/bpftrace.log" ]; then
            ce_log="$RESULTS_DIR/$cid/attempt_$ce_att/bpftrace.log"
        fi
    fi
    if [ -z "$ce_log" ]; then
        echo "$cid has no PASS attempt with a captured bpftrace.log — counter evidence not runnable" > "$CE_DIR/BLOCKED.txt"
        blocked "counter_evidence: no captured passing log for $cid"
        continue
    fi
    python3 - "$DT_DIR/expect/golden/ddl_bulk_first_5.json" "$CE_DIR/ddl_bulk_first_5_mutated.json" <<'PYEOF'
import json, sys
g = json.load(open(sys.argv[1]))
order = g['expected_dispatch_order']
order[0], order[1] = order[1], order[0]
g['name'] = g['name'] + '_mutated'
g['notes'] = ('COUNTER EVIDENCE COPY: first two expected entries swapped '
              '(inverts the BULK lane arrival FIFO 4608->3072 into 3072->4608). '
              'compare_expect.py MUST FAIL against the captured M01 log.')
json.dump(g, open(sys.argv[2], 'w'), indent=2)
PYEOF
    ce_trace="$DT_DIR/traces/micro/ddl_bulk_first_5.json"
    python3 "$COMPARE_PY" --bpftrace-log "$ce_log" \
        --expect "$CE_DIR/ddl_bulk_first_5_mutated.json" --trace "$ce_trace" \
        --policy q_ddl --tier 2 --out "$CE_DIR/counter_evidence_compare.json" \
        > "$CE_DIR/counter_evidence_stdout.txt" 2>&1
    ce_rc=$?
    ce_status="$(python3 -c "import json;print(json.load(open('$CE_DIR/counter_evidence_compare.json')).get('status','?'))" 2>/dev/null)"
    {
        echo "command: python3 compare_expect.py --bpftrace-log $cid/attempt_$ce_att/bpftrace.log --expect mutated(4608<->3072 swapped) --trace ddl_bulk_first_5.json --policy q_ddl --tier 2"
        echo "result: exit $ce_rc, status $ce_status"
        echo "error: $(python3 -c "import json;print(json.load(open('$CE_DIR/counter_evidence_compare.json')).get('errors'))" 2>/dev/null)"
    } > "$CE_DIR/README.txt"
    if [ "$ce_rc" -eq 1 ] && [ "$ce_status" = "FAIL" ]; then
        pass "counter_evidence (mutated golden FAILs, exit 1)"
        ce_done=1
    else
        fail "counter_evidence: mutated golden did NOT fail (rc=$ce_rc status=$ce_status)"
    fi
done
[ "$ce_done" = "1" ] || [ -f "$CE_DIR/BLOCKED.txt" ] || blocked "counter_evidence: not executed"

fs_cleanup_ioext
log "=== q_ddl acceptance END ==="

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