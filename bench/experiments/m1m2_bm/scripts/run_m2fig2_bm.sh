#!/usr/bin/env bash
# run_m2fig2_bm.sh —— 控制机编排：在裸机 bm 上跑 M2'（m2_fig2 复刻，9 臂）实验。
#
# 复刻 scripts/run_m2_fig2_virtme.sh（kvmhost）的流程，搬到裸机 bm：
#   标定已完成（LEDGER §5：solo 3305 MiB/s → rate=1536，勿重跑；探针文件
#   /opt/ioext/m2fig2/probe/solo_data 保留、绝不触碰）；
#   yamls 由 gen_m2_fig2_yamls_bm.py 预生成（experiments/m1m2_bm/configs/m2fig2/）。
#
# 每次调用的流程：
#   [1] rsync yamls + calibration + validate 脚本 → bm 部署根（写入
#       src/vm/configs/task/isol_bench/rig_m2_fig2_bm/：ConfigParser 的 base_scene
#       解析要求配置文件路径含 "src/vm/configs"）
#   [2] 远端 preflight（远端 flock 互斥下：只读环境检查 + 子 cgroup 旋钮可用性
#       探测 + io.max 限速 smoke）+ 一次性预分配并填满 32G fio 目标文件
#       （direct=1 用；marker data/.m2f2_data_filled；绝不碰 probe/solo_data）
#   [3] 逐 sequence：远端 flock 互斥 → reset（pkill fio / scheduler=none / 清
#       root io.cost qos / 删 ib_m2f2_* 子 cgroup）→ vm.executor（per-seq yaml）
#       → 远端 validate_isol_bench_result.py → tar+scp 回传本机
#       runs/m2fig2/<RUN_ID>/<SEQ>/（保持 plot_m2_fig2.py 兼容的
#       step_0/ib_m2f2_* 布局，原样回传不改结构）
#   [4] 本机 per-arm validate（validate_m2fig2_arm_bm.py，判据对照
#       docs/plans/m2_fig2_replication_experiment.md）；PASS/SKIP 完整性门。
#
# 纪律（对照任务书与 LEDGER）：
#   - sudo 走 bm 的 NOPASSWD 通道（LEDGER 决策 5：/etc/sudoers.d/90-ioext-nopasswd），
#     远端一律 `sudo -n`；SUDO_PASS/remote_sudo 不进入本实验路径；preflight 校验
#     `sudo -n true`，失效即硬退出。
#   - io.max 写在子 cgroup（bm 语义：io.* 控制文件仅出现在子 cgroup）；设备
#     MAJ:MIN 运行时动态获取（/sys/block/nvme0n1/dev；bm 的 coreutils stat 不支持
#     %H:%L），绝不硬编码 8:0。io.max smoke 实证 "MAJ:MIN 限速"对根分区上的
#     direct 文件读生效后才开跑。
#   - io.latency 等旋钮可用性先探测（LEDGER：io.latency 需在子 cgroup 实测），
#     缺失的臂显式 SKIP(UNAVAILABLE) 记账，绝不静默降级跑出误导数据。
#   - 所有远端命令均带超时（客户端 timeout + 远端 timeout + flock -w）。
#   - bm 是专用实验机：reset 的 `pkill -9 -x fio` 与 kvmhost 口径一致。
#   - --dry-run：只打印全部将执行的 ssh/executor/回传命令与输出路径（远端脚本
#     全文），绝不连 bm、绝不写任何文件、绝不启动实验。
#
# 用法：
#   bash experiments/m1m2_bm/scripts/run_m2fig2_bm.sh [--dry-run] [--skip-push] \
#        [--all | SEQ ...]        # 默认按 kvmhost 顺序全部 9 臂
# 环境变量：
#   RUN_ID=…          本轮 ID（默认 m86f2_<UTC 时间戳>）；产物落 runs/m2fig2/$RUN_ID/
#   MAX_RETRIES=2     每 sequence 重试上限（kvmhost 口径）
#   FORCE_RERUN=1     忽略本机缓存（已有且 validate 通过的 sequence 直接跳过）
#   FILL_REFRESH=1    强制重填 32G 数据文件（默认 marker 存在即跳过）
#   LOCK_WAIT=7200     远端 flock 等待上限秒（与其他 campaign 互斥）
#   PREFLIGHT_SMOKE=1 io.max 限速 smoke（默认开；失败=中止全部，防无效数据）
#   EXEC_TIMEOUT=480  单次 executor 远端超时秒（110s 工作负载 + 开销）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$EXP_DIR/../.." && pwd)"
# shellcheck source=ssh_env_bm.sh
source "$SCRIPT_DIR/ssh_env_bm.sh"

# ---- 常量与路径 --------------------------------------------------------------
DEPLOY_ROOT="/opt/ioext"
REMOTE_M2="$DEPLOY_ROOT/m2fig2"
REMOTE_DATA_DIR="$REMOTE_M2/data"
REMOTE_DATA_FILE="$REMOTE_M2/data/m2f2_data.bin"   # 预分配 32G，direct=1 目标
REMOTE_CFG_DIR="$DEPLOY_ROOT/src/vm/configs/task/isol_bench/rig_m2_fig2_bm"
REMOTE_VALIDATE="$DEPLOY_ROOT/scripts/validate_isol_bench_result.py"
LOCAL_CFG_DIR="${CFG_DIR:-$EXP_DIR/configs/m2fig2}"   # CFG_DIR=<dir> 可选覆盖（如 m2fig2_rate2644）
CALIB_JSON="$LOCAL_CFG_DIR/calibration_bm.json"
ARM_VALIDATOR="$SCRIPT_DIR/validate_m2fig2_arm_bm.py"
GEN_SCRIPT="$SCRIPT_DIR/gen_m2_fig2_yamls_bm.py"
DATA_SIZE_GB=32
TARGET_DEVICE=nvme0n1
LOCK_FILE="/tmp/m2fig2_bm.lock"
RUN_ID="${RUN_ID:-m86f2_$(date -u +%Y%m%dT%H%M%SZ)}"
MAX_RETRIES="${MAX_RETRIES:-2}"
FORCE_RERUN="${FORCE_RERUN:-0}"
FILL_REFRESH="${FILL_REFRESH:-0}"
LOCK_WAIT="${LOCK_WAIT:-7200}"
PREFLIGHT_SMOKE="${PREFLIGHT_SMOKE:-1}"
EXEC_TIMEOUT="${EXEC_TIMEOUT:-480}"
REMOTE_OUT_BASE="/tmp/m2fig2_bm/$RUN_ID"
LOCAL_RUNS="$EXP_DIR/runs/m2fig2/$RUN_ID"
LOCAL_LOG="$EXP_DIR/runs/m2fig2/${RUN_ID}.run.log"
SSH_PUSH_TIMEOUT=180        # rsync/scp 客户端超时
SMOKE_CAP_MB=64             # io.max smoke 限速值（MiB/s）
SMOKE_RBPS=$((SMOKE_CAP_MB * 1024 * 1024))
target="$SSH_USER@$SSH_HOST"

# kvmhost 生成器的 SEQUENCE_ORDER（io.cost 臂在 io.latency 前，io.latency 收尾）
ALL_SEQS="M2F2_a_none M2F2_b_mqdl_prio M2F2_c_bfq M2F2_d_bfq_weight M2F2_e_io_max M2F2_g_io_cost M2F2_h_io_cost_weight M2F2_i_native_kyber M2F2_f_io_latency"
declare -A ARM_SCHED=(
    [M2F2_a_none]=none [M2F2_b_mqdl_prio]=mq-deadline [M2F2_c_bfq]=bfq [M2F2_d_bfq_weight]=bfq
    [M2F2_e_io_max]=none [M2F2_f_io_latency]=none [M2F2_g_io_cost]=none
    [M2F2_h_io_cost_weight]=none [M2F2_i_native_kyber]=kyber)
declare -A ARM_KNOB=(
    [M2F2_b_mqdl_prio]=io.prio.class [M2F2_d_bfq_weight]=io.bfq.weight
    [M2F2_e_io_max]=io.max [M2F2_f_io_latency]=io.latency
    [M2F2_g_io_cost]=root:io.cost.model [M2F2_h_io_cost_weight]=root:io.cost.model)

DRY_RUN=0 SKIP_PUSH=0

log()  { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die()  { echo "BLOCKED: $*" >&2; exit 2; }

# 带客户端超时的底层执行器：`timeout` 不能直接执行函数（ssh_cmd/scp_cmd/rsync_cmd），
# 故用 ssh_env_bm.sh 已 source 的 SSH_OPTS/RSYNC_SSH_OPTS 组装原始命令再包 timeout。
ssh_exec()   { timeout "$1" ssh -n ${SSH_OPTS[@]} ${SSH_PORT_OPTS[@]+"${SSH_PORT_OPTS[@]}"} "$SSH_USER@$SSH_HOST" "${@:2}"; }
scp_exec()   { timeout "$1" scp ${SSH_OPTS[@]} ${SSH_PORT_OPTS[@]+"${SSH_PORT_OPTS[@]}"} "${@:2}"; }
rsync_exec() { timeout "$1" rsync -avz -e "$RSYNC_SSH_OPTS" "${@:2}"; }

# ---- 参数解析 ----------------------------------------------------------------
args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   DRY_RUN=1 ;;
        --skip-push) SKIP_PUSH=1 ;;
        --all)       args+=($ALL_SEQS) ;;
        *)           args+=("$1") ;;
    esac
    shift
done
if [ "${#args[@]}" -eq 0 ]; then
    WANTED="$ALL_SEQS"
else
    for seq in "${args[@]}"; do
        echo "$ALL_SEQS" | tr ' ' '\n' | grep -qx "$seq" || die "unknown sequence: $seq (valid: $ALL_SEQS)"
    done
    WANTED="$(printf '%s\n' "${args[@]}" | sort -u | tr '\n' ' ')"
    ordered=""
    for seq in $ALL_SEQS; do          # 恢复 kvmhost 执行顺序
        if echo "$WANTED" | tr ' ' '\n' | grep -qx "$seq"; then ordered="$ordered $seq"; fi
    done
    WANTED="${ordered# }"
fi
[ -n "$WANTED" ] || die "no sequences requested"

# ---- 执行包装（dry-run 只打印，绝不执行）--------------------------------------
# run_remote <client_timeout_s> <use_flock:0|1> ；远端脚本经 stdin（本地变量已在
# heredoc 展开为字面量），base64 包装防引号丢失，远端 flock 互斥 + timeout。
# dry-run 模式输出走 stderr（stdout 保持干净供 $(...) 捕获）。
run_remote() {
    local ctimeout="$1" use_lock="$2"
    local b64 lock_part cmd
    b64="$(cat | base64 -w0)"
    if [ "$use_lock" = 1 ]; then lock_part="flock -w $LOCK_WAIT $LOCK_FILE"; else lock_part=""; fi
    cmd="echo ${b64} | base64 -d | $lock_part timeout $ctimeout bash"
    if [ "$DRY_RUN" = 1 ]; then
        echo "[dry-run][remote $SSH_USER@$SSH_HOST] { ${b64:0:24}… | base64 -d | $lock_part timeout $ctimeout bash }  # 脚本体（本机变量已展开）：" >&2
        echo "${b64}" | base64 -d | sed 's/^/    | /' >&2
        return 0
    fi
    # 客户端总界 = flock 等待(LOCK_WAIT) + 远端脚本运行(ctimeout) + 60s 余量
    ssh_exec "$((ctimeout + LOCK_WAIT + 60))" "$cmd"
}
run_local() {  # run_local <desc> <cmd...>
    local desc="$1"; shift
    if [ "$DRY_RUN" = 1 ]; then
        echo "[dry-run][local] $desc: $*" >&2
    else
        log ">> $desc"
        "$@"
    fi
}
push_files() {  # push_files <rsync-args...>
    if [ "$DRY_RUN" = 1 ]; then
        echo "[dry-run][rsync→bm] rsync -avz -e ssh $*" >&2
        return 0
    fi
    rsync_exec "$SSH_PUSH_TIMEOUT" "$@"
}
pull_files() {  # pull_files <timeout_s> <scp-args...>
    local tmo="$1"; shift
    if [ "$DRY_RUN" = 1 ]; then
        echo "[dry-run][scp←bm] timeout $tmo scp $*" >&2
        return 0
    fi
    scp_exec "$tmo" "$@"
}
ssh_step() {  # ssh_step <timeout_s> <cmd...>：短 ssh（无 stdin 脚本）
    local tmo="$1"; shift
    if [ "$DRY_RUN" = 1 ]; then
        echo "[dry-run][ssh $SSH_USER@$SSH_HOST] timeout $tmo ssh … $*" >&2
        return 0
    fi
    ssh_exec "$tmo" "$*"
}

# ---- [0] 本机前置：yamls / calibration / 脚本齐备 ---------------------------
for f in "$CALIB_JSON" "$ARM_VALIDATOR" "$GEN_SCRIPT"; do
    [ -f "$f" ] || die "本机缺文件: $f"
done
N_YAMLS="$(find "$LOCAL_CFG_DIR" -maxdepth 1 -name 'm2_fig2_bm_*.yml' 2>/dev/null | wc -l)"
if [ "$N_YAMLS" != 10 ]; then
    run_local "yamls 缺失/过期，重新生成" python3 "$GEN_SCRIPT"
    N_YAMLS="$(find "$LOCAL_CFG_DIR" -maxdepth 1 -name 'm2_fig2_bm_*.yml' | wc -l)"
fi
[ "$N_YAMLS" = 10 ] || die "yaml 数量异常: $N_YAMLS（应为 10 = 9 per-seq + 1 all）"

if [ "$DRY_RUN" = 1 ]; then
    log "=== DRY-RUN：只打印将执行的命令与路径；绝不连 bm、不写文件、不启动实验 ==="
else
    mkdir -p "$LOCAL_RUNS"
    exec > >(tee -a "$LOCAL_LOG") 2>&1
    cp -f "$CALIB_JSON" "$LOCAL_RUNS/calibration.json"
fi
log "RUN_ID=$RUN_ID  sequences=[$WANTED]  retries=$MAX_RETRIES"
log "远端输出: $REMOTE_OUT_BASE/<SEQ>/  →  本机: $LOCAL_RUNS/<SEQ>/（plot_m2_fig2.py 兼容布局）"

# ---- [1] 推送 yamls + calibration + validate 脚本到 bm ----------------------
if [ "$SKIP_PUSH" = 1 ]; then
    log "[1] 推送跳过（--skip-push）"
else
    log "[1] 推送 yamls(10) + calibration_bm.json + validate_isol_bench_result.py → bm"
    ssh_step 30 "mkdir -p '$REMOTE_CFG_DIR' '$REMOTE_M2' '$DEPLOY_ROOT/scripts'"
    push_files "$LOCAL_CFG_DIR/" "$target:$REMOTE_CFG_DIR/" \
        || die "yamls 推送失败（rsync $LOCAL_CFG_DIR/ → $REMOTE_CFG_DIR/）"
    push_files "$CALIB_JSON" "$target:$REMOTE_M2/calibration_bm.json" \
        || die "calibration 推送失败"
    push_files "$REPO_ROOT/scripts/validate_isol_bench_result.py" "$target:$REMOTE_VALIDATE" \
        || die "validate 脚本推送失败"
fi

# ---- [2] 远端 preflight + 一次性数据文件预分配（结构化输出行）----------------
log "[2] 远端 preflight（flock + 只读检查 + 子 cgroup 旋钮探测 + io.max smoke）+ ${DATA_SIZE_GB}G 数据文件预分配"
PREFLIGHT_OUT="$(run_remote 900 1 <<REMOTE_EOF || true
set -euo pipefail
R_DATA_DIR="$REMOTE_DATA_DIR"
R_DATA="$REMOTE_DATA_FILE"
echo "RESULT host \$(hostname)"
echo "RESULT kernel \$(uname -r)"
echo "RESULT fio \$(fio --version 2>/dev/null || echo MISSING)"
echo "RESULT dev_majmin \$(cat /sys/block/$TARGET_DEVICE/dev 2>/dev/null || echo MISSING)"
echo "RESULT schedulers \$(cat /sys/block/$TARGET_DEVICE/queue/scheduler 2>/dev/null)"
echo "RESULT cgroup2 \$(stat -fc %T /sys/fs/cgroup)"
echo "RESULT free_home_gib \$(df -BG /home/anon | tail -1 | awk '{print \$4}' | tr -dc '0-9')"
echo "RESULT venv_import_vm \$(cd $DEPLOY_ROOT/src && $DEPLOY_ROOT/.venv/bin/python -c 'import vm; print("ok")' 2>&1 | tail -1)"
if [ -f "$REMOTE_M2/probe/solo_data" ]; then echo "RESULT solo_data PRESENT"; else echo "RESULT solo_data MISSING"; fi
if sudo -n true 2>/dev/null; then echo "RESULT sudo_nopasswd yes"; else echo "RESULT sudo_nopasswd no"; fi
MAJMIN="\$(cat /sys/block/$TARGET_DEVICE/dev)"

# --- 一次性预分配并填满 ${DATA_SIZE_GB}G 数据文件（direct=1 目标；绝不碰 probe/solo_data）
if [ ! -f "\$R_DATA_DIR/.m2f2_data_filled" ] || [ "$FILL_REFRESH" = "1" ]; then
    mkdir -p "\$R_DATA_DIR"
    rm -f "\$R_DATA" "\$R_DATA_DIR/.m2f2_data_filled"
    echo "FILL start ${DATA_SIZE_GB}G -> \$R_DATA"
    timeout 600 fio --name=m2f2_fill --filename="\$R_DATA" --ioengine=io_uring \\
        --direct=1 --bs=1M --rw=write --iodepth=16 --size=${DATA_SIZE_GB}G \\
        --output-format=json --output=/tmp/m2f2_fill.json >/dev/null
    sync
    sz=\$(stat -c %s "\$R_DATA")
    expect=\$(( ${DATA_SIZE_GB} * 1024 * 1024 * 1024 ))
    [ "\$sz" = "\$expect" ] || { echo "FILL size-mismatch \$sz != \$expect"; exit 3; }
    touch "\$R_DATA_DIR/.m2f2_data_filled"
    echo "FILL done \$sz bytes"
else
    echo "FILL cached \$(stat -c %s "\$R_DATA" 2>/dev/null || echo MISSING)"
fi

# --- 子 cgroup 旋钮可用性探测（scratch cgroup 建立即删）----------------------
sudo -n mkdir -p /sys/fs/cgroup/m2f2_probe
for knob in io.max io.latency io.prio.class io.bfq.weight io.weight; do
    if sudo -n test -f "/sys/fs/cgroup/m2f2_probe/\$knob"; then
        echo "KNOB \$knob PRESENT"
    else
        echo "KNOB \$knob MISSING"
    fi
done
sudo -n rmdir /sys/fs/cgroup/m2f2_probe 2>/dev/null || true
if [ -f /sys/fs/cgroup/io.cost.model ]; then echo "KNOB root:io.cost.model PRESENT"; else echo "KNOB root:io.cost.model MISSING"; fi

# --- io.max smoke：子 cgroup 写 "MAJ:MIN rbps=${SMOKE_CAP_MB}MiB/s"，读数据文件，
#     实证 259:0 限速对根分区（nvme0n1p2）上的 direct 文件读生效（bm 语义）---
if [ "$PREFLIGHT_SMOKE" = "1" ]; then
    SMK=/sys/fs/cgroup/m2f2_smoke
    sudo -n mkdir -p \$SMK
    printf '%s rbps=${SMOKE_RBPS} wbps=max riops=max wiops=max\\n' "\$MAJMIN" \\
        | sudo -n tee \$SMK/io.max >/dev/null
    sudo -n env R_DATA="\$R_DATA" bash -c '
        printf "%s\\n" "\$\$" > /sys/fs/cgroup/m2f2_smoke/cgroup.procs
        exec timeout 30 fio --name=smoke --filename="\$R_DATA" --ioengine=io_uring \\
            --direct=1 --bs=64k --rw=randread --iodepth=8 --size=512M \\
            --runtime=5 --time_based=1 --readonly \\
            --output-format=json --output=/tmp/m2f2_smoke.json' >/dev/null 2>&1 || true
    BW_BYTES=\$($DEPLOY_ROOT/.venv/bin/python -c "import json;d=json.load(open('/tmp/m2f2_smoke.json'));print(d['jobs'][0]['read']['bw_bytes'])" 2>/dev/null || echo 0)
    sudo -n rmdir \$SMK 2>/dev/null || true
    echo "SMOKE observed_bytes \$BW_BYTES"
fi
echo "PREFLIGHT_DONE"
REMOTE_EOF
)"
if [ "$DRY_RUN" = 1 ]; then
    PREFLIGHT_OUT=""
    MAJMIN="(dry-run: 运行时动态获取)"
    log "[2] dry-run：preflight/探测值不解析（上面已打印将执行的完整远端脚本）"
else
    echo "$PREFLIGHT_OUT" | grep -q '^PREFLIGHT_DONE$' \
        || die "preflight 未完成（缺 PREFLIGHT_DONE；远端输出见上）"
    echo "$PREFLIGHT_OUT"
    kv()   { echo "$PREFLIGHT_OUT" | awk -v k="$1" '$1=="RESULT" && $2==k { $1=""; $2=""; sub(/^[ \t]+/,""); print; exit }'; }
    knob() { echo "$PREFLIGHT_OUT" | awk -v k="$1" '$1=="KNOB" && $2==k {print $3; exit}'; }
    sched_ok() { echo "$(kv schedulers)" | tr '][ ' '\n\n\n' | grep -qx "$1"; }

    # ---- preflight 判定（fail-visible）----
    [ "$(kv host)" = "user" ] || die "bm 主机身份异常: $(kv host)"
    case "$(kv kernel)" in 7.3.1-ioext*) log "kernel: $(kv kernel)" ;; *) die "内核非 7.3.1-ioext*: $(kv kernel)" ;; esac
    [ "$(kv fio)" != "MISSING" ]   || die "bm 缺 fio"
    MAJMIN="$(kv dev_majmin)"
    echo "$MAJMIN" | grep -qE '^[0-9]+:[0-9]+$' || die "nvme0n1 MAJ:MIN 动态获取失败: $MAJMIN"
    [ "$(kv sudo_nopasswd)" = "yes" ] || die "sudo NOPASSWD 通道失效（LEDGER 决策 5）"
    [ "$(kv cgroup2)" = "cgroup2fs" ] || die "/sys/fs/cgroup 非 cgroup2fs"
    [ "$(kv venv_import_vm)" = "ok" ] || die "venv import vm 失败: $(kv venv_import_vm)"
    FREE_GIB="$(kv free_home_gib)"
    [ "$FREE_GIB" -ge 40 ] 2>/dev/null || die "home 剩余 ${FREE_GIB}G < 40G，放不下 ${DATA_SIZE_GB}G 数据文件"
    [ "$(kv solo_data)" = "PRESENT" ] || echo "WARN: probe/solo_data 不在（标定证据文件，请核查）" >&2
    log "preflight OK: kernel=$(kv kernel) dev=$MAJMIN free=${FREE_GIB}G fio=$(kv fio) schedulers=[$(kv schedulers)]"
    if echo "$PREFLIGHT_OUT" | grep -q '^FILL start'; then
        log "数据文件已填充: $(echo "$PREFLIGHT_OUT" | awk '/^FILL done/{print $3" "$4}')"
    else
        log "数据文件缓存命中: $(echo "$PREFLIGHT_OUT" | awk '/^FILL cached/{print $3" "$4}')"
    fi

    if [ "$PREFLIGHT_SMOKE" = "1" ]; then
        SMOKE_BYTES="$(echo "$PREFLIGHT_OUT" | awk '/^SMOKE observed_bytes/{print $3}')"
        if python3 -c "
import sys
b = ${SMOKE_BYTES:-0}
cap = $SMOKE_CAP_MB * 1048576
sys.exit(0 if cap * 0.25 <= b <= cap * 1.5 else 1)"; then
            log "io.max smoke PASS: observed=$(( SMOKE_BYTES / 1048576 )) MiB/s vs cap=${SMOKE_CAP_MB} MiB/s（子 cgroup + $MAJMIN 限速对根分区 direct 读生效）"
        else
            die "io.max smoke 失败: observed_bytes=${SMOKE_BYTES} 期望≈${SMOKE_CAP_MB} MiB/s —— $MAJMIN 限速未作用于根分区文件读，e 臂数据口径作废；人工核查后可 PREFLIGHT_SMOKE=0 重试"
        fi
    fi
fi

# ---- [3] 逐 sequence 执行 ------------------------------------------------------
if [ "$DRY_RUN" != 1 ]; then mkdir -p "$LOCAL_RUNS"; fi
PASS=0; SKIP=0
declare -A SKIP_REASON
for SEQ in $WANTED; do
    SEQ_CFG="$REMOTE_CFG_DIR/m2_fig2_bm_${SEQ}.yml"
    R_OUT="$REMOTE_OUT_BASE/$SEQ"
    L_OUT="$LOCAL_RUNS/$SEQ"
    echo ""
    echo "=== [$SEQ] scheduler=${ARM_SCHED[$SEQ]} knob=${ARM_KNOB[$SEQ]:-none} ==="

    # 可用性 gate（preflight 探测；dry-run 无探测值，跳过判定）
    if [ -n "${PREFLIGHT_OUT:-}" ]; then
        avail_ok=1
        if ! sched_ok "${ARM_SCHED[$SEQ]}"; then
            SKIP_REASON[$SEQ]="scheduler ${ARM_SCHED[$SEQ]} 不在 $TARGET_DEVICE 队列"; avail_ok=0
        fi
        knob_req="${ARM_KNOB[$SEQ]:-}"
        if [ "$avail_ok" = 1 ] && [ -n "$knob_req" ] && [ "$(knob "$knob_req")" != "PRESENT" ]; then
            SKIP_REASON[$SEQ]="子 cgroup 旋钮 $knob_req 缺失（内核能力面，LEDGER 待决）"; avail_ok=0
        fi
        if [ "$avail_ok" = 0 ]; then
            echo ">>> [$SEQ] SKIP(UNAVAILABLE: ${SKIP_REASON[$SEQ]})"
            echo "$SEQ" >> "$LOCAL_RUNS/_skip.log"; SKIP=$((SKIP+1)); continue
        fi
    fi

    # 本机缓存跳过（同 RUN_ID 续跑；dry-run 不看缓存，保证打印全部命令）
    if [ "$DRY_RUN" != 1 ] && [ "$FORCE_RERUN" != 1 ] && [ -f "$L_OUT/metadata.json" ] && \
       python3 "$ARM_VALIDATOR" --results "$LOCAL_RUNS" --sequence "$SEQ" \
            --calibration "$LOCAL_RUNS/calibration.json" >/dev/null 2>&1; then
        echo ">>> [$SEQ] PASS (cached valid result)"
        echo "$SEQ" >> "$LOCAL_RUNS/_pass.log"; PASS=$((PASS+1)); continue
    fi
    [ "$DRY_RUN" != 1 ] && rm -rf "$L_OUT" "$L_OUT.tar.gz" 2>/dev/null || true

    arm_passed=0
    ATTEMPTS="$MAX_RETRIES"
    if [ "$DRY_RUN" = 1 ]; then ATTEMPTS=1; fi
    for attempt in $(seq 1 "$ATTEMPTS"); do
        [ "$DRY_RUN" = 1 ] || echo "--- attempt $attempt/$MAX_RETRIES ---"
        RUN_OUT="$(run_remote "$EXEC_TIMEOUT" 1 <<REMOTE_EOF || echo RUN_REMOTE_FAILED
set -uo pipefail
SEQ="$SEQ"
SEQ_CFG="$SEQ_CFG"
R_OUT="$R_OUT"
LOG="$REMOTE_OUT_BASE/logs/\${SEQ}.a${attempt}.log"
mkdir -p "\$R_OUT" "$REMOTE_OUT_BASE/logs"

# reset（kvmhost 口径）：清 fio 残留 / scheduler 回 none / 清 root io.cost qos /
# 删 ib_m2f2_* 与 m2f2_* scratch 子 cgroup（MAJ:MIN 每次动态取，不硬编码）
sudo -n pkill -9 -x fio 2>/dev/null || true
sudo -n sh -c "echo none > /sys/block/$TARGET_DEVICE/queue/scheduler" 2>/dev/null || true
if [ -f /sys/fs/cgroup/io.cost.qos ]; then
    MAJMIN_DYN="\$(cat /sys/block/$TARGET_DEVICE/dev)"
    echo "\$MAJMIN_DYN enable=0" | sudo -n tee /sys/fs/cgroup/io.cost.qos >/dev/null 2>&1 || true
fi
sudo -n rm -rf /sys/fs/cgroup/ib_m2f2_* /sys/fs/cgroup/m2f2_probe /sys/fs/cgroup/m2f2_smoke 2>/dev/null || true
sleep 2

rm -rf "\$R_OUT" "\$R_OUT.tar.gz"; mkdir -p "\$R_OUT"
cd $DEPLOY_ROOT/src
IOEXT_DESTRUCTIVE_TEST=1 timeout $EXEC_TIMEOUT $DEPLOY_ROOT/.venv/bin/python -m vm.executor \\
    --config "\$SEQ_CFG" --sequence "\$SEQ" --output "\$R_OUT" \\
    --log-level INFO --log-file "\$LOG" --become-password '' 2>&1 | tail -200
rc=\${PIPESTATUS[0]}
if [ "\$rc" != 0 ]; then
    echo "EXECUTOR_RC \$rc"
    tail -30 "\$LOG" 2>/dev/null || true
    exit 1
fi
cd $DEPLOY_ROOT
if ! timeout 60 $DEPLOY_ROOT/.venv/bin/python "$REMOTE_VALIDATE" \\
        --config "\$SEQ_CFG" --sequence "\$SEQ" --result-dir "\$R_OUT"; then
    echo "REMOTE_VALIDATE INVALID"
    exit 1
fi
tar czf "\$R_OUT.tar.gz" -C "$REMOTE_OUT_BASE" "\$SEQ" || { echo "TAR_FAIL"; exit 1; }
echo "SEQ_PASS"
REMOTE_EOF
)"
        if [ "$DRY_RUN" = 1 ]; then
            cat >&2 <<DRYEOF
[dry-run][scp←bm] timeout 300 scp $SSH_HOST:$R_OUT.tar.gz $LOCAL_RUNS/$SEQ.tar.gz
[dry-run][scp←bm] timeout 60 scp $SSH_HOST:$REMOTE_OUT_BASE/logs/$SEQ.a${attempt}.log $LOCAL_RUNS/$SEQ.a${attempt}.log
[dry-run][local] untar: tar xzf $LOCAL_RUNS/$SEQ.tar.gz -C $LOCAL_RUNS   → $LOCAL_RUNS/$SEQ/step_0/ib_m2f2_${SEQ#M2F2_}_t_{1,2,3}/（executor 原样回传，plot_m2_fig2.py 兼容布局）
[dry-run][local] per-arm validate: python3 $ARM_VALIDATOR --results $LOCAL_RUNS --sequence $SEQ --calibration $LOCAL_RUNS/calibration.json
DRYEOF
            continue
        fi
        if echo "$RUN_OUT" | grep -q '^SEQ_PASS$'; then
            pull_files 300 "$SSH_HOST:$R_OUT.tar.gz" "$LOCAL_RUNS/$SEQ.tar.gz" \
                || { echo ">>> [$SEQ] 回传失败（tar）"; continue; }
            pull_files 60 "$SSH_HOST:$REMOTE_OUT_BASE/logs/$SEQ.a${attempt}.log" \
                "$LOCAL_RUNS/$SEQ.a${attempt}.log" || true
            run_local "untar $SEQ → $LOCAL_RUNS/$SEQ" tar xzf "$LOCAL_RUNS/$SEQ.tar.gz" -C "$LOCAL_RUNS"
            if python3 "$ARM_VALIDATOR" --results "$LOCAL_RUNS" --sequence "$SEQ" \
                 --calibration "$LOCAL_RUNS/calibration.json"; then
                echo ">>> [$SEQ] PASS (attempt $attempt)"
                echo "$SEQ" >> "$LOCAL_RUNS/_pass.log"; PASS=$((PASS+1)); arm_passed=1; break
            else
                echo ">>> [$SEQ] 本机 per-arm validate 失败（数据保留于 $L_OUT 供检查）"
            fi
        else
            echo ">>> [$SEQ] attempt $attempt 失败（executor/远端 validate 未过）"
            echo "$RUN_OUT" | tail -30
        fi
        [ "$attempt" = "$ATTEMPTS" ] || sleep 3
    done
    if [ "$arm_passed" != 1 ] && [ "$DRY_RUN" != 1 ]; then
        echo "$SEQ" >> "$LOCAL_RUNS/_skip.log"; SKIP=$((SKIP+1))
    fi
done

# ---- [4] 汇总 ------------------------------------------------------------------
if [ "$DRY_RUN" = 1 ]; then
    log "=== DRY-RUN 结束：以上为全部将执行的 ssh/executor/回传命令与输出路径 ==="
    exit 0
fi
TOTAL=$((PASS+SKIP))
echo ""
echo "=== m2_fig2_bm SUMMARY: PASS=$PASS SKIP=$SKIP TOTAL=$TOTAL (RUN_ID=$RUN_ID) ==="
printf 'PASS=%d SKIP=%d TOTAL=%d\n' "$PASS" "$SKIP" "$TOTAL" > "$LOCAL_RUNS/_last_run_status"
if [ "$SKIP" -gt 0 ]; then
    echo "SKIP 明细:"
    for s in $(cat "$LOCAL_RUNS/_skip.log" 2>/dev/null); do
        echo "  - $s: ${SKIP_REASON[$s]:-重试 $MAX_RETRIES 次未过（见 $LOCAL_RUNS/$s.a*.log）}"
    done
fi
echo "成图（kvmhost 同款 plot，直接指向本轮 runs 目录）："
echo "  python3 scripts/plot_m2_fig2.py --results $LOCAL_RUNS --calibration $LOCAL_RUNS/calibration.json"
log "结果目录: $LOCAL_RUNS"
[ "$SKIP" -eq 0 ]
