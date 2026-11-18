#!/bin/bash
# tests/io/full_suite/deterministic/run_deterministic.sh
# 确定性分发测试 (三层架构: Tier-0 Smoke / Tier-1 Invariants / Tier-2 Oracle)
# 被 full_suite.sh source, 继承 FS_* 变量
set -euo pipefail

BASE_DIR="${BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}"
FS_DIR="${FS_DIR:-$BASE_DIR/tests/io/full_suite}"
[ -z "${FS_LOADER_DIR:-}" ] && source "$FS_DIR/lib_test.sh"

DT_DIR="$FS_DIR/deterministic"
TRACE_DRIVER="$DT_DIR/trace_driver"
BPFTRACE_SCRIPT="$DT_DIR/bpftrace/trace_dispatch.bt"
COMPARE_PY="$DT_DIR/compare_expect.py"
CHECK_INFLIGHT_PY="$DT_DIR/check_inflight.py"

if [ ! -x "$TRACE_DRIVER" ]; then
    fs_log "building trace_driver..."
    gcc -O2 -o "$TRACE_DRIVER" "$DT_DIR/trace_driver.c" 2>/dev/null || {
        fs_warn "trace_driver build failed; all DT cases will be BLOCKED"
    }
fi

# 从 trace JSON 读取 submit_mode 决定 trace_driver 参数
# Tier-2 默认 async; Tier-0/1 保持 sync (兼容)
trace_driver_args() {
    local trace_file="$1"
    local tier="$2"
    if [ "$tier" -ge 2 ]; then
        local mode
        mode=$(python3 -c "import json;d=json.load(open('$trace_file'));print(d.get('defaults',{}).get('submit_mode','sync'))" 2>/dev/null || echo sync)
        if [ "$mode" = "async_batch" ] || [ "$mode" = "async" ]; then
            local batch
            batch=$(python3 -c "import json;d=json.load(open('$trace_file'));print(d.get('defaults',{}).get('batch_size',8))" 2>/dev/null || echo 8)
            echo "--mode async --batch $batch"
            return
        fi
    fi
    echo ""
}

run_dt_case() {
    local case_id="$1"
    local policy="$2"
    local trace_file="$3"
    local expect_file="$4"
    local tier="$5"
    local extra_loader_args="${6:-}"
    local attempt="$7"

    local case_dir="$FS_RESULTS_DIR/dt_$(echo "$case_id" | tr '/.' '__')"
    mkdir -p "$case_dir"
    local loader_log="$case_dir/loader.log"
    local bpftrace_log="$case_dir/bpftrace.log"
    local trace_out="$case_dir/trace_output.txt"
    local compare_out="$case_dir/compare.json"
    local inflight_out="$case_dir/inflight.json"
    local stats_file="$case_dir/bpftool_stats.json"

    local mark
    mark="$(fs_dmesg_mark "dt_${case_id}_${attempt}")"

    # trace_driver 必须 exist (Tier-0+ 不允许 fio fallback 假 PASS)
    if [ ! -x "$TRACE_DRIVER" ]; then
        fs_warn "[$case_id] trace_driver missing — BLOCKED (no fio fallback)"
        return 1
    fi

    fs_set_scx_scheduler

    local extra_args=()
    [ -n "$extra_loader_args" ] && extra_args+=($extra_loader_args)

    local env_prefix=""
    if [ "$policy" = "q_heimdall_v2" ]; then
        env_prefix="HEIMDALL_V2_DUMMY_ENFORCE=1"
    fi

    if [ -n "$env_prefix" ]; then
        env $env_prefix fs_start_loader "$policy" "$loader_log" "${extra_args[@]}"
    else
        fs_start_loader "$policy" "$loader_log" "${extra_args[@]}"
    fi

    if ! fs_wait_attached "$policy" "$FS_LOADER_PID" 15; then
        cat "$loader_log" >&2
        fs_stop_loader "$FS_LOADER_PID"
        return 1
    fi

    # collect bpftool stats before (for Tier-1 invariant cases)
    if [ "$tier" -ge 1 ]; then
        local stats_map_name=""
        case "$policy" in
            q_merge_probe) stats_map_name="merge_stats" ;;
            q_mq_deadline) stats_map_name="merge_stats_map" ;;
            q_heimdall_v2) stats_map_name="hm_v2_stats" ;;
            q_heimdall) stats_map_name="hm_stats" ;;
            q_kyber) stats_map_name="kyber_merge_cfg" ;;
        esac
        if [ -n "$stats_map_name" ] && command -v bpftool >/dev/null 2>&1; then
            bpftool -j map dump name "$stats_map_name" > "$case_dir/stats_before.json" 2>/dev/null || true
        fi
    fi

    # bpftrace
    local bpf_pid=""
    if [ -f "$BPFTRACE_SCRIPT" ] && command -v bpftrace >/dev/null 2>&1; then
        bpftrace "$BPFTRACE_SCRIPT" > "$bpftrace_log" 2>&1 &
        bpf_pid=$!
        # attach-wait (Phase-2 fix): on a cold VM the first bpftrace
        # invocation can take >1s to attach (BTF load); without waiting the
        # trace plays before probes are live and the capture window is lost.
        local aw=0
        until grep -q 'Attaching' "$bpftrace_log" 2>/dev/null; do
            sleep 0.2
            aw=$((aw + 1))
            [ "$aw" -ge 30 ] && { fs_warn "bpftrace attach-wait timeout"; break; }
        done
        sleep 1
    fi

    # play trace
    local driver_args
    driver_args="$(trace_driver_args "$trace_file" "$tier")"
    fs_log "[$case_id] trace_driver args: $driver_args"
    if [ -n "$driver_args" ]; then
        "$TRACE_DRIVER" "$FS_DEV_PATH" "$trace_file" $driver_args > "$trace_out" 2>&1
    else
        "$TRACE_DRIVER" "$FS_DEV_PATH" "$trace_file" > "$trace_out" 2>&1
    fi
    local trace_rc=$?
    if [ "$trace_rc" -ne 0 ]; then
        fs_warn "[$case_id] trace_driver exited $trace_rc"
        cat "$trace_out" >&2 || true
    fi

    sleep 1

    # stop bpftrace
    if [ -n "$bpf_pid" ]; then
        kill -INT "$bpf_pid" 2>/dev/null || true
        wait "$bpf_pid" 2>/dev/null || true
    fi

    # 将 trace_driver 的 TRACE_PLAY marker 追加到 bpftrace 日志,
    # 使 compare_expect.py 能用同一文件做窗口隔离
    grep -E 'TRACE_PLAY_(START|END)' "$trace_out" >> "$bpftrace_log" 2>/dev/null || true

    # collect bpftool stats after
    if [ "$tier" -ge 1 ]; then
        local stats_map_name=""
        case "$policy" in
            q_merge_probe) stats_map_name="merge_stats" ;;
            q_mq_deadline) stats_map_name="merge_stats_map" ;;
            q_heimdall_v2) stats_map_name="hm_v2_stats" ;;
            q_heimdall) stats_map_name="hm_stats" ;;
            q_kyber) stats_map_name="kyber_merge_cfg" ;;
        esac
        if [ -n "$stats_map_name" ] && command -v bpftool >/dev/null 2>&1; then
            bpftool -j map dump name "$stats_map_name" > "$stats_file" 2>/dev/null || true
        fi
    fi

    fs_stop_loader "$FS_LOADER_PID"

    # dmesg check
    if fs_dmesg_has_errors "$mark"; then
        fs_dmesg_snapshot "$mark" > "$case_dir/dmesg_errors.txt"
        return 1
    fi

    # bpftrace log must exist for assertion
    if [ ! -f "$bpftrace_log" ] || [ ! -s "$bpftrace_log" ]; then
        fs_warn "[$case_id] bpftrace log empty or missing"
        return 1
    fi

    # G1 gate (Tier>=2): scheduler must have >=2 inflight to claim reordering
    if [ "$tier" -ge 2 ] && [ -x "$CHECK_INFLIGHT_PY" ]; then
        python3 "$CHECK_INFLIGHT_PY" --bpftrace-log "$bpftrace_log" \
            --trace "$trace_file" \
            --out "$inflight_out" --min-inflight 2 >/dev/null 2>&1 || {
            fs_warn "[$case_id] G1 FAIL: scheduler_inactive (max_inflight<2)"
            cat "$inflight_out" 2>/dev/null >&2 || true
            return 1
        }
        # also verify G2 (dispatch_with_choice >= 1)
        local g2
        g2=$(python3 -c "import json;d=json.load(open('$inflight_out'));print(1 if d.get('dispatch_with_choice',0)>=1 else 0)" 2>/dev/null || echo 0)
        if [ "$g2" != "1" ]; then
            fs_warn "[$case_id] G2 FAIL: no dispatch with multiple pending"
            return 1
        fi
    fi

    # run compare_expect.py
    local expect_arg="${expect_file:-}"
    [ -z "$expect_arg" ] && expect_arg="NONE"

    local stats_arg=""
    [ -f "$stats_file" ] && stats_arg="--bpftool-stats $stats_file"

    python3 "$COMPARE_PY" \
        --bpftrace-log "$bpftrace_log" \
        --expect "$expect_arg" \
        --trace "$trace_file" \
        --policy "$policy" \
        --tier "$tier" \
        $stats_arg \
        --out "$compare_out" 2>&1 | tee "$case_dir/compare_stdout.txt" >&2 || true

    # read compare result
    if [ -f "$compare_out" ]; then
        local status
        status=$(python3 -c "import json;print(json.load(open('$compare_out')).get('status','FAIL'))" 2>/dev/null || echo "FAIL")
        if [ "$status" != "PASS" ]; then
            cat "$compare_out" >&2 || true
            return 1
        fi
    else
        fs_warn "[$case_id] compare.json not generated"
        return 1
    fi

    cp "$loader_log" "$case_dir/" 2>/dev/null || true
    return 0
}

# Case matrix: case_id|policy|trace|expect|tier|extra_args
# tier 0 = smoke, 1 = invariants, 2 = micro oracle
DT_CASES=(
    # ── Tier-0 Smoke (10 policies, 1 each) ──
    "q_mq_deadline-DT-SMOKE|q_mq_deadline|$DT_DIR/traces/smoke/seq_4k_read_3.json||0|"
    "q_bfq-DT-SMOKE|q_bfq|$DT_DIR/traces/smoke/seq_4k_read_3.json||0|"
    "q_heimdall_v2-DT-SMOKE|q_heimdall_v2|$DT_DIR/traces/smoke/seq_4k_read_3.json||0|"
    "q_kyber-DT-SMOKE|q_kyber|$DT_DIR/traces/smoke/seq_4k_read_3.json||0|"
    "q_heimdall-DT-SMOKE|q_heimdall|$DT_DIR/traces/smoke/seq_4k_read_3.json||0|"
    "q_sp_edf-DT-SMOKE|q_sp_edf|$DT_DIR/traces/smoke/seq_4k_read_3.json||0|"
    "q_merge_probe-DT-SMOKE|q_merge_probe|$DT_DIR/traces/smoke/seq_4k_read_3.json||0|"
    "q_wedf-DT-SMOKE|q_wedf|$DT_DIR/traces/smoke/seq_4k_read_3.json||0|"
    "q_prio-DT-SMOKE|q_prio|$DT_DIR/traces/smoke/seq_4k_read_3.json||0|"
    "q_none-DT-SMOKE|q_none|$DT_DIR/traces/smoke/seq_4k_read_3.json||0|"
    "q_ddl-DT-SMOKE|q_ddl|$DT_DIR/traces/smoke/seq_4k_read_3.json||0|"

    # ── Tier-1 Invariants ──
    "q_merge_probe-DT-I01|q_merge_probe|$DT_DIR/traces/micro/adjacent_read_8.json|$DT_DIR/expect/manual/merge_probe_mode0.json|1|--mode=0"
    "q_merge_probe-DT-I02|q_merge_probe|$DT_DIR/traces/micro/adjacent_read_8.json|$DT_DIR/expect/manual/merge_probe_mode1.json|1|--mode=1"
    "q_merge_probe-DT-I03|q_merge_probe|$DT_DIR/traces/micro/adjacent_read_8.json|$DT_DIR/expect/manual/merge_probe_mode2.json|1|--mode=2"
    "q_merge_probe-DT-I04|q_merge_probe|$DT_DIR/traces/micro/adjacent_read_8.json|$DT_DIR/expect/manual/merge_probe_mode3.json|1|--mode=3"
    "q_kyber-DT-I01|q_kyber|$DT_DIR/traces/micro/adjacent_read_8.json|$DT_DIR/expect/manual/kyber_merge_on.json|1|--merge-enable=1"
    "q_kyber-DT-I02|q_kyber|$DT_DIR/traces/micro/adjacent_read_8.json|$DT_DIR/expect/manual/kyber_merge_off.json|1|--merge-enable=0"
    "q_kyber-DT-T01|q_kyber|$DT_DIR/traces/micro/kyber_read_fifo_8.json|$DT_DIR/expect/partial/kyber_t1_counts.json|1|--merge-enable=0"
    "q_heimdall_v2-DT-I01|q_heimdall_v2|$DT_DIR/traces/micro/seq_read_16.json|$DT_DIR/expect/manual/heimdall_v2_hold.json|1|"
    "q_heimdall_v2-DT-I02|q_heimdall_v2|$DT_DIR/traces/micro/seq_read_16.json|$DT_DIR/expect/manual/heimdall_v2_insert.json|1|"
    "q_heimdall-DT-I01|q_heimdall|$DT_DIR/traces/micro/seq_read_16.json||1|"
    "q_none-DT-I01|q_none|$DT_DIR/traces/micro/seq_read_16.json||1|"
    "q_wedf-DT-I01|q_wedf|$DT_DIR/traces/micro/wedf_sector_asc_8.json|$DT_DIR/expect/partial/wedf_t1_counts.json|1|"
    "q_ddl-DT-I01|q_ddl|$DT_DIR/traces/micro/ddl_bulk_first_5.json|$DT_DIR/expect/partial/ddl_t1_counts.json|1|"

    # ── Tier-2 Micro Oracle ──
    "q_mq_deadline-DT-M01|q_mq_deadline|$DT_DIR/traces/micro/mqdd_seq_5.json|$DT_DIR/expect/golden/mqdd_seq_5.json|2|"
    "q_mq_deadline-DT-M02|q_mq_deadline|$DT_DIR/traces/micro/mqdd_rev_5.json|$DT_DIR/expect/golden/mqdd_rev_5.json|2|"
    "q_mq_deadline-DT-M03|q_mq_deadline|$DT_DIR/traces/micro/mqdd_lane_rw_5.json|$DT_DIR/expect/golden/mqdd_lane_rw_5.json|2|"
    "q_mq_deadline-DT-M04|q_mq_deadline|$DT_DIR/traces/micro/mqdd_prio_5.json|$DT_DIR/expect/golden/mqdd_prio_5.json|2|"
    "q_mq_deadline-DT-M05|q_mq_deadline|$DT_DIR/traces/micro/mqdd_adjacent_merge_8.json|$DT_DIR/expect/golden/mqdd_adjacent_merge_8.json|2|--merge-mode=3"
    # q_mq_deadline-DT-M06 (mqdd_starv_20) deliberately NOT registered here:
    # the starvation-flip golden is rig-dependent (needs the virtio+200MBps
    # saturation rig where submission outpaces completion by 10x+; the shared
    # matrix runs the historical nvme rig where the flip point moves and the
    # write-epoch constraint fails - verified full_suite_20260901_174251).
    # Validated PASS on its designed rig (results/mqdd_phase3/, scratch run
    # full_suite_20260901_172250); belongs in a future mqdd triad CI.
    "q_prio-DT-M01|q_prio|$DT_DIR/traces/micro/prio_5.json|$DT_DIR/expect/golden/prio_5.json|2|"
    "q_prio-DT-M02|q_prio|$DT_DIR/traces/micro/prio_tie_5.json|$DT_DIR/expect/golden/prio_tie_5.json|2|"
    "q_sp_edf-DT-M01|q_sp_edf|$DT_DIR/traces/micro/spedf_deadline_5.json|$DT_DIR/expect/golden/spedf_deadline_5.json|2|"
    "q_sp_edf-DT-M02|q_sp_edf|$DT_DIR/traces/micro/spedf_tie_prio_5.json|$DT_DIR/expect/golden/spedf_tie_prio_5.json|2|"
    "q_sp_edf-DT-M03|q_sp_edf|$DT_DIR/traces/micro/spedf_large_small_8.json|$DT_DIR/expect/golden/spedf_large_small_8.json|2|"
    "q_sp_edf-DT-M04|q_sp_edf|$DT_DIR/traces/micro/spedf_burst_18.json|$DT_DIR/expect/golden/spedf_burst_18.json|2|"
    "q_kyber-DT-K01|q_kyber|$DT_DIR/traces/micro/kyber_read_fifo_8.json|$DT_DIR/expect/golden/kyber_read_fifo_8.json|2|--merge-enable=0"
    "q_kyber-DT-K02|q_kyber|$DT_DIR/traces/micro/kyber_rw_domains_8.json|$DT_DIR/expect/partial/kyber_rw_domains_8.json|2|--merge-enable=0 --read-target-ns 100000000000000 --write-target-ns 100000000000000"
    "q_kyber-DT-K03|q_kyber|$DT_DIR/traces/micro/kyber_adjacent_merge_8.json|$DT_DIR/expect/partial/kyber_merge_cov.json|2|--merge-enable=1"
    "q_wedf-DT-M01|q_wedf|$DT_DIR/traces/micro/wedf_sector_asc_8.json|$DT_DIR/expect/golden/wedf_sector_asc_8.json|2|"
    "q_wedf-DT-M02|q_wedf|$DT_DIR/traces/micro/wedf_rw_edf_8.json|$DT_DIR/expect/golden/wedf_rw_edf_8.json|2|"
    "q_ddl-DT-M01|q_ddl|$DT_DIR/traces/micro/ddl_bulk_first_5.json|$DT_DIR/expect/golden/ddl_bulk_first_5.json|2|"
    "q_ddl-DT-M02|q_ddl|$DT_DIR/traces/micro/ddl_cost_class_5.json|$DT_DIR/expect/golden/ddl_cost_class_5.json|2|"
    "q_ddl-DT-M03|q_ddl|$DT_DIR/traces/micro/ddl_lat_fifo_desc_5.json|$DT_DIR/expect/golden/ddl_lat_fifo_desc_5.json|2|"
    "q_none-DT-M01|q_none|$DT_DIR/traces/micro/seq_5.json|$DT_DIR/expect/golden/seq_5.json|2|"
)

fs_log "running ${#DT_CASES[@]} deterministic cases (Tier-0/1/2)..."

for entry in "${DT_CASES[@]}"; do
    IFS='|' read -r case_id policy trace expect tier extra <<< "$entry"

    if [ ! -x "$FS_LOADER_DIR/loader_${policy}" ]; then
        fs_record_result "$case_id" "BLOCKED" "loader_${policy} missing"
        continue
    fi
    if [ ! -f "$trace" ]; then
        fs_record_result "$case_id" "BLOCKED" "trace missing: $(basename "$trace")"
        continue
    fi
    # Tier-2 without expect → BLOCKED (not silently PASS)
    if [ "$tier" = "2" ] && [ -z "$expect" ]; then
        fs_record_result "$case_id" "BLOCKED" "Tier-2 requires golden oracle"
        continue
    fi
    if [ "$tier" = "2" ] && [ -n "$expect" ] && [ ! -f "$expect" ]; then
        fs_record_result "$case_id" "BLOCKED" "golden oracle missing: $(basename "$expect")"
        continue
    fi

    run_with_retry() {
        local cid="$1" att="$2"
        run_dt_case "$cid" "$policy" "$trace" "$expect" "$tier" "$extra" "$att"
    }
    fs_run_case_with_retry "$case_id" run_with_retry 5
done

fs_log "deterministic phase: PASS=$FS_CASES_PASS FAIL=$FS_CASES_FAIL BLOCKED=$FS_CASES_BLOCKED"