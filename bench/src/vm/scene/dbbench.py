"""Self-contained io-ext RocksDB db_bench scene.

Single-binary (no JVM) RocksDB global-load scene mirroring the proven
IoExtYcsbRocksdbScene lifecycle: auto format/mount of the target data disk,
cgroup-v2 accounting wrapper, scheduler pinning, optional io-ext loader chain,
and phase-sequential db_bench runs with a settle discipline between the DB
build and the measurement phases.

Phases are declared as an ordered list; each entry is either a db_bench
benchmark run (fillrandom / compact / readrandom / readwhilewriting /
readrandomwriterandom / ...) or a pure settle sleep used to drain background
flush/compaction debt before the next measurement window.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from vm.scene.base_scene import Scene
from vm.scene.utils import DataScorer

from .common import (
    DEFAULT_ROOT_CGROUP_PATH,
    SUDO_BIN,
    cleanup_cgroup,
    expand_path,
    interrupt_process,
    loader_log_paths,
    popen_in_own_session,
    run_sudo_shell,
    setup_cgroup_v2,
    setup_cgroup_io_controls,
    setup_iocost_at_root,
    set_scheduler,
    start_loader_chain,
    stop_loader_chain,
    validate_block_device_name,
    wrap_command_in_cgroup,
)
from .ycsb_rocksdb import _blkid_type, _read_cgroup_io_snapshot, _to_bool


logger = logging.getLogger("io_ext_dbbench")


class IoExtDbBenchScene(Scene):
    """Run RocksDB db_bench phase sequences inside an io-ext cgroup."""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        db = parameters.get("dbbench") or parameters.get("database") or {}
        self.dbbench_bin = expand_path(db.get("dbbench_bin"))
        self.rocksdb_home = expand_path(db.get("rocksdb_home")) or (
            os.path.dirname(os.path.dirname(self.dbbench_bin)) if self.dbbench_bin else None
        )
        self.db_path = expand_path(db.get("db_path"))
        self.use_direct_io = _to_bool(db.get("use_direct_io", db.get("direct_io", True)))
        self.common_args: Dict[str, Any] = dict(db.get("common_args") or {})
        if not self.dbbench_bin or not self.db_path:
            raise ValueError("dbbench.dbbench_bin and dbbench.db_path are required")

        raw_phases = parameters.get("phases") or []
        if not isinstance(raw_phases, list) or not raw_phases:
            raise ValueError("phases must be a non-empty ordered list")
        self.phases: List[Dict[str, Any]] = []
        for idx, raw in enumerate(raw_phases):
            if not isinstance(raw, dict):
                raise ValueError(f"phases[{idx}] must be a mapping: {raw!r}")
            phase = dict(raw)
            phase.setdefault("name", str(phase.get("benchmark") or f"phase{idx}"))
            phase.setdefault("args", {})
            self.phases.append(phase)

        target_device = parameters.get("target_device")
        if not target_device:
            raise ValueError("parameters.target_device is required, e.g. nvme0n1")
        self.target_device = validate_block_device_name(str(target_device))

        io_ext = parameters.get("io_ext") or {}
        self.io_ext_cfg: Dict[str, Any] = dict(io_ext)
        self.scheduler = io_ext.get("scheduler", parameters.get("scheduler"))

        system_cfg = parameters.get("system") or {}
        init_cfg = parameters.get("init") or {}
        self.auto_format_disk = _to_bool(system_cfg.get("auto_format_disk", False))
        self.format_device = _to_bool(system_cfg.get("format_device", init_cfg.get("format_device", False)))
        self.skip_disk_setup = _to_bool(system_cfg.get("skip_disk_setup", False))
        self.mount_options = str(system_cfg.get("mount_options", "noatime,nodiratime") or "").strip()
        self.cgroup_path = expand_path(system_cfg.get("cgroup_path") or "/sys/fs/cgroup/dbbench_rig")

        cgroup_cfg = io_ext.get("cgroup") or {}
        self.io_ext_cgroup_path = expand_path(
            cgroup_cfg.get("path") or io_ext.get("cgroup_path") or self.cgroup_path
        )
        self.io_ext_cgroup_controllers = list(cgroup_cfg.get("controllers") or ["io"])
        self.io_ext_cgroup_io_weight = cgroup_cfg.get("io_weight", cgroup_cfg.get("io.weight"))
        self.io_ext_cgroup_io_max = cgroup_cfg.get("io_max", cgroup_cfg.get("io.max"))
        self.io_ext_cgroup_io_latency = cgroup_cfg.get("io_latency", cgroup_cfg.get("io.latency"))
        self.io_ext_cgroup_io_bfq_weight = cgroup_cfg.get("io_bfq_weight", cgroup_cfg.get("io.bfq.weight"))
        io_cost_cfg = cgroup_cfg.get("io_cost", cgroup_cfg.get("io.cost")) or {}
        self.io_ext_iocost_model = io_cost_cfg.get("model")
        self.io_ext_iocost_qos = io_cost_cfg.get("qos")
        self.io_ext_iocost_root_path = expand_path(
            (io_ext.get("root_cgroup") or {}).get("path") if isinstance(io_ext.get("root_cgroup"), dict)
            else io_ext.get("root_cgroup_path")
        ) or DEFAULT_ROOT_CGROUP_PATH

        self.output_dir = expand_path(parameters.get("output_dir")) or "./test_results"
        self.duration = int(parameters.get("duration", 7200))
        self.drop_caches_before_run = _to_bool(parameters.get("drop_caches_before_run", False))

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self._loader_procs: list[subprocess.Popen[str]] = []
        self._bench_proc: Optional[subprocess.Popen[str]] = None
        self.disk_setup_info: Dict[str, Any] = {}
        self.collected_data: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def setup(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        if not os.path.isfile(self.dbbench_bin):
            raise FileNotFoundError(f"db_bench executable not found: {self.dbbench_bin}")

        mount_point = os.path.dirname(self.db_path)
        if self.skip_disk_setup:
            # RQ5 co-location: the campaign runner pre-mounts the shared data
            # disk (and local smoke runs on an already-mounted filesystem such
            # as /var/tmp), so the scene must not format or mount anything.
            logger.info("system.skip_disk_setup=true: skipping test disk setup for %s", self.target_device)
        else:
            self._setup_test_disk(self.target_device, mount_point, self.auto_format_disk, self.format_device)
        if self.io_ext_cgroup_path:
            setup_cgroup_v2(self.io_ext_cgroup_path, self.io_ext_cgroup_controllers)

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        self._apply_cgroup_io_controls()
        self.collected_data = {
            "target_device": self.target_device,
            "scheduler": self.scheduler,
            "dbbench_bin": self.dbbench_bin,
            "rocksdb_home": self.rocksdb_home,
            "db_path": self.db_path,
            "use_direct_io": self.use_direct_io,
            "common_args": dict(self.common_args),
            "phases": [dict(phase) for phase in self.phases],
            "io_ext_cgroup_path": self.io_ext_cgroup_path,
            "io_ext_cgroup_controls": {},
            "disk_setup": dict(self.disk_setup_info),
            "skip_disk_setup": self.skip_disk_setup,
            "drop_caches_before_run": self.drop_caches_before_run,
        }
        self.start_time = None
        self.end_time = None

    def _run(self) -> None:
        self.start_time = time.time()
        self._record_io_snapshot("before_run")
        if os.path.exists(self.db_path):
            shutil.rmtree(self.db_path)
        os.makedirs(self.db_path, exist_ok=True)

        if self.drop_caches_before_run:
            self._drop_caches()
        self._set_scheduler()
        self._start_loader()

        phase_results: Dict[str, Dict[str, Any]] = {}
        phase_logs: Dict[str, str] = {}
        try:
            existing_db = False
            for idx, phase in enumerate(self.phases):
                if self.should_stop():
                    return
                name = str(phase["name"])
                benchmark = str(phase.get("benchmark") or "")
                settle_sec = float(phase.get("settle_after_sec", 0) or 0)

                if benchmark == "sleep":
                    logger.info("phase %s: settle sleep %.0fs", name, float(phase.get("settle_sec", 0)))
                    self._sleep_phase(float(phase.get("settle_sec", 0)))
                    phase_results[name] = {"kind": "sleep", "settle_sec": float(phase.get("settle_sec", 0))}
                    continue

                out_file = os.path.join(self.output_dir, f"{self.scene_id}_{name}.log")
                self._record_io_snapshot(f"before_{name}")
                phase_io_max = self._apply_phase_io_max(phase)
                rc = self._run_benchmark(name, benchmark, phase, out_file, existing_db)
                self._record_io_snapshot(f"after_{name}")
                if phase_io_max:
                    self._revert_phase_io_max()
                existing_db = True

                parsed = _parse_dbbench_log(out_file)
                parsed["returncode"] = rc
                parsed["log"] = out_file
                parsed["phase_io_max"] = phase_io_max
                parsed["io_delta"] = _io_stat_delta(
                    self.collected_data.get("io_stat_snapshots", {}).get(f"before_{name}"),
                    self.collected_data.get("io_stat_snapshots", {}).get(f"after_{name}"),
                )
                phase_results[name] = parsed
                phase_logs[name] = out_file
                self.collected_data.setdefault("phase_order", []).append(name)

                if rc != 0 and not self.should_stop():
                    raise RuntimeError(f"db_bench phase {name} failed returncode={rc} output={out_file}")
                if parsed.get("ops_per_sec") is None and not self.should_stop():
                    raise RuntimeError(f"db_bench phase {name} produced no parsable ops/sec line: {out_file}")

                if settle_sec > 0:
                    logger.info("phase %s: settle sleep %.0fs", name, settle_sec)
                    self._sleep_phase(settle_sec)
        finally:
            self._stop_loader()
            self.end_time = time.time()
            self._record_io_snapshot("after_run")

        self.collected_data["phase_results"] = phase_results
        self.collected_data["phase_logs"] = phase_logs
        self.collected_data["io_stat_summary"] = _io_stat_delta(
            self.collected_data.get("io_stat_snapshots", {}).get("before_run"),
            self.collected_data.get("io_stat_snapshots", {}).get("after_run"),
        )
        self.collected_data["db_footprint_after"] = _du_summary(self.db_path)

    def end(self) -> None:
        # Runs on the controller thread (Scene.stop()) while the scene thread
        # may be inside _run_benchmark's poll loop: read the shared handle
        # exactly once and tolerate it being None or already cleared.
        proc = self._bench_proc
        if proc is not None:
            interrupt_process(proc, timeout_sec=15)
        self._bench_proc = None
        self._stop_loader()

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        cleanup_cgroup(self.io_ext_cgroup_path)

    def collect(self) -> Dict[str, Any]:
        if self.start_time is None or self.end_time is None:
            raise RuntimeError("scene has not completed")
        self.collected_data["start_time"] = self.start_time
        self.collected_data["end_time"] = self.end_time
        self.collected_data["duration_wall"] = self.end_time - self.start_time
        return self.collected_data

    def stat(self) -> List[str]:
        if self.start_time is not None and self.end_time is not None:
            self.collect()
        os.makedirs(self.output_dir, exist_ok=True)
        path = os.path.join(self.output_dir, f"dbbench_{self.scene_id}_{int(time.time())}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.collected_data, f, indent=2, ensure_ascii=False, sort_keys=True)
        result_files: List[str] = [path]
        for log_path in (self.collected_data.get("phase_logs") or {}).values():
            if log_path and os.path.isfile(log_path):
                result_files.append(log_path)
        return result_files

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.start_time = self.collected_data.get("start_time")
        self.end_time = self.collected_data.get("end_time")
        duration = self.collected_data.get("duration_wall")
        self.duration = float(duration) if duration is not None else 0.0

    def score(self) -> float:
        phases = self.collected_data.get("phase_results") or {}
        read_ops = None
        for name in ("readrandom", "readwhilewriting", "readrandomwriterandom"):
            result = phases.get(name) or {}
            if result.get("ops_per_sec") is not None:
                read_ops = float(result["ops_per_sec"])
        if read_ops is None or read_ops <= 0:
            logger.warning("db_bench scene has no parsable ops/sec; returning zero score")
            return 0.0
        self.duration = float(self.collected_data.get("duration_wall") or 0.0)
        scorer = DataScorer(
            data=[read_ops, read_ops],
            threshold=1000.0,
            positive=True,
            value_exponent=0.5,
        )
        score = scorer.calculate_score()
        logger.info("db_bench ops/sec %.2f, score %.2f", read_ops, score)
        return float(score)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _setup_test_disk(self, device_name: str, mount_point: str, auto_format: bool, force_format: bool) -> None:
        device_path = f"/dev/{validate_block_device_name(device_name)}"
        if not os.path.exists(device_path):
            raise FileNotFoundError(f"block device does not exist: {device_path}")

        res = subprocess.run([SUDO_BIN, "blkid", device_path], capture_output=True, text=True, check=False)
        fs_before = _blkid_type(res.stdout)
        formatted = False
        if force_format:
            logger.info("force formatting %s as ext4", device_path)
            self._format_ext4(device_path, mount_point)
            formatted = True
        elif fs_before != "ext4":
            if auto_format:
                logger.info("formatting %s as ext4", device_path)
                self._format_ext4(device_path, mount_point)
                formatted = True
            else:
                raise RuntimeError(f"{device_path} is not ext4; format it or set system.auto_format_disk: true")

        res_after = subprocess.run([SUDO_BIN, "blkid", device_path], capture_output=True, text=True, check=False)

        os.makedirs(mount_point, exist_ok=True)
        check = subprocess.run(["mountpoint", "-q", mount_point], capture_output=True, text=True)
        if check.returncode != 0:
            mount_cmd = [SUDO_BIN, "mount"]
            if self.mount_options:
                mount_cmd.extend(["-o", self.mount_options])
            mount_cmd.extend([device_path, mount_point])
            subprocess.run(mount_cmd, check=True)
            subprocess.run([SUDO_BIN, "chmod", "777", mount_point], check=True)
        self.disk_setup_info = {
            "device_path": device_path,
            "mount_point": mount_point,
            "filesystem_before": fs_before,
            "filesystem_after": _blkid_type(res_after.stdout),
            "formatted": formatted,
            "force_format": force_format,
            "auto_format": auto_format,
            "mount_options": self.mount_options,
        }

    def _format_ext4(self, device_path: str, mount_point: str) -> None:
        q_device = shlex.quote(device_path)
        q_mount = shlex.quote(mount_point)
        script = "\n".join(
            [
                "set -e",
                f"if mountpoint -q {q_mount}; then umount {q_mount}; fi",
                f"while read -r target; do [ -n \"$target\" ] && umount \"$target\"; done < <(findmnt -rn -S {q_device} -o TARGET || true)",
                f"mkfs.ext4 -F {q_device}",
            ]
        )
        result = run_sudo_shell(script, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"failed to format {device_path} as ext4: {(result.stderr or result.stdout).strip()}")

    def _drop_caches(self) -> None:
        run_sudo_shell("sync; printf '%s\\n' 3 > /proc/sys/vm/drop_caches", check=False)

    def _set_scheduler(self) -> None:
        set_scheduler(self.target_device, self.scheduler)

    def _apply_phase_io_max(self, phase: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Apply a per-phase io.max override declared as ``phase["io_max"]``.

        Returns the applied spec (truthy) when an override was written so the
        caller knows to :meth:`_revert_phase_io_max` after the phase; returns
        None (pure no-op) when the phase carries no io_max, leaving the
        scene-level controls applied in prepare() in effect. Fix 2026-10-31:
        the M1 cache campaign calls this unconditionally per measured phase
        but the methods were never implemented (AttributeError killed every
        db_bench phase run).
        """
        io_max = phase.get("io_max")
        if not io_max or not self.io_ext_cgroup_path:
            return None
        applied = setup_cgroup_io_controls(
            self.io_ext_cgroup_path,
            f"/dev/{self.target_device}",
            None,
            io_max,
            None,
        )
        return applied or {"io_max": io_max}

    def _revert_phase_io_max(self) -> None:
        """Restore the scene-level io.max state (or unlimited) after a phase override."""
        setup_cgroup_io_controls(
            self.io_ext_cgroup_path,
            f"/dev/{self.target_device}",
            self.io_ext_cgroup_io_weight,
            self.io_ext_cgroup_io_max
            if self.io_ext_cgroup_io_max is not None
            else {"rbps": "max", "wbps": "max", "riops": "max", "wiops": "max"},
            self.io_ext_cgroup_io_latency,
            None,  # io_prio_class: not exposed by this scene
            self.io_ext_cgroup_io_bfq_weight,
        )

    def _apply_cgroup_io_controls(self) -> None:
        self.io_ext_cgroup_controls = setup_cgroup_io_controls(
            self.io_ext_cgroup_path,
            f"/dev/{self.target_device}",
            self.io_ext_cgroup_io_weight,
            self.io_ext_cgroup_io_max,
            self.io_ext_cgroup_io_latency,
            None,  # io_prio_class: not exposed by this scene
            self.io_ext_cgroup_io_bfq_weight,
        )
        # io.cost (model + qos) must be written at the cgroup root; same
        # mechanism as the fio scene (setup_iocost_at_root).
        if self.io_ext_iocost_model is not None or self.io_ext_iocost_qos is not None:
            iocost_result = setup_iocost_at_root(
                f"/dev/{self.target_device}",
                self.io_ext_iocost_model,
                self.io_ext_iocost_qos,
                root_cgroup_path=self.io_ext_iocost_root_path,
            )
            if iocost_result:
                self.io_ext_cgroup_controls["io_cost"] = iocost_result

    def _start_loader(self) -> None:
        self._loader_procs = start_loader_chain(self.parameters, self.output_dir, self.io_ext_cgroup_path)

    def _stop_loader(self) -> None:
        if self._loader_procs:
            self.collected_data.setdefault("loader_logs", loader_log_paths(self._loader_procs))
        stop_loader_chain(self._loader_procs)
        self._loader_procs = []

    def _record_io_snapshot(self, name: str) -> None:
        snapshots = self.collected_data.setdefault("io_stat_snapshots", {})
        snapshots[name] = _read_cgroup_io_snapshot(self.io_ext_cgroup_path)

    def _sleep_phase(self, settle_sec: float) -> None:
        deadline = time.time() + settle_sec
        while time.time() < deadline:
            if self.should_stop():
                return
            time.sleep(min(1.0, max(0.0, deadline - time.time())))

    def _build_command(self, name: str, benchmark: str, phase: Dict[str, Any], existing_db: bool) -> List[str]:
        args: Dict[str, Any] = dict(phase.get("args") or {})
        cmd = [self.dbbench_bin, f"--benchmarks={benchmark}", f"--db={self.db_path}"]
        merged: Dict[str, Any] = {}
        merged.update(self.common_args)
        merged.update(args)
        if self.use_direct_io:
            merged.setdefault("use_direct_reads", 1)
            merged.setdefault("use_direct_io_for_flush_and_compaction", 1)
        merged.setdefault("histogram", 1)
        merged.setdefault("stats_interval", 1000000)
        if existing_db:
            merged.setdefault("use_existing_db", 1)
        for key in sorted(merged):
            value = merged[key]
            if value is None:
                continue
            if isinstance(value, bool):
                value = int(value)
            cmd.append(f"--{key}={value}")
        duration = phase.get("duration")
        if duration:
            cmd.append(f"--duration={int(duration)}")
        return cmd

    def _run_benchmark(
        self, name: str, benchmark: str, phase: Dict[str, Any], out_file: str, existing_db: bool
    ) -> int:
        cmd = self._build_command(name, benchmark, phase, existing_db)
        env: Dict[str, str] = {}
        if self.rocksdb_home:
            lib_dirs = [self.rocksdb_home, os.path.join(self.rocksdb_home, "hostlibs")]
            env["LD_LIBRARY_PATH"] = ":".join(d for d in lib_dirs if os.path.isdir(d))
        wrapped = wrap_command_in_cgroup(cmd, self.io_ext_cgroup_path, env=env)
        final_cmd = list(wrapped) if not isinstance(wrapped, str) else wrapped

        logger.info("db_bench phase %s (benchmarks=%s) output -> %s", name, benchmark, out_file)
        # Fix 2026-11-05 (RQ5A_BE_COMPACT r2/r3): Scene.stop() runs end() on
        # the controller thread while _run() (and this method) executes on
        # the scene daemon thread; end() nulls self._bench_proc after
        # interrupting, which raced the poll()/returncode dereferences below
        # and raised AttributeError ('NoneType' object has no attribute
        # 'poll'/'returncode'). Every handle use below goes through this
        # local reference captured right after spawn; the shared attribute
        # is only a publication channel for the interrupt/cleanup paths.
        proc: Optional[subprocess.Popen[str]] = None
        with open(out_file, "w", encoding="utf-8") as out_f:
            try:
                proc = popen_in_own_session(
                    final_cmd,
                    stdout=out_f,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except OSError as exc:
                raise RuntimeError(
                    f"db_bench phase {name} (benchmark={benchmark}) failed to spawn "
                    f"command {final_cmd!r}: {exc}"
                ) from exc
            if proc is None:
                raise RuntimeError(
                    f"db_bench phase {name} (benchmark={benchmark}) failed to spawn "
                    f"command {final_cmd!r}: popen_in_own_session returned no process handle"
                )
            self._bench_proc = proc
            try:
                while proc.poll() is None:
                    if self.should_stop():
                        interrupt_process(proc, timeout_sec=15)
                        break
                    time.sleep(0.3)
                rc = proc.returncode
                if rc is None:
                    rc = proc.wait()
            finally:
                # Only clear the shared handle if end() (another thread) has
                # not already done so; never clobber a different/None value.
                if self._bench_proc is proc:
                    self._bench_proc = None
            if rc is None:
                raise RuntimeError(
                    f"db_bench phase {name} (benchmark={benchmark}) return code unavailable "
                    f"for command {final_cmd!r} (output {out_file})"
                )
        return int(rc or 0)


# ----------------------------------------------------------------------
# db_bench output parsing
# ----------------------------------------------------------------------
_RESULT_LINE_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*([0-9.]+) micros/op\s+([0-9.]+) ops/sec"
    r"\s+([0-9.]+) seconds\s+([0-9]+) operations(?:;\s*([0-9.]+) MB/s)?"
    r"(?:.*?\((\d+) of (\d+) found\))?",
    re.MULTILINE,
)
_SECTION_RE = re.compile(r"^Microseconds per (\w[\w ]*?)\s*:\s*$", re.MULTILINE)
_PERCENTILES_RE = re.compile(
    r"Percentiles:\s*"
    r"P50:\s*([0-9.]+)\s+P75:\s*([0-9.]+)\s+P99:\s*([0-9.]+)\s+"
    r"P99\.9:\s*([0-9.]+)\s+P99\.99:\s*([0-9.]+)"
)
_COMPACTION_PENDING_RE = re.compile(r"Compaction debt\w*\s*[:=]\s*([0-9]+)", re.IGNORECASE)


def _parse_dbbench_log(log_path: str) -> Dict[str, Any]:
    """Parse one db_bench phase log into structured metrics.

    Extracts the summary result line (ops/sec etc.) and every latency
    histogram section ("Microseconds per read:" / "... per write:" /
    "... per op:") with its percentile row.  readwhilewriting reports the
    foreground READ microbench line; readrandomwriterandom reports both a
    read and a write histogram section in that order.
    """
    result: Dict[str, Any] = {
        "ops_per_sec": None,
        "micros_per_op": None,
        "seconds": None,
        "operations": None,
        "mb_per_sec": None,
        "found": None,
        "found_of": None,
        "histograms": {},
        "compaction_debt": None,
    }
    if not os.path.isfile(log_path):
        return result
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return result

    match = None
    for candidate in _RESULT_LINE_RE.finditer(text):
        match = candidate  # keep the LAST summary line (final aggregate)
    if match:
        result["benchmark_line"] = match.group(1)
        result["micros_per_op"] = _float_or_none(match.group(2))
        result["ops_per_sec"] = _float_or_none(match.group(3))
        result["seconds"] = _float_or_none(match.group(4))
        result["operations"] = _int_or_none(match.group(5))
        result["mb_per_sec"] = _float_or_none(match.group(6))
        if match.group(7) is not None:
            result["found"] = _int_or_none(match.group(7))
            result["found_of"] = _int_or_none(match.group(8))

    for section_match in _SECTION_RE.finditer(text):
        section_name = section_match.group(1).strip().lower().replace(" ", "_")
        window = text[section_match.end(): section_match.end() + 2000]
        pct = _PERCENTILES_RE.search(window)
        if not pct:
            continue
        result["histograms"][section_name] = {
            "p50_us": _float_or_none(pct.group(1)),
            "p75_us": _float_or_none(pct.group(2)),
            "p99_us": _float_or_none(pct.group(3)),
            "p99_9_us": _float_or_none(pct.group(4)),
            "p99_99_us": _float_or_none(pct.group(5)),
        }

    debt = _COMPACTION_PENDING_RE.search(text)
    if debt:
        result["compaction_debt"] = _int_or_none(debt.group(1))
    return result


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _io_stat_delta(before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return {"exists": False}
    before_totals = before.get("totals") or {}
    after_totals = after.get("totals") or {}
    keys = sorted(set(before_totals) | set(after_totals))
    return {
        "exists": bool(before.get("exists") or after.get("exists")),
        "before_ts": before.get("timestamp"),
        "after_ts": after.get("timestamp"),
        "delta": {key: int(after_totals.get(key) or 0) - int(before_totals.get(key) or 0) for key in keys},
    }


def _du_summary(path: str) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"path": path, "exists": os.path.exists(path), "apparent_bytes": 0, "file_count": 0}
    if not summary["exists"]:
        return summary
    for dirpath, _dirnames, filenames in os.walk(path):
        for filename in filenames:
            try:
                st = os.stat(os.path.join(dirpath, filename))
            except OSError:
                continue
            summary["file_count"] += 1
            summary["apparent_bytes"] += int(st.st_size)
    return summary