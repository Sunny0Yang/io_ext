#!/bin/bash
# d1_overhead_probe_bm.sh — D1 framework overhead & scalability probe (bare-metal, kernel 7.3.1-ioext2)
#
# Replicates the kvmhost D1 (framework overhead / scalability) definition on bm:
#   Phase A  decomposition matrix, single tenant 4K QD1 randread solo:
#     A1 native none (no framework)
#     A2 scx_io bare (no loader, kernel fail-open path)
#     A3 scx_io + loader_q_none (bypass DIRECT policy, "empty" loader)
#     A4 scx_io + loader_q_fifo (p0/W400, queue only, no arbiter)
#     A5 A4 + loader_arb_feedback (full f1-equivalent single-tenant stack)
#     A6 A5 + idle q_fifo loaders tb/tc/td (p5/W100, p2/W200, p5/W100) = exact f1 T1 replica
#     A7 native none repeat (drift check)
#   Phase B  cgroup horizontal scaling n in {2,4,8,16}: native none vs scx_io+q_fifo(all p0/W400)
#            + one arbfb run at n=4 with f1 prios (a0/400 b5/100 c2/200 d5/100)
#   Phase C  vertical: native + io.latency target=1ms on tenant cgroup; native + root io.cost.qos
#            with unreachable lat target (restored to enable=0 after)
#   Phase D  telemetry during A5/A6/B-arbfb: bpftool map dumps (tenant_state/hctx_state/fb_state/
#            fb_hold_cnt/active_cgroup_ids) + dmesg tail delta
#
#  - per-IO lat logs (--write_lat_log): *_slat.log / *_clat.log / *_lat.log per tenant
#  - file IO only, testfile <= 8G under iocost_probe
#  - restores scheduler and root io.cost.qos at exit; single probe <= 60s
#
# Usage (run as root on bm, inside flock): d1_overhead_probe_bm.sh [--out DIR] [--quick]
set -uo pipefail
OUT="/opt/ioext/dyn_tenant_regions/iocost_probe/d1_probe_$(date +%H%M%S)"
LOADER_DIR="/opt/ioext/io-ext/userspace"
CG="/sys/fs/cgroup"
FILE="$OUT/d1probe.bin"
TARGET_FILE=""
QUICK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --quick) QUICK=1; shift ;;
    *) echo "unknown arg $1" >&2; exit 1 ;;
  esac
done
[ -x "$LOADER_DIR/loader_q_fifo" ] || { echo "loader_q_fifo missing" >&2; exit 1; }
mkdir -p "$OUT"
FILE="$OUT/d1probe.bin"

DEVNAME=$(findmnt -nvo SOURCE -T "$OUT" 2>/dev/null | xargs basename 2>/dev/null || true)
if [ -z "$DEVNAME" ] || [ ! -e "/sys/block/$DEVNAME" ]; then
  PART=$(lsblk -no pkname "$(findmnt -nvo SOURCE -T "$OUT" 2>/dev/null)" 2>/dev/null | head -1)
  DEVNAME="${PART:-nvme0n1}"
fi
SCHED_PATH="/sys/block/$DEVNAME/queue/scheduler"
SCHED_BEFORE=$(cat "$SCHED_PATH")
IOSTAT_BEFORE=$(cat /sys/fs/cgroup/io.stat 2>/dev/null | head -5)
echo "[env] kernel=$(uname -r) fio=$(fio --version) dev=$DEVNAME sched=[$SCHED_BEFORE]"
echo "[env] root_io_cost_qos_before=[$(cat $CG/io.cost.qos 2>/dev/null)]"

restore() {
  local now; now=$(cat "$SCHED_PATH" 2>/dev/null || true)
  if [ "$now" != "$SCHED_BEFORE" ]; then
    echo "$SCHED_BEFORE" > "$SCHED_PATH" 2>/dev/null
    echo "[restore] scheduler -> [$(cat "$SCHED_PATH")]"
  fi
  # root red line: io.cost.qos must end enable=0
  local qos qdev
  qos=$(cat $CG/io.cost.qos 2>/dev/null || true)
  qdev=$(cat "/sys/block/$DEVNAME/dev" 2>/dev/null || true)
  if [ -n "$qdev" ] && grep -qE "(^| )1 (auto|user)" <<< "$qos"; then
    echo "$qdev 0 auto 100 100000 20000 100 100000" > $CG/io.cost.qos 2>/dev/null
    echo "[restore] io.cost.qos -> [$(cat $CG/io.cost.qos)]"
  fi
  pkill -f 'loader_q_fifo|loader_arb_feedback|loader_q_none' 2>/dev/null
  sleep 0.3
  for t in ta tb tc td t2 t3 t4 t5 t6 t7 t8 t9 t10 t11 t12 t13 t14 t15 t16; do
    [ -d "$CG/d1_$t" ] && rmdir "$CG/d1_$t" 2>/dev/null
  done
  chmod -R a+rX "$OUT" 2>/dev/null
}
trap restore EXIT

# ---- testfile (<= 2G); MUST be zero-filled: fallocate-only leaves unwritten
# extents whose reads are served without device IO even under O_DIRECT ----
if [ ! -f "$FILE" ] || [ "$(stat -c%s "$FILE")" -ne 2147483648 ]; then
  fallocate -l 2G "$FILE" || true
fi
dd if=/dev/zero of="$FILE" bs=1M count=2048 status=none
sync
printf '+io' > "$CG/cgroup.subtree_control" 2>/dev/null || true

RT_A=15; RAMP_A=2
RT_B=10; RAMP_B=1
if [ "$QUICK" = 1 ]; then RT_A=8; RT_B=6; fi

mkcg() { # name
  mkdir -p "$CG/d1_$1"
}

# spawn_fio NAME OUTDIR CGNAME RUNTIME RAMP [extra fio args...] -> echoes wrapper pid
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

wait_fio() { local p=$1; local t0=$(date +%s); while kill -0 $p 2>/dev/null; do
  sleep 0.5; [ $(( $(date +%s) - t0 )) -gt 90 ] && { kill -9 $p; echo "[warn] fio $p killed after 90s"; }; done; }

set_sched() { # sched
  local want="$1"; local now=$(cat "$SCHED_PATH")
  if ! grep -q "\b$want\b" <<< "$now"; then echo "ERR: $want not available in [$now]"; return 1; fi
  echo "$want" > "$SCHED_PATH"
  echo "[sched] -> [$(cat "$SCHED_PATH")]"
}

kill_loaders() { pkill -f 'loader_q_fifo|loader_arb_feedback|loader_q_none' 2>/dev/null; sleep 0.5; }

dump_maps() { # outfile
  local f="$1"; : > "$f"
  echo "== $(date +%H:%M:%S.%N)" >> "$f"
  for m in tenant_state hctx_state fb_state fb_hold_cnt active_cgroup_ids policy_meta; do
    echo "--- map $m" >> "$f"
    bpftool map dump name "$m" 2>&1 | head -400 >> "$f"
  done
}

log_dmesg() { dmesg | tail -40 >> "$1" 2>/dev/null; }

: > "$OUT/summary.txt"

run_native_bg() { # OUTDIR -> nothing; native none A-level run
  local od=$1 p
  p=$(spawn_fio ta "$od" ta "$RT_A" "$RAMP_A")
  wait_fio "$p"
}

########## Phase A ##########
echo "== Phase A: decomposition matrix (4K QD1 randread solo, ${RT_A}s + ${RAMP_A}s ramp)" | tee -a "$OUT/summary.txt"

# A1 native none (no cgroup wrapper? use cgroup for parity of environment minus control)
mkcg ta
run_native_bg "$OUT/A1_native_none"
echo "[done] A1" | tee -a "$OUT/summary.txt"

# A2 scx_io bare, no loader
set_sched scx_io
run_native_bg "$OUT/A2_scxio_bare"
echo "[done] A2" | tee -a "$OUT/summary.txt"

# A3 scx_io + loader_q_none on ta
"$LOADER_DIR/loader_q_none" -c "$CG/d1_ta" -p 0 -W 400 >"$OUT/A3_loader.log" 2>&1 &
sleep 0.5
run_native_bg "$OUT/A3_scxio_qnone"
kill_loaders
echo "[done] A3" | tee -a "$OUT/summary.txt"

# A4 scx_io + q_fifo only
"$LOADER_DIR/loader_q_fifo" -c "$CG/d1_ta" -p 0 -W 400 >"$OUT/A4_loader.log" 2>&1 &
sleep 0.5
run_native_bg "$OUT/A4_scxio_qfifo"
kill_loaders
echo "[done] A4" | tee -a "$OUT/summary.txt"

# A5 q_fifo + arb_feedback (single tenant, fresh arbiter)
"$LOADER_DIR/loader_q_fifo" -c "$CG/d1_ta" -p 0 -W 400 >"$OUT/A5_loader.log" 2>&1 &
sleep 0.2
"$LOADER_DIR/loader_arb_feedback" >"$OUT/A5_arb.log" 2>&1 &
sleep 0.5
( for i in $(seq 1 12); do dump_maps "$OUT/A5_maps_snap_$i.txt"; log_dmesg "$OUT/A5_dmesg_tail.txt"; sleep 2; done ) &
local_bgpid=$!
run_native_bg "$OUT/A5_scxio_arbfb"
kill_loaders; kill $local_bgpid 2>/dev/null
echo "[done] A5" | tee -a "$OUT/summary.txt"

# A6 = A5 + idle tb/tc/td loaders (exact f1 T1 replica)
mkcg tb; mkcg tc; mkcg td
"$LOADER_DIR/loader_q_fifo" -c "$CG/d1_ta" -p 0 -W 400 >"$OUT/A6_loader_ta.log" 2>&1 &
"$LOADER_DIR/loader_q_fifo" -c "$CG/d1_tb" -p 5 -W 100 >"$OUT/A6_loader_tb.log" 2>&1 &
"$LOADER_DIR/loader_q_fifo" -c "$CG/d1_tc" -p 2 -W 200 >"$OUT/A6_loader_tc.log" 2>&1 &
"$LOADER_DIR/loader_q_fifo" -c "$CG/d1_td" -p 5 -W 100 >"$OUT/A6_loader_td.log" 2>&1 &
sleep 0.2
"$LOADER_DIR/loader_arb_feedback" >"$OUT/A6_arb.log" 2>&1 &
sleep 0.5
( for i in $(seq 1 12); do dump_maps "$OUT/A6_maps_snap_$i.txt"; log_dmesg "$OUT/A6_dmesg_tail.txt"; sleep 2; done ) &
local_bgpid=$!
run_native_bg "$OUT/A6_scxio_arbfb_f1shape"
kill_loaders; kill $local_bgpid 2>/dev/null
echo "[done] A6" | tee -a "$OUT/summary.txt"

# A7 native none repeat (drift)
set_sched none
run_native_bg "$OUT/A7_native_none_rep"
echo "[done] A7" | tee -a "$OUT/summary.txt"

########## Phase B: cgroup scaling ##########
echo "== Phase B: cgroup scaling (4K QD1 randread x n, ${RT_B}s + ${RAMP_B}s ramp)" | tee -a "$OUT/summary.txt"
run_scaling() { # N STACK(native|qfifo|arbfb)
  local n=$1 stack=$2 od="$OUT/B_${n}cg_$3" i p
  mkdir -p "$od"
  for ((i=1;i<=n;i++)); do mkcg "t$i"; done
  if [ "$stack" != native ]; then
    set_sched scx_io
    local prios=(0 5 2 5); local ws=(400 100 200 100)
    for ((i=1;i<=n;i++)); do
      local pi=0 wi=400
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
  # wait for all fio
  sleep 2
  local t0=$(date +%s)
  while pgrep -f "output=$od/" >/dev/null 2>&1; do
    sleep 1; [ $(( $(date +%s) - t0 )) -gt 120 ] && { pkill -9 -f "output=$od/"; break; }
  done
  dump_maps "$od/maps_final.txt"
  kill_loaders
  set_sched none
  echo "[done] B n=$n stack=$2 -> $od" | tee -a "$OUT/summary.txt"
}
run_scaling 2 native "native"
run_scaling 2 qfifo "qfifo"
run_scaling 4 native "native"
run_scaling 4 qfifo "qfifo"
run_scaling 8 native "native"
run_scaling 8 qfifo "qfifo"
run_scaling 16 native "native"
run_scaling 16 qfifo "qfifo"
run_scaling 4 arbfb "arbfb"

########## Phase C: vertical ##########
echo "== Phase C: vertical (single cgroup, native none)" | tee -a "$OUT/summary.txt"
set_sched none
mkcg ta
# C1 io.latency target=1ms on tenant cgroup (unreachable, inactive semantics)
echo "1000" > "$CG/d1_ta/io.latency" 2>/dev/null && echo "[C1] io.latency=1000 set" || echo "[C1] io.latency set FAILED"
run_native_bg "$OUT/C1_native_iolat"
echo 0 > "$CG/d1_ta/io.latency" 2>/dev/null || true
echo "[done] C1" | tee -a "$OUT/summary.txt"

# C2 root io.cost.qos with unreachable lat target (200ms); restored enable=0 by trap
QDEV=$(cat "/sys/block/$DEVNAME/dev" 2>/dev/null || true)
echo "$QDEV 1 auto 100 100000 200000 100 100000" > $CG/io.cost.qos 2>/dev/null \
  && echo "[C2] io.cost.qos enabled on $QDEV (lat=200000us unreachable)" \
  || echo "[C2] io.cost.qos set FAILED"
run_native_bg "$OUT/C2_native_iocost"
echo "$QDEV 0 auto 100 100000 20000 100 100000" > $CG/io.cost.qos 2>/dev/null || true
echo "[done] C2  root_io_cost_qos_now=[$(cat $CG/io.cost.qos)]" | tee -a "$OUT/summary.txt"

echo "[env] scheduler_final=[$(cat $SCHED_PATH)]"
echo "[env] root_io_cost_qos_final=[$(cat $CG/io.cost.qos)]"
echo "== probe complete: $OUT" | tee -a "$OUT/summary.txt"
rm -f "$FILE"
chmod -R a+rX "$OUT"
exit 0
