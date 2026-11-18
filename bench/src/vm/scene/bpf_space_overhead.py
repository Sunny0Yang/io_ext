"""BPF map and cgroup memory overhead monitor for frame-cost experiments."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from glob import glob
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base_scene import Scene

logger = logging.getLogger("bpf_space_overhead")

DEFAULT_OUTPUT_DIR = "/home/anon/ioext-bench/test_results/system/bpf_space_overhead"


class BpfSpaceOverheadScene(Scene):
    """Snapshot BPF map memory and cgroup memory.current before/during/after a workload."""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        self.duration = _float_param(parameters.get("duration", 300), 300.0)
        self.sample_interval = max(0.2, _float_param(parameters.get("sample_interval", 5.0), 5.0))
        self.max_samples = max(1, int(parameters.get("max_samples", 7200)))
        self.require_snapshot = _bool_param(parameters.get("require_snapshot", False))
        self.map_name_regex = str(parameters.get("map_name_regex", r"(scx|ioext|iosched)"))
        self.pin_path_regex = str(parameters.get("pin_path_regex", r"/sys/fs/bpf/(.*(scx|ioext|iosched).*)"))
        self.include_all_maps = _bool_param(parameters.get("include_all_maps", False))
        self.cgroup_paths = [str(path) for path in parameters.get("cgroup_paths", ["/sys/fs/cgroup"])]
        self.cgroup_path_globs = [str(pattern) for pattern in parameters.get("cgroup_path_globs", [])]
        self.bpftool_bin = str(parameters.get("bpftool_bin", "bpftool"))

        self.output_dir = _expand_path(parameters.get("output_dir", DEFAULT_OUTPUT_DIR))
        self.output_file = _expand_path(
            parameters.get("output_file", os.path.join(self.output_dir, f"{self.scene_id}_summary.json"))
        )

        self.privilege_config = parameters.get("privilege", {}) or {}
        self.privilege_method = str(self.privilege_config.get("method", "sudo_nopasswd"))
        self.askpass_script_path = str(self.privilege_config.get("askpass_script", "") or "")
        self.password_env_var = str(self.privilege_config.get("env_var", "SUDO_PASSWORD") or "SUDO_PASSWORD")
        self.temp_askpass_script: Optional[str] = None

        self.warmup_time = max(0.0, _float_param(parameters.get("warmup_time", 0.0), 0.0))
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.samples: List[Dict[str, Any]] = []
        self._after_sampled: bool = False

    def setup(self) -> None:
        _validate_env_var_name(self.password_env_var)
        if self.privilege_method == "sudo_askpass":
            self._create_askpass_script()

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        self.start_time = None
        self.end_time = None
        self.samples = []
        self.collected_data = {}
        self._after_sampled = False
        if os.path.exists(self.output_file):
            os.remove(self.output_file)

    def _run(self) -> None:
        self.start_time = time.time()
        self._append_sample("before")

        # Warmup phase: label samples as "warmup" until warmup_time elapses.
        # This avoids polluting "during" samples with pre-workload data when
        # used as a monitoring scene that starts before the workload.
        warmup_deadline = self.start_time + self.warmup_time if self.warmup_time > 0 else self.start_time

        while not self.should_stop():
            now = time.time()
            if self.duration > 0 and self.start_time is not None and now - self.start_time >= self.duration:
                break
            if self._should_stop.wait(timeout=self.sample_interval):
                break
            phase = "warmup" if time.time() < warmup_deadline else "during"
            self._append_sample(phase)

        if not self._after_sampled:
            self._append_sample("after")
            self._after_sampled = True
        self.end_time = time.time()

    def end(self) -> None:
        self._should_stop.set()
        if self.start_time is not None and self.end_time is None:
            if not self._after_sampled:
                self._append_sample("after")
                self._after_sampled = True
            self.end_time = time.time()

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        self.end()
        if self.temp_askpass_script and os.path.exists(self.temp_askpass_script):
            os.remove(self.temp_askpass_script)
        self.temp_askpass_script = None

    def collect(self) -> Dict[str, Any]:
        data = self._build_result()
        summary = data["summary"]
        if self.require_snapshot and summary.get("sample_count", 0) <= 0:
            raise RuntimeError("bpf_space_overhead completed without samples")
        self.collected_data = data
        return data

    def stat(self) -> List[str]:
        data = self.collected_data or self._build_result()
        if self.require_snapshot and data.get("summary", {}).get("sample_count", 0) <= 0:
            raise RuntimeError("bpf_space_overhead completed without samples")
        os.makedirs(os.path.dirname(self.output_file), exist_ok=True)
        with open(self.output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        return [self.output_file]

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.output_file = data_path
        self.samples = list(self.collected_data.get("samples", []))
        self.start_time = self.collected_data.get("metadata", {}).get("start_time")
        self.end_time = self.collected_data.get("metadata", {}).get("end_time")

    def score(self) -> float:
        return 0.0

    def _append_sample(self, phase: str) -> None:
        sample = self._snapshot(phase)
        self.samples.append(sample)
        if len(self.samples) > self.max_samples:
            self.samples = self.samples[-self.max_samples :]

    def _snapshot(self, phase: str) -> Dict[str, Any]:
        maps, bpftool_error = self._read_bpf_maps()
        filtered_maps = [bpf_map for bpf_map in maps if self._map_matches(bpf_map)]
        cgroup_memory = self._read_cgroup_memory()
        return {
            "timestamp": time.time(),
            "phase": phase,
            "bpftool_error": bpftool_error,
            "map_count_total": len(maps),
            "map_count_matched": len(filtered_maps),
            "matched_map_memlock_bytes": sum(_number(map_info.get("bytes_memlock")) for map_info in filtered_maps),
            "matched_map_estimated_bytes": sum(_estimate_map_bytes(map_info) for map_info in filtered_maps),
            "matched_maps": filtered_maps,
            "cgroup_memory_current": cgroup_memory,
        }

    def _map_matches(self, map_info: Dict[str, Any]) -> bool:
        if self.include_all_maps:
            return True
        name = str(map_info.get("name", ""))
        pinned_paths = map_info.get("pinned") or []
        if isinstance(pinned_paths, str):
            pinned_paths = [pinned_paths]
        if re.search(self.map_name_regex, name):
            return True
        return any(re.search(self.pin_path_regex, str(path)) for path in pinned_paths)

    def _read_bpf_maps(self) -> tuple[List[Dict[str, Any]], str]:
        cmd = [self.bpftool_bin, "-f", "-j", "map", "show"]
        try:
            run_env = os.environ.copy()
            if self.privilege_method == "sudo_askpass" and self.askpass_script_path:
                run_env["SUDO_ASKPASS"] = self.askpass_script_path
            proc = subprocess.run(
                self._privileged_cmd(cmd),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                env=run_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [], str(exc)
        if proc.returncode != 0:
            return [], proc.stderr.strip() or f"bpftool exited {proc.returncode}"
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            return [], f"bpftool JSON decode failed: {exc}"
        if not isinstance(payload, list):
            return [], "bpftool JSON root is not a list"
        return [item for item in payload if isinstance(item, dict)], ""

    def _read_cgroup_memory(self) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for raw_path in self._expanded_cgroup_paths():
            path = _expand_path(raw_path)
            memory_file = os.path.join(path, "memory.current")
            try:
                with open(memory_file, "r", encoding="utf-8") as f:
                    result[path] = {"memory_current_bytes": int(f.read().strip()), "available": True}
            except (OSError, ValueError) as exc:
                result[path] = {"memory_current_bytes": 0, "available": False, "error": str(exc)}
        return result

    def _expanded_cgroup_paths(self) -> List[str]:
        paths = [_expand_path(path) for path in self.cgroup_paths]
        for pattern in self.cgroup_path_globs:
            paths.extend(sorted(glob(_expand_path(pattern))))
        seen = set()
        deduped: List[str] = []
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            deduped.append(path)
        return deduped

    def _build_result(self) -> Dict[str, Any]:
        samples = list(self.samples)
        return {
            "metadata": {
                "scene_id": self.scene_id,
                "scene_name": self.scene_name,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "duration": (self.end_time - self.start_time) if self.end_time and self.start_time else 0,
                "bpftool_bin": self.bpftool_bin,
                "map_name_regex": self.map_name_regex,
                "pin_path_regex": self.pin_path_regex,
                "include_all_maps": self.include_all_maps,
                "cgroup_paths": self.cgroup_paths,
                "cgroup_path_globs": self.cgroup_path_globs,
                "warmup_time": self.warmup_time,
                "output_file": self.output_file,
            },
            "summary": _summarize_samples(samples),
            "samples": samples,
        }

    def _privileged_cmd(self, cmd: List[str]) -> List[str]:
        if self.privilege_method == "sudo_askpass":
            return ["sudo", "-E", "-A"] + cmd
        if self.privilege_method in {"sudo_cache", "sudo_nopasswd"}:
            return ["sudo", "-E"] + cmd
        return cmd

    def _create_askpass_script(self) -> None:
        if self.askpass_script_path:
            return
        fd, self.temp_askpass_script = tempfile.mkstemp(suffix=".sh")
        os.close(fd)
        with open(self.temp_askpass_script, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "${{{self.password_env_var}}}"\n')
        os.chmod(self.temp_askpass_script, 0o700)
        self.askpass_script_path = self.temp_askpass_script


def _summarize_samples(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    memlock = [float(sample.get("matched_map_memlock_bytes", 0)) for sample in samples]
    estimated = [float(sample.get("matched_map_estimated_bytes", 0)) for sample in samples]
    matched_counts = [float(sample.get("map_count_matched", 0)) for sample in samples]
    before = next((sample for sample in samples if sample.get("phase") == "before"), samples[0] if samples else {})
    after = next((sample for sample in reversed(samples) if sample.get("phase") == "after"), samples[-1] if samples else {})
    before_memlock = _number(before.get("matched_map_memlock_bytes"))
    after_memlock = _number(after.get("matched_map_memlock_bytes"))
    during_samples = [s for s in samples if s.get("phase") == "during"]
    warmup_samples = [s for s in samples if s.get("phase") == "warmup"]
    during_memlock = [float(s.get("matched_map_memlock_bytes", 0)) for s in during_samples]
    during_estimated = [float(s.get("matched_map_estimated_bytes", 0)) for s in during_samples]
    during_counts = [float(s.get("map_count_matched", 0)) for s in during_samples]
    return {
        "sample_count": len(samples),
        "warmup_sample_count": len(warmup_samples),
        "during_sample_count": len(during_samples),
        "bpftool_error_count": sum(1 for sample in samples if sample.get("bpftool_error")),
        "matched_map_count": _stats(matched_counts),
        "matched_map_memlock_bytes": _stats(memlock),
        "matched_map_estimated_bytes": _stats(estimated),
        "during_matched_map_count": _stats(during_counts),
        "during_matched_map_memlock_bytes": _stats(during_memlock),
        "during_matched_map_estimated_bytes": _stats(during_estimated),
        "before_memlock_bytes": before_memlock,
        "after_memlock_bytes": after_memlock,
        "delta_memlock_bytes": after_memlock - before_memlock,
        "before_cgroup_memory_current": before.get("cgroup_memory_current", {}),
        "after_cgroup_memory_current": after.get("cgroup_memory_current", {}),
    }


def _stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"count": 0, "avg": 0.0, "min": 0.0, "max": 0.0, "last": 0.0}
    return {"count": len(values), "avg": sum(values) / len(values), "min": min(values), "max": max(values), "last": values[-1]}


def _estimate_map_bytes(map_info: Dict[str, Any]) -> int:
    memlock = _number(map_info.get("bytes_memlock"))
    if memlock > 0:
        return int(memlock)
    key = _number(map_info.get("bytes_key"))
    value = _number(map_info.get("bytes_value"))
    entries = max(1, _number(map_info.get("max_entries")))
    map_type = str(map_info.get("type", "")).lower()
    cpu_count = max(1, os.cpu_count() or 1)
    if "percpu" in map_type:
        return int(entries * (key + value + 40) * cpu_count)
    if "hash" in map_type:
        return int(entries * (key + value + 40))
    return int(entries * max(value, key + value))


def _number(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BPF map/cgroup memory overhead monitor")
    parser.add_argument("--id", default="bpf_space_overhead")
    parser.add_argument("--name", default="BPF space overhead")
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(level=logging.INFO)
    scene = BpfSpaceOverheadScene(
        args.id,
        args.name,
        {"duration": args.duration, "output_dir": args.output_dir, "sample_interval": args.sample_interval},
    )
    try:
        scene.setup()
        scene.prepare()
        scene.start()
        scene.end()
        scene.collect()
        print(scene.stat())
    finally:
        scene.cleanup()
        scene.teardown()
