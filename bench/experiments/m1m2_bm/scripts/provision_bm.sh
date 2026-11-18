#!/usr/bin/env bash
# provision_bm.sh —— 控制机执行：把运行环境部署到 bm:/opt/ioext
# 仿 scripts/run_m1_cache_campaign.py 的控制机编排（rsync 代码 -> venv -> 部署
# java/m1 bundle -> 只读 preflight），适配裸机 bm 的限制：
#   - 部署根用 /opt/ioext（非 /opt：sudo 密码未提供，只能非 root 起步）
#   - 明确不做：mkfs / 挂盘 / 改 block scheduler / 任何 remote_sudo 操作
#   - bm 有外网，venv 依赖直接 pip 安装；Python 3.14.8，ensurepip 失败时
#     fallback venv --without-pip + get-pip.py
# 幂等：bundle 已在远端 staging 就位则跳过上传与解压；venv 依赖齐全则跳过重建。
# 用法: bash experiments/m1m2_bm/scripts/provision_bm.sh [--skip-sync]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
EXP_DIR="$REPO_ROOT/experiments/m1m2_bm"
# shellcheck source=ssh_env_bm.sh
source "$EXP_DIR/scripts/ssh_env_bm.sh"

# ---- 路径集中定义 ------------------------------------------------------------
DEPLOY_ROOT="/opt/ioext"
REMOTE_STAGING="$DEPLOY_ROOT/staging"
M2_REMOTE_ROOT="$REMOTE_STAGING/motivation_m2_pilot_20261030_bundle"
M1_REMOTE_ROOT="$REMOTE_STAGING/m1_cache_bm_bundle"
M2_TAR="$EXP_DIR/bundles/motivation_m2_pilot_20261030_bundle.tar.gz"
M2_TAR_SHA="$M2_TAR.sha256"
M1_TAR="$EXP_DIR/bundles/m1_cache_bm_bundle.tar.gz"
M1_TAR_SHA="$M1_TAR.sha256"
M2_JAVA_BIN="$M2_REMOTE_ROOT/motivation_m2_pilot_20261030/runtime/java-21-openjdk-amd64/bin/java"
M1_DB_BENCH="$M1_REMOTE_ROOT/rocksdb/db_bench"
M1_SHORE_KITS="$M1_REMOTE_ROOT/tailbench/tailbench-v0.9/shore/shore-kits/shore_kits_integrated"
M1_TPCC="$M1_REMOTE_ROOT/data/db-tpcc-1"
VENV_DEPS="colorlog PyYAML mock numpy psutil typing-extensions"
RUN_TAG="provision_$(date +%Y%m%d_%H%M%S)"
LOG_DIR="$EXP_DIR/runs"
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/$RUN_TAG.log") 2>&1

# rsync 代码集：与 run_m1_cache_campaign.py SYNC_REL_PATHS 一致，另加 isol_bench
# README 与本实验 scripts 目录（--relative 保持 src/vm 树形落位）。
SYNC_REL_PATHS=(
    "src/vm/scene"
    "src/vm/configs/scene"
    "src/vm/configs/task/isol_bench/rig_m1_cache"
    "src/vm/configs/task/isol_bench/rig_rq1_directio"
    "src/vm/configs/task/isol_bench/rig_rq5_coloc"
    "src/vm/configs/task/isol_bench/README.md"
    "src/vm/config.py"
    "src/vm/controller.py"
    "src/vm/executor.py"
    "src/vm/evaluator.py"
    "src/vm/explore_tier.py"
    "src/vm/__init__.py"
    "experiments/m1m2_bm/scripts"
)

die() { echo "BLOCKED: $*" >&2; exit 2; }
target="$SSH_USER@$SSH_HOST"

# 远端批量脚本执行：单 base64 包装防引号/换行丢失（与双 base64 的 remote_sudo 同思路，
# 但不需要 sudo）。SSH 侧 ConnectTimeout/BatchMode 由 ssh_env_bm.sh 统一携带。
remote_script() {
    local b64
    b64=$(base64 -w0)
    ssh_cmd "echo ${b64} | base64 -d | bash"
}

# ---- [0] 本地基线：bundle 产物齐备 + sha256 通过 ------------------------------
echo "=== [0] 本地 bundle 校验（缺则先跑 build_bundles_bm.sh）==="
for pair in "$M2_TAR:$M2_TAR_SHA" "$M1_TAR:$M1_TAR_SHA"; do
    tar="${pair%%:*}"; sha="${pair##*:}"
    [ -f "$tar" ] || die "bundle 缺失: $tar"
    [ -f "$sha" ] || die "sha256 文件缺失: $sha"
    ( cd "$(dirname "$tar")" && sha256sum -c "$(basename "$sha")" ) || die "bundle 校验失败: $tar"
done
echo "--- bundle 校验通过"

# ---- [1] 可达性 + 主机身份检查（只读）----------------------------------------
echo "=== [1] ssh 可达性与身份检查 ==="
out=$(ssh_cmd 'printf "HOST=%s KERNEL=%s\n" "$(hostname)" "$(uname -r)"') || die "bm 不可达（检查 ssh 别名与密钥）"
echo "$out"
echo "$out" | grep -qE '^HOST=user($| )' || die "主机名不是 user，疑似连错机器"
echo "$out" | grep -qE 'KERNEL=7\.3\.1-ioext1($| )' || die "内核不是 7.3.1-ioext1，疑似错内核"

# ---- [2] rsync 代码集 --------------------------------------------------------
if [ "${1:-}" = "--skip-sync" ]; then
    echo "=== [2] 代码 rsync 跳过（--skip-sync）==="
else
    echo "=== [2] rsync 代码 + 配置 → $target:$DEPLOY_ROOT ==="
    for rel in "${SYNC_REL_PATHS[@]}"; do
        [ -e "$REPO_ROOT/$rel" ] || die "同步源缺失: $REPO_ROOT/$rel"
    done
    ( cd "$REPO_ROOT" && rsync -avz --relative --exclude '__pycache__' \
        -e "$RSYNC_SSH_OPTS" "${SYNC_REL_PATHS[@]}" "$target:$DEPLOY_ROOT/" ) \
        || die "代码 rsync 失败"
fi

# ---- [3] 远端 venv：确认/重建 + 依赖安装 --------------------------------------
echo "=== [3] 远端 venv（依赖: $VENV_DEPS）==="
venv_script=$(cat <<EOF
set -e
cd '$DEPLOY_ROOT'
PY=.venv/bin/python
if [ -x "\$PY" ] && "\$PY" -c 'import colorlog, yaml, numpy, psutil, typing_extensions' 2>/dev/null; then
    echo VENV_PRESENT
    exit 0
fi
echo "--- (re)build venv (python3 \$(python3 --version 2>&1 | cut -d' ' -f2))"
rm -rf .venv
if ! python3 -m venv .venv >/dev/null 2>&1; then
    echo "--- ensurepip 失败，fallback: --without-pip + get-pip.py"
    python3 -m venv .venv --without-pip
    if command -v curl >/dev/null 2>&1; then
        curl -fsSLo /tmp/get-pip_bm.py https://bootstrap.pypa.io/get-pip.py
    else
        wget -qO /tmp/get-pip_bm.py https://bootstrap.pypa.io/get-pip.py
    fi
    "\$PY" /tmp/get-pip_bm.py
fi
"\$PY" -m pip install --quiet --upgrade pip
"\$PY" -m pip install --quiet $VENV_DEPS
"\$PY" -c 'import colorlog, yaml, numpy, psutil, typing_extensions'
echo VENV_OK
EOF
)
out=$(printf '%s' "$venv_script" | remote_script) || die "远端 venv 构建失败（bm 有外网，pip 按要求失败即退出）"
echo "$out" | grep -qE 'VENV_PRESENT|VENV_OK' || die "远端 venv 状态异常:\n$out"
echo "$out"

# ---- [4] java bundle：上传 + sha256 校验 + 解压到固定 staging -----------------
echo "=== [4] java bundle → $M2_REMOTE_ROOT ==="
if ssh_cmd "test -x $(printf '%q' "$M2_JAVA_BIN") && echo PRESENT || echo MISSING" 2>/dev/null | grep -q PRESENT; then
    echo "--- 远端已就位，跳过上传与解压"
else
    echo "--- 上传 bundle（$(du -h "$M2_TAR" | cut -f1)）"
    ( rsync -avz -e "$RSYNC_SSH_OPTS" "$M2_TAR" "$M2_TAR_SHA" "$target:/tmp/" ) || die "java bundle 上传失败"
    m2_script=$(cat <<EOF
set -e
cd /tmp
sha256sum -c motivation_m2_pilot_20261030_bundle.tar.gz.sha256
rm -rf '$M2_REMOTE_ROOT'
mkdir -p '$M2_REMOTE_ROOT'
tar xzf motivation_m2_pilot_20261030_bundle.tar.gz -C '$M2_REMOTE_ROOT'
rm -f motivation_m2_pilot_20261030_bundle.tar.gz motivation_m2_pilot_20261030_bundle.tar.gz.sha256
test -x '$M2_JAVA_BIN' && echo M2_EXTRACT_OK
EOF
)
    out=$(printf '%s' "$m2_script" | remote_script) || die "java bundle 远端解压失败"
    echo "$out" | grep -q M2_EXTRACT_OK || die "java bundle 解压后缺 java:\n${out}"
    echo "$out"
fi

# ---- [5] m1 bundle：上传 + sha256 校验 + 解压到固定 staging -------------------
echo "=== [5] m1 bundle → $M1_REMOTE_ROOT ==="
probe_m1="test -x $(printf '%q' "$M1_DB_BENCH") && test -f $(printf '%q' "$M1_SHORE_KITS") && test -f $(printf '%q' "$M1_TPCC") && echo PRESENT || echo MISSING"
if ssh_cmd "$probe_m1" 2>/dev/null | grep -q PRESENT; then
    echo "--- 远端已就位，跳过上传与解压"
else
    echo "--- 上传 bundle（$(du -h "$M1_TAR" | cut -f1)）"
    ( rsync -avz -e "$RSYNC_SSH_OPTS" "$M1_TAR" "$M1_TAR_SHA" "$target:/tmp/" ) || die "m1 bundle 上传失败"
    m1_script=$(cat <<EOF
set -e
cd /tmp
sha256sum -c m1_cache_bm_bundle.tar.gz.sha256
rm -rf '$M1_REMOTE_ROOT'
mkdir -p '$M1_REMOTE_ROOT'
tar xzf m1_cache_bm_bundle.tar.gz -C '$M1_REMOTE_ROOT' --strip-components=1
rm -f m1_cache_bm_bundle.tar.gz m1_cache_bm_bundle.tar.gz.sha256
find '$M1_REMOTE_ROOT' -type f | wc -l
test -x '$M1_DB_BENCH' && test -f '$M1_SHORE_KITS' && test -f '$M1_TPCC' && echo M1_EXTRACT_OK
EOF
)
    out=$(printf '%s' "$m1_script" | remote_script) || die "m1 bundle 远端解压失败"
    echo "$out" | grep -q M1_EXTRACT_OK || die "m1 bundle 解压不完整:\n${out}"
    echo "$out"
fi

# ---- [6] 只读 preflight（hostname/uname/fio/盘信息；绝不 mkfs/挂盘/动 scheduler）-
echo "=== [6] 远端只读 preflight ==="
preflight_script=$(cat <<'EOF'
set -e
echo "hostname: $(hostname)"
echo "kernel:   $(uname -r)"
echo "python3:  $(python3 --version 2>&1)"
echo "fio:      $(fio --version 2>/dev/null || echo MISSING)"
echo "disk:"
lsblk -e7 -o NAME,MODEL,SIZE,FSTYPE,MOUNTPOINT 2>/dev/null || lsblk
echo "rootfs:   $(findmnt -rn -T / -o SOURCE)"
echo "home free: $(df -ph /home/anon | tail -1 | awk '{print $4}')"
echo "nvme0n1 scheduler (只读查看): $(cat /sys/block/nvme0n1/queue/scheduler 2>/dev/null || echo n/a)"
echo "cgroup: $(stat -fc %T /sys/fs/cgroup 2>/dev/null)"
echo "ioext bins: $(ls /home/anon/iosched_ext/iox_* 2>/dev/null | wc -l) 个 iox_* 于 /home/anon/iosched_ext"
/opt/ioext/.venv/bin/python -c 'import colorlog, yaml, numpy, psutil, typing_extensions; print("venv import check: OK")'
echo "注: 本步只读；mkfs/挂盘/修改 scheduler 均不在 provision 范围（无 sudo）。"
EOF
)
out=$(printf '%s' "$preflight_script" | remote_script) || die "preflight 失败"
echo "$out"
echo "$out" | grep -q 'fio:      fio' || echo "WARN: 远端 fio 缺失，压测前需补装" >&2

echo "=== provision_bm 完成 → 部署根 $DEPLOY_ROOT（日志 $LOG_DIR/$RUN_TAG.log）==="