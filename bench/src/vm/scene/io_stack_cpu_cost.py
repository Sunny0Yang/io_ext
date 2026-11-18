"""
IO 栈块层 io_start→issue 路径 bpftrace 场景（io_stack_cpu_cost.bt）。
"""

import argparse
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .bpftrace_block_base import BpftraceBlockLayerScene, setup_logging

logger = logging.getLogger("bpftrace_block")


class IoStackCpuCostScene(BpftraceBlockLayerScene):
    """block_io_start → block_rq_issue 软件路径耗时采集。"""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        parameters = dict(parameters or {})
        output_dir = _expand_path(
            parameters.get("output_dir", "/home/anon/ioext-bench/test_results/block_bpf/io_stack_cpu_cost")
        )
        parameters.setdefault("output_file", os.path.join(output_dir, f"{scene_id}.bpftrace.log"))
        super().__init__(scene_id, scene_name, "io_stack_cpu_cost.bt", parameters)
        self.output_dir = os.path.dirname(self.output_file) or output_dir
        self.summary_file = _expand_path(
            parameters.get("summary_file", os.path.join(self.output_dir, f"{scene_id}_summary.json"))
        )
        self.require_events = _bool_param(parameters.get("require_events", True))

    def setup(self) -> None:
        _validate_env_var_name(str(self.password_env_var))
        if self.privilege_method == "sudo_askpass":
            self._create_askpass_script()

    def prepare(self) -> None:
        if not os.path.isfile(self.script_path):
            raise FileNotFoundError(f"io_stack_cpu_cost bpftrace script not found: {self.script_path}")
        resolved_bpftrace = _which_bpftrace()
        if not resolved_bpftrace:
            raise FileNotFoundError("bpftrace executable not found")
        os.makedirs(self.output_dir, exist_ok=True)
        self.start_time = None
        self.end_time = None
        self.collected_data = {}
        for path in [self.output_file, self.summary_file]:
            if os.path.exists(path):
                os.remove(path)

    def collect(self) -> Dict[str, Any]:
        data = self._build_collection_data(enforce_requirements=True)
        self.collected_data = data
        return data

    def stat(self) -> List[str]:
        data = self.collected_data or self._build_collection_data(enforce_requirements=False)
        os.makedirs(os.path.dirname(self.summary_file), exist_ok=True)
        with open(self.summary_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        files = [self.summary_file]
        if os.path.exists(self.output_file):
            files.append(self.output_file)
        return files

    def deserialize(self, data_path: str) -> None:
        if data_path.endswith(".json"):
            with open(data_path, "r", encoding="utf-8") as f:
                self.collected_data = json.load(f)
            self.summary_file = data_path
        else:
            super().deserialize(data_path)

    def score(self) -> float:
        return 0.0

    def _build_collection_data(self, enforce_requirements: bool) -> Dict[str, Any]:
        raw_text = _read_text_if_exists(self.output_file)
        summary = _parse_io_stack_output(raw_text)
        data = {
            "metadata": {
                "scene_id": self.scene_id,
                "scene_name": self.scene_name,
                "script": self.script_basename,
                "script_path": self.script_path,
                "output_file": self.output_file,
                "summary_file": self.summary_file,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "duration": (self.end_time - self.start_time) if self.end_time and self.start_time else 0,
                "require_events": self.require_events,
                "returncode": self.agent_process.returncode if self.agent_process else None,
            },
            "summary": summary,
            "raw_tail": _last_lines(raw_text, 80),
        }
        if enforce_requirements and self.require_events:
            _validate_required_summary(summary)
        return data

    def _create_askpass_script(self) -> None:
        if self.askpass_script_path:
            return
        fd, self.temp_askpass_script = tempfile.mkstemp(suffix=".sh")
        os.close(fd)
        with open(self.temp_askpass_script, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "${{{self.password_env_var}}}"\n')
        os.chmod(self.temp_askpass_script, 0o700)
        self.askpass_script_path = self.temp_askpass_script

    def _run_with_privilege(self, cmd: List[str], **popen_kwargs: Any) -> subprocess.Popen:
        if self.privilege_method == "sudo_askpass":
            env = {
                "PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
                "SUDO_ASKPASS": self.askpass_script_path,
            }
            if self.password_env_var in os.environ:
                env[self.password_env_var] = os.environ[self.password_env_var]
            return subprocess.Popen(["sudo", "-A"] + cmd, env=env, **popen_kwargs)
        if self.privilege_method in {"sudo_cache", "sudo_nopasswd"}:
            return subprocess.Popen(["sudo"] + cmd, **popen_kwargs)
        return subprocess.Popen(cmd, **popen_kwargs)


def _which_bpftrace() -> str:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, "bpftrace")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    for candidate in ["/usr/bin/bpftrace", "/usr/local/bin/bpftrace"]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _expand_path(path: Any) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(str(path))))


def _bool_param(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _validate_env_var_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"invalid environment variable name: {name}")


def _read_text_if_exists(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _last_lines(text: str, limit: int) -> List[str]:
    return text.splitlines()[-limit:]


def _parse_io_stack_output(text: str) -> Dict[str, Any]:
    return {
        "start_marker_seen": "IO_STACK_CPU_COST_START" in text,
        "summary_begin_seen": "IO_STACK_CPU_COST_SUMMARY_BEGIN" in text,
        "summary_end_seen": "IO_STACK_CPU_COST_SUMMARY_END" in text,
        "start_event_count": _parse_scalar_map(text, "isc_start_event_count"),
        "issue_count": _parse_scalar_map(text, "isc_issue_count"),
        "exact_match_count": _parse_scalar_map(text, "isc_exact_match_count"),
        "approx_match_count": _parse_scalar_map(text, "isc_approx_match_count"),
        "path_ns": _parse_stats_map(text, "isc_path_ns"),
        "read_path_ns": _parse_stats_map(text, "isc_read_path_ns"),
        "write_path_ns": _parse_stats_map(text, "isc_write_path_ns"),
        "other_path_ns": _parse_stats_map(text, "isc_other_path_ns"),
        "read_count": _parse_scalar_map(text, "isc_read_count"),
        "write_count": _parse_scalar_map(text, "isc_write_count"),
        "other_count": _parse_scalar_map(text, "isc_other_count"),
        "has_path_histogram": "@isc_path_ns_hist" in text,
        "has_slow_stacks": "@isc_slow_path_stacks" in text,
        "has_read_stats": "@isc_read_path_ns" in text,
        "has_write_stats": "@isc_write_path_ns" in text,
        "line_count": len(text.splitlines()),
    }


def _parse_scalar_map(text: str, name: str) -> int:
    matches = re.findall(rf"@{re.escape(name)}:\s*([0-9]+)", text)
    return int(matches[-1]) if matches else 0


def _parse_stats_map(text: str, name: str) -> Dict[str, float]:
    """Parse bpftrace stats() output: { .count = N, .average = A, .total = T }"""
    line_match = re.findall(rf"@{re.escape(name)}:\s*{{([^}}]+)}}", text)
    if not line_match:
        return {"count": 0, "avg_ns": 0.0, "total_ns": 0.0}
    inner = line_match[-1]
    count = _extract_labeled_number(inner, ["count"])
    avg = _extract_labeled_number(inner, ["average"])
    total = _extract_labeled_number(inner, ["total"])
    return {"count": count, "avg_ns": avg, "total_ns": total}


def _extract_labeled_number(text: str, labels: List[str]) -> float:
    for label in labels:
        match = re.search(rf"\.?{label}\s*(?:=)?\s*([0-9]+(?:\.[0-9]+)?)", text)
        if match:
            return float(match.group(1))
    return 0.0


def _validate_required_summary(summary: Dict[str, Any]) -> None:
    missing = []
    for key in ["start_marker_seen", "summary_begin_seen", "summary_end_seen"]:
        if not summary.get(key):
            missing.append(key)
    for key in ["start_event_count", "issue_count"]:
        if summary.get(key, 0) <= 0:
            missing.append(key)
    queue_stats = summary.get("path_ns", {})
    if queue_stats.get("count", 0) <= 0:
        missing.append("path_ns.count")
    if not summary.get("has_path_histogram"):
        missing.append("has_path_histogram")
    if missing:
        raise RuntimeError(f"io_stack_cpu_cost missing required BPF evidence: {', '.join(missing)}")


def parse_args():
    parser = argparse.ArgumentParser(description="IO 栈块层 io_start→issue bpftrace")
    parser.add_argument("--id", default="io_stack_cpu_cost", help="场景 ID")
    parser.add_argument("--name", default="IO 栈块层软件路径耗时", help="场景名称")
    parser.add_argument("--duration", type=int, default=300, help="采集时长(秒)，0 表示直到进程被中断")
    parser.add_argument(
        "--privilege-method",
        choices=["sudo_askpass", "sudo_cache", "sudo_nopasswd", "none"],
        default="sudo_askpass",
        help="权限方式，none 为直接运行 bpftrace",
    )
    parser.add_argument("--askpass-script", default="", help="SUDO_ASKPASS 脚本路径")
    parser.add_argument("--password-env", default="SUDO_PASSWORD", help="密码环境变量名")
    parser.add_argument("--output-file", default="", help="bpftrace 标准输出日志路径")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    setup_logging(args.log_level)
    params = {
        "duration": args.duration,
        "privilege": {
            "method": args.privilege_method,
            "askpass_script": args.askpass_script,
            "env_var": args.password_env,
        },
    }
    if args.output_file:
        params["output_file"] = args.output_file

    scene = IoStackCpuCostScene(args.id, args.name, params)
    try:
        scene.setup()
        scene.prepare()
        scene.start()
        scene.end()
        scene.collect()
        scene.stat()
        scene.cleanup()
        scene.teardown()
    except KeyboardInterrupt:
        logger.info("用户中断")
        scene.end()
        scene.cleanup()
        scene.teardown()
    except Exception:
        logger.exception("运行失败")
        scene.end()
        scene.cleanup()
        scene.teardown()
        raise
