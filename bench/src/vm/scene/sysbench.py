"""Self-contained io-ext sysbench OLTP scene."""

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
    cleanup_cgroup,
    expand_path,
    file_tail,
    interrupt_process,
    loader_log_paths,
    popen_in_own_session,
    run_sudo_shell,
    setup_cgroup_io_controls,
    setup_cgroup_v2,
    set_scheduler,
    start_loader_chain,
    stop_loader_chain,
    validate_block_device_name,
    wrap_command_in_cgroup,
)


logger = logging.getLogger("io_ext_sysbench")


_SUPPORTED_SYSBENCH_TESTS = {"oltp_read_only", "oltp_write_only"}
_THROUGHPUT_RE = re.compile(
    r"(?:throughput|throuput)\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*events/s",
    re.IGNORECASE,
)
_TRANSACTIONS_RE = re.compile(
    r"transactions\s*:\s*[0-9]+\s*\(([0-9]+(?:\.[0-9]+)?)\s+per\s+sec\.\)",
    re.IGNORECASE,
)
_EVENTS_RE = re.compile(
    r"events\s+per\s+second\s*:\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
_INLINE_LATENCY_RE = re.compile(r"latency\s*\(ms\)\s*:\s*(.+)", re.IGNORECASE)
_LATENCY_FIELD_RE = re.compile(
    r"\b(avg|p95|p99|p999)\s*=\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
_SYSBENCH_LATENCY_LINE_RE = re.compile(
    r"^\s*(avg|95th percentile|99th percentile|99\.9th percentile)\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class IoExtSysbenchScene(Scene):
    """Run sysbench OLTP through an io-ext loader chain and cgroup."""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        target_device = parameters.get("target_device")
        if not target_device:
            raise ValueError("parameters.target_device is required, e.g. nullb0 or nvme0n1")
        self.target_device = validate_block_device_name(str(target_device))

        self.sysbench_cfg: Dict[str, Any] = dict(parameters.get("sysbench") or {})
        test = str(self.sysbench_cfg.get("test") or "").strip()
        if not test:
            raise ValueError("parameters.sysbench.test is required")
        if test not in _SUPPORTED_SYSBENCH_TESTS:
            raise ValueError(f"parameters.sysbench.test must be one of {sorted(_SUPPORTED_SYSBENCH_TESTS)}")
        self.test = test

        self.mysql_host = str(self.sysbench_cfg.get("mysql_host", "localhost"))
        self.mysql_port = int(self.sysbench_cfg.get("mysql_port", 3306))
        self.mysql_user = str(self.sysbench_cfg.get("mysql_user", "root"))
        self.mysql_password = str(self.sysbench_cfg.get("mysql_password", ""))
        self.mysql_db = str(self.sysbench_cfg.get("mysql_db", "sbtest"))
        self.tables = int(self.sysbench_cfg.get("tables", 1))
        self.table_size = int(self.sysbench_cfg.get("table_size", 10000))
        self.threads = int(self.sysbench_cfg.get("threads", 1))
        self.time_sec = int(self.sysbench_cfg.get("time", 60))
        self.rand_type = str(self.sysbench_cfg.get("rand_type", "uniform"))

        self.system_cfg: Dict[str, Any] = dict(parameters.get("system") or {})
        self.output_dir = expand_path(parameters.get("output_dir")) or "./test_results"
        self.job_name = parameters.get("job_name", "arena_sysbench")

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

        self.drop_caches_before_run = parameters.get("drop_caches_before_run", True)
        self.cooldown_sec = float(parameters.get("cooldown_sec", 1.0))
        self.duration = int(parameters.get("duration", self.time_sec + self.cooldown_sec + 30))
        self.score_threshold = float(parameters.get("score_threshold", self.sysbench_cfg.get("score_threshold", 100.0)))

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self._sysbench_proc: Optional[subprocess.Popen[str]] = None
        self._sysbench_stdout_paths: List[str] = []
        self._sysbench_stderr_paths: List[str] = []

    def setup(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        dev_path = f"/dev/{self.target_device}"
        if not os.path.exists(dev_path):
            raise FileNotFoundError(f"block device does not exist: {dev_path}")
        if self.io_ext_cgroup_path:
            setup_cgroup_v2(self.io_ext_cgroup_path, self.io_ext_cgroup_controllers)

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        self._apply_cgroup_io_controls()
        self.collected_data = {
            "runs": [],
            "target_device": self.target_device,
            "sysbench": self._safe_sysbench_config(),
            "scheduler": self.scheduler,
            "io_ext_cgroup_path": self.io_ext_cgroup_path,
            "io_ext_cgroup_controls": self.io_ext_cgroup_controls,
        }
        self._sysbench_stdout_paths = []
        self._sysbench_stderr_paths = []
        self.start_time = None
        self.end_time = None

    def _safe_sysbench_config(self) -> Dict[str, Any]:
        return {
            "test": self.test,
            "mysql_host": self.mysql_host,
            "mysql_port": self.mysql_port,
            "mysql_user": self.mysql_user,
            "mysql_password_configured": bool(self.mysql_password),
            "mysql_db": self.mysql_db,
            "tables": self.tables,
            "table_size": self.table_size,
            "threads": self.threads,
            "time": self.time_sec,
            "rand_type": self.rand_type,
        }

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

    def _build_sysbench_argv(self) -> List[str]:
        return [
            "sysbench",
            f"--test={self.test}",
            f"--mysql-host={self.mysql_host}",
            f"--mysql-port={self.mysql_port}",
            f"--mysql-user={self.mysql_user}",
            f"--mysql-password={self.mysql_password}",
            f"--mysql-db={self.mysql_db}",
            f"--tables={self.tables}",
            f"--table-size={self.table_size}",
            f"--threads={self.threads}",
            f"--time={self.time_sec}",
            f"--rand-type={self.rand_type}",
            "run",
        ]

    def _build_full_cmd(self, sysbench_argv: List[str]) -> List[str]:
        cmd: List[str] = []
        cpu_bind = self.system_cfg.get("cpu_bind")
        if cpu_bind:
            cmd.extend(["taskset", "-c", str(cpu_bind)])
        cmd.extend(sysbench_argv)
        wrapped = wrap_command_in_cgroup(cmd, self.io_ext_cgroup_path)
        return list(wrapped) if not isinstance(wrapped, str) else [BASH_BIN, "-lc", wrapped]

    @staticmethod
    def _redact_command(argv: List[str]) -> List[str]:
        return ["--mysql-password=<redacted>" if arg.startswith("--mysql-password=") else arg for arg in argv]

    @staticmethod
    def _parse_sysbench_output(output: str) -> Dict[str, Any]:
        metrics: Dict[str, Any] = {}

        throughput_match = _THROUGHPUT_RE.search(output) or _TRANSACTIONS_RE.search(output) or _EVENTS_RE.search(output)
        if throughput_match:
            metrics["throughput_events_per_sec"] = float(throughput_match.group(1))

        inline_latency = _INLINE_LATENCY_RE.search(output)
        if inline_latency:
            for key, raw_value in _LATENCY_FIELD_RE.findall(inline_latency.group(1)):
                normalized = key.lower()
                if normalized == "avg":
                    metrics["latency_avg_ms"] = float(raw_value)
                elif normalized == "p99":
                    metrics["latency_p99_ms"] = float(raw_value)
                elif normalized == "p999":
                    metrics["latency_p999_ms"] = float(raw_value)
                elif normalized == "p95":
                    metrics["latency_p95_ms"] = float(raw_value)

        for label, raw_value in _SYSBENCH_LATENCY_LINE_RE.findall(output):
            normalized = label.lower()
            if normalized == "avg":
                metrics.setdefault("latency_avg_ms", float(raw_value))
            elif normalized == "95th percentile":
                metrics.setdefault("latency_p95_ms", float(raw_value))
            elif normalized == "99th percentile":
                metrics.setdefault("latency_p99_ms", float(raw_value))
            elif normalized == "99.9th percentile":
                metrics.setdefault("latency_p999_ms", float(raw_value))

        return metrics

    def _run(self) -> None:
        self.start_time = time.time()
        if self.drop_caches_before_run:
            self._drop_caches()

        self._set_scheduler()
        self._start_loader()

        try:
            if self.should_stop():
                return

            stdout_path = os.path.join(self.output_dir, f"{self.scene_id}_sysbench.stdout.log")
            stderr_path = os.path.join(self.output_dir, f"{self.scene_id}_sysbench.stderr.log")
            self._sysbench_stdout_paths.append(stdout_path)
            self._sysbench_stderr_paths.append(stderr_path)

            sysbench_argv = self._build_sysbench_argv()
            full_cmd = self._build_full_cmd(sysbench_argv)
            logger.info("running io-ext sysbench: cmd=%s", " ".join(self._redact_command(full_cmd)))

            with open(stdout_path, "w", encoding="utf-8") as stdout_f, open(
                stderr_path,
                "w",
                encoding="utf-8",
            ) as stderr_f:
                proc = popen_in_own_session(
                    full_cmd,
                    stdout=stdout_f,
                    stderr=stderr_f,
                    text=True,
                )
                self._sysbench_proc = proc
                while proc.poll() is None:
                    if self.should_stop():
                        interrupt_process(proc)
                        break
                    time.sleep(0.2)

            rc = proc.returncode
            stdout_tail = file_tail(stdout_path, max_chars=20000)
            stderr_tail = file_tail(stderr_path, max_chars=4000)
            if not self.should_stop() and rc not in (0, None):
                raise RuntimeError(f"sysbench failed returncode={rc} stderr={stderr_tail[:2000]!r}")

            output = "\n".join(part for part in (stdout_tail, stderr_tail) if part)
            run_data = {
                "test": self.test,
                "threads": self.threads,
                "time": self.time_sec,
                "stdout_log": stdout_path,
                "stderr_log": stderr_path,
                "returncode": rc,
            }
            if stdout_tail:
                run_data["stdout_tail"] = stdout_tail
            if stderr_tail:
                run_data["stderr_tail"] = stderr_tail
            run_data.update(self._parse_sysbench_output(output))
            self.collected_data["runs"].append(run_data)
            self._sysbench_proc = None
            time.sleep(self.cooldown_sec)
        finally:
            self._stop_loader()
            self.end_time = time.time()

    def end(self) -> None:
        interrupt_process(self._sysbench_proc)
        self._sysbench_proc = None
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
        path = os.path.join(self.output_dir, f"sysbench_{self.scene_id}_{int(time.time())}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.collected_data, f, indent=2, ensure_ascii=False, sort_keys=True)
        return [
            path,
            *[stdout_path for stdout_path in self._sysbench_stdout_paths if os.path.isfile(stdout_path)],
            *[stderr_path for stderr_path in self._sysbench_stderr_paths if os.path.isfile(stderr_path)],
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
        throughputs = [
            float(run["throughput_events_per_sec"])
            for run in runs
            if run.get("throughput_events_per_sec") is not None
        ]
        if not throughputs:
            logger.warning("sysbench scene has no throughput_events_per_sec; returning zero score")
            return 0.0
        self.duration = float(self.collected_data.get("duration_wall") or 0.0)
        scorer = DataScorer(
            data=throughputs,
            threshold=self.score_threshold,
            positive=True,
            value_exponent=0.5,
        )
        score = scorer.calculate_score()
        mean_throughput = sum(throughputs) / len(throughputs)
        logger.info("sysbench throughput %.2f events/s, score %.2f", mean_throughput, score)
        return float(score)
