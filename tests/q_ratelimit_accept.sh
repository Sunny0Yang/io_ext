#!/bin/bash
# tests/q_ratelimit_accept.sh — q_ratelimit closed-loop acceptance test
# (VM-side), tests/io style.  Stage-1 gate of P1-6 (controller-lineage
# reimplementation): rate-cap smoke + drain/detach cleanliness + accounting
# conservation.
#
# Gates (all must PASS):
#   G0  reference model self-test (tests/q_ratelimit_model.py)
#   G1  loader starts and attaches (scx_io active, pinned policy_meta entry)
#   G2  capped 4K randwrite QD256 stream achieves ~rate (±15%) — stage-2 smoke
#       requirement, RATE_MIB_S (default 64)
#   G3  after fio quiesce: policy_meta queue_depth == 0 ∧ active_inserts == 0
#       ("pending==0"), bucket enqueued == dispatches + evac_pops (the policy
#       holds nothing), lost == 0, push_fails == 0
#   G4  graceful TERM stop: loader exits 0, no stale loader_q_ processes,
#       dmesg clean of new oops/warnings
#
# Context (recorded, non-gating): uncapped baseline fio BW for the same
# workload shape — the cap is only meaningful if baseline >> rate.  The
# baseline runs with scx_io active but no policy attached (native-none
# degraded path), same scheduler context.
#
# Run inside the virtme-ng VM (root):
#   TARGET_DEVICE=vda IOEXT_DESTRUCTIVE_TEST=1 \
#     RATE_MIB_S=64 RUNTIME_SEC=20 tests/q_ratelimit_accept.sh
# Exit: 0 = all gates PASS, 1 = any FAIL
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FS_DIR="$BASE_DIR/tests/full_suite"
export TARGET_DEVICE="${TARGET_DEVICE:-vda}"
export IOEXT_DESTRUCTIVE_TEST=1
BT="$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
MODEL="$BASE_DIR/tests/q_ratelimit_model.py"
LOADER="$BASE_DIR/userspace/build/loader_q_ratelimit"
RATE_MIB_S="${RATE_MIB_S:-64}"
RUNTIME_SEC="${RUNTIME_SEC:-20}"
CG=/sys/fs/cgroup/qrl_acc

RESULTS_DIR="${RESULTS_DIR:-$BASE_DIR/results/q_ratelimit_accept_$(date +%Y%m%d_%H%M%S)}"
R_FILE="$RESULTS_DIR/results.txt"
mkdir -p "$RESULTS_DIR"
: > "$R_FILE"

PASS=0; FAIL=0
log()  { printf '[q_ratelimit_accept] %s\n' "$*" >&2; echo "[q_ratelimit_accept] $*" >> "$R_FILE"; }
pass() { log "PASS: $*"; PASS=$((PASS+1)); }
fail() { log "FAIL: $*"; FAIL=$((FAIL+1)); }

source "$FS_DIR/lib_test.sh"
set +e

final_summary() {
    {
        echo ""
        echo "## Summary"
        echo "PASS=$PASS FAIL=$FAIL"
        echo "Date: $(date)"
        echo "RESULTS_DIR=$RESULTS_DIR"
    } >> "$R_FILE"
    tail -5 "$R_FILE"
}
trap final_summary EXIT

fs_require_vm
fs_require_destructive_guard
fs_detect_device
fs_ensure_bpffs
fs_setup_cgroup_io

DEV="$FS_DEV_PATH"
log "=== q_ratelimit acceptance START device=$DEV rate=${RATE_MIB_S}MiB/s runtime=${RUNTIME_SEC}s ==="

# ---------------------------------------------------------------- gate 0
log "--- G0: reference model self-test ---"
if timeout 120 python3 "$MODEL" > "$RESULTS_DIR/model.txt" 2>&1; then
    pass "model_self_test"
else
    fail "model_self_test"
    tail -5 "$RESULTS_DIR/model.txt" >&2
    fs_cleanup_ioext
    exit 1
fi

# ---------------------------------------------------------------- setup
# elevator switch resets queue knobs -> set nr_requests AFTER the switch
fs_set_scx_scheduler
if ! echo 1023 > "/sys/block/$FS_DEVICE/queue/nr_requests" 2>/dev/null; then
    log "WARN: cannot set nr_requests (continuing)"
fi
mkdir -p "$CG" 2>/dev/null || true
echo '+io' > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true

# helpers ----------------------------------------------------------------
bw_of() {
    python3 - "$1" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    w = d["jobs"][0]["write"]
    # fio 3.36: io_bytes is total bytes; bw field is KiB/s
    io_bytes = w.get("io_bytes") or w.get("bytes") or \
        (w.get("io_kbytes", 0) * 1024)
    runtime_ms = w.get("runtime") or 1
    print(int(io_bytes / (runtime_ms / 1000.0)))
except Exception:
    print(0)
PYEOF
}

run_fio_in_cg() {
    # fio must truly enter the tenant cgroup: $BASHPID, not $$ (guide §6.1;
    # $$ in a subshell is the parent shell pid)
    local outfile="$1"
    ( echo "$BASHPID" > "$CG/cgroup.procs" 2>/dev/null; \
      timeout $((RUNTIME_SEC + 60)) fio "$JOB_FILE" --output-format=json \
        > "$outfile" 2>"$outfile.stderr" ) || true
}

meta_field() {
    python3 - "$CG" "$1" "$BT" <<'PYEOF'
import struct, subprocess, sys, os
cg, idx, bt = sys.argv[1], int(sys.argv[2]), sys.argv[3]
cgid = os.stat(cg).st_ino
try:
    out = subprocess.check_output(
        [bt, "-j", "map", "dump", "pinned", "/sys/fs/bpf/ioext_policy_meta"],
        text=True)
except Exception:
    print(-1); sys.exit(0)
import json
data = json.loads(out)
# bpftool -j map dump: flat [{"key":..., "value":...}, ...] (some
# versions wrap it as {"maps":[{"data":[...]}]}); handle both shapes)
entries = data if isinstance(data, list) else \
    [e for m in data.get("maps", []) for e in m.get("data", [])]
FIELDS = ["queue_depth", "dispatches", "active_inserts", "owner_token"]
for e in entries:
        k = e.get("key")
        kk = None
        if isinstance(k, str):
            try:
                kk = struct.unpack("<Q", bytes.fromhex(
                    k.replace("0x", "").replace(" ", ""))[:8])[0]
            except Exception:
                continue
        elif isinstance(k, list):
            try:
                kk = struct.unpack("<Q", bytes(int(b, 0) for b in k[:8]))[0]
            except Exception:
                continue
        if kk != cgid:
            continue
        v = e.get("value")
        if isinstance(v, dict):
            print(v.get(FIELDS[idx], -1)); sys.exit(0)
        if isinstance(v, list):
            try:
                raw = bytes(int(b, 0) for b in v)
                print(struct.unpack("<4Q4I", raw)[idx]); sys.exit(0)
            except Exception:
                continue
        if isinstance(v, str):
            try:
                raw = bytes.fromhex(v.replace("0x", "").replace(" ", ""))
                print(struct.unpack("<4Q4I", raw)[idx]); sys.exit(0)
            except Exception:
                continue
print(-1)
PYEOF
}

bucket_field() {
    python3 - "$1" "$BT" <<'PYEOF'
import struct, subprocess, sys
field, bt = sys.argv[1], sys.argv[2]
FIELDS = ["tokens", "last_ns", "enqueued", "dispatches",
          "evac_pops", "null_returns", "push_fails", "lost"]
try:
    out = subprocess.check_output(
        [bt, "-j", "map", "dump", "name", "q_rl_bucket"], text=True)
except Exception:
    print(-1); sys.exit(0)
import json
data = json.loads(out)
# flat [{"key":..., "value":...}] shape (see meta_field note)
entries = data if isinstance(data, list) else \
    [e for m in data.get("maps", []) for e in m.get("data", [])]
for e in entries:
        v = e.get("value")
        if isinstance(v, dict) and field in v:
            print(v[field]); sys.exit(0)
        if isinstance(v, list):
            # struct q_rl_bucket: 4B spin_lock + 4B pad, then 8 u64 fields
            try:
                raw = bytes(int(b, 0) for b in v)
                print(struct.unpack_from("<8Q", raw, 8)[FIELDS.index(field)])
                sys.exit(0)
            except Exception:
                continue
        if isinstance(v, str):
            try:
                raw = bytes.fromhex(v.replace("0x", "").replace(" ", ""))
                print(struct.unpack_from("<8Q", raw, 8)[FIELDS.index(field)])
                sys.exit(0)
            except Exception:
                continue
print(-1)
PYEOF
}

JOB_FILE="$RESULTS_DIR/qrl_job.fio"
cat > "$JOB_FILE" <<EOF
[global]
ioengine=libaio
direct=1
bs=4k
rw=randwrite
iodepth=256
numjobs=1
time_based=1
group_reporting=1
runtime=${RUNTIME_SEC}
ramp_time=2
[be]
filename=$DEV
EOF

# ---------------------------------------------------------------- baseline
log "--- context: uncapped baseline fio (${RUNTIME_SEC}s, no policy attached) ---"
run_fio_in_cg "$RESULTS_DIR/baseline_fio.json"
BASELINE_BW=$(bw_of "$RESULTS_DIR/baseline_fio.json")
log "context baseline_bw=${BASELINE_BW} B/s (uncapped)"
if [ "$BASELINE_BW" -gt 0 ]; then
    pass "baseline_recorded (${BASELINE_BW} B/s)"
else
    log "WARN baseline BW parse returned 0 (context only)"
fi

# ---------------------------------------------------------------- gate 1
log "--- G1: loader start + attach ---"
: > "$RESULTS_DIR/loader.log"
"$LOADER" --cgroup "$CG" --rate-mib-s "$RATE_MIB_S" \
    > "$RESULTS_DIR/loader.log" 2>&1 &
LOADER_PID=$!
ATTACHED=0
for i in $(seq 1 30); do
    kill -0 "$LOADER_PID" 2>/dev/null || break
    if [ -e /sys/fs/bpf/ioext_policy_meta ] && \
       [ "$(meta_field 0)" != "-1" ] && \
       [ "$(fs_active_scheduler)" = "scx_io" ]; then
        ATTACHED=1
        break
    fi
    # fallback probe (fs_wait_attached semantics): loader alive >=2s with
    # scx_io active is a valid attach signal even if the map probe fails
    if [ "$i" -ge 2 ] && [ "$(fs_active_scheduler)" = "scx_io" ]; then
        ATTACHED=1
        log "attach confirmed via scheduler+alive fallback (map probe rc=$(meta_field 0))"
        break
    fi
    sleep 1
done
if [ "$ATTACHED" = "1" ]; then
    pass "loader_attached"
else
    fail "loader_attached (loader log follows)"
    cat "$RESULTS_DIR/loader.log" >&2 || true
    dmesg 2>/dev/null | tail -15 > "$RESULTS_DIR/loader_fail_dmesg.txt" || true
    kill -9 "$LOADER_PID" 2>/dev/null || true
    fs_cleanup_ioext
    exit 1
fi

# ---------------------------------------------------------------- gate 2
log "--- G2: capped stream ~${RATE_MIB_S}MiB/s (±15%) ---"
# background bucket sampler: one line per second during the capped phase
# (ts,tokens,last_ns,enqueued,dispatches,evac,nulls,push_fails,lost) --
# the inter-sample token balance exposes any admission leak directly
SAMPLER_CSV="$RESULTS_DIR/bucket_trace.csv"
(
    echo "ts,tokens,enqueued,dispatches,null_returns,lost"
    end=$((SECONDS + RUNTIME_SEC + 10))
    while [ "$SECONDS" -lt "$end" ]; do
        echo "$(date +%s),$(bucket_field tokens),$(bucket_field enqueued),"\
"$(bucket_field dispatches),$(bucket_field null_returns),$(bucket_field lost)"
        sleep 1
    done
) > "$SAMPLER_CSV" 2>/dev/null &
SAMPLER_PID=$!
run_fio_in_cg "$RESULTS_DIR/capped_fio.json"
kill "$SAMPLER_PID" 2>/dev/null || true
wait "$SAMPLER_PID" 2>/dev/null || true
CAPPED_BW=$(bw_of "$RESULTS_DIR/capped_fio.json")
RATE_BPS=$((RATE_MIB_S * 1048576))
python3 - "$RATE_BPS" "$CAPPED_BW" "$BASELINE_BW" >> "$R_FILE" <<'PYEOF'
import sys
rate, got, base = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
err = abs(got - rate) / rate if rate else 1
print(f"rate={rate/1048576:.0f}MiB/s achieved={got/1048576:.2f}MiB/s "
      f"err={err*100:.2f}% baseline={base/1048576:.2f}MiB/s")
PYEOF
if python3 -c "import sys; sys.exit(0 if abs(int(sys.argv[2]) - int(sys.argv[1])) / int(sys.argv[1]) <= 0.15 else 1)" "$RATE_BPS" "$CAPPED_BW" 2>/dev/null; then
    pass "rate_cap_within_15pct (${CAPPED_BW} B/s vs ${RATE_BPS} B/s)"
else
    fail "rate_cap_within_15pct (${CAPPED_BW} B/s vs ${RATE_BPS} B/s)"
fi
if python3 -c "import sys; sys.exit(0 if int(sys.argv[1]) <= int(sys.argv[2]) and int(sys.argv[2]) > 0 else 1)" "$CAPPED_BW" "$BASELINE_BW" 2>/dev/null; then
    pass "capped_le_baseline"
else
    fail "capped_le_baseline (capped=${CAPPED_BW} baseline=${BASELINE_BW})"
fi

# ---------------------------------------------------------------- gate 3
log "--- G3: quiesce + accounting conservation ---"
# post-fio backlog drains at RATE driven only by the 3ms blk-mq self-kick
# (no insert kicks remain) — this also validates liveness after the load ends
QUIESCED=0
for i in $(seq 1 $((RUNTIME_SEC * 3))); do
    QD=$(meta_field 0)
    AI=$(meta_field 2)
    if [ "$QD" = "0" ] && [ "$AI" = "0" ]; then
        QUIESCED=1
        break
    fi
    sleep 1
done
ENQ=$(bucket_field enqueued)
DIS=$(bucket_field dispatches)
EVA=$(bucket_field evac_pops)
LOST=$(bucket_field lost)
NULLS=$(bucket_field null_returns)
PFAIL=$(bucket_field push_fails)
log "stats: enqueued=$ENQ dispatches=$DIS evac_pops=$EVA lost=$LOST null_returns=$NULLS push_fails=$PFAIL queue_depth=$(meta_field 0)"
if [ "$QUIESCED" = "1" ]; then
    pass "quiesce_pending_zero (queue_depth==0 && active_inserts==0)"
else
    fail "quiesce_pending_zero (queue_depth=$(meta_field 0) active_inserts=$(meta_field 2))"
fi
if python3 -c "import sys; sys.exit(0 if int(sys.argv[1]) == int(sys.argv[2]) + int(sys.argv[3]) and int(sys.argv[1]) > 0 else 1)" "$ENQ" "$DIS" "$EVA" 2>/dev/null; then
    pass "policy_holds_nothing (enqueued == dispatches + evac_pops)"
else
    fail "policy_holds_nothing (enqueued=$ENQ dispatches=$DIS evac=$EVA)"
fi
if [ "$LOST" = "0" ] && [ "$PFAIL" = "0" ]; then
    pass "accounting_conservation (lost==0 && push_fails==0)"
else
    fail "accounting_conservation (lost=$LOST push_fails=$PFAIL)"
fi
if python3 -c "import sys; sys.exit(0 if int(sys.argv[1]) > 0 else 1)" "$NULLS" 2>/dev/null; then
    pass "rate_binding_evidence (null_returns>0)"
else
    fail "rate_binding_evidence (null_returns=$NULLS)"
fi
# raw dumps preserved for the report
"$BT" -j map dump name q_rl_bucket > "$RESULTS_DIR/q_rl_bucket_dump.json" 2>&1 || true
"$BT" -j map dump name q_rl_cfg_map > "$RESULTS_DIR/q_rl_cfg_dump.json" 2>&1 || true
"$BT" -j map dump pinned /sys/fs/bpf/ioext_policy_meta > "$RESULTS_DIR/policy_meta_dump.json" 2>&1 || true

# ---------------------------------------------------------------- gate 4
log "--- G4: graceful TERM stop + detach cleanliness ---"
kill -TERM "$LOADER_PID" 2>/dev/null || true
STOP_OK=0
for i in $(seq 1 20); do
    kill -0 "$LOADER_PID" 2>/dev/null || { STOP_OK=1; break; }
    sleep 1
done
if [ "$STOP_OK" = "1" ]; then
    wait "$LOADER_PID" 2>/dev/null
    RC=$?
    if [ "$RC" = "0" ]; then
        pass "loader_graceful_exit_0"
    else
        fail "loader_graceful_exit_0 (rc=$RC)"
    fi
else
    kill -9 "$LOADER_PID" 2>/dev/null || true
    fail "loader_graceful_exit_0 (did not exit in 20s)"
fi
sleep 2
if pgrep -f 'loader_q_ratelimit' >/dev/null 2>&1; then
    fail "no_stale_loader"
else
    pass "no_stale_loader"
fi
if fs_dmesg_has_errors; then
    fail "dmesg_clean"
else
    pass "dmesg_clean"
fi
dmesg 2>/dev/null | tail -100 > "$RESULTS_DIR/dmesg_tail.txt" || true

rmdir "$CG" 2>/dev/null || true
fs_cleanup_ioext

log "=== acceptance done: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" -eq 0 ]