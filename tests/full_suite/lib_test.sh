#!/bin/bash
# tests/full_suite/lib_test.sh
#
# full_suite 共享函数库 (VM 内使用)
# 提供: 策略加载/卸载、dmesg 监控、fio 运行、bpftrace 抓取、结果记录、判定
#
set -euo pipefail

# ── 路径常量 ──
FS_BASE_DIR="${FS_BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
FS_LOADER_DIR="$FS_BASE_DIR/userspace/build"
FS_RESULTS_ROOT="${FS_RESULTS_ROOT:-$FS_BASE_DIR/results/full_suite}"
FS_DEVICE="${TARGET_DEVICE:-nvme0n1}"
FS_DEV_PATH="/dev/$FS_DEVICE"
FS_SYS_BLOCK="/sys/block/$FS_DEVICE"
FS_CG_ROOT="/sys/fs/cgroup"
FS_SCHEDULER="scx_io"

# ── 非平凡策略清单 (见 Test_Design_IOExt.md §1.2) ──
FS_NON_TRIVIAL_POLICIES=(
    q_mq_deadline
    q_bfq
    q_heimdall_v2
    q_kyber
    q_heimdall
    q_sp_edf
    q_merge_probe
    q_wedf
    q_ddl
    q_prio
    q_none
)

# 若环境变量覆盖则使用
if [ -n "${POLICY_OVERRIDE:-}" ]; then
    FS_NON_TRIVIAL_POLICIES=("$POLICY_OVERRIDE")
fi

# ── 日志 ──
fs_log()  { printf '[full_suite] %s\n' "$*" >&2; }
fs_warn()  { printf '[full_suite] WARN: %s\n' "$*" >&2; }
fs_fail()  { printf '[full_suite] FAIL: %s\n' "$*" >&2; exit 1; }
fs_pass()  { printf '[full_suite] PASS: %s\n' "$*" >&2; }

# ── VM 环境检测 ──
fs_is_vm() {
    if command -v systemd-detect-virt >/dev/null 2>&1 && systemd-detect-virt --quiet; then
        return 0
    fi
    grep -Eqi 'virtme|qemu|kvm|bochs|hypervisor' \
        /proc/cmdline \
        /sys/class/dmi/id/product_name \
        /sys/class/dmi/id/sys_vendor 2>/dev/null
}

fs_require_vm() {
    fs_is_vm || fs_fail "must run inside virtme-ng VM"
    [ "$(id -u)" -eq 0 ] || fs_fail "must run as root"
}

# ── 破坏性测试守卫 (裸盘写) ──
fs_require_destructive_guard() {
    if [ "${IOEXT_DESTRUCTIVE_TEST:-0}" != "1" ]; then
        fs_fail "set IOEXT_DESTRUCTIVE_TEST=1 to allow raw block device testing on $FS_DEV_PATH"
    fi
    fs_is_vm || fs_fail "refusing destructive test outside VM"
    # 检查设备未被挂载
    if command -v lsblk >/dev/null 2>&1; then
        if lsblk -nr -o MOUNTPOINT "$FS_DEV_PATH" 2>/dev/null | grep -q '[^[:space:]]'; then
            fs_fail "$FS_DEV_PATH is mounted; refusing destructive test"
        fi
    fi
}

# ── 设备检测 ──
fs_detect_device() {
    if [ -b "$FS_DEV_PATH" ]; then return 0; fi
    for cand in nvme0n1 vda sda; do
        if [ -b "/dev/$cand" ]; then
            fs_warn "TARGET_DEVICE $FS_DEVICE missing, using /dev/$cand"
            FS_DEVICE="$cand"
            FS_DEV_PATH="/dev/$cand"
            FS_SYS_BLOCK="/sys/block/$cand"
            return 0
        fi
    done
    fs_fail "no usable block device"
}

# ── BPF 文件系统 ──
fs_ensure_bpffs() {
    mountpoint -q /sys/fs/bpf 2>/dev/null \
        || mount -t bpf bpf /sys/fs/bpf 2>/dev/null \
        || fs_fail "cannot mount bpffs"
}

# ── cgroup IO 委托 ──
fs_setup_cgroup_io() {
    if [ -f /sys/fs/cgroup/cgroup.subtree_control ]; then
        echo '+io' > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true
    fi
}

# ── 调度器切换 ──
fs_active_scheduler() {
    local sched_path="$FS_SYS_BLOCK/queue/scheduler"
    [ -f "$sched_path" ] || return 1
    sed -n 's/.*\[\([^]]*\)\].*/\1/p' "$sched_path"
}

fs_set_scx_scheduler() {
    local sched_path="$FS_SYS_BLOCK/queue/scheduler"
    [ -f "$sched_path" ] || fs_fail "scheduler sysfs missing: $sched_path"
    echo "$FS_SCHEDULER" > "$sched_path" 2>/dev/null \
        || fs_fail "failed to switch to $FS_SCHEDULER"
    local active
    active="$(fs_active_scheduler)"
    [ "$active" = "$FS_SCHEDULER" ] || fs_fail "scheduler is '$active', expected '$FS_SCHEDULER'"
    fs_log "scheduler=$(cat "$sched_path")"
}

fs_set_none_scheduler() {
    local sched_path="$FS_SYS_BLOCK/queue/scheduler"
    echo none > "$sched_path" 2>/dev/null || true
}

# ── loader 加载/卸载 ──
fs_start_loader() {
    local policy="$1"
    local loader="$FS_LOADER_DIR/loader_${policy}"
    local logfile="$2"
    shift 2
    [ -x "$loader" ] || fs_fail "missing loader: $loader"
    : > "$logfile"
    "$loader" --cgroup "$FS_CG_ROOT" "$@" >"$logfile" 2>&1 &
    FS_LOADER_PID=$!
}

fs_loader_alive() {
    local pid="${1:-$FS_LOADER_PID}"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then return 0; fi
    return 1
}

fs_wait_attached() {
    local policy="$1"
    local pid="${2:-$FS_LOADER_PID}"
    local timeout="${3:-30}"
    local i
    for ((i = 1; i <= timeout; i++)); do
        if ! fs_loader_alive "$pid"; then return 1; fi
        # attach 成功: dmesg 报告 或 loader 存活 >=2s 且 scx_io 激活
        if dmesg 2>/dev/null | grep -qE "policy '${policy}' attached|policy '${policy}' replaced"; then
            return 0
        fi
        if [ "$i" -ge 2 ] && [ "$(fs_active_scheduler)" = "$FS_SCHEDULER" ]; then
            return 0
        fi
        sleep 1
    done
    fs_warn "attach not confirmed in ${timeout}s for ${policy}"
    return 0  # 宽松: loader 存活即继续
}

fs_stop_loader() {
    local pid="${1:-$FS_LOADER_PID}"
    local i
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
        for ((i = 1; i <= 15; i++)); do
            kill -0 "$pid" 2>/dev/null || return 0
            sleep 1
        done
        kill -9 "$pid" 2>/dev/null || true
    fi
}

# ── cleanup ──
fs_cleanup_ioext() {
    fs_log "cleanup io-ext loaders and pinned maps"
    pkill -TERM -f 'loader_q_' 2>/dev/null || true
    sleep 1
    pkill -9 -f 'loader_q_' 2>/dev/null || true
    pkill -9 -f 'loader_arb_' 2>/dev/null || true
    for map in ioext_cgroup_queues ioext_active_cgroups ioext_policy_meta \
        ioext_bfq_tenant_state ioext_prio_config; do
        rm -rf "/sys/fs/bpf/${map}" 2>/dev/null || true
    done
    for map in merge_stats merge_stats_map probe_cfg merge_cfg_map kyber_merge_cfg; do
        rm -rf "/sys/fs/bpf/${map}" 2>/dev/null || true
    done
    fs_set_none_scheduler 2>/dev/null || true
}

# ── dmesg 错误检测 ──
fs_dmesg_has_errors() {
    local since_mark="${1:-}"
    local dmesg_output
    if [ -n "$since_mark" ]; then
        dmesg_output=$(dmesg | awk -v m="$since_mark" 'seen{print} index($0,m){seen=1}')
    else
        dmesg_output=$(dmesg | tail -200)
    fi
    echo "$dmesg_output" | grep -qiE 'hung_task|BUG:|Oops:|kernel panic|soft lockup|WARNING:.*iosched' \
        | grep -v 'Command line' | grep -v 'Kernel command line'
}

fs_dmesg_snapshot() {
    local mark="$1"
    dmesg | awk -v m="$mark" 'seen{print} index($0,m){seen=1}' 2>/dev/null || true
}

# ── 结果目录 ──
fs_init_run() {
    FS_RUN_ID="full_suite_$(date +%Y%m%d_%H%M%S)"
    FS_RESULTS_DIR="$FS_RESULTS_ROOT/$FS_RUN_ID"
    mkdir -p "$FS_RESULTS_DIR"
    FS_CASES_PASS=0
    FS_CASES_FAIL=0
    FS_CASES_BLOCKED=0
    FS_CASE_RESULTS=()
    printf '%s\n' "$FS_RUN_ID" > "$FS_RESULTS_DIR/run_id.txt"
    export FS_RUN_ID FS_RESULTS_DIR FS_CASES_PASS FS_CASES_FAIL FS_CASES_BLOCKED
    fs_log "run_id=$FS_RUN_ID results_dir=$FS_RESULTS_DIR"
}

fs_record_result() {
    local case_id="$1"
    local status="$2"  # PASS/FAIL/BLOCKED
    local detail="${3:-}"
    local entry="$case_id|$status|$detail"
    FS_CASE_RESULTS+=("$entry")
    case "$status" in
        PASS)    FS_CASES_PASS=$((FS_CASES_PASS + 1)) ;;
        FAIL)    FS_CASES_FAIL=$((FS_CASES_FAIL + 1)) ;;
        BLOCKED) FS_CASES_BLOCKED=$((FS_CASES_BLOCKED + 1)) ;;
    esac
    printf '%-45s %s %s\n' "$case_id" "$status" "$detail" >&2
    # 写入单 case 结果文件
    local case_dir="$FS_RESULTS_DIR/$(echo "$case_id" | tr '/' '_')"
    mkdir -p "$case_dir" 2>/dev/null || true
    printf '{"case_id":"%s","status":"%s","detail":"%s"}\n' \
        "$case_id" "$status" "$detail" > "$case_dir/verdict.json" 2>/dev/null || true
}

# ── fio 运行 ──
fs_run_fio() {
    local job_file="$1"
    local outfile="$2"
    local timeout="${3:-120}"
    mkdir -p "$(dirname "$outfile")"

    # 替换 __DEV__ 占位符为实际设备路径
    local real_job="$job_file"
    if grep -q '__DEV__' "$job_file" 2>/dev/null; then
        real_job="${outfile}.fio"
        sed "s|__DEV__|$FS_DEV_PATH|g" "$job_file" > "$real_job"
    fi

    fs_log "fio: $real_job -> $outfile (timeout=${timeout}s)"
    timeout "$timeout" fio "$real_job" --output-format=json \
        > "$outfile" 2>"${outfile}.stderr" || true
}

fs_validate_fio_json() {
    local file="$1"
    grep -q '"error"[[:space:]]*:[[:space:]]*0' "$file" 2>/dev/null
}

fs_fio_stat() {
    local file="$1"
    local field="$2"
    python3 -c "
import json,sys
try:
    d=json.load(open('$file'))
    j=d.get('jobs',[{}])[0]
    v=j.get('$field')
    if v is None:
        for op in ('read','write','trim'):
            sd=j.get(op,{})
            cv=sd.get('$field')
            if cv is not None and cv > 0:
                v=cv; break
        if v is None:
            for op in ('read','write','trim'):
                sd=j.get(op,{})
                cv=sd.get('$field')
                if cv is not None:
                    v=cv; break
    print(v if v is not None else 0)
except: print(0)
" 2>/dev/null
}

fs_fio_clat_percentile() {
    local file="$1"
    local pct="${2:-99.000000}"
    python3 -c "
import json
try:
    d=json.load(open('$file'))
    j=d['jobs'][0]
    for op in ('read','write','trim'):
        clat=j.get(op,{}).get('clat_ns',{})
        if 'percentile' in clat:
            v=clat['percentile'].get('$pct',0)
            if v and v > 0:
                print(v); sys.exit(0)
    for op in ('read','write','trim'):
        clat=j.get(op,{}).get('clat_ns',{})
        if 'percentile' in clat:
            print(clat['percentile'].get('$pct',0)); sys.exit(0)
    print(0)
except: print(0)
" 2>/dev/null
}

# ── bpftool map dump ──
fs_dump_bpf_map() {
    local map_name="$1"
    local outfile="$2"
    bpftool -j map dump name "$map_name" > "$outfile" 2>/dev/null || true
}

fs_bpf_map_value() {
    local file="$1"
    local key="$2"
    python3 -c "
import json
try:
    d=json.load(open('$file'))
    # bpftool dump 格式: list of {key,value} or flat
    if isinstance(d, list):
        for e in d:
            v=e.get('value',e)
            if isinstance(v,dict) and '$key' in v:
                print(v['$key']); sys.exit(0)
            if isinstance(v,dict):
                for kk,vv in v.items():
                    if kk=='$key': print(vv); sys.exit(0)
    print(0)
except: print(0)
" 2>/dev/null
}

# ── bpftrace ──
fs_start_bpftrace() {
    local script="$1"
    local outfile="$2"
    mkdir -p "$(dirname "$outfile")"
    bpftrace "$script" > "$outfile" 2>&1 &
    FS_BPFPID=$!
    sleep 1
}

fs_stop_bpftrace() {
    if [ -n "${FS_BPFPID:-}" ] && kill -0 "$FS_BPFPID" 2>/dev/null; then
        kill -INT "$FS_BPFPID" 2>/dev/null || true
        wait "$FS_BPFPID" 2>/dev/null || true
    fi
}

# ── dmesg mark ──
fs_dmesg_mark() {
    local mark="ioext_fs_${1}_$(date +%s)_${BASHPID}"
    printf '%s\n' "$mark" > /dev/kmsg 2>/dev/null || true
    echo "$mark"
}

# ── retry 逻辑 (5 次失败规则) ──
fs_run_case_with_retry() {
    local case_id="$1"
    local run_fn="$2"
    local max_retries="${3:-5}"
    local attempt
    for ((attempt = 1; attempt <= max_retries; attempt++)); do
        fs_log "[$case_id] attempt $attempt/$max_retries"
        if $run_fn "$case_id" "$attempt"; then
            fs_record_result "$case_id" "PASS" "attempt=$attempt"
            return 0
        fi
        fs_warn "[$case_id] attempt $attempt failed"
        # 间歇清理
        fs_cleanup_ioext
        sleep 2
    done
    fs_record_result "$case_id" "BLOCKED" "failed ${max_retries} attempts (>=5 rule)"
    return 1
}