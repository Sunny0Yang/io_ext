#!/bin/bash
# gen_netconsole_bm.sh — arm/disarm netconsole on bm so a wedge freeze still
# ships kernel messages to this workstation (DIAG follow-up; root-disk IO can
# be dead while the NIC still works — that is exactly the observed state).
#
# Local (control machine) usage:
#   gen_netconsole_bm.sh arm     -> arms netconsole on bm, starts local UDP sink
#   gen_netconsole_bm.sh sink    -> (re)start the local UDP listener only
#   gen_netconsole_bm.sh disarm  -> remove netconsole on bm, stop sink
#   gen_netconsole_bm.sh print   -> just print the remote commands
#
# Requires: experiments/m1m2_bm/scripts/ssh_env_bm.sh (ssh_cmd/remote_sudo),
# bm primary NIC (127.0.0.1/24), same L2 as this workstation.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/../../m1m2_bm/scripts/ssh_env_bm.sh"

DEV="${NETCONSOLE_IFACE:-eth0}"
SRV_PORT=6666            # udp port on the workstation that receives dmesg
LOG="${HOME}/ioext-bench/experiments/dyn_tenant_bm/results/netconsole_bm.log"

WS_IP=$(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -1)
WS_MAC=$(ip -o link show dev "$(ip -4 -o addr show scope global | awk '{print $2}' | head -1)" | awk '{print $17}')
[ -n "$WS_IP" ] && [ -n "$WS_MAC" ] || { echo "cannot autodetect workstation ip/mac" >&2; exit 1; }

# modprobe receives the value of the netconsole parameter; do not repeat the
# `netconsole=` key inside that value.
CONF="5666@127.0.0.1/${DEV},${SRV_PORT}@${WS_IP}/${WS_MAC}"

arm_remote='
modprobe netconsole netconsole="'"$CONF"'" 2>/dev/null || {
    # module builtin or param reload needed
    if [ -d /sys/module/netconsole ]; then
        echo netconsole already loaded; ls /sys/kernel/config/netconsole 2>/dev/null
    else
        echo FAILED to load netconsole >&2; exit 1
    fi
}
# shorter hung-task reporting so a freeze ships backtraces quickly
sysctl -w kernel.hung_task_timeout_secs=30 >/dev/null
sysctl -w kernel.hung_task_all_cpu_backtrace=1 2>/dev/null || true
dmesg | tail -2
echo NETCONSOLE-ARMED'
disarm_remote='rmmod netconsole 2>/dev/null; echo NETCONSOLE-DISARMED'
sink_py='
import socket,time,sys
out=sys.argv[1]
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.bind(("0.0.0.0",'"$SRV_PORT"'))
s.settimeout(1.0)
print(f"[sink] listening udp/{int(sys.argv[2])} -> {out}",flush=True)
with open(out,"ab",0) as f:
    while True:
        try:
            data,_=s.recvfrom(65536)
            f.write((time.strftime("%H:%M:%S") + " ").encode()+data+b"\n")
        except socket.timeout:
            pass
'

case "${1:-print}" in
    arm)
        remote_sudo "$arm_remote"
        pkill -f "netconsole_sink_bm" 2>/dev/null || true
        nohup python3 -c "$sink_py" "$LOG" "$SRV_PORT" > /tmp/netconsole_sink_bm.log 2>&1 &
        echo "[netconsole] sink started, log=$LOG"
        ;;
    sink)
        pkill -f "netconsole_sink_bm" 2>/dev/null || true
        nohup python3 -c "$sink_py" "$LOG" "$SRV_PORT" > /tmp/netconsole_sink_bm.log 2>&1 &
        echo "[netconsole] sink restarted, log=$LOG"
        ;;
    disarm)
        remote_sudo "$disarm_remote"
        pkill -f "netconsole_sink_bm" 2>/dev/null || true
        echo "[netconsole] sink stopped, log kept=$LOG"
        ;;
    print)
        echo "remote arm:  remote_sudo '$arm_remote'"
        echo "remote disarm: remote_sudo '$disarm_remote'"
        echo "sink: python3 udp://0.0.0.0:$SRV_PORT -> $LOG"
        ;;
    *) echo "usage: $0 arm|sink|disarm|print" >&2; exit 2 ;;
esac
