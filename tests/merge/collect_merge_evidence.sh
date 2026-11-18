#!/bin/bash
# collect_merge_evidence.sh - per-case evidence collection (runs inside VM)
# Usage: collect_merge_evidence.sh <case_dir> <fio_json_path> <policy_name>
set -euo pipefail

CASE_DIR="${1:?need case_dir}"
FIO_JSON="${2:?need fio_json}"
POLICY="${3:?need policy_name}"

BASE_DIR="${BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
LOADER_DIR="${LOADER_DIR:-$BASE_DIR/userspace/build}"
TARGET_DEVICE="${TARGET_DEVICE:-nvme0n1}"
SCHEDULER="${SCHEDULER:-scx_io}"

mkdir -p "$CASE_DIR"

dump_env() {
    {
        echo "=== env $(date -u +%FT%TZ) ==="
        echo "nproc=$(nproc)"
        echo "uname=$(uname -r)"
        echo "lsblk:"
        lsblk -o NAME,SIZE,TYPE,MOUNTPOINT 2>/dev/null || true
        echo "scheduler: $(cat /sys/block/$TARGET_DEVICE/queue/scheduler 2>/dev/null || echo unknown)"
        echo "ioext_debug: $(grep -c 'CONFIG_IOSCHED_EXT_DEBUG=y' /proc/config.gz 2>/dev/null || zcat /proc/config.gz 2>/dev/null | grep -c 'CONFIG_IOSCHED_EXT_DEBUG=y' || echo unknown)"
    } > "$CASE_DIR/env.txt"
}

dump_merge_stats() {
    local map_name="$1"
    local stats_json="$CASE_DIR/merge_stats.json"
    local map_path="/sys/fs/bpf/${map_name}"
    local bpftool

    bpftool="${BPFTOOL:-$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool}"
    if [ ! -x "$bpftool" ]; then
        bpftool="$(command -v bpftool || echo bpftool)"
    fi

    if [ ! -e "$map_path" ]; then
        echo "{\"error\":\"map $map_name not pinned\"}" > "$stats_json"
        return 0
    fi

    "$bpftool" map dump pinned "$map_path" 2>/dev/null > "$stats_json" || \
        echo "{\"error\":\"bpftool dump failed\"}" > "$stats_json"
}

check_obs() {
    local obs_status="$CASE_DIR/obs_status.txt"
    local symbols=(scx_io_allow_merge scx_io_bio_merge scx_io_request_merge
                   scx_io_former_request scx_io_request_merged)
    {
        echo "=== bpftrace observability check ==="
        local all_ok=1
        for sym in "${symbols[@]}"; do
            if grep -q " ${sym}$" /proc/kallsyms 2>/dev/null; then
                echo "OK: $sym present in kallsyms"
            else
                echo "MISSING: $sym (likely inlined)"
                all_ok=0
            fi
        done
        if [ "$all_ok" -eq 1 ]; then
            echo "OBS_PROBE_OK"
        else
            echo "OBS_PROBE_UNAVAILABLE"
        fi
        echo "ioext_debug_kernel: $(grep -c 'CONFIG_IOSCHED_EXT_DEBUG=y' /proc/config.gz 2>/dev/null || zcat /proc/config.gz 2>/dev/null | grep -c 'CONFIG_IOSCHED_EXT_DEBUG=y' || echo unknown)"
    } > "$obs_status"
}

run_bpftrace() {
    local bt_log="$CASE_DIR/bpftrace_merge.log"
    local bt_script="$(dirname "${BASH_SOURCE[0]}")/bpftrace_merge.bt"
    local sym

    sym="scx_io_allow_merge"
    if ! grep -q " ${sym}$" /proc/kallsyms 2>/dev/null; then
        echo "SKIP: bpftrace unavailable (symbols inlined)" > "$bt_log"
        return 0
    fi

    if ! command -v bpftrace >/dev/null 2>&1; then
        echo "SKIP: bpftrace not installed" > "$bt_log"
        return 0
    fi

    timeout 30 bpftrace "$bt_script" > "$bt_log" 2>&1 || true
}

dump_dmesg() {
    local tag="$1"
    dmesg > "$CASE_DIR/dmesg_${tag}.txt" 2>/dev/null || true
}

dump_sysfs() {
    {
        echo "=== mq sysfs ==="
        cat /sys/block/$TARGET_DEVICE/queue/scheduler 2>/dev/null || true
        echo "inflight: $(cat /sys/block/$TARGET_DEVICE/inflight 2>/dev/null || echo N/A)"
    } > "$CASE_DIR/mq_sysfs.txt"
}

dump_trace() {
    cat /sys/kernel/debug/tracing/trace 2>/dev/null | tail -200 > "$CASE_DIR/trace_snapshot.txt" || true
}

dump_fio() {
    if [ -f "$FIO_JSON" ]; then
        cp "$FIO_JSON" "$CASE_DIR/fio.json"
    else
        echo '{"error":"fio json missing"}' > "$CASE_DIR/fio.json"
    fi
}

write_manifest() {
    local manifest="$CASE_DIR/manifest.json"
    local commit
    commit="$(cd "$BASE_DIR" && git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    cat > "$manifest" <<EOF
{
  "case_id": "$(basename "$CASE_DIR")",
  "policy": "$POLICY",
  "kernel": "$(uname -r)",
  "commit": "$commit",
  "ioext_debug": "$(grep -c 'CONFIG_IOSCHED_EXT_DEBUG=y' /proc/config.gz 2>/dev/null || zcat /proc/config.gz 2>/dev/null | grep -c 'CONFIG_IOSCHED_EXT_DEBUG=y' || echo unknown)",
  "arbiter_loaded": "$(pgrep -f 'loader_arb_' >/dev/null && echo yes || echo no)",
  "target_device": "$TARGET_DEVICE",
  "nproc": "$(nproc)",
  "timestamp": "$(date -u +%FT%TZ)"
}
EOF
}

dump_env
check_obs
run_bpftrace
dump_sysfs
dump_trace
dump_fio
write_manifest

case "$POLICY" in
    q_merge_probe)
        dump_merge_stats "merge_stats"
        ;;
    q_mq_deadline)
        dump_merge_stats "merge_stats_map"
        ;;
esac

echo "[collect_merge_evidence] done for $POLICY -> $CASE_DIR"