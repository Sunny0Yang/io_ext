#!/usr/bin/env bash
# D1 v2 device-recovery watcher for 86.
# Every WATCH_INTERVAL s, run the SAME solo native fio probe as the d1_ladder_v2_bm.sh
# health gate (4K randread QD1 io_uring direct, runtime 10s + ramp 1s, seed 4242,
# cpu 2) and log the IOPS.  Exit 0 after two consecutive probes >= HEALTH_MIN_IOPS
# (device recovered -> main session can re-GO the v2 experiment); exit 1 after the
# probe budget is exhausted without recovery.
# Read-only with respect to the experiment environment: it only creates its own
# probe file (d1_health_watch.bin) and runs fio; no scheduler/cgroup/loader changes.

set -u
WATCH_INTERVAL=900
PROBES=32
HEALTH_MIN_IOPS=50000
LOG=/opt/ioext/experiments/dyn_tenant_bm/results/d1_health_watch.log
FIO_BIN=/home/anon/d1_health_watch.bin

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

log "watcher start (interval=${WATCH_INTERVAL}s, probes=${PROBES}, min_iops=${HEALTH_MIN_IOPS})"

pass_streak=0
for i in $(seq 1 "$PROBES"); do
  json="$(ssh bm "
    [ -f ${FIO_BIN} ] || dd if=/dev/zero of=${FIO_BIN} bs=1M count=2048 status=none
    fio --name=hp --filename=${FIO_BIN} --direct=1 --ioengine=io_uring --bs=4k \
      --rw=randread --iodepth=1 --size=2G --time_based=1 --randrepeat=0 --randseed=4242 \
      --cpus_allowed=2 --runtime=10 --ramp_time=1 --clat_percentiles=1 \
      --percentile_list=50:99 --output-format=json 2>/dev/null" 2>>"$LOG")"
  iops="$(printf '%s' "$json" | python3 -I -c "import json,sys
try:
    j=json.load(sys.stdin)
    print('%.0f' % j['jobs'][0]['read']['iops'])
except Exception:
    print('ERR')")"
  log "probe $i: iops=${iops}"
  if [ "$iops" != "ERR" ] && [ "$(printf '%.0f' "$iops")" -ge "$HEALTH_MIN_IOPS" ]; then
    pass_streak=$((pass_streak+1))
    if [ "$pass_streak" -ge 2 ]; then
      log "watcher: RECOVERED (${pass_streak} consecutive passes) -> exit 0"
      exit 0
    fi
  else
    pass_streak=0
  fi
  sleep "$WATCH_INTERVAL"
done

log "watcher: no recovery within budget -> exit 1"
exit 1
