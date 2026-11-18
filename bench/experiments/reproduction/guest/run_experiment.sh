#!/bin/bash
# run_experiment.sh — ONE (experiment, arm, rep) run inside the guest VM.
# Usage: run_experiment.sh --algo <name> --arm <none|native|iox> --rep <N>
#                          --out <run_dir> [--skip-reset]
#
# Guarantees (plan §5):
#   - preflight records scheduler/cgroup/loader state and asserts the arm
#     actually took effect (post-setup snapshot);
#   - 3 x 10 s QD1 sentinels bracket the workload (phase drift evidence);
#   - fail-stop on fio/loader failure, no retry, no cherry-picking;
#   - loaders get SIGINT (graceful drain) — never SIGKILL;
#   - manifest.json captures run metadata, kernel, commit, hashes, cmdline.

set -u

ALGO="" ARM="" REP="" OUT=""
SKIP_RESET=0
while [ $# -gt 0 ]; do
    case "$1" in
    --algo) ALGO="$2"; shift 2;;
    --arm)  ARM="$2"; shift 2;;
    --rep)  REP="$2"; shift 2;;
    --out)  OUT="$2"; shift 2;;
    --skip-reset) SKIP_RESET=1; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
    esac
done
[ -n "$ALGO" ] && [ -n "$ARM" ] && [ -n "$REP" ] && [ -n "$OUT" ] || { echo "missing args" >&2; exit 2; }

RUN_DIR="$OUT"
export RUN_DIR
LIB_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ioext_repro_lib.sh
. "$LIB_DIR/ioext_repro_lib.sh"

mkdir -p "$RUN_DIR"
LOADER_PIDS="$RUN_DIR/loader_pids.txt"
: > "$LOADER_PIDS"

fail() {
    echo "[run FAIL] $*" | tee -a "$RUN_DIR/runner.log" >&2
    snapshot_state "FAIL_$1" 2>/dev/null || true
    stop_loaders
    set_scheduler none 2>/dev/null || true
    echo '{"status":"FAIL","reason":"'"$*"'"}' > "$RUN_DIR/RESULT.json"
    exit 1
}

echo "[run] algo=$ALGO arm=$ARM rep=$REP out=$RUN_DIR" | tee -a "$RUN_DIR/runner.log"

# ---------------------------------------------------------------------------
# 0. preflight
# ---------------------------------------------------------------------------
pgrep -a loader_ >/dev/null 2>&1 && fail "leftover loader process before run"

# Work-package A (2026-11-14): BPF program destruction is RCU-delayed — a
# finished/killed loader's iosched programs can outlive it for seconds, and a
# still-registered OLD arbiter splits dispatch with the new one (measured:
# shares pinned at 0.50 by a leftover per-hctx arbiter).  Wait for a clean
# slate before touching the scheduler.
BPFTOOL_BIN="${BPFTOOL_BIN:-$LIB_DIR/bpftool}"
N_IOSCHED=-1
if [ -x "$BPFTOOL_BIN" ]; then
    for i in $(seq 1 40); do
        N_IOSCHED=$("$BPFTOOL_BIN" prog show 2>/dev/null | grep -ciE 'pick_cgroup|insert_request' || true)
        [ "$N_IOSCHED" = "0" ] && break
        sleep 1
    done
    echo "[run] preflight iosched programs=$N_IOSCHED (waited ${i}s)" | tee -a "$RUN_DIR/runner.log"
    [ "$N_IOSCHED" = "0" ] || fail "iosched BPF programs still loaded after 40s (leftover arbiter would split dispatch)"
else
    echo "[run] WARN: bpftool not at $BPFTOOL_BIN — iosched program preflight skipped" | tee -a "$RUN_DIR/runner.log"
fi

[ -f "$DATA_LC" ] || fail "missing $DATA_LC"
[ -f "$DATA_BE" ] || fail "missing $DATA_BE"
uname -a > "$RUN_DIR/uname.txt"
cat /proc/version >> "$RUN_DIR/uname.txt"
cat "$(sched_file)" > "$RUN_DIR/scheduler_before.txt"
snapshot_state "preflight"

[ "$SKIP_RESET" = 1 ] || reset_data_files "$ALGO" || fail "data reset"

# Work-package A: read experiments must produce REAL device read IO — a
# phantom read (unwritten file) invalidates the run (P0_AUDIT.md §4/§6.2).
case "$ALGO" in
mq_deadline|kyber|linnos)
    if verify_read_io_authenticity "$DATA_LC" 2; then
        echo "[run] read-authenticity PASS" | tee -a "$RUN_DIR/runner.log"
    elif [ "${IOEXT_ALLOW_PHANTOM_READ:-0}" = "1" ]; then
        echo "[run][DEVIATION] phantom read tolerated by IOEXT_ALLOW_PHANTOM_READ=1" | tee -a "$RUN_DIR/runner.log"
    else
        fail "read IO not reaching device (phantom read on $DATA_LC; prewrite failed?)"
    fi
    ;;
esac

# ---------------------------------------------------------------------------
# 1. arm activation
# ---------------------------------------------------------------------------
activate "$ALGO" "$ARM"
rc=$?
[ $rc -eq 0 ] || fail "activate rc=$rc"

sleep 1   # let loaders finish attach

# arm-take-effect assertions (plan: baseline must be REAL, name==mechanism)
SCHED_NOW="$(cur_scheduler 2>/dev/null || awk '{print $1}' "$(sched_file)")"
case "$ALGO:$ARM" in
    mq_deadline:native) [ "$SCHED_NOW" = "mq-deadline" ] || fail "arm not effective: scheduler=$SCHED_NOW" ;;
    kyber:native)       [ "$SCHED_NOW" = "kyber" ] || fail "arm not effective: scheduler=$SCHED_NOW" ;;
    mq_deadline:iox|kyber:iox|io_max:iox|io_weight:iox|io_cost:iox|io_latency:iox|d2fq:iox|linnos:iox)
        pgrep -a loader_ >/dev/null 2>&1 || fail "arm not effective: no loader running"
        [ "$SCHED_NOW" = "scx_io" ] || fail "arm not effective: scheduler=$SCHED_NOW (want scx_io)" ;;
    io_max:native)  grep -q "wbps=${IO_MAX_WBPS}" "$BE_CG/io.max" 2>/dev/null || fail "io.max line missing" ;;
    io_max:none)    ! grep -q "wbps=[0-9]" "$BE_CG/io.max" 2>/dev/null || fail "none arm has io.max cap" ;;
    io_weight:native) [ "$SCHED_NOW" = "bfq" ] || fail "io.weight carrier not bfq" ;;
    io_cost:native) grep -q "enable=1" /sys/fs/cgroup/io.cost.qos 2>/dev/null || fail "io.cost qos not enabled" ;;
    io_latency:native) grep -q "target=12000" "$LC_CG/io.latency" 2>/dev/null || fail "io.latency target line missing (silent-skip trap)" ;;
esac
snapshot_state "armed"

# ---------------------------------------------------------------------------
# 2. sentinel before
# ---------------------------------------------------------------------------
run_sentinel before || true

# ---------------------------------------------------------------------------
# 3. workload (the ONE frozen experiment)
# ---------------------------------------------------------------------------
run_workload "$ALGO" || fail "workload rc=$?"

# ---------------------------------------------------------------------------
# 4. sentinel after + teardown
# ---------------------------------------------------------------------------
run_sentinel after || true
snapshot_state "post_workload"

# Work-package mq-rerun: capture IOX selection telemetry (sel_flip/starved)
# while the loader is still alive (the map dies with it)
case "$ALGO:$ARM" in
mq_deadline:iox)
    "$BPFTOOL_BIN" map dump name sel_stats_map > "$RUN_DIR/sel_stats_map.txt" 2>&1 || true
    ;;
esac

stop_loaders
set_scheduler none 2>/dev/null || true
# scrub cgroup knobs so the next arm starts clean
for cg in "$LC_CG" "$BE_CG" "$T100_CG" "$T400_CG"; do
    [ -d "$cg" ] || continue
    for f in io.max io.weight io.bfq.weight io.latency; do
        case "$f" in
        io.max)    echo "${DEVMJMN} rbps=max wbps=max riops=max wiops=max" > "$cg/$f" 2>/dev/null || true ;;
        io.weight) echo "100" > "$cg/$f" 2>/dev/null || true ;;
        io.bfq.weight) echo "100" > "$cg/$f" 2>/dev/null || true ;;
        # Work-package A fix (K1 root cause): io.latency writes REQUIRE the
        # MAJ:MIN prefix (E10; blkg_conf_open_bdev parses it from the body) —
        # the prefix-less write silently failed and left target=12000 armed on
        # lc for every later experiment (P0_AUDIT.md §3.1).
        io.latency) echo "${DEVMJMN} target=100000000" > "$cg/$f" 2>/dev/null || true ;;
        esac
    done
done
echo "none" > /sys/fs/cgroup/io.cost.qos 2>/dev/null || true
# work-package samplers enabled blkcg_debug_stats — reset it
echo 0 > /sys/module/blkcg/parameters/blkcg_debug_stats 2>/dev/null || true
snapshot_state "torn_down"

# ---------------------------------------------------------------------------
# 5. manifest
# ---------------------------------------------------------------------------
LOADER_HASHES=""
for l in "$LOADERS_DIR"/loader_*; do
    LOADER_HASHES="$LOADER_HASHES$(basename "$l"):$(sha256sum "$l" | cut -d' ' -f1)\n"
done
cat > "$RUN_DIR/manifest.json" <<EOF
{
  "run_id": "$(basename "$RUN_DIR")",
  "algo": "$ALGO",
  "arm": "$ARM",
  "rep": $REP,
  "host": "$(hostname)",
  "uname": "$(uname -r)",
  "scheduler_before": "$(cat "$RUN_DIR/scheduler_before.txt" 2>/dev/null | tr -d '\n')",
  "loaders_dir": "$LOADERS_DIR",
  "loader_hashes": "$LOADER_HASHES",
  "fio_version": "$(fio --version 2>/dev/null | head -1)",
  "blkdev": "$BLKDEV",
  "started_unix": $(stat -c %Y "$RUN_DIR" 2>/dev/null || echo 0),
  "finished_unix": $(date +%s)
}
EOF

# compress per-IO logs in place (disk budget: root.img); JSON stays raw
    gzip -f "$RUN_DIR"/*.log 2>/dev/null || true

    echo '{"status":"OK"}' > "$RUN_DIR/RESULT.json"
echo "[run OK] $RUN_DIR" | tee -a "$RUN_DIR/runner.log"
exit 0
