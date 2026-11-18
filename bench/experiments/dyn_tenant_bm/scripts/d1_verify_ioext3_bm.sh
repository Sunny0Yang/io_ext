#!/bin/bash
# d1_verify_ioext3_bm.sh — D1 fast-path verification on bm, kernel 7.3.1-ioext3
# (replicates d1_overhead_probe_bm.sh §1 A5 probe, reduced to A1 + A5')
#
#   A1  native none, single tenant 4K QD1 randread (control)
#   A5' scx_io + loader_q_fifo(p0/W400) + loader_arb_feedback (fast-path stack)
#
# Criteria (vs D1_overhead_bm ioext2 baseline A5: clat p99 3944us, IOPS 10.4k):
#   A5' clat p99 <= 100us (expect ~50us), IOPS >= 45k, clat >500us frac ~ 0%
#   A5' slat p99 <= 10us;  A1 native clat p99 ~ 50us
# Telemetry: fastpath_selects counter via bpftool map dump cgroup_drr (snapshots)
#
# Usage (run as root on bm; caller holds /tmp/ioxhost_work.lock): d1_verify_ioext3_bm.sh [--out DIR]
set -uo pipefail
OUT="/opt/ioext/dyn_tenant_regions/iocost_probe/d1verify_ioext3_$(date +%H%M%S)"
LOADER_DIR="/opt/ioext/io-ext/userspace"
CG="/sys/fs/cgroup"
RT=15; RAMP=2
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 1 ;;
  esac
done
[ -x "$LOADER_DIR/loader_q_fifo" ] || { echo "loader_q_fifo missing" >&2; exit 1; }
[ -x "$LOADER_DIR/loader_arb_feedback" ] || { echo "loader_arb_feedback missing" >&2; exit 1; }
mkdir -p "$OUT"
FILE="$OUT/d1verify.bin"

DEVNAME=$(findmnt -nvo SOURCE -T "$OUT" 2>/dev/null | xargs basename 2>/dev/null || true)
if [ -z "$DEVNAME" ] || [ ! -e "/sys/block/$DEVNAME" ]; then
  PART=$(lsblk -no pkname "$(findmnt -nvo SOURCE -T "$OUT" 2>/dev/null)" 2>/dev/null | head -1)
  DEVNAME="${PART:-nvme0n1}"
fi
SCHED_PATH="/sys/block/$DEVNAME/queue/scheduler"
SCHED_BEFORE=$(cat "$SCHED_PATH")
echo "[env] kernel=$(uname -r) fio=$(fio --version) dev=$DEVNAME sched=[$SCHED_BEFORE]"
echo "[env] root_io_cost_qos_before=[$(cat $CG/io.cost.qos 2>/dev/null)]"

restore() {
  # red line: finish with scheduler [none] explicitly (writing the full list
  # back re-selects the LAST name, which reactivated scx_io on the first run)
  local now; now=$(cat "$SCHED_PATH" 2>/dev/null || true)
  if ! grep -q '^\[none\]' <<< "$now"; then
    echo none > "$SCHED_PATH" 2>/dev/null
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
  pkill -f 'loader_q_fifo|loader_arb_feedback' 2>/dev/null
  sleep 0.3
  [ -d "$CG/d1_vta" ] && rmdir "$CG/d1_vta" 2>/dev/null
  chmod -R a+rX "$OUT" 2>/dev/null
}
trap restore EXIT

# 2G testfile, MUST be zero-filled (fallocate-only leaves unwritten extents)
dd if=/dev/zero of="$FILE" bs=1M count=2048 status=none
sync
printf '+io' > "$CG/cgroup.subtree_control" 2>/dev/null || true
mkdir -p "$CG/d1_vta"

spawn_fio() { # NAME OUTDIR
  local nm="$1" od="$2"
  shift 2
  mkdir -p "$od"
  bash -c "set -e
printf '%s\n' \"\$BASHPID\" > $CG/d1_vta/cgroup.procs
exec fio --name=w$nm --filename=$FILE --direct=1 --ioengine=io_uring --bs=4k \
 --rw=randread --iodepth=1 --size=2G --time_based=1 --randrepeat=0 \
 --runtime=$RT --ramp_time=$RAMP --clat_percentiles=1 --slat_percentiles=1 \
 --percentile_list=50:90:99:99.9:99.99 --output-format=json --output=$od/${nm}_fio.json \
 --write_lat_log=$od/${nm}_lat \
 $*" > "$od/${nm}_console.out" 2>&1 &
  echo $!
}
wait_fio() { local p=$1; local t0=$(date +%s); while kill -0 $p 2>/dev/null; do
  sleep 0.5; [ $(( $(date +%s) - t0 )) -gt 120 ] && { kill -9 $p; echo "[warn] fio $p killed after 120s"; }; done; }

set_sched() {
  local want="$1"; local now=$(cat "$SCHED_PATH")
  if ! grep -q "\b$want\b" <<< "$now"; then echo "ERR: $want not available in [$now]"; return 1; fi
  echo "$want" > "$SCHED_PATH"
  echo "[sched] -> [$(cat "$SCHED_PATH")]"
}
kill_loaders() { pkill -f 'loader_q_fifo|loader_arb_feedback' 2>/dev/null; sleep 0.5; }
dump_maps() {
  local f="$1"; : > "$f"
  echo "== $(date +%H:%M:%S.%N)" >> "$f"
  for m in cgroup_drr tenant_state hctx_state fb_state fb_hold_cnt active_cgroup_ids policy_meta; do
    echo "--- map $m" >> "$f"
    bpftool map dump name "$m" 2>&1 | head -400 >> "$f"
  done
}
: > "$OUT/summary.txt"

# ---- A1 native none ----
echo "== A1 native none (4K QD1 randread solo, ${RT}s + ${RAMP}s ramp)" | tee -a "$OUT/summary.txt"
set_sched none
p=$(spawn_fio ta "$OUT/A1_native_none")
wait_fio "$p"
echo "[done] A1" | tee -a "$OUT/summary.txt"

# ---- A5' scx_io + q_fifo(p0/W400) + arb_feedback ----
echo "== A5' scx_io + q_fifo(p0/W400) + arb_feedback (fast-path stack)" | tee -a "$OUT/summary.txt"
set_sched scx_io
"$LOADER_DIR/loader_q_fifo" -c "$CG/d1_vta" -p 0 -W 400 >"$OUT/A5_loader.log" 2>&1 &
sleep 0.2
"$LOADER_DIR/loader_arb_feedback" >"$OUT/A5_arb.log" 2>&1 &
sleep 0.5
( for i in $(seq 1 9); do dump_maps "$OUT/A5_maps_snap_$i.txt"; sleep 2; done ) &
snap_pid=$!
p=$(spawn_fio ta "$OUT/A5_scxio_arbfb")
wait_fio "$p"
kill_loaders; kill $snap_pid 2>/dev/null
echo "[done] A5'" | tee -a "$OUT/summary.txt"

echo "[env] scheduler_final=[$(cat "$SCHED_PATH")]"
echo "[env] root_io_cost_qos_final=[$(cat $CG/io.cost.qos 2>/dev/null)]"
dmesg | tail -40 > "$OUT/dmesg_tail.txt" 2>/dev/null
echo "== verify complete: $OUT" | tee -a "$OUT/summary.txt"
rm -f "$FILE"
chmod -R a+rX "$OUT"
exit 0
