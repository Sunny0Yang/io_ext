#!/bin/bash
# d1_framework_ladder_bm.sh — D1' framework-overhead experiments (E1-E4) on bare-metal bm,
# kernel 7.3.1-ioext3.  Protocol: experiments/dyn_tenant_bm/D1_FRAMEWORK_FIGURE_DESIGN.md §12
# (as amended by §12.4: arbiter rung is q_fifo+arb_noop, 5 arms).
#
# Workload everywhere: fio 4K randread, QD1, io_uring, direct=1, time_based, one job per
# cgroup, 2G zero-filled testfile in OUT, runtime 10s + ramp 1s, percentile_list 50:90:99:99.9,
# --write_lat_log per run, --output-format=json.  Scheduler readback before/after every arm.
#
# Phases:
#   00  device-state probe: 10s solo native 4K QD1 randread (same-day baseline, mandatory)
#   E1  attach latency: per loader start a wrapper writes spawn epoch (date +%s.%N) just
#       before exec; a poller loops `bpftool struct_ops show` until the struct_ops map name
#       (scx_q_noop / scx_q_fifo / noop_arbiter) appears; attach_ms = first_seen - spawn.
#       One sample per loader start; ladder reps give 5 per loader arm.
#   E2+E3 ladder (one set of runs): 5 arms x 5 reps, round-robin rounds, arm order shuffled
#       per round: native | bare scx_io | scx_io+q_noop | scx_io+q_fifo | scx_io+q_fifo+arb_noop
#   E4a fixed load: exactly 4 fio QD1 jobs (distinct --randseed per job) split across
#       1 / 2 / 4 cgroups (4on1cg / 2+2 / 1+1+1+1), stacks native vs q_noop attach-all
#       (one loader per cgroup); 5 reps interleaved, layout order shuffled per round,
#       native/q_noop run back-to-back per layout (paired).
#   E4b registered-idle: 1 active fio job in d1_ta; N_idle in {1,3,7,15} extra cgroups each
#       with q_noop loader attached but no fio (total registered 2/4/8/16); control = native
#       with same number of plain cgroups (no loaders).  N_idle=0 baseline = E3 q_noop reps.
#
# Usage (root on bm; caller must hold /tmp/ioxhost_work.lock):
#   d1_framework_ladder_bm.sh [--out DIR]
set -uo pipefail

OUT="/opt/ioext/dyn_tenant_regions/iocost_probe/d1_ladder_$(date +%H%M%S)"
LOADER_DIR="/opt/ioext/io-ext/userspace"
CG="/sys/fs/cgroup"
RT=10; RAMP=1
REPS=5
Q_NOOP="$LOADER_DIR/loader_q_noop"; Q_FIFO="$LOADER_DIR/loader_q_fifo"; ARB_NOOP="$LOADER_DIR/loader_arb_noop"
RUNID="$(basename "$OUT")"

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; RUNID="$(basename "$OUT")"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 1 ;;
  esac
done

for b in "$Q_NOOP" "$Q_FIFO" "$ARB_NOOP"; do
  [ -x "$b" ] || { echo "missing loader $b" >&2; exit 1; }
done
command -v bpftool >/dev/null || { echo "bpftool missing" >&2; exit 1; }
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
loader_sha256:
$(sha256sum "$Q_NOOP" "$Q_FIFO" "$ARB_NOOP")
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
  pkill -f 'loader_q_noop|loader_q_fifo|loader_arb_noop' 2>/dev/null
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
    echo "io_cost_qos_now=[$(cat $CG/io.cost.qos 2>/dev/null)]"
    echo "loaders_running=[$(pgrep -af 'loader_q_noop|loader_q_fifo|loader_arb_noop' || echo none)]"
    echo "d1_cgroups=[$(ls -d $CG/d1_* 2>/dev/null || echo none)]"
    echo "struct_ops_now=[$(bpftool struct_ops show 2>/dev/null | tr '\n' ';' || echo none)]"
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
  pkill -f 'loader_q_noop|loader_q_fifo|loader_arb_noop' 2>/dev/null
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

########## Phase 00: device-state probe ##########
log "== Phase 00: device-state probe (solo native 4K QD1, ${RT}s+${RAMP}s)"
set_sched none
P00="$OUT/00_device_probe"
mkdir -p "$P00"
mkcg ta
p=$(spawn_fio probe ta "$P00" 4242 2)
wait_fio "$p"
echo "device_probe_sched_after=[$(cat "$SCHED_PATH")]" >> "$OUT/env.txt"
log "device probe done"

rmcgs

########## E2 + E3 ladder ##########
log "== E2/E3 ladder: 5 arms x $REPS reps (round-robin, shuffled per round)"
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

########## E4a: fixed load (4 jobs total), layouts 1/2/4 cgroups ##########
log "== E4a fixed load: 4 QD1 jobs, layouts 4on1cg/2+2/1+1+1+1 x {native,q_noop}, $REPS reps"
# layout -> cgroup list for jobs 1..4
LAYOUTS="4on1cg twox2 fourx1"
cg_for_job() { # layout job
  case "$1" in
    4on1cg) echo ta ;;
    twox2)  case "$2" in 1|2) echo ta ;; 3|4) echo tb ;; esac ;;
    fourx1) case "$2" in 1) echo ta ;; 2) echo tb ;; 3) echo tc ;; 4) echo td ;; esac ;;
  esac
}
SEEDS=(9101 9202 9303 9404)
for round in $(seq 1 "$REPS"); do
  maybe_reduce_reps
  for layout in $(shuf -e $LAYOUTS); do
    for stack in native q_noop; do
      d="$OUT/e4a/${layout}_${stack}_r${round}"
      mkdir -p "$d"
      sb=$(cat "$SCHED_PATH")
      if [ "$stack" = native ]; then
        set_sched none
      else
        set_sched scx_io
      fi
      # create the cgroups this layout needs
      for j in 1 2 3 4; do mkcg "$(cg_for_job "$layout" "$j")"; done
      if [ "$stack" = q_noop ]; then
        # one q_noop loader per DISTINCT cgroup (attach-all); E1 samples not taken here
        for c in $(for j in 1 2 3 4; do cg_for_job "$layout" "$j"; done | sort -u); do
          "$Q_NOOP" -c "$CG/d1_$c" -p 0 -W 400 > "$d/q_noop_$c.log" 2>&1 &
        done
        sleep 0.8
      fi
      pids=()
      for j in 1 2 3 4; do
        c=$(cg_for_job "$layout" "$j")
        pids+=("$(spawn_fio "j$j" "$c" "$d/cg$j" "${SEEDS[$((j-1))]}" $((j+1)))")
      done
      wait_fio "${pids[@]}"
      if [ "$stack" = q_noop ]; then kill_loaders; wait_detached; fi
      sa=$(cat "$SCHED_PATH")
      write_meta "$d" "e4a_${layout}_${stack}" "$round" "layout=$layout" "stack=$stack" "sched_before=[$sb]" "sched_after=[$sa]"
      log "done e4a $layout $stack r$round"
    done
  done
  rmcgs
done
set_sched none

########## E4b: registered-idle ##########
log "== E4b registered-idle: 1 active job, N_idle in {1,3,7,15}, stacks native/q_noop, $REPS reps"
IDLES=(1 3 7 15)
COMBOS=""
for n in "${IDLES[@]}"; do for st in native q_noop; do COMBOS+="$n:$st "; done; done
for round in $(seq 1 "$REPS"); do
  maybe_reduce_reps
  for combo in $(shuf -e $COMBOS); do
    n="${combo%%:*}"; stack="${combo##*:}"
    d="$OUT/e4b/N${n}_${stack}_r${round}"
    mkdir -p "$d"
    sb=$(cat "$SCHED_PATH")
    if [ "$stack" = native ]; then set_sched none; else set_sched scx_io; fi
    mkcg ta
    for ((i=1;i<=n;i++)); do mkcg "t$((i+1))"; done
    if [ "$stack" = q_noop ]; then
      # registered = active + idle cgroups, each with one q_noop loader
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
    write_meta "$d" "e4b_N${n}_${stack}" "$round" "n_idle=$n" "stack=$stack" "n_registered=$((n+1))" "sched_before=[$sb]" "sched_after=[$sa]"
    log "done e4b N=$n $stack r$round"
  done
  rmcgs
done
set_sched none

echo "dmesg_tail_begin" > "$OUT/dmesg_tail.txt"
dmesg | tail -60 >> "$OUT/dmesg_tail.txt" 2>/dev/null
echo "io_cost_qos_final=[$(cat $CG/io.cost.qos 2>/dev/null)]" >> "$OUT/env.txt"
echo "sched_final=[$(cat "$SCHED_PATH")]" >> "$OUT/env.txt"
echo "elapsed_total_s=$(elapsed_s)" >> "$OUT/env.txt"
log "== ladder complete: $OUT (elapsed $(elapsed_s)s)"
exit 0
