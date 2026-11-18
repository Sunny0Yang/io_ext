#!/usr/bin/env bash
# sc_run_chaos_bm.sh — S-CHAOS single-run orchestrator (RUNS ON kvmhost, as root,
# via `sudo -S bash` from sc_launch_bm.sh).
#
# One run = guards -> scx_io attach -> cgroup -> dmesg/sampler taps -> prefill ->
# chaos fio (randwrite 4x QD64 + crc32c verify) -> injector (hot-switch + faults)
# -> post checks -> cleanup.  All artifacts under /opt/safety_chaos/results/<seq>/.
set -u

ARM="S1"; REP="r1"; RUNTIME_S=240; SIZE_G_TOTAL=32; WITH_FTRACE=0
INJECTOR_ARGS=""; OUTDIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --arm) ARM="$2"; shift 2;;
    --rep) REP="$2"; shift 2;;
    --runtime-s) RUNTIME_S="$2"; shift 2;;
    --size-g-total) SIZE_G_TOTAL="$2"; shift 2;;
    --injector-args) INJECTOR_ARGS="$2"; shift 2;;
    --with-ftrace) WITH_FTRACE=1; shift;;
    --prefill-s) PREFILL_S="$2"; shift 2;;
    --outdir) OUTDIR="$2"; shift 2;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac
done

DEV=/dev/sdb
DEV_SYS=/sys/block/sdb
CG=/sys/fs/cgroup/scx_safety
BIN=/opt/safety_chaos/bin
SCRIPTS=/opt/safety_chaos/scripts
BASE=/opt/safety_chaos/results
SEQ="safety_chaos_${ARM}_${REP}_$(date +%Y%m%d_%H%M%S)"
OUTDIR="${OUTDIR:-$BASE/$SEQ}"
mkdir -p "$OUTDIR"
META="$OUTDIR/run_meta.json"

fail() { echo "SC-CHAOS GUARD FAIL: $*" | tee -a "$OUTDIR/guard.log"; exit 5; }

# serialize runs WITHOUT fd inheritance: a background holder process owns the
# lock (children spawned by this script do NOT inherit it, so a leftover tap
# process can never keep the lock hostage after the run script dies).
flock -n /tmp/safety_chaos.lock -c 'sleep 86400' 2>/dev/null & LOCK_HOLDER=$!
sleep 0.1
flock -n /tmp/safety_chaos.lock -c true 2>/dev/null \
  && fail "another safety_chaos run holds /tmp/safety_chaos.lock"

echo "=== S-CHAOS run $SEQ (arm=$ARM rep=$REP runtime=${RUNTIME_S}s size=${SIZE_G_TOTAL}G) ==="

# ---------- guards ----------
uname -r | grep -q ioext || fail "kernel is not ioext: $(uname -r)"
[ -b "$DEV" ] || fail "$DEV missing"
SZ=$(cat "$DEV_SYS/size"); [ "$SZ" = "201326592" ] || fail "sdb size sectors=$SZ (expect 201326592 = 96GiB)"
MOUNTED=$(lsblk -no MOUNTPOINT "$DEV" | tr -d ' ' | grep -v '^$' | head -1)
[ -z "$MOUNTED" ] || fail "sdb mounted at $MOUNTED"
pgrep -f "^loader_" >/dev/null && { pgrep -af "^loader_" | head; fail "stale loader processes"; }
pgrep -x fio >/dev/null && fail "another fio is running"
SCHEDS=$(cat "$DEV_SYS/queue/scheduler")
echo "$SCHEDS" | grep -qw scx_io || fail "scx_io not in scheduler list: $SCHEDS"
NR_RQ=$(cat "$DEV_SYS/queue/nr_requests")
echo "guards ok (sched='$SCHEDS' nr_requests=$NR_RQ)" | tee "$OUTDIR/guard.log"

HUNG_SAVE=$(cat /proc/sys/kernel/hung_task_timeout_secs)
echo 30 > /proc/sys/kernel/hung_task_timeout_secs
restore() {
  echo "$HUNG_SAVE" > /proc/sys/kernel/hung_task_timeout_secs 2>/dev/null
  pkill -TERM -f '^loader_' 2>/dev/null
  sleep 0.5
  pkill -KILL -f '^loader_' 2>/dev/null
  [ -d "$CG" ] && rmdir "$CG" 2>/dev/null
  echo none > "$DEV_SYS/queue/scheduler" 2>/dev/null
  TI=/sys/kernel/tracing/instances/sc_chaos
  if [ -d "$TI" ]; then
    echo 0 > "$TI/tracing_on" 2>/dev/null
    echo 0 > "$TI/events/kprobes/ioext_spre/enable" 2>/dev/null
    echo 0 > "$TI/events/kprobes/ioext_sfree/enable" 2>/dev/null
    sleep 0.2
    echo '-:ioext_spre' >> "$TI/kprobe_events" 2>/dev/null
    echo '-:ioext_sfree' >> "$TI/kprobe_events" 2>/dev/null
    rmdir "$TI" 2>/dev/null || rm -rf "$TI" 2>/dev/null
    true
  fi
  kill "$LOCK_HOLDER" 2>/dev/null
  true
}
trap restore EXIT

# ---------- scheduler + cgroup ----------
echo scx_io > "$DEV_SYS/queue/scheduler" || fail "cannot set scx_io"
CUR=$(cat "$DEV_SYS/queue/scheduler")
echo "$CUR" | grep -q '\[scx_io\]' || fail "scx_io not active after echo (got '$CUR')"
mkdir -p "$CG"

# ---------- taps ----------
( dmesg --follow > "$OUTDIR/dmesg_tap.log" 2>&1 ) &
TAP_PID=$!
sleep 0.3
bash "$SCRIPTS/sc_sampler_bm.sh" "$OUTDIR/sampler.csv" 0.05 &
SAMPLER_PID=$!

# kprobe events: per-policy quiesce-start (prepare_teardown) and drain-done
# (policy_free). Deployed kernel gates its teardown pr_info under IOEXT_DEBUG,
# so these probes are the per-event kernel-side signal.  A DEDICATED trace
# instance is used because the top-level trace_pipe allows a single reader
# (a leftover reader makes open fail with EBUSY and the tap silently loses
# every event).
T=/sys/kernel/tracing
TI="$T/instances/sc_chaos"
if [ -d "$T" ]; then
  mkdir -p "$TI" 2>/dev/null
  echo 'p:ioext_spre scx_io_policy_prepare_teardown' >> "$TI/kprobe_events" 2>/dev/null
  echo 'p:ioext_sfree scx_io_policy_free' >> "$TI/kprobe_events" 2>/dev/null
  echo 1 > "$TI/events/kprobes/ioext_spre/enable" 2>/dev/null
  echo 1 > "$TI/events/kprobes/ioext_sfree/enable" 2>/dev/null
  echo 1 > "$TI/tracing_on"
  ( cat "$TI/trace_pipe" > "$OUTDIR/kprobe_tap.log" 2>/dev/null ) &
  KPROBE_PID=$!
fi

# optional ftrace function_graph on the teardown workfn (drain round detail)
if [ "$WITH_FTRACE" = "1" ] && [ -d /sys/kernel/tracing ]; then
  echo 0 > "$T/tracing_on"; echo nop > "$T/current_tracer"
  echo function_graph > "$T/current_tracer"
  echo scx_io_policy_teardown_workfn > "$T/set_graph_function"
  echo 20000 > "$T/buffer_size_kb"
  echo 1 > "$T/tracing_on"
  ( cat "$T/trace_pipe" > "$OUTDIR/fgraph.log" 2>/dev/null ) &
  FTRACE_PID=$!
fi

# ---------- prefill (OPTIONAL, default off) ----------
# Not needed for correctness: fio verify embeds a per-block header (seed+crc);
# with --randrepeat=0 every run uses a fresh seed, so a LOST write shows up as
# a bad/missing verify header and a CORRUPTED write as a crc mismatch.  The
# device only sustains ~40MB/s sequential, so a full-disk prefill would waste
# ~40 min per run.  Opt back in with --prefill-s N for a bounded N-second fill.
if [ "${PREFILL_S:-0}" != "0" ]; then
  fio --name=fill --filename="$DEV" --rw=write --bs=1m --iodepth=32 --direct=1 \
      --time_based --runtime="$PREFILL_S" \
      --output-format=json --output="$OUTDIR/prefill.json" > "$OUTDIR/prefill.stdout" 2>&1 \
    || fail "prefill fio failed (see prefill.stdout)"
fi

# ---------- chaos fio: randwrite 4 jobs x QD64, disjoint regions, full verify ----------
# --size applies PER JOB on the same raw device, so plain --numjobs would make
# all four jobs write the SAME region (their inline verify passes then read
# each other's headers -> bogus "bad header" failures).  Disjoint regions via
# a generated jobfile: job i owns [i*2G, (i+1)*2G) of the SIZE_G_TOTAL window.
SIZE_PER_JOB_G=$(( SIZE_G_TOTAL / 4 ))
[ "$SIZE_PER_JOB_G" -lt 1 ] && SIZE_PER_JOB_G=1   # fio --size=0 means "whole device"
JOBFILE="$OUTDIR/chaos.fio"
: > "$JOBFILE"
cat >> "$JOBFILE" <<EOF
[global]
filename=$DEV
rw=randwrite
bs=4k
direct=1
iodepth=64
randrepeat=0
verify=crc32c
verify_backlog=16384
verify_async=2
verify_fatal=1
EOF
for i in 0 1 2 3; do
  OFF=$(( i * SIZE_PER_JOB_G ))G
  cat >> "$JOBFILE" <<EOF

[j$((i+1))]
offset=$OFF
size=${SIZE_PER_JOB_G}G
EOF
done
fio "$JOBFILE" \
    --output-format=json --output="$OUTDIR/chaos.json" \
    --write_bw_log="$OUTDIR/bw" --write_lat_log="$OUTDIR/lat" \
    > "$OUTDIR/chaos.stdout" 2>&1 &
FIO_PID=$!
echo "fio chaos started pid=$FIO_PID (write+verify phases)"
sleep 3   # warm-up before first switch

# ---------- injector ----------
INJ_LOG="$OUTDIR/injector.log"
PYTHONUNBUFFERED=1 python3 "$SCRIPTS/sc_injector_bm.py" \
  --cgroup "$CG" --bin-dir "$BIN" \
  --kprobe-tap "$OUTDIR/kprobe_tap.log" \
  --duration-s "$RUNTIME_S" \
  --out "$OUTDIR/events.csv" --log "$INJ_LOG" \
  $INJECTOR_ARGS &
INJ_PID=$!

# ---------- watchdog: fio must finish within runtime + verify budget ----------
# device calibrated at ~9k IOPS (4k randwrite QD256) / ~40MB/s (1M seq write)
MAX_BUDGET=$(( 900 + RUNTIME_S + SIZE_G_TOTAL * 100 ))
START=$(date +%s)
while kill -0 "$FIO_PID" 2>/dev/null; do
  NOW=$(date +%s)
  if [ $(( NOW - START )) -gt "$MAX_BUDGET" ]; then
    echo "SC-CHAOS WATCHDOG: fio did not finish in ${MAX_BUDGET}s -> FAIL (hung I/O?)" \
      | tee "$OUTDIR/watchdog.log"
    kill -INT "$FIO_PID" 2>/dev/null; sleep 5; kill -9 "$FIO_PID" 2>/dev/null
    echo '{"fio_result":"watchdog_timeout","hung_io_suspected":true}' > "$OUTDIR/watchdog.json"
    break
  fi
  sleep 2
done
wait "$FIO_PID" 2>/dev/null; FIO_RC=$?

# stop injector (per-row flushed CSV is safe even on SIGTERM)
kill -TERM "$INJ_PID" 2>/dev/null; sleep 2; kill -KILL "$INJ_PID" 2>/dev/null
wait "$INJ_PID" 2>/dev/null

# ---------- post checks ----------
INFL_END=$(cat "$DEV_SYS/inflight")
UPTIME_S0=$(cut -d' ' -f1 /proc/uptime)
cat /proc/sys/kernel/hung_task_timeout_secs > "$OUTDIR/hung_task_timeout_final" 2>/dev/null
dmesg | tail -400 > "$OUTDIR/dmesg_tail.log" 2>/dev/null

# stop taps / ftrace
kill "$SAMPLER_PID" 2>/dev/null; kill "$TAP_PID" 2>/dev/null
[ -n "${KPROBE_PID:-}" ] && kill "$KPROBE_PID" 2>/dev/null
sleep 1; kill -9 "$SAMPLER_PID" "$TAP_PID" ${KPROBE_PID:-} 2>/dev/null
if [ "${FTRACE_PID:-}" != "" ]; then
  echo 0 > /sys/kernel/tracing/tracing_on 2>/dev/null
  kill "$FTRACE_PID" 2>/dev/null; sleep 0.5; kill -9 "$FTRACE_PID" 2>/dev/null
  echo nop > /sys/kernel/tracing/current_tracer 2>/dev/null
fi
restore

cat > "$META" <<EOF
{
  "seq": "$SEQ", "arm": "$ARM", "rep": "$REP",
  "runtime_s": $RUNTIME_S, "size_g_total": $SIZE_G_TOTAL,
  "injector_args": "$INJECTOR_ARGS", "with_ftrace": $WITH_FTRACE,
  "kernel": "$(uname -r)", "nr_requests": $NR_RQ,
  "scheduler_after": "$(cat "$DEV_SYS/queue/scheduler")",
  "inflight_at_end": "$INFL_END",
  "fio_rc": $FIO_RC,
  "uptime_at_end": $UPTIME_S0,
  "finished_at": "$(date -Is)"
}
EOF
echo "=== run done: fio_rc=$FIO_RC inflight_at_end='$INFL_END' seq=$SEQ ==="
INFL_NORM=$(echo $INFL_END)
[ "$FIO_RC" = "0" ] && [ "$INFL_NORM" = "0 0" ] && exit 0 || exit 1
