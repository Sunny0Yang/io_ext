"""Global io-ext loader scene.

This scene owns long-lived io-ext loader processes for a whole benchmark
sequence.  It is intentionally workload-free: tenant scenes still create their
own cgroups and run fio/YCSB/etc., while this scene starts root policies or
global arbiters exactly once through the controller's ``global_scene`` hook.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from vm.scene.base_scene import Scene

from .common import (
    DEFAULT_ROOT_CGROUP_PATH,
    assert_loader_chain_alive,
    expand_path,
    is_process_scheduler_alias,
    loader_log_paths,
    set_scheduler,
    setup_cgroup_v2,
    start_loader_chain,
    stop_loader_chain,
    validate_block_device_name,
    write_json_stat,
)


logger = logging.getLogger("io_ext_loader")


class IoExtLoaderScene(Scene):
    """Start io-ext arbiters/root policies once for concurrent tenant scenes."""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        io_ext = parameters.get("io_ext") or {}
        self.io_ext_cfg: Dict[str, Any] = dict(io_ext)
        self.scheduler = io_ext.get("scheduler", parameters.get("scheduler"))
        target_device = io_ext.get("target_device", parameters.get("target_device"))
        self.target_device = validate_block_device_name(str(target_device)) if target_device else None
        if self.scheduler and not is_process_scheduler_alias(str(self.scheduler)) and not self.target_device:
            raise ValueError("target_device is required when IoExtLoaderScene sets a block IO scheduler")

        root_cgroup_cfg = io_ext.get("root_cgroup") or {}
        root_cgroup_path = None
        if isinstance(root_cgroup_cfg, dict):
            root_cgroup_path = root_cgroup_cfg.get("path")
        self.root_cgroup_path = expand_path(
            root_cgroup_path or io_ext.get("root_cgroup_path") or DEFAULT_ROOT_CGROUP_PATH
        ) or DEFAULT_ROOT_CGROUP_PATH
        root_controllers = root_cgroup_cfg.get("controllers") if isinstance(root_cgroup_cfg, dict) else None
        self.root_cgroup_controllers = list(root_controllers or ["io"])

        self.output_dir = expand_path(parameters.get("output_dir")) or "./test_results/io_ext/loader"
        self.duration = int(parameters.get("duration", 0))
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self._loader_procs: List[subprocess.Popen[str]] = []

    def setup(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        if self.root_cgroup_path:
            setup_cgroup_v2(self.root_cgroup_path, self.root_cgroup_controllers)

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        if self.target_device and self.scheduler:
            set_scheduler(self.target_device, self.scheduler)
        self.start_time = time.time()
        self.end_time = None
        self.collected_data = {
            "scene_id": self.scene_id,
            "target_device": self.target_device,
            "scheduler": self.scheduler,
            "root_cgroup_path": self.root_cgroup_path,
            "io_ext": self.io_ext_cfg,
            "loader_logs": [],
        }
        self._start_loaders()

    def _start_loaders(self) -> None:
        if self._loader_procs:
            return
        self._loader_procs = start_loader_chain(self.parameters, self.output_dir, self.root_cgroup_path)
        self.collected_data["loader_logs"] = loader_log_paths(self._loader_procs)
        logger.info("started %s io-ext global loader processes", len(self._loader_procs))

    def _stop_loaders(self) -> None:
        if self._loader_procs:
            self.collected_data["loader_logs"] = loader_log_paths(self._loader_procs)
        stop_loader_chain(self._loader_procs)
        self._loader_procs = []

    def _run(self) -> None:
        if not self._loader_procs:
            self._start_loaders()
        deadline = time.time() + self.duration if self.duration > 0 else None
        while not self.should_stop():
            if deadline is not None and time.time() >= deadline:
                break
            if self._loader_procs:
                assert_loader_chain_alive(self._loader_procs)
            time.sleep(0.2)

    def end(self) -> None:
        self._stop_loaders()
        if self.end_time is None:
            self.end_time = time.time()

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        self.end()

    def _read_log_tails(self, max_bytes: int = 16384) -> List[Dict[str, Any]]:
        """Tail of each loader stdout/stderr file.

        Loaders emit their final BPF-map stats to stderr on graceful exit
        (e.g. loader_q_ratelimit's q_ratelimit_stats lines).  The raw
        scene output_dir is not archived into the sequence tarball, so
        embed the log tails in the scene stat JSON to keep them with the
        results.
        """
        tails: List[Dict[str, Any]] = []
        for entry in self.collected_data.get("loader_logs", []):
            if not isinstance(entry, dict):
                continue
            for stream in ("stdout", "stderr"):
                path = entry.get(stream)
                if not path or not os.path.isfile(path):
                    continue
                try:
                    with open(path, "r", errors="replace") as f:
                        f.seek(0, os.SEEK_END)
                        size = f.tell()
                        f.seek(max(0, size - max_bytes))
                        content = f.read()
                except OSError:
                    continue
                if content:
                    tails.append({
                        "name": entry.get("name"),
                        "stream": stream,
                        "path": path,
                        "tail": content,
                    })
        return tails

    def collect(self) -> Dict[str, Any]:
        if self.start_time is None:
            raise RuntimeError("io-ext loader scene has not been prepared")
        if self.end_time is None:
            self.end_time = time.time()
        self.collected_data["start_time"] = self.start_time
        self.collected_data["end_time"] = self.end_time
        self.collected_data["duration_wall"] = self.end_time - self.start_time
        self.collected_data["loader_log_tails"] = self._read_log_tails()
        return self.collected_data

    def stat(self) -> str:
        if self.start_time is not None:
            self.collect()
        return write_json_stat(self.output_dir, "io_ext_loader", self.scene_id, self.collected_data)

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.start_time = self.collected_data.get("start_time")
        self.end_time = self.collected_data.get("end_time")
        duration = self.collected_data.get("duration_wall")
        self.duration = int(float(duration)) if duration is not None else 0

    def score(self) -> float:
        return 0.0
