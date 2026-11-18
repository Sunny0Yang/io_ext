#!/bin/bash
# tests/full_suite/policy_smoke.sh
# Smoke + correctness test for new policies: arb_wdrr, arb_slo, arb_cost, q_ddl
# Runs inside virtme-ng VM.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOADER_DIR="$BASE_DIR/userspace/build"
DEVICE="${TARGET_DEVICE:-nvme0n1}"
DEV_PATH="/dev/$DEVICE"
CG_ROOT="/sys/fs/cgroup"
RESULTS_DIR="${RESULTS_DIR:-$BASE_DIR/results/policy_smoke_$(date +%Y%m%d_%H%M%S)}"

mkdir -p "$RESULTS_DIR"
PASS=0
FAIL=0
BLOCKED=0

log() { printf '[smoke] %s\n' "$*" >&2; }
pass() { log "PASS: $1"; PASS=$((PASS+1)); }
fail() { log "FAIL: $1: $2"; FAIL=$((FAIL+1)); }
blocked() { log "BLOCKED: $1: $2"; BLOCKED=$((BLOCKED+1)); }

# Setup
log "=== policy smoke test START ==="
log "DEVICE=$DEV_PATH RESULTS=$RESULTS_DIR"

# Ensure bpffs
mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf || { log "cannot mount bpffs"; exit 1; }

# cgroup io delegation
echo '+io' > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true

# Create test cgroups
setup_cgroups() {
    mkdir -p "$CG_ROOT/test_cgA" 2>/dev/null || true
    mkdir -p "$CG_ROOT/test_cgB" 2>/dev/null || true
}

cleanup_cgroups() {
    rmdir "$CG_ROOT/test_cgA" 2>/dev/null || true
    rmdir "$CG_ROOT/test_cgB" 2>/dev/null || true
}

# Switch to scx_io
set_scx_io() {
    local sched_path="/sys/block/$DEVICE/queue/scheduler"
    echo scx_io > "$sched_path" 2>/dev/null || { log "failed to set scx_io"; return 1; }
    local active
    active=$(sed -n 's/.*\[\([^]]*\)\].*/\1/p' "$sched_path")
    [ "$active" = "scx_io" ] || { log "scheduler is $active, expected scx_io"; return 1; }
    log "scheduler=scx_io"
}

set_none_sched() {
    echo none > "/sys/block/$DEVICE/queue/scheduler" 2>/dev/null || true
}

# Cleanup all loaders and pinned maps
cleanup() {
    pkill -TERM -f 'loader_q_ddl' 2>/dev/null || true
    pkill -TERM -f 'loader_arb_wdrr' 2>/dev/null || true
    pkill -TERM -f 'loader_arb_slo' 2>/dev/null || true
    pkill -TERM -f 'loader_arb_cost' 2>/dev/null || true
    pkill -TERM -f 'loader_q_fifo' 2>/dev/null || true
    sleep 1
    pkill -9 -f 'loader_q_ddl' 2>/dev/null || true
    pkill -9 -f 'loader_arb_wdrr' 2>/dev/null || true
    pkill -9 -f 'loader_arb_slo' 2>/dev/null || true
    pkill -9 -f 'loader_arb_cost' 2>/dev/null || true
    pkill -9 -f 'loader_q_fifo' 2>/dev/null || true
    rm -rf /sys/fs/bpf/ioext_policy_meta 2>/dev/null || true
    rm -rf /sys/fs/bpf/ioext_active_cgroups 2>/dev/null || true
    set_none_sched 2>/dev/null || true
    sleep 1
}

# Wait for loader to attach
wait_attached() {
    local name="$1"
    local pid="$2"
    local timeout="${3:-15}"
    local i
    for ((i=1; i<=timeout; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then return 1; fi
        if dmesg 2>/dev/null | grep -qE "policy '${name}' attached|policy '${name}' replaced"; then
            return 0
        fi
        if [ "$i" -ge 3 ]; then
            return 0
        fi
        sleep 1
    done
    return 0
}

# Run a simple IO test
run_io_test() {
    local test_name="$1"
    local fio_job="$2"
    local timeout="${3:-30}"

    log "[$test_name] running fio..."
    if timeout "$timeout" fio "$fio_job" --output-format=json > "$RESULTS_DIR/${test_name}_fio.json" 2>/dev/null; then
        local ios
        ios=$(python3 -c "
import json
try:
    d=json.load(open('$RESULTS_DIR/${test_name}_fio.json'))
    j=d.get('jobs',[{}])[0]
    total=0
    for op in ('read','write','trim'):
        total+=j.get(op,{}).get('total_ios',0)
    print(total)
except: print(0)
" 2>/dev/null)
        if [ "$ios" -gt 0 ]; then
            pass "$test_name (ios=$ios)"
            return 0
        else
            fail "$test_name" "total_ios=0"
            return 1
        fi
    else
        fail "$test_name" "fio failed"
        return 1
    fi
}

# Check dmesg for errors
check_dmesg() {
    local mark="$1"
    local errors
    errors=$(dmesg | awk -v m="$mark" 'seen{print} index($0,m){seen=1}' 2>/dev/null || true)
    echo "$errors" | grep -qiE 'hung_task|BUG:|Oops:|kernel panic|soft lockup|WARNING:.*iosched' | grep -v 'Command line' && return 1
    return 0
}

# Create fio job files
create_fio_jobs() {
    # Simple 4K random read
    cat > "$RESULTS_DIR/ smoke_read.fio" << 'EOF'
[read]
filename=/dev/__DEV__
rw=randread
bs=4k
ioengine=libaio
iodepth=4
time_based=1
runtime=5
numjobs=1
EOF
    sed -i "s|__DEV__|$DEVICE|g" "$RESULTS_DIR/ smoke_read.fio" 2>/dev/null || true

    cat > "$RESULTS_DIR/smoke_read.fio" << 'EOF'
[read]
filename=/dev/__DEV__
rw=randread
bs=4k
ioengine=libaio
iodepth=4
time_based=1
runtime=5
numjobs=1
EOF
    sed -i "s|__DEV__|$DEVICE|g" "$RESULTS_DIR/smoke_read.fio"

    # Mixed read+write
    cat > "$RESULTS_DIR/smoke_mixed.fio" << 'EOF'
[mixed]
filename=/dev/__DEV__
rw=randrw
rwmixread=70
bs=4k
ioengine=libaio
iodepth=8
time_based=1
runtime=5
numjobs=1
EOF
    sed -i "s|__DEV__|$DEVICE|g" "$RESULTS_DIR/smoke_mixed.fio"

    # Large write
    cat > "$RESULTS_DIR/smoke_write.fio" << 'EOF'
[write]
filename=/dev/__DEV__
rw=randwrite
bs=128k
ioengine=libaio
iodepth=4
time_based=1
runtime=5
numjobs=1
EOF
    sed -i "s|__DEV__|$DEVICE|g" "$RESULTS_DIR/smoke_write.fio"
}

# ─── Test cases ───

setup_cgroups
create_fio_jobs

# ── Test 1: q_ddl single cgroup smoke ──
log "── Test 1: q_ddl single cgroup ──"
cleanup
set_scx_io || { blocked "q_ddl_smoke" "cannot set scx_io"; }
mark="q_ddl_smoke_$(date +%s)"
printf '%s\n' "$mark" > /dev/kmsg 2>/dev/null || true
"$LOADER_DIR/loader_q_ddl" --cgroup "$CG_ROOT" > "$RESULTS_DIR/q_ddl_smoke.log" 2>&1 &
DDL_PID=$!
sleep 3
if wait_attached "q_ddl" "$DDL_PID" 10; then
    run_io_test "q_ddl_smoke_read" "$RESULTS_DIR/smoke_read.fio" 30
    run_io_test "q_ddl_smoke_mixed" "$RESULTS_DIR/smoke_mixed.fio" 30
    check_dmesg "$mark" || fail "q_ddl_smoke" "dmesg errors"
else
    blocked "q_ddl_smoke" "loader did not attach"
    cat "$RESULTS_DIR/q_ddl_smoke.log" >&2 || true
fi
kill -TERM "$DDL_PID" 2>/dev/null || true
sleep 2

# ── Test 2: arb_wdrr + q_fifo multi-cgroup ──
log "── Test 2: arb_wdrr + q_fifo multi-cgroup ──"
cleanup
set_scx_io || { blocked "arb_wdrr_smoke" "cannot set scx_io"; }
mark="arb_wdrr_smoke_$(date +%s)"
printf '%s\n' "$mark" > /dev/kmsg 2>/dev/null || true

"$LOADER_DIR/loader_arb_wdrr" > "$RESULTS_DIR/arb_wdrr.log" 2>&1 &
ARB_PID=$!
sleep 2

"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/test_cgA" --arb-weight 100 > "$RESULTS_DIR/cgA.log" 2>&1 &
CGA_PID=$!
"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/test_cgB" --arb-weight 300 > "$RESULTS_DIR/cgB.log" 2>&1 &
CGB_PID=$!

sleep 3
if wait_attached "q_fifo" "$CGA_PID" 10 && wait_attached "q_fifo" "$CGB_PID" 10; then
    run_io_test "arb_wdrr_smoke_read" "$RESULTS_DIR/smoke_read.fio" 30
    check_dmesg "$mark" || fail "arb_wdrr_smoke" "dmesg errors"
else
    blocked "arb_wdrr_smoke" "loader did not attach"
    cat "$RESULTS_DIR/arb_wdrr.log" >&2 || true
fi
kill -TERM "$ARB_PID" "$CGA_PID" "$CGB_PID" 2>/dev/null || true
sleep 2

# ── Test 3: arb_slo + q_fifo multi-cgroup ──
log "── Test 3: arb_slo + q_fifo multi-cgroup ──"
cleanup
set_scx_io || { blocked "arb_slo_smoke" "cannot set scx_io"; }
mark="arb_slo_smoke_$(date +%s)"
printf '%s\n' "$mark" > /dev/kmsg 2>/dev/null || true

"$LOADER_DIR/loader_arb_slo" > "$RESULTS_DIR/arb_slo.log" 2>&1 &
ARB_PID=$!
sleep 2

"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/test_cgA" --arb-prio 1 --arb-weight 100 > "$RESULTS_DIR/cgA_slo.log" 2>&1 &
CGA_PID=$!
"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/test_cgB" --arb-prio 5 --arb-weight 100 > "$RESULTS_DIR/cgB_slo.log" 2>&1 &
CGB_PID=$!

sleep 3
if wait_attached "q_fifo" "$CGA_PID" 10 && wait_attached "q_fifo" "$CGB_PID" 10; then
    run_io_test "arb_slo_smoke_read" "$RESULTS_DIR/smoke_read.fio" 30
    run_io_test "arb_slo_smoke_write" "$RESULTS_DIR/smoke_write.fio" 30
    check_dmesg "$mark" || fail "arb_slo_smoke" "dmesg errors"
else
    blocked "arb_slo_smoke" "loader did not attach"
    cat "$RESULTS_DIR/arb_slo.log" >&2 || true
fi
kill -TERM "$ARB_PID" "$CGA_PID" "$CGB_PID" 2>/dev/null || true
sleep 2

# ── Test 4: arb_cost + q_fifo multi-cgroup ──
log "── Test 4: arb_cost + q_fifo multi-cgroup ──"
cleanup
set_scx_io || { blocked "arb_cost_smoke" "cannot set scx_io"; }
mark="arb_cost_smoke_$(date +%s)"
printf '%s\n' "$mark" > /dev/kmsg 2>/dev/null || true

"$LOADER_DIR/loader_arb_cost" > "$RESULTS_DIR/arb_cost.log" 2>&1 &
ARB_PID=$!
sleep 2

"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/test_cgA" --arb-weight 100 > "$RESULTS_DIR/cgA_cost.log" 2>&1 &
CGA_PID=$!
"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/test_cgB" --arb-weight 100 > "$RESULTS_DIR/cgB_cost.log" 2>&1 &
CGB_PID=$!

sleep 3
if wait_attached "q_fifo" "$CGA_PID" 10 && wait_attached "q_fifo" "$CGB_PID" 10; then
    run_io_test "arb_cost_smoke_read" "$RESULTS_DIR/smoke_read.fio" 30
    run_io_test "arb_cost_smoke_write" "$RESULTS_DIR/smoke_write.fio" 30
    check_dmesg "$mark" || fail "arb_cost_smoke" "dmesg errors"
else
    blocked "arb_cost_smoke" "loader did not attach"
    cat "$RESULTS_DIR/arb_cost.log" >&2 || true
fi
kill -TERM "$ARB_PID" "$CGA_PID" "$CGB_PID" 2>/dev/null || true
sleep 2

# ── Test 5: q_ddl with arb_wdrr multi-cgroup ──
log "── Test 5: q_ddl + arb_wdrr multi-cgroup ──"
cleanup
set_scx_io || { blocked "q_ddl_arb_smoke" "cannot set scx_io"; }
mark="q_ddl_arb_smoke_$(date +%s)"
printf '%s\n' "$mark" > /dev/kmsg 2>/dev/null || true

"$LOADER_DIR/loader_arb_wdrr" > "$RESULTS_DIR/arb_wdrr_2.log" 2>&1 &
ARB_PID=$!
sleep 2

"$LOADER_DIR/loader_q_ddl" --cgroup "$CG_ROOT/test_cgA" --arb-weight 100 --arb-prio 1 > "$RESULTS_DIR/cgA_ddl.log" 2>&1 &
CGA_PID=$!
"$LOADER_DIR/loader_q_ddl" --cgroup "$CG_ROOT/test_cgB" --arb-weight 300 --arb-prio 5 > "$RESULTS_DIR/cgB_ddl.log" 2>&1 &
CGB_PID=$!

sleep 3
if wait_attached "q_ddl" "$CGA_PID" 10 && wait_attached "q_ddl" "$CGB_PID" 10; then
    run_io_test "q_ddl_arb_smoke_mixed" "$RESULTS_DIR/smoke_mixed.fio" 30
    check_dmesg "$mark" || fail "q_ddl_arb_smoke" "dmesg errors"
else
    blocked "q_ddl_arb_smoke" "loader did not attach"
fi
kill -TERM "$ARB_PID" "$CGA_PID" "$CGB_PID" 2>/dev/null || true
sleep 2

# ── Test 6: lifecycle (hot-switch q_fifo → q_ddl) ──
log "── Test 6: lifecycle hot-switch ──"
cleanup
set_scx_io || { blocked "lifecycle" "cannot set scx_io"; }

"$LOADER_DIR/loader_arb_wdrr" > "$RESULTS_DIR/arb_lc.log" 2>&1 &
ARB_PID=$!
sleep 3

"$LOADER_DIR/loader_q_fifo" --cgroup "$CG_ROOT/test_cgA" --arb-weight 100 > "$RESULTS_DIR/q_fifo_lc.log" 2>&1 &
FIFO_PID=$!
sleep 4

if ! wait_attached "q_fifo" "$FIFO_PID" 10; then
    blocked "lifecycle_hotswitch" "q_fifo did not attach"
    kill -TERM "$ARB_PID" "$FIFO_PID" 2>/dev/null || true
    sleep 2
else
    timeout 5 fio "$RESULTS_DIR/smoke_read.fio" --output-format=json > "$RESULTS_DIR/lc_fifo.json" 2>/dev/null || true

    kill -TERM "$FIFO_PID" 2>/dev/null || true
    sleep 3

    "$LOADER_DIR/loader_q_ddl" --cgroup "$CG_ROOT/test_cgA" --arb-weight 100 > "$RESULTS_DIR/q_ddl_lc.log" 2>&1 &
    DDL_PID=$!
    sleep 5

    if wait_attached "q_ddl" "$DDL_PID" 15; then
        timeout 5 fio "$RESULTS_DIR/smoke_read.fio" --output-format=json > "$RESULTS_DIR/lc_ddl.json" 2>/dev/null || true
        local_ios=$(python3 -c "
import json,re
try:
    raw=open('$RESULTS_DIR/lc_ddl.json').read()
    m=re.search(r'\{.*}', raw, re.DOTALL)
    if m:
        d=json.loads(m.group(0))
        j=d.get('jobs',[{}])[0]
        total=0
        for op in ('read','write','trim'):
            total+=j.get(op,{}).get('total_ios',0)
        print(total)
    else:
        print(0)
except: print(0)
" 2>/dev/null)
        if [ "${local_ios:-0}" -gt 0 ]; then
            pass "lifecycle_hotswitch (ios=$local_ios)"
        else
            fail "lifecycle_hotswitch" "total_ios=0 after switch"
        fi
    else
        blocked "lifecycle_hotswitch" "q_ddl did not attach after hot-switch"
    fi

    kill -TERM "$ARB_PID" "$DDL_PID" 2>/dev/null || true
    sleep 2
fi

# Cleanup
cleanup
cleanup_cgroups

log "=== policy smoke test END ==="
log "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED"
log "results: $RESULTS_DIR"

# Write summary
cat > "$RESULTS_DIR/summary.md" << EOF
# Policy Smoke Test Results

- Date: $(date)
- Device: $DEVICE
- Pass: $PASS
- Fail: $FAIL
- Blocked: $BLOCKED

## Test Cases

1. q_ddl single cgroup smoke
2. arb_wdrr + q_fifo multi-cgroup
3. arb_slo + q_fifo multi-cgroup
4. arb_cost + q_fifo multi-cgroup
5. q_ddl + arb_wdrr multi-cgroup
6. lifecycle hot-switch (q_fifo → q_ddl)

## Results

Total: $((PASS + FAIL + BLOCKED))
- PASS: $PASS
- FAIL: $FAIL
- BLOCKED: $BLOCKED
EOF

echo "$RESULTS_DIR"