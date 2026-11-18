#!/bin/bash
# Pin-all-cgroups control: if wdrr 8cg passes with same CPU, root cause is multi-hctx.
set -euo pipefail
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOADER_DIR="$BASE_DIR/userspace/build"
BT="$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
DEVICE="${TARGET_DEVICE:-vda}"
CG_ROOT="/sys/fs/cgroup"
RUNTIME="${RUNTIME_SEC:-15}"
QD="${FIO_QD:-256}"
PIN="${PIN_CPU:-1}"
TAG="${TAG:-wdrr_8cg_pin}"
OUT="${RESULTS_DIR:-$BASE_DIR/results}/${TAG}_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"

cleanup() {
  pkill -9 -f loader_arb_ 2>/dev/null || true
  pkill -9 -f loader_q_fifo 2>/dev/null || true
  pkill -9 -f fio 2>/dev/null || true
  rm -rf /sys/fs/bpf/ioext_* 2>/dev/null || true
}

echo "=== $TAG pin_cpu=$PIN runtime=$RUNTIME ==="
cleanup
echo scx_io > "/sys/block/$DEVICE/queue/scheduler"
echo 1023 > "/sys/block/$DEVICE/queue/nr_requests" 2>/dev/null || true
mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf
echo '+io' > "$CG_ROOT/cgroup.subtree_control" 2>/dev/null || true

"$LOADER_DIR/loader_arb_wdrr" > "$OUT/arb.log" 2>&1 &
sleep 2
for i in $(seq 1 8); do
  cg="$CG_ROOT/${TAG}_cg$i"
  mkdir -p "$cg"
  "$LOADER_DIR/loader_q_fifo" -c "$cg" -p $((8-i)) > "$OUT/q$i.log" 2>&1 &
done
sleep 2

t0=$(date +%s)
pids=()
for i in $(seq 1 8); do
  (
    echo "$BASHPID" > "$CG_ROOT/${TAG}_cg$i/cgroup.procs"
    exec fio --name="${TAG}_j$i" --filename="/dev/$DEVICE" --rw=randread --bs=4k \
      --ioengine=libaio --direct=1 --iodepth=$QD --runtime=$RUNTIME --time_based=1 \
      --cpus_allowed=$PIN --output="$OUT/fio_$i.json" --output-format=json
  ) &
  pids+=($!)
done

rc=0
for pid in "${pids[@]}"; do
  wait "$pid" || rc=1
done
t1=$(date +%s)
echo "wall_sec=$((t1-t0)) wait_rc=$rc"

"$BT" -j map dump name tenant_state > "$OUT/ts_after.json" 2>/dev/null || echo '[]' > "$OUT/ts_after.json"
python3 - "$OUT/ts_after.json" <<'PY'
import json, struct, sys
FMT = "<qqqqqqqqqqqqqIIQIII4xqqqq"
FIELDS = "deficit pending inflight completed_bytes completed_cost dispatches requeues requeued_bytes serve_selects refill_arms wrap_selects skip_pending_zero refilled_round boost slo_state window_start_ns window_total window_over_target good_windows last_latency_ns ewma_latency_ns last_charge_ns last_active_ns".split()
entries = json.load(open(sys.argv[1]))
hctx=set(); tot={'pending':0,'inflight':0,'skip_pending_zero':0,'dispatches':0}
for e in entries:
    k=e['key']
    h=int(k.get('hctx_idx',0)) if isinstance(k,dict) else int(k[8],0)
    hctx.add(h)
    v=e['value']
    if isinstance(v,dict): vals={f:int(v.get(f,0) or 0) for f in FIELDS}
    else:
        raw=bytes(int(x,0) for x in v)
        vals=dict(zip(FIELDS, struct.unpack(FMT, raw[:struct.calcsize(FMT)])))
    for f in tot: tot[f]+=vals[f]
print(f"tenant_entries={len(entries)} distinct_hctx={sorted(hctx)}")
print(f"TOTAL pending={tot['pending']} inflight={tot['inflight']} disp={tot['dispatches']} skip_p0={tot['skip_pending_zero']}")
PY

for i in $(seq 1 8); do
  python3 -c "import json;d=json.load(open('$OUT/fio_$i.json'));j=d['jobs'][0];print('job$i ios',j['read']['total_ios'])" 2>/dev/null || echo "job$i FAIL"
done
cleanup
for i in $(seq 1 8); do rmdir "$CG_ROOT/${TAG}_cg$i" 2>/dev/null || true; done
echo "RESULT dir=$OUT"
