#!/bin/bash
# q_linnos verifier-fix smoke (single-run, whole segment under /tmp/homo_smoke.lock)
# verifier zero-rejection + observable BE interception (throughput < pass-through)
# + graceful-stop stats lost_fast/lost_held == 0 + full cleanup.
DEV=sdb
TS=$(date +%Y%m%d_%H%M%S)
LOG=/tmp/smoke_qlinnos_v2.log
BASE_JSON=/tmp/qlinnos_base_$TS.json
POL_JSON=/tmp/qlinnos_pol_$TS.json
S() { echo 'REDACTED' | sudo -S "$@"; }

echo "=== q_linnos smoke start $TS dev=$DEV ==="

# ---- 0. defensive pre-clean ----
S pkill -TERM -f loader_q_linnos 2>/dev/null; sleep 1
S rm -f /sys/fs/bpf/ioext_policy_meta /sys/fs/bpf/ioext_active_cgroups
S sh -c "echo none > /sys/block/$DEV/queue/scheduler"
sleep 1
echo "pre-clean: scheduler=$(cat /sys/block/$DEV/queue/scheduler) pins=$(S ls /sys/fs/bpf/ | tr '\n' ' ')"

# ---- 1. pass-through baseline (scheduler none) ----
S timeout 20 fio --name=base --filename=/dev/$DEV --rw=randread --bs=4k --direct=1 \
    --iodepth=256 --ioengine=libaio --time_based=1 --runtime=10 --group_reporting \
    --cpus_allowed=1 --output-format=json > "$BASE_JSON" 2>/tmp/qlinnos_base.err
BASE_BW=$(jq -r '.jobs[0].read.bw_bytes' "$BASE_JSON" 2>/dev/null)
BASE_IOPS=$(jq -r '.jobs[0].read.iops' "$BASE_JSON" 2>/dev/null)
echo "baseline(none): bw_bytes=$BASE_BW iops=$BASE_IOPS"

# ---- 2. policy run: scx_io + loader_q_linnos (default table, BE root cgroup) ----
S sh -c "echo scx_io > /sys/block/$DEV/queue/scheduler"
S sh -c "echo 1023 > /sys/block/$DEV/queue/nr_requests"
sleep 1
: > "$LOG"
S /opt/io-ext/userspace/loader_q_linnos -c /sys/fs/cgroup > "$LOG" 2>&1 &
sleep 3
if ! S pgrep -f loader_q_linnos > /dev/null; then
    echo "LOADER_DIED_EARLY (verifier/load failure?)"
else
    echo "loader alive after 3s (verifier accepted), pins: $(S ls /sys/fs/bpf/ | tr '\n' ' ')"
fi
S timeout 20 fio --name=pol --filename=/dev/$DEV --rw=randread --bs=4k --direct=1 \
    --iodepth=256 --ioengine=libaio --time_based=1 --runtime=10 --group_reporting \
    --cpus_allowed=1 --output-format=json > "$POL_JSON" 2>/tmp/qlinnos_pol.err
POL_BW=$(jq -r '.jobs[0].read.bw_bytes' "$POL_JSON" 2>/dev/null)
POL_IOPS=$(jq -r '.jobs[0].read.iops' "$POL_JSON" 2>/dev/null)
echo "policy(scx_io+q_linnos): bw_bytes=$POL_BW iops=$POL_IOPS"

# ---- 3. graceful stop -> stats dump ----
S pkill -INT -f loader_q_linnos
for i in $(seq 1 20); do
    S pgrep -f loader_q_linnos > /dev/null || break
    sleep 1
done
if S pgrep -f loader_q_linnos > /dev/null; then
    echo "loader still alive after 20s, forcing TERM"; S pkill -TERM -f loader_q_linnos; sleep 2
fi

# ---- 4. cleanup ----
S sh -c "echo none > /sys/block/$DEV/queue/scheduler"
S rm -f /sys/fs/bpf/ioext_policy_meta /sys/fs/bpf/ioext_active_cgroups
S pkill -TERM -f loader_q_linnos 2>/dev/null
sleep 1
echo "post-clean: scheduler=$(cat /sys/block/$DEV/queue/scheduler) pins=$(S ls /sys/fs/bpf/ | tr '\n' ' ')"

# ---- 5. evidence ----
echo "=== verifier/libbpf error lines in loader log (expect none) ==="
grep -E "libbpf|failed|verif|-EINVAL|not allowed" "$LOG" | head -10
echo "=== loader log full ==="
cat "$LOG"
echo "=== summary ==="
echo "BASE_BW_BYTES=$BASE_BW BASE_IOPS=$BASE_IOPS"
echo "POL_BW_BYTES=$POL_BW POL_IOPS=$POL_IOPS"
echo "=== q_linnos smoke end ==="
