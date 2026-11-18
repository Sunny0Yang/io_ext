"""SPDK-internal bdev FIO workload scene.

This scene deliberately does *not* bind or take over any kernel block device.
It creates an SPDK JSON config for an internal bdev (``bdev_null`` by default)
and drives it through FIO's ``spdk_bdev`` ioengine.  The goal is to provide a
userspace SPDK baseline that is comparable to kernel ``null_blk`` without
touching ``/dev/nullb0`` or any NVMe PCI binding state.

It can also convert a Tencent block trace CSV into an FIO iolog and replay that
iolog through the same ``spdk_bdev`` engine.  That mode is real SPDK I/O, but it
is a block-level FIO replay approximation rather than the kernel trace_replay C
helper; configs must label it as such.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from vm.scene.base_scene import Scene
from vm.scene.utils import DataScorer

from .common import (
    SUDO_BIN,
    expand_path,
    file_tail,
    fio_json_metrics,
    interrupt_process,
    popen_in_own_session,
    write_json_stat,
)


logger = logging.getLogger("io_ext_spdk_fio")

DEFAULT_OUTPUT_DIR = "/home/anon/ioext-bench/test_results/io_ext/spdk_fio"
DEFAULT_SPDK_HOME_CANDIDATES = [
    "$SPDK_HOME",
    "/home/anon/ioext-bench/src/vm/assets/spdk/spdk",
]


class IoExtSpdkFioScene(Scene):
    """Run FIO against SPDK internal bdev_null/bdev_malloc."""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        self.output_dir = expand_path(parameters.get("output_dir")) or DEFAULT_OUTPUT_DIR
        self.job_name = str(parameters.get("job_name") or "arena_spdk_fio")
        self.fio_global: Dict[str, Any] = dict(parameters.get("fio_global") or parameters.get("fio") or {})
        if not self.fio_global.get("runtime_sec") and self.fio_global.get("runtime"):
            self.fio_global["runtime_sec"] = self.fio_global["runtime"]
        if not self.fio_global.get("runtime_sec"):
            raise ValueError("fio_global requires runtime_sec or runtime")
        for key in ("bs", "rw", "iodepth"):
            if key not in self.fio_global:
                raise ValueError(f"fio_global.{key} is required")

        self.concurrency_levels: List[int] = list(parameters.get("concurrency_levels") or [])
        if not self.concurrency_levels:
            self.concurrency_levels = [int(self.fio_global.get("numjobs", 1))]
        self.max_concurrency = max(int(level) for level in self.concurrency_levels)
        self.fio_iodepth = _int_param(self.fio_global.get("iodepth"), 1)
        self.high_concurrency_iobuf_tuning = self.max_concurrency * self.fio_iodepth >= 4096

        self.system_cfg: Dict[str, Any] = dict(parameters.get("system") or {})
        self.spdk_cfg: Dict[str, Any] = dict(parameters.get("spdk") or {})
        self.spdk_home_candidates = list(self.spdk_cfg.get("home_candidates") or DEFAULT_SPDK_HOME_CANDIDATES)
        self.require_spdk = _bool_param(self.spdk_cfg.get("require_spdk", True))
        self.use_sudo = _bool_param(self.spdk_cfg.get("use_sudo", True))
        self.require_hugepages = _bool_param(self.spdk_cfg.get("require_hugepages", True))
        self.setup_hugepages = _bool_param(self.spdk_cfg.get("setup_hugepages", True))
        self.enforce_hugepages = _bool_param(self.spdk_cfg.get("enforce_hugepages", True))
        self.allow_system_fio_fallback = _bool_param(self.spdk_cfg.get("allow_system_fio_fallback", False))
        self.hugemem_mb = _int_param(self.spdk_cfg.get("hugemem_mb"), 1024)
        self.setup_timeout_sec = _int_param(self.spdk_cfg.get("setup_timeout_sec"), 90)
        self.spdk_mem_mb = _int_param(self.spdk_cfg.get("spdk_mem_mb"), 512)
        self.bdev_type = str(self.spdk_cfg.get("bdev_type", "null")).strip().lower()
        self.bdev_name = str(self.spdk_cfg.get("bdev_name", "Null0"))
        self.iobuf_options: Dict[str, Any] = dict(self.spdk_cfg.get("iobuf_options") or {})
        self.accel_options: Dict[str, Any] = dict(self.spdk_cfg.get("accel_options") or {})
        self.bdev_options: Dict[str, Any] = dict(self.spdk_cfg.get("bdev_options") or {})
        if self.high_concurrency_iobuf_tuning:
            # 不盲目扩大全局 iobuf large pool：64 jobs 的失败根因是默认每线程
            # small/large cache 太大，而 4K randread 只需要 small buffer。降低 cache
            # 比把 hugepages/spdk_mem 推高更稳，尤其是默认 4GiB virtme-ng VM。
            _merge_missing_options(self.accel_options, {"small_cache_size": 32, "large_cache_size": 0})
            _merge_missing_options(
                self.bdev_options,
                {
                    "bdev_io_cache_size": 128,
                    "iobuf_small_cache_size": 32,
                    "iobuf_large_cache_size": 0,
                },
            )
        self.bdev_json_path = expand_path(self.spdk_cfg.get("bdev_json_path")) or os.path.join(
            self.output_dir, f"{self.scene_id}_spdk_bdev.json"
        )
        self.extra_env = {str(k): str(v) for k, v in dict(self.spdk_cfg.get("env") or {}).items()}
        self.trace_iolog_cfg: Dict[str, Any] = dict(parameters.get("trace_iolog") or self.spdk_cfg.get("trace_iolog") or {})
        self._fio_read_iolog_path: str = ""

        runtime = float(self.fio_global["runtime_sec"])
        self.cooldown_sec = float(parameters.get("cooldown_sec", 1.0))
        runs = len(self.concurrency_levels)
        self.duration = int(parameters.get("duration", runtime * runs + self.cooldown_sec * max(0, runs - 1) + 45))

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self._fio_proc: Optional[subprocess.Popen[str]] = None
        self._fio_json_paths: List[str] = []
        self._fio_stderr_paths: List[str] = []
        self._readiness: Dict[str, Any] = {}

    def setup(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.bdev_json_path), exist_ok=True)
        self._readiness = self._probe_spdk_readiness()
        if self.setup_hugepages and self._readiness.get("spdk_home") and self._needs_hugepage_setup(self._readiness):
            setup_result = self._setup_internal_bdev_hugepages(str(self._readiness["spdk_home"]))
            self._readiness = self._probe_spdk_readiness()
            self._readiness["hugepage_setup"] = setup_result

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        self._fio_json_paths = []
        self._fio_stderr_paths = []
        self.start_time = None
        self.end_time = None
        self.collected_data = {
            "scene_id": self.scene_id,
            "mode": "spdk_internal_bdev_fio",
            "spdk_readiness": self._readiness or self._probe_spdk_readiness(),
            "bdev_type": self.bdev_type,
            "bdev_name": self.bdev_name,
            "iobuf_options": self.iobuf_options,
            "accel_options": self.accel_options,
            "bdev_options": self.bdev_options,
            "bdev_json": self.bdev_json_path,
            "runs": [],
        }
        self._fio_read_iolog_path = self._prepare_trace_iolog()
        if self._fio_read_iolog_path:
            self.collected_data["mode"] = "spdk_internal_bdev_fio_iolog_replay"
            self.collected_data["spdk_trace_replay_semantics"] = "fio_read_iolog_block_level_approximation"
            self.collected_data["read_iolog"] = self._fio_read_iolog_path
        self._write_bdev_json()

    def _run(self) -> None:
        self.start_time = time.time()
        readiness = self.collected_data.get("spdk_readiness") or self._probe_spdk_readiness()
        if not readiness.get("spdk_fio_ready"):
            self.collected_data["skipped"] = True
            self.collected_data["skip_reason"] = readiness.get("skip_reason", "SPDK FIO plugin unavailable")
            self.end_time = time.time()
            if self.require_spdk:
                raise RuntimeError(str(self.collected_data["skip_reason"]))
            return

        try:
            for concurrency in self.concurrency_levels:
                if self.should_stop():
                    break
                tag = f"jobs{concurrency}"
                fio_out = os.path.join(self.output_dir, f"{self.scene_id}_{tag}_fio.json")
                self._fio_json_paths.append(fio_out)
                fio_argv = self._build_fio_argv(
                    concurrency,
                    fio_out,
                    str(readiness["fio_ioengine"]),
                    str(readiness["fio_binary"]),
                )
                full_cmd = self._build_full_cmd(fio_argv, readiness)
                stderr_path = os.path.join(self.output_dir, f"{self.scene_id}_{tag}_fio.stderr.log")
                self._fio_stderr_paths.append(stderr_path)
                logger.info("running SPDK FIO: concurrency=%s cmd=%s", concurrency, " ".join(full_cmd))
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
                    raise RuntimeError(f"SPDK fio failed returncode={rc} stderr={err[:2000]!r}")

                run_data = {
                    "concurrency": concurrency,
                    "fio_json": fio_out,
                    "returncode": rc,
                    "stderr_log": stderr_path,
                }
                if err:
                    run_data["stderr_tail"] = err[-2000:]
                run_data.update(fio_json_metrics(fio_out))
                self.collected_data["runs"].append(run_data)
                self._fio_proc = None
                time.sleep(self.cooldown_sec)
        finally:
            self.end_time = time.time()

    def end(self) -> None:
        interrupt_process(self._fio_proc)
        self._fio_proc = None
        if self.end_time is None and self.start_time is not None:
            self.end_time = time.time()

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        self.end()

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
        stat_path = write_json_stat(self.output_dir, "spdk_fio", self.scene_id, self.collected_data)
        paths = [stat_path, self.bdev_json_path]
        paths.extend(path for path in self._fio_json_paths if os.path.isfile(path))
        paths.extend(path for path in self._fio_stderr_paths if os.path.isfile(path))
        return paths

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.start_time = self.collected_data.get("start_time")
        self.end_time = self.collected_data.get("end_time")
        self.duration = float(self.collected_data.get("duration_wall") or 0.0)

    def score(self) -> float:
        runs = self.collected_data.get("runs") or []
        bandwidths = [run["agg_bw_mbps"] for run in runs if isinstance(run, dict) and run.get("agg_bw_mbps") is not None]
        if not bandwidths:
            return 0.0
        mean_bw = sum(float(value) for value in bandwidths) / len(bandwidths)
        scorer = DataScorer(data=[mean_bw, mean_bw], threshold=500.0, positive=True, value_exponent=0.5)
        return float(scorer.calculate_score())

    def _build_fio_argv(self, concurrency: int, fio_json_path: str, ioengine: str, fio_binary: str = "fio") -> List[str]:
        fio = self.fio_global
        argv = [
            fio_binary or "fio",
            f"--name={self.job_name}",
            f"--filename={self.bdev_name}",
            f"--ioengine={ioengine}",
            f"--spdk_json_conf={self.bdev_json_path}",
            "--thread=1",
            f"--direct={fio.get('direct', 1)}",
            f"--bs={fio['bs']}",
            f"--rw={fio['rw']}",
            f"--iodepth={fio['iodepth']}",
            f"--numjobs={concurrency}",
            f"--runtime={fio['runtime_sec']}",
            "--output-format=json",
            f"--output={fio_json_path}",
        ]
        if self.spdk_mem_mb > 0:
            argv.append(f"--spdk_mem={self.spdk_mem_mb}")
        rw = str(fio.get("rw", ""))
        if "randrw" in rw and fio.get("rwmixread") is not None:
            argv.append(f"--rwmixread={fio['rwmixread']}")
        if fio.get("time_based"):
            argv.append("--time_based")
        if self._fio_read_iolog_path:
            argv.append(f"--read_iolog={self._fio_read_iolog_path}")
            if _bool_param(self.trace_iolog_cfg.get("replay_no_stall", False)):
                argv.append("--replay_no_stall=1")
            if _bool_param(self.trace_iolog_cfg.get("read_iolog_chunked", False)):
                argv.append("--read_iolog_chunked=1")
            replay_redirect = str(self.trace_iolog_cfg.get("replay_redirect") or self.bdev_name)
            if replay_redirect:
                argv.append(f"--replay_redirect={replay_redirect}")
        if fio.get("group_reporting"):
            argv.append("--group_reporting")
        if fio.get("clat_percentiles", True):
            argv.append("--clat_percentiles=1")
        if fio.get("lat_percentiles"):
            argv.append("--lat_percentiles=1")
        percentile_list = fio.get("percentile_list", "99:99.9")
        if percentile_list:
            argv.append(f"--percentile_list={percentile_list}")
        return argv

    def _prepare_trace_iolog(self) -> str:
        if not self.trace_iolog_cfg:
            return ""
        trace_file = _expand_candidate(self.trace_iolog_cfg.get("trace_file") or self.trace_iolog_cfg.get("file") or "")
        if not trace_file or not os.path.isfile(trace_file):
            raise FileNotFoundError(f"trace_iolog.trace_file not found: {trace_file or self.trace_iolog_cfg.get('trace_file')!r}")
        trace_format = str(self.trace_iolog_cfg.get("trace_format", "tencent_csv")).strip().lower()
        if trace_format not in {"tencent", "tencent_csv"}:
            raise ValueError(f"unsupported trace_iolog.trace_format: {trace_format!r}")
        out_path = _expand_candidate(self.trace_iolog_cfg.get("output_file") or "")
        if not out_path:
            out_path = os.path.join(self.output_dir, f"{self.scene_id}_fio_iolog_v2.log")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        max_lines = _int_param(self.trace_iolog_cfg.get("max_lines"), 0)
        speedup = float(self.trace_iolog_cfg.get("speedup", 1.0) or 1.0)
        if speedup <= 0.0:
            raise ValueError("trace_iolog.speedup must be > 0")
        align = _int_param(self.trace_iolog_cfg.get("direct_align"), 4096)
        include_wait = not _bool_param(self.trace_iolog_cfg.get("no_stall", False))
        write_enable = _bool_param(self.trace_iolog_cfg.get("write_enable", False))
        converted = _convert_tencent_csv_to_fio_iolog(
            trace_file=trace_file,
            output_file=out_path,
            filename=self.bdev_name,
            max_lines=max_lines,
            speedup=speedup,
            align=align,
            include_wait=include_wait,
            write_enable=write_enable,
        )
        self.collected_data["trace_iolog"] = converted
        return out_path

    def _build_full_cmd(self, fio_argv: List[str], readiness: Mapping[str, Any]) -> List[str]:
        cmd: List[str] = []
        if self.use_sudo:
            cmd.append(SUDO_BIN)
        env = self._fio_env(readiness)
        if env:
            cmd.append("env")
            cmd.extend(f"{key}={value}" for key, value in env.items())
        cpu_bind = self.system_cfg.get("cpu_bind")
        if cpu_bind:
            cmd.extend(["taskset", "-c", str(cpu_bind)])
        cmd.extend(fio_argv)
        return cmd

    def _fio_env(self, readiness: Mapping[str, Any]) -> Dict[str, str]:
        env = dict(self.extra_env)
        spdk_home = readiness.get("spdk_home")
        if spdk_home:
            lib_dirs = [
                os.path.join(str(spdk_home), "build", "lib"),
                os.path.join(str(spdk_home), "dpdk", "build", "lib"),
            ]
            existing = [path for path in lib_dirs if os.path.isdir(path)]
            if existing:
                old_ld = env.get("LD_LIBRARY_PATH") or os.environ.get("LD_LIBRARY_PATH") or ""
                env["LD_LIBRARY_PATH"] = ":".join([*existing, old_ld]) if old_ld else ":".join(existing)
        return env

    def _write_bdev_json(self) -> None:
        subsystems: List[Dict[str, Any]] = []
        iobuf_rpc = self._iobuf_options_rpc()
        if iobuf_rpc:
            subsystems.append({"subsystem": "iobuf", "config": [iobuf_rpc]})

        accel_rpc = self._accel_options_rpc()
        if accel_rpc:
            subsystems.append({"subsystem": "accel", "config": [accel_rpc]})

        bdev_config: List[Dict[str, Any]] = []
        bdev_options_rpc = self._bdev_options_rpc()
        if bdev_options_rpc:
            bdev_config.append(bdev_options_rpc)
        bdev_config.append(self._bdev_rpc())

        config = {"subsystems": [*subsystems, {"subsystem": "bdev", "config": bdev_config}]}
        with open(self.bdev_json_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False, sort_keys=True)
        self.collected_data["bdev_config"] = config

    def _iobuf_options_rpc(self) -> Dict[str, Any]:
        params = _filtered_options(
            self.iobuf_options,
            {
                "small_pool_count",
                "large_pool_count",
                "small_bufsize",
                "large_bufsize",
                "enable_numa",
            },
        )
        if not params:
            return {}
        return {"method": "iobuf_set_options", "params": params}

    def _accel_options_rpc(self) -> Dict[str, Any]:
        params = _filtered_options(
            self.accel_options,
            {"small_cache_size", "large_cache_size", "task_count", "sequence_count", "buf_count"},
        )
        if not params:
            return {}
        return {"method": "accel_set_options", "params": params}

    def _bdev_options_rpc(self) -> Dict[str, Any]:
        params = _filtered_options(
            self.bdev_options,
            {
                "bdev_io_pool_size",
                "bdev_io_cache_size",
                "bdev_auto_examine",
                "iobuf_small_cache_size",
                "iobuf_large_cache_size",
            },
        )
        if not params:
            return {}
        return {"method": "bdev_set_options", "params": params}

    def _bdev_rpc(self) -> Dict[str, Any]:
        block_size = int(self.spdk_cfg.get("block_size", 4096))
        num_blocks = int(self.spdk_cfg.get("num_blocks", 268_435_456))
        if self.bdev_type in {"null", "bdev_null", "nullblk"}:
            return {
                "method": "bdev_null_create",
                "params": {"name": self.bdev_name, "num_blocks": num_blocks, "block_size": block_size},
            }
        if self.bdev_type in {"malloc", "bdev_malloc"}:
            return {
                "method": "bdev_malloc_create",
                "params": {"name": self.bdev_name, "num_blocks": num_blocks, "block_size": block_size},
            }
        raise ValueError(f"unsupported SPDK bdev_type: {self.bdev_type!r}")

    def _probe_spdk_readiness(self) -> Dict[str, Any]:
        spdk_home = self._find_spdk_home()
        plugin = self._find_fio_plugin(spdk_home)
        fio_binary_candidates = self._fio_binary_candidates(spdk_home)
        fio_bin, fio_binary_source = self._find_fio_binary(fio_binary_candidates)
        hugepages = _read_hugepages()
        hugepages_ready = (not self.require_hugepages) or _hugepages_ready(hugepages, self.hugemem_mb)
        readiness: Dict[str, Any] = {
            "spdk_home_candidates": [_expand_candidate(path) for path in self.spdk_home_candidates],
            "spdk_home": spdk_home,
            "fio_binary": fio_bin,
            "fio_binary_source": fio_binary_source,
            "fio_binary_candidates": [_expand_candidate(candidate["path"]) for candidate in fio_binary_candidates],
            "fio_binary_candidate_sources": [
                {"path": _expand_candidate(candidate["path"]), "source": candidate["source"]}
                for candidate in fio_binary_candidates
            ],
            "fio_binary_exists": bool(fio_bin and os.path.isfile(fio_bin) and os.access(fio_bin, os.X_OK)),
            "allow_system_fio_fallback": self.allow_system_fio_fallback,
            "fio_ioengine": plugin,
            "fio_ioengine_exists": bool(plugin and os.path.isfile(plugin)),
            "hugepages": hugepages,
            "hugepages_required": self.require_hugepages,
            "hugepages_ready": hugepages_ready,
            "setup_hugepages": self.setup_hugepages,
            "hugemem_mb": self.hugemem_mb,
            "bdev_type": self.bdev_type,
            "kernel_device_binding_required": False,
        }
        readiness["spdk_fio_ready"] = bool(
            readiness["fio_binary_exists"] and readiness["fio_ioengine_exists"] and readiness["hugepages_ready"]
        )
        if not readiness["spdk_fio_ready"]:
            readiness["skip_reason"] = self._skip_reason(readiness)
        return readiness

    def _needs_hugepage_setup(self, readiness: Mapping[str, Any]) -> bool:
        if not self.setup_hugepages:
            return False
        if not self.enforce_hugepages:
            return not readiness.get("hugepages_ready")
        hugepages = readiness.get("hugepages") or {}
        return not _hugepages_ready(hugepages, self.hugemem_mb)

    def _fio_binary_candidates(self, spdk_home: str) -> List[Dict[str, str]]:
        configured = self.spdk_cfg.get("fio_binary") or self.spdk_cfg.get("fio")
        candidates: List[Dict[str, str]] = []
        if configured:
            candidates.append({"path": str(configured), "source": "configured_fio_binary"})
        fio_source_dir = _read_spdk_config_value(spdk_home, "CONFIG_FIO_SOURCE_DIR") if spdk_home else ""
        if fio_source_dir:
            candidates.append({"path": os.path.join(fio_source_dir, "fio"), "source": "spdk_config_mk_fio_source_dir"})
        for candidate in self.spdk_cfg.get("fio_binary_candidates") or []:
            candidates.append({"path": str(candidate), "source": "configured_fio_binary_candidate"})
        candidates.extend(
            [
                {"path": "/opt/ioext/temp/fio/fio", "source": "known_validated_candidate"},
                {"path": "/home/anon/ioext-bench/temp/fio/fio", "source": "known_validated_candidate"},
                {"path": "/home/anon/ioext-bench/src/vm/assets/fio/fio", "source": "repo_asset_candidate"},
            ]
        )
        if self.allow_system_fio_fallback:
            system_fio = shutil.which("fio") or ""
            if system_fio:
                candidates.append({"path": system_fio, "source": "system_fio_explicitly_allowed"})
        return candidates

    def _find_fio_binary(self, candidates: Sequence[Mapping[str, str]]) -> tuple[str, str]:
        for candidate in candidates:
            expanded = _expand_candidate(candidate.get("path", ""))
            if expanded and os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                return expanded, str(candidate.get("source") or "unknown")
        return "", ""

    def _setup_internal_bdev_hugepages(self, spdk_home: str) -> Dict[str, Any]:
        nr_pages = max(1, (self.hugemem_mb + 1) // 2)
        script_text = (
            "set -e; "
            "mkdir -p /dev/hugepages; "
            "mountpoint -q /dev/hugepages || mount -t hugetlbfs nodev /dev/hugepages || true; "
            f"printf '%s\\n' {nr_pages} > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages; "
            "grep HugePages /proc/meminfo"
        )
        result: Dict[str, Any] = {
            "mode": "internal_bdev_hugepages_sysfs",
            "method": "direct_sysfs_no_pci_binding",
            "nr_hugepages_2mb": nr_pages,
            "hugemem_mb": self.hugemem_mb,
        }
        cmd: List[str] = []
        if self.use_sudo:
            cmd.append(SUDO_BIN)
        cmd.extend(["/bin/bash", "-lc", script_text])
        logger.info("configuring SPDK hugepages via sysfs without PCI binding: %s", " ".join(cmd))
        try:
            completed = subprocess.run(
                cmd,
                cwd=spdk_home,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.setup_timeout_sec,
                check=False,
            )
            result.update(
                {
                    "returncode": completed.returncode,
                    "stdout_tail": (completed.stdout or "")[-2000:],
                    "stderr_tail": (completed.stderr or "")[-2000:],
                }
            )
        except subprocess.TimeoutExpired as exc:
            result.update(
                {
                    "returncode": None,
                    "error": f"setup.sh timed out after {self.setup_timeout_sec}s",
                    "stdout_tail": ((exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or ""))[-2000:],
                    "stderr_tail": ((exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or ""))[-2000:],
                }
            )
        return result

    def _find_spdk_home(self) -> str:
        for candidate in self.spdk_home_candidates:
            expanded = _expand_candidate(candidate)
            if expanded and os.path.isdir(expanded):
                return expanded
        return ""

    def _find_fio_plugin(self, spdk_home: str) -> str:
        configured = self.spdk_cfg.get("fio_ioengine") or self.spdk_cfg.get("fio_plugin")
        candidates = []
        if configured:
            candidates.append(str(configured))
        if spdk_home:
            candidates.extend(
                [
                    os.path.join(spdk_home, "build", "fio", "spdk_bdev"),
                    os.path.join(spdk_home, "build", "fio", "spdk_bdev.so"),
                    os.path.join(spdk_home, "app", "fio", "bdev", "spdk_bdev"),
                ]
            )
        for candidate in candidates:
            expanded = _expand_candidate(candidate)
            if expanded and os.path.isfile(expanded):
                return expanded
        return ""

    @staticmethod
    def _skip_reason(readiness: Mapping[str, Any]) -> str:
        if not readiness.get("fio_binary_exists"):
            return "fio binary not found"
        if not readiness.get("spdk_home"):
            return "SPDK checkout not found"
        if not readiness.get("fio_ioengine_exists"):
            return "SPDK FIO bdev plugin not found; build SPDK with --with-fio"
        if readiness.get("hugepages_required") and not readiness.get("hugepages_ready"):
            return "SPDK hugepages not configured; run setup.sh with PCI_ALLOWED=none or enable setup_hugepages"
        return "SPDK FIO readiness failed"


def _bool_param(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_param(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _filtered_options(options: Mapping[str, Any], allowed_keys: set[str]) -> Dict[str, Any]:
    filtered: Dict[str, Any] = {}
    for key, value in options.items():
        if key in allowed_keys and value is not None:
            filtered[str(key)] = value
    return filtered


def _merge_missing_options(options: Dict[str, Any], defaults: Mapping[str, Any]) -> None:
    for key, value in defaults.items():
        options.setdefault(key, value)


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


def _read_spdk_config_value(spdk_home: str, key: str) -> str:
    config_path = os.path.join(spdk_home, "mk", "config.mk")
    for line in _read_text(config_path).splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        for separator in ("?=", "="):
            prefix = f"{key}{separator}"
            if text.startswith(prefix):
                return text[len(prefix) :].strip()
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
        digits = "".join(ch for ch in rest if ch.isdigit())
        if digits:
            values[key] = int(digits)
    return values


def _hugepages_ready(values: Mapping[str, int], requested_mb: int = 0) -> bool:
    total = int(values.get("HugePages_Total", 0))
    free = int(values.get("HugePages_Free", 0))
    page_size_kb = int(values.get("Hugepagesize", 2048)) or 2048
    requested_pages = max(1, ((requested_mb * 1024) + page_size_kb - 1) // page_size_kb) if requested_mb else 1
    return total >= requested_pages and free >= requested_pages


def _align_up(value: int, align: int) -> int:
    if value <= 0:
        return align
    return ((value + align - 1) // align) * align


def _align_down(value: int, align: int) -> int:
    return (value // align) * align


def _convert_tencent_csv_to_fio_iolog(
    trace_file: str,
    output_file: str,
    filename: str,
    max_lines: int,
    speedup: float,
    align: int,
    include_wait: bool,
    write_enable: bool,
) -> Dict[str, Any]:
    count = 0
    reads = 0
    writes = 0
    skipped_writes = 0
    base_ts: Optional[float] = None
    prev_rel_us = 0
    max_size = 0
    with open(trace_file, "r", encoding="utf-8", errors="replace") as src, open(output_file, "w", encoding="utf-8") as dst:
        dst.write("fio version 2 iolog\n")
        dst.write(f"{filename} add\n")
        dst.write(f"{filename} open\n")
        for line in src:
            if max_lines > 0 and count >= max_lines:
                break
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 4:
                continue
            try:
                ts = float(parts[0])
                offset_blocks = int(parts[1])
                size_blocks = int(parts[2])
                op = int(parts[3])
            except ValueError:
                continue
            if base_ts is None:
                base_ts = ts
            rel_us = int(max(0.0, ((ts - base_ts) / speedup) * 1_000_000.0))
            if include_wait:
                wait_us = rel_us - prev_rel_us
                if wait_us >= 100:
                    dst.write(f"{filename} wait {wait_us} 0\n")
                prev_rel_us = rel_us
            offset = _align_down(max(0, offset_blocks) * 512, align)
            size = _align_up(max(1, size_blocks) * 512, align)
            max_size = max(max_size, size)
            if op == 1:
                action = "read"
                reads += 1
            else:
                if not write_enable:
                    skipped_writes += 1
                    continue
                action = "write"
                writes += 1
            dst.write(f"{filename} {action} {offset} {size}\n")
            count += 1
        dst.write(f"{filename} close\n")
    if count <= 0:
        raise RuntimeError(f"trace_iolog conversion produced no replayable I/O from {trace_file}")
    return {
        "trace_file": trace_file,
        "fio_iolog": output_file,
        "format": "fio_version_2_iolog",
        "source_trace_format": "tencent_csv",
        "filename": filename,
        "converted_ios": count,
        "reads": reads,
        "writes": writes,
        "skipped_writes": skipped_writes,
        "max_size_bytes": max_size,
        "include_wait": include_wait,
        "speedup": speedup,
        "direct_align": align,
        "semantics": "block_level_fio_iolog_replay_approximation",
    }
