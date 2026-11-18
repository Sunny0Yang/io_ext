#!/usr/bin/env bash
# sc_launch_bm.sh — workstation-side driver: sync scripts, arm netconsole,
# run one S-CHAOS run on kvmhost via sudo, pull artifacts back.
#
# Usage:
#   sc_launch_bm.sh --arm S1 --rep r1 [--runtime-s 240] [--size-g-total 32]
#                   [--injector-args "..."] [--with-ftrace] [--no-netconsole]
#                   [--smoke]     # smoke: size 4G, runtime 30s
# Env: SSH_HOST/SSH_PORT/SSH_USER/SUDO_PASS default from the kvmhost provision archive.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPDIR="$(cd "$HERE/.." && pwd)"
ARCHIVE_ENV="${ARCHIVE_ENV:-/home/anon/ioext-ssh/ssh_env.sh}"
# shellcheck disable=SC1090
[ -f "$ARCHIVE_ENV" ] && source "$ARCHIVE_ENV"
SSH_HOST="${SSH_HOST:-127.0.0.1}"
SSH_PORT="${SSH_PORT:-22}"
SSH_USER="${SSH_USER:-user}"
SUDO_PASS="${SUDO_PASS:-REDACTED}"
SSH="$SSH_USER@$SSH_HOST"
SSH_OPTS="-p $SSH_PORT -o StrictHostKeyChecking=accept-new"

ARM="S1"; REP="r1"; RUNTIME_S=240; SIZE_G=8; INJ_ARGS=""; FTRACE=0; NETCON=1; SMOKE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --arm) ARM="$2"; shift 2;;
    --rep) REP="$2"; shift 2;;
    --runtime-s) RUNTIME_S="$2"; shift 2;;
    --size-g-total) SIZE_G="$2"; shift 2;;
    --injector-args) INJ_ARGS="$2"; shift 2;;
    --with-ftrace) FTRACE=1; shift;;
    --no-netconsole) NETCON=0; shift;;
    --smoke) SMOKE=1; RUNTIME_S=60; SIZE_G=1; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac
done
if [ "$SMOKE" = "1" ] && [ -z "$INJ_ARGS" ]; then
  INJ_ARGS="--period-ms 200 --local-cycle q_fifo,q_none"
fi

echo "=== sync run scripts ==="
ssh $SSH_OPTS "$SSH" "mkdir -p /opt/safety_chaos/bin /opt/safety_chaos/scripts /opt/safety_chaos/results"
rsync -az -e "ssh $SSH_OPTS" "$HERE/sc_run_chaos_bm.sh" "$HERE/sc_sampler_bm.sh" \
  "$SSH:/opt/safety_chaos/scripts/"
rsync -az -e "ssh $SSH_OPTS" "$HERE/sc_injector_bm.py" "$SSH:/opt/safety_chaos/scripts/"

NC_SINK_PID=""
arm_netconsole() {
  echo "=== arm netconsole (wedge safety net) ==="
  mkdir -p "$EXPDIR/results"
  # NB: use awk (not `| head -1`) inside command substitution — head closes the
  # pipe early, ip gets SIGPIPE(141), and set -o pipefail kills this script.
  WS_LINE=$(ip -4 -o addr show scope global | awk 'NR==1')
  WS_DEV=$(echo "$WS_LINE" | awk '{print $2}')
  WS_IP=$(echo "$WS_LINE" | awk '{print $4}' | cut -d/ -f1)
  WS_MAC=$(cat "/sys/class/net/$WS_DEV/address" 2>/dev/null)
  REM_DEV=$(ssh $SSH_OPTS "$SSH" "ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'dev \\K\\S+'")
  REM_IP=$(ssh $SSH_OPTS "$SSH" "ip -4 -o addr show dev $REM_DEV scope global 2>/dev/null | awk 'NR==1{print \$4}' | cut -d/ -f1")
  if [ -z "$WS_IP" ] || [ -z "$WS_MAC" ] || [ -z "$REM_DEV" ] || [ -z "$REM_IP" ]; then
    echo "netconsole autodetect failed (ws=$WS_DEV/$WS_IP rem=$REM_DEV/$REM_IP); continuing without" >&2
    return 0
  fi
  ssh $SSH_OPTS "$SSH" "echo '$SUDO_PASS' | sudo -S modprobe netconsole \
      netconsole='6666@$REM_IP/$REM_DEV,6667@$WS_IP/$WS_MAC'" \
    || { echo "netconsole arm failed; continuing" >&2; return 0; }
  python3 - "$EXPDIR/results/netconsole_sink.log" <<'EOF' &
import socket, sys
out = sys.argv[1]
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", 6667))
with open(out, "ab", 0) as f:
    while True:
        f.write(s.recv(65535))
EOF
  NC_SINK_PID=$!
  echo "netconsole armed (remote dev $REM_DEV/$REM_IP -> $WS_IP:6667)"
}

if [ "$NETCON" = "1" ]; then arm_netconsole; fi

echo "=== run $ARM/$REP on kvmhost ==="
RC=0
ssh $SSH_OPTS "$SSH" "echo '$SUDO_PASS' | sudo -S bash /opt/safety_chaos/scripts/sc_run_chaos_bm.sh \
  --arm $ARM --rep $REP --runtime-s $RUNTIME_S --size-g-total $SIZE_G \
  --injector-args '$INJ_ARGS' $([ $FTRACE = 1 ] && echo --with-ftrace)" || RC=$?

echo "=== disarm netconsole ==="
ssh $SSH_OPTS "$SSH" "echo '$SUDO_PASS' | sudo -S rmmod netconsole 2>/dev/null; true" || true
[ -n "$NC_SINK_PID" ] && kill "$NC_SINK_PID" 2>/dev/null || true

echo "=== pull artifacts ==="
mkdir -p "$EXPDIR/results"
LATEST=$(ssh $SSH_OPTS "$SSH" "ls -1dt /opt/safety_chaos/results/safety_chaos_${ARM}_${REP}_* 2>/dev/null | head -1")
if [ -n "${LATEST:-}" ]; then
  rsync -az -e "ssh $SSH_OPTS" "$SSH:$LATEST/" "$EXPDIR/results/$(basename "$LATEST")/"
  echo "pulled -> $EXPDIR/results/$(basename "$LATEST")"
else
  echo "WARN: no result dir found on remote" >&2
fi
exit $RC
