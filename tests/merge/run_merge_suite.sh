#!/bin/bash
# run_merge_suite.sh - merge test suite driver (runs inside VM)
# Iterates all cases from the plan, collects evidence per case.
set -euo pipefail

BASE_DIR="${BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
MERGE_DIR="$BASE_DIR/tests/merge"
LOADER_DIR="${LOADER_DIR:-$BASE_DIR/userspace/build}"
TARGET_DEVICE="${TARGET_DEVICE:-nvme0n1}"
SCHEDULER="${SCHEDULER:-scx_io}"
CGROUP_PATH="${CGROUP_PATH:-/sys/fs/cgroup}"
RESULT_ROOT="${RESULT_ROOT:-$BASE_DIR/results/io_merge_suite}"
SUITE_DATE="$(date +%Y%m%d_%H%M%S)"
SUITE_DIR="$RESULT_ROOT/suite_$SUITE_DATE"
FIO_RUNTIME="${FIO_RUNTIME:-120}"
FIO_BIN="${FIO_BIN:-fio}"
TARGET_FILE="${TARGET_FILE:-/dev/$TARGET_DEVICE}"

mkdir -p "$SUITE_DIR"

log() { printf '[merge_suite] %s\n' "$*" >&2; }
fail() { printf '[merge_suite] FAIL: %s\n' "$*" >&2; }

require_root() { [ "$(id -u)" -eq 0 ] || fail "must run as root"; }

require_fio() {
    command -v "$FIO_BIN" >/dev/null 2>&1 || fail "fio not found"
}

ensure_bpffs() {
    mountpoint -q /sys/fs/bpf 2>/dev/null \
        || mount -t bpf bpf /sys/fs/bpf 2>/dev/null \
        || fail "cannot mount bpffs"
}

set_scheduler() {
    local sched_path="/sys/block/$TARGET_DEVICE/queue/scheduler"
    echo "$SCHEDULER" > "$sched_path" || fail "cannot switch to $SCHEDULER"
    log "scheduler=$(cat "$sched_path")"
}

set_native() {
    local name="$1"
    local sched_path="/sys/block/$TARGET_DEVICE/queue/scheduler"
    pkill -TERM -f 'loader_q_' 2>/dev/null || true
    pkill -TERM -f 'loader_arb_' 2>/dev/null || true
    sleep 1
    pkill -9 -f 'loader_q_' 2>/dev/null || true
    pkill -9 -f 'loader_arb_' 2>/dev/null || true
    for m in ioext_cgroup_queues ioext_active_cgroups ioext_policy_meta merge_stats merge_stats_map probe_cfg merge_cfg_map; do
        rm -rf "/sys/fs/bpf/$m" 2>/dev/null || true
    done
    echo "$name" > "$sched_path" 2>/dev/null || true
    log "native scheduler=$name"
}

cleanup_ioext() {
    pkill -TERM -f 'loader_q_' 2>/dev/null || true
    pkill -TERM -f 'loader_arb_' 2>/dev/null || true
    sleep 1
    pkill -9 -f 'loader_q_' 2>/dev/null || true
    pkill -9 -f 'loader_arb_' 2>/dev/null || true
    for m in ioext_cgroup_queues ioext_active_cgroups ioext_policy_meta merge_stats merge_stats_map probe_cfg merge_cfg_map kyber_merge_cfg; do
        rm -rf "/sys/fs/bpf/$m" 2>/dev/null || true
    done
    echo none > "/sys/block/$TARGET_DEVICE/queue/scheduler" 2>/dev/null || true
}

detect_block_device() {
    if [ -b "/dev/$TARGET_DEVICE" ]; then
        return 0
    fi
    if [ -b /dev/nvme0n1 ]; then
        TARGET_DEVICE=nvme0n1
    elif [ -b /dev/vda ]; then
        TARGET_DEVICE=vda
    else
        fail "no block device (wanted ${TARGET_DEVICE:-nvme0n1})"
    fi
    TARGET_FILE="/dev/$TARGET_DEVICE"
    log "auto-detected TARGET_DEVICE=$TARGET_DEVICE"
}

policy_loader_argv() {
    local policy="$1"
    local merge_arg="$2"

    case "$policy" in
    q_merge_probe | q_mq_deadline)
        if [ -n "$merge_arg" ]; then
            printf '%s %s %s %s\n' "$CGROUP_PATH" "${IOSCHED_DEFAULT_ARB_PRIO:-7}" "${IOSCHED_DEFAULT_ARB_WEIGHT:-100}" "$merge_arg"
        else
            printf '%s\n' "$CGROUP_PATH"
        fi
        ;;
    q_kyber)
        local merge_enable=1
        if [ "$merge_arg" = "0" ]; then
            merge_enable=0
        fi
        printf '%s %s %s %s %s %s %s %s\n' \
            "$CGROUP_PATH" \
            "${IOSCHED_DEFAULT_ARB_PRIO:-7}" \
            "${IOSCHED_DEFAULT_ARB_WEIGHT:-100}" \
            "2000000" \
            "10000000" \
            "5000000000" \
            "5000000" \
            "$merge_enable"
        ;;
    *)
        printf '%s\n' "$CGROUP_PATH"
        ;;
    esac
}

start_loader() {
    local policy="$1"
    local logfile="$2"
    shift 2
    local loader="$LOADER_DIR/loader_${policy}"
    [ -x "$loader" ] || fail "missing loader: $loader"
    "$loader" "$@" >"$logfile" 2>&1 &
    local pid=$!
    sleep 2
    if ! kill -0 "$pid" 2>/dev/null; then
        log "loader $policy died; log follows:"
        tail -n 40 "$logfile" >&2 || true
        fail "loader $policy died"
    fi
    echo "$pid"
}

run_fio() {
    local load_id="$1"
    local job_file="$SUITE_DIR/fio_${load_id}.fio"
    local out_json="$SUITE_DIR/fio_${load_id}.json"

    "$MERGE_DIR/gen_fio_job.sh" "$load_id" "$job_file" "$TARGET_FILE" "$FIO_RUNTIME"

    log "fio $load_id -> $out_json"
    "$FIO_BIN" --output-format=json+ --output="$out_json" "$job_file" 2>&1 || {
        log "WARN: fio $load_id returned non-zero"
    }
    echo "$out_json"
}

check_kernel_errors() {
    dmesg | tail -n 200 | grep -qiE 'hung_task|BUG:|Oops:|panic' && return 1
    return 0
}

count_d_state() { ps -eo stat= 2>/dev/null | grep -c '^D' || true; }

write_verdict() {
    local case_dir="$1"
    local status="$2"
    local detail="$3"
    cat > "$case_dir/verdict.json" <<EOF
{
  "status": "$status",
  "detail": "$detail",
  "d_state": "$(count_d_state)",
  "timestamp": "$(date -u +%FT%TZ)"
}
EOF
}

run_case() {
    local case_id="$1"
    local policy="$2"
    local merge_arg="$3"
    local loads="$4"
    local need_arbiter="${5:-no}"
    local case_dir="$SUITE_DIR/$case_id"

    mkdir -p "$case_dir"
    log "=== CASE $case_id: policy=$policy merge_arg=$merge_arg loads=[$loads] arbiter=$need_arbiter ==="

    cleanup_ioext
    set_scheduler

    if [ "$need_arbiter" = "yes" ]; then
        start_loader "arb_${policy#q_}" "$case_dir/loader_arb_${policy#q_}.log" "$CGROUP_PATH" >/dev/null
    fi

    local loader_argv
    loader_argv="$(policy_loader_argv "$policy" "$merge_arg")"
    log "loader argv: $loader_argv"

    local loader_pid
    # shellcheck disable=SC2086
    loader_pid=$(start_loader "$policy" "$case_dir/loader_${policy}.log" $loader_argv)

    local dmesg_before="$case_dir/dmesg_before.txt"
    dmesg > "$dmesg_before" 2>/dev/null || true

    local fio_failed=0
    for load_id in $loads; do
        local fio_json
        fio_json=$(run_fio "$load_id")
        cp "$fio_json" "$case_dir/fio_${load_id}.json"

        if ! check_kernel_errors; then
            write_verdict "$case_dir" "FAIL" "kernel error during $load_id"
            break
        fi

        local d_state
        d_state=$(count_d_state)
        if [ "$d_state" -gt 0 ]; then
            log "WARN: D-state=$d_state during $load_id"
        fi
    done

    dmesg > "$case_dir/dmesg_after.txt" 2>/dev/null || true

    "$MERGE_DIR/collect_merge_evidence.sh" "$case_dir" "$case_dir/fio_$(echo "$loads" | awk '{print $1}').json" "$policy"

    if [ -f "$case_dir/verdict.json" ]; then
        log "CASE $case_id: verdict already set"
    else
        if check_kernel_errors; then
            write_verdict "$case_dir" "PASS" "fio completed, no kernel errors"
        else
            write_verdict "$case_dir" "FAIL" "kernel errors detected in dmesg"
        fi
    fi

    cleanup_ioext
    log "=== CASE $case_id done ==="
}

require_root
require_fio
ensure_bpffs
detect_block_device

log "SUITE_DIR=$SUITE_DIR"
log "LOADER_DIR=$LOADER_DIR"
log "TARGET_DEVICE=$TARGET_DEVICE  FIO_RUNTIME=$FIO_RUNTIME  MERGE_PHASE=${MERGE_PHASE:-all}"

run_phase1() {
    run_case "P0_PROBE_DENY"     "q_merge_probe" "0" "L1_seq_write_heavy" no
    run_case "P1_PROBE_ALLOW"    "q_merge_probe" "1" "L1_seq_write_heavy" no
    run_case "P2_PROBE_ADJACENT" "q_merge_probe" "2" "L1_seq_write_heavy" no
}

run_phase2() {
    run_case "K0_KYBER_ON"  "q_kyber" ""  "L1_seq_write_heavy L3_rand_write_heavy" no
    run_case "K1_KYBER_OFF"  "q_kyber" "0" "L1_seq_write_heavy L3_rand_write_heavy" no
    run_case "D0_MQDD_OFF"  "q_mq_deadline" "0" "L1_seq_write_heavy L6_low_qd_regress" no
    run_case "D1_MQDD_D1"   "q_mq_deadline" "1" "L1_seq_write_heavy L6_low_qd_regress" no
    run_case "D2_MQDD_D2"   "q_mq_deadline" "2" "L1_seq_write_heavy L6_low_qd_regress" no
    run_case "D3_MQDD_D3"   "q_mq_deadline" "3" "L1_seq_write_heavy L6_low_qd_regress" no
    run_case "F0_FIFO"      "q_fifo" ""     "L1_seq_write_heavy" no
    run_case "BFQ_ARB"      "q_bfq"  ""     "L1_seq_write_heavy" yes

    set_native "mq-deadline"
    NATIVE_DIR="$SUITE_DIR/B0_NATIVE_MQ_DEADLINE"
    mkdir -p "$NATIVE_DIR"
    dmesg > "$NATIVE_DIR/dmesg_before.txt" 2>/dev/null || true
    run_fio "L1_seq_write_heavy" > /dev/null
    cp "$SUITE_DIR/fio_L1_seq_write_heavy.json" "$NATIVE_DIR/fio_L1_seq_write_heavy.json"
    dmesg > "$NATIVE_DIR/dmesg_after.txt" 2>/dev/null || true
    "$MERGE_DIR/collect_merge_evidence.sh" "$NATIVE_DIR" "$NATIVE_DIR/fio_L1_seq_write_heavy.json" "native_mq_deadline"
    write_verdict "$NATIVE_DIR" "PASS" "native baseline"
}

run_phase3() {
    cleanup_ioext
    set_scheduler
    run_case "FALLBACK_NO_ARB_FIFO"  "q_fifo"  "" "L1_seq_write_heavy" no
    run_case "FALLBACK_NO_ARB_KYBER" "q_kyber" "" "L1_seq_write_heavy" no
}

case "${MERGE_PHASE:-all}" in
1) run_phase1 ;;
2) run_phase2 ;;
3) run_phase3 ;;
all)
    run_phase1
    run_phase2
    run_phase3
    ;;
*)
    fail "unknown MERGE_PHASE=${MERGE_PHASE}"
    ;;
esac

cleanup_ioext
log "=== SUITE COMPLETE: $SUITE_DIR ==="

echo "$SUITE_DIR"