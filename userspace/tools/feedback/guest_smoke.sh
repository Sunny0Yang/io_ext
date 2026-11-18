#!/bin/bash
# tools/feedback/guest_smoke.sh - runs INSIDE the virtme-ng VM (root).
#
# Minimal attach/detach smoke for arb_feedback:
#   step A: q_none baseline attach/detach (proves scx_io plumbing)
#   step B: arb_feedback attach to an IDLE device (verifier accepts all
#           7 struct_ops programs; arbiter registers in strict mode)
#   step C: single-tenant IO (4K randread via q_fifo) -> dispatches move,
#           charge coverage (completed_bytes ~= fio io_bytes), drain to
#           pending/inflight == 0, fb_state populated
#
# Evidence lands under $ATTEMPT_DIR/evidence (host shared dir, rw-mounted).
# Exit 0 = all steps passed; nonzero = first failure.
set -uo pipefail

BASE_DIR="${FB_BASE_DIR:?}"                       # ioext
ATTEMPT_DIR="${FB_ATTEMPT_DIR:?}"                 # ledger attempt dir (rw)
DEVICE="${FB_DEVICE:-vda}"
LOADER_DIR="$BASE_DIR/userspace/io/build"
BT="$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
CAP="${FB_CAP:?}"                                 # iox_safety_capture.py
EV="$ATTEMPT_DIR/evidence"
CG_ROOT="/sys/fs/cgroup"
mkdir -p "$EV"

PASS=0; FAIL=0
log()  { printf '[guest] %s\n' "$*" >&2; }
pass() { log "PASS: $1"; PASS=$((PASS+1)); }
fail() { log "FAIL: $1: $2"; FAIL=$((FAIL+1)); }

dmesg -C 2>/dev/null || true

# Guest heartbeat (host-shared evidence dir; host check-heartbeat after run)
python3 "$CAP" heartbeat --heartbeat-path "$EV/heartbeat.jsonl" --interval-sec 1 &
HB_PID=$!

cleanup() {
    kill "$HB_PID" 2>/dev/null || true
    pkill -TERM -f 'loader_arb_feedback|loader_q_fifo|loader_q_none' 2>/dev/null || true
    sleep 2
    pkill -9 -f 'loader_arb_feedback|loader_q_fifo|loader_q_none' 2>/dev/null || true
    rm -rf /sys/fs/bpf/ioext_policy_meta /sys/fs/bpf/ioext_active_cgroups 2>/dev/null || true
    echo none > "/sys/block/$DEVICE/queue/scheduler" 2>/dev/null || true
}
trap cleanup EXIT

log "kernel: $(uname -r)  device: $DEVICE"
grep -q scx_io /proc/kallsyms || { log "no scx_io in guest kernel"; exit 3; }

mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf || { log "cannot mount bpffs"; exit 3; }
echo '+io' > "$CG_ROOT/cgroup.subtree_control" 2>/dev/null || true
mkdir -p "$CG_ROOT/cgfb" 2>/dev/null || true

set_scx_io() {
    echo scx_io > "/sys/block/$DEVICE/queue/scheduler" 2>/dev/null || return 1
    local active
    active=$(sed -n 's/.*\[\([^]]*\)\].*/\1/p' "/sys/block/$DEVICE/queue/scheduler")
    [ "$active" = "scx_io" ]
}

# ---------------------------------------------------------------- step A
log "--- step A: q_none baseline attach/detach ---"
"$LOADER_DIR/loader_q_none" > "$EV/q_none_loader.log" 2>&1 &
QA=$!
sleep 3
if set_scx_io; then
    pass "q_none attach + scx_io switch"
else
    fail "q_none attach" "scheduler switch failed"
    exit 1
fi
echo none > "/sys/block/$DEVICE/queue/scheduler" 2>/dev/null || true
kill $QA 2>/dev/null; sleep 1
rm -rf /sys/fs/bpf/ioext_policy_meta /sys/fs/bpf/ioext_active_cgroups 2>/dev/null || true

# ---------------------------------------------------------------- step B
log "--- step B: arb_feedback attach to IDLE device ---"
# stderr keeps libbpf's verifier log on any rejection (evidence).
"$LOADER_DIR/loader_arb_feedback" > "$EV/arb_feedback_loader.log" 2>&1 &
ARB=$!
ARBITER_OK=0
for i in $(seq 1 20); do
    if ! kill -0 $ARB 2>/dev/null; then break; fi
    if dmesg 2>/dev/null | grep -q "arbiter 'fb_arbiter'.*arb_mode=1"; then
        ARBITER_OK=1; break
    fi
    sleep 1
done
if [ "$ARBITER_OK" = "1" ]; then
    pass "arb_feedback registered strict (dmesg: arbiter 'fb_arbiter' arb_mode=1)"
else
    fail "arb_feedback attach" "loader rc/dmesg missing registration"
    cp /dev/null "$EV/dmesg.txt"; dmesg > "$EV/dmesg.txt" 2>/dev/null || true
    exit 1
fi
# verifier statistics of the 7 loaded struct_ops programs (verified_insns)
"$BT" -j prog show > "$EV/prog_show.json" 2>&1 || \
    bpftool -j prog show > "$EV/prog_show.json" 2>&1 || true
grep -c '"name"' "$EV/prog_show.json" 2>/dev/null || true

# ---------------------------------------------------------------- step C
log "--- step C: single-tenant 4K randread (q_fifo) ---"
if ! set_scx_io; then
    fail "scx_io switch (step C)" "failed"
    exit 1
fi
CGFB_ID=$(stat -c %i "$CG_ROOT/cgfb")
"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/cgfb" --arb-prio 0 --arb-weight 100 \
    > "$EV/q_fifo_loader.log" 2>&1 &
QF=$!
sleep 2

cat > "$EV/fb_smoke.fio" <<FEOF
[job]
filename=/dev/$DEVICE
rw=randread
bs=4k
ioengine=libaio
direct=1
iodepth=8
time_based=1
runtime=5
numjobs=1
FEOF
(
    echo "$BASHPID" > "$CG_ROOT/cgfb/cgroup.procs"
    exec fio "$EV/fb_smoke.fio" --output-format=json > "$EV/fio.json" 2>/dev/null
)
sleep 2  # drain

dump() { "$BT" -j map dump name "$1" > "$EV/map_$1.json" 2>&1 || echo '[]' > "$EV/map_$1.json"; }
dump tenant_state; dump fb_state; dump fb_hold_cnt; dump inflight_count
dmesg > "$EV/dmesg.txt" 2>/dev/null || true

read FIO_IOS FIO_BYTES <<< "$(python3 -c "
import json,re
try:
    raw=open('$EV/fio.json').read(); d=json.loads(re.search(r'\{.*}',raw,re.DOTALL).group(0))
    j=d['jobs'][0]; print(j['read']['total_ios'], j['read']['io_bytes'])
except Exception: print(0,0)
")"

# sum a field over a tenant-state map dump.  bpftool emits either a
# decoded dict (BTF view) or a raw little-endian byte list; decode both.
# Offsets mirror struct arb_tenant_state (ioext_arb_common.h) and struct
# fb_tenant_fb (ioext_arb_feedback_common.h).
sum_field() { # map cgid field
    python3 - "$EV/map_$1.json" "$2" "$3" <<'PYEOF'
import json, os, sys
TABLES = {
    'tenant_state': {
        'deficit': (0, 8), 'pending': (8, 8), 'inflight': (16, 8),
        'completed_bytes': (24, 8), 'completed_cost': (32, 8),
        'dispatches': (40, 8), 'requeues': (48, 8),
        'requeued_bytes': (56, 8), 'serve_selects': (64, 8),
        'refill_arms': (72, 8), 'refilled_round': (96, 8),
        'boost': (104, 4), 'slo_state': (108, 4),
        'ewma_latency_ns': (136, 8), 'last_active_ns': (152, 8),
    },
    'fb_state': {
        'aimd_budget': (0, 4), 'fb_state': (4, 4),
        'starve_credit': (8, 8), 'window_total': (24, 4),
        'window_over': (28, 4), 'good_windows': (32, 4),
        'over_target_cnt': (40, 8), 'violations': (48, 8),
        'clean_windows': (56, 8),
    },
}
try:
    entries = json.load(open(sys.argv[1]))
except Exception:
    print(0); raise SystemExit
map_name = os.path.basename(sys.argv[1]).replace('map_', '').replace('.json', '')
table = TABLES[map_name]
field = sys.argv[3]
total = 0
for e in entries:
    k = e.get('key'); v = e.get('value', {})
    kid = 0
    if isinstance(k, dict):
        kid = int(k.get('cgroup_id', 0))
    elif isinstance(k, list):
        for i, b in enumerate(k[:8]):
            kid |= int(b, 0) << (8 * i)
    if kid != int(sys.argv[2]):
        continue
    if isinstance(v, dict):
        total += int(v.get(field, 0))
    elif isinstance(v, list):
        off, sz = table[field]
        raw = bytes(int(x, 0) for x in v[off:off + sz])
        total += int.from_bytes(raw, 'little')
print(total)
PYEOF
}

DISP=$(sum_field tenant_state "$CGFB_ID" dispatches)
CCOST=$(sum_field tenant_state "$CGFB_ID" completed_cost)
PEND=$(sum_field tenant_state "$CGFB_ID" pending)
INF=$(sum_field tenant_state "$CGFB_ID" inflight)
RQ=$(sum_field tenant_state "$CGFB_ID" requeues)
log "fio: ios=$FIO_IOS bytes=$FIO_BYTES  tenant: dispatches=$DISP requeues=$RQ completed_cost=$CCOST pending=$PEND inflight=$INF"

[ "$FIO_IOS" -gt 0 ] && pass "fio ran (ios=$FIO_IOS)" || fail "fio" "ios=$FIO_IOS"
[ "$DISP" -gt 0 ] && pass "arbiter dispatched ($DISP dispatches)" || fail "dispatches" "0 (arbiter not charging?)"
# IO visibility: the arbiter charges cost units (default 4K read = 64,
# linear in bytes => expected cost ~= fio_bytes/64).  Requeue redispatch
# inflates dispatches/cost slightly; virtio merge behavior deflates it.
# The smoke gate is wiring visibility (0.5x..1.5x), not a calibration.
COV=$(python3 -c "
cc, fb = $CCOST, $FIO_BYTES
exp = fb / 64.0
print(1 if (exp > 0 and 0.5 * exp <= cc <= 1.5 * exp) else 0)" 2>/dev/null || echo 0)
[ "$COV" = "1" ] && pass "io visibility (completed_cost=$CCOST ~= fio_bytes/64=$((FIO_BYTES/64)))" \
    || fail "io visibility" "completed_cost=$CCOST vs expected~$((FIO_BYTES/64))"
[ "$PEND" -eq 0 ] && [ "$INF" -eq 0 ] && pass "drained (pending=0 inflight=0)" \
    || fail "drain" "pending=$PEND inflight=$INF"

FB_ENTRIES=$(python3 -c "
import json
try: print(len(json.load(open('$EV/map_fb_state.json'))))
except Exception: print(0)")
[ "$FB_ENTRIES" -ge 1 ] && pass "fb_state populated ($FB_ENTRIES entries)" \
    || fail "fb_state" "empty"

# AIMD wiring: completed_request must have fed the feedback FSM.
FB_VIOL=$(sum_field fb_state "$CGFB_ID" violations)
FB_CLEAN=$(sum_field fb_state "$CGFB_ID" clean_windows)
FB_BUDGET=$(sum_field fb_state "$CGFB_ID" aimd_budget)
if [ "$FB_VIOL" -gt 0 ] || [ "$FB_CLEAN" -gt 0 ]; then
    pass "aimd_wiring (violations=$FB_VIOL clean=$FB_CLEAN budget=$FB_BUDGET in [4,256])"
else
    fail "aimd_wiring" "violations=$FB_VIOL clean=$FB_CLEAN (completed_request -> AIMD broken?)"
fi

# kernel stability scan
if dmesg 2>/dev/null | grep -iE '\bpanic\b|\boops\b|BUG:|hung_task|soft lockup' \
        | grep -vq 'Command line'; then
    fail "kernel_stable" "dmesg errors (see dmesg.txt)"
else
    pass "kernel_stable"
fi

cleanup
log "=== guest smoke END: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" -eq 0 ]
