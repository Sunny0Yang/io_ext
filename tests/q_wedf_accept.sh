#!/bin/bash
# tests/q_wedf_accept.sh — q_wedf closed-loop acceptance runner (VM-side).
#
# Mirrors the q_mq_deadline standard (tests/full_suite/deterministic/
# run_dt_mqdd_only.sh run_one flow) with the q_wedf rig requirements:
#
# Gates:
#   0. reference model self-test (tests/q_wedf_model.py — host-runnable,
#      both ACTUAL and INTENDED modes)
#   Per Tier-2 case (EVOLUTION_PLAN.md §1.2):
#     G1  max_inflight >= 2          — check_inflight.py (scheduler active)
#     G2  >=1 ISSUE with >=2 INSERTs pending-but-not-COMPLETED at that ts
#         — computed HERE (the shared compare_expect.py/check_inflight.py
#           never increment dispatch_with_choice: KNOWN shared-layer issue)
#     G3  window purity              — the leading ISSUE burst covers exactly
#           the trace sector set (each once, correct op, >=1ms isolated from
#           any later background IO at the same sectors); completion
#           coverage == trace set. Raw counts recorded as evidence.
#     G4  insert_count == trace request count (±intentional merges) —
#           measured via TRACE_DONE (submission) + burst dispatch coverage
#           + nr match (no merges); compare.json's insert_count became
#           meaningful again after the Phase-2 scx_io tracepoint fix
#   compare_expect.py verdict per case (exact/partial/invariants_only).
#
# Rig (inside VM): scx_io switch + `echo 1023 > .../queue/nr_requests`
# (scheduler pipeline must exceed the device tag pool), BPS-throttled
# virtio disk (200MB/s, set by ci.sh), IOEXT_DESTRUCTIVE_TEST=1.
#
# Weight gap (KNOWN — EVOLUTION_PLAN.md §8 row 5.4): q_wedf's "W" has no
# input path. This runner records the evidence package (grep proofs, loader
# --help, model divergence) and marks the designed wedf_weight_probe case
# BLOCKED-weight. It does NOT fake a weight PASS.
#
# Run inside the virtme-ng VM (root):
#   tests/q_wedf_accept.sh
# Exit: 0 = PASS, 1 = FAIL/BLOCKED. Per-case verdicts and root-cause
# evidence are written under results/q_wedf_accept_<ts>/.
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FS_DIR="$BASE_DIR/tests/full_suite"
DT_DIR="$FS_DIR/deterministic"
LOADER_DIR="$BASE_DIR/userspace/build"
MODEL="$BASE_DIR/tests/q_wedf_model.py"
BPFTRACE_SCRIPT="$DT_DIR/bpftrace/trace_dispatch.bt"
COMPARE_PY="$DT_DIR/compare_expect.py"
CHECK_INFLIGHT_PY="$DT_DIR/check_inflight.py"
TRACE_DRIVER="$DT_DIR/trace_driver"

RESULTS_DIR="${RESULTS_DIR:-$BASE_DIR/results/q_wedf_accept_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$RESULTS_DIR"
PASS=0; FAIL=0; BLOCKED=0
R_FILE="$RESULTS_DIR/results.txt"
: > "$R_FILE"

log()      { printf '[q_wedf_accept] %s\n' "$*" >&2; }
pass()     { log "PASS: $1"; echo "PASS: $1" >> "$R_FILE"; PASS=$((PASS+1)); }
fail()     { log "FAIL: $1: $2"; echo "FAIL: $1: $2" >> "$R_FILE"; FAIL=$((FAIL+1)); }
blocked()  { log "BLOCKED: $1: $2"; echo "BLOCKED: $1: $2" >> "$R_FILE"; BLOCKED=$((BLOCK+1)); }

record_weight_blocked() {
    # BLOCKED-weight is the CORRECT Phase-1 verdict for the weight probe
    # (design artifact, by-design unrunnable) — recorded but NOT counted
    # against the exit code.
    log "BLOCKED-weight: $1: $2"
    echo "BLOCKED-weight (expected, by design): $1: $2" >> "$R_FILE"
}

export TARGET_DEVICE="${TARGET_DEVICE:-vda}"

log "=== q_wedf acceptance START ==="
log "RESULTS=$RESULTS_DIR"

[ "$(id -u)" -eq 0 ] || { log "must run as root"; exit 1; }

# ── shared helpers from lib_test.sh; errexit is disabled right after so
#    every failure is RECORDED instead of aborting the run ──────────────────
source "$FS_DIR/lib_test.sh"
set +e

fs_require_vm
fs_detect_device
DEVICE="$FS_DEVICE"
log "DEVICE=$DEVICE (TARGET_DEVICE=${TARGET_DEVICE})"
fs_require_destructive_guard
fs_ensure_bpffs
fs_setup_cgroup_io
fs_cleanup_ioext

# ── trace_driver: use the shared binary read-only; build a PRIVATE copy only
#    if it is missing (never overwrite the shared one) ─────────────────────
if [ ! -x "$TRACE_DRIVER" ]; then
    mkdir -p /tmp/opencode
    if [ ! -x /tmp/opencode/q_wedf_trace_driver ]; then
        if ! gcc -O2 -o /tmp/opencode/q_wedf_trace_driver "$DT_DIR/trace_driver.c"; then
            blocked "trace_driver" "build failed"
            { echo ""; echo "## Summary"; echo "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED"; } >> "$R_FILE"
            exit 1
        fi
    fi
    TRACE_DRIVER=/tmp/opencode/q_wedf_trace_driver
    log "using private trace_driver at $TRACE_DRIVER"
fi

if [ ! -x "$LOADER_DIR/loader_q_wedf" ]; then
    blocked "loader" "loader_q_wedf missing at $LOADER_DIR (run: make -C userspace KERNEL_SRC=\$PWD/kernel/linux-7.3.1 loader_q_wedf)"
    { echo ""; echo "## Summary"; echo "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED"; } >> "$R_FILE"
    exit 1
fi

trace_driver_args() {
    local trace_file="$1"
    local mode
    mode=$(python3 -c "import json;d=json.load(open('$trace_file'));print(d.get('defaults',{}).get('submit_mode','sync'))" 2>/dev/null || echo sync)
    if [ "$mode" = "async_batch" ] || [ "$mode" = "async" ]; then
        local batch
        batch=$(python3 -c "import json;d=json.load(open('$trace_file'));print(d.get('defaults',{}).get('batch_size',8))" 2>/dev/null || echo 8)
        echo "--mode async --batch $batch"
        return
    fi
    echo ""
}

compute_g3g4() { # bpftrace_log trace_file trace_output out_json
# G3 window purity + G4 coverage, computed here.
# Phase-2 update: the INSERT-observability landmine this gate was designed
# around is FIXED — kernel block/iosched_ext.c scx_io_insert_requests now
# emits trace_block_rq_insert per request (2026-09-01). The G4 method
# (TRACE_DONE + burst coverage + nr match) is kept: it is submission-level
# and stays valid regardless of INSERT observability; insert_events_raw /
# insert_observable in g34.json now reflect real tracepoint coverage.
python3 - "$1" "$2" "$3" "$4" <<'PYEOF'
import json, re, sys
log_path, trace_path, trace_out_path, out_path = sys.argv[1:5]
ISSUE_RE = re.compile(r'ISSUE ts=(\d+) sector=(\d+) nr=(\d+) rwbs=(\S+)')
COMPLETE_RE = re.compile(r'COMPLETE ts=(\d+) sector=(\d+) nr=(\d+) rwbs=(\S+)')
TD_RE = re.compile(r'TRACE_DONE total=(\d+) failures=(\d+)')
td = json.load(open(trace_path))
reqs = td.get('requests', [])
trace_n = len(reqs)
trace_sectors = set(r.get('sector', -1) for r in reqs)
nr_by_sector = {r.get('sector'): r.get('nr_sectors', 8) for r in reqs}
op_by_sector = {r.get('sector'): r.get('op', 'read') for r in reqs}
issues, completes = [], []
insert_lines = 0
with open(log_path) as f:
    for line in f:
        m = ISSUE_RE.search(line)
        if m:
            sec = int(m.group(2))
            if sec in trace_sectors:
                issues.append({'ts': int(m.group(1)), 'sector': sec,
                               'nr': int(m.group(3)), 'rwbs': m.group(4)})
            continue
        m = COMPLETE_RE.search(line)
        if m:
            sec = int(m.group(2))
            if sec in trace_sectors:
                completes.append({'ts': int(m.group(1)), 'sector': sec,
                                  'nr': int(m.group(3)), 'rwbs': m.group(4)})
            continue
        if 'INSERT ts=' in line:
            insert_lines += 1
issues.sort(key=lambda e: e['ts'])
completes.sort(key=lambda e: e['ts'])
burst = issues[:trace_n]
burst_sectors = [e['sector'] for e in burst]
burst_set_ok = (len(burst) == trace_n and set(burst_sectors) == trace_sectors
                and len(set(burst_sectors)) == trace_n)
gap_to_next_ns = (issues[trace_n]['ts'] - issues[trace_n - 1]['ts']
                  if len(issues) > trace_n else None)
gap_ok = gap_to_next_ns is None or gap_to_next_ns >= 1_000_000
def rwbs_op(rwbs):
    if 'W' in rwbs:
        return 'write'
    if 'D' in rwbs:
        return 'discard'
    return 'read'
burst_nr_match = all(e['nr'] == nr_by_sector[e['sector']] for e in burst) if burst_set_ok else False
burst_op_match = all(rwbs_op(e['rwbs']) == op_by_sector[e['sector']] for e in burst) if burst_set_ok else False
complete_coverage_ok = bool(completes) and set(e['sector'] for e in completes) == trace_sectors
trace_done_total = trace_done_failures = None
with open(trace_out_path) as f:
    for line in f:
        m = TD_RE.search(line)
        if m:
            trace_done_total, trace_done_failures = int(m.group(1)), int(m.group(2))
trace_done_ok = trace_done_total == trace_n and trace_done_failures == 0
res = {
    'insert_events_raw': insert_lines,
    'insert_observable': insert_lines > 0,
    'insert_note': ('INSERT events restored by the Phase-2 scx_io tracepoint '
                    'fix; G4 method (TRACE_DONE + burst coverage) unchanged'),
    'trace_n': trace_n,
    'burst_issue_sectors': burst_sectors,
    'burst_set_ok': burst_set_ok,
    'burst_gap_to_next_ns': gap_to_next_ns,
    'burst_gap_ok': gap_ok,
    'burst_nr_match': burst_nr_match,
    'burst_op_match': burst_op_match,
    'complete_coverage_ok': complete_coverage_ok,
    'complete_events': len(completes),
    'trace_done_total': trace_done_total,
    'trace_done_failures': trace_done_failures,
    'G3_pass': burst_set_ok and gap_ok and burst_op_match and complete_coverage_ok,
    'G4_pass': trace_done_ok and burst_set_ok and burst_nr_match,
}
json.dump(res, open(out_path, 'w'), indent=2)
print(json.dumps(res))
PYEOF
}

compute_g2() { # bpftrace_log trace_file out_json — G2 per EVOLUTION_PLAN §1.2
python3 - "$1" "$2" "$3" <<'PYEOF'
import json, re, sys
log_path, trace_path, out_path = sys.argv[1:4]
INSERT_RE = re.compile(r'INSERT ts=(\d+) sector=(\d+)')
ISSUE_RE = re.compile(r'ISSUE ts=(\d+) sector=(\d+)')
COMPLETE_RE = re.compile(r'COMPLETE ts=(\d+) sector=(\d+)')
try:
    trace_sectors = set(r.get('sector', -1) for r in
                        json.load(open(trace_path)).get('requests', []))
except Exception:
    trace_sectors = None
events = []
with open(log_path) as f:
    for line in f:
        m = INSERT_RE.search(line)
        if m:
            ts, sec = int(m.group(1)), int(m.group(2))
            if trace_sectors is None or sec in trace_sectors:
                events.append((ts, 0, sec)); continue
        m = ISSUE_RE.search(line)
        if m:
            ts, sec = int(m.group(1)), int(m.group(2))
            if trace_sectors is None or sec in trace_sectors:
                events.append((ts, 1, sec)); continue
        m = COMPLETE_RE.search(line)
        if m:
            ts, sec = int(m.group(1)), int(m.group(2))
            if trace_sectors is None or sec in trace_sectors:
                events.append((ts, 2, sec)); continue
events.sort(key=lambda e: (e[0], e[1]))
insert_total = sum(1 for e in events if e[1] == 0)
issue_total = sum(1 for e in events if e[1] == 1)
# primary: >=1 ISSUE at which >=2 INSERTs are pending-but-not-COMPLETED
pending = set()
g2_insert_based = 0
for ts, typ, sec in events:
    if typ == 0:
        pending.add(sec)
    elif typ == 1:
        if len(pending) >= 2:
            g2_insert_based += 1
    else:
        pending.discard(sec)
# fallback (only if no INSERT tracepoints appear for scx_io-managed
# requests, as check_inflight.py comments suggest): ISSUE-based inflight
pending = set()
g2_issue_based = 0
for ts, typ, sec in events:
    if typ == 1:
        if len(pending) >= 2:
            g2_issue_based += 1
        pending.add(sec)
    elif typ == 2:
        pending.discard(sec)
if insert_total > 0:
    method, g2 = 'insert_based', g2_insert_based
else:
    method, g2 = 'issue_based_fallback', g2_issue_based
res = {'g2_dispatch_with_choice': g2, 'method': method,
       'g2_insert_based': g2_insert_based, 'g2_issue_based': g2_issue_based,
       'insert_events': insert_total, 'issue_events': issue_total,
       'G2_pass': g2 >= 1}
json.dump(res, open(out_path, 'w'), indent=2)
print(json.dumps(res))
PYEOF
}

# ── gate 0: reference model self-test (both ACTUAL and INTENDED modes) ──────
log "--- gate 0: reference model self-test ---"
if timeout 120 python3 "$MODEL" > "$RESULTS_DIR/model.txt" 2>&1; then
    pass "gate0_model_self_test"
else
    fail "gate0_model_self_test" "$(tail -2 "$RESULTS_DIR/model.txt" | tr '\n' ' ')"
    log "=== q_wedf acceptance END: FAIL (model) ==="
    { echo ""; echo "## Summary"; echo "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED"; echo "Date: $(date)"; } >> "$R_FILE"
    exit 1
fi

# ── run_one: mirrors run_dt_mqdd_only.sh run_one + q_wedf gates ────────────
# rc 0=PASS 1=FAIL(assertion) 2=BLOCKED(env)
run_one() {
    local case_id="$1" trace_file="$2" expect_file="$3" tier="$4" extra="$5" attempt="$6"
    local case_dir="$RESULTS_DIR/dt_$(echo "$case_id" | tr '/.' '__')_a${attempt}"
    mkdir -p "$case_dir"
    local loader_log="$case_dir/loader.log"
    local bpftrace_log="$case_dir/bpftrace.log"
    local trace_out="$case_dir/trace_output.txt"
    local compare_out="$case_dir/compare.json"
    local inflight_out="$case_dir/inflight.json"
    local g2_out="$case_dir/g2.json"
    local mark
    mark="$(fs_dmesg_mark "q_wedf_${case_id}_a${attempt}")"

    fs_set_scx_scheduler || { echo "scx_io switch failed on $DEVICE" > "$case_dir/root_cause.txt"; return 2; }
    echo 1023 > "/sys/block/$DEVICE/queue/nr_requests" 2>/dev/null || true

    fs_start_loader q_wedf "$loader_log" $extra
    if ! fs_wait_attached q_wedf "$FS_LOADER_PID" 15; then
        { echo "loader failed to attach"; cat "$loader_log"; } > "$case_dir/root_cause.txt"
        fs_stop_loader "${FS_LOADER_PID:-}"
        return 2
    fi

    bpftrace "$BPFTRACE_SCRIPT" > "$bpftrace_log" 2>&1 &
    local bpf_pid=$!
    # attach-wait (Phase-3 port): cold VM 首次 bpftrace attach 可 >1s,
    # 不等待则 trace 在探针就绪前播完 (W1 attempt-1 no_events_in_log 的根因)。
    local aw=0
    until grep -q 'Attaching' "$bpftrace_log" 2>/dev/null; do
        sleep 0.2
        aw=$((aw + 1))
        [ "$aw" -ge 30 ] && { log "bpftrace attach-wait timeout"; break; }
    done
    sleep 1

    local driver_args
    driver_args="$(trace_driver_args "$trace_file")"
    echo "[$case_id] driver_args: $driver_args" > "$trace_out"
    if [ -n "$driver_args" ]; then
        "$TRACE_DRIVER" "/dev/$DEVICE" "$trace_file" $driver_args >> "$trace_out" 2>&1 || true
    else
        "$TRACE_DRIVER" "/dev/$DEVICE" "$trace_file" >> "$trace_out" 2>&1 || true
    fi

    sleep 1
    kill -INT "$bpf_pid" 2>/dev/null || true
    wait "$bpf_pid" 2>/dev/null || true

    grep -E 'TRACE_PLAY_(START|END)' "$trace_out" >> "$bpftrace_log" 2>/dev/null || true

    fs_stop_loader "${FS_LOADER_PID:-}"

    if fs_dmesg_has_errors "$mark"; then
        fs_dmesg_snapshot "$mark" > "$case_dir/dmesg_errors.txt"
        echo "dmesg errors after mark $mark" > "$case_dir/root_cause.txt"
        return 2
    fi
    if [ ! -s "$bpftrace_log" ]; then
        echo "bpftrace log empty or missing" > "$case_dir/root_cause.txt"
        return 2
    fi

    local trace_n
    trace_n=$(python3 -c "import json;print(len(json.load(open('$trace_file')).get('requests',[])))" 2>/dev/null || echo 0)

    # G3 evidence: raw (unfiltered) event counts in the captured log
    {
        echo "raw unfiltered counts in bpftrace.log:"
        echo "  INSERT=$(grep -c 'INSERT ts=' "$bpftrace_log")"
        echo "  ISSUE=$(grep -c 'ISSUE ts=' "$bpftrace_log")"
        echo "  COMPLETE=$(grep -c 'COMPLETE ts=' "$bpftrace_log")"
        echo "trace requests: $trace_n"
    } > "$case_dir/g3_evidence.txt"

    local g1="skip(t1)" g2="skip(t1)" g3="-" g4="-"
    if [ "$tier" -ge 2 ]; then
        # G1: scheduler must actually hold >=2 candidates at once
        if python3 "$CHECK_INFLIGHT_PY" --bpftrace-log "$bpftrace_log" \
            --trace "$trace_file" --out "$inflight_out" --min-inflight 2 \
            > "$case_dir/inflight_stdout.txt" 2>&1; then
            g1=$(python3 -c "import json;print(json.load(open('$inflight_out'))['max_inflight'])" 2>/dev/null || echo "?")
        else
            g1="FAIL"
            { echo "G1 FAIL: max_inflight<2 (scheduler_inactive)"; cat "$inflight_out" 2>/dev/null; } > "$case_dir/root_cause.txt"
            return 1
        fi
        # G2: computed here — shared harness dispatch_with_choice is a dead
        # field (compare_expect.py:420-437 / check_inflight.py:56-70 never
        # increment it; run_deterministic.sh:179-185 would always FAIL)
        compute_g2 "$bpftrace_log" "$trace_file" "$g2_out" > "$case_dir/g2_stdout.txt" 2>&1
        if python3 -c "import json,sys;sys.exit(0 if json.load(open('$g2_out'))['G2_pass'] else 1)" 2>/dev/null; then
            g2=$(python3 -c "import json;d=json.load(open('$g2_out'));print('%s(%s)'%(d['g2_dispatch_with_choice'],d['method']))")
        else
            g2="FAIL"
            { echo "G2 FAIL: no ISSUE observed with >=2 pending INSERTs"; cat "$g2_out" 2>/dev/null; } > "$case_dir/root_cause.txt"
            return 1
        fi
    fi

    python3 "$COMPARE_PY" --bpftrace-log "$bpftrace_log" \
        --expect "$expect_file" --trace "$trace_file" \
        --policy q_wedf --tier "$tier" --out "$compare_out" \
        > "$case_dir/compare_stdout.txt" 2>&1 || true

    if [ ! -f "$compare_out" ]; then
        echo "compare.json not generated" > "$case_dir/root_cause.txt"
        return 1
    fi

    local status ins comp iss
    status=$(python3 -c "import json;print(json.load(open('$compare_out')).get('status','FAIL'))" 2>/dev/null || echo FAIL)
    ins=$(python3 -c "import json;print(json.load(open('$compare_out'))['details']['insert_count'])" 2>/dev/null || echo -1)
    iss=$(python3 -c "import json;print(json.load(open('$compare_out'))['details']['issue_count'])" 2>/dev/null || echo -1)
    comp=$(python3 -c "import json;print(json.load(open('$compare_out'))['details']['complete_count'])" 2>/dev/null || echo -1)
    {
        echo "compare.py filtered window counts (sector + dense-burst cluster):"
        echo "  insert=$ins issue=$iss complete=$comp (trace_n=$trace_n)"
        echo "  (insert events restored by Phase-2 scx_io tracepoint fix — see g34.json insert_note)"
        echo "G1=$g1 G2=$g2 G3=$g3 G4=$g4"
    } >> "$case_dir/g3_evidence.txt"

    if [ "$tier" -ge 2 ]; then
        local g34_out="$case_dir/g34.json"
        compute_g3g4 "$bpftrace_log" "$trace_file" "$trace_out" "$g34_out" \
            > "$case_dir/g34_stdout.txt" 2>&1
        if python3 -c "import json,sys;sys.exit(0 if json.load(open('$g34_out'))['G3_pass'] else 1)" 2>/dev/null; then
            g3="PASS"
        else
            g3="FAIL"
            { echo "G3 FAIL: window purity (see g34.json)"; cat "$g34_out" 2>/dev/null; } > "$case_dir/root_cause.txt"
            return 1
        fi
        if python3 -c "import json,sys;sys.exit(0 if json.load(open('$g34_out'))['G4_pass'] else 1)" 2>/dev/null; then
            g4="PASS"
        else
            g4="FAIL"
            { echo "G4 FAIL: coverage (see g34.json)"; cat "$g34_out" 2>/dev/null; } > "$case_dir/root_cause.txt"
            return 1
        fi
    fi

    echo "g1=$g1 g2=$g2 g3=$g3 g4=$g4 status=$status" > "$case_dir/gates.txt"
    if [ "$status" = "PASS" ]; then
        return 0
    fi
    { echo "compare status=$status"; python3 -c "import json;print(json.load(open('$compare_out')).get('errors'))" 2>/dev/null; } > "$case_dir/root_cause.txt"
    return 1
}

# ── case matrix: case_id|trace|expect|tier|extra (extra always empty —
#    loader_q_wedf takes only the common args --cgroup/--arb-prio/--arb-weight) ──
CASES=(
    "W1|$DT_DIR/traces/micro/wedf_sector_asc_8.json|$DT_DIR/expect/golden/wedf_sector_asc_8.json|2|"
    "W2|$DT_DIR/traces/micro/wedf_rw_edf_8.json|$DT_DIR/expect/golden/wedf_rw_edf_8.json|2|"
    "T1|$DT_DIR/traces/micro/wedf_sector_asc_8.json|$DT_DIR/expect/partial/wedf_t1_counts.json|1|"
)

for entry in "${CASES[@]}"; do
    IFS='|' read -r cid trace expect tier extra <<< "$entry"
    log "=== running $cid (tier=$tier) trace=$(basename "$trace") ==="
    verdict="FAIL"; last_rc=1; attempts_used=0
    for attempt in 1 2 3 4 5; do
        attempts_used=$attempt
        rc=0
        run_one "$cid" "$trace" "$expect" "$tier" "$extra" "$attempt" || rc=$?
        last_rc=$rc
        if [ "$rc" -eq 0 ]; then
            pass "$cid (attempt=$attempt)"
            verdict="PASS"
            break
        fi
        log "$cid attempt $attempt rc=$rc ($(tail -1 "$RESULTS_DIR/dt_${cid}_a${attempt}/root_cause.txt" 2>/dev/null))"
        fs_cleanup_ioext
        sleep 1
    done
    if [ "$verdict" != "PASS" ]; then
        if [ "$last_rc" -eq 2 ]; then
            blocked "$cid" "5 attempts, env failure (see dt_${cid}_a${attempts_used}/root_cause.txt)"
        else
            fail "$cid" "5 attempts (see dt_${cid}_a${attempts_used}/root_cause.txt)"
        fi
        fs_cleanup_ioext
    fi
    sleep 1
done

# ── weight-gap evidence package (D8) — recorded, never faked ────────────────
log "--- weight-gap evidence package (D8) ---"
WEG="$RESULTS_DIR/weight_gap"
mkdir -p "$WEG"
{
    echo "== grep -n 'weight' userspace/q_wedf.bpf.c =="
    grep -n "weight" "$BASE_DIR/userspace/q_wedf.bpf.c"
    rc=$?
    [ "$rc" -ne 0 ] && echo "(no matches — desc.weight is never read by q_wedf.bpf.c)"
    echo
    echo "== grep -n 'desc\\.weight\\|\\.weight *=' userspace/ioext_bpf_common.h =="
    grep -n "desc\.weight\|\.weight *=" "$BASE_DIR/userspace/ioext_bpf_common.h"
    rc=$?
    [ "$rc" -ne 0 ] && echo "(no matches — iosched_build_desc never assigns desc.weight; zero-init '= {}' at :195 keeps it 0)"
    echo
    echo "struct field exists but is dead for q_wedf: ioext_policy_shared.h:91 __u32 weight;"
    echo "loader: loader_q_wedf.c:16-17 passes NULL extra opts — no weight knob (see loader_q_wedf_help.txt)"
} > "$WEG/code_weight_never_wired.txt" 2>&1

"$LOADER_DIR/loader_q_wedf" --help > "$WEG/loader_q_wedf_help.txt" 2>&1 || true
{
    echo "== loader_q_wedf --help output (common args only: --cgroup/--arb-prio/--arb-weight/--help/--verbose) =="
    cat "$WEG/loader_q_wedf_help.txt"
    echo
    echo "--arb-weight feeds policy_meta.arb_weight (arbiter control plane),"
    echo "NOT the per-request desc.weight consumed by wedf_base_deadline_ns."
} > "$WEG/loader_cli_no_weight_knob.txt"

python3 "$MODEL" --divergence > "$WEG/model_divergence.txt" 2>&1 || true
cp "$RESULTS_DIR/model.txt" "$WEG/model_selftest.txt" 2>/dev/null || true

{
    echo "wedf_weight_probe (case design + intended golden):"
    echo "  trace:  traces/micro/wedf_weight_probe_8.json (weight field parsed by NO component today)"
    echo "  golden: expect/golden/wedf_weight_probe_intended.json"
    echo "  verdict: BLOCKED-weight — weight input path missing;"
    echo "    1. loader_q_wedf CLI has no weight knob (ioext_cli_args.h:117-123 common args only)"
    echo "    2. iosched_build_desc never sets desc.weight (ioext_bpf_common.h:193-212)"
    echo "    3. q_wedf.bpf.c never reads weight (0 grep matches)"
    echo "    4. no cgroup/ioprio -> weight path exists AT ALL"
    echo "  Phase-2 registration snippet (DT_CASES format, only after unblock):"
    echo "    \"q_wedf-DT-M03|q_wedf|\$DT_DIR/traces/micro/wedf_weight_probe_8.json|\$DT_DIR/expect/golden/wedf_weight_probe_intended.json|2|\""
    echo "  Phase-2 registration snippets for W1/W2 (live-able now):"
    echo "    \"q_wedf-DT-M01|q_wedf|\$DT_DIR/traces/micro/wedf_sector_asc_8.json|\$DT_DIR/expect/golden/wedf_sector_asc_8.json|2|\""
    echo "    \"q_wedf-DT-M02|q_wedf|\$DT_DIR/traces/micro/wedf_rw_edf_8.json|\$DT_DIR/expect/golden/wedf_rw_edf_8.json|2|\""
} > "$WEG/registration_snippets.txt"

record_weight_blocked "wedf_weight_probe" "weight input path missing (loader CLI no knob; desc.weight never set by iosched_build_desc, never read by q_wedf.bpf.c) — case designed, intended golden recorded, NOT runnable on the real policy"

# ── summary ────────────────────────────────────────────────────────────────
fs_cleanup_ioext
log "=== q_wedf acceptance END: PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED (+1 expected BLOCKED-weight) ==="
{
    echo ""
    echo "## Summary"
    echo "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED"
    echo "expected BLOCKED-weight (design): wedf_weight_probe (see weight_gap/)"
    echo "Date: $(date)"
} >> "$R_FILE"
cat "$R_FILE"
echo "RESULTS_DIR=$RESULTS_DIR"

if [ "$FAIL" -gt 0 ] || [ "$BLOCKED" -gt 0 ]; then
    exit 1
fi
exit 0