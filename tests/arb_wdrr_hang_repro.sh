#!/bin/bash
# Reproduce rig_iox D2-like multi-cgroup hang with measurable BPF state.
# Run inside virtme-ng (root):
#   TARGET_DEVICE=vda tests/arb_wdrr_hang_repro.sh
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOADER_DIR="$BASE_DIR/userspace/build"
BT="$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
DEVICE="${TARGET_DEVICE:-vda}"
CG_ROOT="/sys/fs/cgroup"
RUNTIME="${RUNTIME_SEC:-20}"
QD="${FIO_QD:-256}"
NCG="${NCG:-8}"
RESULTS_DIR="${RESULTS_DIR:-$BASE_DIR/results/arb_wdrr_hang_repro_$(date +%Y%m%d_%H%M%S)}"

mkdir -p "$RESULTS_DIR"
REPORT="$RESULTS_DIR/report.txt"
: > "$REPORT"

log() { printf '[hang_repro] %s\n' "$*" | tee -a "$REPORT" >&2; }

cleanup_loaders() {
    pkill -9 -f 'loader_arb_' 2>/dev/null || true
    pkill -9 -f 'loader_q_fifo' 2>/dev/null || true
    rm -rf /sys/fs/bpf/ioext_* 2>/dev/null || true
}

set_scx_io() {
    echo scx_io > "/sys/block/$DEVICE/queue/scheduler"
    echo 1023 > "/sys/block/$DEVICE/queue/nr_requests" 2>/dev/null || true
    sleep 1
}

dump_tenant_state() {
    local out="$1"
    "$BT" -j map dump name tenant_state > "$out" 2>/dev/null || echo '[]' > "$out"
}

dump_policy_meta() {
    local out="$1"
    "$BT" -j map dump pinned /sys/fs/bpf/ioext_policy_meta > "$out" 2>/dev/null \
        || "$BT" -j map dump name policy_meta > "$out" 2>/dev/null \
        || echo '[]' > "$out"
}

summarize_ts() {
    local f="$1"
    python3 - "$f" <<'PY'
import json, struct, sys
FMT = "<qqqqqqqqqqqqqIIQIII4xqqqq"
FIELDS = "deficit pending inflight completed_bytes completed_cost dispatches requeues requeued_bytes serve_selects refill_arms wrap_selects skip_pending_zero refilled_round boost slo_state window_start_ns window_total window_over_target good_windows last_latency_ns ewma_latency_ns last_charge_ns last_active_ns".split()
path = sys.argv[1]
try:
    entries = json.load(open(path))
except Exception:
    print("tenant_state: unreadable"); sys.exit(0)
if not entries:
    print("tenant_state: empty"); sys.exit(0)
agg = {}
for e in entries:
    k = e.get("key")
    if isinstance(k, dict):
        cgid = k.get("cgroup_id", 0)
        hctx = k.get("hctx_idx", 0)
    elif isinstance(k, list):
        cgid = sum(int(b, 0) << (8*i) for i, b in enumerate(k[:8]))
        hctx = int(k[8], 0) if len(k) > 8 else 0
    else:
        continue
    v = e.get("value", {})
    if isinstance(v, dict):
        vals = {f: int(v.get(f, 0) or 0) for f in FIELDS}
    else:
        raw = bytes(int(x, 0) for x in v)
        vals = dict(zip(FIELDS, struct.unpack(FMT, raw[:struct.calcsize(FMT)])))
    key = (cgid, hctx)
    agg[key] = vals
print(f"tenant_state entries={len(agg)}")
tot = {f: 0 for f in ("pending", "inflight", "dispatches", "skip_pending_zero", "requeues")}
for (cgid, hctx), vals in sorted(agg.items()):
    line = f"  cg={cgid} hctx={hctx}: pending={vals['pending']} inflight={vals['inflight']} deficit={vals['deficit']} disp={vals['dispatches']} skip_p0={vals['skip_pending_zero']} req={vals['requeues']}"
    print(line)
    for f in tot:
        tot[f] += vals[f]
print("  TOTAL:", " ".join(f"{k}={tot[k]}" for k in tot))
PY
}

summarize_meta() {
    local f="$1"
    python3 - "$f" <<'PY'
import json, sys
path = sys.argv[1]
try:
    entries = json.load(open(path))
except Exception:
    print("policy_meta: unreadable"); sys.exit(0)
print(f"policy_meta entries={len(entries)}")
for e in entries:
    k = e.get("key")
    v = e.get("value", {})
    if isinstance(k, dict):
        cgid = k.get("cgroup_id", k)
    elif isinstance(k, list):
        cgid = sum(int(b, 0) << (8*i) for i, b in enumerate(k[:8]))
    else:
        cgid = k
    if isinstance(v, dict):
        qd = v.get("queue_depth", 0)
        aw = v.get("arb_weight", 0)
    else:
        qd = aw = "?"
    print(f"  cg={cgid}: queue_depth={qd} arb_weight={aw}")
PY
}

fio_valid() {
    local f="$1"
    python3 - "$f" <<'PY'
import json, re, sys
raw = open(sys.argv[1]).read()
if not raw.strip().startswith("{"):
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        print("invalid"); sys.exit(0)
    raw = m.group(0)
d = json.loads(raw)
jobs = d.get("jobs", [])
ios = sum(j.get("read", {}).get("total_ios", 0) for j in jobs)
rt = max((j.get("read", {}).get("runtime", 0) for j in jobs), default=0)
print(f"valid jobs={len(jobs)} total_ios={ios} max_runtime_ms={rt}")
PY
}

count_dstate_fio() {
    ps aux | awk '/fio/ && $8 ~ /D/ {c++} END{print c+0}'
}

run_case() {
    local tag="$1"
    local arb_loader="$2"
    local ncgs="$3"
    local tag_dir="$RESULTS_DIR/$tag"
    mkdir -p "$tag_dir"

    log "=== CASE $tag loader=$arb_loader ncgs=$ncgs runtime=${RUNTIME}s qd=$QD ==="
    cleanup_loaders
    for i in $(seq 1 16); do rmdir "$CG_ROOT/${tag}_cg$i" 2>/dev/null || true; done
    set_scx_io

    "$LOADER_DIR/$arb_loader" > "$tag_dir/arb.log" 2>&1 &
    local arb_pid=$!
    sleep 2
    if ! kill -0 "$arb_pid" 2>/dev/null; then
        log "FAIL $tag: arb loader died"; cat "$tag_dir/arb.log" | tee -a "$REPORT"; return 1
    fi

    local cgids=()
    local i
    for i in $(seq 1 "$ncgs"); do
        local cg="$CG_ROOT/${tag}_cg$i"
        mkdir -p "$cg"
        cgids+=("$(stat -c %i "$cg")")
        "$LOADER_DIR/loader_q_fifo" -c "$cg" -p $((8 - i)) > "$tag_dir/qfifo_$i.log" 2>&1 &
    done
    sleep 3

    local job="$tag_dir/test.job"
    local out="$tag_dir/fio.json"
    cat > "$job" <<EOF
[global]
ioengine=libaio
direct=1
bs=4k
rw=randread
iodepth=$QD
runtime=$RUNTIME
time_based=1
group_reporting=1
filename=/dev/$DEVICE
EOF
    for i in $(seq 1 "$ncgs"); do
        cat >> "$job" <<EOF

[${tag}_j$i]
cgroup=${tag}_cg$i
cgroup_nodelete=1
numjobs=1
EOF
    done

    dump_tenant_state "$tag_dir/ts_before.json"
    dump_policy_meta "$tag_dir/meta_before.json"

    local t0 t1 rc hang=0
    t0=$(date +%s)
    local sampler_pid=""
    if [ "$arb_loader" = "loader_arb_wdrr" ] || [ "$arb_loader" = "loader_arb_slo" ] || [ "$arb_loader" = "loader_arb_cost" ]; then
        (
            while true; do
                dump_tenant_state "$tag_dir/ts_sample_$(date +%s).json"
                sleep 5
            done
        ) &
        sampler_pid=$!
    fi

    set +e
    timeout $((RUNTIME + 35)) fio --output-format=json --output="$out" "$job" > "$tag_dir/fio.stderr" 2>&1
    rc=$?
    set -e
    t1=$(date +%s)
    [ -n "$sampler_pid" ] && kill "$sampler_pid" 2>/dev/null || true

    local wall=$((t1 - t0))
    local dstate
    dstate=$(count_dstate_fio)
    dump_tenant_state "$tag_dir/ts_after.json"
    dump_policy_meta "$tag_dir/meta_after.json"
    dmesg > "$tag_dir/dmesg.txt" 2>/dev/null || true

    log "  fio_rc=$rc wall_sec=$wall dstate_fio=$dstate"
    if [ -f "$out" ]; then
        fio_valid "$out" | tee -a "$REPORT"
    else
        log "  fio output missing"
    fi
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ] || [ "$dstate" -gt 0 ]; then
        hang=1
        log "  HANG detected (timeout or D-state)"
        tail -5 "$tag_dir/fio.stderr" | tee -a "$REPORT"
    fi
    log "  ts_before:"; summarize_ts "$tag_dir/ts_before.json" | tee -a "$REPORT"
    log "  ts_after:"; summarize_ts "$tag_dir/ts_after.json" | tee -a "$REPORT"
    log "  meta_after:"; summarize_meta "$tag_dir/meta_after.json" | tee -a "$REPORT"

    # pick latest sample during run if hung
    local latest
    latest=$(ls -1 "$tag_dir"/ts_sample_*.json 2>/dev/null | tail -1 || true)
    if [ -n "$latest" ] && [ "$hang" -eq 1 ]; then
        log "  ts_during (latest sample):"; summarize_ts "$latest" | tee -a "$REPORT"
    fi

    pkill -9 -f fio 2>/dev/null || true
    cleanup_loaders
    for i in $(seq 1 "$ncgs"); do rmdir "$CG_ROOT/${tag}_cg$i" 2>/dev/null || true; done

    echo "$tag rc=$rc wall=$wall dstate=$dstate hang=$hang" >> "$RESULTS_DIR/summary.csv"
    log ""
    return 0
}

command -v fio >/dev/null
mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf
echo '+io' > "$CG_ROOT/cgroup.subtree_control" 2>/dev/null || true
[ -x "$LOADER_DIR/loader_arb_wdrr" ] || make -C "$BASE_DIR/userspace" -j"$(nproc)"

log "DEVICE=/dev/$DEVICE RESULTS=$RESULTS_DIR runtime=$RUNTIME qd=$QD"
echo "case,rc,wall,dstate,hang" > "$RESULTS_DIR/summary.csv"

run_case wdrr_1cg loader_arb_wdrr 1
run_case sp_1cg loader_arb_sp 1
run_case sp_8cg loader_arb_sp 8
run_case wdrr_8cg loader_arb_wdrr 8
run_case slo_8cg loader_arb_slo 8

log "=== DONE ==="
log "Full report: $REPORT"
log "Summary: $RESULTS_DIR/summary.csv"
cat "$RESULTS_DIR/summary.csv" | tee -a "$REPORT"
