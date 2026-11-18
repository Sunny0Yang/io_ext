#!/bin/bash
# run_lc_mqdd_only.sh — 仅运行 q_mq_deadline LC 01/02/03 (Phase 2 VM 验证)
set -euo pipefail

BASE_DIR="${BASE_DIR:-$(cd "$(dirname "$0")/../../../.." && pwd)}"
FS_DIR="$BASE_DIR/tests/io/full_suite"
export TARGET_DEVICE="${TARGET_DEVICE:-nvme0n1}"
export IOEXT_DESTRUCTIVE_TEST=1
export POLICY_OVERRIDE=q_mq_deadline
export FULL_SUITE_MODE=lifecycle-only
export ENABLE_COVERAGE=0
export FIO_RUNTIME="${FIO_RUNTIME:-10}"

source "$FS_DIR/lib_test.sh"
source "$FS_DIR/00_setup.sh"

run_one() {
    local case_id="$1" fio_mode="$2" extra="$3"
    local case_dir="$FS_RESULTS_DIR/lc_$(echo "$case_id" | tr '/.' '__')"
    mkdir -p "$case_dir"
    local loader_log="$case_dir/loader.log"
    local fio_out="$case_dir/fio_${fio_mode}.json"
    local mark; mark="$(fs_dmesg_mark "lc_${case_id}")"

    fs_set_scx_scheduler
    fs_start_loader q_mq_deadline "$loader_log" $extra
    if ! fs_wait_attached q_mq_deadline "$FS_LOADER_PID" 20; then
        cat "$loader_log" >&2; fs_stop_loader "$FS_LOADER_PID"; return 1
    fi

    local fio_job="$FS_DIR/lifecycle/fio_jobs/${fio_mode}.fio"
    if [ ! -f "$fio_job" ]; then
        cat > "$case_dir/lc_${fio_mode}.fio" <<FIO
[lc]
filename=$FS_DEV_PATH
ioengine=io_uring
direct=1
rw=$fio_mode
bs=4k
iodepth=16
numjobs=1
runtime=${FIO_RUNTIME}
time_based=1
group_reporting=1
clat_percentiles=1
percentile_list=99:99.9
FIO
        fio_job="$case_dir/lc_${fio_mode}.fio"
    fi

    fs_run_fio "$fio_job" "$fio_out" $((FIO_RUNTIME + 60))
    fs_stop_loader "$FS_LOADER_PID"

    if ! fs_validate_fio_json "$fio_out"; then
        fs_warn "[$case_id] fio error != 0"; return 1
    fi
    if fs_dmesg_has_errors "$mark"; then
        fs_dmesg_snapshot "$mark" > "$case_dir/dmesg_errors.txt"; return 1
    fi
    local total_ios; total_ios=$(fs_fio_stat "$fio_out" "total_ios")
    if [ "${total_ios:-0}" -le 0 ]; then
        fs_warn "[$case_id] total_ios=0"; return 1
    fi
    local iops p99
    iops=$(fs_fio_stat "$fio_out" "iops")
    p99=$(fs_fio_clat_percentile "$fio_out" "99.000000")
    printf '{"case_id":"%s","fio_mode":"%s","total_ios":%s,"iops":%s,"p99_ns":%s}\n' \
        "$case_id" "$fio_mode" "$total_ios" "$iops" "$p99" > "$case_dir/metrics.json"
    fs_log "[$case_id] total_ios=$total_ios iops=$iops p99=$p99"
    return 0
}

CASES=(
    "LC-01|randread|"
    "LC-02|randwrite|"
    "LC-03|randrw|"
)

for entry in "${CASES[@]}"; do
    IFS='|' read -r cid fio_mode extra <<< "$entry"
    fs_log "=== running $cid ($fio_mode) ==="
    for attempt in 1 2 3 4 5; do
        if run_one "q_mq_deadline-$cid" "$fio_mode" "$extra"; then
            fs_pass "q_mq_deadline-$cid (attempt=$attempt)"; break
        fi
        fs_warn "q_mq_deadline-$cid attempt $attempt FAILED"
        fs_cleanup_ioext; sleep 1
    done
    fs_cleanup_ioext; sleep 1
done

# hot-switch q_mq_deadline <-> q_kyber
fs_log "=== hot-switch q_mq_deadline -> q_kyber ==="
fs_set_scx_scheduler
fs_start_loader q_mq_deadline "$FS_RESULTS_DIR/hs_a.log"
fs_wait_attached q_mq_deadline "$FS_LOADER_PID" 15 || { fs_stop_loader "$FS_LOADER_PID"; fs_warn "hs A fail"; }
pid_a=$FS_LOADER_PID
fio --name=hs --filename="$FS_DEV_PATH" --ioengine=io_uring --direct=1 --rw=randread --bs=4k --iodepth=16 --runtime=10 --time_based --output-format=json > "$FS_RESULTS_DIR/hs_fio.json" 2>&1 &
fio_pid=$!
sleep 3
fs_stop_loader "$pid_a"
sleep 1
fs_start_loader q_kyber "$FS_RESULTS_DIR/hs_b.log"
fs_wait_attached q_kyber "$FS_LOADER_PID" 15 || { kill "$fio_pid" 2>/dev/null; fs_stop_loader "$FS_LOADER_PID"; fs_warn "hs B fail"; }
pid_b=$FS_LOADER_PID
wait "$fio_pid" 2>/dev/null || true
fs_stop_loader "$pid_b"
fs_log "hot-switch complete"
fs_cleanup_ioext

fs_log "=== LC validation done. Results: $FS_RESULTS_DIR ==="