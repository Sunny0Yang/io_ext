"""低开销块层 BPF I/O 指标场景。"""

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base_scene import Scene

logger = logging.getLogger("io_metric")

_ASSETS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets", "block_bpf"))
DEFAULT_OUTPUT_DIR = "/home/anon/ioext-bench/test_results/block_bpf/io_metric"


class BpftraceIoMetricScene(Scene):
    """运行轻量 bpftrace 脚本并把 block I/O 指标解析为 JSON。"""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        self.duration = _float_param(parameters.get("duration", 300), 300.0)
        self.output_dir = _expand_path(parameters.get("output_dir", DEFAULT_OUTPUT_DIR))
        self.script_path = _expand_path(parameters.get("script_path", os.path.join(_ASSETS_DIR, "io_metric.bt")))
        self.bpftrace_bin = str(parameters.get("bpftrace_bin", "bpftrace") or "bpftrace")
        self.bpftrace_path = ""
        self.raw_output_file = _expand_path(
            parameters.get("raw_output_file", os.path.join(self.output_dir, f"{self.scene_id}.bpftrace.log"))
        )
        self.summary_file = _expand_path(
            parameters.get("summary_file", os.path.join(self.output_dir, f"{self.scene_id}_summary.json"))
        )
        self.startup_grace_sec = max(0.0, _float_param(parameters.get("startup_grace_sec", 0.8), 0.8))
        self.poll_interval_sec = max(0.05, _float_param(parameters.get("poll_interval_sec", 0.2), 0.2))
        self.require_events = _bool_param(parameters.get("require_events", True))

        self.privilege_config = parameters.get("privilege", {}) or {}
        self.privilege_method = str(self.privilege_config.get("method", "sudo_nopasswd"))
        self.askpass_script_path = str(self.privilege_config.get("askpass_script", "") or "")
        self.password_env_var = str(self.privilege_config.get("env_var", "SUDO_PASSWORD") or "SUDO_PASSWORD")
        _validate_env_var_name(self.password_env_var)

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.returncode: Optional[int] = None
        self.process: Optional[subprocess.Popen] = None
        self.temp_askpass_script: Optional[str] = None
        self._output_handle = None

    def setup(self) -> None:
        if self.privilege_method == "sudo_askpass":
            self._create_askpass_script()

    def prepare(self) -> None:
        if not os.path.isfile(self.script_path):
            raise FileNotFoundError(f"io_metric bpftrace script not found: {self.script_path}")
        resolved_bpftrace = shutil.which(self.bpftrace_bin)
        if not resolved_bpftrace:
            raise FileNotFoundError(f"bpftrace executable not found: {self.bpftrace_bin}")
        self.bpftrace_path = resolved_bpftrace
        os.makedirs(self.output_dir, exist_ok=True)
        self.start_time = None
        self.end_time = None
        self.returncode = None
        self.collected_data = {}
        for path in [self.raw_output_file, self.summary_file]:
            if os.path.exists(path):
                os.remove(path)

    def _run(self) -> None:
        self.start_time = time.time()
        self._start_bpftrace()
        deadline = None if self.duration <= 0 else self.start_time + self.duration
        while not self.should_stop():
            if self.process and self.process.poll() is not None:
                self.returncode = self.process.returncode
                break
            if deadline is not None and time.time() >= deadline:
                break
            time.sleep(self.poll_interval_sec)
        self._stop_bpftrace()
        self.end_time = time.time()

        if self.returncode not in (0, None) and not self.should_stop():
            output_tail = _tail_text(self.raw_output_file)
            raise RuntimeError(f"io_metric bpftrace exited with {self.returncode}: {output_tail}")

    def end(self) -> None:
        self._should_stop.set()
        self._stop_bpftrace()
        if self.end_time is None:
            self.end_time = time.time()

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        self.end()
        if self.temp_askpass_script and os.path.exists(self.temp_askpass_script):
            os.remove(self.temp_askpass_script)
        self.temp_askpass_script = None

    def collect(self) -> Dict[str, Any]:
        data = self._build_collection_data(enforce_requirements=True)
        self.collected_data = data
        return data

    def stat(self) -> List[str]:
        data = self.collected_data
        if not data:
            data = self._build_collection_data(enforce_requirements=False)
        os.makedirs(os.path.dirname(self.summary_file), exist_ok=True)
        with open(self.summary_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        files = [self.summary_file]
        if os.path.exists(self.raw_output_file):
            files.append(self.raw_output_file)
        return files

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.summary_file = data_path

    def score(self) -> float:
        return 0.0

    def _start_bpftrace(self) -> None:
        os.makedirs(os.path.dirname(self.raw_output_file), exist_ok=True)
        self._output_handle = open(self.raw_output_file, "w", encoding="utf-8", errors="replace")
        cmd = [self.bpftrace_path or self.bpftrace_bin, self.script_path]
        try:
            self.process = self._run_with_privilege(
                cmd,
                stdout=self._output_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        except Exception:
            self._close_output_handle()
            raise

        if self.startup_grace_sec > 0:
            time.sleep(self.startup_grace_sec)
            if self.process.poll() is not None:
                self.returncode = self.process.returncode
                output_tail = _tail_text(self.raw_output_file)
                self._close_output_handle()
                raise RuntimeError(f"io_metric bpftrace failed during startup: {output_tail}")

    def _stop_bpftrace(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                self.process.send_signal(signal.SIGINT)
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            finally:
                self.returncode = self.process.returncode
        elif self.process:
            self.returncode = self.process.returncode
        self._close_output_handle()

    def _close_output_handle(self) -> None:
        if self._output_handle:
            try:
                self._output_handle.flush()
            except OSError:
                pass
            self._output_handle.close()
            self._output_handle = None

    def _create_askpass_script(self) -> None:
        if self.askpass_script_path:
            return
        fd, self.temp_askpass_script = tempfile.mkstemp(suffix=".sh")
        os.close(fd)
        with open(self.temp_askpass_script, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "${self.password_env_var}"\n')
        os.chmod(self.temp_askpass_script, 0o700)
        self.askpass_script_path = self.temp_askpass_script

    def _run_with_privilege(self, cmd: List[str], **kwargs: Any) -> subprocess.Popen:
        if self.privilege_method == "sudo_askpass":
            env = {}
            env["SUDO_ASKPASS"] = self.askpass_script_path
            if self.password_env_var in os.environ:
                env[self.password_env_var] = os.environ[self.password_env_var]
            return subprocess.Popen(["sudo", "-A"] + cmd, env=env, **kwargs)
        if self.privilege_method in {"sudo_cache", "sudo_nopasswd"}:
            return subprocess.Popen(["sudo"] + cmd, **kwargs)
        return subprocess.Popen(cmd, **kwargs)

    def _build_collection_data(self, enforce_requirements: bool) -> Dict[str, Any]:
        raw_text = _read_text_if_exists(self.raw_output_file)
        summary = _parse_io_metric_output(raw_text)
        data = {
            "metadata": {
                "scene_id": self.scene_id,
                "scene_name": self.scene_name,
                "script_path": self.script_path,
                "bpftrace_bin": self.bpftrace_bin,
                "bpftrace_path": self.bpftrace_path,
                "raw_output_file": self.raw_output_file,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "duration": (self.end_time - self.start_time) if self.start_time and self.end_time else 0.0,
                "returncode": self.returncode,
                "require_events": self.require_events,
            },
            "summary": summary,
            "raw_tail": _last_lines(raw_text, 80),
        }
        if enforce_requirements and self.require_events:
            _validate_required_summary(summary)
        return data


class IoMetricKeepaliveScene(Scene):
    """为 BPF io_metric smoke 生成少量块 I/O 的短场景。"""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}
        self.duration = _float_param(parameters.get("duration", 5), 5.0)
        self.output_dir = _expand_path(parameters.get("output_dir", os.path.join(DEFAULT_OUTPUT_DIR, "keepalive")))
        self.work_file = _expand_path(parameters.get("work_file", os.path.join(self.output_dir, "io_metric_keepalive.dat")))
        self.summary_file = _expand_path(
            parameters.get("summary_file", os.path.join(self.output_dir, f"{self.scene_id}_summary.json"))
        )
        self.chunk_size = max(4096, int(parameters.get("chunk_size", 4096)))
        self.bytes_per_round = max(self.chunk_size, int(parameters.get("bytes_per_round", 262144)))
        self.round_sleep_sec = max(0.0, _float_param(parameters.get("round_sleep_sec", 0.2), 0.2))
        self.initial_delay_sec = max(0.0, _float_param(parameters.get("initial_delay_sec", 1.5), 1.5))
        self.wait_for_file = _expand_path(parameters.get("wait_for_file", "")) if parameters.get("wait_for_file") else ""
        self.wait_for_text = str(parameters.get("wait_for_text", "") or "")
        self.wait_timeout_sec = max(0.0, _float_param(parameters.get("wait_timeout_sec", 10.0), 10.0))
        self.wait_poll_sec = max(0.05, _float_param(parameters.get("wait_poll_sec", 0.1), 0.1))
        self.cleanup_work_file = _bool_param(parameters.get("cleanup_work_file", True))
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.bytes_written = 0
        self.rounds = 0
        self.readiness_observed = False

    def setup(self) -> None:
        pass

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        work_parent = os.path.dirname(self.work_file)
        if work_parent:
            os.makedirs(work_parent, exist_ok=True)
        self.start_time = None
        self.end_time = None
        self.bytes_written = 0
        self.rounds = 0
        self.readiness_observed = False
        self.collected_data = {}
        for path in [self.summary_file, self.work_file]:
            if os.path.exists(path):
                os.remove(path)

    def _run(self) -> None:
        self.start_time = time.time()
        deadline = self.start_time + self.duration if self.duration > 0 else None
        if self.wait_for_file and self.wait_for_text:
            self._wait_for_readiness_marker(deadline)
        if self.initial_delay_sec > 0 and self._should_stop.wait(timeout=self.initial_delay_sec):
            self.end_time = time.time()
            return
        payload = b"0" * self.chunk_size
        while not self.should_stop():
            if deadline is not None and time.time() >= deadline:
                break
            written_this_round = 0
            with open(self.work_file, "wb") as f:
                while written_this_round < self.bytes_per_round and not self.should_stop():
                    to_write = min(self.chunk_size, self.bytes_per_round - written_this_round)
                    f.write(payload[:to_write])
                    written_this_round += to_write
                f.flush()
                os.fsync(f.fileno())
            self.bytes_written += written_this_round
            self.rounds += 1
            if self._should_stop.wait(timeout=self.round_sleep_sec):
                break
        self.end_time = time.time()

    def end(self) -> None:
        self._should_stop.set()
        if self.end_time is None:
            self.end_time = time.time()

    def cleanup(self) -> None:
        if self.cleanup_work_file and os.path.exists(self.work_file):
            os.remove(self.work_file)

    def teardown(self) -> None:
        self.cleanup()

    def collect(self) -> Dict[str, Any]:
        data = {
            "metadata": {
                "scene_id": self.scene_id,
                "scene_name": self.scene_name,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "duration": (self.end_time - self.start_time) if self.start_time and self.end_time else 0.0,
                "work_file": self.work_file,
            },
            "summary": {
                "rounds": self.rounds,
                "bytes_written": self.bytes_written,
                "bytes_per_round": self.bytes_per_round,
                "chunk_size": self.chunk_size,
                "initial_delay_sec": self.initial_delay_sec,
                "wait_for_file": self.wait_for_file,
                "wait_for_text": self.wait_for_text,
                "readiness_observed": self.readiness_observed,
            },
        }
        if self.bytes_written <= 0:
            raise RuntimeError("io_metric keepalive did not write data")
        self.collected_data = data
        return data

    def stat(self) -> str:
        data = self.collected_data or self.collect()
        os.makedirs(os.path.dirname(self.summary_file), exist_ok=True)
        with open(self.summary_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return self.summary_file

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.summary_file = data_path

    def score(self) -> float:
        return 0.0

    def _wait_for_readiness_marker(self, deadline: Optional[float]) -> None:
        wait_deadline = time.time() + self.wait_timeout_sec
        if deadline is not None:
            wait_deadline = min(wait_deadline, deadline)
        while not self.should_stop():
            if self.wait_for_text in _read_text_if_exists(self.wait_for_file):
                self.readiness_observed = True
                return
            if time.time() >= wait_deadline:
                raise RuntimeError(
                    f"readiness marker {self.wait_for_text!r} not observed in {self.wait_for_file}"
                )
            if self._should_stop.wait(timeout=self.wait_poll_sec):
                break
        if not self.readiness_observed:
            raise RuntimeError(
                f"stopped before readiness marker {self.wait_for_text!r} appeared in {self.wait_for_file}"
            )


def _expand_path(path: Any) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(str(path))))


def _float_param(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool_param(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _validate_env_var_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"invalid environment variable name: {name}")


def _validate_required_summary(summary: Dict[str, Any]) -> None:
    missing = []
    for key in ["start_marker_seen", "summary_begin_seen", "summary_end_seen"]:
        if not summary.get(key):
            missing.append(key)
    for key in ["issue_count", "complete_count", "issue_bytes", "complete_bytes"]:
        if summary.get(key, 0) <= 0:
            missing.append(key)
    service_stats = summary.get("service_ns", {})
    if service_stats.get("count", 0) <= 0:
        missing.append("service_ns.count")
    if not summary.get("has_service_histogram"):
        missing.append("has_service_histogram")
    if missing:
        raise RuntimeError(f"io_metric missing required BPF evidence: {', '.join(missing)}")


def _read_text_if_exists(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tail_text(path: str, limit: int = 4000) -> str:
    text = _read_text_if_exists(path)
    return text[-limit:]


def _last_lines(text: str, limit: int) -> List[str]:
    return text.splitlines()[-limit:]


def _parse_io_metric_output(text: str) -> Dict[str, Any]:
    return {
        "start_marker_seen": "IO_METRIC_START" in text,
        "summary_begin_seen": "IO_METRIC_SUMMARY_BEGIN" in text,
        "summary_end_seen": "IO_METRIC_SUMMARY_END" in text,
        "issue_count": _parse_scalar_map(text, "iom_issue_count"),
        "complete_count": _parse_scalar_map(text, "iom_complete_count"),
        "issue_bytes": _parse_scalar_map(text, "iom_issue_bytes"),
        "complete_bytes": _parse_scalar_map(text, "iom_complete_bytes"),
        "service_ns": _parse_stats_map(text, "iom_service_ns"),
        "has_service_histogram": "@iom_service_us" in text,
        "line_count": len(text.splitlines()),
    }


def _parse_scalar_map(text: str, name: str) -> int:
    matches = re.findall(rf"@{re.escape(name)}:\s*([0-9]+)", text)
    return int(matches[-1]) if matches else 0


def _parse_stats_map(text: str, name: str) -> Dict[str, float]:
    line_match = re.findall(rf"@{re.escape(name)}:\s*([^\n]+)", text)
    if not line_match:
        return {"count": 0, "avg_ns": 0.0, "total_ns": 0.0, "min_ns": 0.0, "max_ns": 0.0}
    line = line_match[-1]
    count = _extract_labeled_number(line, ["count"])
    avg = _extract_labeled_number(line, ["avg", "average"])
    total = _extract_labeled_number(line, ["total"])
    min_value = _extract_labeled_number(line, ["min"])
    max_value = _extract_labeled_number(line, ["max"])
    return {"count": count, "avg_ns": avg, "total_ns": total, "min_ns": min_value, "max_ns": max_value}


def _extract_labeled_number(text: str, labels: List[str]) -> float:
    for label in labels:
        match = re.search(rf"\.?{label}\s*(?:=)?\s*([0-9]+(?:\.[0-9]+)?)", text)
        if match:
            return float(match.group(1))
    return 0.0


def setup_logging(log_level: str = "INFO") -> None:
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"无效的日志级别: {log_level}")
    logging.basicConfig(level=numeric_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="低开销块层 BPF I/O 指标")
    parser.add_argument("--id", default="io_metric")
    parser.add_argument("--name", default="低开销 BPF I/O 指标")
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--privilege-method", choices=["sudo_askpass", "sudo_cache", "sudo_nopasswd", "none"], default="sudo_nopasswd")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], default="INFO")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    setup_logging(args.log_level)
    scene = BpftraceIoMetricScene(
        args.id,
        args.name,
        {"duration": args.duration, "output_dir": args.output_dir, "privilege": {"method": args.privilege_method}},
    )
    try:
        scene.setup()
        scene.prepare()
        scene.start()
        scene.collect()
        logger.info("结果文件: %s", scene.stat())
    finally:
        scene.cleanup()
        scene.teardown()
