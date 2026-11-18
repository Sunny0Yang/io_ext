#!/usr/bin/env bash
# deploy_kernel_bm.sh —— bm 内核安全部署（ioext2）
#
# 可恢复性设计（用户 2026-11-04 要求：部署失败/IO 卡死都必须有远程恢复手段）：
#   R1 部署前不动任何引导状态；dpkg 失败直接退出，旧内核完好，机器无感。
#   R2 GRUB_DEFAULT=saved 且 grub-set-default 钉在旧内核 7.3.1-ioext1：
#      新内核用 grub-reboot 一次性引导；无论新内核以何种方式挂掉，任何一次
#      断电/硬重启都会回到默认的旧内核（grub-reboot 只生效一次）。
#   R3 kernel.sysrq=1：新内核 IO 卡死但 ssh 尚存活时，remote_sudo
#      "echo b > /proc/sysrq-trigger" 立即硬重启回默认旧内核。
#   R4 新内核启动后先做能力面验收（uname/scheduler/io.latency/io.cost/fio smoke），
#      全部通过才 grub-set-default 钉新内核；任何一项失败 = 自动重启回旧内核并报告。
#   R5 新内核 panic 场景：GRUB_TIMEOUT 保底 + sysrq-b；无 BMC，物理断电是最后手段
#      （默认项为旧内核，断电重启即恢复）。
#
# 用法: bash deploy_kernel_bm.sh <linux-image-deb 路径> [--skip-reboot]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/ssh_env_bm.sh"

DEB_PATH="${1:?用法: deploy_kernel_bm.sh <deb路径> [--skip-reboot]}"
SKIP_REBOOT="${2:-}"

DEB_NAME="$(basename "$DEB_PATH")"
# 内核版本号取 deb 包名第一段：linux-image-<kver>_<ver>_amd64.deb -> <kver>
PKG_BASE="${DEB_NAME%%_*}"
NEW_KVER="${PKG_BASE#linux-image-}"
OLD_KVER="7.3.1-ioext1"

log()  { echo "[deploy-bm $(date +%H:%M:%S)] $*"; }
die()  { echo "BLOCKED: $*" >&2; exit 1; }

# ---------- Phase A: 前置校验（全部只读/本机） ----------
[ -f "$DEB_PATH" ] || die "deb 不存在: $DEB_PATH"
[ "$NEW_KVER" != "$PKG_BASE" ] || die "deb 包名不含 linux-image-<kver> 前缀: $DEB_NAME"
log "deb=$DEB_NAME kver=$NEW_KVER"

log "Phase A: 远端身份与现状预检"
out=$(ssh_cmd "hostname && uname -r && ls /boot/vmlinuz-$OLD_KVER && df -h / | tail -1")
echo "$out" | grep -qx "user" || die "hostname 非 user"
echo "$out" | grep -qx "$OLD_KVER" || die "当前运行内核非 $OLD_KVER（$out）"
ssh_cmd "test -d /home/anon/iosched_ext && echo ws_ok"

# ---------- Phase B: 上传 + 安装（失败不重启，旧内核完好） ----------
log "Phase B: 上传 deb 并安装（$DEB_NAME）"
scp_cmd "$DEB_PATH" "$SSH_HOST:/tmp/$DEB_NAME"
remote_sudo "dpkg -i /tmp/$DEB_NAME" || die "dpkg -i 失败（旧内核未被触碰，可直接重试或回退）"

log "Phase B: 安装产物校验（/boot 与 modules/initrd）"
install_check=$(remote_sudo "test -e /boot/vmlinuz-$NEW_KVER && test -d /lib/modules/$NEW_KVER && ls /boot/initrd.img-$NEW_KVER 2>/dev/null && echo BOOT_OK || echo BOOT_MISSING")
echo "$install_check" | grep -q BOOT_OK || die "新内核 /boot/modules/initrd 不完整: $install_check（未重启，安全）"

# ---------- Phase C: GRUB 安全网 ----------
log "Phase C: GRUB 默认项钉旧内核 + sysrq 急停开关"
remote_sudo "cp -n /etc/default/grub /etc/default/grub.bak.\$(date +%s) || true"
# GRUB_DEFAULT -> saved（幂等）
remote_sudo "grep -q '^GRUB_DEFAULT=saved' /etc/default/grub || sed -i 's/^GRUB_DEFAULT=.*/GRUB_DEFAULT=saved/' /etc/default/grub"
remote_sudo "sysctl -w kernel.sysrq=1 >/dev/null"
remote_sudo "update-grub" >/dev/null

# 提取两个内核的 menuentry 标题（精确匹配内核名结尾，排除 (recovery mode)），随后拼完整路径
entries=$(remote_sudo "awk -F\"'\" '/menuentry /{print \$2}' /boot/grub/grub.cfg | grep -E 'Linux (${OLD_KVER}|${NEW_KVER})\$' | head -4")
old_entry=$(echo "$entries" | grep "Linux $OLD_KVER\$" | head -1)
new_entry=$(echo "$entries" | grep "Linux $NEW_KVER\$" | head -1)
[ -n "$old_entry" ] || die "grub.cfg 中未找到旧内核普通条目: $entries"
[ -n "$new_entry" ] || die "grub.cfg 中未找到新内核普通条目: $entries"
case "$old_entry$new_entry" in *recovery*) die "选中了 recovery 条目，中止（远程引导 recovery 会失去 ssh）";; esac
old_path="Advanced options for Ubuntu>$old_entry"
new_path="Advanced options for Ubuntu>$new_entry"
log "old_entry=$old_entry"
log "new_entry=$new_entry"
remote_sudo "grub-set-default '$old_path'"       # R2: 默认永远旧内核
remote_sudo "grub-editenv list | grep saved_entry && echo PIN_OLD_OK"

if [ "$SKIP_REBOOT" = "--skip-reboot" ]; then
    log "已按参数跳过重启；新内核已安装但未引导（grub-reboot 由后续手动触发）"
    exit 0
fi

# ---------- Phase D: 一次性引导新内核 ----------
log "Phase D: grub-reboot 一次性引导 $new_path"
remote_sudo "grub-reboot '$new_path'"
remote_sudo "sync; systemctl reboot" || true
log "等待机器重启回连（先等关机生效，再等上线；总超时 15 分钟）"
sleep 90   # 确保旧系统已彻底下线，避免把"重启前的存活"误判为"新内核已上线"
wait_ssh() {
    for i in $(seq 1 84); do
        if ssh_cmd "echo alive" 2>/dev/null | grep -q alive; then return 0; fi
        sleep 10
    done
    return 1
}
if ! wait_ssh; then
    die "重启后 15 分钟内 ssh 未恢复 —— 需人工/带外介入（默认项=旧内核，硬重启即恢复）"
fi

booted=$(ssh_cmd "uname -r")
log "重启后 uname: $booted"

# ---------- Phase E: 新内核验收，失败即回滚 ----------
rollback() {
    log "ROLLBACK: $* -> 重启回默认旧内核 $OLD_KVER"
    remote_sudo "sync; echo b > /proc/sysrq-trigger" 2>/dev/null || remote_sudo "systemctl reboot" || true
    exit 3
}
if [ "$booted" != "$NEW_KVER" ]; then
    if [ "$booted" = "$OLD_KVER" ]; then
        die "一次重启后仍运行旧内核（grub-reboot 未生效？）—— 未上钉新内核，状态安全"
    fi
    rollback "引导进了未知内核 $booted"
fi

log "Phase E: 新内核能力面验收"
cap=$(ssh_cmd "cat /sys/block/nvme0n1/queue/scheduler; ls /sys/fs/cgroup/io.latency 2>/dev/null && echo IOLAT_OK; ls /sys/fs/cgroup/io.cost.model >/dev/null 2>&1 && echo IOCOST_OK; mkdir -p /tmp/deploy_smoke && cd /tmp/deploy_smoke && fio --name=s --filename=s --size=16M --bs=4k --rw=randread --direct=1 --iodepth=4 --ioengine=io_uring --runtime=2 --time_based 2>/dev/null | grep -m1 IOPS; modinfo -k $NEW_KVER bfq >/dev/null 2>&1 && echo BFQ_BUILTIN_OR_MOD_OK")
echo "----- capability -----"; echo "$cap"; echo "----------------------"
echo "$cap" | grep -q IOLAT_OK || rollback "io.latency 缺失（本次合并的主要目标之一）"
echo "$cap" | grep -q IOCOST_OK || rollback "io.cost 不可用（回归！）"
echo "$cap" | grep -q "IOPS=" || rollback "fio smoke 失败（新内核 IO 不工作）"
echo "$cap" | grep -q BFQ_BUILTIN_OR_MOD_OK || rollback "bfq 不可用"

log "验收通过 → 钉新内核为默认（旧内核条目保留可回退）"
remote_sudo "grub-set-default '$new_path'"
remote_sudo "printf 'kernel.sysrq = 1\n' > /etc/sysctl.d/90-ioext-sysrq.conf"
log "DONE: bm 现运行 $NEW_KVER，默认引导已持久化；回退路径: grub-set-default '$old_path' + reboot"
