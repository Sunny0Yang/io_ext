#!/usr/bin/env python3
"""Sidecar sysfs sampler for the m2ctrl campaign (Phase C2, fig.3 controller arms).

Runs ON THE CONTROL HOST but the sampling loop itself runs on the guest as a
detached daemon, so one ssh session covers a multi-hour campaign:

  python3 scripts/m2ctrl_sidecar_sample.py --action start   [--guest-root /tmp/m2ctrl_sampler_20261031]
  python3 scripts/m2ctrl_sidecar_sample.py --action stop --pull-to <local dir>

Every SAMPLE_INTERVAL seconds the daemon appends a timestamped block with:
  * /sys/fs/cgroup/io.cost.model, io.cost.qos   (root cgroup, device-scoped)
  * root io.stat 8:16 line (truncated; shows iocost "cost"/"idle" stats when active)
  * /sys/fs/cgroup/m2_ctrl_20261031_ycsb/io.latency and io.stat 8:16 line
  * /sys/fs/cgroup/m2_ctrl_20261031_fio/io.latency and io.stat 8:16 line
  * kernel iocost warnings since last sample (dmesg delta, deduped)
Timestamps are guest `date +%s.%N`, same clock domain as the fio
log_unix_epoch=1 bandwidth logs, so per-case windows from the extracted
result.json (io_stat_snapshots before_run/after_run) can be intersected.

This is evidence-only: it never writes to any sysfs file.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SSH_ENV = Path("/home/anon/ioext-ssh/ssh_env.sh")

SAMLER_PID_FILE = "/tmp/m2ctrl_sampler_20261031.pid"
SAMPLER_DIR = "/tmp/m2ctrl_sampler_20261031"
SAMPLER_LOG = f"{SAMPLER_DIR}/state.log"

GUEST_LOOP = r"""
set -u
mkdir -p {sampler_dir}
echo $$ > {pid_file}
last_dmesg_mark=$(wc -l < /var/log/kern.log 2>/dev/null || echo 0)
while true; do
  ts=$(date +%s.%N)
  {{
    echo "=== T=$ts"
    echo "-- io.cost.model"
    cat /sys/fs/cgroup/io.cost.model 2>&1
    echo "-- io.cost.qos"
    cat /sys/fs/cgroup/io.cost.qos 2>&1
    echo "-- root io.stat 8:16"
    grep "^8:16 " /sys/fs/cgroup/io.stat 2>/dev/null | cut -c1-260
    echo "-- ycsb io.latency"
    cat /sys/fs/cgroup/m2_ctrl_20261031_ycsb/io.latency 2>&1
    echo "-- ycsb io.stat 8:16"
    grep "^8:16 " /sys/fs/cgroup/m2_ctrl_20261031_ycsb/io.stat 2>/dev/null | cut -c1-260
    echo "-- fio io.latency"
    cat /sys/fs/cgroup/m2_ctrl_20261031_fio/io.latency 2>&1
    echo "-- fio io.stat 8:16"
    grep "^8:16 " /sys/fs/cgroup/m2_ctrl_20261031_fio/io.stat 2>/dev/null | cut -c1-260
    echo "-- dmesg iocost delta"
    dmesg -T 2>/dev/null | grep -iE "iocost|io cost|blk-iocost" | tail -n 4
  }} >> {sampler_log} 2>&1
  sleep {interval}
done
"""


def _ssh_argv(ssh_env: Path, remote_cmd: str) -> list:
    text = ssh_env.read_text()
    def grab(key: str) -> str:
        m = re.search(rf'export {key}="?([^"\n]+)"?', text)
        if not m:
            raise SystemExit(f"{ssh_env}: missing export {key}")
        return m.group(1).strip()
    host, port, user = grab("SSH_HOST"), grab("SSH_PORT"), grab("SSH_USER")
    return ["ssh", "-n", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=/dev/null", "-p", port,
            f"{user}@{host}", remote_cmd]


def _remote_sudo(ssh_env: Path, cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    import base64
    text = ssh_env.read_text()
    sudo_pass = re.search(r'export SUDO_PASS="?([^"\n]+)"?', text).group(1)
    pw_b64 = base64.b64encode((sudo_pass + "\n").encode()).decode()
    cmd_b64 = base64.b64encode(cmd.encode()).decode()
    wrapped = (f"echo {pw_b64} | base64 -d | sudo -S bash -c "
               f"\"echo {cmd_b64} | base64 -d | bash\"")
    return subprocess.run(_ssh_argv(ssh_env, wrapped), text=True, capture_output=True, timeout=timeout)


def start(ssh_env: Path, interval: float) -> None:
    loop = GUEST_LOOP.format(sampler_dir=SAMPLER_DIR, pid_file=SAMLER_PID_FILE,
                             sampler_log=SAMPLER_LOG, interval=interval)
    # Pass the loop via base64 to survive quoting; detach with nohup+setsid so
    # the ssh session can exit while the daemon keeps sampling.
    import base64
    loop_b64 = base64.b64encode(loop.encode()).decode()
    remote = (
        f"rm -f {SAMLER_PID_FILE}; "
        f"echo {loop_b64} | base64 -d > {SAMPLER_DIR}.loop.sh; "
        f"nohup setsid bash {SAMPLER_DIR}.loop.sh >/dev/null 2>&1 < /dev/null & "
        f"sleep 2; test -f {SAMLER_PID_FILE} && echo SAMPLER_UP pid=$(cat {SAMLER_PID_FILE})"
    )
    res = _remote_sudo(ssh_env, remote, timeout=60)
    out = (res.stdout or "") + (res.stderr or "")
    if "SAMPLER_UP" not in out:
        sys.exit(f"BLOCKED: sampler did not start: {out[-800:]}")
    print(out.strip()[-200:])
    # show first sample as a smoke check
    head = subprocess.run(_ssh_argv(ssh_env, f"sleep 1; head -c 1200 {SAMPLER_LOG}"),
                          text=True, capture_output=True, timeout=60)
    print(head.stdout)


def stop(ssh_env: Path, pull_to: str) -> None:
    _remote_sudo(ssh_env, f"pkill -f {SAMPLER_DIR}.loop.sh || true; sleep 1; "
                          f"tail -c 300 {SAMPLER_LOG}", timeout=60)
    if pull_to:
        text = ssh_env.read_text()
        port = re.search(r'export SSH_PORT="?([^"\n]+)"?', text).group(1)
        user = re.search(r'export SSH_USER="?([^"\n]+)"?', text).group(1)
        host = re.search(r'export SSH_HOST="?([^"\n]+)"?', text).group(1)
        dest = Path(pull_to)
        dest.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(["scp", "-P", port, "-o", "StrictHostKeyChecking=accept-new",
                              "-o", "UserKnownHostsFile=/dev/null",
                              f"{user}@{host}:{SAMPLER_LOG}", str(dest / "m2ctrl_iocost_state.log")],
                             text=True, capture_output=True, timeout=300)
        if res.returncode != 0:
            sys.exit(f"BLOCKED: pull failed: {res.stderr[-500:]}")
        print(f"pulled -> {dest / 'm2ctrl_iocost_state.log'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--action", choices=["start", "stop"], required=True)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--pull-to", default="")
    ap.add_argument("--ssh-env", type=Path, default=DEFAULT_SSH_ENV)
    args = ap.parse_args()
    if args.action == "start":
        start(args.ssh_env, args.interval)
    else:
        stop(args.ssh_env, args.pull_to)
    return 0


if __name__ == "__main__":
    sys.exit(main())
