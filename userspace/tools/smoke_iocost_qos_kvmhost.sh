#!/bin/bash
# arb_iocost qos-vrate acceptance smoke (kvmhost sdb, homo-v2 ctrl coloc shape)
# One arm per invocation; the WHOLE arm (state changes + fio + dumps) runs
# under /tmp/homo_smoke.lock (the caller wraps this in flock).  Single fio
# <= 14s (runtime 12 + LC ramp 2).  Scheduler is always restored to none.
# Arms:
#   none     : scheduler=none, no knobs (baseline)
#   native   : scheduler=none + root io.cost model+qos (campaign vertex) +
#              io.weight 100/200 in the tenant cgroups (n_io_cost config)
#   qos      : scx_io + loader_arb_iocost --qos-rlat-ns 2000000 +
#              q_fifo -p/-W per tenant (x_cost_arb + qos vrate feedback)
#   bytefair : same as qos but --qos-rlat-ns 0 (vrate pinned 1.0 == the OLD
#              static byte-fair arb_iocost, bit-identical semantics)
#   qostight : rlat 100000 (100us, below the achievable LC floor) — the
#              provocation arm: forces the full vrate->margin path in ANY
#              device mode (mechanism verification)
#   qosmed   : rlat 400000 (400us) — equilibrium arm: byte-fair LC p50
#              exceeds it but the reachable floor is below, so the FSM
#              converges to an interior vrate instead of pinning at the
#              floor (target regulation demonstration)
# Usage: smoke_iocost_qos_kvmhost.sh <arm>
set -u
DEV=sdb
ARM="${1:-none}"
TS=$(date +%Y%m%d_%H%M%S)
OUT="/dev/shm/iocost_qos_smoke/${ARM}_${TS}"
RUNTIME=12
LC_RAMPR=2
S() { echo 'REDACTED' | sudo -S "$@" >/dev/null 2>&1; }
SR() { echo 'REDACTED' | sudo -S "$@"; }
RUNC="/sys/fs/cgroup/homo_smoke_lc"
RUNB="/sys/fs/cgroup/homo_smoke_be"
mkdir -p "$OUT"

MM=$(printf '%d:%d' "0x$(stat -c %t /dev/$DEV)" "0x$(stat -c %T /dev/$DEV)")
echo "=== arb_iocost qos smoke arm=$ARM dev=$DEV mm=$MM ts=$TS ==="

pre_clean() {
    SR pkill -TERM -f 'loader_arb_' 2>/dev/null
    SR pkill -TERM -f 'loader_q_fifo' 2>/dev/null
    sleep 1
    S rm -f /sys/fs/bpf/ioext_policy_meta /sys/fs/bpf/ioext_active_cgroups
    S sh -c "echo none > /sys/block/$DEV/queue/scheduler"
    S sh -c "for cg in $RUNC $RUNB; do [ -d \$cg ] && (echo default > \$cg/io.weight 2>/dev/null; rmdir \$cg 2>/dev/null); done"
    sleep 1
}

make_cgroups() {
    S sh -c "echo +io > /sys/fs/cgroup/cgroup.subtree_control"
    S mkdir -p "$RUNC" "$RUNB"
}

post_clean() {
    SR pkill -TERM -f 'loader_arb_' 2>/dev/null
    SR pkill -TERM -f 'loader_q_fifo' 2>/dev/null
    sleep 1
    S rm -f /sys/fs/bpf/ioext_policy_meta /sys/fs/bpf/ioext_active_cgroups
    S sh -c "echo none > /sys/block/$DEV/queue/scheduler"
    S sh -c "[ -f /sys/fs/cgroup/io.cost.qos ] && printf '%s enable=0 ctrl=user\n' '$MM' > /sys/fs/cgroup/io.cost.qos"
    S sh -c "for cg in $RUNC $RUNB; do [ -d \$cg ] && (echo default > \$cg/io.weight 2>/dev/null; rmdir \$cg 2>/dev/null); done"
    sleep 1
    echo "post-clean: scheduler=$(cat /sys/block/$DEV/queue/scheduler) pins=[$(SR ls /sys/fs/bpf/ | tr '\n' ' ')] loaders=[$(SR pgrep -af 'loader_' | tr '\n' ';')]"
}

run_fios() {
    S sh -c "blkdiscard /dev/$DEV" || echo "WARN: blkdiscard failed (continue)"
    sleep 1
    SR bash -c "( echo \$BASHPID > $RUNC/cgroup.procs; exec fio --name=lc \
        --filename=/dev/$DEV --ioengine=libaio --direct=1 --bs=4k \
        --rw=randread --iodepth=32 --numjobs=1 --time_based=1 \
        --runtime=$RUNTIME --ramp_time=$LC_RAMPR --group_reporting \
        --percentile_list=50:90:95:99:99.9 --output-format=json ) \
        > $OUT/lc.json 2> $OUT/lc.err" &
    SR bash -c "( echo \$BASHPID > $RUNB/cgroup.procs; exec fio --name=be \
        --filename=/dev/$DEV --ioengine=libaio --direct=1 --bs=64k \
        --rw=write --iodepth=16 --numjobs=1 --time_based=1 \
        --runtime=$RUNTIME --group_reporting \
        --percentile_list=50:90:95:99:99.9 --output-format=json ) \
        > $OUT/be.json 2> $OUT/be.err" &
    wait
}

report() {
    python3 - "$OUT" <<'PYEOF'
import json, sys
out = sys.argv[1]
def grab(p, kind):
    try:
        j = json.load(open(p))
    except Exception as e:
        print(f"  {p}: parse fail {e}")
        return None
    job = j["jobs"][0]
    d = job[kind]
    lat = {}
    for sec in ("clat_ns", "lat_ns"):
        lat.update(d.get(sec, {}).get("percentile", {}) or {})
    def pct(target):
        for kk, vv in lat.items():
            try:
                if abs(float(kk) - target) < 1e-6:
                    return vv / 1000.0
            except ValueError:
                continue
        return None
    return {"iops": d.get("iops", 0), "bw": d.get("bw_bytes", 0),
            "p50": pct(50.0), "p99": pct(99.0), "p999": pct(99.9)}
lc = grab(f"{out}/lc.json", "read")
be = grab(f"{out}/be.json", "write")
if lc and be:
    share = be["bw"] / (lc["bw"] + be["bw"]) if (lc["bw"] + be["bw"]) else float("nan")
    print(f"  LC: iops={lc['iops']:.0f} bw={lc['bw']/1048576:.1f}MiB/s "
          f"p50={lc['p50']}us p99={lc['p99']}us p99.9={lc['p999']}us")
    print(f"  BE: iops={be['iops']:.0f} bw={be['bw']/1048576:.1f}MiB/s "
          f"p50={be['p50']}us p99={be['p99']}us")
    print(f"  be_share={share:.3f} (bw ratio)")
PYEOF
}

case "$ARM" in
none)
    pre_clean
    make_cgroups
    echo "--- arm none: scheduler=$(cat /sys/block/$DEV/queue/scheduler) ---"
    run_fios
    report
    post_clean
    ;;
native)
    pre_clean
    make_cgroups
    S sh -c "echo 100 > $RUNC/io.weight; echo 200 > $RUNB/io.weight"
    S sh -c "printf '%s ctrl=user model=linear rbps=335544320 rseqiops=82000 rrandiops=82000 wbps=335544320 wseqiops=82000 wrandiops=82000\n' '$MM' > /sys/fs/cgroup/io.cost.model"
    S sh -c "printf '%s enable=1 ctrl=user rpct=99.0 rlat=2000 wpct=95.0 wlat=1000000 min=50.0 max=150.0\n' '$MM' > /sys/fs/cgroup/io.cost.qos"
    echo "--- arm native: io.cost model+qos at root, weights 100/200, scheduler=$(cat /sys/block/$DEV/queue/scheduler) ---"
    SR sh -c "grep '$MM' /sys/fs/cgroup/io.cost.qos || echo 'WARN: io.cost.qos write invisible'"
    run_fios
    report
    post_clean
    ;;
qos|bytefair|qostight|qosmed)
    RLATARG=2000000
    [ "$ARM" = bytefair ] && RLATARG=0
    [ "$ARM" = qostight ] && RLATARG=100000
    [ "$ARM" = qosmed ] && RLATARG=400000
    pre_clean
    make_cgroups
    S sh -c "echo scx_io > /sys/block/$DEV/queue/scheduler"
    S sh -c "echo 1023 > /sys/block/$DEV/queue/nr_requests"
    sleep 1
    SR sh -c "/opt/io-ext/userspace/loader_arb_iocost --qos-rlat-ns $RLATARG \
        > $OUT/arbiter.log 2>&1 & echo started; sleep 2"
    if ! SR pgrep -f '^/opt/io-ext/userspace/loader_arb_iocost' > /dev/null; then
        echo "ARBITER_DIED_EARLY (verifier/load failure?)"
        SR sh -c "grep -E 'failed|verif|-E2BIG|-EINVAL|too large' $OUT/arbiter.log | head"
        post_clean
        exit 1
    fi
    # HARD GATE: the arbiter's maps must exist and hold the loader-written
    # cfg BEFORE any fio runs (the -E2BIG load failure of 20261107 smoke
    # hid behind a pgrep false positive matching the wrapper shell).
    # HARD GATE 2: libbpf creates the maps BEFORE program verification,
    # so a bare map dump is a FALSE PASS on -E2BIG — require the
    # loader-WRITTEN rlat value (present only after load AND attach).
    if ! SR bpftool map dump name iocost_cfg 2>/dev/null | grep -q "qos_rpct"; then
        echo "ARBITER_LOAD_INCOMPLETE (no loader-written cfg: -E2BIG?)"
        SR sh -c "grep -E 'failed|verif|-E2BIG|-EINVAL|too large|Processed' $OUT/arbiter.log | head"
        post_clean
        exit 1
    fi
    echo "arbiter alive (verifier accepted, maps live); pins=[$(SR ls /sys/fs/bpf/ | tr '\n' ' ')]"
    SR sh -c "/opt/io-ext/userspace/loader_q_fifo -c $RUNC -p 2 -W 100 > $OUT/q_lc.log 2>&1 & echo started; sleep 1"
    SR sh -c "/opt/io-ext/userspace/loader_q_fifo -c $RUNB -p 5 -W 200 > $OUT/q_be.log 2>&1 & echo started; sleep 1"
    if [ "$(SR pgrep -c -f loader_q_fifo)" -lt 2 ]; then
        echo "Q_POLICY_DIED_EARLY"
        SR sh -c "cat $OUT/q_lc.log $OUT/q_be.log"
        post_clean
        exit 1
    fi
    echo "--- arm $ARM: scx_io + arb_iocost qos-rlat=$RLATARG + q_fifo -p/-W ---"
    SR sh -c "grep -E 'qos:|vrate clamp' $OUT/arbiter.log || true"
    run_fios
    report
    echo "--- map dumps (vrate state machine evidence) ---"
    for m in iocost_qos iocost_cfg cgroup_drr tenant_state; do
        echo "[$m]"
        SR bpftool map dump name "$m" 2>&1 | head -30
    done > "$OUT/maps.txt"
    head -80 "$OUT/maps.txt"
    post_clean
    ;;
*)
    echo "unknown arm: $ARM"; exit 1 ;;
esac
echo "=== arm $ARM done; artifacts in $OUT ==="
