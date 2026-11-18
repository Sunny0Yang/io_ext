#!/bin/bash
# tests/arb_slo_accept.sh — arb_slo closed-loop acceptance test.
#
# Gates (ref/gpt-advices/refine.md §3 SLO acceptance + POL-02 + dev guide §1/§7):
#   0. reference model self-test (tests/arb_slo_model.py)
#   1. strict mode armed: dmesg "arbiter 'slo_arbiter' ... arb_mode=1"
#      after arbiter attach
#   2. Case 1 (SLO boost): cgA = LC (arb-prio 1, 4K randread), cgB = BE
#      (arb-prio 5, 4K randwrite), equal weights 100/100
#      -> boost asymmetry: A.boost > 1 (mid or after dump) while
#         B.boost == 1 in ALL dumps (per-tenant boost, POL-02: a global
#         max_boost would lift both)
#      -> charge coverage: (completed_bytes - requeued_bytes) ~= fio
#         io_bytes per tenant (+/-2%): no generic fallback dispatch
#      -> state conservation: pending == 0, inflight == 0 for both
#         tenants AND the per-hctx inflight_count map all-zero at end
#      -> hold fired: slo_hold_cnt sum > 0 (the hold gate actually
#         engaged while the LC tenant was violating)
#   3. Case 2 (hold semantics, two short passes):
#      pass 1 BE-only control — inflight reaches the protect threshold
#      but NO LC tenant is in PRESSURE -> hold must NOT fire
#      pass 2 LC-only — LC enters PRESSURE while inflight is at/above
#      the threshold -> hold fires; after the workload drains,
#      inflight_count is all-zero and pending/inflight are 0 (the gate
#      released and the elevator drained completely)
#
# Test methodology (the rules that make DRR arbiters measurable,
# inherited from arb_cost_accept.sh / arb_wdrr_accept.sh):
#   - fio processes are placed INTO their cgroups via cgroup.procs
#     (cd into the cgroup dir does NOT attribute IO); child shells use
#     $BASHPID, never $$ (dev guide §6 pitfall 1)
#   - '+io' is written to cgroup.subtree_control BEFORE the child
#     cgroups are used (pitfall 2)
#   - both fio jobs run on the SAME CPU so both cgroups share one hctx
#   - nr_requests is raised to 1023 after switching to scx_io so the
#     scheduler pipeline exceeds the device queue depth (§4)
#
# slo_hold_cnt / inflight_count are per-hctx ARRAY maps inside the
# loader's BPF object: `bpftool -j map dump name <map>` finds them by
# name (unlike the pinned policy_meta, which needs its pin path —
# pitfall 5); their raw values are hex byte strings -> int(b, 0) decode
# (pitfall 6).
#
# Run inside the virtme-ng VM (root):
#   tests/arb_slo_accept.sh
# Exit: 0 = PASS, 1 = FAIL/BLOCKED
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOADER_DIR="$BASE_DIR/userspace/build"
BT="$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
DEVICE="${TARGET_DEVICE:-nvme0n1}"
CG_ROOT="/sys/fs/cgroup"
SLO_RUNTIME="${SLO_RUNTIME:-45}"      # Case-1 runtime seconds
SLO_MID="${SLO_MID:-15}"              # Case-1 mid-dump offset (T+15s)
SLO_CASE2_T="${SLO_CASE2_T:-15}"      # Case-2 per-pass runtime seconds
SLO_CASE2_MID="${SLO_CASE2_MID:-7}"   # Case-2 mid-dump offset
SLO_PROTECT_INFLIGHT=32               # ARB_SLO_DEFAULT_PROTECT_INFLIGHT
QD="${SLO_QD:-512}"
PIN_CPU="${SLO_PIN_CPU:-1}"
RESULTS_DIR="${RESULTS_DIR:-$BASE_DIR/results/arb_slo_accept_$(date +%Y%m%d_%H%M%S)}"

mkdir -p "$RESULTS_DIR"
PASS=0; FAIL=0; BLOCKED=0
R_FILE="$RESULTS_DIR/results.txt"
: > "$R_FILE"

log()  { printf '[arb_slo_accept] %s\n' "$*" >&2; }
pass() { log "PASS: $1"; echo "PASS: $1" >> "$R_FILE"; PASS=$((PASS+1)); }
fail() { log "FAIL: $1: $2"; echo "FAIL: $1: $2" >> "$R_FILE"; FAIL=$((FAIL+1)); }
blocked() { log "BLOCKED: $1: $2"; echo "BLOCKED: $1: $2" >> "$R_FILE"; BLOCKED=$((BLOCKED+1)); }

log "=== arb_slo acceptance START ==="
log "DEVICE=$DEVICE RESULTS=$RESULTS_DIR runtime1=${SLO_RUNTIME}s mid1=${SLO_MID}s case2=${SLO_CASE2_T}sx2 qd=$QD pin_cpu=$PIN_CPU protect_inflight=$SLO_PROTECT_INFLIGHT"
log "  rig: nr_requests=$(cat /sys/block/$DEVICE/queue/nr_requests 2>/dev/null) hctx0_tags=$(cat /sys/block/$DEVICE/mq/0/nr_tags 2>/dev/null)"

command -v fio >/dev/null 2>&1 || { log "fio not found"; exit 1; }
mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf || { log "cannot mount bpffs"; exit 1; }
# '+io' BEFORE the child cgroups exist (dev guide §6 pitfall 2): without
# it the child cgroups get no io css and policies register to root.
echo '+io' > "$CG_ROOT/cgroup.subtree_control" 2>/dev/null || true
mkdir -p "$CG_ROOT/cgA" "$CG_ROOT/cgB" 2>/dev/null || true

cleanup() {
    pkill -TERM -f 'loader_arb_slo\|loader_q_fifo' 2>/dev/null || true
    sleep 2
    pkill -9 -f 'loader_arb_slo\|loader_q_fifo' 2>/dev/null || true
    rm -rf /sys/fs/bpf/ioext_policy_meta /sys/fs/bpf/ioext_active_cgroups 2>/dev/null || true
    echo none > "/sys/block/$DEVICE/queue/scheduler" 2>/dev/null || true
    sleep 1
}

set_scx_io() {
    echo scx_io > "/sys/block/$DEVICE/queue/scheduler" 2>/dev/null
    # Grow the scheduler tag pool to its maximum (live-request pipeline).
    # It must exceed the device queue depth, otherwise blk-mq drains the
    # elevator straight into the device and the arbiter never sees a
    # backlog (nr_requests == device tags on both nvme and virtio here).
    echo 1023 > "/sys/block/$DEVICE/queue/nr_requests" 2>/dev/null || true
    sleep 1
}

# ---------------------------------------------------------------- helpers

get_fio_stat() { # file -> "ios bytes"
    python3 -c "
import json,re
try:
    raw = open('$1').read()
    m = re.search(r'\{.*}', raw, re.DOTALL)
    if m:
        d = json.loads(m.group(0))
        j = d.get('jobs', [{}])[0]
        bs = 0; ios = 0
        for op in ('read', 'write', 'trim'):
            sd = j.get(op, {})
            bs += sd.get('io_bytes', 0)
            ios += sd.get('total_ios', 0)
        print(f'{ios} {bs}')
    else:
        print('0 0')
except Exception:
    print('0 0')
" 2>/dev/null
}

dump_tenant_state() { # file
    "$BT" -j map dump name tenant_state > "$1" 2>/dev/null || echo '[]' > "$1"
}

dump_array_map() { # map_name file
    "$BT" -j map dump name "$1" > "$2" 2>/dev/null || echo '[]' > "$2"
}

TS_FIELDS="deficit pending inflight completed_bytes completed_cost dispatches requeues requeued_bytes serve_selects refill_arms wrap_selects skip_pending_zero refilled_round boost slo_state window_start_ns window_total window_over_target good_windows last_latency_ns ewma_latency_ns last_charge_ns last_active_ns"

decode_tenant_entries() { # dump_file -> "cgid:field=value ..." per line
    python3 -c "
import json, struct
FMT = '<qqqqqqqqqqqqqIIQIII4xqqqq'
FIELDS = '$TS_FIELDS'.split()
try:
    entries = json.load(open('$1'))
except Exception:
    print(0); raise SystemExit
for e in entries:
    k = e.get('key')
    if isinstance(k, dict):
        kid = k.get('cgroup_id')
        hctx = k.get('hctx_idx', 0)
    elif isinstance(k, list):
        kid = 0
        for i, b in enumerate(k[:8]):
            kid |= int(b, 0) << (8 * i)
        hctx = int(k[8], 0) if len(k) > 8 else 0
    else:
        continue
    v = e.get('value', {})
    if isinstance(v, dict):
        for f in FIELDS:
            print(f'{kid}:{f}={v.get(f, 0)}')
    else:
        raw = bytes(int(x, 0) for x in v)
        vals = struct.unpack(FMT, raw[:struct.calcsize(FMT)])
        for f, val in zip(FIELDS, vals):
            print(f'{kid}:{f}={val}')
" 2>/dev/null
}

tenant_stat() { # dump_file cgid field -> summed value over all hctx
    decode_tenant_entries "$1" | awk -F'[=:]' -v cgid="$2" -v field="$3" '
        $1 == cgid && $2 == field { sum += $3 }
        END { print sum + 0 }'
}

tenant_entries() { # dump_file -> number of distinct cgroup_ids
    python3 -c "
import json
try:
    entries = json.load(open('$1'))
except Exception:
    print(0); raise SystemExit
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
print(len(ids))
" 2>/dev/null
}

# sum of every value in a u64 ARRAY map dump (slo_hold_cnt /
# inflight_count).  bpftool -j emits either raw hex byte strings
# (BTF-less view, the format this rig actually produces — verified in
# the arb_cost_accept results) or a decoded number/dict; both are
# handled (int(b, 0) tolerance, dev guide §6 pitfall 6).
array_map_sum() { # dump_file -> decimal sum
    python3 -c "
import json
try:
    entries = json.load(open('$1'))
except Exception:
    print(0); raise SystemExit
total = 0
for e in entries:
    v = e.get('value', 0)
    if isinstance(v, dict):
        for x in v.values():
            if isinstance(x, (int, float)):
                total += int(x)
            break
    elif isinstance(v, list):
        n = 0
        for i, b in enumerate(v):
            n |= int(b, 0) << (8 * i)
        total += n
    else:
        total += int(v)
print(total)
" 2>/dev/null
}

make_fio_job() { # name bs rw runtime
    cat > "$RESULTS_DIR/$1.fio" <<FEOF
[job]
filename=/dev/$DEVICE
rw=$3
bs=$2
ioengine=libaio
direct=1
iodepth=$QD
time_based=1
runtime=$4
numjobs=1
cpus_allowed=$PIN_CPU
FEOF
}

run_fio_in_cg() { # cgroup jobfile outfile
    (
        # $$ is the parent shell's pid even in a subshell: use BASHPID,
        # otherwise the whole test script migrates into the cgroup
        echo "$BASHPID" > "$1/cgroup.procs" &&
        exec fio "$2" --output-format=json > "$3" 2>/dev/null
    )
}

start_arbiter() { # log file
    "$LOADER_DIR/loader_arb_slo" > "$1" 2>&1 &
}

# The kernel-visible arbiter NAME is the .struct_ops.link C variable name
# in arb_slo.bpf.c (`struct iosched_arbiter_ops slo_arbiter`), NOT the
# .name field of the BPF value ("arb_slo"): the loader copies
# bpf_map__name(struct_ops_map) into ops->name (ioext_loader_common.h),
# and the kernel prints "iosched: arbiter '%s' ... arb_mode=%llu" from
# ops->name / ops->arb_mode (arb_slo.bpf.c sets .arb_mode =
# IOSCHED_ARB_STRICT = 1).  Same chain as arb_cost (see its accept
# script for the source verification).
wait_arbiter_strict() { # seconds; polls dmesg for the reg line
    local timeout="$1" i
    for ((i = 0; i < timeout; i++)); do
        if dmesg 2>/dev/null | grep -q "arbiter 'slo_arbiter'.*arb_mode=1"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_registered() { # cgidA cgidB seconds; both appear in policy_meta
    local a="$1" b="$2" timeout="$3" i
    for ((i = 0; i < timeout; i++)); do
        # policy_meta is pinned from BPF_MAP_CREATE without map_name, so
        # `bpftool map dump name policy_meta` finds nothing: dump by pin path
        "$BT" -j map dump pinned /sys/fs/bpf/ioext_policy_meta \
            > "$RESULTS_DIR/.meta_probe.json" 2>/dev/null || true
        if [ "$(python3 -c "
import json
try: entries = json.load(open('$RESULTS_DIR/.meta_probe.json'))
except Exception: entries = []
cgids = set()
for e in entries:
    k = e.get('key')
    if isinstance(k, dict):
        cgids.add(k.get('cgroup_id'))
    elif isinstance(k, list):
        kid = 0
        for i, b in enumerate(k[:8]):
            kid |= int(b, 0) << (8 * i)
        cgids.add(kid)
print(1 if int('$a') in cgids and int('$b') in cgids else 0)
" 2>/dev/null)" = "1" ]; then
            return 0
        fi
        sleep 1
    done
    return 1
}

check_kernel_stable() { # dmesg_file
    if grep -iE '\bpanic\b|\boops\b|BUG:|hung_task|soft lockup|WARNING:.*iosched' "$1" 2>/dev/null \
       | grep -vq 'Command line'; then
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------- gate 0

log "--- gate 0: reference model self-test ---"
# tests/arb_slo_model.py is the algorithm spec for the SLO DRR core +
# window FSM + hold gate (pure python, host-side; mirrors
# arb_cost_model.py's role).  Exit-code gated; short-circuit on failure.
if timeout 120 python3 "$BASE_DIR/tests/arb_slo_model.py" > "$RESULTS_DIR/model.txt" 2>&1; then
    pass "model_self_test"
else
    fail "model_self_test" "$(tail -2 "$RESULTS_DIR/model.txt" | tr '\n' ' ')"
    log "=== arb_slo acceptance END: FAIL (model) ==="
    echo "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED" >> "$R_FILE"
    exit 1
fi

# ================================================================ Case 1

# THE SLO boost case: cgA is LC (arb-prio 1 -> slo class LC, 100us
# target), cgB is BE (arb-prio 5 -> no latency target, boost can never
# move).  Equal weights 100/100.  On the throttled rig every A request
# takes milliseconds >> 100us, so ~100% of A's completions violate and A
# walks NORMAL -> PRESSURE (boost 4) -> SEVERE (boost 8) within the
# first two 100ms windows; B's boost stays 1 in every dump.  Expected:
# A.boost > 1 in the mid/after dumps while B.boost == 1 everywhere —
# the per-tenant boost proof (POL-02 forbids a global max_boost).
#
# Runtime rationale (SLO_RUNTIME=45): with boost 8 A's round budget is
# 64B*100*8 = 51.2KB, B's 6.4KB (the adaptive floor max(4096,
# last_round_max_charge) never binds above these); one A+B cycle ~=
# 57.6KB ~= 0.3ms at the 200MB/s throttle -> 45s ~= ~150K cycles ->
# snapshot skew bounded to one partial turn (negligible).
log "--- Case 1: cgA=LC(prio 1, w=100, 4K randread) / cgB=BE(prio 5, w=100, 4K randwrite), expect A.boost>1 & B.boost==1, hold fires ---"
cleanup
set_scx_io || { blocked "slo_c1" "no scx_io"; }
dmesg -C 2>/dev/null || true

start_arbiter "$RESULTS_DIR/slo_c1_arb.log"
ARB_PID=$!
if ! wait_arbiter_strict 10; then
    fail "slo_c1_strict" "no 'arb_mode=1' in dmesg after arbiter attach"
    dmesg 2>/dev/null | grep 'iosched: arbiter' | tail -2 > "$RESULTS_DIR/slo_c1_arb_dmesg.txt" || true
else
    pass "slo_c1_strict" 
fi

CGA_ID=$(stat -c %i "$CG_ROOT/cgA")
CGB_ID=$(stat -c %i "$CG_ROOT/cgB")
log "  cgA id=$CGA_ID cgB id=$CGB_ID"

"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgA" --arb-prio 1 --arb-weight 100 > "$RESULTS_DIR/slo_c1_cgA.log" 2>&1 &
QA_PID=$!
sleep 2
"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgB" --arb-prio 5 --arb-weight 100 > "$RESULTS_DIR/slo_c1_cgB.log" 2>&1 &
QB_PID=$!
sleep 2

if ! wait_registered "$CGA_ID" "$CGB_ID" 10; then
    blocked "slo_c1" "policies not registered in policy_meta"
    kill $ARB_PID $QA_PID $QB_PID 2>/dev/null || true
else
    pass "slo_c1_registered"
    make_fio_job "slo_c1a" "4k" "randread" "$SLO_RUNTIME"
    make_fio_job "slo_c1b" "4k" "randwrite" "$SLO_RUNTIME"
    dump_tenant_state "$RESULTS_DIR/slo_c1_ts_before.json"
    dump_array_map slo_hold_cnt "$RESULTS_DIR/slo_c1_hold_before.json"
    dump_array_map inflight_count "$RESULTS_DIR/slo_c1_inflight_before.json"

    run_fio_in_cg "$CG_ROOT/cgA" "$RESULTS_DIR/slo_c1a.fio" "$RESULTS_DIR/slo_c1_a.json" &
    FA=$!
    run_fio_in_cg "$CG_ROOT/cgB" "$RESULTS_DIR/slo_c1b.fio" "$RESULTS_DIR/slo_c1_b.json" &
    FB=$!
    sleep "$SLO_MID"
    dump_tenant_state "$RESULTS_DIR/slo_c1_ts_mid.json"
    dump_array_map slo_hold_cnt "$RESULTS_DIR/slo_c1_hold_mid.json"
    dump_array_map inflight_count "$RESULTS_DIR/slo_c1_inflight_mid.json"
    wait $FA $FB

    dmesg > "$RESULTS_DIR/slo_c1_dmesg.txt" 2>/dev/null || true
    dump_tenant_state "$RESULTS_DIR/slo_c1_ts_after.json"
    dump_array_map slo_hold_cnt "$RESULTS_DIR/slo_c1_hold_after.json"
    dump_array_map inflight_count "$RESULTS_DIR/slo_c1_inflight_after.json"

    read IOS_A BYTES_A <<< "$(get_fio_stat "$RESULTS_DIR/slo_c1_a.json")"
    read IOS_B BYTES_B <<< "$(get_fio_stat "$RESULTS_DIR/slo_c1_b.json")"
    BOOST_A_MID=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_mid.json" "$CGA_ID" boost)
    BOOST_A_AFTER=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_after.json" "$CGA_ID" boost)
    STATE_A_MID=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_mid.json" "$CGA_ID" slo_state)
    STATE_A_AFTER=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_after.json" "$CGA_ID" slo_state)
    BOOST_B_BEFORE=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_before.json" "$CGB_ID" boost)
    BOOST_B_MID=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_mid.json" "$CGB_ID" boost)
    BOOST_B_AFTER=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_after.json" "$CGB_ID" boost)
    HOLD_MID=$(array_map_sum "$RESULTS_DIR/slo_c1_hold_mid.json")
    HOLD_AFTER=$(array_map_sum "$RESULTS_DIR/slo_c1_hold_after.json")
    IF_MID=$(array_map_sum "$RESULTS_DIR/slo_c1_inflight_mid.json")
    IF_AFTER=$(array_map_sum "$RESULTS_DIR/slo_c1_inflight_after.json")
    log "  cgA(LC, 4K randread):  ios=$IOS_A bytes=$BYTES_A boost(mid/after)=$BOOST_A_MID/$BOOST_A_AFTER state=$STATE_A_MID/$STATE_A_AFTER"
    log "  cgB(BE, 4K randwrite): ios=$IOS_B bytes=$BYTES_B boost(before/mid/after)=$BOOST_B_BEFORE/$BOOST_B_MID/$BOOST_B_AFTER"
    log "  hold_cnt(mid/after)=$HOLD_MID/$HOLD_AFTER inflight(mid/after)=$IF_MID/$IF_AFTER"

    # THE per-tenant boost proof (POL-02 / refine.md §3 Case 1):
    # A.boost > 1 in the mid OR after dump (A violates its 100us target
    # on the throttled rig) while B.boost == 1 in ALL THREE dumps.
    # Before the workload starts cgB has no tenant_state entry yet (it
    # appears on the first inserted_request), so the before-dump value is
    # 0 = absent: B must simply not be boosted (0 absent or 1 default).
    # A global max_boost — forbidden — would have lifted B too.
    if [ "$BOOST_A_MID" -gt 1 ] || [ "$BOOST_A_AFTER" -gt 1 ]; then
        A_BOOSTED=1
    else
        A_BOOSTED=0
    fi
    if [ "$A_BOOSTED" = "1" ] && [ "$BOOST_B_BEFORE" -le 1 ] && \
       [ "$BOOST_B_MID" -eq 1 ] && [ "$BOOST_B_AFTER" -eq 1 ]; then
        pass "slo_c1_boost_asymmetry (A.boost mid/after=$BOOST_A_MID/$BOOST_A_AFTER, B.boost=$BOOST_B_BEFORE/$BOOST_B_MID/$BOOST_B_AFTER)"
    else
        fail "slo_c1_boost_asymmetry" "A.boost mid=$BOOST_A_MID after=$BOOST_A_AFTER, B.boost before/mid/after=$BOOST_B_BEFORE/$BOOST_B_MID/$BOOST_B_AFTER (a lifted B means a global max_boost crept in)"
    fi

    CC_A=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_after.json" "$CGA_ID" completed_bytes)
    CC_B=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_after.json" "$CGB_ID" completed_bytes)
    RQ_A=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_after.json" "$CGA_ID" requeued_bytes)
    RQ_B=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_after.json" "$CGB_ID" requeued_bytes)
    log "  cgA: completed_bytes=$CC_A requeued_bytes=$RQ_A vs fio io_bytes=$BYTES_A"
    log "  cgB: completed_bytes=$CC_B requeued_bytes=$RQ_B vs fio io_bytes=$BYTES_B"

    # charge coverage (+/-2%), bytes domain (SLO charges
    # nr_sectors*512 at dispatch, refunds the same bytes on requeue):
    # net_bytes_X = completed_bytes_X - requeued_bytes_X ~= fio
    # io_bytes_X.  Generic-fallback dispatches never pass
    # dispatched_request (no charge), so a coverage break == a fallback
    # dispatch ran == strict-mode violation.
    NET_A=$(python3 -c "print($CC_A - $RQ_A)" 2>/dev/null || echo 0)
    NET_B=$(python3 -c "print($CC_B - $RQ_B)" 2>/dev/null || echo 0)
    COV_OK=$(python3 -c "
na, ea, nb, eb = $NET_A, $BYTES_A, $NET_B, $BYTES_B
ok = ea * 0.98 <= na <= ea * 1.02 and eb * 0.98 <= nb <= eb * 1.02
print(1 if ok else 0)
" 2>/dev/null || echo 0)
    if [ "$COV_OK" = "1" ]; then
        pass "slo_c1_charge_coverage (net bytes == fio io_bytes, no fallback)"
    else
        fail "slo_c1_charge_coverage" "net A=$NET_A B=$NET_B vs fio bytes A=$BYTES_A B=$BYTES_B"
    fi

    # state conservation: both tenants retire to pending == 0 and
    # inflight == 0 at end, and the per-hctx inflight_count map is
    # all-zero (the elevator drained completely).
    PEND_A=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_after.json" "$CGA_ID" pending)
    PEND_B=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_after.json" "$CGB_ID" pending)
    INF_A=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_after.json" "$CGA_ID" inflight)
    INF_B=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_after.json" "$CGB_ID" inflight)
    if [ "$PEND_A" -eq 0 ] && [ "$PEND_B" -eq 0 ] && \
       [ "$INF_A" -eq 0 ] && [ "$INF_B" -eq 0 ] && [ "$IF_AFTER" -eq 0 ]; then
        pass "slo_c1_state_conservation (pending/inflight 0, inflight_count map all-zero)"
    else
        fail "slo_c1_state_conservation" "pending A=$PEND_A B=$PEND_B inflight A=$INF_A B=$INF_B inflight_count_sum=$IF_AFTER"
    fi

    # hold gate engaged: slo_hold_cnt accumulated decisions while the LC
    # tenant was violating (A in PRESSURE + per-hctx inflight >= 32).
    if [ "$HOLD_AFTER" -gt 0 ]; then
        pass "slo_c1_hold_fired (slo_hold_cnt sum=$HOLD_AFTER, mid=$HOLD_MID)"
    else
        fail "slo_c1_hold_fired" "slo_hold_cnt sum=$HOLD_AFTER (hold never engaged: LC never in PRESSURE, inflight never >= $SLO_PROTECT_INFLIGHT, or hook broken)"
    fi

    # tenant entries + dispatch activity
    N=$(tenant_entries "$RESULTS_DIR/slo_c1_ts_after.json")
    if [ "$N" -ge 2 ]; then
        pass "slo_c1_tenant_entries (n=$N)"
    else
        fail "slo_c1_tenant_entries" "n=$N (inserted_request hook not firing?)"
    fi

    DA=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_after.json" "$CGA_ID" dispatches)
    DB=$(tenant_stat "$RESULTS_DIR/slo_c1_ts_after.json" "$CGB_ID" dispatches)
    if [ "$DA" -gt 0 ] && [ "$DB" -gt 0 ]; then
        pass "slo_c1_tenant_dispatches (A=$DA B=$DB)"
    else
        fail "slo_c1_tenant_dispatches" "A=$DA B=$DB"
    fi

    if check_kernel_stable "$RESULTS_DIR/slo_c1_dmesg.txt"; then
        pass "slo_c1_kernel_stable"
    else
        fail "slo_c1_kernel_stable" "dmesg errors"
    fi

    kill $ARB_PID $QA_PID $QB_PID 2>/dev/null || true
    sleep 2
fi

# ================================================================ Case 2

# Hold-gate semantics, two short passes on a FRESH arbiter (fresh maps,
# so slo_hold_cnt starts at 0 for both passes):
#   pass 1 (control): only cgB (BE) drives the device.  Per-hctx
#   inflight reaches the protect threshold (>= 32) — the same condition
#   that would engage hold — but NO LC tenant is in PRESSURE, so the
#   gate must stay closed (slo_hold_cnt == 0).  This is the negative
#   control refine.md §3 Case 3 implies ("hold never fires when no LC
#   tenant is in PRESSURE").
#   pass 2 (pairing): only cgA (LC) drives.  A violates its target
#   (~100% over), enters PRESSURE within the first windows, and the
#   inflight count sits at/above the threshold -> hold fires
#   (slo_hold_cnt > 0 by mid).  After A's fio exits, inflight drains to
#   zero, the gate releases, and the elevator empties completely
#   (pending == 0, per-hctx inflight_count all-zero).
log "--- Case 2: hold semantics — pass 1: BE-only control (no hold); pass 2: LC-only pressure (hold fires, then releases) ---"
cleanup
set_scx_io || { blocked "slo_c2" "no scx_io"; }
dmesg -C 2>/dev/null || true

start_arbiter "$RESULTS_DIR/slo_c2_arb.log"
ARB_PID=$!
if wait_arbiter_strict 10; then
    pass "slo_c2_strict_armed"
else
    fail "slo_c2_strict_armed" "no 'arb_mode=1' in dmesg after arbiter attach"
fi

"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgA" --arb-prio 1 --arb-weight 100 > "$RESULTS_DIR/slo_c2_cgA.log" 2>&1 &
QA_PID=$!
sleep 2
"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgB" --arb-prio 5 --arb-weight 100 > "$RESULTS_DIR/slo_c2_cgB.log" 2>&1 &
QB_PID=$!
sleep 2

if ! wait_registered "$CGA_ID" "$CGB_ID" 10; then
    blocked "slo_c2" "policies not registered"
    kill $ARB_PID $QA_PID $QB_PID 2>/dev/null || true
else
    pass "slo_c2_registered"

    # ---- pass 1: BE-only control --------------------------------
    make_fio_job "slo_c2p1" "4k" "randread" "$SLO_CASE2_T"
    run_fio_in_cg "$CG_ROOT/cgB" "$RESULTS_DIR/slo_c2p1.fio" "$RESULTS_DIR/slo_c2_p1.json" &
    FB=$!
    sleep "$SLO_CASE2_MID"
    dump_array_map inflight_count "$RESULTS_DIR/slo_c2_p1_inflight_mid.json"
    dump_array_map slo_hold_cnt "$RESULTS_DIR/slo_c2_p1_hold_mid.json"
    wait $FB
    dump_array_map slo_hold_cnt "$RESULTS_DIR/slo_c2_p1_hold_end.json"
    dump_tenant_state "$RESULTS_DIR/slo_c2_p1_ts_end.json"
    IF_P1_MID=$(array_map_sum "$RESULTS_DIR/slo_c2_p1_inflight_mid.json")
    HOLD_P1_END=$(array_map_sum "$RESULTS_DIR/slo_c2_p1_hold_end.json")
    log "  pass1 (BE only): inflight(mid)=$IF_P1_MID hold_cnt(end)=$HOLD_P1_END"
    if [ "$IF_P1_MID" -ge "$SLO_PROTECT_INFLIGHT" ] && [ "$HOLD_P1_END" -eq 0 ]; then
        pass "slo_c2_no_hold_without_lc_pressure (inflight=$IF_P1_MID >= $SLO_PROTECT_INFLIGHT but hold_cnt=0)"
    else
        fail "slo_c2_no_hold_without_lc_pressure" "inflight(mid)=$IF_P1_MID hold_cnt(end)=$HOLD_P1_END (threshold reached without LC pressure must NOT hold)"
    fi

    sleep 2

    # ---- pass 2: LC-only pressure -------------------------------
    make_fio_job "slo_c2p2" "4k" "randread" "$SLO_CASE2_T"
    run_fio_in_cg "$CG_ROOT/cgA" "$RESULTS_DIR/slo_c2p2.fio" "$RESULTS_DIR/slo_c2_p2.json" &
    FA=$!
    sleep "$SLO_CASE2_MID"
    dump_tenant_state "$RESULTS_DIR/slo_c2_p2_ts_mid.json"
    dump_array_map inflight_count "$RESULTS_DIR/slo_c2_p2_inflight_mid.json"
    dump_array_map slo_hold_cnt "$RESULTS_DIR/slo_c2_p2_hold_mid.json"
    wait $FA
    dump_tenant_state "$RESULTS_DIR/slo_c2_p2_ts_end.json"
    dump_array_map inflight_count "$RESULTS_DIR/slo_c2_p2_inflight_end.json"
    dump_array_map slo_hold_cnt "$RESULTS_DIR/slo_c2_p2_hold_end.json"

    STATE_A_MID=$(tenant_stat "$RESULTS_DIR/slo_c2_p2_ts_mid.json" "$CGA_ID" slo_state)
    BOOST_A_MID=$(tenant_stat "$RESULTS_DIR/slo_c2_p2_ts_mid.json" "$CGA_ID" boost)
    IF_P2_MID=$(array_map_sum "$RESULTS_DIR/slo_c2_p2_inflight_mid.json")
    HOLD_P2_MID=$(array_map_sum "$RESULTS_DIR/slo_c2_p2_hold_mid.json")
    HOLD_P2_END=$(array_map_sum "$RESULTS_DIR/slo_c2_p2_hold_end.json")
    log "  pass2 (LC only): state(mid)=$STATE_A_MID boost(mid)=$BOOST_A_MID inflight(mid)=$IF_P2_MID hold_cnt(mid/end)=$HOLD_P2_MID/$HOLD_P2_END"
    # evidence pairing: at the mid snapshot A is in PRESSURE with a
    # lifted boost AND inflight is at/above the threshold, and the hold
    # counter has accumulated decisions by then.
    if [ "$STATE_A_MID" -eq 1 ] && [ "$BOOST_A_MID" -gt 1 ] && \
       [ "$IF_P2_MID" -ge "$SLO_PROTECT_INFLIGHT" ] && [ "$HOLD_P2_MID" -gt 0 ]; then
        pass "slo_c2_hold_fired_under_pressure (state=$STATE_A_MID boost=$BOOST_A_MID inflight=$IF_P2_MID hold_cnt=$HOLD_P2_MID)"
    else
        fail "slo_c2_hold_fired_under_pressure" "state=$STATE_A_MID boost=$BOOST_A_MID inflight(mid)=$IF_P2_MID hold_cnt(mid)=$HOLD_P2_MID"
    fi

    # hold released after drain: with the workload gone, inflight drops
    # below the threshold, the gate opens, and the elevator empties.
    PEND_A=$(tenant_stat "$RESULTS_DIR/slo_c2_p2_ts_end.json" "$CGA_ID" pending)
    INF_A=$(tenant_stat "$RESULTS_DIR/slo_c2_p2_ts_end.json" "$CGA_ID" inflight)
    IF_P2_END=$(array_map_sum "$RESULTS_DIR/slo_c2_p2_inflight_end.json")
    if [ "$PEND_A" -eq 0 ] && [ "$INF_A" -eq 0 ] && [ "$IF_P2_END" -eq 0 ]; then
        pass "slo_c2_hold_released_after_drain (pending=$PEND_A inflight=$INF_A inflight_count=$IF_P2_END)"
    else
        fail "slo_c2_hold_released_after_drain" "pending=$PEND_A inflight=$INF_A inflight_count=$IF_P2_END (gate stuck or drain incomplete)"
    fi

    dmesg > "$RESULTS_DIR/slo_c2_dmesg.txt" 2>/dev/null || true
    if check_kernel_stable "$RESULTS_DIR/slo_c2_dmesg.txt"; then
        pass "slo_c2_kernel_stable"
    else
        fail "slo_c2_kernel_stable" "dmesg errors"
    fi

    kill $ARB_PID $QA_PID $QB_PID 2>/dev/null || true
    sleep 2
fi

# ================================================================ summary

cleanup
rmdir "$CG_ROOT/cgA" "$CG_ROOT/cgB" 2>/dev/null || true

log "=== arb_slo acceptance END: PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED ==="
{
    echo ""
    echo "## Summary"
    echo "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED"
    echo "Date: $(date)"
} >> "$R_FILE"
cat "$R_FILE"
echo "RESULTS_DIR=$RESULTS_DIR"

if [ "$FAIL" -gt 0 ] || [ "$BLOCKED" -gt 0 ]; then
    exit 1
fi
exit 0