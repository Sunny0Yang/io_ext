#!/usr/bin/env python3
"""Campaign runner for the M1 cache / native-matrix / M2-controller / RQ1
real-app configs (docs/plans/paper_motivation_design.md §0.2).

Control-host orchestrator in the run_motivation_m2_high_remote.sh style:

  source ioext-ssh/ssh_env.sh  (guest 127.0.0.1)
  [1] read-only guest preflight (sdb identity, idleness, executor locks)
  [2] rsync code + configs to the guest deploy root /opt/ioext-bench
  [3] provision bundles: m2 java bundle (YCSB) + m1 bundle (db_bench +
      librocksdb + TailBench shore tree + db-tpcc-1)
  [4] per case = (sequence x repeat, case name <seq>_r<k>):
        flock /tmp/paper_sdb_executor.lock (held for the whole case)
        reset /dev/sdb: umount -> mkfs.ext4 -F -> mount <mount>   (state reset)
        vm.executor --config <cfg> --sequence <seq> --output /tmp/<RUN_ID>/<case>
        tar case (+ monitoring/scene output dirs) ->
            /opt/ioext-bench/psdb_tars/<case>.tar.gz -> scp back to --output

Repeats re-run the SAME sequence N times; the yml is never duplicated. Disk
state reset between repeats is the runner's per-case mkfs+mount; the page-cache
reset required by the cache=on attribution discipline is the scenes'
drop_caches_before_run: true (every cache=on yml in rig_m1_cache keeps it true).

This script never formats anything locally and never runs workloads locally;
every write goes through SSH to the authorized kvmhost guest. It refuses to
touch any device other than /dev/sdb.

Usage:
  python3 scripts/run_m1_cache_campaign.py --campaign m1cacheon --probe
  python3 scripts/run_m1_cache_campaign.py --campaign m1matrix --repeats 3
  python3 scripts/run_m1_cache_campaign.py --campaign m2ctrl --rates 8,16 --repeats 3
  python3 scripts/run_m1_cache_campaign.py --campaign rq1-ycsb --only IO_COST --repeats 3
"""
from __future__ import annotations

import argparse
import base64
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SSH_ENV = Path("/home/anon/ioext-ssh/ssh_env.sh")
GUEST_ROOT = "/opt/ioext-bench"
GUEST_PSDB_TARS = "/opt/ioext-bench/psdb_tars"
EXPECTED_SDB_SIZE = 103079215104  # 96 GiB authorized raw test disk (kvmhost)
LOCK_FILES = ("paper_sdb_executor", "rig_iox_executor", "motivation_m2_executor")

# Local bundle sources (control host) and their guest staging roots.
M2_BUNDLE_TAR = BASE_DIR / "results/motivation_io_behavior_20261029_1793274117/motivation_m2_pilot_20261030_bundle.tar.gz"
M2_BUNDLE_GUEST_ROOT = "/tmp/motivation_m2_pilot_20261030_bundle"
M1_BUNDLE_GUEST = "/tmp/m1_cache_20261031_bundle"
M1_ROCKSDB_SRC = Path("/home/anon/rocksdb")
M1_TAILBENCH_SHORE_SRC = Path("/home/anon/tailbench/tailbench-v0.9/shore")

# Runner-side rsync set: scene code + task/scene configs (assets excluded -- the
# guest checkout already carries the YCSB runtime; __pycache__ never synced).
SYNC_REL_PATHS = [
    "src/vm/scene",
    "src/vm/configs/scene",
    "src/vm/configs/task/isol_bench/rig_m1_cache",
    "src/vm/configs/task/isol_bench/rig_rq1_directio",
    "src/vm/configs/task/isol_bench/rig_rq5_coloc",
    "src/vm/config.py",
    "src/vm/controller.py",
    "src/vm/executor.py",
    "src/vm/evaluator.py",
    "src/vm/explore_tier.py",
    "src/vm/__init__.py",
]

CAMPAIGNS = {
    "m1cacheon": {
        "config": "src/vm/configs/task/isol_bench/rig_m1_cache/m1_cache_on.yml",
        "mount": "/mnt/m1_cache_20261031",
        "tmp_root": "/tmp/m1_cache_20261031",
        "bundle": "m1",
        "results_root": "results/motivation_io_behavior_20261029_1793274117/remote_runs",
    },
    "m1cacheab": {
        "config": "src/vm/configs/task/isol_bench/rig_m1_cache/m1_cache_ab.yml",
        "mount": "/mnt/m1_cache_20261031",
        "tmp_root": "/tmp/m1_cache_20261031",
        "bundle": "m1",
        "results_root": "results/motivation_io_behavior_20261029_1793274117/remote_runs",
    },
    "m1matrix": {
        "config": "src/vm/configs/task/isol_bench/rig_m1_cache/m1_native_matrix_cacheon.yml",
        "mount": "/mnt/m1_cache_20261031",
        "tmp_root": "/tmp/m1_cache_20261031",
        "bundle": "m1",
        "results_root": "results/motivation_io_behavior_20261029_1793274117/remote_runs",
    },
    "m2ctrl": {
        "config": "src/vm/configs/task/isol_bench/rig_m1_cache/m2_controller_arms.yml",
        "mount": "/mnt/m2_ctrl_20261031",
        "tmp_root": "/tmp/m2_ctrl_20261031",
        "bundle": "java",
        "results_root": "results/motivation_io_behavior_20261029_1793274117/remote_runs",
    },
    "rq1-ycsb": {
        "config": "src/vm/configs/task/isol_bench/rig_m1_cache/rq1_realapps_ycsb.yml",
        "mount": "/mnt/rq1_20261031",
        "tmp_root": "/tmp/rq1_20261031",
        "bundle": "m1",
        "results_root": "results/rq1_realapps/ycsb",
    },
    "rq1-dbbench": {
        "config": "src/vm/configs/task/isol_bench/rig_m1_cache/rq1_realapps_dbbench.yml",
        "mount": "/mnt/rq1_20261031",
        "tmp_root": "/tmp/rq1_20261031",
        "bundle": "m1",
        "results_root": "results/rq1_realapps/dbbench",
    },
    "rq1-shore": {
        "config": "src/vm/configs/task/isol_bench/rig_m1_cache/rq1_realapps_shore.yml",
        "mount": "/mnt/rq1_20261031",
        "tmp_root": "/tmp/rq1_20261031",
        "bundle": "m1",
        "results_root": "results/rq1_realapps/shore",
    },
    # RQ1 cache=off (direct-I/O) rounds, device-adapted configs:
    # src/vm/configs/task/isol_bench/rig_rq1_directio/.
    "rq1dio-ycsb": {
        "config": "src/vm/configs/task/isol_bench/rig_rq1_directio/rq1_directio_ycsb.yml",
        "mount": "/mnt/rq1dio_20261102",
        "tmp_root": "/tmp/rq1dio_20261102",
        "bundle": "m1",
        "results_root": "results/rq1_realapps_directio_ycsb",
    },
    "rq1dio-dbbench": {
        "config": "src/vm/configs/task/isol_bench/rig_rq1_directio/rq1_directio_dbbench.yml",
        "mount": "/mnt/rq1dio_20261102",
        "tmp_root": "/tmp/rq1dio_20261102",
        "bundle": "m1",
        "results_root": "results/rq1_realapps_directio_dbbench",
    },
    "rq1dio-shore": {
        "config": "src/vm/configs/task/isol_bench/rig_rq1_directio/rq1_directio_shore.yml",
        "mount": "/mnt/rq1dio_20261102",
        "tmp_root": "/tmp/rq1dio_20261102",
        "bundle": "m1",
        "case_setup": "cp -f /tmp/m1_cache_20261031_bundle/data/db-tpcc-1 /mnt/rq1dio_20261102/db-tpcc-1",
        "results_root": "results/rq1_realapps_directio_shore",
    },
    # RQ5 LC/BE co-location campaigns (docs/plans/rq5_realapp_coloc_experiment.md §2); bundle "m1" covers both runtimes: YCSB java-21 (ensure_m2_java_bundle, unconditional) + db_bench (ensure_m1_bundle).
    "rq5a-coloc": {
        "config": "src/vm/configs/task/isol_bench/rig_rq5_coloc/rq5a_interfere.yml",
        "mount": "/mnt/rq5_coloc",
        "tmp_root": "/tmp/rq5_coloc",
        "bundle": "m1",
        "results_root": "results/rq5_realapps_coloc/a_interfere",
    },
    "rq5b-coloc": {
        "config": "src/vm/configs/task/isol_bench/rig_rq5_coloc/rq5b_policy_matrix.yml",
        "mount": "/mnt/rq5_coloc",
        "tmp_root": "/tmp/rq5_coloc",
        "bundle": "m1",
        "results_root": "results/rq5_realapps_coloc/b_policy",
    },
    "rq5c-coloc": {
        "config": "src/vm/configs/task/isol_bench/rig_rq5_coloc/rq5c_dynamic.yml",
        "mount": "/mnt/rq5_coloc",
        "tmp_root": "/tmp/rq5_coloc",
        "bundle": "m1",
        "results_root": "results/rq5_realapps_coloc/c_dynamic",
    },
}


def die(msg: str) -> "None":
    print(f"BLOCKED: {msg}", file=sys.stderr)
    sys.exit(2)


class SshEnv:
    """Minimal python mirror of archives/.../ssh_env.sh (ssh/scp/rsync/remote_sudo)."""

    def __init__(self, path: Path):
        text = path.read_text()
        def grab(key: str) -> str:
            m = re.search(rf'export {key}="?([^"\n]+)"?', text)
            if not m:
                die(f"{path}: missing export {key}")
            return m.group(1).strip()
        self.host = grab("SSH_HOST")
        self.port = grab("SSH_PORT")
        self.user = grab("SSH_USER")
        self.sudo_pass = grab("SUDO_PASS")
        self.ssh_opts = ["-o", "StrictHostKeyChecking=accept-new",
                         "-o", "UserKnownHostsFile=/dev/null",
                         "-o", "ServerAliveInterval=15",
                         "-o", "ServerAliveCountMax=8"]

    def _ssh_argv(self, remote_cmd: str) -> list:
        return ["ssh", "-n", *self.ssh_opts, "-p", self.port,
                f"{self.user}@{self.host}", remote_cmd]

    def remote(self, cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
        # Capture output: callers match on probe.stdout ("PRESENT"), which is
        # always None with inherited streams (runner bug fix 2026-10-31 -- it
        # made every run re-upload the guest bundles).
        return subprocess.run(self._ssh_argv(cmd), text=True, capture_output=True, timeout=timeout)

    def remote_output(self, cmd: str, timeout: int = 120) -> str:
        res = subprocess.run(self._ssh_argv(cmd), text=True, capture_output=True, timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError(f"remote command failed rc={res.returncode}: {cmd}\n{res.stderr}")
        return res.stdout

    def remote_sudo(self, cmd: str, timeout: int = 3600) -> subprocess.CompletedProcess:
        # Same double-base64 dance as ssh_env.sh remote_sudo().
        # Capture output: callers inspect stdout (rq5 reset_guest matches
        # 'unmounted'/'enable=0'); always None with inherited streams -- same
        # runner bug class fixed on remote() 2026-10-31.
        pw_b64 = base64.b64encode((self.sudo_pass + "\n").encode()).decode()
        cmd_b64 = base64.b64encode(cmd.encode()).decode()
        wrapped = (f"echo {pw_b64} | base64 -d | sudo -S bash -c "
                   f"\"echo {cmd_b64} | base64 -d | bash\"")
        return subprocess.run(self._ssh_argv(wrapped), text=True, timeout=timeout,
                              capture_output=True)

    def rsync(self, srcs: list, dest: str, timeout: int = 3600,
              dereference: bool = False) -> subprocess.CompletedProcess:
        # dereference=True (-L): copy symlink TARGETS as real files. Needed for
        # the m1 bundle hostlibs, whose .so symlinks point at control-host
        # absolute paths (anaconda) and would arrive broken on the guest
        # (db_bench rc=127 / libgflags not found -- fixed 2026-10-31).
        argv = ["rsync", "-avz", "-L" if dereference else "", "-e",
                "ssh " + " ".join(self.ssh_opts) + f" -p {self.port}",
                *srcs, f"{self.user}@{self.host}:{dest}"]
        return subprocess.run([a for a in argv if a], text=True, timeout=timeout)

    def scp(self, remote_path: str, local_path: Path, timeout: int = 1800) -> subprocess.CompletedProcess:
        argv = ["scp", *self.ssh_opts, "-P", self.port,
                f"{self.user}@{self.host}:{remote_path}", str(local_path)]
        return subprocess.run(argv, text=True, timeout=timeout)


def load_sequences(config_path: Path) -> list:
    try:
        import yaml
    except ImportError:
        die("PyYAML is required on the control host to enumerate sequences")
    cfg = yaml.safe_load(config_path.read_text())
    return [str(seq["sequence_id"]) for seq in cfg.get("test_sequences", [])]


def list_repo_changes_for_sync() -> list:
    existing = []
    for rel in SYNC_REL_PATHS:
        path = BASE_DIR / rel
        if not path.exists():
            die(f"sync source missing: {path}")
        existing.append(rel)
    return existing


def preflight(ssh: SshEnv) -> str:
    checks = [
        'test "$(hostname)" = target-host',
        "test -b /dev/sdb",
        f'test "$(lsblk -dnbo SIZE /dev/sdb | tr -d " ")" = {EXPECTED_SDB_SIZE}',
        'test -z "$(findmnt -rn -S /dev/sdb -o TARGET || true)"',
        'test -z "$(find /sys/class/block/sdb/holders -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)"',
        'test "$(findmnt -rn -T / -o SOURCE)" != /dev/sdb',
        'test -z "$(pgrep -x fio || true)"',
        'test -z "$(pgrep -x java || true)"',
        'test -z "$(pgrep -x db_bench || true)"',
    ]
    for name in LOCK_FILES:
        checks.append(f"flock -n /tmp/{name}.lock -c true")
    checks.append('printf "HOST=%s KERNEL=%s\\n" "$(hostname)" "$(uname -r)"')
    cmd = "set -e;\n" + ";\n".join(checks)
    res = ssh.remote(cmd, timeout=60)
    if res.returncode != 0:
        die(f"guest preflight failed rc={res.returncode} (see docs: sdb must be idle, "
            "unmounted, and every executor lock free)")
    return res.stdout or ""


def ensure_code_sync(ssh: SshEnv, skip: bool) -> None:
    if skip:
        print("=== [2] code sync skipped (--skip-sync) ===")
        return
    print("=== [2] rsync code + configs to guest ===")
    srcs = list_repo_changes_for_sync()
    # --relative preserves the src/vm tree layout under the guest deploy root.
    argv = ["rsync", "-avz", "--exclude", "__pycache__", "--relative", "-e",
            "ssh " + " ".join(ssh.ssh_opts) + f" -p {ssh.port}",
            *srcs, f"{ssh.user}@{ssh.host}:{GUEST_ROOT}/"]
    res = subprocess.run(argv, text=True, timeout=3600, cwd=BASE_DIR)
    if res.returncode != 0:
        die(f"code rsync failed rc={res.returncode}")


def ensure_m2_java_bundle(ssh: SshEnv, skip: bool) -> None:
    java_bin = f"{M2_BUNDLE_GUEST_ROOT}/motivation_m2_pilot_20261030/runtime/java-21-openjdk-amd64/bin/java"
    probe = ssh.remote(f"test -x {shlex.quote(java_bin)} && echo PRESENT || echo MISSING", timeout=30)
    if "PRESENT" in (probe.stdout or ""):
        print("=== [3] m2 java bundle present on guest ===")
        return
    if skip:
        die("m2 java bundle missing on guest and --skip-bundle given")
    if not M2_BUNDLE_TAR.is_file():
        die(f"m2 bundle tar missing locally: {M2_BUNDLE_TAR}")
    print("=== [3] uploading m2 java bundle ===")
    tmp_tar = f"/tmp/{M2_BUNDLE_TAR.name}"
    res = ssh.rsync([str(M2_BUNDLE_TAR)], tmp_tar, timeout=3600)
    if res.returncode != 0:
        die("m2 bundle upload failed")
    script = (f"set -e; echo '{_sha256_local(M2_BUNDLE_TAR)}  {tmp_tar}' | sha256sum -c -; "
              f"rm -rf {M2_BUNDLE_GUEST_ROOT}; mkdir -p {M2_BUNDLE_GUEST_ROOT}; "
              f"tar xzf {tmp_tar} -C {M2_BUNDLE_GUEST_ROOT}; rm -f {tmp_tar}")
    res = ssh.remote_sudo(script, timeout=3600)
    if res.returncode != 0:
        die("m2 bundle extraction failed on guest")


def _sha256_local(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_m1_bundle(ssh: SshEnv, skip: bool) -> None:
    probe = ssh.remote(
        f"test -x {M1_BUNDLE_GUEST}/rocksdb/db_bench && "
        f"test -f {M1_BUNDLE_GUEST}/tailbench/tailbench-v0.9/shore/shore-kits/shore_kits_integrated && "
        f"test -f {M1_BUNDLE_GUEST}/data/db-tpcc-1 && echo PRESENT || echo MISSING",
        timeout=30,
    )
    if "PRESENT" in (probe.stdout or ""):
        print("=== [3] m1 bundle present on guest ===")
        return
    if skip:
        die("m1 bundle missing on guest and --skip-bundle given")
    for src in (M1_ROCKSDB_SRC / "db_bench", M1_ROCKSDB_SRC / "librocksdb.so.11.2.0",
                M1_ROCKSDB_SRC / "hostlibs", M1_TAILBENCH_SHORE_SRC):
        if not src.exists():
            die(f"m1 bundle source missing on control host: {src}")
    print("=== [3] provisioning m1 bundle (db_bench + librocksdb + tailbench shore) ===")
    prep = ssh.remote_sudo(
        f"rm -rf {M1_BUNDLE_GUEST}/staging; "
        f"mkdir -p {M1_BUNDLE_GUEST}/staging {M1_BUNDLE_GUEST}/rocksdb "
        f"{M1_BUNDLE_GUEST}/tailbench/tailbench-v0.9 {M1_BUNDLE_GUEST}/data; "
        f"chown -R {ssh.user}: {M1_BUNDLE_GUEST}",
        timeout=60,
    )
    if prep.returncode != 0:
        die("m1 bundle staging dirs could not be created on guest")
    res = ssh.rsync(
        [
            str(M1_ROCKSDB_SRC / "db_bench"),
            str(M1_ROCKSDB_SRC / "librocksdb.so"),
            str(M1_ROCKSDB_SRC / "librocksdb.so.11"),
            str(M1_ROCKSDB_SRC / "librocksdb.so.11.2"),
            str(M1_ROCKSDB_SRC / "librocksdb.so.11.2.0"),
            str(M1_ROCKSDB_SRC / "hostlibs"),
            str(M1_TAILBENCH_SHORE_SRC),
        ],
        f"{M1_BUNDLE_GUEST}/staging/",
        timeout=7200,
        dereference=True,
    )
    if res.returncode != 0:
        die("m1 bundle rsync failed")
    script = (
        "set -e; "
        f"mkdir -p {M1_BUNDLE_GUEST}/rocksdb {M1_BUNDLE_GUEST}/tailbench/tailbench-v0.9 {M1_BUNDLE_GUEST}/data; "
        f"mv {M1_BUNDLE_GUEST}/staging/db_bench {M1_BUNDLE_GUEST}/staging/librocksdb.so* "
        f"{M1_BUNDLE_GUEST}/staging/hostlibs {M1_BUNDLE_GUEST}/rocksdb/; "
        f"mv {M1_BUNDLE_GUEST}/staging/shore {M1_BUNDLE_GUEST}/tailbench/tailbench-v0.9/; "
        f"mkdir -p {M1_BUNDLE_GUEST}/data; "
        f"cp {M1_BUNDLE_GUEST}/tailbench/tailbench-v0.9/shore/db-tpcc-1 {M1_BUNDLE_GUEST}/data/db-tpcc-1; "
        f"rm -rf {M1_BUNDLE_GUEST}/staging; "
        f"test -x {M1_BUNDLE_GUEST}/rocksdb/db_bench"
    )
    res = ssh.remote_sudo(script, timeout=1800)
    if res.returncode != 0:
        die("m1 bundle staging failed on guest")


def case_script(cfg_rel: str, seq: str, run_id: str, case_name: str, mount: str, tmp_root: str, case_setup: str = "") -> str:
    case_dir = f"/tmp/{run_id}/{case_name}"
    config = f"{GUEST_ROOT}/{cfg_rel}"
    return "\n".join(
        [
            "set -e",
            # Self-heal the executor locks: with fs.protected_regular=2 (Ubuntu
            # default), root CANNOT re-open a lock file owned by a regular user
            # in sticky /tmp (observed 2026-11-04: one non-sudo run left a
            # user-owned paper_sdb_executor.lock and every later case died with
            # "exec 9>...: Permission denied"). Recreate as root before flock.
            "rm -f /tmp/paper_sdb_executor.lock /tmp/rig_iox_executor.lock "
            "/tmp/motivation_m2_executor.lock",
            # Hold the paper_sdb executor lock for the whole case; fail fast if busy.
            "exec 9>/tmp/paper_sdb_executor.lock",
            "flock -n 9 || { echo 'LOCK_BUSY paper_sdb_executor'; exit 3; }",
            "exec 8>/tmp/rig_iox_executor.lock; flock -n 8 || exit 3",
            "exec 7>/tmp/motivation_m2_executor.lock; flock -n 7 || exit 3",
            "test -z \"$(pgrep -x 'fio|java|db_bench|shore_kits_integrated' || true)\"",
            "test $(lsblk -dnbo SIZE /dev/sdb) = 103079215104",
            "test $(df -Pk /tmp | tail -1 | tr -s ' ' | cut -d' ' -f4) -gt 1048576",
            # State reset: fresh ext4 on the authorized raw test disk per case.
            "for mp in $(findmnt -rn -S /dev/sdb -o TARGET || true); do",
            '  case "$mp" in /mnt/*) umount "$mp";; *) echo "REFUSE_TO_UMOUNT $mp"; exit 4;; esac;',
            "done",
            f"mkdir -p /tmp/{run_id}",
            # NOTE: case_dir itself must stay EMPTY until the executor creates it --
            # the controller refuses non-empty custom result dirs, so mkfs.log goes
            # to a SIBLING file and is copied into case_dir only post-run (tar).
            f"mkfs.ext4 -F -E lazy_itable_init=0,lazy_journal_init=0 /dev/sdb > /tmp/{run_id}/{case_name}.mkfs.log 2>&1",
            f"mkdir -p {mount}",
            f"mount -o noatime,nodiratime /dev/sdb {mount}",
            *( [case_setup] if case_setup else [] ),
            # Fresh per-case monitoring/scene roots (group-id templated output dirs
            # are reused across repeats; the case tar snapshots them per case).
            f"rm -rf {tmp_root}/monitor {tmp_root}/scene",
            f"mkdir -p {tmp_root}/monitor {tmp_root}/scene",
            f"cd {GUEST_ROOT}",
            "rc=0",
            f"PERF_ARENA_WORKSPACE={GUEST_ROOT} PYTHONPATH={GUEST_ROOT}/src "
            f"python3 -m vm.executor --config {config} --sequence {shlex.quote(seq)} "
            f"--output {case_dir} --log-level INFO || rc=$?",
            # Snapshot monitor/scene outputs into the case dir before tarring.
            f"cp -a {tmp_root}/monitor {tmp_root}/scene {case_dir}/ 2>/dev/null || true",
            f"cp -f /tmp/{run_id}/{case_name}.mkfs.log {case_dir}/mkfs.log 2>/dev/null || true",
            f"mkdir -p {GUEST_PSDB_TARS}",
            f"tar czf {GUEST_PSDB_TARS}/{case_name}.tar.gz -C /tmp/{run_id} {case_name}",
            f'echo "CASE_RC=$rc"',
            "exit $rc",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True, choices=sorted(CAMPAIGNS))
    parser.add_argument("--repeats", type=int, default=1, help="repeats per sequence (case <seq>_r<k>)")
    parser.add_argument("--only", default="", help="comma-separated sequence-id substrings to keep")
    parser.add_argument("--rates", default="", help="m2ctrl only: offered FIO rates, e.g. 8,16 -> sequence substring _8M/_16M")
    parser.add_argument("--probe", action="store_true", help="single probe case: first matching sequence, one run")
    parser.add_argument("--output", type=Path, default=None, help="local results dir (default: campaign results_root/<RUN_ID>)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--ssh-env", type=Path, default=DEFAULT_SSH_ENV)
    parser.add_argument("--skip-sync", action="store_true", help="skip the code/config rsync (guest already current)")
    parser.add_argument("--skip-bundle", action="store_true", help="fail instead of provisioning missing guest bundles")
    parser.add_argument("--extract", action="store_true", help="extract pulled tars under --output")
    parser.add_argument("--keep-going", action="store_true", help="continue with next case after a failure")
    parser.add_argument("--list", action="store_true", help="list sequences and exit")
    args = parser.parse_args()

    camp = CAMPAIGNS[args.campaign]
    config_path = BASE_DIR / camp["config"]
    if not config_path.is_file():
        die(f"missing task config: {config_path}")
    if not args.ssh_env.is_file():
        die(f"missing ssh env: {args.ssh_env}")
    if args.repeats < 1:
        die("--repeats must be >= 1")

    sequences = load_sequences(config_path)
    filters = [t.strip() for t in args.only.split(",") if t.strip()]
    if args.rates:
        if args.campaign != "m2ctrl":
            die("--rates only applies to --campaign m2ctrl")
        filters += [f"_{int(r)}M" for r in args.rates.split(",") if r.strip()]
    if filters:
        sequences = [s for s in sequences if any(f in s for f in filters)]
        if not sequences:
            die(f"--only/--rates matched no sequences; available: "
                f"{load_sequences(config_path)}")
    if args.probe:
        sequences = sequences[:1]

    if args.list:
        for seq in load_sequences(config_path):
            marker = "*" if not filters or any(f in seq for f in filters) else " "
            print(f"{marker} {seq}")
        return 0

    run_id = args.run_id or f"{args.campaign}_{time.strftime('%Y%m%d_%H%M%S')}"
    output = args.output or (BASE_DIR / camp["results_root"] / run_id)
    if output.exists() and any(output.iterdir()):
        die(f"output dir not empty: {output}")
    output.mkdir(parents=True)

    ssh = SshEnv(args.ssh_env)
    print(f"=== [1] preflight guest {ssh.host} (campaign={args.campaign}) ===")
    print(preflight(ssh))

    ensure_code_sync(ssh, args.skip_sync)
    ensure_m2_java_bundle(ssh, args.skip_bundle)
    if camp["bundle"] == "m1":
        ensure_m1_bundle(ssh, args.skip_bundle)

    cases = []
    for rep in range(1, args.repeats + 1):
        for seq in sequences:
            cases.append((seq, f"{seq}_r{rep}"))
    if args.probe:
        cases = [(cases[0][0], f"{cases[0][0]}_r_probe")]

    meta = {
        "campaign": args.campaign,
        "config": camp["config"],
        "run_id": run_id,
        "guest_host": ssh.host,
        "mount": camp["mount"],
        "repeats": args.repeats,
        "only": args.only,
        "rates": args.rates,
        "probe": args.probe,
        "sequences": [s for s, _ in cases],
        "cases": [c for _, c in cases],
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (output / "campaign.json").write_text(__import__("json").dumps(meta, indent=2))

    failures = []
    for idx, (seq, case_name) in enumerate(cases):
        print(f"=== [4] case {idx + 1}/{len(cases)}: {case_name} ===", flush=True)
        script = case_script(camp["config"], seq, run_id, case_name, camp["mount"], camp["tmp_root"],
                             case_setup=camp.get("case_setup", ""))
        res = ssh.remote_sudo(script, timeout=4 * 3600)
        print(res.stdout[-4000:] if res.stdout else "", end="")
        if res.returncode != 0:
            print(f">>> CASE FAIL {case_name} rc={res.returncode} stderr={(res.stderr or '')[-2000:]}", flush=True)
            failures.append(case_name)
            if not args.keep_going:
                break
        remote_tar = f"{GUEST_PSDB_TARS}/{case_name}.tar.gz"
        pull = ssh.scp(remote_tar, output / f"{case_name}.tar.gz", timeout=1800)
        if pull.returncode != 0:
            print(f">>> PULL FAIL {case_name}")
            failures.append(case_name + ":pull")
            if not args.keep_going:
                break
        if args.extract:
            subprocess.run(["tar", "xzf", str(output / f"{case_name}.tar.gz"), "-C", str(output)], check=False)
        print(f"=== case done: {case_name} rc={res.returncode} ===", flush=True)

    (output / "campaign.json").write_text(
        __import__("json").dumps({**meta, "failures": failures,
                                  "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                 indent=2))
    print(f"campaign {args.campaign} finished: {len(cases) - len(failures)}/{len(cases)} cases ok; output={output}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
