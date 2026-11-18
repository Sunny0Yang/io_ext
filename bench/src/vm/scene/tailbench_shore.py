"""io-ext TailBench Shore smoke scene."""

from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from vm.scene.base_scene import Scene
from vm.scene.utils import DataScorer

from .common import (
    BASH_BIN,
    DEFAULT_ROOT_CGROUP_PATH,
    cleanup_cgroup,
    command_to_shell,
    ensure_output_dir,
    expand_path,
    interrupt_process,
    list_output_paths,
    loader_log_paths,
    popen_in_own_session,
    run_sudo_shell,
    setup_cgroup_io_controls,
    setup_cgroup_v2,
    setup_iocost_at_root,
    set_scheduler,
    start_loader_chain,
    stop_loader_chain,
    validate_block_device_name,
    wrap_command_in_cgroup,
    write_json_stat,
)


logger = logging.getLogger("io_ext_tailbench_shore")


class IoExtTailbenchShoreScene(Scene):
    """Run a short TailBench Shore command and parse lats*.txt when present."""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}
        self.output_dir = expand_path(parameters.get("output_dir")) or "./test_results/io_ext_tailbench_shore"
        self.target_device = validate_block_device_name(str(parameters.get("target_device", "nvme0n1")))
        io_ext = parameters.get("io_ext") or {}
        self.scheduler = io_ext.get("scheduler", parameters.get("scheduler"))
        cgroup_cfg = io_ext.get("cgroup") or {}
        self.cgroup_path = expand_path(cgroup_cfg.get("path") or io_ext.get("cgroup_path") or parameters.get("cgroup_path"))
        self.cgroup_controllers = list(cgroup_cfg.get("controllers") or ["io"])
        # Optional cgroup v2 io controller values (same semantics as the fio /
        # ycsb scenes). Shore itself is pure buffered I/O with no cache
        # switches; these only matter for QoS-controller arms (io.latency on
        # the tenant cgroup, io.cost model/qos at the cgroup root).
        self.cgroup_io_latency = cgroup_cfg.get("io_latency", cgroup_cfg.get("io.latency"))
        io_cost_cfg = cgroup_cfg.get("io_cost", cgroup_cfg.get("io.cost")) or {}
        self.iocost_model = io_cost_cfg.get("model")
        self.iocost_qos = io_cost_cfg.get("qos")
        self.iocost_root_path = expand_path(
            (io_ext.get("root_cgroup") or {}).get("path") if isinstance(io_ext.get("root_cgroup"), dict)
            else io_ext.get("root_cgroup_path")
        ) or DEFAULT_ROOT_CGROUP_PATH
        self.io_controls: Dict[str, Any] = {}

        tailbench = parameters.get("tailbench") or {}
        self.mode = str(tailbench.get("mode", "shore_bench") or "shore_bench").strip().lower()
        self.tailbench_home = expand_path(tailbench.get("home") or "$TAILBENCH_HOME")
        self.working_dir = expand_path(tailbench.get("working_dir") or self.tailbench_home or ".")
        self.build_script = expand_path(tailbench.get("build_script"))
        self.run_build = bool(tailbench.get("run_build", False))
        self.run_script = expand_path(tailbench.get("run_script"))
        self.run_args = [str(arg) for arg in tailbench.get("run_args", [])]
        self.command: Optional[Union[str, Sequence[str]]] = tailbench.get("command")
        self.env_overrides = {str(k): str(v) for k, v in dict(tailbench.get("env") or {}).items()}
        self.latency_globs = list(tailbench.get("latency_globs") or ["lats*.txt", "**/lats*.txt"])
        self.binary_latency_globs = list(tailbench.get("binary_latency_globs") or ["lats*.bin", "**/lats*.bin"])
        self.bench_script = expand_path(tailbench.get("bench_script") or "src/vm/assets/tailbench/shore/shore_bench.py")
        default_binary = os.path.join(self.working_dir or ".", "shore-kits", "shore_kits_integrated")
        self.binary_path = expand_path(tailbench.get("binary") or default_binary)
        self.data_file = expand_path(tailbench.get("data_file"))
        self.shore_conf_overrides = dict(tailbench.get("shore_conf_overrides") or {})
        self.trace_roi = _to_bool(tailbench.get("trace_roi", False))
        self.require_latency = _to_bool(tailbench.get("require_latency", True))
        self.latency_unit = str(tailbench.get("latency_unit", "us") or "us").strip().lower()
        self.binary_latency_format = str(tailbench.get("binary_latency_format", "triplet") or "triplet").strip().lower()
        self.scratch_dir = expand_path(tailbench.get("scratch_dir") or os.path.join(self.output_dir, "scratch"))
        self.p99_threshold_us = float(tailbench.get("p99_threshold_us", 50000.0))
        slo = tailbench.get("slo_deadline_us")
        self.slo_deadline_us = float(slo) if slo is not None else float(self.p99_threshold_us)
        self._effective_mode = self.mode

        self.duration = int(parameters.get("duration", 120))
        # cache=on 口径的 warmup 纪律：sequence 开始前 drop 页缓存，让 cache 命中
        # 来自应用自身缓存（Shore buffer pool）而非上次 run 残留页缓存。
        # 默认 false，与 ycsb_rocksdb.py 的 drop_caches_before_run 模式一致。
        self.drop_caches_before_run = _to_bool(parameters.get("drop_caches_before_run", False))
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self._proc: Optional[subprocess.Popen[str]] = None
        self._roi_proc: Optional[subprocess.Popen[str]] = None
        self._loader_procs: List[subprocess.Popen[str]] = []

    def setup(self) -> None:
        ensure_output_dir(self.output_dir)
        ensure_output_dir(self.scratch_dir)
        if self.cgroup_path:
            setup_cgroup_v2(self.cgroup_path, self.cgroup_controllers)
        if self.run_build and self.build_script:
            self._run_setup_command([self.build_script])
        self._effective_mode = self._select_mode()

    def prepare(self) -> None:
        ensure_output_dir(self.output_dir)
        # Establish controls before the timed lifecycle begins. _run reapplies
        # them idempotently; its first sample must already see the arm active.
        self._apply_io_controls()
        self.collected_data = {
            "target_device": self.target_device,
            "scheduler": self.scheduler,
            "output_dir": self.output_dir,
            "tailbench_home": self.tailbench_home,
            "mode": self.mode,
            "effective_mode": self._effective_mode,
            "bench_script": self.bench_script,
            "binary_path": self.binary_path,
            "data_file": self.data_file,
            "tailbench_env": self._tailbench_env(),
            "shore_conf_overrides": dict(self.shore_conf_overrides),
            "require_latency": self.require_latency,
            "latency_unit": self.latency_unit,
            "binary_latency_format": self.binary_latency_format,
            "drop_caches_before_run": self.drop_caches_before_run,
            "io_ext_cgroup_controls": dict(self.io_controls),
        }
        self.start_time = None
        self.end_time = None

    def _select_mode(self) -> str:
        if self.mode in {"shore_bench", "bench"}:
            return "shore_bench"
        if self.mode in {"shore", "shore_integrated"}:
            return "shore_integrated"
        if self.mode == "command" and (self.command or self.run_script):
            return "command"
        raise ValueError(f"unsupported TailBench Shore mode: {self.mode!r}")

    def _run_setup_command(self, cmd: Sequence[str]) -> None:
        result = subprocess.run(cmd, cwd=self.working_dir, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"TailBench build failed: {result.stderr or result.stdout}")

    def _build_command(self) -> Union[str, Sequence[str]]:
        if self.command:
            return self.command
        return [self.run_script or "", *self.run_args]

    def _drop_caches(self) -> None:
        # Same drop discipline as ycsb_rocksdb.py: sync + /proc/sys/vm/drop_caches=3.
        run_sudo_shell("sync; printf '%s\\n' 3 > /proc/sys/vm/drop_caches", check=False)

    def _apply_io_controls(self) -> None:
        if not self.cgroup_io_latency and self.iocost_model is None and self.iocost_qos is None:
            return
        if self.cgroup_io_latency:
            if not self.cgroup_path:
                raise ValueError("io_ext.cgroup.path is required when io_latency is configured")
            self.io_controls = setup_cgroup_io_controls(
                self.cgroup_path,
                f"/dev/{self.target_device}",
                None,
                None,
                self.cgroup_io_latency,
            )
        if self.iocost_model is not None or self.iocost_qos is not None:
            iocost_result = setup_iocost_at_root(
                f"/dev/{self.target_device}",
                self.iocost_model,
                self.iocost_qos,
                root_cgroup_path=self.iocost_root_path,
            )
            if iocost_result:
                self.io_controls["io_cost"] = iocost_result

    def _run(self) -> None:
        self.start_time = time.time()
        if self.drop_caches_before_run:
            self._drop_caches()
        set_scheduler(self.target_device, self.scheduler)
        self._apply_io_controls()
        self.collected_data["io_ext_cgroup_controls"] = dict(self.io_controls)
        self._loader_procs = start_loader_chain(self.parameters, self.output_dir, self.cgroup_path)
        try:
            result = self._run_tailbench_command()
            self.collected_data.update(result)
            self.collected_data["output_paths"] = list_output_paths(self.output_dir, ["**/*"])
            if result["returncode"] != 0 and not self.should_stop():
                raise RuntimeError(
                    "TailBench Shore command failed with "
                    f"returncode={result['returncode']} stdout={result['stdout_txt']} stderr={result['stderr_txt']}"
                )
            latency_files = self._active_latency_files()
            latencies = self._load_latencies(latency_files)
            self.collected_data["latency_files"] = latency_files
            if latencies:
                self.collected_data["latency_count"] = len(latencies)
                self.collected_data["slo_deadline_us"] = self.slo_deadline_us
                self.collected_data["p50_latency_us"] = _percentile(latencies, 50.0)
                self.collected_data["p95_latency_us"] = _percentile(latencies, 95.0)
                self.collected_data["p99_latency_us"] = _percentile(latencies, 99.0)
                miss_count = sum(1 for value in latencies if value > self.slo_deadline_us)
                total = len(latencies)
                self.collected_data["deadline_miss_count"] = miss_count
                self.collected_data["deadline_miss_rate"] = (miss_count / total) if total else None
            elif self.require_latency:
                raise RuntimeError("TailBench Shore command completed without parseable latency output")
        finally:
            if self._loader_procs:
                self.collected_data.setdefault("loader_logs", loader_log_paths(self._loader_procs))
            stop_loader_chain(self._loader_procs)
            self._loader_procs = []
            self.end_time = time.time()

    def _run_tailbench_command(self) -> Dict[str, Any]:
        env = self._tailbench_env()
        if self._effective_mode == "shore_bench":
            cmd, cwd = self._build_shore_bench_command(env)
            return self._run_wrapped_command(cmd, cwd, env)
        if self._effective_mode == "shore_integrated":
            cmd, cwd = self._build_shore_integrated_command()
            return self._run_wrapped_command(cmd, cwd, env)
        return self._run_wrapped_command(self._build_command(), self.working_dir, env)

    def _tailbench_env(self) -> Dict[str, str]:
        env = dict(self.env_overrides)
        env.setdefault("TBENCH_CLIENT_THREADS", env.get("THREADS", "1"))
        env.setdefault("TBENCH_QPS", env.get("QPS", "1"))
        env.setdefault("TBENCH_MAXREQS", env.get("MAXREQS", "100"))
        env.setdefault("TBENCH_WARMUPREQS", env.get("WARMUPREQS", "10"))
        env.setdefault("TBENCH_MINSLEEPNS", "10000")
        env.setdefault("THREADS", env["TBENCH_CLIENT_THREADS"])
        env.setdefault("QPS", env["TBENCH_QPS"])
        env.setdefault("MAXREQS", env["TBENCH_MAXREQS"])
        env.setdefault("WARMUPREQS", env["TBENCH_WARMUPREQS"])
        env.setdefault("TBENCH_OUTPUT", self.output_dir)
        env.setdefault("TBENCH_OUTPUT_DIR", self.output_dir)
        if self.scratch_dir:
            env.setdefault("SCRATCH_DIR", self.scratch_dir)
        return env

    def _build_shore_bench_command(self, env: Dict[str, str]) -> Tuple[List[str], str]:
        if not self.bench_script:
            raise FileNotFoundError("tailbench.bench_script is required for shore_bench mode")
        if not self.data_file:
            raise FileNotFoundError("tailbench.data_file is required for shore_bench mode")
        return (
            [
                sys.executable,
                self.bench_script,
                "--data-file",
                self.data_file,
                "--output-dir",
                self.output_dir,
                "--max-reqs",
                env.get("TBENCH_MAXREQS", "100"),
                "--warmup-reqs",
                env.get("TBENCH_WARMUPREQS", "10"),
                "--qps",
                env.get("TBENCH_QPS", "1"),
                *(
                    ["--io-file", env["TBENCH_IO_FILE"], "--io-mode", env.get("TBENCH_IO_MODE", "sync_rw")]
                    if env.get("TBENCH_IO_FILE")
                    else []
                ),
            ],
            self.working_dir or self.output_dir,
        )

    def _build_shore_integrated_command(self) -> Tuple[List[str], str]:
        if not self.binary_path:
            raise FileNotFoundError("TailBench Shore binary path is required")
        run_dir = os.path.join(self.output_dir, "shore_run")
        ensure_output_dir(run_dir)
        scratch = os.path.join(self.scratch_dir, f"{self.scene_id}_{int(self.start_time or time.time())}")
        ensure_output_dir(scratch)
        ensure_output_dir(os.path.join(scratch, "log"))
        ensure_output_dir(os.path.join(scratch, "diskrw"))
        self._safe_link(scratch, os.path.join(run_dir, "scratch"))
        self._safe_link(os.path.join(scratch, "log"), os.path.join(run_dir, "log"))
        self._safe_link(os.path.join(scratch, "diskrw"), os.path.join(run_dir, "diskrw"))
        if self.data_file:
            self._safe_link(self.data_file, os.path.join(run_dir, "db-tpcc-1"))

        cmdfile_template = os.path.join(self.working_dir or ".", "shore-kits", "run-templates", "cmdfile.template")
        conf_template = os.path.join(self.working_dir or ".", "shore-kits", "run-templates", "shore.conf.template")
        cmdfile = os.path.join(run_dir, "cmdfile")
        shore_conf = os.path.join(run_dir, "shore.conf")
        self._render_template(cmdfile_template, cmdfile)
        self._render_template(conf_template, shore_conf, apply_conf_overrides=True)
        self.collected_data["shore_conf_overrides"] = dict(self.shore_conf_overrides)
        self.collected_data["shore_conf_rendered"] = open(shore_conf, encoding="utf-8").read()
        if self.data_file:
            self.collected_data["data_file_realpath"] = os.path.realpath(self.data_file)
            self.collected_data["data_file_device"] = subprocess.run(
                ["findmnt", "-n", "-T", self.data_file, "-o", "SOURCE,TARGET,FSTYPE,OPTIONS"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        return ["chrt", "-r", "99", self.binary_path, "-i", cmdfile], run_dir

    def _safe_link(self, src: str, dest: str) -> None:
        if os.path.lexists(dest):
            if os.path.isdir(dest) and not os.path.islink(dest):
                shutil.rmtree(dest)
            else:
                os.unlink(dest)
        os.symlink(src, dest)

    def _render_template(self, source: str, dest: str, *, apply_conf_overrides: bool = False) -> None:
        with open(source, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        threads = self._tailbench_env().get("TBENCH_CLIENT_THREADS", "1")
        max_reqs = self._tailbench_env().get("TBENCH_MAXREQS", "100")
        text = text.replace("@NTHREADS", threads).replace("@REQS", max_reqs)
        for key, value in (self.shore_conf_overrides.items() if apply_conf_overrides else []):
            import re
            pattern = rf"(?m)^({re.escape(str(key))}\s*=\s*).*$"
            text, count = re.subn(pattern, rf"\g<1>{value}", text, count=1)
            if count == 0:
                raise ValueError(f"Shore config template has no key {key!r}")
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)

    def _run_wrapped_command(self, cmd: Union[str, Sequence[str]], cwd: Optional[str], env: Dict[str, str]) -> Dict[str, Any]:
        wrapped = wrap_command_in_cgroup(cmd, self.cgroup_path, env=env)
        final_cmd = list(wrapped) if not isinstance(wrapped, str) else [BASH_BIN, "-lc", wrapped]
        stdout_path = os.path.join(self.output_dir, f"{self.scene_id}_stdout.txt")
        stderr_path = os.path.join(self.output_dir, f"{self.scene_id}_stderr.txt")
        logger.info("running TailBench Shore command: %s", command_to_shell(final_cmd))
        roi_proc = None
        roi_file = None
        roi_path = os.path.join(self.output_dir, "roi_trace.log")
        if self.trace_roi:
            # Userspace function entry probes only; no storage policy changes.
            program = (f'uprobe:{self.binary_path}:_ZN6Client9_startRoiEv {{ printf("ROI_START %llu\\n", nsecs); }} '
                       f'uprobe:{self.binary_path}:_ZN6Client9dumpStatsEv {{ printf("ROI_END %llu\\n", nsecs); }} '
                       'BEGIN { printf("TRACE_READY\\n"); }')
            roi_file = open(roi_path, "w", encoding="utf-8")
            roi_proc = subprocess.Popen(["bpftrace", "-q", "-e", program], stdout=roi_file, stderr=roi_file)
            self._roi_proc = roi_proc
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if "TRACE_READY" in open(roi_path, encoding="utf-8").read():
                    break
                if roi_proc.poll() is not None:
                    raise RuntimeError(f"ROI tracer failed; inspect {roi_path}")
                time.sleep(.1)
            else:
                roi_proc.terminate(); roi_proc.wait(timeout=5); roi_file.close()
                raise RuntimeError("ROI tracer readiness timeout")
        with open(stdout_path, "w", encoding="utf-8") as out_f, open(stderr_path, "w", encoding="utf-8") as err_f:
            proc = popen_in_own_session(
                final_cmd,
                cwd=cwd,
                stdout=out_f,
                stderr=err_f,
                text=True,
            )
            self._proc = proc
            next_storage_sample = time.monotonic()
            while proc.poll() is None:
                if self.data_file and time.monotonic() >= next_storage_sample:
                    self._capture_storage_state()
                    next_storage_sample = time.monotonic() + 30.0
                if self.should_stop():
                    interrupt_process(proc)
                    break
                time.sleep(0.2)
            rc = proc.returncode if proc.returncode is not None else proc.wait()
        if self._proc is proc:
            self._proc = None
        if roi_proc is not None:
            roi_proc.terminate()
            try:
                roi_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                roi_proc.kill(); roi_proc.wait(timeout=5)
            roi_file.close()
            self._roi_proc = None
            trace = open(roi_path, encoding="utf-8").read()
            import re
            starts = re.findall(r"ROI_START (\d+)", trace)
            ends = re.findall(r"ROI_END (\d+)", trace)
            if len(starts) != 1 or len(ends) != 1:
                raise RuntimeError(f"Missing/ambiguous ROI timestamps: {trace}")
            roi_seconds = (int(ends[0]) - int(starts[0])) / 1e9
            self.collected_data["roi_seconds"] = roi_seconds
            self.collected_data["roi_completed_ops_per_sec"] = int(env["TBENCH_MAXREQS"]) / roi_seconds
            self.collected_data["roi_trace"] = trace
        return {"returncode": int(rc or 0), "stdout_txt": stdout_path, "stderr_txt": stderr_path}

    def _capture_storage_state(self) -> None:
        """Persist actual DB file descriptors and cgroup I/O during execution."""
        sample: Dict[str, Any] = {"timestamp": time.time(), "db_fds": []}
        target = os.path.realpath(self.data_file or "")
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                with open(f"{entry.path}/comm", encoding="utf-8") as f:
                    if not f.read().strip().startswith("shore_kits"):
                        continue
                for fd in os.scandir(f"{entry.path}/fd"):
                    try:
                        if os.path.realpath(fd.path) != target:
                            continue
                        info = open(f"{entry.path}/fdinfo/{fd.name}", encoding="utf-8").read()
                        flags_line = next(line for line in info.splitlines() if line.startswith("flags:"))
                        flags = int(flags_line.split()[1], 8)
                        sample["db_fds"].append({"pid": int(entry.name), "fd": fd.name,
                                                 "path": target, "fdinfo": info,
                                                 "o_direct": bool(flags & os.O_DIRECT)})
                    except (OSError, StopIteration):
                        continue
            except OSError:
                continue
        if self.cgroup_path:
            try:
                sample["io_stat"] = open(os.path.join(self.cgroup_path, "io.stat"), encoding="utf-8").read()
            except OSError:
                pass
        self.collected_data.setdefault("storage_samples", []).append(sample)

    def _latency_files(self) -> List[str]:
        files: List[str] = []
        patterns = list(self.latency_globs) + list(self.binary_latency_globs)
        for root in (self.output_dir, self.working_dir):
            files.extend(list_output_paths(root, patterns))
        return sorted(set(files))

    def _active_latency_files(self) -> List[str]:
        return [path for path in self._latency_files() if not self._is_stale_output(path)]

    def _load_latencies(self, paths: Optional[Iterable[str]] = None) -> List[float]:
        values: List[float] = []
        for path in paths or self._active_latency_files():
            if path.endswith(".bin"):
                values.extend(_parse_tailbench_lats_bin(path, self.binary_latency_format))
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        numbers: List[float] = []
                        for token in line.replace(",", " ").split():
                            try:
                                numbers.append(float(token))
                            except ValueError:
                                continue
                        if numbers:
                            value = self._text_latency_to_us(numbers[-1])
                            if value >= 0:
                                values.append(value)
            except OSError:
                continue
        return values

    def _text_latency_to_us(self, value: float) -> float:
        if self.latency_unit == "ms":
            return value * 1000.0
        if self.latency_unit == "ns":
            return value / 1000.0
        return value

    def _is_stale_output(self, path: str) -> bool:
        if not self.start_time:
            return False
        try:
            return os.path.getmtime(path) + 0.001 < self.start_time
        except OSError:
            return True

    def end(self) -> None:
        interrupt_process(self._proc)
        if self._roi_proc is not None and self._roi_proc.poll() is None:
            self._roi_proc.terminate()
            try:
                self._roi_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._roi_proc.kill(); self._roi_proc.wait(timeout=5)
        self._roi_proc = None
        stop_loader_chain(self._loader_procs)
        self._loader_procs = []

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
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
        return write_json_stat(self.output_dir, "tailbench_shore", self.scene_id, self.collected_data)

    def deserialize(self, data_path: str) -> None:
        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.start_time = self.collected_data.get("start_time")
        self.end_time = self.collected_data.get("end_time")
        self.duration = float(self.collected_data.get("duration_wall") or 0.0)

    def score(self) -> float:
        if self.collected_data.get("returncode") not in (0, None):
            return 0.0
        p99 = self.collected_data.get("p99_latency_us")
        if not isinstance(p99, (int, float)) or p99 <= 0:
            return 0.0
        scorer = DataScorer(
            data=[float(p99), float(p99)],
            threshold=self.p99_threshold_us,
            positive=False,
            value_exponent=0.5,
        )
        return float(scorer.calculate_score())


def _percentile(values: Iterable[float], percentile: float) -> Optional[float]:
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (percentile / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _parse_tailbench_lats_bin(path: str, binary_latency_format: str = "triplet") -> List[float]:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    if not data:
        return []
    fmt = str(binary_latency_format or "triplet").strip().lower()
    triplet_aliases = {"triplet", "tailbench_triplet", "qqq", "queue_service_sojourn"}
    pair_aliases = {"pair", "tailbench_pair", "qq", "service_end_to_end"}
    if fmt in triplet_aliases:
        return _parse_tailbench_triplet_lats(data)
    if fmt in pair_aliases:
        return _parse_tailbench_pair_lats(data)
    if fmt != "auto":
        raise ValueError(f"unsupported TailBench binary latency format: {binary_latency_format!r}")

    can_triplet = len(data) % 24 == 0
    can_pair = len(data) % 16 == 0
    if can_triplet and not can_pair:
        return _parse_tailbench_triplet_lats(data)
    if can_pair and not can_triplet:
        return _parse_tailbench_pair_lats(data)
    if can_triplet and can_pair:
        raise ValueError(
            "ambiguous TailBench lats.bin length; set tailbench.binary_latency_format "
            "to 'triplet' or 'pair'"
        )
    return []


def _parse_tailbench_triplet_lats(data: bytes) -> List[float]:
    if len(data) % 24 != 0:
        return []
    values = [
        _tailbench_ns_to_us(enqueue_ns, sojourn_or_end_ns)
        for enqueue_ns, _, sojourn_or_end_ns in struct.iter_unpack("<QQQ", data)
    ]
    return [value for value in values if value >= 0]


def _parse_tailbench_pair_lats(data: bytes) -> List[float]:
    if len(data) % 16 != 0:
        return []
    values = [
        _tailbench_ns_to_us(enqueue_ns, end_or_latency_ns)
        for enqueue_ns, end_or_latency_ns in struct.iter_unpack("<QQ", data)
    ]
    return [value for value in values if value >= 0]


def _tailbench_ns_to_us(start_or_zero_ns: int, latency_or_end_ns: int) -> float:
    # Some TailBench forks store absolute enqueue/end timestamps rather than a
    # pre-subtracted sojourn time. Absolute CLOCK_MONOTONIC-like nanoseconds are
    # large; convert them to a delta before returning microseconds.
    if start_or_zero_ns >= 1_000_000_000_000 and latency_or_end_ns >= start_or_zero_ns:
        return float(latency_or_end_ns - start_or_zero_ns) / 1000.0
    return float(latency_or_end_ns) / 1000.0
