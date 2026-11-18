#!/bin/bash
# guest_mixed_high_iops_repro.sh — virtme-ng guest repro for mixed deep-QD load +
# io_ext on a *leaf* test disk (good) vs policy pinned on root cgroup (bad: can
# starve jbd2 / sync on the same queue).  Evidence -> $REPRO_DIR/
set -uo pipefail

REPRO_DIR="${REPRO_DIR:?}"
BASE_DIR="${HS_BASE_DIR:?}"
DEVICE="${HS_DEVICE:-vda}"
LOADER_DIR="$BASE_DIR/userspace/io/build"
BT="$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
CG_ROOT="/sys/fs/cgroup"
DURATION="${HS_DURATION:-45}"
ATTACH_ROOT="${HS_ATTACH_ROOT:-0}"   # 1 = deliberately attach q_fifo on root (anti-pattern)

mkdir -p "$REPRO_DIR"
log() { printf '[mixed_repro] %s\n' "$*" | tee -a "$REPRO_DIR/repro.log" >&2; }

dmesg -C 2>/dev/null || true
grep -q scx_io /proc/kallsyms || { log "FATAL: guest kernel lacks scx_io"; exit 3; }
mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf || exit 3
echo '+io' > "$CG_ROOT/cgroup.subtree_control" 2>/dev/null || true

# Tenants (same geometry as dynamic-tenant T4 coexistence, no rate caps).
for n in ta tb tc td; do
  mkdir -p "$CG_ROOT/$n"
done

log "kernel=$(uname -r) device=$DEVICE duration=${DURATION}s attach_root_policy=$ATTACH_ROOT"
log "IOSCHED_EXT_DEBUG in kernel: $(grep -q 'CONFIG_IOSCHED_EXT_DEBUG=y' /proc/config.gz 2>/dev/null && zcat /proc/config.gz | grep IOSCHED_EXT_DEBUG || grep IOSCHED_EXT /boot/config-$(uname -r) 2>/dev/null || echo 'unknown')"

echo mq-deadline > "/sys/block/$DEVICE/queue/scheduler" 2>/dev/null || true
echo 1023 > "/sys/block/$DEVICE/queue/nr_requests" 2>/dev/null || true

# Churn the guest rootfs (9p overlay) to pull host-side metadata/journal traffic
# while io_ext is (optionally) attached at root cgroup — scope-mismatch repro.
if [ "${HS_ROOTFS_CHURN:-1}" = "1" ]; then
  ( while true; do
      dd if=/dev/zero of=/tmp/ioext_root_churn bs=4096 count=64 conv=fdatasync 2>/dev/null
      sleep 0.02
    done ) &
  JBD_PID=$!
  log "background fdatasync churn on /tmp (rootfs scope coupling)"
fi

# Resident arbiter + device scx_io
log "--- attach arb_wdrr ---"
"$LOADER_DIR/loader_arb_wdrr" >> "$REPRO_DIR/arb.log" 2>&1 &
ARB=$!
sleep 2
echo scx_io > "/sys/block/$DEVICE/queue/scheduler" || { log "scx_io switch failed"; exit 4; }

attach_q() {
  local cgpath="$1" name="$2" prio="$3" weight="$4"
  "$LOADER_DIR/loader_q_fifo" -c "$cgpath" -p "$prio" -W "$weight" \
    >> "$REPRO_DIR/${name}_q.log" 2>&1 &
  echo $!
}

if [ "$ATTACH_ROOT" = "1" ]; then
  log "WARN: attaching q_fifo on ROOT cgroup (reproduces scope mistake)"
  QROOT=$(attach_q "$CG_ROOT" root 0 400)
  sleep 1
fi

QTA=$(attach_q "$CG_ROOT/ta" ta 0 400)
QTB=$(attach_q "$CG_ROOT/tb" tb 5 100)
QTC=$(attach_q "$CG_ROOT/tc" tc 2 200)
QTD=$(attach_q "$CG_ROOT/td" td 5 100)
sleep 2

fio_common=(--ioengine=io_uring --direct=1 --norandommap=1 --randrepeat=1
  --group_reporting=0 --time_based=1 --runtime="$DURATION")

run_fio() {
  local tag="$1" cg="$2" rw="$3" bs="$4" qd="$5"
  local out="$REPRO_DIR/fio_${tag}.json"
  (
    echo "$BASHPID" > "$CG_ROOT/$cg/cgroup.procs"
    exec fio --name="$tag" --filename="/dev/$DEVICE" --rw="$rw" \
      --bs="$bs" --iodepth="$qd" --output="$out" "${fio_common[@]}"
  ) >> "$REPRO_DIR/fio_${tag}.log" 2>&1 &
  PIDS+=("$!")
}

log "--- start mixed fio (target: high aggregate IOPS on $DEVICE) ---"
PIDS=()
run_fio a ta randread 4k 1
run_fio b tb randread 64k 8
run_fio c tc randread 64k 8
run_fio d td randwrite 4k 64

# Mid-run hot swap on tenant B (fifo -> mq_deadline -> fifo) under load.
(
  sleep $((DURATION / 3))
  log "--- hot swap B: fifo -> mq_deadline ---"
  kill -TERM "$QTB" 2>/dev/null || true
  sleep 2
  QTB=$("$LOADER_DIR/loader_q_mq_deadline" -c "$CG_ROOT/tb" -p 5 -W 100 \
    >> "$REPRO_DIR/tb_q_mqd.log" 2>&1 & echo $!)
  sleep $((DURATION / 3))
  log "--- hot swap B: mq_deadline -> fifo ---"
  kill -TERM "$QTB" 2>/dev/null || true
  sleep 2
  QTB=$(attach_q "$CG_ROOT/tb" tb 5 100)
) &
SWAP=$!

# 1 Hz: tenant_state + policy_meta + dmesg tail markers
(
  end=$((SECONDS + DURATION + 15))
  while [ "$SECONDS" -lt "$end" ]; do
    ts=$(date -Iseconds)
    "$BT" map dump name tenant_state > "$REPRO_DIR/.tenant_state.json" 2>/dev/null || true
    echo "$ts tenant_state_dump=$([ -s "$REPRO_DIR/.tenant_state.json" ] && wc -c < "$REPRO_DIR/.tenant_state.json" || echo 0)" \
      >> "$REPRO_DIR/sampler.log"
    dmesg | tail -n 5 | rg -i 'evacuate stall|forcing teardown|jbd2|hung_task|blocked' \
      >> "$REPRO_DIR/dmesg_hits.log" 2>/dev/null || true
    sleep 1
  done
) &
SAM=$!

FAIL=0
for pid in "${PIDS[@]}"; do
  if ! wait "$pid" 2>/dev/null; then FAIL=$((FAIL+1)); fi
done
wait "$SWAP" 2>/dev/null || true
kill "$SAM" 2>/dev/null || true
[ -n "${JBD_PID:-}" ] && kill "$JBD_PID" 2>/dev/null || true

dmesg > "$REPRO_DIR/dmesg_final.txt"
"$BT" map dump name tenant_state > "$REPRO_DIR/tenant_state_final.json" 2>/dev/null || true
[ -f /sys/fs/bpf/ioext_policy_meta ] && \
  "$BT" map dump pinned /sys/fs/bpf/ioext_policy_meta > "$REPRO_DIR/policy_meta_final.json" 2>/dev/null || true

# Aggregate IOPS from fio JSON
python3 - <<PY >> "$REPRO_DIR/summary.txt" 2>&1
import json, glob, os
repro = "$REPRO_DIR"
total_iops = 0.0
for path in sorted(glob.glob(os.path.join(repro, "fio_*.json"))):
    try:
        j = json.load(open(path))
        jobs = j.get("jobs") or []
        iops = sum(x["read"].get("iops", 0) + x["write"].get("iops", 0) for x in jobs)
        total_iops += iops
        print(f"{os.path.basename(path)}: {iops:.1f} IOPS")
    except Exception as e:
        print(f"{os.path.basename(path)}: parse_error {e}")
print(f"aggregate_steady_iops_sum: {total_iops:.1f}")
PY

log "--- detach loaders (watch for hang) ---"
DETACH_PIDS="$QTA $QTB $QTC $QTD $ARB"
[ -n "${QROOT:-}" ] && DETACH_PIDS="$DETACH_PIDS $QROOT"
timeout 120 bash -c "
  kill -TERM $DETACH_PIDS 2>/dev/null
  sleep 5
  echo none > /sys/block/$DEVICE/queue/scheduler
" >> "$REPRO_DIR/detach.log" 2>&1
DETACH_RC=$?
if [ "$DETACH_RC" -eq 124 ]; then
  log "FAIL: detach/sync path timed out (120s) — possible wedge"
  FAIL=$((FAIL+1))
fi

if rg -q 'evacuate stall limit reached|evacuate stalled' "$REPRO_DIR/dmesg_final.txt"; then
  log "HIT: evacuate stall messages in dmesg (kernel teardown wedge class)"
  FAIL=$((FAIL+1))
fi

if [ "$FAIL" -gt 0 ]; then
  log "repro finished with FAIL indicators (see summary.txt, dmesg_final.txt)"
  exit 1
fi
log "repro finished: no timeout/evacuate-stall signals (virtme IOPS ceiling << 800k)"
exit 0
