"""Self-contained io-ext YCSB/RocksDB scene."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from vm.scene.base_scene import Scene
from vm.scene.utils import DataScorer

from .common import (
    BASH_BIN,
    DEFAULT_ROOT_CGROUP_PATH,
    SUDO_BIN,
    cleanup_cgroup,
    expand_path,
    interrupt_process,
    loader_log_paths,
    popen_in_own_session,
    read_cgroup_memory_snapshot,
    run_sudo_shell,
    setup_cgroup_v2,
    setup_cgroup_io_controls,
    setup_iocost_at_root,
    set_scheduler,
    start_loader_chain,
    stop_loader_chain,
    validate_block_device_name,
    wrap_command_in_cgroup,
)


logger = logging.getLogger("io_ext_ycsb_rocksdb")


DEFAULT_SPDK_HOME_CANDIDATES = [
    "$SPDK_HOME",
    "/home/anon/ioext-bench/temp/spdk",
    "/home/anon/spdk",
    "/opt/spdk",
]


class IoExtYcsbRocksdbScene(Scene):
    """Run YCSB/RocksDB inside an io-ext cgroup and loader chain."""

    WORKLOAD_TYPE_MAP: Dict[str, str] = {
        "a": "workloads/workloada",
        "b": "workloads/workloadb",
        "c": "workloads/workloadc",
        "d": "workloads/workloadd",
        "e": "workloads/workloade",
        "f": "workloads/workloadf",
    }

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        db = parameters.get("database") or {}
        self.db_type = db.get("db_type", "rocksdb")
        self.data_dir = expand_path(db.get("data_dir"))
        self.ycsb_dir = expand_path(db.get("ycsb_dir"))
        self.use_direct_io = _to_bool(db.get("use_direct_io", False))
        self.storage_backend = _normalize_storage_backend(db.get("storage_backend", db.get("storage_mode", "kernel_block")))
        options_file = db.get("options_file") or db.get("optionsfile") or db.get("rocksdb_options_file")
        self.rocksdb_options_file = expand_path(str(options_file)) if options_file else None
        self._rocksdb_options_file_for_run: Optional[str] = None
        try:
            self.write_buffer_size = int(db.get("write_buffer_size", 4194304))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"database.write_buffer_size must be an integer byte count: {db.get('write_buffer_size')!r}") from exc
        if self.write_buffer_size <= 0:
            raise ValueError("database.write_buffer_size must be positive")
        # Block cache control (RocksDB block-based table factory):
        #   None      -> no block_based_table_factory line is written; the
        #                RocksDB default cache stays enabled (rocksdbjni 6.2.2
        #                defaults to an 8 MiB LRU block cache).
        #   0         -> explicit off: block_based_table_factory={no_block_cache=true}
        #   >0        -> explicit on: block_based_table_factory={block_cache=<bytes>}
        # Verified against the rocksdbjni 6.2.2 option parsers (the YCSB runtime
        # bundles rocksdbjni-6.2.2): inside block_based_table_factory={...},
        # block_cache=<size> parses via NewLRUCache(ParseSizeT(value)) and
        # no_block_cache is a registered boolean option.
        raw_block_cache = db.get("block_cache_size")
        if raw_block_cache is None or (isinstance(raw_block_cache, str) and not raw_block_cache.strip()):
            self.block_cache_size: Optional[int] = None
        else:
            try:
                self.block_cache_size = int(raw_block_cache)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"database.block_cache_size must be a non-negative integer byte count or null: {raw_block_cache!r}"
                ) from exc
            if self.block_cache_size < 0:
                raise ValueError(f"database.block_cache_size must be >= 0 or null, got {self.block_cache_size}")
        # Fail-fast guard (verified 2026-10-31 against the exact bundled jar):
        # the YCSB runtime's rocksdbjni native library (rocksdbjni-6.2.2.jar,
        # build dated 2019-08-03) CANNOT carry table options in an options
        # file -- inline `block_based_table_factory={...}` values are rejected
        # at parse ("unable to parse the specified CF option
        # block_based_table_factory", even for plain keys like block_size), and
        # a [TableOptions/:rocksdb.BlockBasedTable] section parses but
        # SEGFAULTS the JVM at DB open. A legacy ini (DBOptions + CFOptions
        # scalars only, the historical direct-I/O shape) parses fine, and the
        # default block cache measures 8 MiB LRU via the
        # rocksdb.block-cache-capacity property. So block_cache_size only
        # becomes usable after upgrading the YCSB rocksdbjni jar to an
        # official >= 6.2.2 build, which requires force_block_cache_ini: true
        # as an explicit acknowledgment.
        self.force_block_cache_ini = _to_bool(db.get("force_block_cache_ini", False))
        if self.block_cache_size is not None and not self.force_block_cache_ini:
            raise ValueError(
                "database.block_cache_size is configured, but the bundled YCSB rocksdbjni runtime "
                "cannot carry table/cache options in an options file (inline "
                "block_based_table_factory={...} rejected; [TableOptions] section segfaults at open; "
                "verified 2026-10-31 against the bundled rocksdbjni-6.2.2.jar). Leave block_cache_size "
                "null (RocksDB default 8 MiB LRU block cache) or upgrade the YCSB rocksdbjni jar and "
                "set database.force_block_cache_ini: true."
            )
        if not self.data_dir or not self.ycsb_dir:
            raise ValueError("database.data_dir and database.ycsb_dir are required")

        workload = parameters.get("ycsb_workload") or {}
        workload_type = workload.get("workload_type") or workload.get("workload_category")
        if workload_type:
            if workload_type not in self.WORKLOAD_TYPE_MAP:
                raise ValueError(
                    f"Invalid workload_type '{workload_type}'; "
                    f"valid types: {sorted(self.WORKLOAD_TYPE_MAP.keys())}"
                )
            self.workload_file = self.WORKLOAD_TYPE_MAP[workload_type]
        else:
            self.workload_file = os.path.expanduser(os.path.expandvars(str(workload.get("workload_file") or "")))
        self.recordcount = int(workload.get("recordcount", 0))
        self.operationcount = int(workload.get("operationcount", 0))
        self.ycsb_threads = int(workload.get("threads", 1))
        # Offered ops/sec rate passed to YCSB as -target (run phase only).
        raw_target = workload.get("target")
        if raw_target is None or (isinstance(raw_target, str) and not raw_target.strip()):
            self.ycsb_target: Optional[int] = None
        else:
            try:
                self.ycsb_target = int(raw_target)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"ycsb_workload.target must be an integer ops/sec rate or null: {raw_target!r}") from exc
            if self.ycsb_target <= 0:
                raise ValueError(f"ycsb_workload.target must be positive, got {self.ycsb_target}")
        self.ycsb_properties: Dict[str, Any] = dict(workload.get("properties") or {})
        self.jvm_args = _normalize_jvm_args(workload.get("jvm_args"))
        self.process_environment = dict(db.get("environment") or {})
        if not self.workload_file or self.recordcount <= 0 or self.operationcount <= 0:
            raise ValueError("ycsb_workload requires workload_file, recordcount, and operationcount")

        target_device = parameters.get("target_device")
        if not target_device:
            raise ValueError("parameters.target_device is required, e.g. nvme0n1")
        self.target_device = validate_block_device_name(str(target_device))

        io_ext = parameters.get("io_ext") or {}
        self.io_ext_cfg: Dict[str, Any] = dict(io_ext)
        self.scheduler = io_ext.get("scheduler", parameters.get("scheduler"))

        system_cfg = parameters.get("system") or {}
        init_cfg = parameters.get("init") or {}
        self.mem_limit_mb = int(system_cfg.get("mem_limit_mb", 0))
        self.memory_swap_max = _format_memory_limit_value(
            system_cfg.get("memory_swap_max_mb", system_cfg.get("mem_swap_max_mb"))
        )
        self.require_swap_activity = _to_bool(system_cfg.get("require_swap_activity", False))
        self.ensure_swapfile = _to_bool(system_cfg.get("ensure_swapfile", False))
        self.swapfile_size_mb = int(system_cfg.get("swapfile_size_mb", 1024))
        default_swapfile = os.path.join(os.path.dirname(self.data_dir or "/tmp"), "ioext_swapfile")
        self.swapfile_path = expand_path(system_cfg.get("swapfile_path") or default_swapfile)
        self.auto_format_disk = _to_bool(system_cfg.get("auto_format_disk", False))
        self.format_device = _to_bool(system_cfg.get("format_device", init_cfg.get("format_device", False)))
        self.skip_disk_setup = _to_bool(system_cfg.get("skip_disk_setup", False))
        self.mount_options = str(system_cfg.get("mount_options", "noatime,nodiratime") or "").strip()
        self.cgroup_path = expand_path(system_cfg.get("cgroup_path") or "/sys/fs/cgroup/io_ext_arena")

        cgroup_cfg = io_ext.get("cgroup") or {}
        self.io_ext_cgroup_path = expand_path(cgroup_cfg.get("path") or io_ext.get("cgroup_path") or self.cgroup_path)
        self.io_ext_cgroup_controllers = list(cgroup_cfg.get("controllers") or ["io"])
        self.io_ext_cgroup_io_weight = cgroup_cfg.get("io_weight", cgroup_cfg.get("io.weight"))
        self.io_ext_cgroup_io_max = cgroup_cfg.get("io_max", cgroup_cfg.get("io.max"))
        self.io_ext_cgroup_io_latency = cgroup_cfg.get("io_latency", cgroup_cfg.get("io.latency"))
        self.io_ext_cgroup_io_bfq_weight = cgroup_cfg.get("io_bfq_weight", cgroup_cfg.get("io.bfq.weight"))
        io_cost_cfg = cgroup_cfg.get("io_cost", cgroup_cfg.get("io.cost")) or {}
        self.io_ext_iocost_model = io_cost_cfg.get("model")
        self.io_ext_iocost_qos = io_cost_cfg.get("qos")
        self.io_ext_iocost_root_path = expand_path(
            (io_ext.get("root_cgroup") or {}).get("path") if isinstance(io_ext.get("root_cgroup"), dict)
            else io_ext.get("root_cgroup_path")
        ) or DEFAULT_ROOT_CGROUP_PATH
        if (self.io_ext_cgroup_io_weight is not None or self.io_ext_cgroup_io_max is not None) and not self.io_ext_cgroup_path:
            raise ValueError("io_ext.cgroup.path is required when io_weight or io_max is configured")
        if self.io_ext_cgroup_io_weight is not None or self.io_ext_cgroup_io_max is not None:
            enabled_controllers = {str(ctrl).strip().lstrip("+") for ctrl in self.io_ext_cgroup_controllers}
            if "io" not in enabled_controllers:
                self.io_ext_cgroup_controllers.append("io")
        self.io_ext_cgroup_controls: Dict[str, Any] = {}
        if self.mem_limit_mb > 0 and self.io_ext_cgroup_path and self.cgroup_path:
            memory_cgroup = os.path.normpath(self.cgroup_path)
            io_cgroup = os.path.normpath(self.io_ext_cgroup_path)
            if memory_cgroup != io_cgroup:
                raise ValueError(
                    "YCSB memory limiting and io-ext scheduling must use the same cgroup path; "
                    f"got system.cgroup_path={self.cgroup_path!r} and io_ext.cgroup.path={self.io_ext_cgroup_path!r}"
                )

        self.output_dir = expand_path(parameters.get("output_dir")) or "./test_results"
        self.duration = int(parameters.get("duration", 7200))
        self.drop_caches_before_run = _to_bool(parameters.get("drop_caches_before_run", True))

        spdk_cfg = dict(db.get("spdk") or parameters.get("spdk") or {})
        self.spdk_cfg = spdk_cfg
        self.spdk_home_candidates = list(spdk_cfg.get("home_candidates") or DEFAULT_SPDK_HOME_CANDIDATES)
        self.spdk_require_real = _to_bool(spdk_cfg.get("require_real_spdk", self.storage_backend == "spdk_real"))
        self.spdk_allow_device_bind = _to_bool(spdk_cfg.get("allow_device_bind", False))
        self.spdk_run_setup_status = _to_bool(spdk_cfg.get("run_setup_status", True))
        self.spdk_command_timeout_sec = float(spdk_cfg.get("command_timeout_sec", 12.0))

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self._loader_procs: list[subprocess.Popen[str]] = []
        self._ycsb_proc: Optional[subprocess.Popen[str]] = None
        self.disk_setup_info: Dict[str, Any] = {}
        self.swap_setup_info: Dict[str, Any] = {}

    def setup(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        ycsb_bin = os.path.join(self.ycsb_dir, "bin", "ycsb")
        if not os.path.isfile(ycsb_bin):
            raise FileNotFoundError(f"YCSB executable not found: {ycsb_bin}")
        if self.storage_backend == "spdk_real":
            raise RuntimeError(
                "database.storage_backend=spdk_real requires a real SPDK-capable RocksDB Env/JNI build; "
                "the bundled vanilla YCSB RocksDB binding cannot produce SPDK I/O, so this mode fails closed"
            )

        mount_point = os.path.dirname(self.data_dir)
        if self.skip_disk_setup:
            # RQ5 co-location: the campaign runner pre-mounts the shared data
            # disk (and local smoke runs on an already-mounted filesystem such
            # as /var/tmp), so the scene must not format or mount anything.
            logger.info("system.skip_disk_setup=true: skipping test disk setup for %s", self.target_device)
        else:
            self._setup_test_disk(self.target_device, mount_point, self.auto_format_disk, self.format_device)

        if self.ensure_swapfile:
            self.swap_setup_info = self._ensure_swapfile()

        if self.mem_limit_mb > 0:
            self._setup_memory_cgroup(self.mem_limit_mb)
        if self.io_ext_cgroup_path:
            setup_cgroup_v2(self.io_ext_cgroup_path, self.io_ext_cgroup_controllers)

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        self._apply_cgroup_io_controls()
        self._rocksdb_options_file_for_run = self._prepare_rocksdb_options_file()
        self.collected_data = {
            "target_device": self.target_device,
            "scheduler": self.scheduler,
            "storage_backend": self.storage_backend,
            "effective_storage_backend": self._effective_storage_backend(),
            "spdk_io_verified": False,
            "data_dir": self.data_dir,
            "ycsb_dir": self.ycsb_dir,
            "io_ext_cgroup_path": self.io_ext_cgroup_path,
            "io_ext_cgroup_controls": self.io_ext_cgroup_controls,
            "use_direct_io": self.use_direct_io,
            "block_cache_size": self.block_cache_size,
            "rocksdb_options_file": self._rocksdb_options_file_for_run,
            "ycsb_properties": dict(self.ycsb_properties),
            "ycsb_target": self.ycsb_target,
            "jvm_args": self.jvm_args,
            "process_environment": self.process_environment,
            "drop_caches_before_run": self.drop_caches_before_run,
            "auto_format_disk": self.auto_format_disk,
            "format_device": self.format_device,
            "skip_disk_setup": self.skip_disk_setup,
            "disk_setup": dict(self.disk_setup_info),
            "memory_limit_mb": self.mem_limit_mb,
            "memory_swap_max": self.memory_swap_max,
            "require_swap_activity": self.require_swap_activity,
            "swap_setup": dict(self.swap_setup_info),
            "logical_data_estimate": self._estimate_logical_data_bytes(),
        }
        self.start_time = None
        self.end_time = None

    def _setup_test_disk(self, device_name: str, mount_point: str, auto_format: bool, force_format: bool) -> None:
        device_path = f"/dev/{validate_block_device_name(device_name)}"
        if not os.path.exists(device_path):
            raise FileNotFoundError(f"block device does not exist: {device_path}")

        res = subprocess.run([SUDO_BIN, "blkid", device_path], capture_output=True, text=True, check=False)
        fs_before = _blkid_type(res.stdout)
        formatted = False
        if force_format:
            logger.info("force formatting %s as ext4", device_path)
            self._format_ext4(device_path, mount_point)
            formatted = True
        elif fs_before != "ext4":
            if auto_format:
                logger.info("formatting %s as ext4", device_path)
                self._format_ext4(device_path, mount_point)
                formatted = True
            else:
                raise RuntimeError(f"{device_path} is not ext4; format it or set system.auto_format_disk: true")

        res_after = subprocess.run([SUDO_BIN, "blkid", device_path], capture_output=True, text=True, check=False)
        fs_after = _blkid_type(res_after.stdout)

        os.makedirs(mount_point, exist_ok=True)
        check = subprocess.run(["mountpoint", "-q", mount_point], capture_output=True, text=True)
        if check.returncode != 0:
            mount_cmd = [SUDO_BIN, "mount"]
            if self.mount_options:
                mount_cmd.extend(["-o", self.mount_options])
            mount_cmd.extend([device_path, mount_point])
            subprocess.run(mount_cmd, check=True)
            subprocess.run([SUDO_BIN, "chmod", "777", mount_point], check=True)
        self.disk_setup_info = {
            "device_path": device_path,
            "mount_point": mount_point,
            "filesystem_before": fs_before,
            "filesystem_after": fs_after,
            "formatted": formatted,
            "force_format": force_format,
            "auto_format": auto_format,
            "mount_options": self.mount_options,
        }

    def _format_ext4(self, device_path: str, mount_point: str) -> None:
        q_device = shlex.quote(device_path)
        q_mount = shlex.quote(mount_point)
        q_swapfile = shlex.quote(self.swapfile_path) if self.swapfile_path else ""
        swapoff_line = f"swapoff {q_swapfile} 2>/dev/null || true" if q_swapfile else "true"
        script = "\n".join(
            [
                "set -e",
                swapoff_line,
                f"if mountpoint -q {q_mount}; then umount {q_mount}; fi",
                f"while read -r target; do [ -n \"$target\" ] && umount \"$target\"; done < <(findmnt -rn -S {q_device} -o TARGET || true)",
                f"mkfs.ext4 -F {q_device}",
            ]
        )
        result = run_sudo_shell(script, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"failed to format {device_path} as ext4: {(result.stderr or result.stdout).strip()}")

    def _ensure_swapfile(self) -> Dict[str, Any]:
        if not self.swapfile_path or self.swapfile_size_mb <= 0:
            raise ValueError("system.ensure_swapfile requires swapfile_path and positive swapfile_size_mb")
        q_path = shlex.quote(self.swapfile_path)
        q_parent = shlex.quote(os.path.dirname(self.swapfile_path))
        size_mb = int(self.swapfile_size_mb)
        script = "\n".join(
            [
                "set -e",
                "created=0",
                "before=$(swapon --show=NAME,SIZE,USED --bytes --noheadings 2>/dev/null || true)",
                "if [ -z \"$before\" ]; then",
                f"  mkdir -p {q_parent}",
                f"  swapoff {q_path} 2>/dev/null || true",
                f"  rm -f {q_path}",
                f"  fallocate -l {size_mb}M {q_path} || dd if=/dev/zero of={q_path} bs=1M count={size_mb} status=none",
                f"  chmod 600 {q_path}",
                f"  mkswap -f {q_path} >/dev/null",
                f"  swapon {q_path}",
                "  created=1",
                "fi",
                "printf '%s\n' '---created---'",
                "printf '%s\n' \"$created\"",
                "printf '%s\n' '---before---'",
                "printf '%s\n' \"$before\"",
                "printf '%s\n' '---after---'",
                "swapon --show=NAME,SIZE,USED --bytes --noheadings 2>/dev/null || true",
            ]
        )
        result = run_sudo_shell(script, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"failed to ensure swapfile {self.swapfile_path}: {(result.stderr or result.stdout).strip()}")
        return {
            "ensure_swapfile": True,
            "created_swapfile": _swapfile_created(result.stdout),
            "swapfile_path": self.swapfile_path,
            "swapfile_size_mb": size_mb,
            "swapon_output": result.stdout,
        }

    def _teardown_swapfile(self) -> None:
        if not self.swap_setup_info.get("created_swapfile") or not self.swapfile_path:
            return
        q_path = shlex.quote(self.swapfile_path)
        run_sudo_shell(f"swapoff {q_path} 2>/dev/null || true\nrm -f {q_path} 2>/dev/null || true", check=False)

    def _prepare_rocksdb_options_file(self) -> Optional[str]:
        """Generate the RocksDB options ini passed via rocksdb.optionsfile.

        Generated when either (a) use_direct_io is set (O_DIRECT DBOptions)
        or (b) block_cache_size is configured (explicit block cache control
        on the buffered path -- without this the options file would never be
        generated when use_direct_io=false, leaving the block cache at the
        rocksdbjni default and un-attributable).

        Syntax verified against the rocksdbjni 6.2.2 parsers (the YCSB
        runtime bundles rocksdbjni-6.2.2):
          - block_based_table_factory={...} is parsed by
            GetBlockBasedTableOptionsFromString (options_helper.cc);
          - inside it, ``block_cache=<size>`` is handled by the
            block_based_table_factory.cc cache branch:
            ``NewLRUCache(ParseSizeT(value))`` for a bare size, so
            ``block_cache=33554432`` is a 32 MiB LRU block cache;
          - ``no_block_cache=true`` is a registered boolean option
            (block_based_table_factory.h block_based_table_type_info) and
            BlockBasedTableFactory::BlockBasedTableFactory resets
            block_cache when it is set, so capacity 0 is expressed as
            no_block_cache=true (cleaner than block_cache=0, which would
            create a zero-capacity LRU object instead of disabling the
            cache). block_cache_compressed is deliberately NOT written:
            it defaults to nullptr in 6.2.2 (no compressed block cache).
        """
        if self.rocksdb_options_file:
            return self.rocksdb_options_file
        if not self.use_direct_io and self.block_cache_size is None:
            return None
        if self.use_direct_io:
            path = os.path.join(self.output_dir, f"{self.scene_id}_rocksdb_direct_io_options.ini")
        else:
            path = os.path.join(self.output_dir, f"{self.scene_id}_rocksdb_buffered_options.ini")
        buffer_size = self.write_buffer_size
        if self.block_cache_size is None:
            bbtf_line = None
        elif self.block_cache_size == 0:
            bbtf_line = "  block_based_table_factory={no_block_cache=true}"
        else:
            bbtf_line = f"  block_based_table_factory={{block_cache={self.block_cache_size}}}"
        db_options_lines = [
            "  create_if_missing=true",
            "  create_missing_column_families=true",
        ]
        if self.use_direct_io:
            db_options_lines.extend(
                [
                    "  use_direct_reads=true",
                    "  use_direct_io_for_flush_and_compaction=true",
                ]
            )
        db_options_lines.extend(
            [
                "  allow_mmap_reads=false",
                "  allow_mmap_writes=false",
            ]
        )
        cf_lines = [f"  write_buffer_size={buffer_size}"]
        if bbtf_line:
            cf_lines.append(bbtf_line)
        content = "\n".join(
            [
                "[Version]",
                "  rocksdb_version=6.2.2",
                "  options_file_version=1.1",
                "",
                "[DBOptions]",
                *db_options_lines,
                "",
            ]
            + [f"[CFOptions \"default\"]", *cf_lines, "", f"[CFOptions \"usertable\"]", *cf_lines, ""]
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _setup_memory_cgroup(self, mem_limit_mb: int) -> None:
        if not self.cgroup_path:
            return
        setup_cgroup_v2(self.cgroup_path, ("memory", "io"))
        limit_bytes = mem_limit_mb * 1024 * 1024
        cg_path = shlex.quote(self.cgroup_path)
        script = "\n".join(
            [
                "set -e",
                f"test -f {cg_path}/memory.max",
                f"printf '%s\n' {limit_bytes} > {cg_path}/memory.max",
                *self._memory_swap_max_script_lines(cg_path),
            ]
        )
        result = run_sudo_shell(script, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"failed to configure memory cgroup {self.cgroup_path}: {(result.stderr or result.stdout).strip()}")

    def _memory_swap_max_script_lines(self, cg_path: str) -> list[str]:
        if self.memory_swap_max is None:
            return []
        return [
            f"test -f {cg_path}/memory.swap.max",
            f"printf '%s\n' {shlex.quote(str(self.memory_swap_max))} > {cg_path}/memory.swap.max",
        ]

    def _teardown_memory_cgroup(self) -> None:
        if not self.cgroup_path:
            return
        run_sudo_shell(f"printf '%s\n' {os.getpid()} > /sys/fs/cgroup/cgroup.procs 2>/dev/null || true", check=False)
        cleanup_cgroup(self.cgroup_path)

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
            self.io_ext_cgroup_io_latency,
            None,  # io_prio_class: not exposed by this scene
            self.io_ext_cgroup_io_bfq_weight,
        )
        # io.cost (model + qos) lives at the cgroup root, not on the tenant
        # cgroup; same mechanism as the fio scene (setup_iocost_at_root).
        if self.io_ext_iocost_model is not None or self.io_ext_iocost_qos is not None:
            iocost_result = setup_iocost_at_root(
                f"/dev/{self.target_device}",
                self.io_ext_iocost_model,
                self.io_ext_iocost_qos,
                root_cgroup_path=self.io_ext_iocost_root_path,
            )
            if iocost_result:
                self.io_ext_cgroup_controls["io_cost"] = iocost_result

    def _start_loader(self) -> None:
        self._loader_procs = start_loader_chain(self.parameters, self.output_dir, self.io_ext_cgroup_path)

    def _stop_loader(self) -> None:
        if self._loader_procs:
            self.collected_data.setdefault("loader_logs", loader_log_paths(self._loader_procs))
        stop_loader_chain(self._loader_procs)
        self._loader_procs = []

    def _run_ycsb(self, phase: str, out_file: str) -> int:
        workload_path = os.path.join(self.ycsb_dir, self.workload_file)
        if not os.path.isfile(workload_path):
            raise FileNotFoundError(f"workload file does not exist: {workload_path}")

        ycsb_bin = os.path.join(self.ycsb_dir, "bin", "ycsb")
        cmd = _launcher_command(ycsb_bin)
        if self.jvm_args:
            cmd.extend(["-jvm-args", self.jvm_args])
        cmd.extend([
            phase,
            self.db_type,
            "-s",
            "-P",
            workload_path,
            "-p",
            f"rocksdb.dir={self.data_dir}",
        ])
        if self._rocksdb_options_file_for_run:
            cmd.extend(["-p", f"rocksdb.optionsfile={self._rocksdb_options_file_for_run}"])
        cmd.extend(
            [
                "-p",
                f"recordcount={self.recordcount}",
                "-p",
                f"operationcount={self.operationcount}",
                "-threads",
                str(self.ycsb_threads),
            ]
        )
        if self.ycsb_target is not None and phase == "run":
            # -target caps the offered ops/sec (RQ5 fixed-rate LC tenant).
            # Like maxexecutiontime below, it must stay out of the load phase:
            # a rate-limited load would drag out keyspace population and skew
            # every run-phase measurement.  Load populates at full speed;
            # only the run phase is rate-bounded.
            cmd.extend(["-target", str(self.ycsb_target)])
        for key in sorted(self.ycsb_properties):
            if phase == "load" and _is_load_excluded_property(key):
                # YCSB honors maxexecutiontime in the load phase too and kills
                # inserts mid-way, leaving a partially populated keyspace that
                # turns the run phase into a NOT_FOUND storm.  Load must fully
                # insert recordcount; only the run phase is time-bounded.
                logger.info(
                    "skipping YCSB property %s=%s during load phase",
                    key,
                    self.ycsb_properties[key],
                )
                continue
            cmd.extend(["-p", f"{key}={self.ycsb_properties[key]}"])
        wrapped = wrap_command_in_cgroup(cmd, self.io_ext_cgroup_path, env=self.process_environment)
        final_cmd = list(wrapped) if not isinstance(wrapped, str) else [BASH_BIN, "-lc", wrapped]

        logger.info("YCSB %s output -> %s", phase, out_file)
        with open(out_file, "w", encoding="utf-8") as out_f:
            self._ycsb_proc = popen_in_own_session(
                final_cmd,
                stdout=out_f,
                stderr=subprocess.STDOUT,
                cwd=self.ycsb_dir,
                text=True,
            )
            while self._ycsb_proc.poll() is None:
                if self.should_stop():
                    interrupt_process(self._ycsb_proc, timeout_sec=15)
                    break
                time.sleep(0.3)
            rc = self._ycsb_proc.returncode if self._ycsb_proc.returncode is not None else self._ycsb_proc.wait()
        self._ycsb_proc = None
        return int(rc or 0)

    def _run(self) -> None:
        self.start_time = time.time()
        load_out = os.path.join(self.output_dir, f"{self.scene_id}_load.txt")
        run_out = os.path.join(self.output_dir, f"{self.scene_id}_run.txt")

        self._record_storage_backend_evidence()

        if os.path.exists(self.data_dir):
            shutil.rmtree(self.data_dir)
        os.makedirs(self.data_dir, exist_ok=True)

        self._record_data_footprint("before_load")
        self._record_io_snapshot("before_load")

        if self.drop_caches_before_run:
            self._drop_caches()
        self._set_scheduler()
        self._start_loader()

        try:
            self._record_memory_snapshot("before_load")
            rc_load = self._run_ycsb("load", load_out)
            self._record_memory_snapshot("after_load")
            self._record_data_footprint("after_load")
            self._record_io_snapshot("after_load")
            self.collected_data["load_returncode"] = rc_load
            self.collected_data["load_txt"] = load_out
            if rc_load != 0 and not self.should_stop():
                raise RuntimeError(f"YCSB load failed returncode={rc_load} output={load_out}")
            if _ycsb_output_has_failure(load_out) and not self.should_stop():
                raise RuntimeError(f"YCSB load output contains RocksDB/YCSB exception markers: {load_out}")
            if self.should_stop():
                return

            if self.drop_caches_before_run:
                self._drop_caches()
                time.sleep(3)
            self._record_memory_snapshot("before_run")
            self._record_data_footprint("before_run")
            self._record_io_snapshot("before_run")

            rc_run = self._run_ycsb("run", run_out)
            self._record_memory_snapshot("after_run")
            self._record_data_footprint("after_run")
            self._record_io_snapshot("after_run")
            self.collected_data["run_returncode"] = rc_run
            self.collected_data["run_txt"] = run_out

            if rc_run != 0 and not self.should_stop():
                raise RuntimeError(f"YCSB run failed returncode={rc_run} output={run_out}")
            if _ycsb_output_has_failure(run_out) and not self.should_stop():
                raise RuntimeError(f"YCSB run output contains RocksDB/YCSB exception markers: {run_out}")

            throughput = _parse_ycsb_throughput(run_out)
            self.collected_data["throughput_ops_per_sec"] = throughput

            ycsb_stats = _parse_ycsb_stats(run_out)
            self.collected_data["ycsb_stats"] = ycsb_stats
            ycsb_latency_summary = _extract_ycsb_latency_summary(ycsb_stats)
            self.collected_data["ycsb_latency_summary"] = ycsb_latency_summary

            self.collected_data["rocksdb_direct_options"] = self._collect_rocksdb_direct_options()
            self.collected_data["data_footprint_summary"] = self._summarize_data_footprint()
            self.collected_data["io_stat_summary"] = self._summarize_io_stat()
            self.collected_data["memory_cgroup_summary"] = self._summarize_memory_cgroup()
            if (throughput is None or throughput <= 0) and not self.should_stop():
                raise RuntimeError(f"YCSB run produced invalid throughput {throughput!r}: {run_out}")
            if self.require_swap_activity and not self.collected_data["memory_cgroup_summary"].get("swap_observed"):
                raise RuntimeError("YCSB swap-pressure run did not observe cgroup swap activity")
        finally:
            self._stop_loader()
            self.end_time = time.time()

    def _collect_rocksdb_direct_options(self) -> Dict[str, Any]:
        log_path = os.path.join(self.data_dir, "LOG")
        lines = []
        if os.path.isfile(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = [
                        line.strip()
                        for line in f
                        if "use_direct" in line or "allow_mmap" in line or "options file" in line.lower()
                    ]
            except OSError:
                lines = []
        return {
            "log_path": log_path,
            "direct_io_requested": self.use_direct_io,
            "options_file": self._rocksdb_options_file_for_run,
            "log_direct_lines": lines[:40],
        }

    def _record_storage_backend_evidence(self) -> None:
        if self.storage_backend == "kernel_block":
            self.collected_data["spdk_io_verified"] = False
            return
        readiness = self._probe_spdk_readiness()
        self.collected_data["spdk_readiness"] = readiness
        self.collected_data["spdk_io_verified"] = False
        self.collected_data["effective_storage_backend"] = self._effective_storage_backend()
        if self.storage_backend == "spdk_real":
            raise RuntimeError(
                "database.storage_backend=spdk_real is not available with the bundled vanilla YCSB RocksDB binding; "
                f"SPDK readiness={readiness}"
            )

    def _effective_storage_backend(self) -> str:
        if self.storage_backend == "spdk_probe_only":
            return "kernel_block_with_spdk_probe_only"
        return self.storage_backend

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
            "allow_device_bind": self.spdk_allow_device_bind,
            "require_real_spdk": self.spdk_require_real,
        }
        readiness["spdk_available"] = bool(
            readiness["setup_script_exists"]
            and (readiness["identify_bin_exists"] or readiness["bdevperf_bin_exists"])
        )
        if readiness["setup_script_exists"] and self.spdk_run_setup_status:
            readiness["setup_status"] = _run_sudo_command(
                [setup_script, "status"], timeout_sec=self.spdk_command_timeout_sec
            )
        readiness["safe_bind_skipped_reason"] = self._spdk_skip_reason(readiness)
        return readiness

    def _find_spdk_home(self) -> str:
        for candidate in self.spdk_home_candidates:
            expanded = _expand_candidate(candidate)
            if expanded and os.path.isdir(expanded):
                return expanded
        return ""

    def _spdk_skip_reason(self, readiness: Dict[str, Any]) -> str:
        if self.spdk_allow_device_bind:
            return "allow_device_bind=true; YCSB smoke still refuses to bind without a real SPDK RocksDB Env"
        if not readiness.get("spdk_home"):
            return "SPDK checkout not found"
        if not readiness.get("setup_script_exists"):
            return "SPDK scripts/setup.sh not found"
        if readiness.get("hugepages", {}).get("HugePages_Total", 0) <= 0:
            return "no hugepages configured"
        if not readiness.get("vfio_pci_loaded") and not readiness.get("uio_pci_generic_loaded"):
            return "vfio-pci/uio driver unavailable"
        return "device binding disabled by default to keep VM block device intact"

    def _record_data_footprint(self, name: str) -> None:
        footprints = self.collected_data.setdefault("data_footprint_snapshots", {})
        footprints[name] = _snapshot_data_footprint(self.data_dir)

    def _record_io_snapshot(self, name: str) -> None:
        snapshots = self.collected_data.setdefault("io_stat_snapshots", {})
        snapshots[name] = _read_cgroup_io_snapshot(self.io_ext_cgroup_path)

    def _summarize_data_footprint(self) -> Dict[str, Any]:
        snapshots = self.collected_data.get("data_footprint_snapshots") or {}
        before = snapshots.get("before_load") or {}
        after = snapshots.get("after_run") or {}
        apparent_delta = int(after.get("apparent_bytes") or 0) - int(before.get("apparent_bytes") or 0)
        allocated_delta = int(after.get("allocated_bytes") or 0) - int(before.get("allocated_bytes") or 0)
        logical = self.collected_data.get("logical_data_estimate") or {}
        logical_bytes = int(logical.get("estimated_bytes") or 0) if isinstance(logical, dict) else 0
        return {
            "before_label": "before_load",
            "after_label": "after_run",
            "file_count_delta": int(after.get("file_count") or 0) - int(before.get("file_count") or 0),
            "apparent_bytes_delta": apparent_delta,
            "allocated_bytes_delta": allocated_delta,
            "logical_data_estimated_bytes": logical_bytes,
            "apparent_over_logical_ratio": _safe_ratio(apparent_delta, logical_bytes),
            "allocated_over_logical_ratio": _safe_ratio(allocated_delta, logical_bytes),
            "after_file_type_bytes": after.get("file_type_bytes", {}),
        }

    def _summarize_io_stat(self) -> Dict[str, Any]:
        snapshots = self.collected_data.get("io_stat_snapshots") or {}
        before = snapshots.get("before_load") or {}
        after = snapshots.get("after_run") or {}
        before_totals = before.get("totals") or {}
        after_totals = after.get("totals") or {}
        keys = sorted(set(before_totals) | set(after_totals))
        return {
            "before_label": "before_load",
            "after_label": "after_run",
            "exists": bool(before.get("exists") or after.get("exists")),
            "delta": {key: int(after_totals.get(key) or 0) - int(before_totals.get(key) or 0) for key in keys},
        }

    def _estimate_logical_data_bytes(self) -> Dict[str, Any]:
        fieldcount = int(self.ycsb_properties.get("fieldcount", 10))
        fieldlength = int(self.ycsb_properties.get("fieldlength", 100))
        estimated = int(self.recordcount) * fieldcount * fieldlength
        return {
            "recordcount": self.recordcount,
            "fieldcount": fieldcount,
            "fieldlength": fieldlength,
            "estimated_bytes": estimated,
            "source": "recordcount * fieldcount * fieldlength; YCSB workloada defaults are 10 fields of 100 bytes",
        }

    def _record_memory_snapshot(self, name: str) -> None:
        if self.mem_limit_mb <= 0 or not self.cgroup_path:
            return
        snapshots = self.collected_data.setdefault("memory_cgroup_snapshots", {})
        snapshots[name] = read_cgroup_memory_snapshot(self.cgroup_path)

    def _summarize_memory_cgroup(self) -> Dict[str, Any]:
        snapshots = self.collected_data.get("memory_cgroup_snapshots") or {}
        before = snapshots.get("before_load") or {}
        after = snapshots.get("after_run") or {}
        before_stat = before.get("memory_stat") or {}
        after_stat = after.get("memory_stat") or {}
        before_events = before.get("memory_events") or {}
        after_events = after.get("memory_events") or {}
        before_swap_events = before.get("memory_swap_events") or {}
        after_swap_events = after.get("memory_swap_events") or {}
        max_swap_current = max(
            int(snapshot.get("memory_swap_current") or 0)
            for snapshot in snapshots.values()
            if isinstance(snapshot, dict)
        ) if snapshots else 0
        max_memory_peak = max(
            int(snapshot.get("memory_peak") or 0)
            for snapshot in snapshots.values()
            if isinstance(snapshot, dict)
        ) if snapshots else 0
        max_swap_peak = max(
            int(snapshot.get("memory_swap_peak") or 0)
            for snapshot in snapshots.values()
            if isinstance(snapshot, dict)
        ) if snapshots else 0
        pswpout_delta = int(after_stat.get("pswpout") or 0) - int(before_stat.get("pswpout") or 0)
        swap_max_delta = int(after_swap_events.get("max") or 0) - int(before_swap_events.get("max") or 0)
        swap_fail_delta = int(after_swap_events.get("fail") or 0) - int(before_swap_events.get("fail") or 0)
        memory_max_delta = int(after_events.get("max") or 0) - int(before_events.get("max") or 0)
        oom_kill_delta = int(after_events.get("oom_kill") or 0) - int(before_events.get("oom_kill") or 0)
        return {
            "max_memory_current": max(
                int(snapshot.get("memory_current") or 0)
                for snapshot in snapshots.values()
                if isinstance(snapshot, dict)
            ) if snapshots else 0,
            "max_memory_peak": max_memory_peak,
            "max_memory_swap_current": max_swap_current,
            "max_memory_swap_peak": max_swap_peak,
            "pswpout_delta": pswpout_delta,
            "memory_events_max_delta": memory_max_delta,
            "memory_events_oom_kill_delta": oom_kill_delta,
            "memory_swap_events_max_delta": swap_max_delta,
            "memory_swap_events_fail_delta": swap_fail_delta,
            "swap_observed": max_swap_current > 0 or max_swap_peak > 0 or pswpout_delta > 0,
        }

    def end(self) -> None:
        interrupt_process(self._ycsb_proc, timeout_sec=15)
        self._ycsb_proc = None
        self._stop_loader()

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        if self.mem_limit_mb > 0:
            self._teardown_memory_cgroup()
        if self.io_ext_cgroup_path and self.io_ext_cgroup_path != self.cgroup_path:
            cleanup_cgroup(self.io_ext_cgroup_path)
        elif self.mem_limit_mb <= 0:
            cleanup_cgroup(self.io_ext_cgroup_path)
        self._teardown_swapfile()

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
        path = os.path.join(self.output_dir, f"ycsb_rocksdb_{self.scene_id}_{int(time.time())}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.collected_data, f, indent=2, ensure_ascii=False, sort_keys=True)
        result_files: List[str] = [path]
        for raw_key in ("load_txt", "run_txt"):
            raw_path = self.collected_data.get(raw_key)
            if raw_path and os.path.isfile(raw_path):
                result_files.append(raw_path)
        return result_files

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.start_time = self.collected_data.get("start_time")
        self.end_time = self.collected_data.get("end_time")
        duration = self.collected_data.get("duration_wall")
        self.duration = float(duration) if duration is not None else 0.0

    def score(self) -> float:
        throughput = self.collected_data.get("throughput_ops_per_sec")
        if throughput is None or throughput <= 0:
            logger.warning("YCSB scene has no valid throughput; returning zero score")
            return 0.0
        self.duration = float(self.collected_data.get("duration_wall") or 0.0)
        scorer = DataScorer(
            data=[float(throughput), float(throughput)],
            threshold=5000.0,
            positive=True,
            value_exponent=0.5,
        )
        score = scorer.calculate_score()
        logger.info("YCSB throughput %.2f ops/s, score %.2f", throughput, score)
        return float(score)


_THROUGHPUT_LINE = re.compile(
    r"^\[OVERALL\],\s*Throughput\(ops/sec\)\s*,\s*([0-9.eE+-]+)\s*$",
    re.MULTILINE,
)

_PYTHON_SHEBANG_RE = re.compile(r"^#!(?:/usr/bin/env\s+)?python[0-9.]*(?:\s+-[A-Za-z0-9.]+)*\s*$")

_LOAD_EXCLUDED_PROPERTIES = {"maxexecutiontime", "maxexecutiontime-seconds"}


def _is_load_excluded_property(key: Any) -> bool:
    return str(key).strip().lower() in _LOAD_EXCLUDED_PROPERTIES


def _launcher_command(ycsb_bin: str) -> List[str]:
    """Return the command prefix that invokes the YCSB launcher reliably.

    ``bin/ycsb`` ships with ``#!/usr/bin/env python``.  This scene runs YCSB
    through the cgroup wrapper (``sudo bash -lc``), whose PATH is sudo's
    secure_path; on python3-only systems (Ubuntu without ``python-is-python3``)
    a bare ``python`` does not exist there and the launcher dies with
    returncode 127 before printing anything.  For any python-shebang launcher,
    invoke it with the interpreter running this process (absolute path, always
    compatible) instead of relying on the shebang.
    """
    try:
        with open(ycsb_bin, "r", encoding="utf-8", errors="replace") as f:
            first_line = f.readline()
    except OSError:
        return [ycsb_bin]
    if _PYTHON_SHEBANG_RE.match(first_line.strip()):
        interpreter = sys.executable or "python3"
        return [interpreter, ycsb_bin]
    return [ycsb_bin]

_YCSB_METRIC_LINE = re.compile(
    r"^\s*\[([A-Za-z][A-Za-z0-9_]+?)\]\s*,\s*([A-Za-z0-9_.]+(?:\([^)]+\))?[A-Za-z0-9_.=]*)\s*,\s*(.+?)\s*$",
    re.MULTILINE,
)

_BLKID_TYPE_RE = re.compile(r'\bTYPE="([^"]+)"')


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _format_memory_limit_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped == "max":
            return stripped
        return str(int(stripped) * 1024 * 1024)
    return str(int(value) * 1024 * 1024)


def _normalize_jvm_args(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (list, tuple)):
        parts = [str(part) for part in value if str(part).strip()]
        return shlex.join(parts) if parts else None
    return str(value)


def _swapfile_created(swapon_output: str) -> bool:
    lines = [line.strip() for line in (swapon_output or "").splitlines()]
    for index, line in enumerate(lines):
        if line == "---created---" and index + 1 < len(lines):
            return lines[index + 1] == "1"
    return False


def _blkid_type(output: str) -> Optional[str]:
    match = _BLKID_TYPE_RE.search(output or "")
    return match.group(1) if match else None


def _ycsb_output_has_failure(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    return "site.ycsb.DBException" in text or "org.rocksdb.RocksDBException" in text


def _parse_ycsb_throughput(run_txt_path: str) -> Optional[float]:
    if not os.path.isfile(run_txt_path):
        return None
    try:
        with open(run_txt_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    match = _THROUGHPUT_LINE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_ycsb_stats(run_txt_path: str) -> Dict[str, Any]:
    """Parse comprehensive YCSB run output into structured metrics.

    YCSB with ``-s`` emits CSV lines like::

        [OVERALL], RunTime(ms), 60000
        [OVERALL], Throughput(ops/sec), 1666.67
        [READ], Operations, 50000
        [READ], AverageLatency(us), 850.5
        [READ], MinLatency(us), 120
        [READ], MaxLatency(us), 25000
        [READ], 95thPercentileLatency(us), 1800
        [READ], 99thPercentileLatency(us), 4500
        [READ], 99.9PercentileLatency(us), 3183
        [READ], 99.99PercentileLatency(us), 7083
        [READ], Return=OK, 49980
        [UPDATE], AverageLatency(us), 3200.2
        ...

    The ``99.9``/``99.99`` lines (no "th" suffix) are emitted only with
    ``measurementtype=hdrhistogram`` + ``hdrhistogram.percentiles=50,95,99,99.9,99.99``;
    the basic caliber stops at the built-in ``50th/95th/99th`` lines.

    Returns a dict with:
        overall: {run_time_ms, throughput_ops_per_sec}
        per_operation: {op_type: {average_latency_us, min_latency_us,
                       max_latency_us, p95_latency_us, p99_latency_us,
                       operations, return_ok, return_error, ...}}
    """
    result: Dict[str, Any] = {
        "overall": {},
        "per_operation": {},
    }
    if not os.path.isfile(run_txt_path):
        return result
    try:
        with open(run_txt_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return result

    for match in _YCSB_METRIC_LINE.finditer(text):
        section = match.group(1)
        metric = match.group(2)
        raw_value = match.group(3).strip()

        try:
            value: Union[float, str] = float(raw_value)
        except ValueError:
            value = raw_value

        if section == "OVERALL":
            result["overall"][metric] = value
        else:
            op_metrics: Dict[str, Union[float, str]] = result["per_operation"].setdefault(section, {})
            op_metrics[metric] = value

    return result


def _extract_ycsb_latency_summary(ycsb_stats: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a flat latency summary dict from parsed YCSB stats.

    Produces top-level keys like:
        latency_read_avg_us, latency_read_p99_us, latency_read_p95_us,
        latency_read_min_us, latency_read_max_us, latency_read_ops,
        latency_update_avg_us, latency_update_p99_us, ...
        latency_read_p999_us, latency_read_p9999_us, ...
        (hdrhistogram caliber only; absent when the run.txt has no
        99.9/99.99PercentileLatency lines)
        overall_runtime_ms, overall_throughput_ops_per_sec
    """
    summary: Dict[str, Any] = {}

    overall = ycsb_stats.get("overall") or {}
    for key, value in overall.items():
        flat_key = f"overall_{key}"
        summary[flat_key] = value

    per_op = ycsb_stats.get("per_operation") or {}
    latency_metric_map = {
        "AverageLatency(us)": "avg_us",
        "MinLatency(us)": "min_us",
        "MaxLatency(us)": "max_us",
        "50thPercentileLatency(us)": "p50_us",
        "95thPercentileLatency(us)": "p95_us",
        "99thPercentileLatency(us)": "p99_us",
        "99.9PercentileLatency(us)": "p999_us",
        "99.99PercentileLatency(us)": "p9999_us",
        "Operations": "ops",
        "Return=OK": "return_ok",
        "Return=ERROR": "return_error",
    }
    for op_type, metrics in per_op.items():
        op_lower = op_type.lower()
        for ycsb_key, suffix in latency_metric_map.items():
            if ycsb_key in metrics:
                summary[f"latency_{op_lower}_{suffix}"] = metrics[ycsb_key]

    return summary


def _normalize_storage_backend(value: Any) -> str:
    raw = str(value or "kernel_block").strip().lower().replace("-", "_")
    aliases = {
        "kernel": "kernel_block",
        "block": "kernel_block",
        "kernel_block": "kernel_block",
        "spdk_probe": "spdk_probe_only",
        "spdk_probe_only": "spdk_probe_only",
        "spdk_real": "spdk_real",
    }
    if raw in aliases:
        return aliases[raw]
    raise ValueError(
        "database.storage_backend must be one of kernel_block, spdk_probe_only, or spdk_real; "
        f"got {value!r}"
    )


def _snapshot_data_footprint(path: str) -> Dict[str, Any]:
    root = expand_path(path) or path
    snapshot: Dict[str, Any] = {
        "path": root,
        "exists": os.path.exists(root),
        "file_count": 0,
        "directory_count": 0,
        "apparent_bytes": 0,
        "allocated_bytes": 0,
        "file_type_bytes": {},
        "file_type_counts": {},
    }
    if not snapshot["exists"]:
        return snapshot

    file_type_bytes: Dict[str, int] = {}
    file_type_counts: Dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        snapshot["directory_count"] += len(dirnames)
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            try:
                st = os.stat(full_path)
            except OSError:
                continue
            category = _rocksdb_file_category(filename)
            size = int(st.st_size)
            allocated = int(st.st_blocks) * 512
            snapshot["file_count"] += 1
            snapshot["apparent_bytes"] += size
            snapshot["allocated_bytes"] += allocated
            file_type_bytes[category] = file_type_bytes.get(category, 0) + size
            file_type_counts[category] = file_type_counts.get(category, 0) + 1
    snapshot["file_type_bytes"] = file_type_bytes
    snapshot["file_type_counts"] = file_type_counts
    return snapshot


def _rocksdb_file_category(filename: str) -> str:
    upper = filename.upper()
    lower = filename.lower()
    if lower.endswith((".sst", ".ldb")):
        return "sst"
    if upper.startswith("MANIFEST"):
        return "manifest"
    if upper == "CURRENT":
        return "current"
    if upper == "OPTIONS" or upper.startswith("OPTIONS-"):
        return "options"
    if upper == "LOCK":
        return "lock"
    if lower.endswith(".log") or upper == "LOG" or upper.startswith("LOG."):
        return "log_or_wal"
    return Path(filename).suffix.lower().lstrip(".") or "other"


def _read_cgroup_io_snapshot(cgroup_path: Optional[str]) -> Dict[str, Any]:
    if not cgroup_path:
        return {"cgroup_path": cgroup_path, "exists": False, "error": "cgroup_path is empty"}
    cg_path = expand_path(cgroup_path) or cgroup_path
    path = os.path.join(cg_path, "io.stat")
    snapshot: Dict[str, Any] = {
        "cgroup_path": cg_path,
        "path": path,
        "exists": os.path.isfile(path),
        "timestamp": time.time(),
        "devices": {},
        "totals": {},
    }
    if not snapshot["exists"]:
        return snapshot
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError as exc:
        snapshot["error"] = str(exc)
        return snapshot
    totals: Dict[str, int] = {}
    devices: Dict[str, Dict[str, int]] = {}
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        device = parts[0]
        values: Dict[str, int] = {}
        for token in parts[1:]:
            if "=" not in token:
                continue
            key, raw_value = token.split("=", 1)
            try:
                value = int(raw_value)
            except ValueError:
                continue
            values[key] = value
            totals[key] = totals.get(key, 0) + value
        devices[device] = values
    snapshot["raw_lines"] = lines
    snapshot["devices"] = devices
    snapshot["totals"] = totals
    return snapshot


def _safe_ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return float(numerator) / float(denominator)


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


def _list_dir(path: str) -> list[str]:
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


def _run_sudo_command(argv: list[str], timeout_sec: float) -> Dict[str, Any]:
    cmd = [SUDO_BIN, *[str(part) for part in argv]]
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
