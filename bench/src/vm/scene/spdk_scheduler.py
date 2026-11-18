"""SPDK readiness and io-ext scheduler binding smoke scene."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from vm.scene.base_scene import Scene

from .common import (
    cleanup_cgroup,
    expand_path,
    loader_log_paths,
    set_scheduler,
    setup_cgroup_v2,
    start_loader_chain,
    stop_loader_chain,
    validate_block_device_name,
    write_json_stat,
)


DEFAULT_OUTPUT_DIR = "/home/anon/ioext-bench/test_results/io_ext/spdk_scheduler"
DEFAULT_SPDK_HOME_CANDIDATES = [
    "$SPDK_HOME",
    "/home/anon/ioext-bench/temp/spdk",
    "/home/anon/spdk",
    "/opt/spdk",
]


class IoExtSpdkSchedulerBindingScene(Scene):
    """Probe SPDK readiness and verify safe kernel scheduler/loader binding.

    SPDK device binding removes the NVMe block device from the kernel, so the
    default smoke intentionally stays in probe/fallback mode. It records why SPDK
    cannot run and still proves the io-ext path that remains safe in a VM:
    cgroup creation, scheduler write/readback, and loader process lifecycle.
    """

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        target_device = parameters.get("target_device", "nvme0n1")
        self.target_device = validate_block_device_name(str(target_device))
        self.device_path = f"/dev/{self.target_device}"
        self.output_dir = expand_path(parameters.get("output_dir")) or DEFAULT_OUTPUT_DIR
        self.duration = int(parameters.get("duration", 30))

        scheduler_cfg = dict(parameters.get("scheduler_probe") or {})
        self.target_scheduler = str(scheduler_cfg.get("target_scheduler", parameters.get("scheduler", "scx_io")))
        self.restore_original_scheduler = _bool_param(scheduler_cfg.get("restore_original", True))
        self.require_scheduler_binding = _bool_param(scheduler_cfg.get("require_binding", True))
        self.require_target_available = _bool_param(scheduler_cfg.get("require_target_available", True))

        self.spdk_cfg = dict(parameters.get("spdk") or {})
        self.spdk_home_candidates = list(
            self.spdk_cfg.get("home_candidates") or DEFAULT_SPDK_HOME_CANDIDATES
        )
        self.require_spdk = _bool_param(self.spdk_cfg.get("require_spdk", False))
        self.allow_device_bind = _bool_param(self.spdk_cfg.get("allow_device_bind", False))
        self.run_setup_status = _bool_param(self.spdk_cfg.get("run_setup_status", True))
        self.spdk_command_timeout_sec = float(self.spdk_cfg.get("command_timeout_sec", 12.0))

        io_ext_cfg = dict(parameters.get("io_ext") or {})
        self.io_ext_cfg = io_ext_cfg
        cgroup_cfg = io_ext_cfg.get("cgroup") or {}
        self.cgroup_path = expand_path(cgroup_cfg.get("path") or io_ext_cfg.get("cgroup_path"))
        self.cgroup_controllers = list(cgroup_cfg.get("controllers") or ["io"])
        self.run_loader_probe = _bool_param(io_ext_cfg.get("run_loader_probe", True))
        self.require_loader_probe = _bool_param(io_ext_cfg.get("require_loader_probe", True))

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.collected_data: Dict[str, Any] = {}
        self._loader_procs: List[subprocess.Popen[str]] = []

    def setup(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        if not os.path.exists(self.device_path):
            raise FileNotFoundError(f"target block device does not exist: {self.device_path}")
        if self.cgroup_path:
            setup_cgroup_v2(self.cgroup_path, self.cgroup_controllers)

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        self.start_time = None
        self.end_time = None
        self.collected_data = {
            "target_device": self.target_device,
            "device_path": self.device_path,
            "target_scheduler": self.target_scheduler,
            "restore_original_scheduler": self.restore_original_scheduler,
            "allow_device_bind": self.allow_device_bind,
            "require_spdk": self.require_spdk,
            "require_scheduler_binding": self.require_scheduler_binding,
            "require_loader_probe": self.require_loader_probe,
        }

    def _run(self) -> None:
        self.start_time = time.time()
        self.collected_data["spdk_readiness"] = self._probe_spdk_readiness()
        self.collected_data["scheduler_probe"] = self._run_scheduler_and_loader_probe()
        self.end_time = time.time()
        self.collected_data["effective_mode"] = self._determine_effective_mode()
        self._validate_requirements()

    def end(self) -> None:
        self._should_stop.set()
        self._stop_loader()

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        self.end()
        cleanup_cgroup(self.cgroup_path)

    def collect(self) -> Dict[str, Any]:
        if self.start_time is None or self.end_time is None:
            raise RuntimeError("scene has not completed")
        self.collected_data["start_time"] = self.start_time
        self.collected_data["end_time"] = self.end_time
        self.collected_data["duration_wall"] = self.end_time - self.start_time
        return self.collected_data

    def stat(self) -> str:
        if self.start_time is not None and self.end_time is not None:
            self.collect()
        return write_json_stat(self.output_dir, "spdk_scheduler", self.scene_id, self.collected_data)

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.start_time = self.collected_data.get("start_time")
        self.end_time = self.collected_data.get("end_time")
        self.duration = float(self.collected_data.get("duration_wall") or 0.0)

    def score(self) -> float:
        scheduler_ok = bool(self.collected_data.get("scheduler_probe", {}).get("binding_success"))
        scheduler_probe = self.collected_data.get("scheduler_probe", {})
        restore_required = bool(scheduler_probe.get("restore_required"))
        restore_ok = bool(scheduler_probe.get("restored")) if restore_required else True
        loader_cfg = self.collected_data.get("scheduler_probe", {}).get("loader_probe", {})
        loader_required = bool(self.collected_data.get("require_loader_probe"))
        loader_ok = bool(loader_cfg.get("success")) if loader_required else True
        spdk_required = bool(self.collected_data.get("require_spdk"))
        spdk_ok = bool(self.collected_data.get("spdk_readiness", {}).get("spdk_available")) if spdk_required else True
        return 1.0 if scheduler_ok and restore_ok and loader_ok and spdk_ok else 0.0

    def _probe_spdk_readiness(self) -> Dict[str, Any]:
        spdk_home = self._find_spdk_home()
        setup_script = os.path.join(spdk_home, "scripts", "setup.sh") if spdk_home else ""
        identify_bin = os.path.join(spdk_home, "build", "bin", "spdk_nvme_identify") if spdk_home else ""
        bdevperf_bin = os.path.join(spdk_home, "build", "bin", "spdk_bdevperf") if spdk_home else ""
        pci_bdf = _extract_pci_bdf(_readlink(f"/sys/block/{self.target_device}/device"))
        readiness: Dict[str, Any] = {
            "spdk_home_candidates": [_expand_candidate(path) for path in self.spdk_home_candidates],
            "spdk_home": spdk_home,
            "setup_script": setup_script,
            "setup_script_exists": bool(setup_script and os.path.isfile(setup_script)),
            "identify_bin": identify_bin,
            "identify_bin_exists": bool(identify_bin and os.path.isfile(identify_bin)),
            "bdevperf_bin": bdevperf_bin,
            "bdevperf_bin_exists": bool(bdevperf_bin and os.path.isfile(bdevperf_bin)),
            "hugepages": _read_hugepages(),
            "vfio_pci_loaded": os.path.isdir("/sys/module/vfio_pci"),
            "uio_pci_generic_loaded": os.path.isdir("/sys/module/uio_pci_generic"),
            "igb_uio_loaded": os.path.isdir("/sys/module/igb_uio"),
            "iommu_group_count": _count_dirs("/sys/kernel/iommu_groups"),
            "nvme_controllers": _list_dir("/sys/class/nvme"),
            "target_pci_bdf": pci_bdf,
            "target_pci_driver": _pci_driver_name(pci_bdf),
            "allow_device_bind": self.allow_device_bind,
        }
        readiness["spdk_available"] = bool(
            readiness["setup_script_exists"]
            and (readiness["identify_bin_exists"] or readiness["bdevperf_bin_exists"])
        )
        if readiness["setup_script_exists"] and self.run_setup_status:
            readiness["setup_status"] = _run_sudo_command(
                [setup_script, "status"], timeout_sec=self.spdk_command_timeout_sec
            )
        readiness["safe_bind_skipped_reason"] = self._spdk_skip_reason(readiness)
        return readiness

    def _run_scheduler_and_loader_probe(self) -> Dict[str, Any]:
        before = _read_scheduler_state(self.target_device)
        result: Dict[str, Any] = {
            "before": before,
            "target_scheduler": self.target_scheduler,
            "target_available": self.target_scheduler in before.get("available", []),
            "binding_success": False,
            "restored": False,
        }
        if self.require_target_available and not result["target_available"]:
            result["error"] = f"target scheduler {self.target_scheduler!r} not available"
            return result

        original_scheduler = before.get("current")
        result["original_scheduler"] = original_scheduler
        result["restore_required"] = bool(self.restore_original_scheduler and original_scheduler)
        try:
            set_scheduler(self.target_device, self.target_scheduler)
            after_set = _read_scheduler_state(self.target_device)
            result["after_set"] = after_set
            result["binding_success"] = after_set.get("current") == self.target_scheduler
            if result["binding_success"] and self.run_loader_probe:
                result["loader_probe"] = self._run_loader_probe()
        finally:
            if self.restore_original_scheduler and original_scheduler:
                try:
                    set_scheduler(self.target_device, str(original_scheduler))
                    restored = _read_scheduler_state(self.target_device)
                    result["after_restore"] = restored
                    result["restored"] = restored.get("current") == original_scheduler
                except Exception as exc:
                    result["restore_error"] = str(exc)
        return result

    def _run_loader_probe(self) -> Dict[str, Any]:
        loader_cfgs = self.io_ext_cfg.get("loaders") or self.io_ext_cfg.get("loader_chain") or []
        if not loader_cfgs:
            return {"enabled": False, "success": not self.require_loader_probe, "reason": "no loaders configured"}
        probe: Dict[str, Any] = {"enabled": True, "success": False, "loader_logs": []}
        try:
            self._loader_procs = start_loader_chain(self.parameters, self.output_dir, self.cgroup_path)
            probe["loader_logs"] = loader_log_paths(self._loader_procs)
            probe["process_count"] = len(self._loader_procs)
            probe["success"] = len(self._loader_procs) > 0
            return probe
        except Exception as exc:
            probe["error"] = str(exc)
            return probe
        finally:
            self._stop_loader()

    def _stop_loader(self) -> None:
        if self._loader_procs:
            self.collected_data.setdefault("loader_logs", loader_log_paths(self._loader_procs))
        stop_loader_chain(self._loader_procs)
        self._loader_procs = []

    def _find_spdk_home(self) -> str:
        for candidate in self.spdk_home_candidates:
            expanded = _expand_candidate(candidate)
            if expanded and os.path.isdir(expanded):
                return expanded
        return ""

    def _spdk_skip_reason(self, readiness: Mapping[str, Any]) -> str:
        if self.allow_device_bind:
            return "allow_device_bind=true; destructive bind still not executed by this smoke"
        if not readiness.get("spdk_home"):
            return "SPDK checkout not found"
        if not readiness.get("setup_script_exists"):
            return "SPDK scripts/setup.sh not found"
        if readiness.get("hugepages", {}).get("HugePages_Total", 0) <= 0:
            return "no hugepages configured"
        if not readiness.get("vfio_pci_loaded") and not readiness.get("uio_pci_generic_loaded"):
            return "vfio-pci/uio driver unavailable"
        return "device binding disabled by default to keep VM block device intact"

    def _determine_effective_mode(self) -> str:
        if self.collected_data.get("spdk_readiness", {}).get("spdk_available") and self.allow_device_bind:
            return "spdk_available_bind_disabled_smoke"
        return "spdk_readiness_fallback_scheduler_loader_probe"

    def _validate_requirements(self) -> None:
        spdk_readiness = self.collected_data.get("spdk_readiness", {})
        scheduler_probe = self.collected_data.get("scheduler_probe", {})
        if self.require_spdk and not spdk_readiness.get("spdk_available"):
            raise RuntimeError(f"SPDK is required but unavailable: {spdk_readiness.get('safe_bind_skipped_reason')}")
        if self.require_scheduler_binding and not scheduler_probe.get("binding_success"):
            raise RuntimeError(f"scheduler binding failed: {scheduler_probe}")
        if scheduler_probe.get("restore_required") and not scheduler_probe.get("restored"):
            raise RuntimeError(f"scheduler restore failed: {scheduler_probe}")
        loader_probe = scheduler_probe.get("loader_probe") or {}
        if self.require_loader_probe and not loader_probe.get("success"):
            raise RuntimeError(f"loader probe failed: {loader_probe}")


def _bool_param(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _expand_candidate(path: Any) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(text))
    if "$" in expanded:
        return ""
    return os.path.abspath(expanded)


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _readlink(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return ""


def _read_scheduler_state(device: str) -> Dict[str, Any]:
    path = f"/sys/block/{validate_block_device_name(device)}/queue/scheduler"
    text = _read_text(path)
    available: List[str] = []
    current = ""
    for token in text.split():
        if token.startswith("[") and token.endswith("]"):
            current = token.strip("[]")
            available.append(current)
        else:
            available.append(token)
    return {"path": path, "raw": text, "current": current, "available": available, "exists": bool(text)}


def _read_hugepages() -> Dict[str, int]:
    wanted = {"HugePages_Total", "HugePages_Free", "HugePages_Rsvd", "HugePages_Surp", "Hugepagesize"}
    values: Dict[str, int] = {}
    for line in _read_text("/proc/meminfo").splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        if key not in wanted:
            continue
        match = re.search(r"[0-9]+", rest)
        if match:
            values[key] = int(match.group(0))
    return values


def _count_dirs(path: str) -> int:
    try:
        return sum(1 for item in os.listdir(path) if os.path.isdir(os.path.join(path, item)))
    except OSError:
        return 0


def _list_dir(path: str) -> List[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _extract_pci_bdf(path: str) -> str:
    matches = re.findall(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", path)
    return matches[-1] if matches else ""


def _pci_driver_name(bdf: str) -> str:
    if not bdf:
        return ""
    driver = _readlink(f"/sys/bus/pci/devices/{bdf}/driver")
    return os.path.basename(driver) if driver else ""


def _run_sudo_command(argv: Iterable[str], timeout_sec: float) -> Dict[str, Any]:
    cmd = ["/usr/bin/sudo", *[str(part) for part in argv]]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"cmd": cmd, "timeout": True, "returncode": None, "stdout": exc.stdout or "", "stderr": exc.stderr or ""}
    return {
        "cmd": cmd,
        "timeout": False,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }
