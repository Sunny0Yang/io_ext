#!/bin/bash
# ioext_repro_lib.sh — per-algorithm frozen experiment definitions for the
# io_ext reproduction campaign (plan §4: 8 algorithms, one workload each,
# arms none/native/iox; LinnOS and D2FQ have no native arm in the VM).
#
# Sourced by run_experiment.sh.  Execution lives INSIDE the guest VM.
#
# Frozen numbers (plan-fixed unless noted "主会话补定"):
#   io.max cap = 32 MiB/s = 33554432 B/s                      (plan)
#   io.weight ratio = 100:400                                 (plan)
#   io.cost weights = 100:200, qos rlat=2ms rpct=99 min=50 max=150
#                   (主会话补定; model coefficients = guest virtio
#                    calibration frozen in /opt/ioext_repro/iocost_model.env)
#   io.latency target = 2 ms                                  (plan)
#   kyber targets = read 2ms / write 10ms                     (主会话补定,
#                 homo_v2 frozen CLI values)
#   q_linnos table = LC 0/0/0, BE 0/40/60, busy_ref 24, hold 500us
#                   (主会话补定; band coverage checked in the diagnostic run)
#   LC fio: 4k randread QD32; BE fio: 128k seqwrite QD16      (主会话补定)
#   step protocol: 3 x 30s, LC QD 32->64->32                  (主会话补定)
#   d2fq: both tenants 4k randread QD32 (same-type, share==weight check)
#   linnos: LC 4k randread QD8 steady; BE 4k randwrite QD 8/16/32/64 x30s

set -u

RESULTS_ROOT="${RESULTS_ROOT:-/var/lib/ioext_repro/results}"
LOADERS_DIR="${LOADERS_DIR:-/opt/ioext_repro/loaders_stats1}"
DATA_LC="${DATA_LC:-/data/lc.bin}"
DATA_BE="${DATA_BE:-/data/be.bin}"
LC_CG="${LC_CG:-/sys/fs/cgroup/lc}"
BE_CG="${BE_CG:-/sys/fs/cgroup/be}"
T100_CG="${T100_CG:-/sys/fs/cgroup/t100}"
T400_CG="${T400_CG:-/sys/fs/cgroup/t400}"
BLKDEV="${BLKDEV:-vdb}"
DEVMJMN="${DEVMJMN:-$(cat "/sys/block/${BLKDEV}/dev" 2>/dev/null || echo 253:16)}"
FIO_SIZE="${FIO_SIZE:-30G}"

IO_MAX_WBPS=33554432          # 32 MiB/s (plan)
IOLAT_TARGET_NS=12000000      # 12 ms (re-anchored for LC=write: p1 p99 ~7ms, p2(QD64) ~14.5ms -> 12ms binds only on the step; diag_iolat_none_w)
KYBER_READ_TARGET_NS=80000    # 80 us  (re-anchored, same rationale)
KYBER_WRITE_TARGET_NS=1000000
IOCOST_RLAT_NS=12000000      # IOX loader parses ns (12 ms) — correct for --qos-rlat-ns
# Work-package io.cost fix (2026-11-15): blk-iocost's io.cost.qos latency
# params are parsed in MICROSECONDS (blk-iocost.c:2872 params.qos *
# NSEC_PER_USEC vs ns) — the original campaign wrote the NS number
# (rlat=12000000 → 12 s target, wlat=1000000 → 1 s target), so the native
# controller's latency-violation signal could never fire and vrate pinned at
# the max clamp 150.00 (the "native 恒 150" finding).  Corrected targets:
IOCOST_RLAT_US=12000         # 12 ms read target
IOCOST_WLAT_US=1000          # 1 ms  write target
Q_LINNOS_BUSY_REF=24
Q_LINNOS_MAX_HOLD_US=500

# io.cost.model line (MAJ:MIN-prefixed, write-ready).  Frozen from the guest
# vdb calibration (iocost_coef_gen_bm.py file mode, 2026-11-14) in
# /opt/ioext_repro/iocost_model.env; the hardcoded fallback is that same
# measurement for resilience.
IOCOST_MODEL="${IOCOST_MODEL:-$(grep -m1 "^253:16" /opt/ioext_repro/iocost_model.env 2>/dev/null || echo "253:16 model=linear rbps=1282428580 rseqiops=317715 rrandiops=286557 wbps=640363068 wseqiops=197100 wrandiops=64121")}"
IOCOST_QOS="${IOCOST_QOS:-${DEVMJMN} enable=1 ctrl=user rpct=99 rlat=${IOCOST_RLAT_US} \
wpct=95 wlat=${IOCOST_WLAT_US} min=50 max=150}"

REPRO_LOG() { echo "[repro $(date +%s.%N)] $*" >> "${RUN_DIR:-/tmp}/runner.log" 2>/dev/null || true; }

sched_file() { echo "/sys/block/${BLKDEV}/queue/scheduler"; }

# "none mq-deadline [kyber] bfq" -> the active entry is the bracketed one
cur_scheduler() {
    local tok
    for tok in $(cat "$(sched_file)" 2>/dev/null); do
        case "$tok" in
        \[*\]) echo "${tok:1:${#tok}-2}"; return 0 ;;
        esac
    done
    return 1
}

set_scheduler() { echo "$1" > "$(sched_file)"; }

cgroup_write() { echo "$3" > "$1/$2"; }

ensure_cgroups() {
    local cg
    for cg in "$@"; do mkdir -p "$cg"; done
}

start_loader() {  # start_loader <tag> <cmd...>
    local tag="$1"; shift
    ( "$@" ) >> "${RUN_DIR}/loader_${tag}.stdout" 2>> "${RUN_DIR}/loader_${tag}.stderr" &
    echo $! >> "${RUN_DIR}/loader_pids.txt"
}

stop_loaders() {
    local pid alive
    if [ -f "${RUN_DIR}/loader_pids.txt" ]; then
        while read -r pid; do kill -INT "$pid" 2>/dev/null || true; done < "${RUN_DIR}/loader_pids.txt"
        for _ in $(seq 1 30); do
            alive=0
            while read -r pid; do kill -0 "$pid" 2>/dev/null && alive=$((alive+1)); done < "${RUN_DIR}/loader_pids.txt"
            [ "$alive" = 0 ] && break
            sleep 0.5
        done
    fi
    rm -f "${RUN_DIR}/loader_pids.txt"
    sleep 1   # let the quiesce dumps land in the logs
}

snapshot_state() {  # snapshot_state <tag>
    local tag="$1"
    {
        echo "== $tag $(date +%s.%N)"
        echo "scheduler: $(cat "$(sched_file)")"
        # U5 (VALIDITY_REPORT): native activation readbacks that the original
        # campaign never captured at runtime
        for t in read_lat_nsec write_lat_nsec; do
            [ -f "/sys/block/${BLKDEV}/queue/iosched/$t" ] && \
                echo "iosched/$t: $(cat "/sys/block/${BLKDEV}/queue/iosched/$t")"
        done
        echo "root io.stat vrate: $(grep -o 'cost.vrate=[0-9.]*' /sys/fs/cgroup/io.stat 2>/dev/null | head -1 || echo none)"
        for cg in "$LC_CG" "$BE_CG" "$T100_CG" "$T400_CG"; do
            [ -d "$cg" ] || continue
            for f in io.max io.weight io.bfq.weight io.latency; do
                [ -f "$cg/$f" ] && echo "$(basename "$cg")/$f: $(cat "$cg/$f")"
            done
        done
        echo "root io.cost.model: $(grep "^${DEVMJMN}" /sys/fs/cgroup/io.cost.model 2>/dev/null || echo none)"
        echo "root io.cost.qos:  $(grep "^${DEVMJMN}" /sys/fs/cgroup/io.cost.qos 2>/dev/null || echo none)"
        echo "loaders: $(pgrep -a loader_ 2>/dev/null | tr '\n' ';' || echo none)"
    } >> "${RUN_DIR}/state_snapshot.log"
}

# ---------------------------------------------------------------------------
# fio jobs
# ---------------------------------------------------------------------------
# mk_job_file <path> <name> <rw> <bs> <iodepth> <runtime> <file> <logprefix> <logmode> [extras]
#   logmode "full": per-IO latency log + 1 s bw/iops logs  (kyber/io_latency LC:
#                   the figures need the 1 s-bucket p99 series from per-IO data)
#   logmode "agg":  1 s bw/iops logs only; percentiles come from the fio JSON
mk_job_file() {
    local path="$1" name="$2" rw="$3" bs="$4" iodepth="$5" runtime="$6" f="$7" logp="$8" mode="${9:-agg}" extras="${10:-}"
    mkdir -p "$(dirname "$path")"
    local latline="log_avg_msec=1000"
    if [ "$mode" = "full" ]; then
        latline="write_lat_log=${logp}"
    fi
    cat > "$path" <<JOBEOF
[global]
ioengine=libaio
direct=1
group_reporting
percentile_list=50:90:95:99:99.9
clat_percentiles=1
time_based=1
runtime=${runtime}
size=${FIO_SIZE}
norandommap=1
randrepeat=0
log_avg_msec=1000

[${name}]
rw=${rw}
bs=${bs}
iodepth=${iodepth}
filename=${f}
${latline}
write_bw_log=${logp}
write_iops_log=${logp}
${extras}
JOBEOF
}

# Launch one fio job with the fio process moved into <cg> before exec.
run_fio_in_cg() {  # run_fio_in_cg <tag> <cg|-> <jobfile>
    local tag="$1" cg="$2" jobfile="$3"
    if [ "$cg" = "-" ]; then
        fio --output="${RUN_DIR}/fio_${tag}.json" --output-format=json \
            "$jobfile" > "${RUN_DIR}/fio_${tag}.stdout" 2>&1 &
    else
        bash -c 'echo $$ > "$1/cgroup.procs" 2>/dev/null; exec fio --output="$2" --output-format=json "$3"' \
            _ "$cg" "${RUN_DIR}/fio_${tag}.json" "$jobfile" \
            > "${RUN_DIR}/fio_${tag}.stdout" 2>&1 &
    fi
}

run_fio_pair() {  # run_fio_pair <tag> <lc_cg|-> <lc_job> <be_cg|-> <be_job>
    local tag="$1" lcg="$2" lcj="$3" bcg="$4" bej="$5" rc_lc rc_be
    run_fio_in_cg "${tag}_lc" "$lcg" "$lcj"; local pid_lc=$!
    run_fio_in_cg "${tag}_be" "$bcg" "$bej"; local pid_be=$!
    wait "$pid_lc"; rc_lc=$?
    wait "$pid_be"; rc_be=$?
    [ $rc_lc -eq 0 ] && [ $rc_be -eq 0 ] || { REPRO_LOG "fio pair ${tag} FAILED rc=$rc_lc/$rc_be"; return 1; }
    return 0
}

run_sentinel() {  # run_sentinel <tag>: 3 x 10 s QD1 native-path randread
    local tag="$1" i
    for i in 1 2 3; do
        fio --name="sent_${tag}_${i}" --filename="$DATA_LC" --rw=randread \
            --bs=4k --iodepth=1 --direct=1 --runtime=10 --time_based \
            --ioengine=libaio --group_reporting --output-format=json \
            --output="${RUN_DIR}/sentinel_${tag}_${i}.json" \
            >/dev/null 2>&1
    done
}

reset_data_files() {
    # Reset the written ranges: remember each file's provisioned size, truncate
    # to deallocate, re-fallocate to the SAME size (data.img holds exactly two
    # files — any size growth cannot be allocated), trim free space.
    #
    # Work-package A (2026-11-14): truncate+fallocate leaves EVERY extent
    # UNWRITTEN, and iomap's DIO read path zero-fills hole/unwritten ranges
    # without issuing any device bio (fs/iomap/direct-io.c iomap_dio_hole_iter)
    # — the original campaign's "reads" never reached the device and read-side
    # accounting/controllers looked dead (P0_AUDIT.md §4).  Experiments whose
    # workload READS the data files therefore prewrite real data here; d2fq
    # prewrites too (E12: allocation-free 4k demand is the contention design).
    #   $1 = algo name (optional; run_experiment passes $ALGO)
    local sz_lc sz_be
    sz_lc="$(stat -c %s "$DATA_LC" 2>/dev/null || echo 33285996544)"
    sz_be="$(stat -c %s "$DATA_BE" 2>/dev/null || echo 33285996544)"
    truncate -s 0 "$DATA_LC" "$DATA_BE" 2>/dev/null || true
    fallocate -l "$sz_lc" "$DATA_LC" || return 1
    fallocate -l "$sz_be" "$DATA_BE" || return 1
    fstrim -v /data >/dev/null 2>&1 || true
    if algo_needs_prewritten_data "${1:-}" || [ "${IOEXT_FORCE_PREWRITE:-0}" = "1" ]; then
        prewrite_data_files || return 1
    fi
}

# Experiments whose workload READS the data files need real bytes on the
# device; d2fq prewrites under E12 (see run_workload).  Everything else keeps
# the original unwritten-file protocol (write workloads allocate as they go —
# that allocation latency is part of their recorded behavior).
algo_needs_prewritten_data() {
    case "${1:-}" in
    mq_deadline|kyber|linnos|d2fq) return 0 ;;
    # io_weight prewrites only in its E12 contention variant (the frozen 128k
    # protocol's allocation latency is part of its recorded behavior)
    io_weight) [ "${IOW_WORKLOAD:-frozen128k}" = "e12_4k_qd64" ] && return 0 || return 1 ;;
    *) return 1 ;;
    esac
}

prewrite_data_files() {
    # Overwrite both data files with real data (O_DIRECT, both files in
    # parallel), then sync.  ~45-90 s for 2 x 31G at the observed ~0.7 GB/s.
    local f sz szmb
    for f in "$DATA_LC" "$DATA_BE"; do
        sz="$(stat -c %s "$f" 2>/dev/null || echo 33285996544)"
        szmb=$(( sz / 1048576 ))
        dd if=/dev/zero of="$f" bs=1M count="$szmb" oflag=direct status=none &
    done
    wait
    sync
    REPRO_LOG "prewrite done ($(du -B1 "$DATA_LC" "$DATA_BE" 2>/dev/null | cut -f1 | tr '\n' '/') bytes)"
}

# Fail-stop gate (work-package A): a short direct read MUST move the DEVICE
# read counter (diskstats — the same per-cpu bd_stats that root io.stat
# aggregates via blkcg_fill_root_iostats).  A phantom read (unwritten file)
# yields delta ~ noise and the run must not proceed (P0_AUDIT.md §6.2-4).
verify_read_io_authenticity() {  # <file> [seconds]
    local f="$1" secs="${2:-2}" src dev r0 r1 delta
    src="$(findmnt -n -o SOURCE --target "$f" 2>/dev/null || echo "/dev/${BLKDEV}")"
    dev="$(basename "$src")"
    r0=$(awk -v d="$dev" '$3==d{print $4}' /proc/diskstats)
    fio --name=rdchk --filename="$f" --rw=randread --bs=4k --iodepth=4 \
        --direct=1 --runtime="$secs" --time_based --ioengine=libaio \
        --size=64M --output=/dev/null >/dev/null 2>&1
    r1=$(awk -v d="$dev" '$3==d{print $4}' /proc/diskstats)
    delta=$(( r1 - r0 ))
    echo "read-auth: dev=$dev reads $r0 -> $r1 (delta=$delta, file=$f)"
    [ "$delta" -ge 500 ]
}

# ---------------------------------------------------------------------------
# per-algorithm arm activation + workload execution
# ---------------------------------------------------------------------------
JOB_DIR() { echo "${RUN_DIR}/jobs"; }

# activate <algo> <arm>: set scheduler / cgroup knobs / attach loaders.
activate() {
    local algo="$1" arm="$2"
    ensure_cgroups "$LC_CG" "$BE_CG"

    case "$algo" in
    mq_deadline)
        case "$arm" in
        none)   set_scheduler none ;;
        native) set_scheduler mq-deadline
                # mq-rerun: in-run mechanism observation (R1 gap: sel_flip/
                # starved never collected).  debugfs batching/starved/queued/
                # dispatch/owned_by_driver at 1 s.
                ( while :; do
                      printf '%s ' "$(date +%s.%N)"
                      for tk in batching starved queued dispatch owned_by_driver; do
                          v=$(cat "/sys/kernel/debug/block/${BLKDEV}/$tk" 2>/dev/null | tr '\n' '/')
                          printf '%s=[%s] ' "$tk" "${v:-ERR}"
                      done
                      echo
                      sleep 1
                  done ) >> "$RUN_DIR/native_mq_debugfs.log" 2>&1 &
                echo $! >> "${RUN_DIR}/loader_pids.txt" ;;
        iox)    set_scheduler scx_io
                start_loader q_mq_deadline "$LOADERS_DIR/loader_q_mq_deadline" -c /sys/fs/cgroup ;;
        esac
        ;;
    kyber)
        case "$arm" in
        none)   set_scheduler none ;;
        native) set_scheduler kyber
                echo 80000 > "/sys/block/${BLKDEV}/queue/iosched/read_lat_nsec"
                echo 1000000 > "/sys/block/${BLKDEV}/queue/iosched/write_lat_nsec"
                # kyber-rerun: in-run native depth sampling (fixes R1 deviation
                # D2 -- companion-run observation).  debugfs sched/{read,write}_
                # tokens 'depth=' at 1 s; teardown-tail READ_ERR lines are
                # filtered by analysis (R1 fig2 convention).
                ( while :; do
                      printf '%s' "$(date +%s.%N)"
                      for tk in read_tokens write_tokens; do
                          v=$(grep -o 'depth=[0-9]*' \
                              "/sys/kernel/debug/block/${BLKDEV}/sched/$tk" 2>/dev/null \
                              | head -1 | cut -d= -f2)
                          printf ' %s' "${v:-ERR}"
                      done
                      echo
                      sleep 1
                  done ) >> "$RUN_DIR/native_kyber_depth.log" 2>&1 &
                echo $! >> "${RUN_DIR}/loader_pids.txt" ;;
        iox)    set_scheduler scx_io
                start_loader q_kyber "$LOADERS_DIR/loader_q_kyber" -c /sys/fs/cgroup \
                    --read-target-ns "$KYBER_READ_TARGET_NS" \
                    --write-target-ns "$KYBER_WRITE_TARGET_NS" \
                    --stats-interval-ms 1000 ;;
        esac
        ;;
    io_max)
        ensure_cgroups "$BE_CG"
        case "$arm" in
        none)   set_scheduler none ;;
        native) set_scheduler none
                cgroup_write "$BE_CG" io.max "${DEVMJMN} rbps=max wbps=${IO_MAX_WBPS} riops=max wiops=max" ;;
        iox)    set_scheduler scx_io
                start_loader q_iomax "$LOADERS_DIR/loader_q_iomax" -c "$BE_CG" \
                    --rbps 0 --wbps "$IO_MAX_WBPS" ;;
        esac
        ;;
    io_weight)
        # LC tenant traffic must be WRITE: read-side cgroup attribution is
        # broken in the ioext kernel, so a read-LC tenant would be invisible
        # to the arbiter ledger and the 100:400 weight would be unenforceable.
        case "$arm" in
        none)   set_scheduler none ;;
        native) set_scheduler bfq
                # BFQ's cgroup-v2 knob is io.bfq.weight (bfq-cgroup.c); plain
                # io.weight is blk-iocost's file and is a no-op for BFQ.
                cgroup_write "$LC_CG" io.bfq.weight "100"
                cgroup_write "$BE_CG" io.bfq.weight "400"
                cgroup_write "$LC_CG" io.weight "100"
                cgroup_write "$BE_CG" io.weight "400" ;;
        iox)    set_scheduler scx_io
                start_loader arb_wdrr "$LOADERS_DIR/loader_arb_wdrr"
                start_loader q_lc "$LOADERS_DIR/loader_q_fifo" -c "$LC_CG" -p 2 -W 100
                start_loader q_be "$LOADERS_DIR/loader_q_fifo" -c "$BE_CG" -p 5 -W 400 ;;
        esac
        ;;
    io_cost)
        case "$arm" in
        none)   set_scheduler none ;;
        native) set_scheduler none
                cgroup_write /sys/fs/cgroup io.cost.model "$IOCOST_MODEL"
                cgroup_write "$LC_CG" io.weight "100"
                cgroup_write "$BE_CG" io.weight "200"
                cgroup_write /sys/fs/cgroup io.cost.qos  "$IOCOST_QOS"
                # io-cost-rerun: in-run native vrate/usage sampling (R1 gap D2:
                # companion-run) + full io.stat line (blkcg_debug_stats adds
                # cost.wait/indebt/indelay when writable).
                echo 1 > /sys/module/blkcg/parameters/blkcg_debug_stats 2>/dev/null || true
                ( while :; do
                      printf '%s ' "$(date +%s.%N)"
                      grep "^${DEVMJMN}" /sys/fs/cgroup/io.stat 2>/dev/null | head -1 \
                          || printf 'io.stat=ERR'
                      echo
                      sleep 1
                  done ) >> "$RUN_DIR/native_iocost_iostat.log" 2>&1 &
                echo $! >> "${RUN_DIR}/loader_pids.txt" ;;
        iox)    set_scheduler scx_io
                start_loader arb_iocost "$LOADERS_DIR/loader_arb_iocost" \
                    --qos-rlat-ns "$IOCOST_RLAT_NS" --stats-interval-ms 1000
                start_loader q_lc "$LOADERS_DIR/loader_q_fifo" -c "$LC_CG" -p 2 -W 100
                start_loader q_be "$LOADERS_DIR/loader_q_fifo" -c "$BE_CG" -p 5 -W 200 ;;
        esac
        ;;
    io_latency)
        case "$arm" in
        none)   set_scheduler none ;;
        native) set_scheduler none
                cgroup_write "$LC_CG" io.latency "${DEVMJMN} target=12000"
                # io-latency-rerun: in-run native scale-state sampling (R1 gap:
                # max_depth never observed).  blkcg_debug_stats exposes depth=
                # on the cgroup io.stat line; 1 s.  NOTE: on 7.3.1-ioext3 the
                # parameter does not exist at runtime (built-in; needs cmdline
                # blkcg.blkcg_debug_stats=1) — the sampler then logs ERR lines
                # and the depth attribution stays open (HOMO_STATUS_MATRIX §6).
                [ -f /sys/module/blkcg/parameters/blkcg_debug_stats ] && \
                    echo 1 > /sys/module/blkcg/parameters/blkcg_debug_stats 2>/dev/null || true
                ( while :; do
                      v=$(grep "^${DEVMJMN}" "$LC_CG/io.stat" 2>/dev/null \
                          | grep -o 'depth=[0-9]*' | cut -d= -f2)
                      printf '%s depth=%s\n' "$(date +%s.%N)" "${v:-ERR}"
                      sleep 1
                  done ) >> "$RUN_DIR/native_iolat_depth.log" 2>&1 &
                echo $! >> "${RUN_DIR}/loader_pids.txt" ;;
        iox)    set_scheduler scx_io
                start_loader arb_iolatency "$LOADERS_DIR/loader_arb_iolatency" \
                    --lat-target-ns "$IOLAT_TARGET_NS" --stats-interval-ms 1000
                # LC must use a NON-bypass queue loader (q_fifo): q_none is
                # BYPASS_MODE_DIRECT, so LC requests never enter the scx_io
                # completion hook and the window FSM/boost/hold feedback is
                # starved (ledger evidence: smoke_iolat_iox completions all
                # attributed to BE).  FIFO for a single-tenant LC stream is
                # behaviorally ~passthrough and keeps attribution alive.
                start_loader q_lc "$LOADERS_DIR/loader_q_fifo" -c "$LC_CG" -p 2 -W 100
                start_loader q_be "$LOADERS_DIR/loader_q_fifo" -c "$BE_CG" -p 5 -W 200 ;;
        esac
        ;;
    d2fq)
        ensure_cgroups "$T100_CG" "$T400_CG"
        case "$arm" in
        none)   set_scheduler none ;;
        native) echo "d2fq native arm N/A inside virtio VM (ioextd2fq needs NVMe WRRU)" >&2; return 2 ;;
        iox)    set_scheduler scx_io
                start_loader arb_d2fq "$LOADERS_DIR/loader_arb_d2fq" --stats-interval-ms 1000
                start_loader q_t100 "$LOADERS_DIR/loader_q_fifo" -c "$T100_CG" -p 5 -W 100
                start_loader q_t400 "$LOADERS_DIR/loader_q_fifo" -c "$T400_CG" -p 5 -W 400 ;;
        esac
        ;;
    linnos)
        case "$arm" in
        none)   set_scheduler none ;;
        native) echo "linnos native arm N/A (no trusted reference implementation)" >&2; return 2 ;;
        iox)    set_scheduler scx_io
                start_loader q_lc "$LOADERS_DIR/loader_q_linnos" -c "$LC_CG" -p 2 -W 100 \
                    --busy-ref "$Q_LINNOS_BUSY_REF" --max-hold-us "$Q_LINNOS_MAX_HOLD_US" \
                    --ir-low 0:0 --ir-mid 0:0 --ir-high 0:0 --stats-interval-ms 1000
                start_loader q_be "$LOADERS_DIR/loader_q_linnos" -c "$BE_CG" -p 5 -W 200 \
                    --busy-ref "$Q_LINNOS_BUSY_REF" --max-hold-us "$Q_LINNOS_MAX_HOLD_US" \
                    --ir-low 0:0 --ir-mid 0:40 --ir-high 0:60 --stats-interval-ms 1000 ;;
        esac
        ;;
    *) echo "unknown algo: $algo" >&2; return 2 ;;
    esac
}

# run_workload <algo>: execute the ONE frozen workload (phase protocol included).
run_workload() {
    local algo="$1" J; J="$(JOB_DIR)"
    case "$algo" in
    mq_deadline)
        mk_job_file "$J/mq.job" mixed randrw 4k 32 60 "$DATA_LC" mq agg "rwmixread=70"
        run_fio_in_cg mq "-" "$J/mq.job" && wait $! ;;
    kyber|io_cost|io_latency)
        # LC io type: io_cost/io_latency use randwrite because read-side blkcg
        # accounting is broken in the ioext kernel (completion-fed FSMs would
        # never engage for reads; see ALGORITHM_AUDIT + probe evidence).
        case "$algo" in
        kyber) local lc_rw=randread ;;
        *)     local lc_rw=randwrite ;;
        esac
        mk_job_file "$J/p1_lc.job" lc "$lc_rw" 4k 32 30 "$DATA_LC" lat_p1_lc full
        mk_job_file "$J/p1_be.job" be write 128k 16 30 "$DATA_BE" lat_p1_be agg
        mk_job_file "$J/p2_lc.job" lc "$lc_rw" 4k 64 30 "$DATA_LC" lat_p2_lc full
        mk_job_file "$J/p2_be.job" be write 128k 16 30 "$DATA_BE" lat_p2_be agg
        mk_job_file "$J/p3_lc.job" lc "$lc_rw" 4k 32 30 "$DATA_LC" lat_p3_lc full
        mk_job_file "$J/p3_be.job" be write 128k 16 30 "$DATA_BE" lat_p3_be agg
        run_fio_pair p1 "$LC_CG" "$J/p1_lc.job" "$BE_CG" "$J/p1_be.job" || return 1
        run_fio_pair p2 "$LC_CG" "$J/p2_lc.job" "$BE_CG" "$J/p2_be.job" || return 1
        run_fio_pair p3 "$LC_CG" "$J/p3_lc.job" "$BE_CG" "$J/p3_be.job" || return 1
        ;;
    io_max)
        mk_job_file "$J/iomax.job" be randwrite 4k 32 60 "$DATA_BE" lat_iomax agg
        run_fio_in_cg iomax "$BE_CG" "$J/iomax.job" && wait $! ;;
    io_weight)
        # both tenants saturating same-type streams: weight shares only
        # differentiate under contention.  E12 variant (4k QD64 over
        # prewritten files, ~90-170k IOPS/tenant) guarantees the arbiter is
        # the contended resource -- the frozen 128k QD16 demand (~2.6-4.2k
        # IOPS/tenant) sits below the dispatch capacity on this VM
        # (P1_D2FQ_FIX.md §7; d2fq formal rep1 measured 0.5001 there).
        if [ "${IOW_WORKLOAD:-frozen128k}" = "e12_4k_qd64" ]; then
            mk_job_file "$J/w_lc.job" lc write 4k 64 90 "$DATA_LC" lat_w_lc agg
            mk_job_file "$J/w_be.job" be write 4k 64 90 "$DATA_BE" lat_w_be agg
        else
            mk_job_file "$J/w_lc.job" lc write 128k 16 90 "$DATA_LC" lat_w_lc agg
            mk_job_file "$J/w_be.job" be write 128k 16 90 "$DATA_BE" lat_w_be agg
        fi
        run_fio_pair w "$LC_CG" "$J/w_lc.job" "$BE_CG" "$J/w_be.job" || return 1 ;;
    d2fq)
        # E12 (user-approved 2026-11-14): the frozen 128k seqwrite QD16
        # workload's demand (~2.6-4.2k IOPS/tenant, allocation-limited) never
        # exceeds this VM's arbiter dispatch capacity (~8-11k IOPS/s), so the
        # two queues are never simultaneously backlogged and NO correct WFQ
        # can express weights there — shares degenerate to the demand ratio
        # 0.50 (formal rep1: 0.5001 with the fix loaded; P1_D2FQ_FIX.md §5/§7).
        # 4k writes over PREWRITTEN files arrive at ~90-170k IOPS/tenant,
        # creating the contention the weight semantics need (clean-room
        # diagnostics: 0.753/0.779/0.768 vs broken-regime 0.50).  Revert to
        # the frozen form with D2FQ_WORKLOAD=frozen128k.
        if [ "${D2FQ_WORKLOAD:-e12_4k_qd64}" = "e12_4k_qd64" ]; then
            mk_job_file "$J/d2fq_t100.job" t100 write 4k 64 90 "$DATA_LC" lat_t100 agg
            mk_job_file "$J/d2fq_t400.job" t400 write 4k 64 90 "$DATA_BE" lat_t400 agg
        else
            mk_job_file "$J/d2fq_t100.job" t100 write 128k 16 90 "$DATA_LC" lat_t100 agg
            mk_job_file "$J/d2fq_t400.job" t400 write 128k 16 90 "$DATA_BE" lat_t400 agg
        fi
        run_fio_pair d2fq "$T100_CG" "$J/d2fq_t100.job" "$T400_CG" "$J/d2fq_t400.job" || return 1 ;;
    linnos)
        local p qd
        for p in 1 2 3 4; do
            case $p in 1) qd=8;; 2) qd=16;; 3) qd=32;; 4) qd=64;; esac
            mk_job_file "$J/linnos_p${p}_lc.job" lc randread 4k 8 30 "$DATA_LC" "lat_p${p}_lc" agg
            mk_job_file "$J/linnos_p${p}_be.job" be randwrite 4k "$qd" 30 "$DATA_BE" "lat_p${p}_be" agg
            run_fio_pair "linnos_p${p}" "$LC_CG" "$J/linnos_p${p}_lc.job" \
                         "$BE_CG" "$J/linnos_p${p}_be.job" || return 1
        done ;;
    *) echo "unknown algo: $algo" >&2; return 2 ;;
    esac
}
