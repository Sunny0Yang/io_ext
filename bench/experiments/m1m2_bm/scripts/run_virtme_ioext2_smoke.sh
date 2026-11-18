#!/usr/bin/env bash
# Host-side: boot ioext2 bzImage in virtme-ng and run capability smoke (pre-deploy gate).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIMG="${KIMG:-/opt/ioext-io_ext/kernel/linux-7.3.1/arch/x86/boot/bzImage}"
GUEST_SCRIPT="$SCRIPT_DIR/virtme_ioext2_smoke.sh"

[ -f "$KIMG" ] || { echo "missing bzImage: $KIMG" >&2; exit 1; }
[ -x "$GUEST_SCRIPT" ] || chmod +x "$GUEST_SCRIPT"

echo "KIMG=$KIMG"
script -q -c "vng --run '$KIMG' --cpus 4 --memory 4G --rwdir '$SCRIPT_DIR=$SCRIPT_DIR' --exec 'bash $GUEST_SCRIPT'" /dev/null
