#!/bin/bash
# tests/arb_cost_accept.sh — arb_cost closed-loop acceptance test.
#
# Gates (ref/gpt-advices/refine.md §4 COST acceptance + dev guide §1/§7):
#   0. reference model self-test (tests/arb_cost_model.py)
#   1. strict mode armed: dmesg "arbiter 'cost_arbiter' ... arb_mode=1"
#      after arbiter attach
#   2. Case 1: equal weights (256/256), cgA 4K randread / cgB 4K randwrite
#      -> completed_cost ratio A/B ~= 1 (COST fairness, THE proof) while
#         request-count ratio A/B ~= 4 (NOT request-count fairness)
#   3. Case 2: weights 256:768, both 4K randread
#      -> cost/request/bytes ratio B/A ~= 3 (weight plumbing in the
#         cost domain; Case 1 covers the cost-vs-request divergence)
#   4. state conservation: tenant_state entries exist for both cgroups,
#      dispatches evolve, and charged cost covers the expected op cost
#      (charge coverage == proof that no generic fallback dispatch ran)
#
# Test methodology (the two rules that make DRR arbiters measurable,
# inherited from arb_wdrr_accept.sh):
#   - fio processes are placed INTO their cgroups via cgroup.procs
#     (cd into the cgroup dir does NOT attribute IO)
#   - both fio jobs run on the SAME CPU so both cgroups share one hctx;
#     per-hctx arbitration cannot arbitrate tenants on different hctxes
#
# Run inside the virtme-ng VM (root):
#   tests/arb_cost_accept.sh
# Exit: 0 = PASS, 1 = FAIL/BLOCKED
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOADER_DIR="$BASE_DIR/userspace/build"
BT="$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
DEVICE="${TARGET_DEVICE:-nvme0n1}"
CG_ROOT="/sys/fs/cgroup"
COST_RUNTIME="${COST_RUNTIME:-45}"    # Case-1 runtime seconds
COST_RUNTIME2="${COST_RUNTIME2:-90}"  # Case-2 runtime seconds
QD="${COST_QD:-512}"
PIN_CPU="${COST_PIN_CPU:-1}"
RESULTS_DIR="${RESULTS_DIR:-$BASE_DIR/results/arb_cost_accept_$(date +%Y%m%d_%H%M%S)}"

mkdir -p "$RESULTS_DIR"
PASS=0; FAIL=0; BLOCKED=0
R_FILE="$RESULTS_DIR/results.txt"
: > "$R_FILE"

log()  { printf '[arb_cost_accept] %s\n' "$*" >&2; }
pass() { log "PASS: $1"; echo "PASS: $1" >> "$R_FILE"; PASS=$((PASS+1)); }
fail() { log "FAIL: $1: $2"; echo "FAIL: $1: $2" >> "$R_FILE"; FAIL=$((FAIL+1)); }
blocked() { log "BLOCKED: $1: $2"; echo "BLOCKED: $1: $2" >> "$R_FILE"; BLOCKED=$((BLOCKED+1)); }

log "=== arb_cost acceptance START ==="
log "DEVICE=$DEVICE RESULTS=$RESULTS_DIR runtime1=${COST_RUNTIME}s runtime2=${COST_RUNTIME2}s qd=$QD pin_cpu=$PIN_CPU"
log "  rig: nr_requests=$(cat /sys/block/$DEVICE/queue/nr_requests 2>/dev/null) hctx0_tags=$(cat /sys/block/$DEVICE/mq/0/nr_tags 2>/dev/null)"

command -v fio >/dev/null 2>&1 || { log "fio not found"; exit 1; }
mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf || { log "cannot mount bpffs"; exit 1; }
echo '+io' > "$CG_ROOT/cgroup.subtree_control" 2>/dev/null || true
mkdir -p "$CG_ROOT/cgA" "$CG_ROOT/cgB" 2>/dev/null || true

cleanup() {
    pkill -TERM -f 'loader_arb_cost\|loader_q_fifo' 2>/dev/null || true
    sleep 2
    pkill -9 -f 'loader_arb_cost\|loader_q_fifo' 2>/dev/null || true
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

key_cgid() { # entry key -> cgroup id (named dict or raw little-endian bytes)
    python3 -c "
import json,sys
e = json.loads(sys.argv[1])
k = e.get('key')
if isinstance(k, dict):
    print(k.get('cgroup_id', 0))
elif isinstance(k, list):
    v = 0
    for i, b in enumerate(k[:8]):
        v |= int(b, 0) << (8 * i)
    print(v)
else:
    print(0)
" "$1" 2>/dev/null
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
    "$LOADER_DIR/loader_arb_cost" > "$1" 2>&1 &
}

# The kernel-visible arbiter NAME is the .struct_ops.link C variable name
# in arb_cost.bpf.c (line 147: `struct iosched_arbiter_ops cost_arbiter`),
# NOT the .name field of the BPF value ("arb_cost"): the loader copies
# bpf_map__name(struct_ops_map) — the C variable name — into ops->name
# (ioext_loader_common.h:1131), and the kernel prints
#   "iosched: arbiter '%s' ... arb_mode=%llu"   (iosched_ext.c:4028)
# from ops->name and ops->arb_mode (arb_cost.bpf.c:154 sets
# .arb_mode = IOSCHED_ARB_STRICT = 1).  Verified: (a) the variable
# declaration in arb_cost.bpf.c; (b) the identical chain for arb_wdrr —
# arb_wdrr.bpf.c:149 declares `wdrr_arbiter`, the wdrr accept greps
# 'wdrr_arbiter' and its CI runs green; (c) the old run log
# results/verify_20260819_195715/cost_c1_arb.log holds only loader stderr
# ("arbiter: value_size=192") — the registration line goes to dmesg, not
# the loader log — so the string is source-verified.
wait_arbiter_strict() { # seconds; polls dmesg for the reg line
    local timeout="$1" i
    for ((i = 0; i < timeout; i++)); do
        if dmesg 2>/dev/null | grep -q "arbiter 'cost_arbiter'.*arb_mode=1"; then
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
# tests/arb_cost_model.py is the algorithm spec for the cost-charge
# semantics (pure python, host-side; mirrors arb_wdrr_model.py's role).
# Exit-code gated; short-circuit on failure.
if timeout 120 python3 "$BASE_DIR/tests/arb_cost_model.py" > "$RESULTS_DIR/model.txt" 2>&1; then
    pass "model_self_test"
else
    fail "model_self_test" "$(tail -2 "$RESULTS_DIR/model.txt" | tr '\n' ' ')"
    log "=== arb_cost acceptance END: FAIL (model) ==="
    echo "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED" >> "$R_FILE"
    exit 1
fi

# ================================================================ Case 1

# THE cost-fairness case: equal weights, different ops.  Equal weights
# -> equal round budgets -> equal charged cost long-run (cost fairness),
# while the cheap-op tenant completes ~4x the requests (4K read costs 64,
# 4K write costs 256) -> request counts are NOT fair.
#
# Weights 256/256: comfortably above the S6 cost floor, no ew bump —
# quantum(ARB_COST_QUANTUM=1) * ew(256) = 256 cost units per round, and
# the adaptive floor max(ARB_COST_MIN_BUDGET=64, last_round_max_charge)
# stays at 256 after B's 4K-write turns (charge 256 <= budget 256), so
# neither tenant is bumped.  Weights <= 64 are floor-compressed to the
# same budget (ioext_arb_common.h refill branch; the bump only raises ew
# when quantum*ew < floor), so fixtures must stay >= 64; wdrr's 100/300
# fixture works too (100 > 64) but 256 keeps clear of the boundary.
#
# Runtime rationale (COST_RUNTIME=45): round budget = 256 cost/turn
# (A-turn = 4 x 4K reads = 16KB ~= 80us, B-turn = 1 x 4K write = 4KB ~=
# 20us at the 200MB/s throttle); full cycle ~= 24KB ~= 0.12ms -> 45s ~=
# hundreds of thousands of cycles -> snapshot skew bounded to one
# partial turn (negligible).
log "--- Case 1: weights 256:256, A=4K randread / B=4K randwrite, expect cost ~1:1, requests ~4:1 ---"
cleanup
set_scx_io || { blocked "cost_c1" "no scx_io"; }
dmesg -C 2>/dev/null || true

start_arbiter "$RESULTS_DIR/cost_c1_arb.log"
ARB_PID=$!
if ! wait_arbiter_strict 10; then
    fail "cost_c1_strict_armed" "no 'arb_mode=1' in dmesg after arbiter attach"
    dmesg 2>/dev/null | grep 'iosched: arbiter' | tail -2 > "$RESULTS_DIR/cost_c1_arb_dmesg.txt" || true
else
    pass "cost_c1_strict_armed"
fi

CGA_ID=$(stat -c %i "$CG_ROOT/cgA")
CGB_ID=$(stat -c %i "$CG_ROOT/cgB")
log "  cgA id=$CGA_ID cgB id=$CGB_ID"

"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgA" --arb-weight 256 > "$RESULTS_DIR/cost_c1_cgA.log" 2>&1 &
QA_PID=$!
sleep 2
"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgB" --arb-weight 256 > "$RESULTS_DIR/cost_c1_cgB.log" 2>&1 &
QB_PID=$!
sleep 2

if ! wait_registered "$CGA_ID" "$CGB_ID" 10; then
    blocked "cost_c1" "policies not registered in policy_meta"
    kill $ARB_PID $QA_PID $QB_PID 2>/dev/null || true
else
    pass "cost_c1_registered"
    make_fio_job "cost_c1a" "4k" "randread" "$COST_RUNTIME"
    make_fio_job "cost_c1b" "4k" "randwrite" "$COST_RUNTIME"
    dump_tenant_state "$RESULTS_DIR/cost_c1_ts_before.json"

    run_fio_in_cg "$CG_ROOT/cgA" "$RESULTS_DIR/cost_c1a.fio" "$RESULTS_DIR/cost_c1_a.json" &
    FA=$!
    run_fio_in_cg "$CG_ROOT/cgB" "$RESULTS_DIR/cost_c1b.fio" "$RESULTS_DIR/cost_c1_b.json" &
    FB=$!
    wait $FA $FB

    dmesg > "$RESULTS_DIR/cost_c1_dmesg.txt" 2>/dev/null || true
    dump_tenant_state "$RESULTS_DIR/cost_c1_ts_after.json"

    read IOS_A BYTES_A <<< "$(get_fio_stat "$RESULTS_DIR/cost_c1_a.json")"
    read IOS_B BYTES_B <<< "$(get_fio_stat "$RESULTS_DIR/cost_c1_b.json")"
    CC_A=$(tenant_stat "$RESULTS_DIR/cost_c1_ts_after.json" "$CGA_ID" completed_cost)
    CC_B=$(tenant_stat "$RESULTS_DIR/cost_c1_ts_after.json" "$CGB_ID" completed_cost)
    RQ_A=$(tenant_stat "$RESULTS_DIR/cost_c1_ts_after.json" "$CGA_ID" requeued_bytes)
    RQ_B=$(tenant_stat "$RESULTS_DIR/cost_c1_ts_after.json" "$CGB_ID" requeued_bytes)
    log "  cgA(w=256, 4K randread):  ios=$IOS_A bytes=$BYTES_A cost=$CC_A refund=$RQ_A"
    log "  cgB(w=256, 4K randwrite): ios=$IOS_B bytes=$BYTES_B cost=$CC_B refund=$RQ_B"

    # THE cost-fairness proof: charged-cost ratio ~= 1 under equal
    # weights despite different op mix (completed_cost from tenant_state,
    # summed over all hctx).
    if [ "$CC_A" -gt 0 ] && [ "$CC_B" -gt 0 ]; then
        CR=$(python3 -c "print(f'{$CC_A/$CC_B:.3f}')" 2>/dev/null || echo 0)
        log "  cost ratio A/B = $CR (expected ~1.0, window [0.75, 1.35])"
        if python3 -c "exit(0 if 0.75 <= float('$CR') <= 1.35 else 1)" 2>/dev/null; then
            pass "cost_c1_cost_fairness (A/B=$CR)"
        else
            fail "cost_c1_cost_fairness" "A/B=$CR outside [0.75,1.35]"
        fi
    else
        fail "cost_c1_cost_fairness" "zero completed_cost A=$CC_A B=$CC_B (cgroup attribution broken?)"
    fi

    # NOT request-count fair: 4K reads are 4x cheaper than 4K writes, so
    # equal-cost service means the read tenant completes ~4x the requests.
    # Two assertions: explicit >= 2.5 (proves NOT 1:1 request fairness)
    # and the [2.5, 5.5] quantitative window (fio total_ios ratio).
    if [ "$IOS_A" -gt 0 ] && [ "$IOS_B" -gt 0 ]; then
        RR=$(python3 -c "print(f'{$IOS_A/$IOS_B:.3f}')" 2>/dev/null || echo 0)
        log "  request ratio A/B = $RR (expected ~4: 4K read cost 1 vs 4K write cost 4)"
        if python3 -c "exit(0 if float('$RR') >= 2.5 else 1)" 2>/dev/null; then
            pass "cost_c1_not_request_fair (req A/B=$RR >= 2.5)"
        else
            fail "cost_c1_not_request_fair" "req A/B=$RR ~= 1: request-count fairness, cost arbitration not in effect"
            [ "$RR" != "0" ] && python3 -c "exit(0 if float('$RR') < 1.5 else 1)" 2>/dev/null && \
                log "  HINT: ratio ~=1 means device not saturated / rig failure — check OSEXT_VM_BPS_THROTTLE=200000000, nr_requests=1023, OSEXT_VM_DISK=virtio"
        fi
        if python3 -c "exit(0 if 2.5 <= float('$RR') <= 5.5 else 1)" 2>/dev/null; then
            pass "cost_c1_request_ratio_window (A/B=$RR in [2.5,5.5])"
        else
            fail "cost_c1_request_ratio_window" "A/B=$RR outside [2.5,5.5]"
        fi
    else
        fail "cost_c1_not_request_fair" "zero ios A=$IOS_A B=$IOS_B (cgroup attribution broken?)"
    fi

    # charge coverage (strict quantitative proof, +/-2%):
    # CRITICAL QUIRK: for arb_cost the requeue refund is COST-valued, but
    # the shared struct field is still named requeued_bytes —
    # arb_note_requeue (ioext_arb_common.h:447-466) accumulates the refund
    # into ts->requeued_bytes regardless of unit (ioext_arb_common.h:463:
    # `ts->requeued_bytes += refund;`), and arb_cost.bpf.c:141-143 passes
    # arb_cost_estimate(opf, nr_sectors) — cost units — as that refund.
    # So: net_cost_X = completed_cost_X - requeued_bytes_X (both cost).
    #
    # Cost table (ioext_arb_common.h arb_cost_estimate, base_cost), rows
    # READ / WRITE x size buckets (<=4K, <=16K, <=64K, <=128K, >128K):
    #   READ : 1   4  16  32  64
    #   WRITE: 4  16  64 128 256
    # 4K read -> cost 1; 4K write -> cost 4.  Both jobs are single-op, so
    # fio total_ios == op ios and expected_cost = read_ios_A*1 (tenant A)
    # / write_ios_B*4 (tenant B).
    #
    # Rationale: generic-fallback dispatches never pass dispatched_request
    # (no arb_note_dispatched_cost charge), so a coverage break == a
    # generic fallback dispatch ran == strict-mode violation.
    NET_A=$(python3 -c "print($CC_A - $RQ_A)" 2>/dev/null || echo 0)
    NET_B=$(python3 -c "print($CC_B - $RQ_B)" 2>/dev/null || echo 0)
    EXP_A=$((IOS_A * 1))
    EXP_B=$((IOS_B * 4))
    log "  net cost: A=$NET_A B=$NET_B vs expected: A=$EXP_A B=$EXP_B"
    COV_OK=$(python3 -c "
na, ea, nb, eb = $NET_A, $EXP_A, $NET_B, $EXP_B
ok = ea * 0.98 <= na <= ea * 1.02 and eb * 0.98 <= nb <= eb * 1.02
print(1 if ok else 0)
" 2>/dev/null || echo 0)
    if [ "$COV_OK" = "1" ]; then
        pass "cost_c1_charge_coverage (charged cost == expected op cost, no fallback)"
    else
        fail "cost_c1_charge_coverage" "net cost A=$NET_A B=$NET_B vs expected A=$EXP_A B=$EXP_B"
    fi

    # state conservation
    N=$(tenant_entries "$RESULTS_DIR/cost_c1_ts_after.json")
    if [ "$N" -ge 2 ]; then
        pass "cost_c1_tenant_state_entries (n=$N)"
    else
        fail "cost_c1_tenant_state_entries" "n=$N (inserted_request hook not firing?)"
    fi

    DA=$(tenant_stat "$RESULTS_DIR/cost_c1_ts_after.json" "$CGA_ID" dispatches)
    DB=$(tenant_stat "$RESULTS_DIR/cost_c1_ts_after.json" "$CGB_ID" dispatches)
    if [ "$DA" -gt 0 ] && [ "$DB" -gt 0 ]; then
        pass "cost_c1_tenant_dispatches (A=$DA B=$DB)"
    else
        fail "cost_c1_tenant_dispatches" "A=$DA B=$DB"
    fi

    if check_kernel_stable "$RESULTS_DIR/cost_c1_dmesg.txt"; then
        pass "cost_c1_kernel_stable"
    else
        fail "cost_c1_kernel_stable" "dmesg errors"
    fi

    kill $ARB_PID $QA_PID $QB_PID 2>/dev/null || true
    sleep 2
fi

# ================================================================ Case 2

# Weight-ratio plumbing under cost (mirrors wdrr Case 1's role): both
# tenants run IDENTICAL op+bs (4K randread), so cost ratio == request
# ratio == bytes ratio — this case validates weight plumbing in the
# cost domain.  It does NOT validate cost-vs-request divergence; Case 1
# does that.
#
# Weights 256/768 (both above the floor, no compression) -> round budget
# ratio 1:3.  COST_RUNTIME2=90 rationale: cycle = 4+12 x 4K reads ~= 64KB
# ~= 0.32ms at the 200MB/s throttle -> 90s ~= hundreds of thousands of
# cycles, so wdrr's [2.0, 4.0] window is safe against turn-granularity
# skew (one partial turn is negligible).
log "--- Case 2: weights 256:768, both 4K randread, expect B/A ratios ~3 ---"
cleanup
set_scx_io || { blocked "cost_c2" "no scx_io"; }
dmesg -C 2>/dev/null || true

start_arbiter "$RESULTS_DIR/cost_c2_arb.log"
ARB_PID=$!
if wait_arbiter_strict 10; then
    pass "cost_c2_strict_armed"
else
    fail "cost_c2_strict_armed" "no 'arb_mode=1' in dmesg after arbiter attach"
fi

"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgA" --arb-weight 256 > "$RESULTS_DIR/cost_c2_cgA.log" 2>&1 &
QA_PID=$!
sleep 2
"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgB" --arb-weight 768 > "$RESULTS_DIR/cost_c2_cgB.log" 2>&1 &
QB_PID=$!
sleep 2

if ! wait_registered "$CGA_ID" "$CGB_ID" 10; then
    blocked "cost_c2" "policies not registered"
    kill $ARB_PID $QA_PID $QB_PID 2>/dev/null || true
else
    pass "cost_c2_registered"
    make_fio_job "cost_c2a" "4k" "randread" "$COST_RUNTIME2"
    make_fio_job "cost_c2b" "4k" "randread" "$COST_RUNTIME2"

    run_fio_in_cg "$CG_ROOT/cgA" "$RESULTS_DIR/cost_c2a.fio" "$RESULTS_DIR/cost_c2_a.json" &
    FA=$!
    run_fio_in_cg "$CG_ROOT/cgB" "$RESULTS_DIR/cost_c2b.fio" "$RESULTS_DIR/cost_c2_b.json" &
    FB=$!
    wait $FA $FB

    dmesg > "$RESULTS_DIR/cost_c2_dmesg.txt" 2>/dev/null || true
    dump_tenant_state "$RESULTS_DIR/cost_c2_ts_after.json"

    read IOS_A BYTES_A <<< "$(get_fio_stat "$RESULTS_DIR/cost_c2_a.json")"
    read IOS_B BYTES_B <<< "$(get_fio_stat "$RESULTS_DIR/cost_c2_b.json")"
    CC_A=$(tenant_stat "$RESULTS_DIR/cost_c2_ts_after.json" "$CGA_ID" completed_cost)
    CC_B=$(tenant_stat "$RESULTS_DIR/cost_c2_ts_after.json" "$CGB_ID" completed_cost)
    RQ_A=$(tenant_stat "$RESULTS_DIR/cost_c2_ts_after.json" "$CGA_ID" requeued_bytes)
    RQ_B=$(tenant_stat "$RESULTS_DIR/cost_c2_ts_after.json" "$CGB_ID" requeued_bytes)
    log "  cgA(w=256, 4K randread): ios=$IOS_A bytes=$BYTES_A cost=$CC_A refund=$RQ_A"
    log "  cgB(w=768, 4K randread): ios=$IOS_B bytes=$BYTES_B cost=$CC_B refund=$RQ_B"

    # cost ratio B/A
    if [ "$CC_A" -gt 0 ] && [ "$CC_B" -gt 0 ]; then
        CR=$(python3 -c "print(f'{$CC_B/$CC_A:.3f}')" 2>/dev/null || echo 0)
        log "  cost ratio B/A = $CR (expected ~3.0, window [2.0, 4.0])"
        if python3 -c "exit(0 if 2.0 <= float('$CR') <= 4.0 else 1)" 2>/dev/null; then
            pass "cost_c2_weight_cost_ratio (B/A=$CR)"
        else
            fail "cost_c2_weight_cost_ratio" "B/A=$CR outside [2.0,4.0]"
            [ "$CR" != "0" ] && python3 -c "exit(0 if float('$CR') < 1.5 else 1)" 2>/dev/null && \
                log "  HINT: ratio ~=1 means the scheduler queue never backlogged (device not saturated). Run with OSEXT_VM_BPS_THROTTLE=200000000"
        fi
    else
        fail "cost_c2_weight_cost_ratio" "zero completed_cost A=$CC_A B=$CC_B"
    fi

    # request ratio B/A
    if [ "$IOS_A" -gt 0 ] && [ "$IOS_B" -gt 0 ]; then
        RR=$(python3 -c "print(f'{$IOS_B/$IOS_A:.3f}')" 2>/dev/null || echo 0)
        log "  request ratio B/A = $RR (expected ~3.0, window [2.0, 4.0])"
        if python3 -c "exit(0 if 2.0 <= float('$RR') <= 4.0 else 1)" 2>/dev/null; then
            pass "cost_c2_weight_request_ratio (B/A=$RR)"
        else
            fail "cost_c2_weight_request_ratio" "B/A=$RR outside [2.0,4.0]"
        fi
    else
        fail "cost_c2_weight_request_ratio" "zero ios A=$IOS_A B=$IOS_B"
    fi

    # bytes ratio B/A
    if [ "$BYTES_A" -gt 0 ] && [ "$BYTES_B" -gt 0 ]; then
        BR=$(python3 -c "print(f'{$BYTES_B/$BYTES_A:.3f}')" 2>/dev/null || echo 0)
        log "  bytes ratio B/A = $BR (expected ~3.0, window [2.0, 4.0])"
        if python3 -c "exit(0 if 2.0 <= float('$BR') <= 4.0 else 1)" 2>/dev/null; then
            pass "cost_c2_weight_bytes_ratio (B/A=$BR)"
        else
            fail "cost_c2_weight_bytes_ratio" "B/A=$BR outside [2.0,4.0]"
        fi
    else
        fail "cost_c2_weight_bytes_ratio" "zero bytes A=$BYTES_A B=$BYTES_B"
    fi

    # charge coverage (+/-2%): all requests are 4K reads -> cost 1 each,
    # so expected_cost = ios * 1 per tenant.  net_cost uses the
    # requeued_bytes cost-quirk documented in Case 1.  A coverage break
    # == generic fallback dispatch ran == strict violation.
    NET_A=$(python3 -c "print($CC_A - $RQ_A)" 2>/dev/null || echo 0)
    NET_B=$(python3 -c "print($CC_B - $RQ_B)" 2>/dev/null || echo 0)
    EXP_A=$((IOS_A * 1))
    EXP_B=$((IOS_B * 1))
    log "  net cost: A=$NET_A B=$NET_B vs expected: A=$EXP_A B=$EXP_B"
    COV_OK=$(python3 -c "
na, ea, nb, eb = $NET_A, $EXP_A, $NET_B, $EXP_B
ok = ea * 0.98 <= na <= ea * 1.02 and eb * 0.98 <= nb <= eb * 1.02
print(1 if ok else 0)
" 2>/dev/null || echo 0)
    if [ "$COV_OK" = "1" ]; then
        pass "cost_c2_charge_coverage"
    else
        fail "cost_c2_charge_coverage" "net cost A=$NET_A B=$NET_B vs expected A=$EXP_A B=$EXP_B"
    fi

    # state conservation
    N=$(tenant_entries "$RESULTS_DIR/cost_c2_ts_after.json")
    if [ "$N" -ge 2 ]; then
        pass "cost_c2_tenant_state_entries (n=$N)"
    else
        fail "cost_c2_tenant_state_entries" "n=$N (inserted_request hook not firing?)"
    fi

    DA=$(tenant_stat "$RESULTS_DIR/cost_c2_ts_after.json" "$CGA_ID" dispatches)
    DB=$(tenant_stat "$RESULTS_DIR/cost_c2_ts_after.json" "$CGB_ID" dispatches)
    if [ "$DA" -gt 0 ] && [ "$DB" -gt 0 ]; then
        pass "cost_c2_tenant_dispatches (A=$DA B=$DB)"
    else
        fail "cost_c2_tenant_dispatches" "A=$DA B=$DB"
    fi

    if check_kernel_stable "$RESULTS_DIR/cost_c2_dmesg.txt"; then
        pass "cost_c2_kernel_stable"
    else
        fail "cost_c2_kernel_stable" "dmesg errors"
    fi

    kill $ARB_PID $QA_PID $QB_PID 2>/dev/null || true
    sleep 2
fi

# ================================================================ summary

cleanup
rmdir "$CG_ROOT/cgA" "$CG_ROOT/cgB" 2>/dev/null || true

log "=== arb_cost acceptance END: PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED ==="
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