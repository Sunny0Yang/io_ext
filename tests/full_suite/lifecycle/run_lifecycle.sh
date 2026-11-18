#!/bin/bash
# tests/io/full_suite/lifecycle/run_lifecycle.sh
# 动态压力与生命周期测试: Attach -> Run(fio) -> Detach
# 被 full_suite.sh source, 继承 FS_* 变量
set -euo pipefail

BASE_DIR="${BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}"
FS_DIR="${FS_DIR:-$BASE_DIR/tests/io/full_suite}"
[ -z "${FS_LOADER_DIR:-}" ] && source "$FS_DIR/lib_test.sh"

LC_DIR="$FS_DIR/lifecycle"
FIO_JOBS_DIR="$LC_DIR/fio_jobs"
RUNTIME="${FIO_RUNTIME:-15}"

run_lc_case() {
    local case_id="$1"
    local policy="$2"
    local fio_mode="${3:-randread}"
    local extra_loader_args="${4:-}"
    local attempt="$5"

    local case_dir="$FS_RESULTS_DIR/lc_$(echo "$case_id" | tr '/.' '__')"
    mkdir -p "$case_dir"
    local loader_log="$case_dir/loader.log"
    local fio_out="$case_dir/fio_${fio_mode}.json"
    local mark
    mark="$(fs_dmesg_mark "lc_${case_id}_${attempt}")"

    # 切换调度器
    fs_set_scx_scheduler

    # 特殊: q_bfq 需先 arb_bfq
    local arb_pid=""
    if [ "$policy" = "q_bfq" ]; then
        local arb_log="$case_dir/arb_bfq.log"
        "$FS_LOADER_DIR/loader_arb_bfq" --cgroup "$FS_CG_ROOT" > "$arb_log" 2>&1 &
        arb_pid=$!
        sleep 1
        if ! kill -0 "$arb_pid" 2>/dev/null; then
            cat "$arb_log" >&2
            return 1
        fi
    fi

    # 启动 loader
    local extra=()
    [ -n "$extra_loader_args" ] && extra+=($extra_loader_args)

    if [ "$policy" = "q_heimdall_v2" ]; then
        HEIMDALL_V2_DUMMY_ENFORCE=1 \
        fs_start_loader "$policy" "$loader_log" "${extra[@]}"
    else
        fs_start_loader "$policy" "$loader_log" "${extra[@]}"
    fi

    if ! fs_wait_attached "$policy" "$FS_LOADER_PID" 20; then
        cat "$loader_log" >&2
        fs_stop_loader "$FS_LOADER_PID"
        [ -n "$arb_pid" ] && fs_stop_loader "$arb_pid"
        return 1
    fi

    # 运行 fio
    local fio_job="$FIO_JOBS_DIR/${fio_mode}.fio"
    if [ ! -f "$fio_job" ]; then
        fs_warn "missing fio job: $fio_job, generating inline"
        cat > "$case_dir/lc_${fio_mode}.fio" <<FIO
[lc]
filename=$FS_DEV_PATH
ioengine=io_uring
direct=1
rw=$fio_mode
bs=4k
iodepth=16
numjobs=1
runtime=${RUNTIME}
time_based=1
group_reporting=1
clat_percentiles=1
percentile_list=99:99.9
FIO
        fio_job="$case_dir/lc_${fio_mode}.fio"
    fi

    fs_run_fio "$fio_job" "$fio_out" $((RUNTIME + 60))

    # 停止 loader (优雅 detach)
    fs_stop_loader "$FS_LOADER_PID"
    [ -n "$arb_pid" ] && fs_stop_loader "$arb_pid"

    # 判定 1: fio 成功
    if ! fs_validate_fio_json "$fio_out"; then
        fs_warn "[$case_id] fio error != 0"
        return 1
    fi

    # 判定 2: dmesg 无错误
    if fs_dmesg_has_errors "$mark"; then
        fs_dmesg_snapshot "$mark" > "$case_dir/dmesg_errors.txt"
        return 1
    fi

    # 判定 3: 有实际 IO (total_ios > 0)
    local total_ios
    total_ios=$(fs_fio_stat "$fio_out" "total_ios")
    if [ "${total_ios:-0}" -le 0 ]; then
        fs_warn "[$case_id] total_ios=0, no IO completed"
        return 1
    fi

    # 记录指标
    local iops p99
    iops=$(fs_fio_stat "$fio_out" "iops")
    p99=$(fs_fio_clat_percentile "$fio_out" "99.000000")
    printf '{"case_id":"%s","policy":"%s","fio_mode":"%s","total_ios":%s,"iops":%s,"p99_ns":%s}\n' \
        "$case_id" "$policy" "$fio_mode" "$total_ios" "$iops" "$p99" \
        > "$case_dir/metrics.json" 2>/dev/null || true

    return 0
}

# 生命周期 Case 矩阵
# 格式: case_id|policy|fio_mode|extra_args
LC_CASES=()
for pol in "${FS_NON_TRIVIAL_POLICIES[@]}"; do
    LC_CASES+=("${pol}-LC-01|${pol}|randread|")
    LC_CASES+=("${pol}-LC-02|${pol}|randwrite|")
    LC_CASES+=("${pol}-LC-03|${pol}|randrw|")
done
# 策略特有
LC_CASES+=("q_mq_deadline-LC-07|q_mq_deadline|seqwrite|--merge-mode=3")
LC_CASES+=("q_kyber-LC-07|q_kyber|randread|--merge-enable=0")
LC_CASES+=("q_kyber-LC-08|q_kyber|randread|--merge-enable=1")

# hot-switch
LC_HOTSWITCH_CASES=(
    "q_fifo|q_bfq"
    "q_mq_deadline|q_kyber"
    "q_kyber|q_heimdall_v2"
    "q_wedf|q_sp_edf"
)

run_hotswitch_case() {
    local case_id="$1"
    local pol_a="$2"
    local pol_b="$3"
    local attempt="$4"
    local case_dir="$FS_RESULTS_DIR/lc_hs_$(echo "$case_id" | tr '/.' '__')"
    mkdir -p "$case_dir"
    local mark
    mark="$(fs_dmesg_mark "hs_${case_id}_${attempt}")"

    fs_set_scx_scheduler

    # 启动 A
    local log_a="$case_dir/loader_a.log"
    fs_start_loader "$pol_a" "$log_a"
    fs_wait_attached "$pol_a" "$FS_LOADER_PID" 15 || { fs_stop_loader "$FS_LOADER_PID"; return 1; }
    local pid_a=$FS_LOADER_PID

    # 启动 fio
    local fio_job="$FIO_JOBS_DIR/randread.fio"
    [ -f "$fio_job" ] || fio_job="$case_dir/hs.fio"
    if [ ! -f "$fio_job" ]; then
        cat > "$fio_job" <<FIO
[lc]
filename=$FS_DEV_PATH
ioengine=io_uring
direct=1
rw=randread
bs=4k
iodepth=16
numjobs=1
runtime=$RUNTIME
time_based=1
group_reporting=1
FIO
    fi

    fio "$fio_job" --output-format=json > "$case_dir/fio_hs.json" 2>&1 &
    local fio_pid=$!
    sleep 3

    # 切换到 B
    fs_stop_loader "$pid_a"
    sleep 1
    local log_b="$case_dir/loader_b.log"
    fs_start_loader "$pol_b" "$log_b"
    fs_wait_attached "$pol_b" "$FS_LOADER_PID" 15 || { kill "$fio_pid" 2>/dev/null; fs_stop_loader "$FS_LOADER_PID"; return 1; }
    local pid_b=$FS_LOADER_PID

    wait "$fio_pid" 2>/dev/null || true
    fs_stop_loader "$pid_b"

    if fs_dmesg_has_errors "$mark"; then
        fs_dmesg_snapshot "$mark" > "$case_dir/dmesg_errors.txt"
        return 1
    fi
    return 0
}

fs_log "running ${#LC_CASES[@]} lifecycle cases..."

for entry in "${LC_CASES[@]}"; do
    IFS='|' read -r case_id policy fio_mode extra <<< "$entry"
    if [ ! -x "$FS_LOADER_DIR/loader_${policy}" ]; then
        fs_record_result "$case_id" "BLOCKED" "loader_${policy} missing"
        continue
    fi
    run_with_retry() {
        local cid="$1" att="$2"
        run_lc_case "$cid" "$policy" "$fio_mode" "$extra" "$att"
    }
    fs_run_case_with_retry "$case_id" run_with_retry 5
done

# hot-switch cases
fs_log "running ${#LC_HOTSWITCH_CASES[@]} hot-switch cases..."
for entry in "${LC_HOTSWITCH_CASES[@]}"; do
    IFS='|' read -r pol_a pol_b <<< "$entry"
    case_id="HS-${pol_a}_to_${pol_b}"
    if [ ! -x "$FS_LOADER_DIR/loader_${pol_a}" ] || [ ! -x "$FS_LOADER_DIR/loader_${pol_b}" ]; then
        fs_record_result "$case_id" "BLOCKED" "loader missing"
        continue
    fi
    run_hs_retry() {
        local cid="$1" att="$2"
        run_hotswitch_case "$cid" "$pol_a" "$pol_b" "$att"
    }
    fs_run_case_with_retry "$case_id" run_hs_retry 5
done

fs_log "lifecycle phase: PASS=$FS_CASES_PASS FAIL=$FS_CASES_FAIL BLOCKED=$FS_CASES_BLOCKED"