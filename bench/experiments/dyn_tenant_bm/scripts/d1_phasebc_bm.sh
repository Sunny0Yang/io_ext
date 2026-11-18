#!/bin/bash
# d1_phasebc_bm.sh — D1 Phase B (cgroup scaling) + Phase C (vertical), run as root on 86.
# Usage: d1_phasebc_bm.sh OUTDIR  (OUTDIR contains d1probe.bin from phase A, or will create one)
set -uo pipefail
OUT="${1:?OUTDIR required}"
LOADER_DIR="/opt/ioext/io-ext/userspace"
CG="/sys/fs/cgroup"
FILE="$OUT/d1probe.bin"

DEVNAME=$(findmnt -nvo SOURCE -T "$OUT" 2>/dev/null | xargs basename 2>/dev/null || true)
if [ -z "$DEVNAME" ] || [ ! -e "/sys/block/$DEVNAME" ]; then
  PART=$(lsblk -no pkname "$(findmnt -nvo SOURCE -T "$OUT" 2>/dev/null)" 2>/dev/null | head -1)
  DEVNAME="${PART:-nvme0n1}"
fi
SCHED_PATH="/sys/block/$DEVNAME/queue/scheduler"
SCHED_BEFORE=$(cat "$SCHED_PATH")
echo "[env] kernel=$(uname -r) dev=$DEVNAME sched=[$SCHED_BEFORE]"

restore() {
  local now; now=$(cat "$SCHED_PATH" 2>/dev/null || true)
  if [ "$now" != "$SCHED_BEFORE" ]; then
    echo "$SCHED_BEFORE" > "$SCHED_PATH" 2>/dev/null
    echo "[restore] scheduler -> [$(cat "$SCHED_PATH")]"
  fi
  local qos qdev
  qos=$(cat $CG/io.cost.qos 2>/dev/null || true)
  qdev=$(cat "/sys/block/$DEVNAME/dev" 2>/dev/null || true)
  if [ -n "$qdev" ] && grep -qE "(^| )1 (auto|user)" <<< "$qos"; then
    echo "$qdev 0 auto 100 100000 20000 100 100000" > $CG/io.cost.qos 2>/dev/null
    echo "[restore] io.cost.qos -> [$(cat $CG/io.cost.qos)]"
  fi
  pkill -f 'loader_q_fifo|loader_arb_feedback|loader_q_none' 2>/dev/null
  sleep 0.3
  for t in ta t2 t3 t4 t5 t6 t7 t8 t9 t10 t11 t12 t13 t14 t15 t16; do
    [ -d "$CG/d1_$t" ] && rmdir "$CG/d1_$t" 2>/dev/null
  done
  chmod -R a+rX "$OUT" 2>/dev/null
}
trap restore EXIT

if [ ! -f "$FILE" ]; then
  fallocate -l 2G "$FILE" || true
fi
dd if=/dev/zero of="$FILE" bs=1M count=2048 status=none
sync
printf '+io' > "$CG/cgroup.subtree_control" 2>/dev/null || true

RT_B=10; RAMP_B=1

mkcg() { mkdir -p "$CG/d1_$1"; }

spawn_fio() {
  local nm="$1" od="$2" cgn="$3" rt="$4" ramp="$5"; shift 5
  mkdir -p "$od"
  bash -c "set -e
printf '%s\n' \"\$BASHPID\" > $CG/d1_$cgn/cgroup.procs
exec fio --name=w$nm --filename=$FILE --direct=1 --ioengine=io_uring --bs=4k \
 --rw=randread --iodepth=1 --size=2G --time_based=1 --randrepeat=0 \
 --runtime=$rt --ramp_time=$ramp --clat_percentiles=1 --slat_percentiles=1 \
 --percentile_list=50:90:99:99.9:99.99 --output-format=json --output=$od/${nm}_fio.json \
 --write_lat_log=$od/${nm}_lat \
 $*" > "$od/${nm}_console.out" 2>&1 &
  echo $!
}

set_sched() {
  local want="$1"; local now
  now=$(cat "$SCHED_PATH")
  if ! grep -q "\b$want\b" <<< "$now"; then echo "ERR: $want not available in [$now]"; return 1; fi
  echo "$want" > "$SCHED_PATH"
  echo "[sched] -> [$(cat "$SCHED_PATH")]"
}
kill_loaders() { pkill -f 'loader_q_fifo|loader_arb_feedback|loader_q_none' 2>/dev/null; sleep 0.5; }
dump_maps() {
  local f="$1"; : > "$f"
  echo "== $(date +%H:%M:%S.%N)" >> "$f"
  for m in tenant_state hctx_state fb_state fb_hold_cnt active_cgroup_ids policy_meta; do
    echo "--- map $m" >> "$f"
    bpftool map dump name "$m" 2>&1 | head -400 >> "$f"
  done
}

: > "$OUT/summary.txt"

echo "== Phase B: cgroup scaling (4K QD1 randread x n, ${RT_B}s + ${RAMP_B}s ramp)" | tee -a "$OUT/summary.txt"
run_scaling() { # N STACK LABEL
  local n=$1 stack=$2 od="$OUT/B_${1}cg_$3" i
  mkdir -p "$od"
  for ((i=1;i<=n;i++)); do mkcg "t$i"; done
  if [ "$stack" != native ]; then
    set_sched scx_io
    local prios=(0 5 2 5) ws=(400 100 200 100) pi wi
    for ((i=1;i<=n;i++)); do
      if [ "$stack" = arbfb ]; then pi=${prios[$(( (i-1)%4 ))]}; wi=${ws[$(( (i-1)%4 ))]};
      else pi=0; wi=400; fi
      "$LOADER_DIR/loader_q_fifo" -c "$CG/d1_t$i" -p $pi -W $wi >"$od/q_t$i.log" 2>&1 &
    done
    if [ "$stack" = arbfb ]; then "$LOADER_DIR/loader_arb_feedback" >"$od/arb.log" 2>&1 & fi
    sleep 0.8
  fi
  for ((i=1;i<=n;i++)); do
    spawn_fio "t$i" "$od/cg$i" "t$i" "$RT_B" "$RAMP_B" "--randseed=$((i*7919))" >/dev/null
  done
  sleep 2
  local t0
  t0=$(date +%s)
  while pgrep -f "output=$od/" >/dev/null 2>&1; do
    sleep 1; [ $(( $(date +%s) - t0 )) -gt 120 ] && { pkill -9 -f "output=$od/"; break; }
  done
  dump_maps "$od/maps_final.txt"
  kill_loaders
  set_sched none
  echo "[done] B n=$n stack=$stack -> $od" | tee -a "$OUT/summary.txt"
}
run_scaling 2 native native
run_scaling 2 qfifo qfifo
run_scaling 4 native native
run_scaling 4 qfifo qfifo
run_scaling 8 native native
run_scaling 8 qfifo qfifo
run_scaling 16 native native
run_scaling 16 qfifo qfifo
run_scaling 4 arbfb arbfb

echo "== Phase C: vertical (single cgroup, native none)" | tee -a "$OUT/summary.txt"
set_sched none
mkcg ta
echo "target=1000" > "$CG/d1_ta/io.latency" 2>/dev/null && echo "[C1] io.latency target=1000 set" || echo "[C1] io.latency set FAILED"
spawn_fio ta "$OUT/C1_native_iolat" ta "$RT_B" "$RAMP_B" >/dev/null
t0=$(date +%s); while pgrep -f "output=$OUT/C1_native_iolat/" >/dev/null 2>&1; do sleep 1; [ $(( $(date +%s) - t0 )) -gt 90 ] && pkill -9 -f "output=$OUT/C1_native_iolat/" && break; done
echo 0 > "$CG/d1_ta/io.latency" 2>/dev/null || true
echo "[done] C1" | tee -a "$OUT/summary.txt"

QDEV=$(cat "/sys/block/$DEVNAME/dev" 2>/dev/null || true)
echo "$QDEV enable=1 ctrl=user rpct=95.00 rlat=100000 wpct=95.00 wlat=100000 min=50.00 max=150.00" > $CG/io.cost.qos 2>/dev/null \
  && echo "[C2] io.cost.qos enabled on $QDEV (rlat/wlat=100000us unreachable)" \
  || echo "[C2] io.cost.qos set FAILED"
spawn_fio ta "$OUT/C2_native_iocost" ta "$RT_B" "$RAMP_B" >/dev/null
t0=$(date +%s); while pgrep -f "output=$OUT/C2_native_iocost/" >/dev/null 2>&1; do sleep 1; [ $(( $(date +%s) - t0 )) -gt 90 ] && pkill -9 -f "output=$OUT/C2_native_iocost/" && break; done
echo "$QDEV enable=0 ctrl=user rpct=95.00 rlat=20000 wpct=95.00 wlat=20000 min=50.00 max=150.00" > $CG/io.cost.qos 2>/dev/null || true
echo "[done] C2  root_io_cost_qos_now=[$(cat $CG/io.cost.qos)]" | tee -a "$OUT/summary.txt"

echo "[env] scheduler_final=[$(cat $SCHED_PATH)]"
echo "[env] root_io_cost_qos_final=[$(cat $CG/io.cost.qos)]"
echo "== phaseBC complete" | tee -a "$OUT/summary.txt"
rm -f "$FILE"
chmod -R a+rX "$OUT"
exit 0
