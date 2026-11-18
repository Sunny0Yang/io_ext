from __future__ import annotations

import csv
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base_scene import Scene


class PerfStatMonitorScene(Scene):
    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}
        self.events = list(parameters.get("events") or ["cycles", "instructions", "cache-misses", "cache-references"])
        self.cpu_list = str(parameters.get("cpu_list") or "").strip()
        self.sample_interval_ms = int(parameters.get("sample_interval_ms", 1000))
        self.output_dir = os.path.abspath(os.path.expandvars(str(parameters.get("output_dir") or "./test_results/perf_stat")))
        self.output_file = os.path.join(self.output_dir, f"{scene_id}_perf.csv")
        self.summary_file = os.path.join(self.output_dir, f"{scene_id}_summary.json")
        self.duration = float(parameters.get("duration", 0))
        self.require_available = bool(parameters.get("require_available", False))
        self.perf_path = shutil.which("perf")
        self.process: Optional[subprocess.Popen[str]] = None
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None

    def setup(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        if self.require_available and not self.perf_path:
            raise FileNotFoundError("perf is not installed")

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        for path in (self.output_file, self.summary_file):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        self.collected_data = {}
        self.start_time = None
        self.end_time = None

    def _run(self) -> None:
        self.start_time = time.time()
        if not self.perf_path:
            self._should_stop.wait(timeout=self.duration if self.duration > 0 else 0.1)
            self.end_time = time.time()
            return
        command = [
            self.perf_path,
            "stat",
            "-x,",
            "-I",
            str(self.sample_interval_ms),
            "-e",
            ",".join(self.events),
            "-o",
            self.output_file,
            "-a",
        ]
        if self.cpu_list:
            command.extend(["-C", self.cpu_list])
        command.extend(["--", "sleep", str(max(1, int(self.duration or 86400)))])
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        # Read self.process into a local per iteration: end() (called from
        # the controller's stop path) nulls self.process, and dereferencing
        # it directly here races with that teardown.
        while True:
            proc = self.process
            if proc is None or proc.poll() is not None:
                break
            if self.should_stop():
                break
            time.sleep(0.1)
        self.end()
        self.end_time = time.time()

    def end(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process = None

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        self.end()

    def collect(self) -> Dict[str, Any]:
        counters = parse_perf_stat_csv(self.output_file)
        cycles = counters.get("cycles")
        instructions = counters.get("instructions")
        cache_misses = counters.get("cache-misses")
        cache_references = counters.get("cache-references")
        self.collected_data = {
            "available": bool(self.perf_path and counters),
            "perf_path": self.perf_path,
            "cpu_list": self.cpu_list,
            "events": self.events,
            "counters": counters,
            "cycles": cycles,
            "instructions": instructions,
            "cpi": cycles / instructions if cycles and instructions else None,
            "ipc": instructions / cycles if cycles and instructions else None,
            "cache_miss_rate": cache_misses / cache_references if cache_misses and cache_references else None,
            "start_time": self.start_time,
            "end_time": self.end_time,
        }
        return self.collected_data

    def stat(self) -> List[str]:
        self.collect()
        with open(self.summary_file, "w", encoding="utf-8") as stream:
            json.dump(self.collected_data, stream, indent=2, sort_keys=True)
        return [path for path in (self.output_file, self.summary_file) if os.path.isfile(path)]

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as stream:
            self.collected_data = json.load(stream)

    def score(self) -> float:
        return 0.0


def parse_perf_stat_csv(path: str | Path) -> Dict[str, float]:
    counters: Dict[str, float] = {}
    try:
        stream = Path(path).open("r", encoding="utf-8", errors="replace", newline="")
    except OSError:
        return counters
    with stream:
        for row in csv.reader(stream):
            if len(row) < 4:
                continue
            value_text = row[1].strip().replace(" ", "")
            event = row[3].strip()
            try:
                value = float(value_text)
            except ValueError:
                continue
            if event:
                counters[event] = counters.get(event, 0.0) + value
    return counters
