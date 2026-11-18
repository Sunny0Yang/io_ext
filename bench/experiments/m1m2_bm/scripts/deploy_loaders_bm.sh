#!/usr/bin/env bash
# deploy_loaders_bm.sh —— RQ5 iox 臂 io_ext loader 的“激活”脚本（上传 + 校验 + 冒烟）。
#
# ⚠️ 本脚本是 LATER STEP：只在 bm 空闲时运行。硬性前置（Phase 1，全部只读探测）：
#     ① /opt/ioext/.m1_campaign.lock 无人持有（flock -n 试探，抢不到=有
#        m1/m2 campaign 在跑 → BLOCKED）；
#     ② bm 上无任何 vm.executor 进程；
#     ③ bm 上无残留 loader_q_*/loader_arb_* 进程（会撞 struct_ops 注册）。
#   campaign 运行期间绝不允许运行本脚本：loader 冒烟会切 scx_io、写 bpffs、
#   触碰 /sys/block/*/queue/scheduler —— 与在跑实验互毁。
#
# 用法: bash deploy_loaders_bm.sh [--bundle <tar.gz>] [--smoke-config <yml>]
#                                   [--smoke-sequence <seq>] [--skip-smoke] [--force]
#   --bundle         缺省 bundles/rq5_ioext_loaders_bm.tar.gz
#   --smoke-config   executor 冒烟用任务配置（vm.executor --config；bm 版 RQ5 冒烟
#                   配置就绪后传入；未传且未 --skip-smoke 则只上传不冒烟）
#   --force          目标目录已存在且内容不同时强制覆盖（默认幂等校验后跳过）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/ssh_env_bm.sh"

BUNDLE_DEFAULT="$REPO_ROOT/experiments/m1m2_bm/bundles/rq5_ioext_loaders_bm.tar.gz"
BUNDLE="$BUNDLE_DEFAULT"
SMOKE_CONFIG=""
SMOKE_SEQUENCE=""
SKIP_SMOKE=0
FORCE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --bundle)         BUNDLE="${2:?}"; shift 2 ;;
        --smoke-config)   SMOKE_CONFIG="${2:?}"; shift 2 ;;
        --smoke-sequence) SMOKE_SEQUENCE="${2:?}"; shift 2 ;;
        --skip-smoke)     SKIP_SMOKE=1; shift ;;
        --force)          FORCE=1; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

REMOTE_ROOT="/opt/ioext"
REMOTE_IOEXT="$REMOTE_ROOT/io-ext"                # NEW 目录（bm 上此前不存在）
REMOTE_LOCK="$REMOTE_ROOT/.m1_campaign.lock"
TARGET_DEVICE="nvme0n1"
TARGET_KERNEL="7.3.1-ioext2"

log()  { echo "[deploy-loaders86 $(date +%H:%M:%S)] $*"; }
die()  { echo "BLOCKED: $*" >&2; exit 1; }

# ---- Phase 0: 本机 bundle 完整性 ------------------------------------------------
[ -f "$BUNDLE" ] || die "bundle 缺失: $BUNDLE（先跑 build_rq5_ioext_loaders_bundle_bm.sh）"
[ -f "$BUNDLE.sha256" ] || die "sha256 侧车缺失: $BUNDLE.sha256"
( cd "$(dirname "$BUNDLE")" && sha256sum -c "$(basename "$BUNDLE").sha256" ) >/dev/null \
    || die "bundle sha256 校验失败"
log "bundle OK: $(basename "$BUNDLE")"

# ---- Phase 1: bm 空闲预检（全部只读，绝不写任何状态）---------------------------
log "Phase 1: 只读空闲预检（lock / executor / 残留 loader / 内核）"
out=$(ssh_cmd "uname -r")
[ "$out" = "$TARGET_KERNEL" ] || die "bm 内核非 $TARGET_KERNEL（实测 $out）"
log "bm kernel = $out"

# campaign lock：抢不到 = 有 campaign 持锁在跑 → 立即放弃（绝不能等锁，campaign 优先）
# 注意：必须以 append(>>) 打开探测 —— campaign 会把 case 标记写进 lock 文件，
# `>` 会截断它（写副作用）；`>>` + flock -n 是纯探测。
lock_probe=$(ssh_cmd "
    exec 9>>'$REMOTE_LOCK' 2>/dev/null || { echo LOCK_FILE_UNCREATABLE; exit 0; }
    if flock -n 9 2>/dev/null; then echo LOCK_FREE; flock -u 9 2>/dev/null; exec 9>&-;
    else echo LOCK_HELD; fi")
case "$lock_probe" in
    LOCK_FREE)      log "campaign lock: 无持有者（空闲）" ;;
    LOCK_HELD)      die "m1/m2 campaign 正在运行（$REMOTE_LOCK 被持有）—— 禁止激活" ;;
    LOCK_FILE_UNCREATABLE) die "无法创建 lock 探测文件（权限/父目录缺失）" ;;
    *)              die "lock 探测异常: $lock_probe" ;;
esac

# vm.executor / 残留 loader 进程
execs=$(ssh_cmd "pgrep -af 'vm\.executor' | grep -v pgrep || true")
[ -z "$execs" ] || die "bm 上仍有 vm.executor 在跑:\n$execs"
loaders=$(ssh_cmd "pgrep -af 'loader_(q_|arb_)' | grep -v pgrep || true")
[ -z "$loaders" ] || die "bm 上有残留 io_ext loader 进程:\n$loaders"
log "无 vm.executor / 无残留 loader"

# 记录当前 scheduler（冒烟后必须还原）+ 磁盘余量
PRESCHED=$(ssh_cmd "cat /sys/block/$TARGET_DEVICE/queue/scheduler")
PRESCHED_NAME=$(echo "$PRESCHED" | sed -n 's/.*\[\([^]]*\)\].*/\1/p')
log "pre-smoke scheduler = $PRESCHED (active: $PRESCHED_NAME)"
free_kb=$(ssh_cmd "df -Pk /home | tail -1 | awk '{print \$4}'")
[ "$free_kb" -gt 1048576 ] || die "bm /home 余量不足 1 GiB (${free_kb} KiB)"

# ---- Phase 2: 上传 + 解压到 NEW 目录 -------------------------------------------
log "Phase 2: 上传并解压到 $REMOTE_IOEXT/userspace/"
DEB_NAME_TMP="$REMOTE_ROOT/$(basename "$BUNDLE")"
if ssh_cmd "test -d '$REMOTE_IOEXT/userspace'"; then
    if [ "$FORCE" -eq 1 ]; then
        log "目标目录已存在，--force 覆盖"
        ssh_cmd "rm -rf '$REMOTE_IOEXT'"
    else
        log "目标目录已存在：跳过上传（幂等；如需覆盖请 --force）"
    fi
fi
if ! ssh_cmd "test -d '$REMOTE_IOEXT/userspace'"; then
    scp_cmd "$BUNDLE" "$SSH_HOST:$DEB_NAME_TMP"
    ssh_cmd "mkdir -p '$REMOTE_IOEXT' && tar xzf '$DEB_NAME_TMP' -C '$REMOTE_IOEXT' --strip-components=1 && rm -f '$DEB_NAME_TMP'"
fi
ssh_cmd "test -x '$REMOTE_IOEXT/userspace/loader_q_ddl' && test -x '$REMOTE_IOEXT/userspace/loader_arb_wdrr'" \
    || die "解压后 loader 不在位: $REMOTE_IOEXT/userspace/"

# ---- Phase 3: 远端逐文件 sha256（对照 bundle 内 MANIFEST）+ 安全 --help 探测 ------
# --help 在参数解析阶段 exit(0)，不 open/load 任何 BPF —— 空闲机上也零副作用。
log "Phase 3: 远端 sha256（对照 bundle 内 MANIFEST）+ loader --help 探测"
BUNDLE_TOP="$(basename "$BUNDLE" .tar.gz)"
expected=$(tar xzOf "$BUNDLE" "$BUNDLE_TOP/MANIFEST.loaders.txt" \
    | awk '$3 ~ /^userspace\/loader_/ {n=split($3,a,"/"); print $1"  "a[n]}' | sort)
[ -n "$expected" ] || die "bundle 内 MANIFEST.loaders.txt 无 loader 条目（打包异常）"
remote_sha=$(ssh_cmd "cd '$REMOTE_IOEXT/userspace' && sha256sum loader_q_ddl loader_q_fifo loader_q_sp_edf loader_q_wedf loader_arb_wdrr" | sort)
if [ "$expected" != "$remote_sha" ]; then
    die "远端 sha256 与 bundle MANIFEST 不一致:\n--expected--\n$expected\n--actual--\n$remote_sha"
fi
log "5 个 loader sha256 与 MANIFEST 一致"
for l in loader_q_ddl loader_q_fifo loader_q_sp_edf loader_q_wedf loader_arb_wdrr; do
    ssh_cmd "'$REMOTE_IOEXT/userspace/$l' --help >/dev/null 2>&1" \
        || die "$l --help 探测失败（静态二进制不可运行?）"
done
log "5 个 loader --help 全部 OK（未加载 BPF）"

# ---- Phase 4: executor 驱动的 loader 冒烟（可选）--------------------------------
if [ "$SKIP_SMOKE" -eq 1 ]; then
    log "Phase 4: --skip-smoke，跳过冒烟（仅上传+校验完成）"
elif [ -z "$SMOKE_CONFIG" ]; then
    log "Phase 4: 未提供 --smoke-config，跳过冒烟（bm 版 RQ5 iox 冒烟配置就绪后重跑本脚本）"
else
    log "Phase 4: executor 冒烟（再次执行 Phase 1 同款空闲预检）"
    # 冒烟本身也可能撞上刚启动的 campaign：入口处再抢一次锁探测
    lock_probe=$(ssh_cmd "exec 9>>'$REMOTE_LOCK' 2>/dev/null; if flock -n 9 2>/dev/null; then echo LOCK_FREE; flock -u 9 2>/dev/null; else echo LOCK_HELD; fi")
    [ "$lock_probe" = "LOCK_FREE" ] || die "冒烟前检测到 campaign 持锁 —— 放弃冒烟（上传结果保留）"
    seq_args=""
    [ -n "$SMOKE_SEQUENCE" ] && seq_args="--sequence $SMOKE_SEQUENCE"
    smoke_cfg_remote="$REMOTE_IOEXT/$(basename "$SMOKE_CONFIG")"
    scp_cmd "$SMOKE_CONFIG" "$SSH_HOST:$smoke_cfg_remote"
    # executor 驱动（与 campaign 同款调用口径：cwd=src + 部署 venv）
    ssh_cmd "cd '$REMOTE_ROOT/src' && '$REMOTE_ROOT/.venv/bin/python' -m vm.executor --config '$smoke_cfg_remote' $seq_args --output /tmp/rq5_loader_smoke_\$(date +%s) --log-level INFO" \
        || die "vm.executor 冒烟失败（查看上方输出；loader/struct_ops 语义问题需人工介入）"
    # 冒烟后卫生：scheduler 还原 + 无残留 loader + bpffs ioext pin 清点
    log "冒烟后卫生检查"
    postsched_name=$(ssh_cmd "cat /sys/block/$TARGET_DEVICE/queue/scheduler" | sed -n 's/.*\[\([^]]*\)\].*/\1/p')
    if [ "$postsched_name" != "$PRESCHED_NAME" ]; then
        log "scheduler 被冒烟改动（现 $postsched_name），还原为冒烟前 $PRESCHED_NAME"
        remote_sudo "echo '$PRESCHED_NAME' > /sys/block/$TARGET_DEVICE/queue/scheduler" || true
    fi
    left=$(ssh_cmd "pgrep -af 'loader_(q_|arb_)' | grep -v pgrep || true")
    [ -z "$left" ] || die "冒烟后仍有 loader 残留:\n$left"
    pins=$(ssh_cmd "ls /sys/fs/bpf/ 2>/dev/null | grep -i ioext || true")
    [ -z "$pins" ] && log "bpffs 无 ioext 残留 pin" || log "WARN: bpffs 残留 pin（冒烟场景 cleanup_bpffs_pins 未清?）: $pins"
fi

log "完成。RQ5 配置侧引用: export IOEXT_WORKSPACE=$REMOTE_IOEXT → \$IOEXT_WORKSPACE/userspace/loader_*"
