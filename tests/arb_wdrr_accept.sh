#!/bin/bash
# tests/arb_wdrr_accept.sh — arb_wdrr closed-loop acceptance test.
#
# Gates (ref/gpt-advices/refine.md Done criteria + dev guide):
#   0. reference model self-test (tests/arb_wdrr_model.py)
#   1. strict mode armed: dmesg "arb_mode=1" after arbiter attach
#   2. Case 1: cgA(w=100) + cgB(w=300), 4K randread -> bytes_B/bytes_A in [2.0, 4.0]
#   3. Case 2: equal weights, A=4K / B=128K -> bytes ratio in [0.7, 1.3]
#      (byte fairness, NOT request-count fairness)
#   4. state conservation: tenant_state entries exist for both cgroups,
#      dispatches/deficit evolve, and charged bytes cover fio bytes
#      (charge coverage == proof that no generic fallback dispatch ran)
#
# Test methodology (the two rules that make WDRR measurable):
#   - fio processes are placed INTO their cgroups via cgroup.procs
#     (cd into the cgroup dir does NOT attribute IO)
#   - both fio jobs run on the SAME CPU so both cgroups share one hctx;
#     per-hctx WDRR cannot arbitrate tenants that live on different hctxes
#
# Run inside the virtme-ng VM (root):
#   tests/arb_wdrr_accept.sh
# Exit: 0 = PASS, 1 = FAIL/BLOCKED
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOADER_DIR="$BASE_DIR/userspace/build"
BT="$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
DEVICE="${TARGET_DEVICE:-nvme0n1}"
CG_ROOT="/sys/fs/cgroup"
RUNTIME="${WDRR_RUNTIME:-10}"
QD="${WDRR_QD:-512}"
PIN_CPU="${WDRR_PIN_CPU:-1}"
RESULTS_DIR="${RESULTS_DIR:-$BASE_DIR/results/arb_wdrr_accept_$(date +%Y%m%d_%H%M%S)}"

mkdir -p "$RESULTS_DIR"
PASS=0; FAIL=0; BLOCKED=0
R_FILE="$RESULTS_DIR/results.txt"
: > "$R_FILE"

log()  { printf '[arb_wdrr_accept] %s\n' "$*" >&2; }
pass() { log "PASS: $1"; echo "PASS: $1" >> "$R_FILE"; PASS=$((PASS+1)); }
fail() { log "FAIL: $1: $2"; echo "FAIL: $1: $2" >> "$R_FILE"; FAIL=$((FAIL+1)); }
blocked() { log "BLOCKED: $1: $2"; echo "BLOCKED: $1: $2" >> "$R_FILE"; BLOCKED=$((BLOCKED+1)); }

log "=== arb_wdrr acceptance START ==="
log "DEVICE=$DEVICE RESULTS=$RESULTS_DIR runtime=${RUNTIME}s qd=$QD pin_cpu=$PIN_CPU"
log "  rig: nr_requests=$(cat /sys/block/$DEVICE/queue/nr_requests 2>/dev/null) hctx0_tags=$(cat /sys/block/$DEVICE/mq/0/nr_tags 2>/dev/null)"

command -v fio >/dev/null 2>&1 || { log "fio not found"; exit 1; }
mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf || { log "cannot mount bpffs"; exit 1; }
echo '+io' > "$CG_ROOT/cgroup.subtree_control" 2>/dev/null || true
mkdir -p "$CG_ROOT/cgA" "$CG_ROOT/cgB" 2>/dev/null || true

cleanup() {
    pkill -TERM -f 'loader_arb_wdrr\|loader_q_fifo' 2>/dev/null || true
    sleep 2
    pkill -9 -f 'loader_arb_wdrr\|loader_q_fifo' 2>/dev/null || true
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

make_fio_job() { # name bs
    cat > "$RESULTS_DIR/$1.fio" <<FEOF
[job]
filename=/dev/$DEVICE
rw=randread
bs=$2
ioengine=libaio
direct=1
iodepth=$QD
time_based=1
runtime=$RUNTIME
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
    "$LOADER_DIR/loader_arb_wdrr" > "$1" 2>&1 &
}

wait_arbiter_strict() { # seconds; polls dmesg for the reg line
    local timeout="$1" i
    for ((i = 0; i < timeout; i++)); do
        if dmesg 2>/dev/null | grep -q "arbiter 'wdrr_arbiter'.*arb_mode=1"; then
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
if timeout 120 python3 "$BASE_DIR/tests/arb_wdrr_model.py" > "$RESULTS_DIR/model.txt" 2>&1; then
    pass "model_self_test"
else
    fail "model_self_test" "$(tail -2 "$RESULTS_DIR/model.txt" | tr '\n' ' ')"
    log "=== arb_wdrr acceptance END: FAIL (model) ==="
    echo "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED" >> "$R_FILE"
    exit 1
fi

# ================================================================ Case 1

log "--- Case 1: weight 100:300, 4K randread, expect ratio ~3 ---"
cleanup
set_scx_io || { blocked "wdrr_c1" "no scx_io"; }
dmesg -C 2>/dev/null || true

start_arbiter "$RESULTS_DIR/c1_arb.log"
ARB_PID=$!
if ! wait_arbiter_strict 10; then
    fail "c1_strict_armed" "no 'arb_mode=1' in dmesg after arbiter attach"
    dmesg 2>/dev/null | grep 'iosched: arbiter' | tail -2 > "$RESULTS_DIR/c1_arb_dmesg.txt" || true
else
    pass "c1_strict_armed"
fi

CGA_ID=$(stat -c %i "$CG_ROOT/cgA")
CGB_ID=$(stat -c %i "$CG_ROOT/cgB")
log "  cgA id=$CGA_ID cgB id=$CGB_ID"

"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgA" --arb-weight 100 > "$RESULTS_DIR/c1_cgA.log" 2>&1 &
QA_PID=$!
sleep 2
"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgB" --arb-weight 300 > "$RESULTS_DIR/c1_cgB.log" 2>&1 &
QB_PID=$!
sleep 2

if ! wait_registered "$CGA_ID" "$CGB_ID" 10; then
    blocked "wdrr_c1" "policies not registered in policy_meta"
    kill $ARB_PID $QA_PID $QB_PID 2>/dev/null || true
else
    make_fio_job "c1" "4k"
    dump_tenant_state "$RESULTS_DIR/c1_ts_before.json"

    run_fio_in_cg "$CG_ROOT/cgA" "$RESULTS_DIR/c1.fio" "$RESULTS_DIR/c1_a.json" &
    FA=$!
    run_fio_in_cg "$CG_ROOT/cgB" "$RESULTS_DIR/c1.fio" "$RESULTS_DIR/c1_b.json" &
    FB=$!
    wait $FA $FB

    dmesg > "$RESULTS_DIR/c1_dmesg.txt" 2>/dev/null || true
    dump_tenant_state "$RESULTS_DIR/c1_ts_after.json"

    read IOS_A BYTES_A <<< "$(get_fio_stat "$RESULTS_DIR/c1_a.json")"
    read IOS_B BYTES_B <<< "$(get_fio_stat "$RESULTS_DIR/c1_b.json")"
    log "  cgA(w=100): ios=$IOS_A bytes=$BYTES_A"
    log "  cgB(w=300): ios=$IOS_B bytes=$BYTES_B"

    if [ "$BYTES_A" -gt 0 ] && [ "$BYTES_B" -gt 0 ]; then
        R=$(python3 -c "print(f'{$BYTES_B/$BYTES_A:.3f}')" 2>/dev/null || echo 0)
        log "  ratio B/A = $R (expected ~3.0, window [2.0, 4.0])"
        if python3 -c "exit(0 if 2.0 <= float('$R') <= 4.0 else 1)" 2>/dev/null; then
            pass "wdrr_c1_weight_ratio (B/A=$R)"
        else
            fail "wdrr_c1_weight_ratio" "B/A=$R outside [2.0,4.0]"
            [ "$R" != "0" ] && python3 -c "exit(0 if float('$R') < 1.5 else 1)" 2>/dev/null && \
                log "  HINT: ratio ~=1 means the scheduler queue never backlogged (device not saturated). Run with OSEXT_VM_BPS_THROTTLE=200000000"
        fi
    else
        fail "wdrr_c1_weight_ratio" "zero bytes A=$BYTES_A B=$BYTES_B (cgroup attribution broken?)"
    fi

    # state conservation + charge coverage
    N=$(tenant_entries "$RESULTS_DIR/c1_ts_after.json")
    if [ "$N" -ge 2 ]; then
        pass "c1_tenant_state_entries (n=$N)"
    else
        fail "c1_tenant_state_entries" "n=$N (inserted_request hook not firing?)"
    fi

    DA=$(tenant_stat "$RESULTS_DIR/c1_ts_after.json" "$CGA_ID" dispatches)
    DB=$(tenant_stat "$RESULTS_DIR/c1_ts_after.json" "$CGB_ID" dispatches)
    if [ "$DA" -gt 0 ] && [ "$DB" -gt 0 ]; then
        pass "c1_tenant_dispatches (A=$DA B=$DB)"
    else
        fail "c1_tenant_dispatches" "A=$DA B=$DB"
    fi

    NET_A=$(python3 -c "print($(tenant_stat "$RESULTS_DIR/c1_ts_after.json" "$CGA_ID" completed_bytes) - $(tenant_stat "$RESULTS_DIR/c1_ts_after.json" "$CGA_ID" requeued_bytes))" 2>/dev/null || echo 0)
    NET_B=$(python3 -c "print($(tenant_stat "$RESULTS_DIR/c1_ts_after.json" "$CGB_ID" completed_bytes) - $(tenant_stat "$RESULTS_DIR/c1_ts_after.json" "$CGB_ID" requeued_bytes))" 2>/dev/null || echo 0)
    log "  charged: A=$NET_A B=$NET_B vs fio: A=$BYTES_A B=$BYTES_B"
    COV_OK=$(python3 -c "
a, fa, b, fb = $NET_A, $BYTES_A, $NET_B, $BYTES_B
ok = fa * 0.98 <= a <= fa * 1.02 and fb * 0.98 <= b <= fb * 1.02
print(1 if ok else 0)
" 2>/dev/null || echo 0)
    if [ "$COV_OK" = "1" ]; then
        pass "c1_charge_coverage (all dispatches arbiter-selected, no fallback)"
    else
        fail "c1_charge_coverage" "charged A=$NET_A B=$NET_B vs fio A=$BYTES_A B=$BYTES_B"
    fi

    if check_kernel_stable "$RESULTS_DIR/c1_dmesg.txt"; then
        pass "c1_kernel_stable"
    else
        fail "c1_kernel_stable" "dmesg errors"
    fi

    kill $ARB_PID $QA_PID $QB_PID 2>/dev/null || true
    sleep 2
fi

# ================================================================ Case 2

log "--- Case 2: equal weights, A=4K B=128K, expect bytes ~1:1 ---"
cleanup
set_scx_io || { blocked "wdrr_c2" "no scx_io"; }
dmesg -C 2>/dev/null || true

start_arbiter "$RESULTS_DIR/c2_arb.log"
ARB_PID=$!
if wait_arbiter_strict 10; then
    pass "c2_strict_armed"
else
    fail "c2_strict_armed" "no 'arb_mode=1' in dmesg after arbiter attach"
fi

"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgA" --arb-weight 100 > "$RESULTS_DIR/c2_cgA.log" 2>&1 &
QA_PID=$!
sleep 2
"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgB" --arb-weight 100 > "$RESULTS_DIR/c2_cgB.log" 2>&1 &
QB_PID=$!
sleep 2

if ! wait_registered "$CGA_ID" "$CGB_ID" 10; then
    blocked "wdrr_c2" "policies not registered"
    kill $ARB_PID $QA_PID $QB_PID 2>/dev/null || true
else
    make_fio_job "c2a" "4k"
    make_fio_job "c2b" "128k"

    run_fio_in_cg "$CG_ROOT/cgA" "$RESULTS_DIR/c2a.fio" "$RESULTS_DIR/c2_a.json" &
    FA=$!
    run_fio_in_cg "$CG_ROOT/cgB" "$RESULTS_DIR/c2b.fio" "$RESULTS_DIR/c2_b.json" &
    FB=$!
    wait $FA $FB

    dmesg > "$RESULTS_DIR/c2_dmesg.txt" 2>/dev/null || true
    dump_tenant_state "$RESULTS_DIR/c2_ts_after.json"

    read IOS_A BYTES_A <<< "$(get_fio_stat "$RESULTS_DIR/c2_a.json")"
    read IOS_B BYTES_B <<< "$(get_fio_stat "$RESULTS_DIR/c2_b.json")"
    log "  cgA(4K):  ios=$IOS_A bytes=$BYTES_A"
    log "  cgB(128K): ios=$IOS_B bytes=$BYTES_B"

    if [ "$BYTES_A" -gt 0 ] && [ "$BYTES_B" -gt 0 ]; then
        R=$(python3 -c "print(f'{$BYTES_A/$BYTES_B:.3f}')" 2>/dev/null || echo 0)
        log "  bytes ratio A/B = $R (expected ~1.0, window [0.7, 1.3])"
        if python3 -c "exit(0 if 0.7 <= float('$R') <= 1.3 else 1)" 2>/dev/null; then
            pass "wdrr_c2_byte_fairness (A/B=$R)"
        else
            fail "wdrr_c2_byte_fairness" "A/B=$R outside [0.7,1.3]"
        fi

        RR=$(python3 -c "print(f'{$IOS_A/$IOS_B:.2f}')" 2>/dev/null || echo 0)
        log "  request ratio A/B = $RR (must NOT be ~1; expected ~32)"
        if python3 -c "exit(0 if float('$RR') > 8 else 1)" 2>/dev/null; then
            pass "wdrr_c2_not_request_fair (req=$RR)"
        else
            fail "wdrr_c2_not_request_fair" "req=$RR ~= 1: request-count fairness, WDRR semantics broken"
        fi
    else
        fail "wdrr_c2_byte_fairness" "zero bytes"
    fi

    NET_A=$(python3 -c "print($(tenant_stat "$RESULTS_DIR/c2_ts_after.json" "$CGA_ID" completed_bytes) - $(tenant_stat "$RESULTS_DIR/c2_ts_after.json" "$CGA_ID" requeued_bytes))" 2>/dev/null || echo 0)
    NET_B=$(python3 -c "print($(tenant_stat "$RESULTS_DIR/c2_ts_after.json" "$CGB_ID" completed_bytes) - $(tenant_stat "$RESULTS_DIR/c2_ts_after.json" "$CGB_ID" requeued_bytes))" 2>/dev/null || echo 0)
    log "  charged: A=$NET_A B=$NET_B vs fio: A=$BYTES_A B=$BYTES_B"
    COV_OK=$(python3 -c "
a, fa, b, fb = $NET_A, $BYTES_A, $NET_B, $BYTES_B
ok = fa * 0.98 <= a <= fa * 1.02 and fb * 0.98 <= b <= fb * 1.02
print(1 if ok else 0)
" 2>/dev/null || echo 0)
    if [ "$COV_OK" = "1" ]; then
        pass "c2_charge_coverage"
    else
        fail "c2_charge_coverage" "charged A=$NET_A B=$NET_B vs fio A=$BYTES_A B=$BYTES_B"
    fi

    if check_kernel_stable "$RESULTS_DIR/c2_dmesg.txt"; then
        pass "c2_kernel_stable"
    else
        fail "c2_kernel_stable" "dmesg errors"
    fi

    kill $ARB_PID $QA_PID $QB_PID 2>/dev/null || true
    sleep 2
fi

# ================================================================ summary

cleanup
rmdir "$CG_ROOT/cgA" "$CG_ROOT/cgB" 2>/dev/null || true

log "=== arb_wdrr acceptance END: PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED ==="
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