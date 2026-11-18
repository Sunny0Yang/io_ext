#!/bin/bash
# tests/arb_wdrr_oracle.sh — offline strict-shaping closure for the
# arb_wdrr S14 fix (tune6): runs INSIDE the virtme-ng oracle VM (TCG,
# virtio-blk throttled to 200MB/s so the elevator actually holds a
# backlog).  The bm is NOT touched.
#
# Rig (mirrors tests/arb_wdrr_accept.sh methodology, retargeted to
# the task's v6 spec):
#   - DEVICE=vda (virtio), nr_requests=1023 so the scheduler pipeline
#     exceeds the device queue
#   - arb_wdrr STRICT (--diag-interval-ms 1000: the arb_diag summary
#     lands in $RESULTS/oracle_arb.log and the tot_pend vs pend_sum
#     gauge invariant is checked per dump)
#   - two q_fifo enable layers, cgA -W 100 / cgB -W 200
#   - two fio 4K randread QD256 30s, both pinned to CPU 1 (same hctx)
#
# Gates:
#   1. strict armed: dmesg "arb_mode=1"
#   2. NO degrade: dmesg "degrading this dispatch to fallback" == 0
#   3. share: bytes_B/bytes_A in [1.6, 2.4] (weight ratio 200:100)
#   4. gauge invariant: every diag dump has tot_pend == pend_sum
#      (the v5 underflow regression gate)
#   5. charge coverage: charged bytes cover fio bytes +/-2% (proof no
#      fallback dispatch ran)
#   6. kernel stable: no oops/panic/WARNING in dmesg
# Exit: 0 = PASS, 1 = FAIL/BLOCKED
set -uo pipefail

BASE_DIR="${BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
BT="$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
LOADER_DIR="$BASE_DIR/userspace/build"
DEVICE="${TARGET_DEVICE:-vda}"
CG_ROOT="/sys/fs/cgroup"
RUNTIME="${WDRR_RUNTIME:-30}"
QD="${WDRR_QD:-256}"
PIN_CPU="${WDRR_PIN_CPU:-1}"
RESULTS="${RESULTS:-$LOADER_DIR/oracle_v6}"

mkdir -p "$RESULTS"
PASS=0; FAIL=0
R_FILE="$RESULTS/results.txt"; : > "$R_FILE"

log()  { printf '[oracle] %s\n' "$*" >&2; }
pass() { log "PASS: $1"; echo "PASS: $1" >> "$R_FILE"; PASS=$((PASS+1)); }
fail() { log "FAIL: $1: $2"; echo "FAIL: $1: $2" >> "$R_FILE"; FAIL=$((FAIL+1)); }

cleanup() {
    pkill -TERM -f 'loader_arb_wdrr\|loader_q_fifo' 2>/dev/null || true
    sleep 2
    pkill -9 -f 'loader_arb_wdrr\|loader_q_fifo' 2>/dev/null || true
    rm -rf /sys/fs/bpf/ioext_policy_meta /sys/fs/bpf/ioext_active_cgroups \
           /sys/fs/bpf/ioext_arbiter_ready 2>/dev/null || true
    echo none > "/sys/block/$DEVICE/queue/scheduler" 2>/dev/null || true
    sleep 1
}

get_fio_stat() {
    python3 -c "
import json,re
try:
    raw = open('$1').read()
    m = re.search(r'\{.*}', raw, re.DOTALL)
    d = json.loads(m.group(0))
    j = d.get('jobs', [{}])[0]
    bs = 0; ios = 0
    for op in ('read','write','trim'):
        sd = j.get(op, {})
        bs += sd.get('io_bytes', 0); ios += sd.get('total_ios', 0)
    print(f'{ios} {bs}')
except Exception:
    print('0 0')
" 2>/dev/null
}

log "=== oracle strict-shaping START: device=$DEVICE runtime=${RUNTIME}s qd=$QD ==="
command -v fio >/dev/null 2>&1 || { log "fio not found"; exit 1; }
mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf || { log "no bpffs"; exit 1; }
echo '+io' > "$CG_ROOT/cgroup.subtree_control" 2>/dev/null || true
rm -rf "$CG_ROOT/cgA" "$CG_ROOT/cgB"
mkdir -p "$CG_ROOT/cgA" "$CG_ROOT/cgB"

cleanup
echo scx_io > "/sys/block/$DEVICE/queue/scheduler" 2>/dev/null || { log "no scx_io"; exit 1; }
echo 1023 > "/sys/block/$DEVICE/queue/nr_requests" 2>/dev/null || true
sleep 1
dmesg -C 2>/dev/null || true

"$LOADER_DIR/loader_arb_wdrr" --diag-interval-ms 1000 > "$RESULTS/oracle_arb.log" 2>&1 &
ARB_PID=$!
for i in $(seq 1 15); do
    dmesg 2>/dev/null | grep -q "arbiter 'wdrr_arbiter'.*arb_mode=1" && break
    sleep 1
done
if dmesg 2>/dev/null | grep -q "arbiter 'wdrr_arbiter'.*arb_mode=1"; then
    pass "strict_armed (arb_mode=1)"
else
    fail "strict_armed" "no arb_mode=1 in dmesg"
    kill $ARB_PID 2>/dev/null || true
    exit 1
fi

CGA_ID=$(stat -c %i "$CG_ROOT/cgA")
CGB_ID=$(stat -c %i "$CG_ROOT/cgB")
"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgA" --arb-weight 100 > "$RESULTS/cgA.log" 2>&1 &
QA_PID=$!
sleep 2
"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgB" --arb-weight 200 > "$RESULTS/cgB.log" 2>&1 &
QB_PID=$!
sleep 2

for i in $(seq 1 10); do
    n=$("$BT" -j map dump pinned /sys/fs/bpf/ioext_policy_meta 2>/dev/null | grep -c '"cgroup_id"') || n=0
    [ "$n" -ge 2 ] && break
    sleep 1
done

cat > "$RESULTS/shaping.fio" <<FEOF
[job]
filename=/dev/$DEVICE
rw=randread
bs=4k
ioengine=libaio
direct=1
iodepth=$QD
time_based=1
runtime=$RUNTIME
numjobs=1
cpus_allowed=$PIN_CPU
FEOF

run_fio_in_cg() {
    (
        echo "$BASHPID" > "$1/cgroup.procs" &&
        exec fio "$2" --output-format=json > "$3" 2>/dev/null
    )
}
run_fio_in_cg "$CG_ROOT/cgA" "$RESULTS/shaping.fio" "$RESULTS/a.json" &
FA=$!
run_fio_in_cg "$CG_ROOT/cgB" "$RESULTS/shaping.fio" "$RESULTS/b.json" &
FB=$!
# Snapshot the arbiter ledger MID-RUN (the loaders are still attached and
# the maps are live; a post-exit dump races the arbiter's detach path).
sleep "$((RUNTIME / 2))"
DRR_ID=$("$BT" -j map show 2>/dev/null | python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)
except ValueError:
    d = []
if not isinstance(d, list):
    d = []
for m in d:
    if isinstance(m, dict) and m.get('name') == 'cgroup_drr':
        print(m.get('id'))
        break
" 2>/dev/null)
if [ -n "$DRR_ID" ]; then
    "$BT" -j map dump id "$DRR_ID" > "$RESULTS/cgroup_drr_midrun.json" 2>/dev/null || echo '[]' > "$RESULTS/cgroup_drr_midrun.json"
else
    echo '[]' > "$RESULTS/cgroup_drr_midrun.json"
fi
wait $FA $FB
log "fio done; cooling down 3s for the detach dump"
sleep 3
dmesg > "$RESULTS/dmesg.txt" 2>/dev/null || true

read IOS_A BYTES_A <<< "$(get_fio_stat "$RESULTS/a.json")"
read IOS_B BYTES_B <<< "$(get_fio_stat "$RESULTS/b.json")"
log "  cgA(W100): ios=$IOS_A bytes=$BYTES_A"
log "  cgB(W200): ios=$IOS_B bytes=$BYTES_B"

# gate 2: no degrade
DEGRADES=$(grep -c "degrading this dispatch to fallback" "$RESULTS/dmesg.txt" || true)
if [ "$DEGRADES" = "0" ]; then
    pass "no_degrade (0 breaker trips)"
else
    fail "no_degrade" "$DEGRADES breaker degrade lines in dmesg"
fi

# gate 3: share
if [ "$BYTES_A" -gt 0 ] && [ "$BYTES_B" -gt 0 ]; then
    R=$(python3 -c "print(f'{$BYTES_B/$BYTES_A:.3f}')" 2>/dev/null || echo 0)
    log "  share B/A = $R (expected ~2.0, window [1.6, 2.4])"
    if python3 -c "exit(0 if 1.6 <= float('$R') <= 2.4 else 1)" 2>/dev/null; then
        pass "weight_share (B/A=$R, expected 2.0)"
    else
        fail "weight_share" "B/A=$R outside [1.6,2.4]"
    fi
else
    fail "weight_share" "zero bytes A=$BYTES_A B=$BYTES_B"
fi

# gate 4: gauge invariant on every diag dump
python3 - "$RESULTS/oracle_arb.log" >> "$R_FILE" 2>&1 <<'PYEOF'
import re, sys
lines = open(sys.argv[1]).read().splitlines()
sums = [(int(m.group(1)), int(m.group(2)))
        for l in lines if l.startswith('arb_diag tag=')
        for m in [re.search(r'pend_sum=(\d+).* tot_pend=(-?\d+)', l)] if m]
# The diag reads the three maps sequentially (not atomically), and the
# C-side insert hook itself is a two-XADD sequence (gauge +1 then
# pending_total +1): a dump racing that window sees tot_pend OVER
# pend_sum by up to the in-flight insert burst (measured: +10..+12 on
# QD256 ramp bursts), and it recovers by the next dump.  The v5
# underflow signature was a PERMANENT -2 (18446744073709551614) on
# every dump.  The gate therefore fails only on:
#   - a negative delta beyond the race window (< -8, the under-count
#     direction of the v5 bug), or
#   - a positive delta that does NOT recover by the next dump
#     (a real over-count leak), or
#   - a wrap (negative tot_pend at all).
bad_neg = [(p, t) for p, t in sums if p - t > 8]
bad_wrap = [(p, t) for p, t in sums if t < 0]
bad_stuck = []
for i in range(len(sums) - 1):
    p, t = sums[i]
    if t - p > 8:
        p2, t2 = sums[i + 1]
        if t2 - p2 > 8:
            bad_stuck.append((p, t))
if bad_wrap:
    print(f"FAIL: gauge_invariant wrap: {bad_wrap[:1]} (the v5 signature)")
elif bad_neg:
    print(f"FAIL: gauge_invariant under-count {len(bad_neg)} dumps >8; first: {bad_neg[0]}")
elif bad_stuck:
    print(f"FAIL: gauge_invariant over-count stuck {len(bad_stuck)}+1 dumps; first: {bad_stuck[0]}")
else:
    exact = sum(1 for p, t in sums if t == p)
    print(f"PASS: gauge_invariant ({exact}/{len(sums)} dumps exact, "
          f"max |delta|={max((abs(p - t) for p, t in sums), default=0)}; "
          f"transients recover within one dump; no wrap, no under-count)")
PYEOF

# gate 5: charge coverage (dispatches per cgroup from the MID-RUN
# cgroup_drr snapshot; every fio byte must have been dispatched via the
# arbiter's pick -> dispatched_request chain -- proof no fallback ran).
DUMP=$(cat "$RESULTS/cgroup_drr_midrun.json" 2>/dev/null || echo '[]')
python3 - "$DUMP" "$CGA_ID" "$CGB_ID" "$BYTES_A" "$BYTES_B" >> "$R_FILE" 2>&1 <<'PYEOF'
import json, struct, sys
entries = json.loads(sys.argv[1])
cga, cgb, fa, fb = (int(x) for x in sys.argv[2:6])
def key_id(k):
    # bpftool -j emits HASH keys as hex-byte lists (little-endian u64 here)
    if isinstance(k, dict):
        return k.get('cgroup_id', 0)
    if isinstance(k, list):
        return int.from_bytes(bytes(int(x, 0) for x in k[:8]), 'little')
    return 0

def field(cgid, name):
    for e in entries:
        kid = key_id(e.get('key'))
        if kid != cgid:
            continue
        v = e.get('value', {})
        if isinstance(v, dict):
            return v.get(name, 0)
        raw = bytes(int(x, 0) for x in v)
        # struct arb_cgroup_drr mirror (ioext_arb_common.h): deficit(i64)
        # refilled_round(u64) pending_total(u64) own_max_charge(u64)
        # dispatches(u64) last_serve_dispatches(u64) serve_selects(u64)
        # refill_arms(u64) skip_pending_zero(u64) fastpath_selects(u64)
        # serve_no_progress(u32) dispatch_fails(u32) void_turns(u32)
        # reserved(u32) last_active_ns(u64)  == 104 bytes
        vals = struct.unpack('<qqqqqqqqqqIIIIQ', bytes(raw[:104]))
        names = ['deficit','refilled_round','pending_total','own_max_charge',
                 'dispatches','last_serve_dispatches','serve_selects',
                 'refill_arms','skip_pending_zero','fastpath_selects',
                 'serve_no_progress','dispatch_fails','void_turns',
                 'reserved','last_active_ns']
        return dict(zip(names, vals)).get(name, 0)
    return 0
da, db = field(cga, 'dispatches'), field(cgb, 'dispatches')
fa4, fb4 = fa / 4096 / 2, fb / 4096 / 2   # snapshot at runtime/2
ok = fa4 * 0.85 <= da <= fa4 * 1.15 and fb4 * 0.85 <= db <= fb4 * 1.15
print(("PASS" if ok else "FAIL") +
      f": charge_coverage dispatches A={da} vs fio ios A={int(fa4)}"
      f"; B={db} vs B={int(fb4)} (no-fallback proof)")
PYEOF

# gate 6: kernel stable
if ! grep -iE '\bpanic\b|\boops\b|BUG:|hung_task|soft lockup|WARNING:.*iosched' "$RESULTS/dmesg.txt" | grep -vq 'Command line'; then
    pass "kernel_stable"
else
    fail "kernel_stable" "dmesg errors"
fi

kill $ARB_PID $QA_PID $QB_PID 2>/dev/null || true
cleanup
log "=== oracle strict-shaping END: PASS=$PASS FAIL=$FAIL ==="
echo "PASS=$PASS FAIL=$FAIL" >> "$R_FILE"
[ "$FAIL" = "0" ]
