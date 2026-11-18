#!/bin/bash
# Control-host wrapper: rsync ioext-bench + run one paper_sdb batch on kvmhost.
# Thin sibling of run_rig_iox_remote.sh — rig scripts/configs untouched.
# Usage: run_paper_sdb_remote.sh --desideratum P1|P2|PD2|PD3|PD4|PQ3|PQ4K|PQ4M|PQ5|PQ6|PI6
#           [--sequence SEQ] [--rsync-only] [--skip-sync]
# Env: ARCHIVE_DIR (default: the kvmhost provision archive).
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE_DIR="${ARCHIVE_DIR:-/home/anon/ioext-ssh}"
SSH_ENV="${SSH_ENV:-$ARCHIVE_DIR/ssh_env.sh}"
GUEST_ROOT="/opt/ioext-bench"
DESIDERATUM=""
SEQUENCE=""
RSYNC_ONLY=0
SKIP_SYNC=0

usage() { sed -n '2,7p' "$0"; exit 1; }
while [ $# -gt 0 ]; do
    case "$1" in
        --desideratum) DESIDERATUM="$2"; shift 2 ;;
        --sequence)    SEQUENCE="$2"; shift 2 ;;
        --rsync-only)  RSYNC_ONLY=1; SKIP_SYNC=0; shift ;;
        --skip-sync)   SKIP_SYNC=1; shift ;;
        *) echo "unknown: $1" >&2; usage ;;
    esac
done
[ -f "$SSH_ENV" ] || { echo "missing $SSH_ENV" >&2; exit 1; }
# shellcheck source=/dev/null
source "$SSH_ENV"
# ssh keepalive: a dead guest must tear the connection down promptly instead
# of leaving the wrapper blocked on a half-open TCP socket for hours.
SSH_OPTS+=(-o ServerAliveInterval=15 -o ServerAliveCountMax=4)

if [ "$SKIP_SYNC" -eq 0 ]; then
    echo "=== rsync ioext-bench (paper_sdb) ==="
    rsync_cmd -av --exclude .venv --exclude results --exclude .git \
        --exclude '__pycache__' --exclude '*.pyc' \
        "$BASE_DIR/" "$SSH_USER@$SSH_HOST:$GUEST_ROOT/"
fi
if [ "$RSYNC_ONLY" -eq 1 ]; then
    echo "rsync-only complete"
    exit 0
fi

[ -n "$DESIDERATUM" ] || usage
REMOTE_ARGS="--desideratum $DESIDERATUM"
[ -n "$SEQUENCE" ] && REMOTE_ARGS="$REMOTE_ARGS --sequence $SEQUENCE"

echo "=== preflight on guest (kernel + loaders + scheduler) ==="
IOEXT_WT="${IOEXT_WORKSPACE:-/opt/ioext-io_ext}"
[ -x "$IOEXT_WT/scripts/verify_osext_kernel_btf.sh" ] && \
    "$IOEXT_WT/scripts/verify_osext_kernel_btf.sh" --remote || true
case "$DESIDERATUM" in
    P1|P2|PD2|PD3|PD4|PQ3|PQ4K|PQ4M|PQ5|PQ6|PD2QB|PD3QB|PD4BW)
        # paper_sdb runs BOTH native-arm and iox-arm sequences in one file;
        # do not force a scheduler here — each sequence pins its own via the
        # global scene (rig-native semantics).
        remote_sudo "modprobe bfq 2>/dev/null; ls /opt/io-ext/userspace/loader_q_fifo"
        ;;
    PI6)
        # rig_iomaxsim fidelity matrix: q_ratelimit loader must be deployed
        # (rsync'd by this task alongside the q_fifo baseline check).
        remote_sudo "modprobe bfq 2>/dev/null; ls /opt/io-ext/userspace/loader_q_fifo /opt/io-ext/userspace/loader_q_ratelimit"
        ;;
esac

echo "=== run paper_sdb on guest (detached): $REMOTE_ARGS ==="
REMOTE_LOG="/tmp/psdb_${DESIDERATUM,,}.log"
remote_sudo "cd $GUEST_ROOT && rm -f $REMOTE_LOG && setsid nohup bash scripts/run_paper_sdb.sh $REMOTE_ARGS > $REMOTE_LOG 2>&1 < /dev/null &"
# Poll until the guest-side batch runner exits (setsid: it survives ssh
# drops; each poll is a fresh ssh so the poll itself is resilient too).
SSH_MISS=0
while :; do
    sleep 60
    # [.] breaks the self-match: the poller's own sudo-bash cmdline contains
    # the literal pattern, and pgrep -f would otherwise always match ITSELF.
    if remote_sudo "pgrep -f 'run_paper_sdb[.]sh --desideratum $DESIDERATUM' >/dev/null" 2>/dev/null; then
        SSH_MISS=0
        remote_sudo "tail -2 $REMOTE_LOG" 2>/dev/null | tr -d '\r' || true
    elif remote_sudo "true" 2>/dev/null; then
        # ssh works AND the runner is genuinely gone -> real batch exit
        SSH_MISS=0
        echo "=== guest batch runner exited; batch log tail: ==="
        remote_sudo "tail -4 $REMOTE_LOG" 2>/dev/null | tr -d '\r' || true
        break
    else
        # transient ssh failure: must NOT be read as a runner exit (a blip
        # during allocation flips previously burned through the whole
        # batch queue against the guest-side flock)
        SSH_MISS=$((SSH_MISS + 1))
        echo "=== poll ssh failed (attempt $SSH_MISS); retrying ==="
        if [ "$SSH_MISS" -ge 5 ]; then
            echo "=== 5 consecutive ssh failures; treating batch as orphaned (guest runner may still hold the lock) ==="
            break
        fi
    fi
done

echo "=== pull results ==="
LOCAL_OUT="$ARCHIVE_DIR/paper_sdb_${DESIDERATUM,,}"
mkdir -p "$LOCAL_OUT"
if [ -n "$SEQUENCE" ]; then
    remote_sudo "test -f /tmp/paper_sdb_${SEQUENCE}.tar.gz"
    scp_cmd "$SSH_USER@$SSH_HOST:/tmp/paper_sdb_${SEQUENCE}.tar.gz" "$LOCAL_OUT/"
else
    remote_sudo "ls /tmp/paper_sdb_*.tar.gz 2>/dev/null || true"
    scp_cmd "$SSH_USER@$SSH_HOST:/tmp/paper_sdb_*.tar.gz" "$LOCAL_OUT/" 2>/dev/null || true
fi
echo "=== remote run complete; tarballs in $LOCAL_OUT ==="