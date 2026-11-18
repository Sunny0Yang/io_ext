"""io-ext policy transition scene for hot detach and direct replacement tests."""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from typing import Any, Dict, List, Mapping, Optional

from vm.scene.base_scene import Scene


from .common import (
    DEFAULT_BPFFS_PIN_PATHS,
    DEFAULT_ROOT_CGROUP_PATH,
    assert_loader_chain_alive,
    cleanup_bpffs_pins,
    cleanup_stale_loaders,
    expand_path,
    loader_log_paths,
    set_scheduler,
    setup_cgroup_v2,
    start_loader_chain,
    stop_loader_chain,
    validate_block_device_name,
    write_json_stat,
)


logger = logging.getLogger("io_ext_policy_transition")


class IoExtPolicyTransitionScene(Scene):
    """Run explicit scheduler/policy transition phases while monitors are active.

    The controller can express only step-level scene starts/stops.  This scene
    provides a finer timeline inside one step so bpftrace monitors observe the
    exact detach or replacement trigger and the post-transition settling window.
    """

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        target_device = parameters.get("target_device")
        if not target_device:
            raise ValueError("parameters.target_device is required")
        self.target_device = validate_block_device_name(str(target_device))
        self.native_scheduler = str(parameters.get("native_scheduler", "none"))
        self.scx_scheduler = str(parameters.get("scx_scheduler", "scx_io"))
        self.final_scheduler = str(parameters.get("final_scheduler", self.native_scheduler))

        root_cgroup = parameters.get("root_cgroup") or {}
        if not isinstance(root_cgroup, Mapping):
            raise ValueError("parameters.root_cgroup must be a mapping when provided")
        self.root_cgroup_path = expand_path(str(root_cgroup.get("path") or DEFAULT_ROOT_CGROUP_PATH))
        self.root_cgroup_controllers = list(root_cgroup.get("controllers") or ["io"])

        self.output_dir = expand_path(parameters.get("output_dir")) or "./test_results/io_ext/policy_transition"
        self.poll_interval_sec = float(parameters.get("poll_interval_sec", 0.1))
        self.cleanup_stale_on_prepare = bool(parameters.get("cleanup_stale_on_prepare", True))
        self.cleanup_bpffs_on_prepare = bool(parameters.get("cleanup_bpffs_on_prepare", True))
        self.cleanup_bpffs_on_teardown = bool(parameters.get("cleanup_bpffs_on_teardown", False))
        self.stale_loader_patterns = list(parameters.get("stale_loader_patterns") or ["loader_arb_", "loader_q_"])
        self.bpffs_pin_paths = list(parameters.get("bpffs_pin_paths") or DEFAULT_BPFFS_PIN_PATHS)

        policy_profiles = parameters.get("policy_profiles") or {}
        if not isinstance(policy_profiles, Mapping) or not policy_profiles:
            raise ValueError("parameters.policy_profiles must be a non-empty mapping")
        self.policy_profiles: Dict[str, Dict[str, Any]] = {}
        for key, value in policy_profiles.items():
            if isinstance(value, Mapping):
                self.policy_profiles[str(key)] = dict(copy.deepcopy(value))
        if len(self.policy_profiles) != len(policy_profiles):
            raise ValueError("each policy profile must be a mapping")

        phases = parameters.get("phases") or []
        if not isinstance(phases, list) or not phases:
            raise ValueError("parameters.phases must be a non-empty list")
        self.phases: List[Dict[str, Any]] = []
        for index, phase in enumerate(phases):
            if not isinstance(phase, Mapping):
                raise ValueError(f"parameters.phases[{index}] must be a mapping")
            normalized = dict(phase)
            normalized.setdefault("name", f"phase_{index}")
            normalized.setdefault("duration_sec", normalized.get("duration", 0))
            normalized.setdefault("action", "sleep")
            self.phases.append(normalized)

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self._active_loader_groups: List[Dict[str, Any]] = []
        self._retired_loader_groups: List[Dict[str, Any]] = []

    def setup(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        setup_cgroup_v2(self.root_cgroup_path, self.root_cgroup_controllers)

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        self._stop_all_loader_groups()
        if self.cleanup_stale_on_prepare:
            cleanup_stale_loaders(self.stale_loader_patterns)
        if self.cleanup_bpffs_on_prepare:
            cleanup_bpffs_pins(self.bpffs_pin_paths)
        self.start_time = None
        self.end_time = None
        self._active_loader_groups = []
        self._retired_loader_groups = []
        self.collected_data = {
            "scene_id": self.scene_id,
            "scene_name": self.scene_name,
            "target_device": self.target_device,
            "native_scheduler": self.native_scheduler,
            "scx_scheduler": self.scx_scheduler,
            "final_scheduler": self.final_scheduler,
            "root_cgroup_path": self.root_cgroup_path,
            "phases": [],
            "events": [],
            "loader_groups": [],
        }

    def _run(self) -> None:
        self.start_time = time.time()
        self._record_event("run_start", phase="", details={"phase_count": len(self.phases)})
        try:
            for phase in self.phases:
                if self.should_stop():
                    break
                self._run_phase(phase)
            self._record_event("run_complete", phase="", details={"stopped": self.should_stop()})
        except Exception:
            self._record_event("run_error_cleanup", phase="", details={})
            self._stop_all_loader_groups()
            self._safe_set_scheduler(self.final_scheduler, "error_final_scheduler")
            raise
        finally:
            if self.end_time is None:
                self.end_time = time.time()

    def _run_phase(self, phase: Mapping[str, Any]) -> None:
        phase_name = str(phase.get("name") or "phase")
        action = str(phase.get("action") or "sleep").strip().lower()
        duration = max(0.0, float(phase.get("duration_sec", 0) or 0))
        started_at = time.time()
        phase_record: Dict[str, Any] = {
            "name": phase_name,
            "action": action,
            "policy": phase.get("policy"),
            "duration_configured_sec": duration,
            "started_at": started_at,
            "started_relative_sec": self._relative_time(started_at),
            "status": "running",
        }
        self.collected_data["phases"].append(phase_record)
        self._record_event("phase_start", phase=phase_name, details={"action": action, "duration_sec": duration})

        if action in {"native", "none", "baseline"}:
            if bool(phase.get("ensure_detached", True)):
                self._record_event("native_stop_existing_begin", phase=phase_name, details={})
                self._stop_all_loader_groups()
                self._record_event("native_stop_existing_end", phase=phase_name, details={})
            self._safe_set_scheduler(str(phase.get("scheduler", self.native_scheduler)), "native_scheduler", phase_name)
        elif action == "attach":
            if bool(phase.get("stop_existing", True)):
                self._stop_all_loader_groups()
            self._safe_set_scheduler(str(phase.get("scheduler", self.scx_scheduler)), "attach_scheduler", phase_name)
            self._start_policy(str(phase.get("policy") or ""), phase_name, replace_existing=False)
        elif action == "detach":
            self._record_event("detach_trigger", phase=phase_name, details=self._active_group_summary())
            self._stop_all_loader_groups()
            self._record_event("detach_complete", phase=phase_name, details={})
            self._safe_set_scheduler(str(phase.get("scheduler", self.native_scheduler)), "detach_scheduler", phase_name)
        elif action in {"switch", "replace"}:
            self._safe_set_scheduler(str(phase.get("scheduler", self.scx_scheduler)), "switch_scheduler", phase_name)
            self._record_event("switch_trigger", phase=phase_name, details=self._active_group_summary())
            self._start_policy(str(phase.get("policy") or ""), phase_name, replace_existing=True)
            self._record_event("switch_complete", phase=phase_name, details=self._active_group_summary())
        elif action == "sleep":
            pass
        else:
            raise ValueError(f"unsupported policy transition phase action: {action}")

        self._sleep_interruptibly(duration)
        ended_at = time.time()
        phase_record.update(
            {
                "ended_at": ended_at,
                "ended_relative_sec": self._relative_time(ended_at),
                "duration_wall_sec": ended_at - started_at,
                "status": "stopped" if self.should_stop() else "success",
            }
        )
        self._record_event("phase_end", phase=phase_name, details={"action": action})

    def _start_policy(self, policy_id: str, phase_name: str, replace_existing: bool) -> None:
        if not policy_id:
            raise ValueError(f"phase {phase_name} requires a policy id")
        profile = copy.deepcopy(self.policy_profiles.get(policy_id))
        if not profile:
            raise ValueError(f"unknown policy profile: {policy_id}")

        if replace_existing:
            self._retired_loader_groups.extend(self._active_loader_groups)
            self._active_loader_groups = []

        policy_output_dir = os.path.join(self.output_dir, f"{int(time.time())}_{phase_name}_{policy_id}")
        os.makedirs(policy_output_dir, exist_ok=True)
        parameters = self._profile_to_loader_parameters(profile, policy_output_dir)
        self._record_event(
            "policy_start_begin",
            phase=phase_name,
            details={"policy": policy_id, "replace_existing": replace_existing, "output_dir": policy_output_dir},
        )
        procs = start_loader_chain(parameters, policy_output_dir, self.root_cgroup_path)
        group = {
            "policy": policy_id,
            "phase": phase_name,
            "replace_existing": replace_existing,
            "started_at": time.time(),
            "started_relative_sec": self._relative_time(),
            "output_dir": policy_output_dir,
            "loader_logs": loader_log_paths(procs),
            "processes": procs,
            "stopped_at": None,
            "returncodes": [],
        }
        self._active_loader_groups.append(group)
        self._record_event("policy_start_end", phase=phase_name, details=self._public_group(group))

    def _profile_to_loader_parameters(self, profile: Mapping[str, Any], output_dir: str) -> Dict[str, Any]:
        parameters = copy.deepcopy(profile.get("parameters") or {})
        io_ext = copy.deepcopy(parameters.get("io_ext") or profile.get("io_ext") or {})
        if not isinstance(io_ext, dict):
            raise ValueError("policy profile io_ext must be a mapping")
        io_ext.setdefault("cleanup_stale", False)
        io_ext.setdefault("cleanup_bpffs_pins", False)
        root_cgroup = io_ext.setdefault("root_cgroup", {})
        if isinstance(root_cgroup, dict):
            root_cgroup.setdefault("path", self.root_cgroup_path)
            root_cgroup.setdefault("controllers", self.root_cgroup_controllers)
        parameters["io_ext"] = io_ext
        parameters.setdefault("target_device", self.target_device)
        parameters.setdefault("scheduler", self.scx_scheduler)
        parameters.setdefault("output_dir", output_dir)
        return parameters

    def _stop_all_loader_groups(self) -> None:
        groups = [*self._active_loader_groups, *self._retired_loader_groups]
        self._active_loader_groups = []
        self._retired_loader_groups = []
        for group in groups:
            self._stop_loader_group(group)

    def _stop_loader_group(self, group: Dict[str, Any]) -> None:
        procs = list(group.get("processes") or [])
        if procs:
            group["loader_logs"] = loader_log_paths(procs)
        stop_loader_chain(procs)
        group["stopped_at"] = time.time()
        group["stopped_relative_sec"] = self._relative_time(group["stopped_at"])
        group["returncodes"] = [proc.returncode for proc in procs]
        public_group = self._public_group(group)
        self.collected_data.setdefault("loader_groups", []).append(public_group)
        self._record_event("policy_stop", phase=str(group.get("phase") or ""), details=public_group)

    def _active_group_summary(self) -> Dict[str, Any]:
        return {"active_policies": [str(group.get("policy")) for group in self._active_loader_groups]}

    def _public_group(self, group: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "policy": group.get("policy"),
            "phase": group.get("phase"),
            "replace_existing": group.get("replace_existing"),
            "started_at": group.get("started_at"),
            "started_relative_sec": group.get("started_relative_sec"),
            "stopped_at": group.get("stopped_at"),
            "stopped_relative_sec": group.get("stopped_relative_sec"),
            "output_dir": group.get("output_dir"),
            "loader_logs": group.get("loader_logs") or [],
            "returncodes": group.get("returncodes") or [],
        }

    def _safe_set_scheduler(self, scheduler: str, event_name: str, phase: str = "") -> None:
        self._record_event(f"{event_name}_begin", phase=phase, details={"scheduler": scheduler})
        set_scheduler(self.target_device, scheduler)
        self._record_event(f"{event_name}_end", phase=phase, details={"scheduler": scheduler})

    def _sleep_interruptibly(self, duration_sec: float) -> None:
        deadline = time.time() + duration_sec
        while not self.should_stop() and time.time() < deadline:
            for group in self._active_loader_groups:
                procs = group.get("processes") or []
                if procs:
                    assert_loader_chain_alive(procs)
            time.sleep(min(self.poll_interval_sec, max(0.0, deadline - time.time())))

    def _record_event(self, event: str, phase: str, details: Mapping[str, Any]) -> None:
        now = time.time()
        self.collected_data.setdefault("events", []).append(
            {
                "event": event,
                "phase": phase,
                "timestamp": now,
                "relative_sec": self._relative_time(now),
                "details": dict(details),
            }
        )

    def _relative_time(self, timestamp: Optional[float] = None) -> float:
        if self.start_time is None:
            return 0.0
        return float((timestamp if timestamp is not None else time.time()) - self.start_time)

    def end(self) -> None:
        self._stop_all_loader_groups()
        try:
            self._safe_set_scheduler(self.final_scheduler, "final_scheduler")
        except Exception as exc:
            logger.warning("failed to restore final scheduler %s: %s", self.final_scheduler, exc)
        if self.end_time is None:
            self.end_time = time.time()

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        self.end()
        if self.cleanup_bpffs_on_teardown:
            cleanup_bpffs_pins(self.bpffs_pin_paths)

    def collect(self) -> Dict[str, Any]:
        if self.start_time is None:
            raise RuntimeError("policy transition scene has not started")
        if self.end_time is None:
            self.end_time = time.time()
        self.collected_data["start_time"] = self.start_time
        self.collected_data["end_time"] = self.end_time
        self.collected_data["duration_wall_sec"] = self.end_time - self.start_time
        self.collected_data["active_loader_groups_at_collect"] = [
            self._public_group(group) for group in self._active_loader_groups
        ]
        self.collected_data["retired_loader_groups_at_collect"] = [
            self._public_group(group) for group in self._retired_loader_groups
        ]
        return self.collected_data

    def stat(self) -> str:
        if self.start_time is not None:
            self.collect()
        return write_json_stat(self.output_dir, "policy_transition", self.scene_id, self.collected_data)

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.start_time = self.collected_data.get("start_time")
        self.end_time = self.collected_data.get("end_time")

    def score(self) -> float:
        return 0.0
