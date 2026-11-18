#!/bin/bash
# d1_ladder_v2_bm.sh — D1 v2 experiment ladder on bare-metal bm, kernel 7.3.1-ioext3.
# Mirrors the discipline of d1_framework_ladder_bm.sh (flock /tmp/ioxhost_work.lock, trap
# restore, set_sched/mkcg/rmcgs, spawn_fio/wait_fio, E1 attach probes, sched readback
# before/after every run, shuf interleaving, maybe_reduce_reps wall-clock guard).
#
# PURPOSE
#   Produce a same-phase (同相位) dataset for the D1 v2 figure set:
#   device health gate + E3 ladder rerun + E4a-ext (16-job fixed load) + E4b-v2
#   (registered-idle incl. n_idle=0 in-phase baseline) + E6 (instrumentation on/off
#   paired micro-pricing of the d1_instr probe).
#
# PHASES
#   0  device-health gate: 1x solo native fio (same profile as everywhere else: 4K
#      randread, QD1, io_uring, direct=1, runtime 10s + ramp 1s). Parse IOPS from the
#      fio json; if < 50000 write an abort report, restore the environment
#      (sched back to original selection, cgroups removed, loaders killed) and exit
#      non-zero WITHOUT entering any later phase.
#   1  E3 ladder rerun: 5 arms {native, bare, q_noop, q_fifo, qfifo_arb} x REPS reps,
#      round-robin with arm order shuffled per round; arm implementations are
#      line-identical to d1_framework_ladder_bm.sh, including the E1 attach-latency
#      probes (spawn/seen epochs -> attach.csv) and per-run fio lat logs.
#   2  E4a-ext fixed load: EXACTLY 16 fio QD1 jobs with 16 DISTINCT randseeds
#      (one seed per job, fixed across reps and layouts), distributed over layouts
#      16on1cg / 8+8 / 4x4 / 2x8 / 1x16 cgroups, stacks {native, q_noop attach-all
#      (one loader per DISTINCT cgroup)} x REPS reps. Within each rep the
#      layouts x stacks combinations are interleaved via `shuf -e`. E4a-ext keeps
#      ONLY the fio json per job (no per-IO lat log — aggregate IOPS and clat p99
#      are in the json); each run dir gets a meta.txt (layout/stack/rep/sched
#      readback). Jobs are pinned to cpus 2..7 round-robin (8 logical cpus, 0-1
#      left to housekeeping, mirroring the base script's cpu-2 convention).
#   3  E4b-v2 registered-idle: 1 active fio job in d1_ta; n_idle in {0,1,3,7,15}
#      extra cgroups, each idle cgroup carrying an attached q_noop loader but no
#      fio. n_idle=0 is the in-phase N_registered=1 baseline (for the q_noop arm
#      that means exactly one loader on d1_ta). native arm = same cgroup counts,
#      no loaders, sched none. Per-run lat logs kept.
#   4  E6 instrumentation on/off pairing: arms {q_noop, q_fifo} x {instr_off,
#      instr_on} x REPS reps, interleaved via shuf -e; solo single job with the
#      ladder profile. instr_off = plain policy-loader run (in-phase control).
#      instr_on = start $LDIR/loader_d1_instr (delivered by a separate track;
#      args overridable via D1_INSTR_ARGS, default placeholder "-c $CG/d1_ta")
#      AFTER the policy loader, then wait for the EXACT stdout marker line
#      "d1_instr: attached" (30 s timeout -> run is marked instr_attach_failed in
#      meta.txt and fio is SKIPPED for that run). After fio ends and BEFORE killing
#      the loaders, dump the segs map:
#        bpftool -j map dump pinned /sys/fs/bpf/d1_instr_segs > d1_instr_segs.json
#      then kill loaders. fio json + lat logs are kept for all E6 runs.
#   5  finalize: env snapshot (sched state, io.cost.qos, dmesg tail, df -h),
#      SUMMARY.md with per-phase run counts, restore verification (trap).
#
# SAFETY CONSTRAINTS (hard)
#   - Must run as root on bm (previous ladders ran via sudo; scheduler file,
#     cgroup fs and /sys/fs/bpf are root-only).
#   - flock /tmp/ioxhost_work.lock NON-BLOCKING at start; if the lock is held,
#     abort immediately without touching anything.
#   - No reboot, no kernel/grub changes, no touching pre-existing /sys/fs/bpf
#     pins. The single allowed unlink is a STALE /sys/fs/bpf/d1_instr_sefs node
#     of the exact name "d1_instr_segs" before the instr loader attaches.
#   - Never kill any process not started by this script (pkill patterns are
#     limited to the loader binary names this script starts; fio is only ever
#     killed by pid).
#   - cgroup operations are limited to creating/removing /sys/fs/cgroup/d1_*
#     experiment-prefixed directories.
#   - On ANY exit path the trap restores: scheduler to the original selection
#     (sched_ext detached => /sys/kernel/sched_ext/state == disabled), all
#     experiment loaders killed, all d1_* cgroups removed; final state is
#     appended to env.txt for manual re-verification.
#
# Usage (root on bm):
#   d1_ladder_v2_bm.sh [--out DIR]
set -uo pipefail

OUT="/opt/ioext/dyn_tenant_regions/iocost_probe/d1_ladder_v2_$(date +%H%M%S)"
LOADER_DIR="/opt/ioext/io-ext/userspace"
CG="/sys/fs/cgroup"
LOCK="/tmp/ioxhost_work.lock"
RT=10; RAMP=1
REPS=5
# Health gate floor.  Default 50000 (healthy device).  Overridable via env for
# an explicitly authorized degraded-state run (e.g. HEALTH_MIN_IOPS=15000); the
# gate then still catches catastrophic states, and env.txt records the override.
HEALTH_MIN_IOPS="${HEALTH_MIN_IOPS:-50000}"
Q_NOOP="$LOADER_DIR/loader_q_noop"; Q_FIFO="$LOADER_DIR/loader_q_fifo"; ARB_NOOP="$LOADER_DIR/loader_arb_noop"
D1_INSTR="$LOADER_DIR/loader_d1_instr"          # placeholder path, delivered by separate track
D1_INSTR_MARKER="d1_instr: attached"
D1_INSTR_PIN="/sys/fs/bpf/d1_instr_segs"
D1_INSTR_ARGS="${D1_INSTR_ARGS:--c $CG/d1_ta}"  # placeholder args; finalize when loader lands
PLOCKER_RE='loader_q_noop|loader_q_fifo|loader_arb_noop|loader_d1_instr'
RUNID="$(basename "$OUT")"

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; RUNID="$(basename "$OUT")"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 1 ;;
  esac
done

[ "$(id -u)" = 0 ] || { echo "must run as root (sudo) on bm" >&2; exit 1; }

# ---- work lock: NON-BLOCKING; if held, abort without touching anything ----
exec 9>"$LOCK" 2>/dev/null || { echo "cannot open $LOCK" >&2; exit 1; }
if ! flock -n 9; then
  echo "BLOCKED: $LOCK is held by another experiment; aborting (nothing touched)" >&2
  exit 43
fi

for b in "$Q_NOOP" "$Q_FIFO" "$ARB_NOOP"; do
  [ -x "$b" ] || { echo "missing loader $b" >&2; exit 1; }
done
# loader_d1_instr is OPTIONAL at run time (instr_on runs self-skip on failure),
# but warn loudly if absent so the operator knows E6 instr_on will be marked failed.
[ -x "$D1_INSTR" ] || echo "WARN: $D1_INSTR not present/executable; E6 instr_on runs will be marked instr_attach_failed" >&2
command -v bpftool >/dev/null || { echo "bpftool missing" >&2; exit 1; }
command -v flock >/dev/null || { echo "flock missing" >&2; exit 1; }
mkdir -p "$OUT"
FILE="$OUT/d1ladder.bin"

DEVNAME=$(findmnt -nvo SOURCE -T "$OUT" 2>/dev/null | xargs basename 2>/dev/null || true)
if [ -z "$DEVNAME" ] || [ ! -e "/sys/block/$DEVNAME" ]; then
  PART=$(lsblk -no pkname "$(findmnt -nvo SOURCE -T "$OUT" 2>/dev/null)" 2>/dev/null | head -1)
  DEVNAME="${PART:-nvme0n1}"
fi
SCHED_PATH="/sys/block/$DEVNAME/queue/scheduler"
SCHED_BEFORE=$(cat "$SCHED_PATH")
SCHED_ORIG_SEL=$(grep -oE '\[[^]]+\]' <<< "$SCHED_BEFORE" | tr -d '[]' | head -1)
[ -n "$SCHED_ORIG_SEL" ] || SCHED_ORIG_SEL="none"
QDEV=$(cat "/sys/block/$DEVNAME/dev" 2>/dev/null || true)
START_TS=$(date +%s)

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$OUT/summary.txt"; }

cat > "$OUT/env.txt" <<EOF
runid: $RUNID
date: $(date -Is)
uname: $(uname -r)
hostname: $(hostname)
fio: $(fio --version 2>&1 | head -1)
bpftool: $(bpftool version 2>&1 | head -1)
bpftool_attach_syntax: bpftool struct_ops show  (struct_ops map names: scx_q_noop, scx_q_fifo, noop_arbiter)
dev: $DEVNAME ($(cat /sys/block/$DEVNAME/device/model 2>/dev/null || echo '?'))
devnode: $QDEV
sched_before: [$SCHED_BEFORE] (original selection: $SCHED_ORIG_SEL)
io_cost_qos_before: [$(cat $CG/io.cost.qos 2>/dev/null)]
sched_ext_state_before: [$(cat /sys/kernel/sched_ext/state 2>/dev/null)]
loader_sha256:
$(sha256sum "$Q_NOOP" "$Q_FIFO" "$ARB_NOOP")
d1_instr_loader: $D1_INSTR $([ -x "$D1_INSTR" ] && echo present || echo MISSING)
d1_instr_sha256: $([ -x "$D1_INSTR" ] && sha256sum "$D1_INSTR" || echo NA)
cpus: $(nproc)
EOF

restore() {
  local now sel
  now=$(cat "$SCHED_PATH" 2>/dev/null || true)
  sel=$(grep -oE '\[[^]]+\]' <<< "$now" | tr -d '[]' | head -1)
  if [ "$sel" != "$SCHED_ORIG_SEL" ]; then
    echo "$SCHED_ORIG_SEL" > "$SCHED_PATH" 2>/dev/null
    log "[restore] scheduler -> [$(cat "$SCHED_PATH")]"
  fi
  # root red line: io.cost.qos must end enable=0
  local qos
  qos=$(cat $CG/io.cost.qos 2>/dev/null || true)
  if [ -n "$QDEV" ] && grep -qE "(^| )1 (auto|user)" <<< "$qos"; then
    echo "$QDEV 0 auto 100 100000 20000 100 100000" > $CG/io.cost.qos 2>/dev/null
    log "[restore] io.cost.qos -> [$(cat $CG/io.cost.qos)]"
  fi
  pkill -f "$PLOCKER_RE" 2>/dev/null
  sleep 0.3
  for d in $CG/d1_*; do
    [ -d "$d" ] && rmdir "$d" 2>/dev/null
  done
  rm -f "$FILE"
  chmod -R a+rX "$OUT" 2>/dev/null
  # final restore verification for the report
  {
    echo "restore_verify: $(date -Is)"
    echo "scheduler_now=[$(cat "$SCHED_PATH" 2>/dev/null)]"
    echo "sched_ext_state_now=[$(cat /sys/kernel/sched_ext/state 2>/dev/null)]"
    echo "io_cost_qos_now=[$(cat $CG/io.cost.qos 2>/dev/null)]"
    echo "loaders_running=[$(pgrep -af "$PLOCKER_RE" || echo none)]"
    echo "d1_cgroups=[$(ls -d $CG/d1_* 2>/dev/null || echo none)]"
    echo "struct_ops_now=[$(bpftool struct_ops show 2>/dev/null | tr '\n' ';' || echo none)]"
    echo "lock_released=on_exit (fd9 closed with process)"
  } >> "$OUT/env.txt"
  log "restore verified (see env.txt tail)"
}
trap restore EXIT

# 2G testfile; MUST be zero-filled (fallocate-only leaves unwritten extents)
dd if=/dev/zero of="$FILE" bs=1M count=2048 status=none
sync
printf '+io' > "$CG/cgroup.subtree_control" 2>/dev/null || true

set_sched() { # sched
  local want="$1"; local now
  now=$(cat "$SCHED_PATH")
  if ! grep -q "\b$want\b" <<< "$now"; then echo "ERR: $want not available in [$now]" >&2; return 1; fi
  echo "$want" > "$SCHED_PATH"
  echo "[sched] -> [$(cat "$SCHED_PATH")]"
}

mkcg() { mkdir -p "$CG/d1_$1"; }
rmcgs() { for d in $CG/d1_*; do [ -d "$d" ] && rmdir "$d" 2>/dev/null; done; }

kill_loaders() {
  pkill -f "$PLOCKER_RE" 2>/dev/null
  sleep 0.4
}
wait_detached() { # wait until no ladder struct_ops remains
  local t0=$(date +%s)
  while bpftool struct_ops show 2>/dev/null | grep -qE 'scx_q_noop|scx_q_fifo|noop_arbiter'; do
    sleep 0.2
    [ $(( $(date +%s) - t0 )) -gt 5 ] && { log "WARN: struct_ops still present after 5s"; break; }
  done
}

# ---- fio ----
# spawn_fio NAME CGNAME OUTDIR SEED CPULIST [extra fio args] -> echoes wrapper pid
# (keeps per-IO lat log via --write_lat_log)
spawn_fio() {
  local nm="$1" cgn="$2" od="$3" seed="$4" cpu="$5"; shift 5
  mkdir -p "$od"
  bash -c "set -e
printf '%s\n' \"\$BASHPID\" > $CG/d1_$cgn/cgroup.procs
exec fio --name=w$nm --filename=$FILE --direct=1 --ioengine=io_uring --bs=4k \
 --rw=randread --iodepth=1 --size=2G --time_based=1 --randrepeat=0 --randseed=$seed \
 --cpus_allowed=$cpu --runtime=$RT --ramp_time=$RAMP --clat_percentiles=1 \
 --slat_percentiles=1 --percentile_list=50:90:99:99.9 --output-format=json \
 --output=$od/${nm}_fio.json --write_lat_log=$od/${nm}_lat $*" \
    > "$od/${nm}_console.out" 2>&1 &
  echo $!
}

# spawn_fio_json: same contract as spawn_fio but NO per-IO lat log (E4a-ext only;
# aggregate IOPS and clat p99 live in the json).
spawn_fio_json() {
  local nm="$1" cgn="$2" od="$3" seed="$4" cpu="$5"; shift 5
  mkdir -p "$od"
  bash -c "set -e
printf '%s\n' \"\$BASHPID\" > $CG/d1_$cgn/cgroup.procs
exec fio --name=w$nm --filename=$FILE --direct=1 --ioengine=io_uring --bs=4k \
 --rw=randread --iodepth=1 --size=2G --time_based=1 --randrepeat=0 --randseed=$seed \
 --cpus_allowed=$cpu --runtime=$RT --ramp_time=$RAMP --clat_percentiles=1 \
 --slat_percentiles=1 --percentile_list=50:90:99:99.9 --output-format=json \
 --output=$od/${nm}_fio.json $*" \
    > "$od/${nm}_console.out" 2>&1 &
  echo $!
}

wait_fio() { # pid...  (timeout RT+RAMP+120 per wait loop iteration set)
  local p t0
  for p in "$@"; do
    t0=$(date +%s)
    while kill -0 "$p" 2>/dev/null; do
      sleep 0.5
      if [ $(( $(date +%s) - t0 )) -gt $((RT + RAMP + 120)) ]; then
        kill -9 "$p" 2>/dev/null; log "WARN: fio $p killed after timeout"
      fi
    done
  done
}

# ---- E1 attach probe ----
# start_loader_probe BIN spawn_file log_file [args...] -> echoes wrapper pid
start_loader_probe() {
  local bin="$1" sf="$2" lf="$3"; shift 3
  bash -c "date +%s.%N > '$sf'; exec '$bin' $*" > "$lf" 2>&1 &
  echo $!
}

# poll_attach TOKEN seen_file -> echoes poller pid; loops bpftool struct_ops show ~every 2ms
poll_attach() {
  local token="$1" seen="$2"
  ( local end=$(( $(date +%s) + 20 ))
    while [ $(date +%s) -le $end ]; do
      if bpftool struct_ops show 2>/dev/null | grep -q "$token"; then
        date +%s.%N > "$seen"; exit 0
      fi
      sleep 0.002
    done
    exit 1 ) &
  echo $!
}

# wait_attach TOKEN spawn_file seen_file  -> echoes attach_ms or NA
wait_attach() {
  local token="$1" sf="$2" seen="$3" t0 pp
  pp=$(poll_attach "$token" "$seen")
  t0=$(date +%s)
  while [ ! -s "$seen" ]; do
    sleep 0.02
    if ! kill -0 "$pp" 2>/dev/null; then log "WARN: attach poller expired for $token" >&2; echo "NA"; return 1; fi
    if [ $(( $(date +%s) - t0 )) -gt 25 ]; then kill "$pp" 2>/dev/null; log "WARN: attach timeout for $token" >&2; echo "NA"; return 1; fi
  done
  kill "$pp" 2>/dev/null
  awk -v a="$(cat "$sf")" -v b="$(cat "$seen")" 'BEGIN{printf "%.1f", (b-a)*1000}'
}

rec_attach() { # phase arm rep token attach_ms spawn seen
  echo "$1,$2,$3,$4,$5,$6,$7" >> "$OUT/attach.csv"
}

# wait_marker FILE MARKER TIMEOUT_S -> 0 if exact marker line appeared, 1 on timeout
wait_marker() {
  local f="$1" marker="$2" tmo="$3" t0=$(date +%s)
  while [ $(( $(date +%s) - t0 )) -le "$tmo" ]; do
    grep -Fqx "$marker" "$f" 2>/dev/null && return 0
    sleep 0.05
  done
  return 1
}

write_meta() { # dir arm rep k=v...
  local d="$1" arm="$2" rep="$3"; shift 3
  {
    echo "runid=$RUNID"
    echo "arm=$arm"
    echo "rep=$rep"
    echo "ts_start=$(date -Is)"
    for kv in "$@"; do echo "$kv"; done
    echo "struct_ops_after=[$(bpftool struct_ops show 2>/dev/null | tr '\n' ';')]"
  } > "$d/meta.txt"
}

elapsed_s() { echo $(( $(date +%s) - START_TS )); }
maybe_reduce_reps() { # hard wall-clock guard: never below 3 reps
  if [ "$REPS" -gt 3 ] && [ "$(elapsed_s)" -gt 2700 ]; then
    log "wall clock >45min: reducing remaining phases to 3 reps"
    REPS=3
  fi
}

: > "$OUT/attach.csv"
echo "phase,arm,rep,token,attach_ms,spawn_epoch,seen_epoch" >> "$OUT/attach.csv"

########## Phase 0: device-health gate ##########
log "== Phase 0: device-health gate (solo native 4K QD1, ${RT}s+${RAMP}s, threshold ${HEALTH_MIN_IOPS} IOPS)"
set_sched none
P00="$OUT/00_device_health"
mkdir -p "$P00"
mkcg ta
p=$(spawn_fio healthprobe ta "$P00" 4242 2)
wait_fio "$p"
HEALTH_IOPS=$(python3 -I -c "
import json,sys
try:
    d=json.load(open('$P00/healthprobe_fio.json'))
    print(int(d['jobs'][0]['read']['iops']))
except Exception as e:
    print('ERR', e)" 2>/dev/null | head -1)
case "$HEALTH_IOPS" in
  ''|ERR*) HEALTH_IOPS=0 ;;
esac
echo "device_health_iops=$HEALTH_IOPS (threshold=$HEALTH_MIN_IOPS)" >> "$OUT/env.txt"
log "device-health probe IOPS=$HEALTH_IOPS"
if [ "$HEALTH_IOPS" -lt "$HEALTH_MIN_IOPS" ]; then
  {
    echo "ABORT: device-health gate FAILED at $(date -Is)"
    echo "iops=$HEALTH_IOPS threshold=$HEALTH_MIN_IOPS"
    echo "probe_json=$P00/healthprobe_fio.json"
    echo "sched_now=[$(cat "$SCHED_PATH" 2>/dev/null)]"
    echo "action=aborting before E3/E4a-ext/E4b-v2/E6; trap restores sched/cgroups/loaders"
  } > "$OUT/ABORT_device_health.txt"
  log "ABORT: device below ${HEALTH_MIN_IOPS} IOPS (got $HEALTH_IOPS); see ABORT_device_health.txt"
  rmcgs
  exit 42
fi
rmcgs

########## Phase 1: E3 ladder rerun (same-phase for the whole figure set) ##########
log "== E3 ladder rerun: 5 arms x $REPS reps (round-robin, shuffled per round)"
ARMS="native bare q_noop q_fifo qfifo_arb"
for round in $(seq 1 "$REPS"); do
  maybe_reduce_reps
  order=$(shuf -e $ARMS)
  log "round $round order: $(echo $order | tr '\n' ' ')"
  for arm in $order; do
    d="$OUT/ladder/${arm}_r${round}"
    sb=$(cat "$SCHED_PATH")
    case "$arm" in
      native)
        set_sched none
        mkcg ta
        mkdir -p "$d"
        p=$(spawn_fio ta ta "$d" 4242 2)
        wait_fio "$p"
        ;;
      bare)
        set_sched scx_io
        mkcg ta
        mkdir -p "$d"
        p=$(spawn_fio ta ta "$d" 4242 2)
        wait_fio "$p"
        ;;
      q_noop)
        set_sched scx_io
        mkcg ta
        mkdir -p "$d"
        sf="$d/q_noop.spawn"; sn="$d/q_noop.seen"
        lpid=$(start_loader_probe "$Q_NOOP" "$sf" "$d/q_noop_loader.log" -c "$CG/d1_ta" -p 0 -W 400)
        ams=$(wait_attach scx_q_noop "$sf" "$sn") || true
        rec_attach ladder q_noop "$round" scx_q_noop "$ams" "$(cat "$sf" 2>/dev/null)" "$(cat "$sn" 2>/dev/null)"
        log "arm q_noop r$round attach_ms=$ams"
        p=$(spawn_fio ta ta "$d" 4242 2)
        wait_fio "$p"
        kill_loaders; wait_detached
        ;;
      q_fifo)
        set_sched scx_io
        mkcg ta
        mkdir -p "$d"
        sf="$d/q_fifo.spawn"; sn="$d/q_fifo.seen"
        lpid=$(start_loader_probe "$Q_FIFO" "$sf" "$d/q_fifo_loader.log" -c "$CG/d1_ta" -p 0 -W 400)
        ams=$(wait_attach scx_q_fifo "$sf" "$sn") || true
        rec_attach ladder q_fifo "$round" scx_q_fifo "$ams" "$(cat "$sf" 2>/dev/null)" "$(cat "$sn" 2>/dev/null)"
        log "arm q_fifo r$round attach_ms=$ams"
        p=$(spawn_fio ta ta "$d" 4242 2)
        wait_fio "$p"
        kill_loaders; wait_detached
        ;;
      qfifo_arb)
        set_sched scx_io
        mkcg ta
        mkdir -p "$d"
        sf1="$d/q_fifo.spawn"; sn1="$d/q_fifo.seen"
        lpid=$(start_loader_probe "$Q_FIFO" "$sf1" "$d/q_fifo_loader.log" -c "$CG/d1_ta" -p 0 -W 400)
        ams1=$(wait_attach scx_q_fifo "$sf1" "$sn1") || true
        rec_attach ladder qfifo_arb "$round" scx_q_fifo "$ams1" "$(cat "$sf1" 2>/dev/null)" "$(cat "$sn1" 2>/dev/null)"
        sf2="$d/arb_noop.spawn"; sn2="$d/arb_noop.seen"
        lpid2=$(start_loader_probe "$ARB_NOOP" "$sf2" "$d/arb_noop_loader.log")
        ams2=$(wait_attach noop_arbiter "$sf2" "$sn2") || true
        rec_attach ladder qfifo_arb "$round" noop_arbiter "$ams2" "$(cat "$sf2" 2>/dev/null)" "$(cat "$sn2" 2>/dev/null)"
        log "arm qfifo_arb r$round attach fifo_ms=$ams1 arb_ms=$ams2"
        p=$(spawn_fio ta ta "$d" 4242 2)
        wait_fio "$p"
        kill_loaders; wait_detached
        ;;
    esac
    sa=$(cat "$SCHED_PATH")
    write_meta "$d" "$arm" "$round" "sched_before=[$sb]" "sched_after=[$sa]"
    log "done $arm r$round"
  done
done
rmcgs; set_sched none

########## Phase 2: E4a-ext fixed load (16 QD1 jobs, 5 layouts x 2 stacks) ##########
log "== E4a-ext: 16 QD1 jobs (distinct seed each), layouts 16on1cg/8+8/4x4/2x8/1x16 x {native,q_noop}, $REPS reps"
# layout -> cgroup name for job j (1..16)
CG16=(ta tb tc td te tf tg th ti tj tk tl tm tn to tp)
cg_for_job16() { # layout job
  local layout="$1" j="$2"
  case "$layout" in
    16on1cg) echo ta ;;
    '8+8')   if [ "$j" -le 8 ]; then echo ta; else echo tb; fi ;;
    '4x4')   echo "${CG16[$(( (j-1)/4 ))]}" ;;
    '2x8')   if [ "$j" -le 8 ]; then echo ta; else echo tb; fi ;;
    '1x16')  echo "${CG16[$(( j-1 ))]}" ;;
  esac
}
SEEDS16=(8101 8202 8303 8404 8505 8606 8707 8808 8909 9010 9111 9212 9313 9414 9515 9616)
LAYOUTS16=('16on1cg' '8+8' '4x4' '2x8' '1x16')
COMBOS16=""
for l in "${LAYOUTS16[@]}"; do for st in native q_noop; do COMBOS16+="$l:$st "; done; done
for round in $(seq 1 "$REPS"); do
  maybe_reduce_reps
  for combo in $(shuf -e $COMBOS16); do
    layout="${combo%%:*}"; stack="${combo##*:}"
    d="$OUT/e4a_ext/${layout}_${stack}_r${round}"
    mkdir -p "$d"
    sb=$(cat "$SCHED_PATH")
    if [ "$stack" = native ]; then set_sched none; else set_sched scx_io; fi
    # create the cgroups this layout needs
    for j in $(seq 1 16); do mkcg "$(cg_for_job16 "$layout" "$j")"; done
    if [ "$stack" = q_noop ]; then
      # attach-all: one q_noop loader per DISTINCT cgroup; no E1 samples here
      for c in $(for j in $(seq 1 16); do cg_for_job16 "$layout" "$j"; done | sort -u); do
        "$Q_NOOP" -c "$CG/d1_$c" -p 0 -W 400 > "$d/q_noop_$c.log" 2>&1 &
      done
      sleep 0.8
    fi
    pids=()
    for j in $(seq 1 16); do
      c=$(cg_for_job16 "$layout" "$j")
      # cpus 2..7 round-robin (0-1 reserved for housekeeping, mirrors base convention)
      pids+=("$(spawn_fio_json "j$j" "$c" "$d/cg$j" "${SEEDS16[$((j-1))]}" $(( 2 + (j-1) % 6 )))")
    done
    wait_fio "${pids[@]}"
    if [ "$stack" = q_noop ]; then kill_loaders; wait_detached; fi
    sa=$(cat "$SCHED_PATH")
    write_meta "$d" "e4aext_${layout}_${stack}" "$round" \
      "layout=$layout" "stack=$stack" "njobs=16" "seeds=${SEEDS16[*]}" \
      "lat_log=off (json only)" "sched_before=[$sb]" "sched_after=[$sa]"
    log "done e4a-ext $layout $stack r$round"
  done
  rmcgs
done
set_sched none

########## Phase 3: E4b-v2 registered-idle (n_idle in {0,1,3,7,15}) ##########
log "== E4b-v2: 1 active job, n_idle in {0,1,3,7,15} (0 = in-phase N_registered=1 baseline), stacks native/q_noop, $REPS reps"
IDLES=(0 1 3 7 15)
COMBOSB=""
for n in "${IDLES[@]}"; do for st in native q_noop; do COMBOSB+="$n:$st "; done; done
for round in $(seq 1 "$REPS"); do
  maybe_reduce_reps
  for combo in $(shuf -e $COMBOSB); do
    n="${combo%%:*}"; stack="${combo##*:}"
    d="$OUT/e4b_v2/N${n}_${stack}_r${round}"
    mkdir -p "$d"
    sb=$(cat "$SCHED_PATH")
    if [ "$stack" = native ]; then set_sched none; else set_sched scx_io; fi
    mkcg ta
    for ((i=1;i<=n;i++)); do mkcg "t$((i+1))"; done
    if [ "$stack" = q_noop ]; then
      # n_idle=0: exactly one loader on the active cgroup d1_ta (N_registered=1)
      "$Q_NOOP" -c "$CG/d1_ta" -p 0 -W 400 > "$d/q_noop_ta.log" 2>&1 &
      for ((i=1;i<=n;i++)); do
        "$Q_NOOP" -c "$CG/d1_t$((i+1))" -p 0 -W 400 > "$d/q_noop_t$((i+1)).log" 2>&1 &
      done
      sleep 0.8
    fi
    p=$(spawn_fio act ta "$d" 4242 2)
    wait_fio "$p"
    if [ "$stack" = q_noop ]; then kill_loaders; wait_detached; fi
    sa=$(cat "$SCHED_PATH")
    write_meta "$d" "e4bv2_N${n}_${stack}" "$round" "n_idle=$n" "stack=$stack" \
      "n_registered=$((n+1))" "sched_before=[$sb]" "sched_after=[$sa]"
    log "done e4b-v2 N=$n $stack r$round"
  done
  rmcgs
done
set_sched none

########## Phase 4: E6 instrumentation on/off pairing ##########
log "== E6: {q_noop,q_fifo} x {instr_off,instr_on} x $REPS reps, solo single job"
COMBOSE=""
for arm in q_noop q_fifo; do for st in off on; do COMBOSE+="$arm:$st "; done; done
for round in $(seq 1 "$REPS"); do
  maybe_reduce_reps
  for combo in $(shuf -e $COMBOSE); do
    arm="${combo%%:*}"; instr="${combo##*:}"
    d="$OUT/e6/${arm}_instr_${instr}_r${round}"
    mkdir -p "$d"
    sb=$(cat "$SCHED_PATH")
    set_sched scx_io
    mkcg ta
    # policy loader first (attach-timed, recorded like the ladder)
    if [ "$arm" = q_noop ]; then
      ptok=scx_q_noop; pbin="$Q_NOOP"; pargs=(-c "$CG/d1_ta" -p 0 -W 400)
    else
      ptok=scx_q_fifo; pbin="$Q_FIFO"; pargs=(-c "$CG/d1_ta" -p 0 -W 400)
    fi
    sf="$d/${arm}.spawn"; sn="$d/${arm}.seen"
    lpid=$(start_loader_probe "$pbin" "$sf" "$d/${arm}_loader.log" "${pargs[@]}")
    ams=$(wait_attach "$ptok" "$sf" "$sn") || true
    rec_attach e6 "$arm" "$round" "$ptok" "$ams" "$(cat "$sf" 2>/dev/null)" "$(cat "$sn" 2>/dev/null)"
    instr_status=skipped
    if [ "$instr" = on ]; then
      # allowed unlink: stale node of the exact name only
      [ -e "$D1_INSTR_PIN" ] && rm -f "$D1_INSTR_PIN"
      bash -c "exec '$D1_INSTR' $D1_INSTR_ARGS" > "$d/d1_instr.log" 2>&1 &
      ipid=$!
      if wait_marker "$d/d1_instr.log" "$D1_INSTR_MARKER" 30; then
        instr_status=attached
        log "e6 $arm instr_on r$round: marker OK"
      else
        instr_status=instr_attach_failed
        log "WARN: e6 $arm instr_on r$round: marker not seen in 30s -> skip fio"
        kill "$ipid" 2>/dev/null; kill_loaders; wait_detached
        write_meta "$d" "e6_${arm}_instr_on" "$round" "arm=$arm" "instr=on" \
          "instr_status=$instr_status" "fio=skipped" \
          "policy_attach_ms=$ams" "sched_before=[$sb]" "sched_after=[$(cat "$SCHED_PATH")]"
        continue
      fi
    fi
    p=$(spawn_fio act ta "$d" 4242 2)
    wait_fio "$p"
    if [ "$instr" = on ] && [ "$instr_status" = attached ]; then
      # dump segs map BEFORE killing loaders
      if [ -e "$D1_INSTR_PIN" ]; then
        bpftool -j map dump pinned "$D1_INSTR_PIN" > "$d/d1_instr_segs.json" 2> "$d/d1_instr_segs.dump.err" \
          || log "WARN: segs map dump failed (see d1_instr_segs.dump.err)"
      else
        echo "pin $D1_INSTR_PIN absent at dump time" > "$d/d1_instr_segs.dump.err"
        log "WARN: $D1_INSTR_PIN absent; no segs dump for r$round"
      fi
    fi
    kill_loaders; wait_detached
    sa=$(cat "$SCHED_PATH")
    write_meta "$d" "e6_${arm}_instr_${instr}" "$round" "arm=$arm" "instr=$instr" \
      "instr_status=$instr_status" "policy_attach_ms=$ams" \
      "sched_before=[$sb]" "sched_after=[$sa]"
    log "done e6 $arm instr=$instr r$round"
  done
  rmcgs
done
set_sched none

########## Phase 5: finalize ##########
{
  echo "dmesg_tail_begin"
  dmesg | tail -60
} > "$OUT/dmesg_tail.txt" 2>/dev/null
df -h "$OUT" /tmp > "$OUT/df_snapshot.txt" 2>&1
echo "io_cost_qos_final=[$(cat $CG/io.cost.qos 2>/dev/null)]" >> "$OUT/env.txt"
echo "sched_final=[$(cat "$SCHED_PATH")]" >> "$OUT/env.txt"
echo "sched_ext_state_final=[$(cat /sys/kernel/sched_ext/state 2>/dev/null)]" >> "$OUT/env.txt"
echo "elapsed_total_s=$(elapsed_s)" >> "$OUT/env.txt"

NL=$(ls -d "$OUT"/ladder/* 2>/dev/null | wc -l)
NA=$(ls -d "$OUT"/e4a_ext/* 2>/dev/null | wc -l)
NB=$(ls -d "$OUT"/e4b_v2/* 2>/dev/null | wc -l)
NE=$(ls -d "$OUT"/e6/* 2>/dev/null | wc -l)
cat > "$OUT/SUMMARY.md" <<EOF
# D1 ladder v2 — $RUNID

- date: $(date -Is), elapsed: $(elapsed_s)s, device: $DEVNAME
- device-health gate: IOPS=$HEALTH_IOPS (threshold $HEALTH_MIN_IOPS) -> PASS
- REPS at completion: $REPS (maybe_reduce_reps may have lowered it to 3)

| phase | run dirs | expected |
|---|---|---|
| 00_device_health | 1 | 1 |
| ladder (E3 rerun, 5 arms) | $NL | $((5 * REPS)) |
| e4a_ext (5 layouts x 2 stacks) | $NA | $((10 * REPS)) |
| e4b_v2 (5 n_idle x 2 stacks) | $NB | $((10 * REPS)) |
| e6 (2 arms x 2 instr) | $NE | $((4 * REPS)) |
| total | $((1 + NL + NA + NB + NE)) | $((1 + 29 * REPS)) |

E6 instr_attach_failed runs: $(grep -l instr_attach_failed "$OUT"/e6/*/meta.txt 2>/dev/null | wc -l)
EOF
log "== d1 ladder v2 complete: $OUT (elapsed $(elapsed_s)s)"
exit 0
