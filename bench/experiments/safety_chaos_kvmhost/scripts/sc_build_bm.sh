#!/usr/bin/env bash
# sc_build_bm.sh — build the S-CHAOS fault policies (q_inject_null/q_inject_stall)
# on the workstation inside the ioext io userspace tree, then rsync the
# loaders to kvmhost.
#
# Usage: scripts/../experiments/safety_chaos_kvmhost/scripts/sc_build_bm.sh [--skip-rsync]
# Env:   IOEXT_WT (default /opt/ioext-io_ext)
#        SC_SSH   (default "user@127.0.0.1", key-based or agent auth required)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IOEXT_WT="${IOEXT_WT:-/opt/ioext-io_ext}"
IO_IO="$IOEXT_WT/userspace/io"
SC_SSH="${SC_SSH:-user@127.0.0.1}"
ARCHIVE_ENV="${ARCHIVE_ENV:-/home/anon/ioext-ssh/ssh_env.sh}"
# shellcheck disable=SC1090
[ -f "$ARCHIVE_ENV" ] && source "$ARCHIVE_ENV"
SUDO_PASS="${SUDO_PASS:-REDACTED}"
SKIP_RSYNC=0
[ "${1:-}" = "--skip-rsync" ] && SKIP_RSYNC=1

[ -d "$IO_IO" ] || { echo "ioext io tree not found: $IO_IO" >&2; exit 1; }

echo "=== install fault policy sources into $IO_IO ==="
for f in q_inject_null.bpf.c q_inject_stall.bpf.c \
         loader_q_inject_null.c loader_q_inject_stall.c; do
    [ -f "$HERE/bpf/$f" ] || { echo "missing source $HERE/bpf/$f" >&2; exit 1; }
    cp -v "$HERE/bpf/$f" "$IO_IO/$f"
done

# register the two policies in the POLICIES make list (idempotent)
if ! grep -q "q_inject_null" "$IO_IO/Makefile"; then
    cp "$IO_IO/Makefile" "$IO_IO/Makefile.pre_sc_chaos.bak"
    sed -i 's/^POLICIES := \(.*\)$/POLICIES := \1 q_inject_null q_inject_stall/' "$IO_IO/Makefile"
    echo "Makefile POLICIES updated (backup: Makefile.pre_sc_chaos.bak)"
fi

echo "=== make q_inject_null q_inject_stall ==="
make -C "$IO_IO" q_inject_null q_inject_stall

ls -l "$IO_IO/build/loader_q_inject_null" "$IO_IO/build/loader_q_inject_stall"

if [ "$SKIP_RSYNC" = "1" ]; then
    echo "=== rsync skipped (--skip-rsync) ==="
    exit 0
fi

echo "=== rsync loaders + run scripts to kvmhost ==="
# one-time bootstrap: /opt needs root; then hand the tree to the ssh user
ssh "$SC_SSH" "echo '$SUDO_PASS' | sudo -S sh -c '
  mkdir -p /opt/safety_chaos/bin /opt/safety_chaos/scripts /opt/safety_chaos/results
  chown -R $(id -u):$(id -g) /opt/safety_chaos'" 2>/dev/null || true
# stock loaders used by the arm matrix + the two fault loaders
LOADER_SET="loader_q_fifo loader_q_none loader_q_wedf loader_arb_noop loader_arb_sp \
            loader_q_inject_null loader_q_inject_stall"
for L in $LOADER_SET; do
  [ -x "$IO_IO/build/$L" ] || { echo "missing $IO_IO/build/$L — run make for it" >&2; exit 1; }
done
rsync -az -e ssh $(for L in $LOADER_SET; do echo "$IO_IO/build/$L"; done) \
                 "$SC_SSH:/opt/safety_chaos/bin/"
rsync -az -e ssh "$HERE/sc_run_chaos_bm.sh" "$HERE/sc_injector_bm.py" \
                 "$HERE/sc_sampler_bm.sh" \
                 "$SC_SSH:/opt/safety_chaos/scripts/"
echo "BUILD+SYNC OK"
