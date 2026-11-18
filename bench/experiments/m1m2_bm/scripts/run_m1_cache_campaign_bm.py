#!/usr/bin/env python3
"""Campaign runner for the M1 cache / cache A-B / native-matrix experiments on
bare-metal bm (ssh alias `bm`, hostname user, kernel 7.3.1-ioext*, single NVMe
nvme0n1 = the root disk). Control-host orchestrator, bm adaptation of
scripts/run_m1_cache_campaign.py (kvmhost sdb version).

bm caliber (experiments/m1m2_bm/LEDGER.md decision 1 -- root-disk data-dir
caliber). Differences vs the kvmhost runner are deliberate and ALL of them:

  kvmhost (sdb)                          bm (nvme0n1 root disk)
  ------------------------------------  ------------------------------------
  per case: umount -> mkfs.ext4 -F     per case: rm -rf + mkdir the data dirs
  -> mount /mnt/m1_cache_20261031      /opt/ioext/data/m1_cache/
                                       {ycsb_db,dbbench_db,shore_scratch}
                                       (user-owned, no sudo) + drop_caches;
                                       NEVER touches the Shore input file
                                       staging/m1_cache_bm_bundle/data/db-tpcc-1
  /dev/sdb raw disk (authorized 96G)   NO block-device writes at runner level
                                       (no mkfs/umount/mount anywhere)
  whole case as root (remote_sudo)     whole case as user; the ONLY privileged
                                       step is drop_caches via NOPASSWD
                                       `sudo -n bash -c` (scenes' internal
                                       run_sudo_shell also NOPASSWD)
  executor lock /tmp/paper_sdb_...     campaign lock /opt/ioext/
                                       .m1_campaign.lock (flock: instant try,
                                       then wait for the holder)
  guest root /opt/ioext-bench      deploy root /opt/ioext
  output /tmp/<RUN_ID>/<case>          output /tmp/m1_cache_bm/<RUN_ID>/<case>
  python3 (system)                     /opt/ioext/.venv/bin/python
                                       (cwd /opt/ioext/src)

Result layout (analysis compatibility -- do not change):
  - case name = <sequence_id>_r<k> (probe: _r_probe) -- identical to kvmhost so
    the CSV regexes keep matching (m1_cache_cases.py CASE_RE,
    M1MATRIX_*_r<k> for the matrix CSVs).
  - case dir = /tmp/m1_cache_bm/<RUN_ID>/<case>/ with step_0/, monitor/ and
    scene/ snapshots copied in before tarring -- the exact layout
    results/motivation_m1_cache_20261031/tools/m1_cache_cases.py and the
    native-matrix summary tool walk (run_results.json + step_0/*/scene JSON +
    monitor/system_metric + monitor/m1_pattern_sampler).
  - local pull: experiments/m1m2_bm/runs/m1_campaign/<RUN_ID>/<case>.tar.gz
    for m1cacheon/m1nativematrix, and .../<RUN_ID>/ab/<case>.tar.gz for
    m1cacheab -- run m1cacheon and m1cacheab with the SAME --run-id and the
    run dir is exactly the --results-root layout m1_cache_cases.py expects
    (cache-on cases at the root, A/B cases under ab/).
  - The analysis side (m1_cache_cases.py) hardcodes dev "sdb" inside
    inflight_stats()/disk_mb_s(); the bm pass needs device nvme0n1
    parameterized there (separate task; noted in the LEDGER risk list).

Per-case remote chain (all under one ssh, base64-wrapped, as user):
  flock <lock> (instant try, else wait)
  -> idleness guard (no fio/java/db_bench/shore_kits_integrated)
  -> free-space guard on / and /tmp
  -> rm -rf + mkdir the three data dirs (state reset part 1)
  -> sudo -n drop_caches (state reset part 2; scenes drop again around the
     measured windows -- attribution discipline unchanged)
  -> rm -rf stale monitor/scene roots + any old case dir/tar of THIS case
  -> cd /opt/ioext/src && .venv/bin/python -m vm.executor
     --config src/vm/configs/task/m1m2_bm/<cfg> --sequence <seq>
     --output /tmp/m1_cache_bm/<RUN_ID>/<seq>_r<k>
  -> cp -a monitor/ scene/ into the case dir -> tar -> CASE_RC
  then scp the tar back and record the case status in campaign.json
  (--resume skips cases already marked ok and pulled).

Safety: this script NEVER runs mkfs/mount/umount anywhere, never writes to
any block device, and every remote command is executed as user user (the only
privileged bit is the NOPASSWD drop_caches inside the case chain; if NOPASSWD
is missing the case fails closed instead of silently skipping the drop).
The driver itself never needs the sudo password (ssh_env_bm.sh SUDO_PASS is
never read or printed by this script).

Usage:
  python3 experiments/m1m2_bm/scripts/run_m1_cache_campaign_bm.py \
      --campaign m1cacheon --probe
  python3 experiments/m1m2_bm/scripts/run_m1_cache_campaign_bm.py \
      --campaign m1cacheon --run-id m1_cache_bm_formal --repeats 3
  python3 experiments/m1m2_bm/scripts/run_m1_cache_campaign_bm.py \
      --campaign m1cacheab --run-id m1_cache_bm_formal --repeats 3
  python3 experiments/m1m2_bm/scripts/run_m1_cache_campaign_bm.py \
      --campaign m1nativematrix --repeats 3
  python3 experiments/m1m2_bm/scripts/run_m1_cache_campaign_bm.py \
      --campaign rq5b86-coloc --probe --only RQ5B86_NONE_LOW
Options: --dry-run prints every remote command without executing anything;
--list prints the sequences; --resume reuses the existing run dir (skipping
ok cases); --fail-fast stops at the first failure (default: record and
continue); --only <substr,substr> filters sequences.

RQ5 bm campaigns (appended 2026-11-05, m1 entries untouched): rq5a86-coloc /
rq5b86-coloc / rq5c86-coloc run the rigs emitted by scripts/
gen_rq5_coloc_yamls.py --target bm (sequence ids RQ5A86_*/RQ5B86_*/RQ5C86_*,
full kvmhost paper caliber, device nvme0n1). Differences vs the m1 campaigns,
all carried in the CAMPAIGNS entries:
  * data dirs: /opt/ioext/data/rq5_coloc/{lc,be} (data_root +
    data_dirs keys) -- reset per case with the SAME root-ownership semantics
    (sudo -n rm -rf, then user mkdir);
  * lock: the same /opt/ioext/.m1_campaign.lock (explicit "lock"
    key) so rq5 and m1/m2 cases serialize against each other;
  * results_root: default local output results/rq5_realapps_coloc_bm/
    {a_interfere,b_policy,c_dynamic}/<run-id> (--output still overrides);
  * the rigs' scene/monitor output dirs are baked to /tmp/m1_cache_bm/
    {scene,monitor} == this runner's per-case snapshot roots, so the
    standard case chain captures them with NO change;
  * the rig ymls are shipped by SYNC_REL_PATHS (src/vm/configs/task/
    isol_bench/rig_rq5_coloc/bm), not by the m1m2_bm configs/ rsync;
  * preflight does NOT check the io-ext loaders (/opt/ioext/
    io-ext/userspace/loader_*): deploying them is a separate task; until
    then the iox arms fail fast at scene start (fail-closed, visible).
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[3]                   # repo root
EXP_DIR = Path(__file__).resolve().parents[1]                    # experiments/m1m2_bm
DEFAULT_SSH_ENV = Path(__file__).resolve().parent / "ssh_env_bm.sh"

# --- bm remote layout ---------------------------------------------------------
DEPLOY_ROOT = "/opt/ioext"
REMOTE_CFG_DIR = f"{DEPLOY_ROOT}/src/vm/configs/task/m1m2_bm"
REMOTE_TMP_ROOT = "/tmp/m1_cache_bm"                 # executor output base (per spec)
REMOTE_LOCK = f"{DEPLOY_ROOT}/.m1_campaign.lock"
REMOTE_DATA_ROOT = f"{DEPLOY_ROOT}/data/m1_cache"
REMOTE_VENV_PY = f"{DEPLOY_ROOT}/.venv/bin/python"
REMOTE_SRC = f"{DEPLOY_ROOT}/src"
REMOTE_EXECUTOR_CWD = REMOTE_SRC

# data dirs reset per case (state reset part 1). NEVER include the Shore
# read-only input (staging/m1_cache_bm_bundle/data/db-tpcc-1) here.
DATA_DIRS = [
    f"{REMOTE_DATA_ROOT}/ycsb_db",
    f"{REMOTE_DATA_ROOT}/dbbench_db",
    f"{REMOTE_DATA_ROOT}/shore_scratch",
]

# RQ5 LC/BE co-location campaigns on bm (docs/plans/
# rq5_realapp_coloc_experiment.md; rigs emitted by scripts/
# gen_rq5_coloc_yamls.py --target bm). The rigs live in the shared repo rig
# dir (shipped by SYNC_REL_PATHS, NOT by the m1m2_bm configs/ rsync) and their
# scene/monitor outputs are baked to /tmp/m1_cache_bm/{scene,monitor} == the
# runner snapshot roots, so the standard case chain captures them unchanged.
RQ5_RIG_REL = "src/vm/configs/task/isol_bench/rig_rq5_coloc/bm"
REMOTE_RQ5_CFG_DIR = f"{DEPLOY_ROOT}/{RQ5_RIG_REL}"
REMOTE_RQ5_DATA_ROOT = f"{DEPLOY_ROOT}/data/rq5_coloc"
RQ5_DATA_DIRS = [
    f"{REMOTE_RQ5_DATA_ROOT}/lc",
    f"{REMOTE_RQ5_DATA_ROOT}/be",
]

# Runner-side rsync set (bare-metal deploy root mirrors the repo layout). The bm task
# configs are rsynced separately into REMOTE_CFG_DIR so the executor resolves
# base_scene paths against <deploy>/src/vm/configs (config.py computes
# config_root from the "src/vm/configs" path segment).
SYNC_REL_PATHS = [
    "src/vm/scene",
    "src/vm/configs/scene",
    "src/vm/config.py",
    "src/vm/controller.py",
    "src/vm/executor.py",
    "src/vm/evaluator.py",
    "src/vm/explore_tier.py",
    "src/vm/__init__.py",
    # RQ5 bm rigs (rq5a86/rq5b86/rq5cbm_*.yml): the rq5*86-coloc campaigns
    # execute these in place (config_root derives from the src/vm/configs
    # path segment), so the rsync must ship the bm/ rig dir.
    RQ5_RIG_REL,
]

CASE_TIMEOUT_SEC = 4 * 3600     # one executor case (worst: ycsb load + 600s ROI)
PULL_TIMEOUT_SEC = 1800         # one tar scp
PREFLIGHT_TIMEOUT_SEC = 60
SYNC_TIMEOUT_SEC = 3600
MIN_ROOT_FREE_KB = 10 * 1024 * 1024   # 10 GiB free on / for the data dirs

CAMPAIGNS = {
    "m1cacheon": {
        "config": "experiments/m1m2_bm/configs/m1_cache_on_bm.yml",
        "remote_config": f"{REMOTE_CFG_DIR}/m1_cache_on_bm.yml",
        "ab_subdir": False,
        "schedulers": ["none"],
        "needs_bpftrace": True,     # m1_pattern_sampler monitoring scene
    },
    "m1cacheab": {
        "config": "experiments/m1m2_bm/configs/m1_cache_ab_bm.yml",
        "remote_config": f"{REMOTE_CFG_DIR}/m1_cache_ab_bm.yml",
        "ab_subdir": True,          # cases pulled under <run>/ab/ (analysis layout)
        "schedulers": ["none"],
        "needs_bpftrace": False,    # A/B arm runs ib_system_metric only
    },
    "m1nativematrix": {
        "config": "experiments/m1m2_bm/configs/m1_native_matrix_cacheon_bm.yml",
        "remote_config": f"{REMOTE_CFG_DIR}/m1_native_matrix_cacheon_bm.yml",
        "ab_subdir": False,
        "schedulers": ["none", "mq-deadline", "kyber", "bfq"],
        "needs_bpftrace": False,    # matrix steps monitor ib_system_metric only
    },
    # ---- RQ5 LC/BE co-location campaigns (appended 2026-11-05) --------------
    # Schema = the m1 entries plus the per-campaign keys the rq5 flow needs:
    #   data_root/data_dirs: per-case state reset (same root-ownership
    #     semantics as DATA_DIRS: sudo -n rm -rf, then user mkdir -- scenes
    #     run db_bench/YCSB through NOPASSWD sudo and leave root-owned files).
    #   lock: the SAME campaign lock as the m1 campaigns (their case chain
    #     flock -n's it and waits for the holder; rq5 and m1 cases therefore
    #     serialize against each other).
    #   results_root: default LOCAL output root for this campaign (under the
    #     repo root); --output still overrides it.
    # Preflight does NOT check the io-ext loaders: the iox arms reference
    # /opt/ioext/io-ext/userspace/loader_* and deploying them is
    # a SEPARATE task; until then iox cases fail fast at scene start
    # (fail_fast_on_scene_error), which is the intended fail-closed behavior.
    "rq5a86-coloc": {
        "config": "src/vm/configs/task/isol_bench/rig_rq5_coloc/bm/rq5abm_interfere.yml",
        "remote_config": f"{REMOTE_RQ5_CFG_DIR}/rq5abm_interfere.yml",
        "ab_subdir": False,
        "schedulers": ["none"],     # Phase A runs native none only
        "needs_bpftrace": False,
        "data_root": REMOTE_RQ5_DATA_ROOT,
        "data_dirs": list(RQ5_DATA_DIRS),
        "lock": REMOTE_LOCK,
        "results_root": "results/rq5_realapps_coloc_bm/a_interfere",
    },
    "rq5b86-coloc": {
        "config": "src/vm/configs/task/isol_bench/rig_rq5_coloc/bm/rq5bbm_policy_matrix.yml",
        "remote_config": f"{REMOTE_RQ5_CFG_DIR}/rq5bbm_policy_matrix.yml",
        "ab_subdir": False,
        "schedulers": ["none", "bfq", "scx_io"],   # 8-arm policy matrix
        "needs_bpftrace": False,
        "data_root": REMOTE_RQ5_DATA_ROOT,
        "data_dirs": list(RQ5_DATA_DIRS),
        "lock": REMOTE_LOCK,
        "results_root": "results/rq5_realapps_coloc_bm/b_policy",
    },
    "rq5c86-coloc": {
        "config": "src/vm/configs/task/isol_bench/rig_rq5_coloc/bm/rq5cbm_dynamic.yml",
        "remote_config": f"{REMOTE_RQ5_CFG_DIR}/rq5cbm_dynamic.yml",
        "ab_subdir": False,
        "schedulers": ["none", "scx_io"],          # NONE/IO_WEIGHT/IOX_Q_ARB
        "needs_bpftrace": False,
        "data_root": REMOTE_RQ5_DATA_ROOT,
        "data_dirs": list(RQ5_DATA_DIRS),
        "lock": REMOTE_LOCK,
        "results_root": "results/rq5_realapps_coloc_bm/c_dynamic",
    },
}

# Read-only preflight probes (per campaign requirements; see preflight()).
PREFLIGHT_PATHS_COMMON = [
    f"{DEPLOY_ROOT}/src/vm/executor.py",
    REMOTE_VENV_PY,
    f"{DEPLOY_ROOT}/src/vm/assets/ycsb/ycsb_runtime/bin/ycsb",
    f"{DEPLOY_ROOT}/src/vm/assets/tailbench/shore/shore_bench.py",
    f"{DEPLOY_ROOT}/staging/motivation_m2_pilot_20261030_bundle/motivation_m2_pilot_20261030/runtime/java-21-openjdk-amd64/bin/java",
    f"{DEPLOY_ROOT}/staging/m1_cache_bm_bundle/rocksdb/db_bench",
    f"{DEPLOY_ROOT}/staging/m1_cache_bm_bundle/rocksdb/librocksdb.so.11.2.0",
    f"{DEPLOY_ROOT}/staging/m1_cache_bm_bundle/tailbench/tailbench-v0.9/shore/shore-kits/shore_kits_integrated",
    f"{DEPLOY_ROOT}/staging/m1_cache_bm_bundle/tailbench/tailbench-v0.9/shore/shore-kits/run-templates/cmdfile.template",
    # READ-ONLY Shore input -- must exist and never be deleted by any reset.
    f"{DEPLOY_ROOT}/staging/m1_cache_bm_bundle/data/db-tpcc-1",
]
PREFLIGHT_PATHS_BPFTRACE = ["/usr/bin/bpftrace"]


def die(msg: str) -> None:
    print(f"BLOCKED: {msg}", file=sys.stderr)
    sys.exit(2)


class SshEnv86:
    """Python mirror of experiments/m1m2_bm/scripts/ssh_env_bm.sh.

    Reads SSH_HOST/SSH_PORT/SSH_USER only. SUDO_PASS is deliberately NOT read:
    the bm case chain needs no runner-level sudo (NOPASSWD drop_caches runs
    inside the remote script), and the password must never appear in this
    process's memory, argv, or logs.
    """

    def __init__(self, path: Path):
        text = path.read_text()

        def grab(key: str) -> str:
            m = re.search(rf'export {key}="?([^"\n]*)"?', text)
            if not m:
                die(f"{path}: missing export {key}")
            return m.group(1).strip()

        self.host = grab("SSH_HOST")
        self.port = grab("SSH_PORT")
        self.user = grab("SSH_USER")
        if not self.host:
            die(f"{path}: SSH_HOST is empty")
        self.ssh_opts = [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=8",
        ]

    # ---- ssh argv helpers --------------------------------------------------
    def _ssh_argv(self, remote_cmd: str) -> list:
        argv = ["ssh", "-n", *self.ssh_opts]
        if self.port:
            argv += ["-p", self.port]
        argv += [f"{self.user}@{self.host}", remote_cmd]
        return argv

    def _rsync_rsh(self) -> str:
        rsh = "ssh " + " ".join(self.ssh_opts)
        if self.port:
            rsh += f" -p {self.port}"
        return rsh

    # ---- remote execution (all capture_output: callers match on stdout) -----
    def remote(self, cmd: str, timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(self._ssh_argv(cmd), text=True,
                              capture_output=True, timeout=timeout)

    def remote_b64(self, script: str, timeout: int) -> subprocess.CompletedProcess:
        """Run a multi-line script via base64 (no quoting loss over ssh)."""
        b64 = base64.b64encode(script.encode()).decode()
        return self.remote(f"echo {b64} | base64 -d | bash", timeout=timeout)

    def rsync_relative(self, rel_paths: list, dest: str, timeout: int,
                       cwd: Path) -> subprocess.CompletedProcess:
        argv = ["rsync", "-avz", "--relative", "--exclude", "__pycache__",
                "-e", self._rsync_rsh(), *rel_paths,
                f"{self.user}@{self.host}:{dest}/"]
        return subprocess.run(argv, text=True, timeout=timeout, cwd=str(cwd))

    def rsync_content(self, src_dir: Path, dest: str, timeout: int) -> subprocess.CompletedProcess:
        argv = ["rsync", "-avz", "--exclude", "__pycache__",
                "-e", self._rsync_rsh(), f"{src_dir}/",
                f"{self.user}@{self.host}:{dest}/"]
        return subprocess.run(argv, text=True, timeout=timeout)

    def scp(self, remote_path: str, local_path: Path, timeout: int) -> subprocess.CompletedProcess:
        argv = ["scp", *self.ssh_opts]
        if self.port:
            argv += ["-P", self.port]
        argv += [f"{self.user}@{self.host}:{remote_path}", str(local_path)]
        return subprocess.run(argv, text=True, timeout=timeout)


def load_sequences(config_path: Path) -> list:
    try:
        import yaml
    except ImportError:
        die("PyYAML is required on the control host to enumerate sequences")
    cfg = yaml.safe_load(config_path.read_text())
    return [str(seq["sequence_id"]) for seq in cfg.get("test_sequences", [])]


# ---------------------------------------------------------------------------
# preflight (read-only)
# ---------------------------------------------------------------------------
def preflight(ssh: SshEnv86, campaign: str, dry_run: bool = False) -> dict:
    camp = CAMPAIGNS[campaign]
    need_paths = list(PREFLIGHT_PATHS_COMMON)
    if camp["needs_bpftrace"]:
        need_paths += PREFLIGHT_PATHS_BPFTRACE

    # Single read-only probe script: identity, kernel, device, schedulers,
    # required paths, free space, idleness, /tmp fs type, campaign lock state.
    checks = [
        'printf "HOST=%s\\nKERNEL=%s\\n" "$(hostname)" "$(uname -r)"',
        "test -d /sys/block/nvme0n1",
        'printf "SCHED_AVAIL=%s\\n" "$(cat /sys/block/nvme0n1/queue/scheduler)"',
        'printf "DISK=%s\\n" "$(lsblk -dnbo SIZE,TYPE /dev/nvme0n1 | tr -s \' \')"',
        'printf "ROOT_FREE_KB=%s\\n" "$(df -Pk / | tail -1 | tr -s \' \' | cut -d\' \' -f4)"',
        'printf "TMP_FS=%s\\nTMP_FREE_KB=%s\\n" "$(findmnt -n -T /tmp -o FSTYPE)" "$(df -Pk /tmp | tail -1 | tr -s \' \' | cut -d\' \' -f4)"',
        'printf "MEM_AVAILABLE_KB=%s\\n" "$(grep MemAvailable /proc/meminfo | tr -s \' \' | cut -d\' \' -f2)"',
        "test -z \"$(pgrep -x 'fio|java|db_bench|shore_kits_integrated' || true)\"",
    ]
    for p in need_paths:
        checks.append(f"test -e {shlex.quote(p)}")
    checks.append(f"test -d {shlex.quote(str(Path(DEPLOY_ROOT)))}")
    script = "set -e\n" + ";\n".join(checks) + ';\nprintf "PREFLIGHT_OK\\n"'

    print(f"=== [1] read-only preflight on {ssh.host} (campaign={campaign}) ===")
    if dry_run:
        print("[dry-run] remote preflight script:")
        print(script)
        return {}
    res = ssh.remote_b64(script, timeout=PREFLIGHT_TIMEOUT_SEC)
    out = res.stdout or ""
    print(out, end="")
    if res.returncode != 0 or "PREFLIGHT_OK" not in out:
        die(f"bm preflight failed rc={res.returncode} stderr={(res.stderr or '')[-1500:]}\n"
            "(identity/device/paths/idleness checks above; see experiments/m1m2_bm/LEDGER.md)")
    info = dict(re.findall(r"^(\w+)=(.*)$", out, re.MULTILINE))

    if info.get("HOST") != "user":
        die(f"hostname is {info.get('HOST')!r}, expected user -- wrong machine?")
    kernel = info.get("KERNEL", "")
    if not kernel.startswith("7.3.1-ioext"):
        print(f">>> WARNING: kernel {kernel!r} is not 7.3.1-ioext* "
              "(recorded in campaign.json; verify the caliber before publishing)")
    sched_avail = info.get("SCHED_AVAIL", "")
    # sysfs marks the ACTIVE scheduler as "[none]"; normalize brackets away
    # before comparing (same parsing as controller._parse_current_io_scheduler).
    sched_tokens = [t.strip("[]") for t in sched_avail.split()]
    for sched in camp["schedulers"]:
        if sched not in sched_tokens:
            die(f"scheduler {sched!r} not available on nvme0n1 (available: {sched_avail!r})")
    if not info.get("TMP_FS"):
        die("could not determine /tmp filesystem type")
    if info.get("TMP_FS") == "tmpfs":
        print(">>> WARNING: /tmp is tmpfs on bm -- executor outputs and case tars "
              "live in RAM; the runner deletes each remote case dir right after "
              "tarring and the tar right after pulling, so the footprint stays "
              "at about one case; still watch MemAvailable during long campaigns")
    try:
        free_kb = int(info.get("ROOT_FREE_KB", "0"))
    except ValueError:
        free_kb = 0
    if free_kb < MIN_ROOT_FREE_KB:
        die(f"root fs free {free_kb} KiB < {MIN_ROOT_FREE_KB} KiB -- not enough "
            "for the per-case data dirs (ycsb_db/dbbench_db/shore_scratch)")
    return info


# ---------------------------------------------------------------------------
# code + config sync
# ---------------------------------------------------------------------------
def ensure_sync(ssh: SshEnv86, skip: bool, dry_run: bool = False) -> None:
    if skip:
        print("=== [2] code/config sync skipped (--skip-sync) ===")
        return
    print("=== [2] rsync code + bm configs -> bm ===")
    for rel in SYNC_REL_PATHS:
        if not (BASE_DIR / rel).exists():
            die(f"sync source missing: {BASE_DIR / rel}")
    cfg_dir = EXP_DIR / "configs"
    if not cfg_dir.is_dir():
        die(f"sync source missing: {cfg_dir}")
    if dry_run:
        print(f"[dry-run] rsync --relative {SYNC_REL_PATHS} -> {DEPLOY_ROOT}/")
        print(f"[dry-run] rsync {cfg_dir}/ -> {REMOTE_CFG_DIR}/")
        return
    res = ssh.rsync_relative(SYNC_REL_PATHS, DEPLOY_ROOT, timeout=SYNC_TIMEOUT_SEC, cwd=BASE_DIR)
    if res.returncode != 0:
        die(f"code rsync failed rc={res.returncode}")
    # bm task configs MUST land under src/vm/configs/ on bm (config.py derives
    # the base_scene search root from the "src/vm/configs" path segment).
    res = ssh.rsync_content(cfg_dir, REMOTE_CFG_DIR, timeout=SYNC_TIMEOUT_SEC)
    if res.returncode != 0:
        die(f"bm config rsync failed rc={res.returncode}")


# ---------------------------------------------------------------------------
# per-case remote chain
# ---------------------------------------------------------------------------
def case_script(remote_config: str, seq: str, run_id: str, case_name: str,
                data_dirs: list = None, lock: str = None) -> str:
    """Per-case remote chain. data_dirs/lock default to the m1 constants;
    the rq5*86-coloc campaigns pass their own (same reset semantics: root
    rm via NOPASSWD sudo, then user-owned re-mkdir)."""
    data_dirs = DATA_DIRS if data_dirs is None else data_dirs
    lock = REMOTE_LOCK if lock is None else lock
    case_dir = f"{REMOTE_TMP_ROOT}/{run_id}/{case_name}"
    lines = [
        "set -e",
        # --- campaign lock: instant try, then wait for the holder ----------
        f"exec 9>{shlex.quote(lock)}",
        "flock -n 9 || { echo 'M1_LOCK_WAIT: another m1/m2 campaign is running; waiting'; flock 9; }",
        "echo \"M1_LOCK_HELD case=" + case_name + "\"",
        # --- idleness guard ------------------------------------------------
        "test -z \"$(pgrep -x 'fio|java|db_bench|shore_kits_integrated' || true)\" "
        "|| { echo 'BUSY_PROCS: fio/java/db_bench/shore still running'; exit 3; }",
        # --- free-space guard (create the tmp root first: df on a missing
        #     path would fail under set -e on the very first case) -----------
        f"mkdir -p {REMOTE_TMP_ROOT}",
        f"test $(df -Pk / | tail -1 | tr -s ' ' | cut -d' ' -f4) -gt {MIN_ROOT_FREE_KB}",
        f"test $(df -Pk {REMOTE_TMP_ROOT} | tail -1 | tr -s ' ' | cut -d' ' -f4) -gt 2097152",
        # --- state reset part 1: data dirs -----------------------------------
        # The scenes launch db_bench/YCSB through run_sudo_shell, so SST/log
        # files AND the dbbench_db dir itself can be ROOT-owned (RocksDB
        # creates the dir if missing) -- user rm -rf then fails with
        # "Permission denied" (m1cacheon_bm_20261105 round 1 failed 12/15
        # this way). rm as root (NOPASSWD), re-mkdir as user afterwards so
        # the scenes write into user-owned dirs each case.
        # NEVER include staging/m1_cache_bm_bundle/data/db-tpcc-1 (read-only input).
        f"sudo -n rm -rf {' '.join(shlex.quote(d) for d in data_dirs)}",
        f"mkdir -p {' '.join(shlex.quote(d) for d in data_dirs)}",
        # --- state reset part 2: page cache (NOPASSWD sudo; fail closed) ----
        # The scenes drop again before load and between load and run
        # (drop_caches_before_run: true) -- the attribution discipline lives
        # in the scenes; this runner-level drop resets residue from the
        # PREVIOUS case (kvmhost replaced it with mkfs).
        "sudo -n bash -c 'sync; printf 3 > /proc/sys/vm/drop_caches' "
        "|| { echo 'DROP_CACHES_FAILED: NOPASSWD sudo missing?'; exit 5; }",
        # --- fresh monitor/scene roots (per-case snapshot roots) ------------
        f"rm -rf {REMOTE_TMP_ROOT}/monitor {REMOTE_TMP_ROOT}/scene",
        f"mkdir -p {REMOTE_TMP_ROOT}/monitor {REMOTE_TMP_ROOT}/scene {shlex.quote(str(Path(case_dir).parent))}",
        # --- retry hygiene: a stale case dir/tar would make the executor's
        #     custom-result-dir check refuse (non-empty) ----------------------
        f"rm -rf {shlex.quote(case_dir)} {shlex.quote(case_dir + '.tar.gz')}",
        # --- executor --------------------------------------------------------
        f"cd {shlex.quote(REMOTE_EXECUTOR_CWD)}",
        "rc=0",
        f"PERF_ARENA_WORKSPACE={DEPLOY_ROOT} PYTHONPATH={REMOTE_SRC} "
        f"{shlex.quote(REMOTE_VENV_PY)} -m vm.executor "
        f"--config {shlex.quote(remote_config)} --sequence {shlex.quote(seq)} "
        f"--output {shlex.quote(case_dir)} --log-level INFO || rc=$?",
        # --- snapshot monitor/scene outputs into the case dir ----------------
        f"cp -a {REMOTE_TMP_ROOT}/monitor {REMOTE_TMP_ROOT}/scene {shlex.quote(case_dir)}/ 2>/dev/null || true",
        # --- tar + rc report ---------------------------------------------------
        f"tar czf {shlex.quote(case_dir + '.tar.gz')} -C {shlex.quote(str(Path(case_dir).parent))} {shlex.quote(case_name)}",
        # /tmp on bm is tmpfs (RAM-backed): drop the case dir right after the
        # tar (the tar carries everything; it is pulled below and then removed
        # remotely), so a 36-case matrix never accumulates in RAM.
        f"rm -rf {shlex.quote(case_dir)}",
        f'echo "CASE_RC=$rc"',
        "exit $rc",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True, choices=sorted(CAMPAIGNS))
    parser.add_argument("--repeats", type=int, default=1,
                        help="repeats per sequence (case <seq>_r<k>; default 1)")
    parser.add_argument("--only", default="",
                        help="comma-separated sequence-id substrings to keep")
    parser.add_argument("--probe", action="store_true",
                        help="single probe case: first matching sequence, one run")
    parser.add_argument("--run-id", default=None,
                        help="run id (default <campaign>_<UTC ts>); reuse the SAME "
                             "run-id for m1cacheon+m1cacheab to build one "
                             "analysis-ready results root (ab/ sublevel)")
    parser.add_argument("--output", type=Path, default=None,
                        help="local results dir (default "
                             "experiments/m1m2_bm/runs/m1_campaign/<RUN_ID>)")
    parser.add_argument("--ssh-env", type=Path, default=DEFAULT_SSH_ENV,
                        help="path to ssh_env_bm.sh (default: alongside this script)")
    parser.add_argument("--skip-sync", action="store_true",
                        help="skip the code/config rsync (bare-metal already current)")
    parser.add_argument("--resume", action="store_true",
                        help="reuse the existing run dir; skip cases already "
                             "marked ok in campaign.json with a pulled tar")
    parser.add_argument("--fail-fast", action="store_true",
                        help="stop at the first case failure (default: record and continue)")
    parser.add_argument("--extract", action="store_true",
                        help="extract each pulled tar under the local run dir")
    parser.add_argument("--list", action="store_true", help="list sequences and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the preflight/sync/case commands without "
                             "executing anything (no ssh at all)")
    args = parser.parse_args()

    camp = CAMPAIGNS[args.campaign]
    config_path = BASE_DIR / camp["config"]
    if not config_path.is_file():
        die(f"missing bm task config: {config_path}")
    if not args.ssh_env.is_file():
        die(f"missing ssh env: {args.ssh_env}")
    if args.repeats < 1:
        die("--repeats must be >= 1")

    sequences = load_sequences(config_path)
    filters = [t.strip() for t in args.only.split(",") if t.strip()]
    if filters:
        sequences = [s for s in sequences if any(f in s for f in filters)]
        if not sequences:
            die(f"--only matched no sequences; available: {load_sequences(config_path)}")

    if args.list:
        for seq in load_sequences(config_path):
            marker = "*" if not filters or any(f in seq for f in filters) else " "
            print(f"{marker} {seq}")
        return 0

    run_id = args.run_id or f"{args.campaign}_{time.strftime('%Y%m%d_%H%M%S')}"
    # Default local output: the m1 campaigns keep the runs/m1_campaign layout;
    # the rq5*86-coloc campaigns default under their per-campaign results_root
    # (repo-relative), mirroring the kvmhost rq5*-coloc entries. --output wins.
    if args.output is not None:
        base_output = args.output
    elif camp.get("results_root"):
        base_output = BASE_DIR / camp["results_root"] / run_id
    else:
        base_output = EXP_DIR / "runs" / "m1_campaign" / run_id
    # m1cacheab cases live under ab/ so one run dir = one analysis root
    # (m1_cache_cases.py walks <root>/<CASE> and <root>/ab/<CASE>).
    output = base_output / "ab" if camp["ab_subdir"] else base_output

    # case list: (sequence, case name)
    cases = []
    for rep in range(1, args.repeats + 1):
        for seq in sequences:
            cases.append((seq, f"{seq}_r{rep}"))
    if args.probe:
        cases = [(cases[0][0], f"{cases[0][0]}_r_probe")]

    # resume state -----------------------------------------------------------
    meta_path = output / "campaign.json"
    prior_status = {}
    if output.exists() and meta_path.is_file():
        try:
            prior = json.loads(meta_path.read_text())
            prior_status = prior.get("cases_status") or {}
        except (json.JSONDecodeError, OSError):
            prior_status = {}

    # resume state: sets of case NAMES (strings), not (seq, name) tuples, so
    # every membership check below is name-vs-name.
    existing_tars = {name for _seq, name in cases
                     if (output / f"{name}.tar.gz").exists()}
    done_cases = {name for name in existing_tars
                  if prior_status.get(name) == "ok"}
    if existing_tars and not args.resume and not args.dry_run:
        die(f"local run dir already holds case tars: {sorted(existing_tars)}; "
            "use --resume to continue this run or a different --run-id/--output")
    todo = [(seq, name) for seq, name in cases if name not in done_cases] \
        if args.resume else cases

    meta = {
        "campaign": args.campaign,
        "config": camp["config"],
        "remote_config": camp["remote_config"],
        "run_id": run_id,
        "host": None,
        "kernel": None,
        "device": "nvme0n1",
        "reset_caliber": "data-dir rm/mkdir + drop_caches (no mkfs/mount; "
                         "LEDGER decision 1)",
        "repeats": args.repeats,
        "only": args.only,
        "probe": args.probe,
        "resume": args.resume,
        "fail_fast": args.fail_fast,
        "sequences": [s for s, _ in cases],
        "cases": [c for _, c in cases],
        "cases_status": {name: ("ok" if name in done_cases else prior_status.get(name))
                         for name in [n for _, n in cases]},
        "skipped_by_resume": sorted(done_cases),
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    if args.dry_run:
        print("=== DRY RUN: no ssh, nothing executed, nothing written ===")
        print(f"campaign={args.campaign} run_id={run_id} repeats={args.repeats} "
              f"probe={args.probe} resume={args.resume}")
        print(f"local output: {output}")
        print(f"sequences ({len(sequences)}): {sequences}")
        print(f"cases ({len(cases)}): {[c for _, c in cases]}")
        if args.resume:
            print(f"resume: skip ok cases {sorted(done_cases)}")
        ssh = SshEnv86(args.ssh_env)
        print(f"ssh target: {ssh.user}@{ssh.host}"
              + (f" port={ssh.port}" if ssh.port else ""))
        preflight(ssh, args.campaign, dry_run=True)
        ensure_sync(ssh, args.skip_sync, dry_run=True)
        for idx, (seq, case_name) in enumerate(todo):
            print(f"--- case {idx + 1}/{len(todo)}: {case_name} ---")
            print(case_script(camp["remote_config"], seq, run_id, case_name,
                              camp.get("data_dirs"), camp.get("lock")))
            print(f"[dry-run] remote: {ssh._ssh_argv('echo <base64-script> | base64 -d | bash')[0]} "
                  f"... {ssh.user}@{ssh.host} (timeout {CASE_TIMEOUT_SEC}s)")
            print(f"[dry-run] pull: scp {ssh.user}@{ssh.host}:"
                  f"{REMOTE_TMP_ROOT}/{run_id}/{case_name}.tar.gz -> {output / (case_name + '.tar.gz')}")
        print("=== end of dry run ===")
        return 0

    output.mkdir(parents=True, exist_ok=True)
    ssh = SshEnv86(args.ssh_env)
    info = preflight(ssh, args.campaign)
    meta["host"] = info.get("HOST")
    meta["kernel"] = info.get("KERNEL")
    meta["scheduler_available"] = info.get("SCHED_AVAIL")
    meta["root_free_kb"] = info.get("ROOT_FREE_KB")
    meta["tmp_fs"] = info.get("TMP_FS")
    meta["preflight_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # LEDGER 待决 D reminder (read-only note; the apt/hold steps are a
    # separate operator action, never done by this driver).
    print(">>> reminder (LEDGER 待决 D): before the FIRST formal campaign on bm, "
          "run the apt-upgrade/hold unattended-upgrades/snap-refresh procedure "
          "and log it in LEDGER §5")

    ensure_sync(ssh, args.skip_sync)

    # post-sync guard: the executor will refuse a missing config; catch it
    # here with a clear message (covers a mistaken --skip-sync on a fresh bm).
    chk = ssh.remote(f"test -f {shlex.quote(camp['remote_config'])} && echo CFG_OK",
                     timeout=60)
    if "CFG_OK" not in (chk.stdout or ""):
        die(f"remote task config missing: {camp['remote_config']} "
            "(drop --skip-sync so the runner rsyncs experiments/m1m2_bm/configs/)")

    failures = []
    for idx, (seq, case_name) in enumerate(todo):
        print(f"=== [3] case {idx + 1}/{len(todo)}: {case_name} ===", flush=True)
        script = case_script(camp["remote_config"], seq, run_id, case_name,
                             camp.get("data_dirs"), camp.get("lock"))
        res = ssh.remote_b64(script, timeout=CASE_TIMEOUT_SEC)
        print((res.stdout or "")[-4000:], end="")
        if res.returncode != 0:
            print(f">>> CASE FAIL {case_name} rc={res.returncode} "
                  f"stderr={(res.stderr or '')[-2000:]}", flush=True)
            meta["cases_status"][case_name] = "fail"
            failures.append(case_name)
        else:
            meta["cases_status"][case_name] = "ok"
        remote_tar = f"{REMOTE_TMP_ROOT}/{run_id}/{case_name}.tar.gz"
        pull = ssh.scp(remote_tar, output / f"{case_name}.tar.gz",
                      timeout=PULL_TIMEOUT_SEC)
        if pull.returncode != 0:
            print(f">>> PULL FAIL {case_name}", flush=True)
            meta["cases_status"][case_name] = "fail:pull"
            failures.append(f"{case_name}:pull")
        else:
            if args.extract:
                subprocess.run(["tar", "xzf", str(output / f"{case_name}.tar.gz"),
                               "-C", str(output)], check=False)
            # remote /tmp is tmpfs: free the pulled tar so long campaigns do
            # not accumulate RAM (kept only when the pull failed, for retry).
            cleanup = ssh.remote(f"rm -f {shlex.quote(remote_tar)}", timeout=120)
            if cleanup.returncode != 0:
                print(f">>> NOTE: remote tar cleanup failed for {case_name} "
                      "(tmpfs pressure possible on long campaigns)", flush=True)
        # persist status after every case so an interrupted run resumes cleanly
        meta["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        meta_path.write_text(json.dumps(meta, indent=2))
        print(f"=== case done: {case_name} status={meta['cases_status'][case_name]} ===",
              flush=True)
        if failures and args.fail_fast:
            break

    meta["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta_path.write_text(json.dumps(meta, indent=2))
    n_ok = sum(1 for c in meta["cases_status"].values() if c == "ok")
    print(f"campaign {args.campaign} finished: {n_ok}/{len(cases)} cases ok; "
          f"output={output}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
