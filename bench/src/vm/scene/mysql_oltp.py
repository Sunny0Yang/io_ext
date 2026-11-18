"""Self-contained io-ext MySQL OLTP scene."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from vm.scene.base_scene import Scene
from vm.scene.utils import DataScorer

from .common import (
    BASH_BIN,
    cleanup_cgroup,
    expand_path,
    file_tail,
    interrupt_process,
    loader_log_paths,
    popen_in_own_session,
    run_sudo_shell,
    setup_cgroup_io_controls,
    setup_cgroup_v2,
    set_scheduler,
    start_loader_chain,
    stop_loader_chain,
    validate_block_device_name,
    wrap_command_in_cgroup,
)


logger = logging.getLogger("io_ext_mysql_oltp")


_VALID_SYSBENCH_TESTS = {"oltp_read_write", "tpcc"}
_THROUGHPUT_PATTERNS = (
    re.compile(r"\bthrou(?:gh)?put\s*:\s*([0-9.eE+-]+)\s*events/s", re.IGNORECASE),
    re.compile(r"\bevents/s\s*\(eps\)\s*:\s*([0-9.eE+-]+)", re.IGNORECASE),
    re.compile(r"\btransactions\s*:\s*[0-9]+\s*\(([0-9.eE+-]+)\s+per sec\.\)", re.IGNORECASE),
)


class IoExtMysqlScene(Scene):
    """Run a MySQL sysbench workload through an io-ext loader chain and cgroup."""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        target_device = parameters.get("target_device")
        if not target_device:
            raise ValueError("parameters.target_device is required, e.g. nvme0n1")
        self.target_device = validate_block_device_name(str(target_device))

        mysql = parameters.get("mysql") or {}
        self.data_dir = expand_path(mysql.get("data_dir") or "/tmp/mysql_data") or "/tmp/mysql_data"
        self.socket = expand_path(mysql.get("socket") or "/tmp/mysql.sock") or "/tmp/mysql.sock"
        self.port = int(mysql.get("port", 3306))
        self.buffer_pool_size = int(mysql.get("buffer_pool_size", 256))
        self.sysbench_test = str(mysql.get("sysbench_test", "oltp_read_write")).strip()
        if self.sysbench_test not in _VALID_SYSBENCH_TESTS:
            raise ValueError(f"mysql.sysbench_test must be one of {sorted(_VALID_SYSBENCH_TESTS)}")
        self.sysbench_tables = int(mysql.get("sysbench_tables", 10))
        self.sysbench_table_size = int(mysql.get("sysbench_table_size", 10000))
        self.sysbench_threads = int(mysql.get("sysbench_threads", 4))
        self.sysbench_time = int(mysql.get("sysbench_time", 60))
        self.sysbench_rand_type = str(mysql.get("sysbench_rand_type", "uniform"))
        self.mysql_ready_timeout_sec = float(mysql.get("ready_timeout_sec", 60.0))

        self.mysqld_bin = _resolve_command(mysql.get("mysqld_bin"), "mysqld", "/usr/sbin/mysqld")
        self.mysql_bin = _resolve_command(mysql.get("mysql_bin"), "mysql", "mysql")
        self.mysqladmin_bin = _resolve_command(mysql.get("mysqladmin_bin"), "mysqladmin", "mysqladmin")
        self.sysbench_bin = _resolve_command(mysql.get("sysbench_bin"), "sysbench", "sysbench")

        io_ext = parameters.get("io_ext") or {}
        self.io_ext_cfg: Dict[str, Any] = dict(io_ext)
        self.scheduler = io_ext.get("scheduler", parameters.get("scheduler"))
        cgroup_cfg = io_ext.get("cgroup") or {}
        self.io_ext_cgroup_path = expand_path(cgroup_cfg.get("path") or io_ext.get("cgroup_path"))
        self.io_ext_cgroup_controllers = list(cgroup_cfg.get("controllers") or ["io"])
        self.io_ext_cgroup_io_weight = cgroup_cfg.get("io_weight", cgroup_cfg.get("io.weight"))
        self.io_ext_cgroup_io_max = cgroup_cfg.get("io_max", cgroup_cfg.get("io.max"))
        if (self.io_ext_cgroup_io_weight is not None or self.io_ext_cgroup_io_max is not None) and not self.io_ext_cgroup_path:
            raise ValueError("io_ext.cgroup.path is required when io_weight or io_max is configured")
        if self.io_ext_cgroup_io_weight is not None or self.io_ext_cgroup_io_max is not None:
            enabled_controllers = {str(ctrl).strip().lstrip("+") for ctrl in self.io_ext_cgroup_controllers}
            if "io" not in enabled_controllers:
                self.io_ext_cgroup_controllers.append("io")
        self.io_ext_cgroup_controls: Dict[str, Any] = {}

        self.output_dir = expand_path(parameters.get("output_dir")) or "./test_results"
        self.duration = int(parameters.get("duration", self.sysbench_time + 180))
        self.drop_caches_before_run = _to_bool(parameters.get("drop_caches_before_run", True))

        self.my_cnf_path = os.path.join(self.data_dir, "my.cnf")
        self.pid_file = os.path.join(self.data_dir, "mysqld.pid")
        self.mysql_error_log = os.path.join(self.output_dir, f"{self.scene_id}_mysqld.err.log")
        self.mysql_init_log = os.path.join(self.output_dir, f"{self.scene_id}_mysqld_init.log")
        self.prepare_output_path = os.path.join(self.output_dir, f"{self.scene_id}_sysbench_prepare.txt")
        self.run_output_path = os.path.join(self.output_dir, f"{self.scene_id}_sysbench_run.txt")

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self._loader_procs: List[subprocess.Popen[str]] = []
        self._mysqld_proc: Optional[subprocess.Popen[str]] = None
        self._sysbench_proc: Optional[subprocess.Popen[str]] = None

    def setup(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)
        dev_path = f"/dev/{self.target_device}"
        if not os.path.exists(dev_path):
            raise FileNotFoundError(f"block device does not exist: {dev_path}")
        self._ensure_commands_available()
        if not self._is_mysql_initialized():
            self._initialize_mysql_data_dir()
        self._write_mysql_config()
        if self.io_ext_cgroup_path:
            setup_cgroup_v2(self.io_ext_cgroup_path, self.io_ext_cgroup_controllers)

    def prepare(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        self._apply_cgroup_io_controls()
        self.collected_data = {
            "target_device": self.target_device,
            "scheduler": self.scheduler,
            "data_dir": self.data_dir,
            "socket": self.socket,
            "port": self.port,
            "buffer_pool_size_mb": self.buffer_pool_size,
            "sysbench_test": self.sysbench_test,
            "sysbench_tables": self.sysbench_tables,
            "sysbench_table_size": self.sysbench_table_size,
            "sysbench_threads": self.sysbench_threads,
            "sysbench_time": self.sysbench_time,
            "sysbench_rand_type": self.sysbench_rand_type,
            "io_ext_cgroup_path": self.io_ext_cgroup_path,
            "io_ext_cgroup_controls": self.io_ext_cgroup_controls,
            "drop_caches_before_run": self.drop_caches_before_run,
            "mysql_config": self.my_cnf_path,
            "mysql_error_log": self.mysql_error_log,
        }
        self.start_time = None
        self.end_time = None

    def _ensure_commands_available(self) -> None:
        for name, command in {
            "mysqld": self.mysqld_bin,
            "mysql": self.mysql_bin,
            "mysqladmin": self.mysqladmin_bin,
            "sysbench": self.sysbench_bin,
        }.items():
            if os.path.isabs(command):
                if not os.path.isfile(command):
                    raise FileNotFoundError(f"{name} executable not found: {command}")
            elif shutil.which(command) is None:
                raise FileNotFoundError(f"{name} executable not found in PATH: {command}")

    def _is_mysql_initialized(self) -> bool:
        mysql_system_db = os.path.join(self.data_dir, "mysql")
        return os.path.isdir(mysql_system_db)

    def _initialize_mysql_data_dir(self) -> None:
        cmd = [self.mysqld_bin, f"--initialize-insecure", f"--datadir={self.data_dir}"]
        logger.info("initializing MySQL data directory: %s", self.data_dir)
        with open(self.mysql_init_log, "w", encoding="utf-8") as out_f:
            result = subprocess.run(cmd, stdout=out_f, stderr=subprocess.STDOUT, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"mysqld --initialize-insecure failed returncode={result.returncode} "
                f"output={file_tail(self.mysql_init_log, max_chars=2000)!r}"
            )

    def _write_mysql_config(self) -> None:
        socket_dir = os.path.dirname(self.socket)
        if socket_dir:
            os.makedirs(socket_dir, exist_ok=True)
        content = "\n".join(
            [
                "[mysqld]",
                f"datadir={self.data_dir}",
                f"socket={self.socket}",
                f"port={self.port}",
                "bind-address=127.0.0.1",
                f"pid-file={self.pid_file}",
                f"log-error={self.mysql_error_log}",
                f"innodb_buffer_pool_size={self.buffer_pool_size}M",
                "skip-name-resolve=1",
                "",
                "[client]",
                f"socket={self.socket}",
                f"port={self.port}",
                "user=root",
                "",
            ]
        )
        with open(self.my_cnf_path, "w", encoding="utf-8") as f:
            f.write(content)

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
        )

    def _start_loader(self) -> None:
        self._loader_procs = start_loader_chain(self.parameters, self.output_dir, self.io_ext_cgroup_path)

    def _stop_loader(self) -> None:
        if self._loader_procs:
            self.collected_data.setdefault("loader_logs", loader_log_paths(self._loader_procs))
        stop_loader_chain(self._loader_procs)
        self._loader_procs = []

    def _start_mysqld(self) -> None:
        self._remove_stale_mysql_runtime_files()
        cmd = [
            self.mysqld_bin,
            f"--defaults-file={self.my_cnf_path}",
            f"--datadir={self.data_dir}",
            f"--socket={self.socket}",
            f"--port={self.port}",
            f"--innodb-buffer-pool-size={self.buffer_pool_size}M",
        ]
        logger.info("starting mysqld: %s", " ".join(cmd))
        log_f = open(self.mysql_error_log, "a", encoding="utf-8")
        try:
            self._mysqld_proc = popen_in_own_session(cmd, stdout=log_f, stderr=subprocess.STDOUT, text=True)
        finally:
            log_f.close()
        self._wait_for_mysql_ready()

    def _remove_stale_mysql_runtime_files(self) -> None:
        for path in (self.socket, self.pid_file):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                logger.debug("failed to remove stale MySQL runtime file %s", path, exc_info=True)

    def _wait_for_mysql_ready(self) -> None:
        deadline = time.time() + self.mysql_ready_timeout_sec
        cmd = [
            self.mysqladmin_bin,
            "--protocol=socket",
            f"--socket={self.socket}",
            "--user=root",
            "ping",
        ]
        while time.time() < deadline:
            if self.should_stop():
                return
            if self._mysqld_proc and self._mysqld_proc.poll() is not None:
                raise RuntimeError(
                    f"mysqld exited during startup returncode={self._mysqld_proc.returncode} "
                    f"log={file_tail(self.mysql_error_log, max_chars=2000)!r}"
                )
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode == 0:
                return
            time.sleep(0.5)
        raise TimeoutError(
            f"MySQL did not become ready within {self.mysql_ready_timeout_sec:.1f}s; "
            f"log={file_tail(self.mysql_error_log, max_chars=2000)!r}"
        )

    def _stop_mysqld(self) -> None:
        if not self._mysqld_proc:
            return
        shutdown_cmd = [
            self.mysqladmin_bin,
            "--protocol=socket",
            f"--socket={self.socket}",
            "--user=root",
            "shutdown",
        ]
        subprocess.run(shutdown_cmd, capture_output=True, text=True, check=False)
        interrupt_process(self._mysqld_proc, timeout_sec=20)
        self._mysqld_proc = None

    def _mysql_client_cmd(self, sql: str) -> List[str]:
        return [
            self.mysql_bin,
            "--protocol=socket",
            f"--socket={self.socket}",
            "--user=root",
            "-e",
            sql,
        ]

    def _create_database(self) -> None:
        result = subprocess.run(
            self._mysql_client_cmd("CREATE DATABASE IF NOT EXISTS sbtest"),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"failed to create sbtest database: {(result.stderr or result.stdout).strip()}")

    def _sysbench_base_argv(self, test_name: str) -> List[str]:
        return [
            self.sysbench_bin,
            test_name,
            "--mysql-host=localhost",
            f"--mysql-socket={self.socket}",
            "--mysql-user=root",
            "--mysql-db=sbtest",
            f"--tables={self.sysbench_tables}",
            f"--table-size={self.sysbench_table_size}",
        ]

    def _run_sysbench_prepare(self) -> int:
        cmd = [*self._sysbench_base_argv("oltp_read_write"), "prepare"]
        logger.info("sysbench prepare output -> %s", self.prepare_output_path)
        with open(self.prepare_output_path, "w", encoding="utf-8") as out_f:
            proc = popen_in_own_session(cmd, stdout=out_f, stderr=subprocess.STDOUT, text=True)
            while proc.poll() is None:
                if self.should_stop():
                    interrupt_process(proc, timeout_sec=15)
                    break
                time.sleep(0.3)
            rc = proc.returncode if proc.returncode is not None else proc.wait()
        return int(rc or 0)

    def _run_sysbench_workload(self) -> int:
        cmd = [
            *self._sysbench_base_argv(self.sysbench_test),
            f"--threads={self.sysbench_threads}",
            f"--time={self.sysbench_time}",
            f"--rand-type={self.sysbench_rand_type}",
            "run",
        ]
        wrapped = wrap_command_in_cgroup(cmd, self.io_ext_cgroup_path)
        final_cmd = list(wrapped) if not isinstance(wrapped, str) else [BASH_BIN, "-lc", wrapped]
        logger.info("sysbench run output -> %s", self.run_output_path)
        with open(self.run_output_path, "w", encoding="utf-8") as out_f:
            self._sysbench_proc = popen_in_own_session(
                final_cmd,
                stdout=out_f,
                stderr=subprocess.STDOUT,
                text=True,
            )
            while self._sysbench_proc.poll() is None:
                if self.should_stop():
                    interrupt_process(self._sysbench_proc, timeout_sec=15)
                    break
                time.sleep(0.3)
            rc = self._sysbench_proc.returncode if self._sysbench_proc.returncode is not None else self._sysbench_proc.wait()
        self._sysbench_proc = None
        return int(rc or 0)

    def _run(self) -> None:
        self.start_time = time.time()
        if self.drop_caches_before_run:
            self._drop_caches()
        self._set_scheduler()
        self._start_loader()

        try:
            self._start_mysqld()
            self._create_database()

            rc_prepare = self._run_sysbench_prepare()
            self.collected_data["prepare_returncode"] = rc_prepare
            self.collected_data["prepare_txt"] = self.prepare_output_path
            if rc_prepare != 0 and not self.should_stop():
                raise RuntimeError(
                    f"sysbench prepare failed returncode={rc_prepare} "
                    f"output={file_tail(self.prepare_output_path, max_chars=2000)!r}"
                )
            if self.should_stop():
                return

            rc_run = self._run_sysbench_workload()
            self.collected_data["run_returncode"] = rc_run
            self.collected_data["run_txt"] = self.run_output_path
            if rc_run != 0 and not self.should_stop():
                raise RuntimeError(
                    f"sysbench run failed returncode={rc_run} "
                    f"output={file_tail(self.run_output_path, max_chars=2000)!r}"
                )

            metrics = _parse_sysbench_metrics(self.run_output_path)
            self.collected_data.update(metrics)
            throughput = metrics.get("throughput_events_per_sec")
            if (throughput is None or throughput <= 0) and not self.should_stop():
                raise RuntimeError(f"sysbench run produced invalid throughput: {self.run_output_path}")
        finally:
            self._stop_mysqld()
            self._stop_loader()
            self.end_time = time.time()

    def end(self) -> None:
        interrupt_process(self._sysbench_proc, timeout_sec=15)
        self._sysbench_proc = None
        self._stop_mysqld()
        self._stop_loader()

    def cleanup(self) -> None:
        self.end()

    def teardown(self) -> None:
        self.end()
        cleanup_cgroup(self.io_ext_cgroup_path)

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
        path = os.path.join(self.output_dir, f"mysql_oltp_{self.scene_id}_{int(time.time())}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.collected_data, f, indent=2, ensure_ascii=False, sort_keys=True)
        result_files: List[str] = [path]
        for raw_path in (
            self.prepare_output_path,
            self.run_output_path,
            self.mysql_error_log,
            self.mysql_init_log,
            self.my_cnf_path,
        ):
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
        throughput = self.collected_data.get("throughput_events_per_sec")
        if throughput is None or throughput <= 0:
            logger.warning("MySQL sysbench scene has no valid throughput; returning zero score")
            return 0.0
        self.duration = float(self.collected_data.get("duration_wall") or 0.0)
        scorer = DataScorer(
            data=[float(throughput), float(throughput)],
            threshold=500.0,
            positive=True,
            value_exponent=0.5,
        )
        score = scorer.calculate_score()
        logger.info("MySQL sysbench throughput %.2f events/s, score %.2f", throughput, score)
        return float(score)


def _resolve_command(configured: Any, command_name: str, fallback: str) -> str:
    if configured:
        expanded = expand_path(str(configured)) if os.path.sep in str(configured) else str(configured)
        return expanded or str(configured)
    resolved = shutil.which(command_name)
    return resolved or fallback


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _parse_sysbench_metrics(run_txt_path: str) -> Dict[str, Any]:
    if not os.path.isfile(run_txt_path):
        return {}
    try:
        with open(run_txt_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}

    throughput = _parse_throughput(text)
    metrics: Dict[str, Any] = {
        "throughput_events_per_sec": throughput,
        "throughput_ops_per_sec": throughput,
        "sysbench_latency": _parse_latency_metrics(text),
    }
    for key, value in metrics["sysbench_latency"].items():
        metrics[f"latency_{key}_ms"] = value
    return metrics


def _parse_throughput(text: str) -> Optional[float]:
    for pattern in _THROUGHPUT_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        try:
            return float(match.group(1))
        except ValueError:
            continue
    return None


def _parse_latency_metrics(text: str) -> Dict[str, float]:
    result: Dict[str, float] = {}
    aliases = {
        "avg": ("avg", "average"),
        "min": ("min",),
        "max": ("max",),
        "p95": ("p95", "95th percentile"),
        "p99": ("p99", "99th percentile"),
        "p999": ("p999", "p99.9", "99.9th percentile"),
    }
    for key, names in aliases.items():
        value = _parse_first_latency_value(text, names)
        if value is not None:
            result[key] = value
    return result


def _parse_first_latency_value(text: str, names: tuple[str, ...]) -> Optional[float]:
    for name in names:
        escaped = re.escape(name)
        match = re.search(rf"\b{escaped}\s*(?:=|:)\s*([0-9.eE+-]+)", text, re.IGNORECASE)
        if not match:
            continue
        try:
            return float(match.group(1))
        except ValueError:
            continue
    return None
