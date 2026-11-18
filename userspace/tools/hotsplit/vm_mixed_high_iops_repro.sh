#!/bin/bash
# Host driver: mixed high-IOPS + optional root-cgroup policy repro in virtme-ng.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OS_EXT="$(cd "$BASE_DIR/../.." && pwd)"
KERNEL_SRC="$OS_EXT/kernel/linux-7.3.1"
VNG_LOCK="${VNG_LOCK:-/tmp/vng.lock}"
VM_CPUS="${VM_CPUS:-16}"
VM_MEMORY="${VM_MEMORY:-6G}"
HS_ATTACH_ROOT="${HS_ATTACH_ROOT:-1}"
HS_DURATION="${HS_DURATION:-40}"
RUN_SECONDS=$((HS_DURATION + 180))

TS="$(date +%Y%m%d_%H%M%S)"
OUT="$OS_EXT/results/hotsplit_disks/mixed_repro_$TS"
DISK="$OUT/disk.img"
SERIAL="$OUT/serial.log"
mkdir -p "$OUT"

log() { printf '[vm_mixed_repro] %s\n' "$*" >&2; }

[ -f "$KERNEL_SRC/arch/x86/boot/bzImage" ] || { log "missing bzImage"; exit 2; }

qemu-img create -f raw "$DISK" 8G >/dev/null
log "output=$OUT attach_root=$HS_ATTACH_ROOT duration=${HS_DURATION}s"

GUEST_CMD="HS_BASE_DIR=$OS_EXT REPRO_DIR=$OUT HS_DEVICE=vda HS_DURATION=$HS_DURATION \
  HS_ATTACH_ROOT=$HS_ATTACH_ROOT bash $BASE_DIR/tools/hotsplit/guest_mixed_high_iops_repro.sh"

VNG_CMD=(vng --run "$KERNEL_SRC"
  --memory "$VM_MEMORY" --cpus "$VM_CPUS"
  --append "console=ttyS0 no-kvmapf loglevel=4"
  --cwd "$OS_EXT/"
  --rwdir "$OUT"
  "--qemu-opts=-drive file=$DISK,format=raw,if=none,id=disk1,cache=none,aio=native -device virtio-blk-pci,drive=disk1,serial=mixedrepro -cpu host,pmu=on -serial file:$SERIAL"
  --exec "$GUEST_CMD")

log "launching vng (timeout ${RUN_SECONDS}s)"
if [ ! -t 1 ]; then
  RUN_WRAPPER=(script -q -c "$(printf '%q ' "${VNG_CMD[@]}")" /dev/null)
else
  RUN_WRAPPER=("${VNG_CMD[@]}")
fi
(
  flock -x 9
  timeout "$RUN_SECONDS" "${RUN_WRAPPER[@]}"
) 9>"$VNG_LOCK"
VNG_RC=$?
log "vng rc=$VNG_RC"
cat "$OUT/summary.txt" 2>/dev/null || true
if [ -f "$OUT/dmesg_final.txt" ]; then
  rg -n 'evacuate stall|forcing teardown|jbd2|blocked for more than' "$OUT/dmesg_final.txt" | head -20 || true
fi
exit "$VNG_RC"
