"""Self-contained io-ext FIO scene."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from glob import glob
from typing import Any, Dict, List, Mapping, Optional, Set

from lib.benchmark.fio_time_log import summarize_fio_time_log

from ..base_scene import Scene
from ..utils import DataScorer

from .common import (
    BASH_BIN,
    DEFAULT_ROOT_CGROUP_PATH,
    SUDO_BIN,
    apply_block_device_tuning,
    cleanup_cgroup,
    expand_path,
    file_tail,
    fio_json_metrics,
    interrupt_process,
    loader_log_paths,
    popen_in_own_session,
    run_sudo_shell,
    setup_cgroup_v2,
    setup_cgroup_io_controls,
    setup_iocost_at_root,
    set_d2fq_setup,
    clear_d2fq_setup,
    set_nr_requests,
    set_scheduler,
    set_scheduler_tunables,
    start_loader_chain,
    stop_loader_chain,
    validate_block_device_name,
    wrap_command_in_cgroup,
)


logger = logging.getLogger("io_ext_fio")


_FIO_BOOL_FLAG_OPTIONS = {
    "time_based",
    "group_reporting",
}
_FIO_LOG_OPTIONS = {
    "write_bw_log",
    "write_lat_log",
    "write_iops_log",
}
_FIO_FILENAME_OPTION_KEYS = (
    "file_name",
    "filename",
    "fio_filename",
)
_FIO_RESERVED_OPTIONS = {
    "name",
    "filename",
    "file_name",
    "fio_filename",
    "output",
    "output_format",
    "output-format",
}
_FIO_HANDLED_CLI_OPTIONS = {
    "name",
    "filename",
    "ioengine",
    "direct",
    "bs",
    "rw",
    "rwmixread",
    "iodepth",
    "numjobs",
    "runtime",
    "runtime_sec",
    "time_based",
    "group_reporting",
    "clat_percentiles",
    "lat_percentiles",
    "percentile_list",
    "output",
    "output_format",
    "output-format",
}


class IoExtFioScene(Scene):
    """Run fio through an io-ext loader chain and cgroup."""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        target_device = parameters.get("target_device")
        if not target_device:
            raise ValueError("parameters.target_device is required, e.g. nullb0 or nvme0n1")
        self.target_device = validate_block_device_name(str(target_device))

        self.fio_global: Dict[str, Any] = dict(parameters.get("fio_global") or parameters.get("fio") or {})
        if not self.fio_global.get("runtime_sec") and not self.fio_global.get("runtime"):
            raise ValueError("fio_global requires runtime_sec or runtime")
        if "runtime_sec" not in self.fio_global and "runtime" in self.fio_global:
            self.fio_global["runtime_sec"] = self.fio_global["runtime"]

        self.fio_filename = self._resolve_fio_filename(parameters)

        raw_jobs = parameters.get("fio_jobs") or []
        if raw_jobs and not isinstance(raw_jobs, list):
            raise ValueError("parameters.fio_jobs must be a list of fio job dictionaries")
        self.fio_jobs: List[Dict[str, Any]] = []
        for idx, raw_job in enumerate(raw_jobs):
            if not isinstance(raw_job, dict):
                raise ValueError(f"parameters.fio_jobs[{idx}] must be a dictionary")
            self.fio_jobs.append(dict(raw_job))

        self.concurrency_levels: List[int] = list(parameters.get("concurrency_levels") or [])
        if not self.concurrency_levels:
            self.concurrency_levels = [int(self.fio_global.get("numjobs", 1))]
        # Base scene fio.yml pins concurrency_levels=[1]; that value is what
        # becomes [global] numjobs. fio_global.numjobs alone does not spawn
        # extra processes.

        self.system_cfg: Dict[str, Any] = dict(parameters.get("system") or {})
        self.output_dir = expand_path(parameters.get("output_dir")) or "./test_results"
        self.job_name = parameters.get("job_name", "arena_fio")

        io_ext = parameters.get("io_ext") or {}
        self.io_ext_cfg: Dict[str, Any] = dict(io_ext)
        self.scheduler = io_ext.get("scheduler", parameters.get("scheduler"))
        self.scheduler_tunables: Dict[str, Any] = dict(
            io_ext.get("scheduler_tunables") or parameters.get("scheduler_tunables") or {}
        )
        cgroup_cfg = io_ext.get("cgroup") or {}
        self.io_ext_cgroup_path = expand_path(cgroup_cfg.get("path") or io_ext.get("cgroup_path"))
        self.io_ext_cgroup_controllers = list(cgroup_cfg.get("controllers") or ["io"])
        self.io_ext_cgroup_io_weight = cgroup_cfg.get("io_weight", cgroup_cfg.get("io.weight"))
        self.io_ext_cgroup_io_max = cgroup_cfg.get("io_max", cgroup_cfg.get("io.max"))
        self.io_ext_cgroup_io_latency = cgroup_cfg.get("io_latency", cgroup_cfg.get("io.latency"))
        self.io_ext_cgroup_io_prio_class = cgroup_cfg.get("io_prio_class", cgroup_cfg.get("io.prio.class"))
        self.io_ext_cgroup_io_bfq_weight = cgroup_cfg.get("io_bfq_weight", cgroup_cfg.get("io.bfq.weight"))
        io_cost_cfg = cgroup_cfg.get("io_cost", cgroup_cfg.get("io.cost")) or {}
        self.io_ext_d2fq = io_ext.get("d2fq")
        self.io_ext_d2fq_controls: Optional[Dict[str, Any]] = None
        self.io_ext_iocost_model = io_cost_cfg.get("model")
        self.io_ext_iocost_qos = io_cost_cfg.get("qos")
        self.io_ext_iocost_root_path = expand_path(
            (io_ext.get("root_cgroup") or {}).get("path") if isinstance(io_ext.get("root_cgroup"), dict)
            else io_ext.get("root_cgroup_path")
        ) or DEFAULT_ROOT_CGROUP_PATH
        has_any_knob = (
            self.io_ext_cgroup_io_weight is not None
            or self.io_ext_cgroup_io_max is not None
            or self.io_ext_cgroup_io_latency is not None
            or self.io_ext_cgroup_io_prio_class is not None
            or self.io_ext_iocost_model is not None
            or self.io_ext_iocost_qos is not None
        )
        if has_any_knob and not self.io_ext_cgroup_path and not bool(parameters.get("fio_subjob_tenants")) and (self.io_ext_cgroup_io_weight is not None or self.io_ext_cgroup_io_max is not None or self.io_ext_cgroup_io_latency is not None or self.io_ext_cgroup_io_prio_class is not None):
            raise ValueError("io_ext.cgroup.path is required when io_weight/io_max/io_latency/io_prio_class is configured")
        if has_any_knob:
            enabled_controllers = {str(ctrl).strip().lstrip("+") for ctrl in self.io_ext_cgroup_controllers}
            if "io" not in enabled_controllers:
                self.io_ext_cgroup_controllers.append("io")
        self.io_ext_cgroup_controls: Dict[str, Any] = {}
        self._loader_procs: List[subprocess.Popen[str]] = []

        # io_ext.cgroups: value-bearing specs for cgroups that host NO fio
        # workload of this scene (e.g. native-arm subtree roots g1/g2 or
        # lc/be in the Session-5 tree grids). setup_cgroup_io_controls is
        # path-generic, so each entry only needs its own path + controller
        # values. Entries must sit ON the tenant cgroup chain so the leaf
        # teardown walk-up removes them; no dedicated cleanup is added.
        raw_extra_cgroups = io_ext.get("cgroups") or []
        if not isinstance(raw_extra_cgroups, list):
            raise ValueError("io_ext.cgroups must be a list of cgroup specs")
        self.io_ext_extra_cgroups: List[Dict[str, Any]] = []
        self._extra_cgroup_controls: Dict[str, Dict[str, Any]] = {}
        for idx, entry in enumerate(raw_extra_cgroups):
            if not isinstance(entry, dict):
                raise ValueError(f"io_ext.cgroups[{idx}] must be a dict")
            path = expand_path(entry.get("path") or entry.get("cgroup_path"))
            if not path:
                raise ValueError(f"io_ext.cgroups[{idx}].path is required")
            self.io_ext_extra_cgroups.append({
                "path": path,
                "controllers": list(entry.get("controllers") or ["io"]),
                "io_weight": entry.get("io_weight", entry.get("io.weight")),
                "io_max": entry.get("io_max", entry.get("io.max")),
                "io_latency": entry.get("io_latency", entry.get("io.latency")),
                "io_prio_class": entry.get("io_prio_class", entry.get("io.prio.class")),
                "io_bfq_weight": entry.get("io_bfq_weight", entry.get("io.bfq.weight")),
            })

        raw_subjob_tenants = parameters.get("fio_subjob_tenants") or []
        self.fio_subjob_mode = bool(raw_subjob_tenants) or str(parameters.get("execution_mode") or "").lower() == "fio_subjob"
        self.fio_subjob_tenants: List[Dict[str, Any]] = []
        self._subjob_cgroup_paths: List[str] = []
        self._subjob_cgroup_controls: Dict[str, Dict[str, Any]] = {}
        if self.fio_subjob_mode:
            for idx, st in enumerate(raw_subjob_tenants):
                if not isinstance(st, dict):
                    raise ValueError(f"parameters.fio_subjob_tenants[{idx}] must be a dict")
                entry = dict(st)
                cgroup_path = expand_path(entry.get("cgroup_path") or "")
                if cgroup_path:
                    self._subjob_cgroup_paths.append(cgroup_path)
                self.fio_subjob_tenants.append(entry)
            self.tenant_count = int(parameters.get("tenant_count") or len(self.fio_subjob_tenants) or 0)
        else:
            self.tenant_count = 0

        null_blk_cfg = parameters.get("null_blk")
        self._null_blk_config: Optional[Dict[str, Any]] = None
        if null_blk_cfg is True:
            self._null_blk_config = {"queue_mode": 2, "submit_queues": 64}
        elif isinstance(null_blk_cfg, dict):
            self._null_blk_config = dict(null_blk_cfg)
        self._remove_null_blk_on_teardown = bool(parameters.get("remove_null_blk_on_teardown", False))

        self.drop_caches_before_run = parameters.get("drop_caches_before_run", True)
        self.cooldown_sec = float(parameters.get("cooldown_sec", 1.0))

        runs = len(self.concurrency_levels)
        runtime = float(self.fio_global["runtime_sec"])
        default_duration = runtime * runs + self.cooldown_sec * max(0, runs - 1) + 30
        self.duration = int(parameters.get("duration", default_duration))

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self._fio_proc: Optional[subprocess.Popen[str]] = None
        self._fio_json_paths: List[str] = []
        self._fio_stderr_paths: List[str] = []
        self._fio_job_file_paths: List[str] = []
        self._fio_time_log_paths: List[str] = []
        self._fio_log_prefixes: List[str] = []

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
        if self.io_ext_cgroup_path:
            setup_cgroup_v2(self.io_ext_cgroup_path, self.io_ext_cgroup_controllers)
        for cg_path in self._subjob_cgroup_paths:
            setup_cgroup_v2(cg_path, self.io_ext_cgroup_controllers)
        for entry in self.io_ext_extra_cgroups:
            setup_cgroup_v2(entry["path"], entry["controllers"])

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        self._apply_cgroup_io_controls()
        if self.fio_subjob_mode:
            self._apply_subjob_cgroup_io_controls()
        self._apply_extra_cgroup_io_controls()
        self.collected_data = {
            "runs": [],
            "target_device": self.target_device,
            "fio_filename": self.fio_filename,
            "scheduler": self.scheduler,
            "io_ext_cgroup_path": self.io_ext_cgroup_path,
            "io_ext_cgroup_controls": self.io_ext_cgroup_controls,
        }
        if self.io_ext_extra_cgroups:
            self.collected_data["io_ext_extra_cgroup_controls"] = dict(self._extra_cgroup_controls)
        if self.fio_subjob_mode:
            self.collected_data["execution_mode"] = "fio_subjob"
            self.collected_data["tenant_count"] = self.tenant_count
            self.collected_data["subjob_cgroup_paths"] = list(self._subjob_cgroup_paths)
            self.collected_data["subjob_cgroup_controls"] = dict(self._subjob_cgroup_controls)
        self._fio_json_paths = []
        self._fio_stderr_paths = []
        self._fio_job_file_paths = []
        self._fio_time_log_paths = []
        self._fio_log_prefixes = []
        self.start_time = None
        self.end_time = None

    @staticmethod
    def _is_simple_fio_value(value: Any) -> bool:
        return value is None or isinstance(value, (str, int, float, bool))

    @staticmethod
    def _validate_fio_option_key(key: str) -> str:
        option = str(key).strip()
        has_invalid_char = any(ch.isspace() for ch in option) or "=" in option or "\n" in option or "\r" in option
        if not option or has_invalid_char:
            raise ValueError(f"invalid fio option key: {key!r}")
        return option

    @staticmethod
    def _fio_value_to_text(value: Any) -> str:
        if isinstance(value, bool):
            return "1" if value else "0"
        return str(value)

    def _resolve_fio_filename(self, parameters: Dict[str, Any]) -> str:
        candidates: Dict[str, str] = {}
        for source_name, source in (("parameters", parameters), ("fio_global", self.fio_global)):
            for key in _FIO_FILENAME_OPTION_KEYS:
                if key not in source:
                    continue
                value = source[key]
                if value is None:
                    continue
                filename = os.path.expandvars(os.path.expanduser(str(value).strip()))
                if not filename:
                    raise ValueError(f"{source_name}.{key} must not be empty")
                candidates[f"{source_name}.{key}"] = filename

        unique_values = set(candidates.values())
        if len(unique_values) > 1:
            details = ", ".join(f"{key}={value!r}" for key, value in sorted(candidates.items()))
            raise ValueError(f"conflicting fio filename aliases: {details}")
        if unique_values:
            return next(iter(unique_values))
        return f"/dev/{self.target_device}"

    def _run_log_prefix(
        self,
        tag: str,
        option: str,
        value: Any,
        job_name: Optional[str] = None,
    ) -> str:
        raw_name = option if value is True or value is None else os.path.basename(str(value).strip())
        log_name = raw_name or option
        pieces = [self.scene_id, tag]
        if job_name:
            pieces.append(job_name)
        pieces.append(log_name)
        prefix = os.path.join(self.output_dir, "_".join(pieces))
        if prefix not in self._fio_log_prefixes:
            self._fio_log_prefixes.append(prefix)
        return prefix

    def _normalize_fio_options(
        self,
        raw_options: Dict[str, Any],
        tag: str,
        job_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        options: Dict[str, Any] = {}
        for raw_key, raw_value in raw_options.items():
            key = self._validate_fio_option_key(str(raw_key))
            if key in _FIO_RESERVED_OPTIONS or raw_value is None:
                continue
            if not self._is_simple_fio_value(raw_value):
                raise ValueError(f"fio option {key!r} must be a simple scalar value")
            if key == "runtime_sec":
                options.setdefault("runtime", raw_value)
                continue
            if key in _FIO_LOG_OPTIONS:
                options[key] = self._run_log_prefix(tag, key, raw_value, job_name)
            elif key in _FIO_BOOL_FLAG_OPTIONS and isinstance(raw_value, bool):
                if raw_value:
                    options[key] = 1
            else:
                options[key] = raw_value
        return options

    def _fio_cli_arg(self, key: str, value: Any) -> str:
        return f"--{key}={self._fio_value_to_text(value)}"

    def _append_generic_fio_cli_options(
        self,
        argv: List[str],
        fio_options: Dict[str, Any],
        handled_options: Set[str],
    ) -> None:
        for key, value in fio_options.items():
            if key in handled_options or value is None:
                continue
            argv.append(self._fio_cli_arg(key, value))

    def _drop_caches(self) -> None:
        run_sudo_shell("sync; printf '%s\n' 3 > /proc/sys/vm/drop_caches", check=False)

    def _set_scheduler(self) -> None:
        set_scheduler(self.target_device, self.scheduler)
        set_scheduler_tunables(self.target_device, self.scheduler_tunables)
        # An elevator switch resets q->nr_requests to the device default
        # (kernel behavior, verified 20260917); setup() applied the declared
        # value BEFORE this switch, so re-assert it here. Env-only values are
        # already re-applied inside set_scheduler().
        declared_nr = self.system_cfg.get("nr_requests")
        if declared_nr is not None:
            set_nr_requests(self.target_device, int(declared_nr))

    def _apply_cgroup_io_controls(self) -> None:
        self.io_ext_cgroup_controls = setup_cgroup_io_controls(
            self.io_ext_cgroup_path,
            f"/dev/{self.target_device}",
            self.io_ext_cgroup_io_weight,
            self.io_ext_cgroup_io_max,
            self.io_ext_cgroup_io_latency,
            self.io_ext_cgroup_io_prio_class,
            self.io_ext_cgroup_io_bfq_weight,
        )
        if self.io_ext_iocost_model is not None or self.io_ext_iocost_qos is not None:
            iocost_result = setup_iocost_at_root(
                f"/dev/{self.target_device}",
                self.io_ext_iocost_model,
                self.io_ext_iocost_qos,
                root_cgroup_path=self.io_ext_iocost_root_path,
            )
            if iocost_result:
                self.io_ext_cgroup_controls["io_cost"] = iocost_result

    def _apply_subjob_cgroup_io_controls(self) -> None:
        device_path = f"/dev/{self.target_device}"
        for idx, st in enumerate(self.fio_subjob_tenants):
            cg_path = expand_path(st.get("cgroup_path") or "")
            if not cg_path:
                continue
            controls = setup_cgroup_io_controls(
                cg_path,
                device_path,
                st.get("io_weight"),
                st.get("io_max"),
                st.get("io_latency"),
                st.get("io_prio_class"),
                st.get("io_bfq_weight"),
            )
            if controls:
                self._subjob_cgroup_controls[cg_path] = controls

    def _apply_extra_cgroup_io_controls(self) -> None:
        if not self.io_ext_extra_cgroups:
            return
        device_path = f"/dev/{self.target_device}"
        for entry in self.io_ext_extra_cgroups:
            controls = setup_cgroup_io_controls(
                entry["path"],
                device_path,
                entry["io_weight"],
                entry["io_max"],
                entry["io_latency"],
                entry["io_prio_class"],
                entry["io_bfq_weight"],
            )
            if controls:
                self._extra_cgroup_controls[entry["path"]] = controls

    def _apply_d2fq_controls(self) -> None:
        if not self.io_ext_d2fq:
            return
        self.io_ext_d2fq_controls = set_d2fq_setup(self.target_device, self.io_ext_d2fq)

    def _start_loader(self) -> None:
        self._loader_procs = start_loader_chain(self.parameters, self.output_dir, self.io_ext_cgroup_path)

    def _stop_loader(self) -> None:
        if self._loader_procs:
            self.collected_data.setdefault("loader_logs", loader_log_paths(self._loader_procs))
        stop_loader_chain(self._loader_procs)
        self._loader_procs = []

    def _build_fio_argv(self, concurrency: int, fio_json_path: str) -> List[str]:
        tag = f"jobs{concurrency}"
        fio = self._normalize_fio_options(self.fio_global, tag, self.job_name)
        for required_key in ("ioengine", "bs", "rw", "iodepth"):
            if required_key not in fio:
                raise ValueError(f"fio_global.{required_key} is required when fio_jobs is not configured")
        argv = [
            "fio",
            f"--name={self.job_name}",
            f"--filename={self.fio_filename}",
            f"--ioengine={fio['ioengine']}",
            f"--direct={fio.get('direct', 1)}",
            f"--bs={fio['bs']}",
            f"--rw={fio['rw']}",
            f"--iodepth={fio['iodepth']}",
            f"--numjobs={concurrency}",
            f"--runtime={fio['runtime']}",
            "--output-format=json",
            f"--output={fio_json_path}",
        ]
        rw = str(fio.get("rw", ""))
        if "randrw" in rw and fio.get("rwmixread") is not None:
            for idx, arg in enumerate(argv):
                if arg.startswith("--rw="):
                    argv.insert(idx + 1, f"--rwmixread={fio['rwmixread']}")
                    break
        if fio.get("time_based"):
            argv.append("--time_based")
        if fio.get("group_reporting"):
            argv.append("--group_reporting")
        if fio.get("clat_percentiles", True):
            argv.append("--clat_percentiles=1")
        if fio.get("lat_percentiles"):
            argv.append("--lat_percentiles=1")
        percentile_list = fio.get("percentile_list", "99:99.9")
        if percentile_list:
            argv.append(f"--percentile_list={percentile_list}")
        self._append_generic_fio_cli_options(argv, fio, _FIO_HANDLED_CLI_OPTIONS)
        return argv

    def _write_fio_job_file(self, concurrency: int, tag: str, job_file_path: str) -> None:
        global_options = self._normalize_fio_options(self.fio_global, tag)
        global_options["filename"] = self.fio_filename
        global_options["numjobs"] = concurrency
        if global_options.get("clat_percentiles", True):
            global_options["clat_percentiles"] = 1
        if global_options.get("percentile_list") is None:
            global_options.setdefault("percentile_list", "99:99.9")

        lines = ["[global]"]
        for key, value in global_options.items():
            if key in _FIO_RESERVED_OPTIONS and key != "filename":
                continue
            if value is None:
                continue
            lines.append(f"{key}={self._fio_value_to_text(value)}")

        for idx, raw_job in enumerate(self.fio_jobs):
            job_name = str(raw_job.get("name") or f"{self.job_name}_{idx}")
            if not job_name or any(ch in job_name for ch in "[]\n\r"):
                raise ValueError(f"invalid fio job name: {job_name!r}")
            job_options = self._normalize_fio_options(raw_job, tag, job_name)
            lines.append("")
            lines.append(f"[{job_name}]")
            for key, value in job_options.items():
                if key in _FIO_RESERVED_OPTIONS or value is None:
                    continue
                lines.append(f"{key}={self._fio_value_to_text(value)}")

        with open(job_file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _build_fio_jobfile_argv(self, concurrency: int, tag: str, fio_json_path: str) -> List[str]:
        job_file_path = os.path.join(self.output_dir, f"{self.scene_id}_{tag}_fio.job")
        self._write_fio_job_file(concurrency, tag, job_file_path)
        self._fio_job_file_paths.append(job_file_path)
        return [
            "fio",
            "--output-format=json",
            f"--output={fio_json_path}",
            job_file_path,
        ]

    def _refresh_fio_time_logs(self) -> List[str]:
        discovered = list(self._fio_time_log_paths)
        seen = set(discovered)
        for prefix in self._fio_log_prefixes:
            for path in glob(f"{prefix}*.log"):
                if os.path.isfile(path) and path not in seen:
                    discovered.append(path)
                    seen.add(path)
        self._fio_time_log_paths = discovered
        return discovered

    def _build_full_cmd(self, fio_argv: List[str]) -> List[str]:
        cmd: List[str] = [SUDO_BIN]
        cpu_bind = self.system_cfg.get("cpu_bind")
        if cpu_bind:
            cmd.extend(["taskset", "-c", str(cpu_bind)])
        cmd.extend(fio_argv)
        if self.fio_subjob_mode:
            return cmd
        wrapped = wrap_command_in_cgroup(cmd, self.io_ext_cgroup_path)
        return list(wrapped) if not isinstance(wrapped, str) else [BASH_BIN, "-lc", wrapped]

    def _run(self) -> None:
        self.start_time = time.time()
        if self.drop_caches_before_run:
            self._drop_caches()

        self._set_scheduler()
        self._apply_d2fq_controls()
        self._start_loader()

        try:
            for concurrency in self.concurrency_levels:
                if self.should_stop():
                    break
                tag = f"jobs{concurrency}"
                fio_out = os.path.join(self.output_dir, f"{self.scene_id}_{tag}_fio.json")
                self._fio_json_paths.append(fio_out)

                fio_argv = (
                    self._build_fio_jobfile_argv(concurrency, tag, fio_out)
                    if self.fio_jobs
                    else self._build_fio_argv(concurrency, fio_out)
                )
                full_cmd = self._build_full_cmd(fio_argv)
                stderr_path = os.path.join(self.output_dir, f"{self.scene_id}_{tag}_fio.stderr.log")
                self._fio_stderr_paths.append(stderr_path)

                logger.info("running io-ext fio: concurrency=%s cmd=%s", concurrency, " ".join(full_cmd))
                with open(stderr_path, "w", encoding="utf-8") as stderr_f:
                    proc = popen_in_own_session(
                        full_cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr_f,
                        text=True,
                    )
                    self._fio_proc = proc
                    while proc.poll() is None:
                        if self.should_stop():
                            interrupt_process(proc)
                            break
                        time.sleep(0.2)

                rc = proc.returncode
                err = file_tail(stderr_path, max_chars=2000)
                if not self.should_stop() and rc not in (0, None):
                    raise RuntimeError(f"fio failed returncode={rc} stderr={err[:2000]!r}")

                run_data: Dict[str, Any] = {
                    "concurrency": concurrency,
                    "fio_json": fio_out,
                    "stderr_log": stderr_path,
                    "returncode": rc,
                }
                if err:
                    run_data["stderr_tail"] = err
                run_data["fio_time_logs"] = self._refresh_fio_time_logs()
                run_data["fio_time_log_summaries"] = [
                    summarize_fio_time_log(path) for path in run_data["fio_time_logs"]
                ]
                run_data.update(fio_json_metrics(fio_out))
                runs = self.collected_data.setdefault("runs", [])
                if not isinstance(runs, list):
                    raise RuntimeError("fio collected_data.runs is not a list")
                runs.append(run_data)
                self._fio_proc = None
                time.sleep(self.cooldown_sec)
        finally:
            self._stop_loader()
            self.end_time = time.time()

    def end(self) -> None:
        interrupt_process(self._fio_proc)
        self._fio_proc = None
        self._stop_loader()

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        if self.io_ext_d2fq:
            clear_d2fq_setup(self.target_device)
        if self._null_blk_config and self._remove_null_blk_on_teardown:
            run_sudo_shell("rmmod null_blk 2>/dev/null || true", check=False)
        cleanup_cgroup(self.io_ext_cgroup_path)
        for cg_path in self._subjob_cgroup_paths:
            cleanup_cgroup(cg_path)

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
        path = os.path.join(self.output_dir, f"fio_{self.scene_id}_{int(time.time())}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.collected_data, f, indent=2, ensure_ascii=False, sort_keys=True)
        return [
            path,
            *[fio_path for fio_path in self._fio_json_paths if os.path.isfile(fio_path)],
            *[stderr_path for stderr_path in self._fio_stderr_paths if os.path.isfile(stderr_path)],
            *[job_path for job_path in self._fio_job_file_paths if os.path.isfile(job_path)],
            *[log_path for log_path in self._refresh_fio_time_logs() if os.path.isfile(log_path)],
        ]

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.start_time = self.collected_data.get("start_time")
        self.end_time = self.collected_data.get("end_time")
        duration = self.collected_data.get("duration_wall")
        self.duration = float(duration) if duration is not None else 0.0

    def score(self) -> float:
        raw_runs = self.collected_data.get("runs")
        runs = raw_runs if isinstance(raw_runs, list) else []
        bandwidths = [
            float(value)
            for run in runs
            if isinstance(run, Mapping)
            for value in [run.get("agg_bw_mbps")]
            if isinstance(value, (int, float))
        ]
        if not bandwidths:
            logger.warning("FIO scene has no agg_bw_mbps; returning zero score")
            return 0.0
        mean_bw = sum(bandwidths) / len(bandwidths)
        duration = self.collected_data.get("duration_wall")
        self.duration = float(duration) if isinstance(duration, (int, float)) else 0.0
        scorer = DataScorer(
            data=[mean_bw, mean_bw],
            threshold=500.0,
            positive=True,
            value_exponent=0.5,
        )
        score = scorer.calculate_score()
        logger.info("FIO aggregate bandwidth %.2f MB/s, score %.2f", mean_bw, score)
        return float(score)
