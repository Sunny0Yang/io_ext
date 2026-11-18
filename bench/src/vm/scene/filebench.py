"""Self-contained io-ext FileBench scene."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from vm.scene.base_scene import Scene
from vm.scene.utils import DataScorer

from .common import (
    BASH_BIN,
    SUDO_BIN,
    apply_block_device_tuning,
    cleanup_cgroup,
    expand_path,
    file_tail,
    interrupt_process,
    loader_log_paths,
    popen_in_own_session,
    run_sudo_shell,
    setup_cgroup_v2,
    setup_cgroup_io_controls,
    set_scheduler,
    start_loader_chain,
    stop_loader_chain,
    validate_block_device_name,
    wrap_command_in_cgroup,
)


logger = logging.getLogger("io_ext_filebench")


_SUPPORTED_WORKLOADS = {"varmail", "videoserver", "fileserver"}
_DEFAULT_PROFILE_DIR = "/usr/local/share/filebench/workloads"
_DEFAULT_WORKLOAD_DIR = "/tmp/filebench_data"
_SET_LINE_RE = re.compile(r"^(?P<prefix>\s*set\s+\$)(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<sep>\s*=\s*)(?P<value>.*)$")
_RUN_LINE_RE = re.compile(r"^\s*run(?:\s+\S+)?\s*$")
_OPS_PER_SEC_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*ops/s", re.IGNORECASE)
_MB_PER_SEC_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*mb/s", re.IGNORECASE)
_MS_PER_OP_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*ms/op", re.IGNORECASE)
_IO_SUMMARY_OPS_RE = re.compile(r"IO\s+Summary:\s*([0-9]+(?:\.[0-9]+)?)\s*ops", re.IGNORECASE)


class IoExtFilebenchScene(Scene):
    """Run FileBench through an io-ext loader chain and cgroup."""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        target_device = parameters.get("target_device")
        if not target_device:
            raise ValueError("parameters.target_device is required, e.g. nullb0 or nvme0n1")
        self.target_device = validate_block_device_name(str(target_device))

        self.filebench_cfg: Dict[str, Any] = dict(parameters.get("filebench") or {})
        self.workload = str(self.filebench_cfg.get("workload") or "").strip().lower()
        if self.workload not in _SUPPORTED_WORKLOADS:
            raise ValueError("filebench.workload must be one of: fileserver, varmail, videoserver")
        if not self.filebench_cfg.get("runtime_sec"):
            raise ValueError("filebench.runtime_sec is required")
        self.runtime_sec = int(self.filebench_cfg["runtime_sec"])
        if self.runtime_sec <= 0:
            raise ValueError("filebench.runtime_sec must be positive")
        self.threads = int(self.filebench_cfg.get("threads", 1))
        if self.threads <= 0:
            raise ValueError("filebench.threads must be positive")
        self.workload_dir = expand_path(
            self.filebench_cfg.get("workload_dir") or os.environ.get("FILEBENCH_DATA_DIR") or _DEFAULT_WORKLOAD_DIR
        ) or _DEFAULT_WORKLOAD_DIR
        self.file_size = self.filebench_cfg.get("file_size")
        self.num_files = self.filebench_cfg.get("num_files")

        self.filebench_bin = self._resolve_filebench_bin()
        self.profile_dir = expand_path(
            self.filebench_cfg.get("profile_dir")
            or os.environ.get("FILEBENCH_WORKLOAD_PROFILE_DIR")
            or os.environ.get("FILEBENCH_WORKLOAD_DIR")
            or _DEFAULT_PROFILE_DIR
        ) or _DEFAULT_PROFILE_DIR
        self.source_profile = self._resolve_source_profile()

        self.system_cfg: Dict[str, Any] = dict(parameters.get("system") or {})
        self.output_dir = expand_path(parameters.get("output_dir")) or "./test_results"

        io_ext = parameters.get("io_ext") or {}
        self.io_ext_cfg: Dict[str, Any] = dict(io_ext)
        self.scheduler = io_ext.get("scheduler", parameters.get("scheduler"))
        cgroup_cfg = io_ext.get("cgroup") or {}
        self.io_ext_cgroup_path = expand_path(cgroup_cfg.get("path") or io_ext.get("cgroup_path"))
        self.io_ext_cgroup_controllers = list(cgroup_cfg.get("controllers") or ["io"])
        self.io_ext_cgroup_io_weight = cgroup_cfg.get("io_weight", cgroup_cfg.get("io.weight"))
        self.io_ext_cgroup_io_max = cgroup_cfg.get("io_max", cgroup_cfg.get("io.max"))
        if (self.io_ext_cgroup_io_weight is not None or self.io_ext_cgroup_io_max is not None) and not self.io_ext_cgroup_path:
            raise ValueError("io_ext.cgroup.path is required when io_weight or io_max is configured")
        if self.io_ext_cgroup_io_weight is not None or self.io_ext_cgroup_io_max is not None:
            enabled_controllers = {str(ctrl).strip().lstrip("+") for ctrl in self.io_ext_cgroup_controllers}
            if "io" not in enabled_controllers:
                self.io_ext_cgroup_controllers.append("io")
        self.io_ext_cgroup_controls: Dict[str, Any] = {}
        self._loader_procs: List[subprocess.Popen[str]] = []

        null_blk_cfg = parameters.get("null_blk")
        self._null_blk_config: Optional[Dict[str, Any]] = None
        if null_blk_cfg is True:
            self._null_blk_config = {"queue_mode": 2, "submit_queues": 64}
        elif isinstance(null_blk_cfg, dict):
            self._null_blk_config = dict(null_blk_cfg)
        self._remove_null_blk_on_teardown = bool(parameters.get("remove_null_blk_on_teardown", False))

        self.drop_caches_before_run = parameters.get("drop_caches_before_run", True)
        self.cooldown_sec = float(parameters.get("cooldown_sec", 1.0))
        self.score_threshold = float(parameters.get("score_threshold", self.filebench_cfg.get("score_threshold", 1000.0)))
        self.duration = int(parameters.get("duration", self.runtime_sec + self.cooldown_sec + 30))

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self._filebench_proc: Optional[subprocess.Popen[str]] = None
        self._filebench_profile_paths: List[str] = []
        self._filebench_stdout_paths: List[str] = []
        self._filebench_stderr_paths: List[str] = []

    def setup(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        if self._null_blk_config:
            run_sudo_shell("rmmod null_blk 2>/dev/null || true", check=False)
            time.sleep(0.5)
            queue_mode = int(self._null_blk_config.get("queue_mode", 2))
            submit_queues = int(self._null_blk_config.get("submit_queues", 64))
            run_sudo_shell(f"modprobe null_blk queue_mode={queue_mode} submit_queues={submit_queues}", check=True)
            time.sleep(1.5)
        dev_path = f"/dev/{self.target_device}"
        if not os.path.exists(dev_path):
            raise FileNotFoundError(f"block device does not exist: {dev_path}")
        tuning = apply_block_device_tuning(self.target_device, self.system_cfg, self._null_blk_config)
        if tuning:
            self.system_cfg = {**self.system_cfg, **tuning}
        if not os.path.isfile(self.source_profile):
            raise FileNotFoundError(f"filebench workload profile does not exist: {self.source_profile}")
        if self.workload_dir:
            os.makedirs(self.workload_dir, exist_ok=True)
        if self.io_ext_cgroup_path:
            setup_cgroup_v2(self.io_ext_cgroup_path, self.io_ext_cgroup_controllers)

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        if self.workload_dir:
            os.makedirs(self.workload_dir, exist_ok=True)
        self._apply_cgroup_io_controls()
        self.collected_data = {
            "runs": [],
            "target_device": self.target_device,
            "workload": self.workload,
            "runtime_sec": self.runtime_sec,
            "threads": self.threads,
            "workload_dir": self.workload_dir,
            "source_profile": self.source_profile,
            "scheduler": self.scheduler,
            "io_ext_cgroup_path": self.io_ext_cgroup_path,
            "io_ext_cgroup_controls": self.io_ext_cgroup_controls,
        }
        self._filebench_profile_paths = []
        self._filebench_stdout_paths = []
        self._filebench_stderr_paths = []
        self.start_time = None
        self.end_time = None

    def _resolve_filebench_bin(self) -> str:
        configured = self.filebench_cfg.get("binary") or self.filebench_cfg.get("filebench_bin")
        if configured:
            return expand_path(str(configured)) or str(configured)
        env_bin = os.environ.get("FILEBENCH_BIN")
        if env_bin:
            return expand_path(env_bin) or env_bin
        default_bin = "/usr/local/bin/filebench"
        return default_bin if os.path.exists(default_bin) else "filebench"

    def _resolve_source_profile(self) -> str:
        configured = self.filebench_cfg.get("profile") or self.filebench_cfg.get("profile_path")
        if configured:
            profile = expand_path(str(configured)) or str(configured)
            return profile
        if not self.profile_dir:
            raise ValueError("filebench.profile_dir resolved to an empty path")
        return os.path.join(self.profile_dir, f"{self.workload}.f")

    @staticmethod
    def _filebench_value(value: Any) -> str:
        text = str(value).strip()
        if not text or "\n" in text or "\r" in text:
            raise ValueError(f"invalid filebench profile value: {value!r}")
        return text

    def _profile_overrides(self) -> Dict[str, str]:
        overrides = {
            "dir": self._filebench_value(self.workload_dir),
            "nthreads": self._filebench_value(self.threads),
            "threads": self._filebench_value(self.threads),
            "runtime": self._filebench_value(self.runtime_sec),
        }
        if self.file_size is not None:
            size = self._filebench_value(self.file_size)
            overrides.update({"filesize": size, "meanfilesize": size})
        if self.num_files is not None:
            files = self._filebench_value(self.num_files)
            overrides.update({"nfiles": files, "files": files})
        return overrides

    def _write_filebench_profile(self) -> str:
        profile_path = os.path.join(self.output_dir, f"{self.scene_id}_{self.workload}_filebench.f")
        overrides = self._profile_overrides()
        run_line_seen = False
        lines: List[str] = []

        with open(self.source_profile, "r", encoding="utf-8", errors="replace") as src:
            for line in src:
                stripped = line.rstrip("\n")
                set_match = _SET_LINE_RE.match(stripped)
                if set_match and set_match.group("name") in overrides:
                    name = set_match.group("name")
                    lines.append(f"{set_match.group('prefix')}{name}{set_match.group('sep')}{overrides[name]}")
                    continue
                if _RUN_LINE_RE.match(stripped):
                    lines.append(f"run {self.runtime_sec}")
                    run_line_seen = True
                    continue
                lines.append(stripped)

        if not run_line_seen:
            lines.append(f"run {self.runtime_sec}")

        with open(profile_path, "w", encoding="utf-8") as dst:
            dst.write("\n".join(lines) + "\n")
        self._filebench_profile_paths.append(profile_path)
        return profile_path

    def _drop_caches(self) -> None:
        run_sudo_shell("sync; printf '%s\n' 3 > /proc/sys/vm/drop_caches", check=False)

    def _set_scheduler(self) -> None:
        set_scheduler(self.target_device, self.scheduler)

    def _apply_cgroup_io_controls(self) -> None:
        self.io_ext_cgroup_controls = setup_cgroup_io_controls(
            self.io_ext_cgroup_path,
            f"/dev/{self.target_device}",
            self.io_ext_cgroup_io_weight,
            self.io_ext_cgroup_io_max,
        )

    def _start_loader(self) -> None:
        self._loader_procs = start_loader_chain(self.parameters, self.output_dir, self.io_ext_cgroup_path)

    def _stop_loader(self) -> None:
        if self._loader_procs:
            self.collected_data.setdefault("loader_logs", loader_log_paths(self._loader_procs))
        stop_loader_chain(self._loader_procs)
        self._loader_procs = []

    def _build_filebench_argv(self, profile_path: str) -> List[str]:
        return [self.filebench_bin, "-f", profile_path]

    def _build_full_cmd(self, filebench_argv: List[str]) -> List[str]:
        cmd: List[str] = [SUDO_BIN]
        cpu_bind = self.system_cfg.get("cpu_bind")
        if cpu_bind:
            cmd.extend(["taskset", "-c", str(cpu_bind)])
        cmd.extend(filebench_argv)
        wrapped = wrap_command_in_cgroup(cmd, self.io_ext_cgroup_path)
        return list(wrapped) if not isinstance(wrapped, str) else [BASH_BIN, "-lc", wrapped]

    @staticmethod
    def _last_float(pattern: re.Pattern[str], text: str) -> Optional[float]:
        matches = pattern.findall(text)
        if not matches:
            return None
        try:
            return float(matches[-1])
        except ValueError:
            return None

    def _parse_filebench_output(self, text: str) -> Dict[str, Any]:
        metrics: Dict[str, Any] = {}
        total_ops = self._last_float(_IO_SUMMARY_OPS_RE, text)
        ops_per_sec = self._last_float(_OPS_PER_SEC_RE, text)
        mb_per_sec = self._last_float(_MB_PER_SEC_RE, text)
        ms_per_op = self._last_float(_MS_PER_OP_RE, text)
        if total_ops is not None:
            metrics["total_ops"] = total_ops
        if ops_per_sec is not None:
            metrics["throughput_ops_per_sec"] = ops_per_sec
        if mb_per_sec is not None:
            metrics["agg_bw_mbps"] = mb_per_sec
        if ms_per_op is not None:
            metrics["avg_latency_ms"] = ms_per_op
        return metrics

    def _run(self) -> None:
        self.start_time = time.time()
        if self.drop_caches_before_run:
            self._drop_caches()

        self._set_scheduler()
        self._start_loader()

        try:
            if not self.should_stop():
                profile_path = self._write_filebench_profile()
                stdout_path = os.path.join(self.output_dir, f"{self.scene_id}_{self.workload}_filebench.stdout.log")
                stderr_path = os.path.join(self.output_dir, f"{self.scene_id}_{self.workload}_filebench.stderr.log")
                self._filebench_stdout_paths.append(stdout_path)
                self._filebench_stderr_paths.append(stderr_path)

                filebench_argv = self._build_filebench_argv(profile_path)
                full_cmd = self._build_full_cmd(filebench_argv)
                logger.info("running io-ext filebench: workload=%s cmd=%s", self.workload, " ".join(full_cmd))

                with open(stdout_path, "w", encoding="utf-8") as stdout_f, open(
                    stderr_path, "w", encoding="utf-8"
                ) as stderr_f:
                    proc = popen_in_own_session(
                        full_cmd,
                        stdout=stdout_f,
                        stderr=stderr_f,
                        text=True,
                    )
                    self._filebench_proc = proc
                    while proc.poll() is None:
                        if self.should_stop():
                            interrupt_process(proc)
                            break
                        time.sleep(0.2)

                rc = proc.returncode
                out = file_tail(stdout_path, max_chars=12000)
                err = file_tail(stderr_path, max_chars=4000)
                if not self.should_stop() and rc not in (0, None):
                    raise RuntimeError(f"filebench failed returncode={rc} stderr={err[:2000]!r}")

                run_data = {
                    "workload": self.workload,
                    "runtime_sec": self.runtime_sec,
                    "threads": self.threads,
                    "profile": profile_path,
                    "stdout_log": stdout_path,
                    "stderr_log": stderr_path,
                    "returncode": rc,
                }
                if out:
                    run_data["stdout_tail"] = out
                if err:
                    run_data["stderr_tail"] = err
                run_data.update(self._parse_filebench_output(f"{out}\n{err}"))
                self.collected_data["runs"].append(run_data)
                self._filebench_proc = None
                time.sleep(self.cooldown_sec)
        finally:
            self._stop_loader()
            self.end_time = time.time()

    def end(self) -> None:
        interrupt_process(self._filebench_proc)
        self._filebench_proc = None
        self._stop_loader()

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        if self._null_blk_config and self._remove_null_blk_on_teardown:
            run_sudo_shell("rmmod null_blk 2>/dev/null || true", check=False)
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
        path = os.path.join(self.output_dir, f"filebench_{self.scene_id}_{int(time.time())}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.collected_data, f, indent=2, ensure_ascii=False, sort_keys=True)
        return [
            path,
            *[profile_path for profile_path in self._filebench_profile_paths if os.path.isfile(profile_path)],
            *[stdout_path for stdout_path in self._filebench_stdout_paths if os.path.isfile(stdout_path)],
            *[stderr_path for stderr_path in self._filebench_stderr_paths if os.path.isfile(stderr_path)],
        ]

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.start_time = self.collected_data.get("start_time")
        self.end_time = self.collected_data.get("end_time")
        duration = self.collected_data.get("duration_wall")
        self.duration = float(duration) if duration is not None else 0.0

    def score(self) -> float:
        runs = self.collected_data.get("runs") or []
        throughputs = [run["throughput_ops_per_sec"] for run in runs if run.get("throughput_ops_per_sec") is not None]
        if not throughputs:
            throughputs = [run["agg_bw_mbps"] for run in runs if run.get("agg_bw_mbps") is not None]
        if not throughputs:
            logger.warning("FileBench scene has no throughput metrics; returning zero score")
            return 0.0
        mean_throughput = sum(float(value) for value in throughputs) / len(throughputs)
        self.duration = float(self.collected_data.get("duration_wall") or 0.0)
        scorer = DataScorer(
            data=[mean_throughput, mean_throughput],
            threshold=self.score_threshold,
            positive=True,
            value_exponent=0.5,
        )
        score = scorer.calculate_score()
        logger.info("FileBench throughput %.2f, score %.2f", mean_throughput, score)
        return float(score)
