"""io-ext standalone trace replay smoke scene."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from vm.scene.base_scene import Scene
from vm.scene.utils import DataScorer

from .common import (
    BASH_BIN,
    cleanup_cgroup,
    command_to_shell,
    ensure_output_dir,
    expand_path,
    interrupt_process,
    list_output_paths,
    loader_log_paths,
    popen_in_own_session,
    read_json,
    setup_cgroup_v2,
    set_scheduler,
    start_loader_chain,
    stop_loader_chain,
    validate_block_device_name,
    wrap_command_in_cgroup,
    write_json_stat,
)


logger = logging.getLogger("io_ext_trace_replay")


class IoExtTraceReplayScene(Scene):
    """Replay a Tencent-style block trace with the in-repository C helper."""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}
        self.output_dir = expand_path(parameters.get("output_dir")) or "./test_results/io_ext/trace_replay"
        self.target_device = validate_block_device_name(str(parameters.get("target_device", "nvme0n1")))
        io_ext = parameters.get("io_ext") or {}
        self.scheduler = io_ext.get("scheduler", parameters.get("scheduler"))
        cgroup_cfg = io_ext.get("cgroup") or {}
        self.cgroup_path = expand_path(cgroup_cfg.get("path") or io_ext.get("cgroup_path"))
        self.cgroup_controllers = list(cgroup_cfg.get("controllers") or ["io"])

        replay = parameters.get("replay") or {}
        self.trace_file = expand_path(replay.get("file") or parameters.get("trace_file"))
        self.trace_format = str(replay.get("trace_format", "tencent_csv") or "tencent_csv").strip().lower()
        self.replay_tool = expand_path(replay.get("tool") or "src/vm/assets/trace_replay/trace_replay")
        self.device_arg = replay.get("device") or f"/dev/{self.target_device}"
        self.replay_output_dir = expand_path(replay.get("output_dir")) or os.path.join(self.output_dir, "replay_output")
        self.max_lines = int(replay.get("max_lines", 0) or 0)
        self.speedup = float(replay.get("speedup", 1.0) or 1.0)
        self.direct_align = int(replay.get("direct_align", 4096) or 4096)
        self.no_stall = _to_bool(replay.get("no_stall", False))
        self.write_enable = _to_bool(replay.get("write_enable", False))
        self.buffered = _to_bool(replay.get("buffered", False))
        self.build_tool = _to_bool(replay.get("build_tool", True))
        self.require_metrics = _to_bool(replay.get("require_metrics", True))

        self.duration = int(parameters.get("duration", 180))
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self._proc: Optional[subprocess.Popen[str]] = None
        self._loader_procs: List[subprocess.Popen[str]] = []
        self._stats_json: Optional[str] = None
        self._io_log: Optional[str] = None

    def setup(self) -> None:
        ensure_output_dir(self.output_dir)
        if self.cgroup_path:
            setup_cgroup_v2(self.cgroup_path, self.cgroup_controllers)
        if self.trace_format not in {"tencent", "tencent_csv"}:
            raise ValueError(f"unsupported trace format: {self.trace_format}")
        if not self.trace_file or not os.path.isfile(self.trace_file):
            raise FileNotFoundError(f"trace file not found: {self.trace_file}")
        self._ensure_replay_tool()

    def _ensure_replay_tool(self) -> None:
        replay_tool = self.replay_tool
        if replay_tool and os.path.isfile(replay_tool):
            if not os.access(replay_tool, os.X_OK):
                os.chmod(replay_tool, os.stat(replay_tool).st_mode | 0o111)
            if not self.build_tool or self._replay_tool_is_current():
                return
        if not self.build_tool:
            raise FileNotFoundError(f"trace replay helper not found: {self.replay_tool}")
        replay_tool = self.replay_tool
        if not replay_tool:
            raise FileNotFoundError("trace replay helper path is empty")
        tool_dir = os.path.dirname(replay_tool)
        makefile = os.path.join(tool_dir, "Makefile")
        if not tool_dir or not os.path.isfile(makefile):
            raise FileNotFoundError(f"trace replay helper and Makefile not found: {replay_tool}")
        result = subprocess.run(["make", "-C", tool_dir], text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                "failed to build trace replay helper "
                f"returncode={result.returncode} stdout={result.stdout[-2000:]} stderr={result.stderr[-2000:]}"
            )
        if not os.path.isfile(replay_tool):
            raise FileNotFoundError(f"trace replay helper build did not create: {replay_tool}")
        os.chmod(replay_tool, os.stat(replay_tool).st_mode | 0o111)

    def _replay_tool_is_current(self) -> bool:
        replay_tool = self.replay_tool
        if not replay_tool or not os.path.isfile(replay_tool):
            return False
        try:
            tool_mtime = os.path.getmtime(replay_tool)
        except OSError:
            return False
        tool_dir = os.path.dirname(replay_tool)
        for dependency in (os.path.join(tool_dir, "trace_replay.c"), os.path.join(tool_dir, "Makefile")):
            try:
                if os.path.getmtime(dependency) > tool_mtime:
                    return False
            except OSError:
                continue
        return True

    def prepare(self) -> None:
        ensure_output_dir(self.output_dir)
        ensure_output_dir(self.replay_output_dir)
        self._stats_json = os.path.join(self.replay_output_dir, f"{self.scene_id}_stats.json")
        self._io_log = os.path.join(self.replay_output_dir, f"{self.scene_id}_iolog.csv")
        self.collected_data = {
            "mode": "trace_replay_c",
            "target_device": self.target_device,
            "scheduler": self.scheduler,
            "output_dir": self.output_dir,
            "replay_output_dir": self.replay_output_dir,
            "trace_format": self.trace_format,
            "trace_file": self.trace_file,
            "trace_max_lines": self.max_lines,
            "speedup": self.speedup,
            "direct_align": self.direct_align,
            "no_stall": self.no_stall,
            "write_enable": self.write_enable,
            "buffered": self.buffered,
            "replay_tool": self.replay_tool,
            "stats_json": self._stats_json,
            "io_log": self._io_log,
            "require_metrics": self.require_metrics,
        }
        self.start_time = None
        self.end_time = None

    def _build_replay_command(self) -> List[str]:
        if not self._stats_json or not self._io_log:
            raise RuntimeError("scene was not prepared")
        cmd = [
            self.replay_tool,
            "--device",
            str(self.device_arg),
            "--trace",
            str(self.trace_file),
            "--output-dir",
            str(self.replay_output_dir),
            "--stats-json",
            self._stats_json,
            "--io-log",
            self._io_log,
            "--speedup",
            str(self.speedup),
            "--align",
            str(self.direct_align),
        ]
        if self.max_lines > 0:
            cmd.extend(["--max-lines", str(self.max_lines)])
        if self.no_stall:
            cmd.append("--no-stall")
        if self.write_enable:
            cmd.append("--write-enable")
        if self.buffered:
            cmd.append("--buffered")
        return cmd

    def _run_command(self, cmd: List[str]) -> Dict[str, Any]:
        wrapped = wrap_command_in_cgroup(cmd, self.cgroup_path)
        final_cmd = list(wrapped) if not isinstance(wrapped, str) else [BASH_BIN, "-lc", wrapped]
        stdout_path = os.path.join(self.output_dir, f"{self.scene_id}_stdout.txt")
        stderr_path = os.path.join(self.output_dir, f"{self.scene_id}_stderr.txt")
        logger.info("running trace replay command: %s", command_to_shell(final_cmd))
        with open(stdout_path, "w", encoding="utf-8") as out_f, open(stderr_path, "w", encoding="utf-8") as err_f:
            proc = popen_in_own_session(final_cmd, stdout=out_f, stderr=err_f, text=True)
            self._proc = proc
            while proc.poll() is None:
                if self.should_stop():
                    interrupt_process(proc)
                    break
                time.sleep(0.2)
            rc = proc.returncode if proc.returncode is not None else proc.wait()
        if self._proc is proc:
            self._proc = None
        return {"returncode": int(rc or 0), "stdout_txt": stdout_path, "stderr_txt": stderr_path}

    def _run(self) -> None:
        self.start_time = time.time()
        set_scheduler(self.target_device, self.scheduler)
        self._loader_procs = start_loader_chain(self.parameters, self.output_dir, self.cgroup_path)
        try:
            result = self._run_command(self._build_replay_command())
            self.collected_data.update(result)
            self.collected_data["output_paths"] = list_output_paths(self.replay_output_dir, ["**/*"])
            self.collected_data.update(self._read_replay_stats())
            error_logs = [path for path in (result["stdout_txt"], result["stderr_txt"]) if _file_has_io_errors(path)]
            if error_logs:
                raise RuntimeError(f"trace replay output contains I/O error markers: {error_logs}")
            if result["returncode"] != 0 and not self.should_stop():
                raise RuntimeError(
                    "trace replay failed with "
                    f"returncode={result['returncode']} stdout={result['stdout_txt']} stderr={result['stderr_txt']}"
                )
            if self.require_metrics and not self._has_replay_metric():
                raise RuntimeError("trace replay completed without throughput, IOPS, bandwidth, or latency metrics")
        finally:
            if self._loader_procs:
                self.collected_data.setdefault("loader_logs", loader_log_paths(self._loader_procs))
            stop_loader_chain(self._loader_procs)
            self._loader_procs = []
            self.end_time = time.time()

    def _read_replay_stats(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {}
        paths: List[str] = []
        if self._stats_json:
            paths.append(self._stats_json)
        paths.extend(list_output_paths(self.replay_output_dir, ["**/*.json"]))
        for path in paths:
            if self._is_stale_output(path):
                continue
            data = read_json(path)
            if not data:
                continue
            stats.setdefault("stats_json", path)
            for key in (
                "throughput",
                "throughput_ops_per_sec",
                "iops",
                "bw_mbps",
                "avg_latency_us",
                "p50_latency_us",
                "p95_latency_us",
                "p99_latency_us",
                "total_ios",
                "completed_ios",
                "errors",
                "late_rate_pct",
            ):
                value = data.get(key)
                if isinstance(value, (int, float)):
                    stats[key] = float(value)
        return stats

    def _is_stale_output(self, path: str) -> bool:
        if not self.start_time:
            return False
        try:
            return os.path.getmtime(path) + 0.001 < self.start_time
        except OSError:
            return True

    def _has_replay_metric(self) -> bool:
        return any(
            isinstance(self.collected_data.get(key), (int, float)) and float(self.collected_data[key]) > 0
            for key in ("bw_mbps", "throughput_ops_per_sec", "throughput", "iops", "p99_latency_us", "avg_latency_us")
        )

    def end(self) -> None:
        interrupt_process(self._proc)
        self._proc = None
        stop_loader_chain(self._loader_procs)
        self._loader_procs = []

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        cleanup_cgroup(self.cgroup_path)

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
        stat_path = write_json_stat(self.output_dir, "trace_replay", self.scene_id, self.collected_data)
        paths = [stat_path]
        for key in ("stats_json", "io_log", "stdout_txt", "stderr_txt"):
            path = self.collected_data.get(key)
            if isinstance(path, str) and os.path.isfile(path) and path not in paths and not self._is_stale_output(path):
                paths.append(path)
        return paths

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.start_time = self.collected_data.get("start_time")
        self.end_time = self.collected_data.get("end_time")
        self.duration = float(self.collected_data.get("duration_wall") or 0.0)

    def score(self) -> float:
        returncode = self.collected_data.get("returncode")
        if returncode not in (0, None):
            return 0.0
        errors = self.collected_data.get("errors")
        if isinstance(errors, (int, float)) and float(errors) > 0:
            return 0.0
        positive_metric = self._positive_metric()
        if positive_metric and positive_metric > 0:
            scorer = DataScorer(data=[positive_metric, positive_metric], threshold=100.0, positive=True, value_exponent=0.5)
            return float(scorer.calculate_score())
        p99 = self.collected_data.get("p99_latency_us")
        if isinstance(p99, (int, float)) and p99 > 0:
            scorer = DataScorer(data=[float(p99), float(p99)], threshold=50000.0, positive=False, value_exponent=0.5)
            return float(scorer.calculate_score())
        avg = self.collected_data.get("avg_latency_us")
        if isinstance(avg, (int, float)) and avg > 0:
            scorer = DataScorer(data=[float(avg), float(avg)], threshold=50000.0, positive=False, value_exponent=0.5)
            return float(scorer.calculate_score())
        return 0.0

    def _positive_metric(self) -> Optional[float]:
        for key in ("bw_mbps", "throughput_ops_per_sec", "throughput", "iops"):
            value = self.collected_data.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _file_has_io_errors(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    return "TRACE_REPLAY_ERROR" in text or "Cannot read" in text or "Cannot write" in text or "Bad request type" in text
