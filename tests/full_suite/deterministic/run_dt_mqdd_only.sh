#!/bin/bash
# run_dt_mqdd_only.sh — 仅运行 q_mq_deadline DT Tier-2 M01-M05 (Phase 0 快速验证)
# 在 VM 内执行, 跳过其它策略和 Tier-0/1
set -euo pipefail

BASE_DIR="${BASE_DIR:-$(cd "$(dirname "$0")/../../../.." && pwd)}"
FS_DIR="$BASE_DIR/tests/io/full_suite"
export TARGET_DEVICE="${TARGET_DEVICE:-nvme0n1}"
export IOEXT_DESTRUCTIVE_TEST=1
export POLICY_OVERRIDE=q_mq_deadline
export FULL_SUITE_MODE=deterministic-only
export ENABLE_COVERAGE=0

source "$FS_DIR/lib_test.sh"
source "$FS_DIR/00_setup.sh"

DT_DIR="$FS_DIR/deterministic"
TRACE_DRIVER="$DT_DIR/trace_driver"
BPFTRACE_SCRIPT="$DT_DIR/bpftrace/trace_dispatch.bt"
COMPARE_PY="$DT_DIR/compare_expect.py"
CHECK_INFLIGHT_PY="$DT_DIR/check_inflight.py"

if [ ! -x "$TRACE_DRIVER" ]; then
    gcc -O2 -o "$TRACE_DRIVER" "$DT_DIR/trace_driver.c" || fs_fail "trace_driver build failed"
fi

trace_driver_args() {
    local trace_file="$1"
    local mode
    mode=$(python3 -c "import json;d=json.load(open('$trace_file'));print(d.get('defaults',{}).get('submit_mode','sync'))" 2>/dev/null || echo sync)
    if [ "$mode" = "async_batch" ] || [ "$mode" = "async" ]; then
        local batch
        batch=$(python3 -c "import json;d=json.load(open('$trace_file'));print(d.get('defaults',{}).get('batch_size',8))" 2>/dev/null || echo 8)
        echo "--mode async --batch $batch"
        return
    fi
    echo ""
}

run_one() {
    local case_id="$1" trace_file="$2" expect_file="$3" extra="$4"
    local case_dir="$FS_RESULTS_DIR/dt_$(echo "$case_id" | tr '/.' '__')"
    mkdir -p "$case_dir"
    local loader_log="$case_dir/loader.log"
    local bpftrace_log="$case_dir/bpftrace.log"
    local trace_out="$case_dir/trace_output.txt"
    local compare_out="$case_dir/compare.json"
    local inflight_out="$case_dir/inflight.json"

    fs_set_scx_scheduler
    fs_start_loader q_mq_deadline "$loader_log" $extra
    if ! fs_wait_attached q_mq_deadline "$FS_LOADER_PID" 15; then
        cat "$loader_log" >&2; fs_stop_loader "$FS_LOADER_PID"; return 1
    fi

    bpftrace "$BPFTRACE_SCRIPT" > "$bpftrace_log" 2>&1 &
    local bpf_pid=$!
    # attach-wait (Phase-2 fix): cold VM 的首次 bpftrace attach 可 >1s,
    # 不等待则 trace 在探针就绪前播完、首 case 窗口丢失 (Phase-1 实测)。
    local aw=0
    until grep -q 'Attaching' "$bpftrace_log" 2>/dev/null; do
        sleep 0.2
        aw=$((aw + 1))
        [ "$aw" -ge 30 ] && { fs_warn "bpftrace attach-wait timeout"; break; }
    done
    sleep 1

    local driver_args
    driver_args="$(trace_driver_args "$trace_file")"
    fs_log "[$case_id] driver_args: $driver_args"
    if [ -n "$driver_args" ]; then
        "$TRACE_DRIVER" "$FS_DEV_PATH" "$trace_file" $driver_args > "$trace_out" 2>&1 || true
    else
        "$TRACE_DRIVER" "$FS_DEV_PATH" "$trace_file" > "$trace_out" 2>&1 || true
    fi

    sleep 1
    kill -INT "$bpf_pid" 2>/dev/null || true
    wait "$bpf_pid" 2>/dev/null || true

    grep -E 'TRACE_PLAY_(START|END)' "$trace_out" >> "$bpftrace_log" 2>/dev/null || true

    fs_stop_loader "$FS_LOADER_PID"

    if fs_dmesg_has_errors "$(fs_dmesg_mark "$case_id")"; then
        fs_warn "[$case_id] dmesg errors"; return 1
    fi

    python3 "$CHECK_INFLIGHT_PY" --bpftrace-log "$bpftrace_log" \
        --trace "$trace_file" \
        --out "$inflight_out" --min-inflight 2 2>&1 | tee "$case_dir/inflight_stdout.txt" || {
        fs_warn "[$case_id] G1 FAIL"; return 1
    }

    python3 "$COMPARE_PY" --bpftrace-log "$bpftrace_log" \
        --expect "$expect_file" --trace "$trace_file" \
        --policy q_mq_deadline --tier 2 --out "$compare_out" 2>&1 | tee "$case_dir/compare_stdout.txt" || true

    local status
    status=$(python3 -c "import json;print(json.load(open('$compare_out')).get('status','FAIL'))" 2>/dev/null || echo FAIL)
    fs_log "[$case_id] status=$status"
    [ "$status" = "PASS" ] && return 0 || return 1
}

CASES=(
    "M01|$DT_DIR/traces/micro/mqdd_seq_5.json|$DT_DIR/expect/golden/mqdd_seq_5.json|"
    "M02|$DT_DIR/traces/micro/mqdd_rev_5.json|$DT_DIR/expect/golden/mqdd_rev_5.json|"
    "M03|$DT_DIR/traces/micro/mqdd_lane_rw_5.json|$DT_DIR/expect/golden/mqdd_lane_rw_5.json|"
    "M04|$DT_DIR/traces/micro/mqdd_prio_5.json|$DT_DIR/expect/golden/mqdd_prio_5.json|"
    "M05|$DT_DIR/traces/micro/mqdd_adjacent_merge_8.json|$DT_DIR/expect/golden/mqdd_adjacent_merge_8.json|--merge-mode=3"
)

for entry in "${CASES[@]}"; do
    IFS='|' read -r cid trace expect extra <<< "$entry"
    fs_log "=== running $cid ==="
    for attempt in 1 2 3 4 5; do
        if run_one "mqdd_$cid" "$trace" "$expect" "$extra"; then
            fs_pass "mqdd_$cid (attempt=$attempt)"
            break
        else
            fs_warn "mqdd_$cid attempt $attempt FAILED"
        fi
        fs_cleanup_ioext
        sleep 1
    done
    fs_cleanup_ioext
    sleep 1
done

fs_log "=== Phase 0 validation done. Results: $FS_RESULTS_DIR ==="