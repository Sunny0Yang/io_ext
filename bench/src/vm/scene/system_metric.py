"""
系统级 CPU/内存指标监测场景。

默认使用纯 Python 读取 /proc/stat、/proc/meminfo 与 /proc/loadavg，避免 VM smoke
依赖外部 sched_agent 二进制。仍保留显式 collection_mode=sched_agent 的旧路径，便于
后续需要原生二进制输出时复用。
"""

import argparse
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ..base_scene import Scene

logger = logging.getLogger("system_metric")


DEFAULT_OUTPUT_DIR = "/home/anon/ioext-bench/test_results/system_metric"
DEFAULT_AGENT_ROOT = "/home/anon/ioext-bench/src/vm/assets/system/system_metric"


class SystemMetricScene(Scene):
    """系统级 CPU/内存监测场景。"""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        """
        初始化系统指标监测场景。

        Args:
            scene_id: 场景ID
            scene_name: 场景名称
            parameters: 场景参数，可选包含：
                - collection_mode: proc/sched_agent/auto，默认 proc
                - output_dir: JSON 输出目录
                - output_file: JSON 输出文件路径
                - duration: 监控持续时间(秒)，0 表示直到 controller stop
                - sample_interval: /proc 采样间隔(秒)
                - require_samples: true 时没有 /proc 样本则失败
                - max_samples: 保留的最大样本数，防止长序列占用过多内存
                - cgroup_paths: 需要采样 cpu.stat 的 cgroup v2 目录列表，默认空
                - agent_path/config_file/agent_output_file: 旧 sched_agent 模式路径
                - privilege: 旧 sched_agent sudo 权限配置
        """
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        self.collection_mode = str(parameters.get("collection_mode", parameters.get("mode", "proc"))).strip().lower()
        if self.collection_mode not in {"proc", "sched_agent", "auto"}:
            raise ValueError(f"unsupported system metric collection_mode: {self.collection_mode!r}")

        self.duration = _float_param(parameters.get("duration", 300), 300.0)
        self.sample_interval = max(0.1, _float_param(parameters.get("sample_interval", 1.0), 1.0))
        self.max_samples = max(1, int(parameters.get("max_samples", 7200)))
        self.require_samples = _bool_param(parameters.get("require_samples", self.collection_mode != "sched_agent"))

        self.output_dir = _expand_path(parameters.get("output_dir", DEFAULT_OUTPUT_DIR))
        self.output_file = _expand_path(
            parameters.get("output_file", os.path.join(self.output_dir, f"{self.scene_id}_summary.json"))
        )

        self.agent_path = _expand_path(
            parameters.get("agent_path", os.path.join(DEFAULT_AGENT_ROOT, "build", "sched_agent"))
        )
        self.config_file = _expand_path(
            parameters.get("config_file", os.path.join(DEFAULT_AGENT_ROOT, "config", "config.json"))
        )
        self.agent_output_file = _expand_path(
            parameters.get("agent_output_file", os.path.join(self.output_dir, "binary_output.bin"))
        )

        self.privilege_config = parameters.get("privilege", {}) or {}
        self.privilege_method = str(self.privilege_config.get("method", "sudo_askpass"))
        self.askpass_script_path = str(self.privilege_config.get("askpass_script", "") or "")
        self.password_env_var = str(self.privilege_config.get("env_var", "SUDO_PASSWORD") or "SUDO_PASSWORD")

        raw_disk_devices = parameters.get("disk_devices") or parameters.get("disk_device")
        if isinstance(raw_disk_devices, str):
            self.disk_devices = [raw_disk_devices.strip()] if raw_disk_devices.strip() else []
        elif isinstance(raw_disk_devices, (list, tuple)):
            self.disk_devices = [str(device).strip() for device in raw_disk_devices if str(device).strip()]
        else:
            self.disk_devices = []

        raw_cgroup_paths = parameters.get("cgroup_paths") or parameters.get("cgroup_path")
        if isinstance(raw_cgroup_paths, str):
            self.cgroup_paths = [os.path.normpath(raw_cgroup_paths)] if raw_cgroup_paths.strip() else []
        elif isinstance(raw_cgroup_paths, (list, tuple)):
            self.cgroup_paths = [
                os.path.normpath(str(path).strip()) for path in raw_cgroup_paths if str(path).strip()
            ]
        else:
            self.cgroup_paths = []

        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.effective_mode: Optional[str] = None
        self.agent_process: Optional[subprocess.Popen[str]] = None
        self.temp_askpass_script: Optional[str] = None

        self.samples: List[Dict[str, Any]] = []
        self.samples_lock = threading.Lock()
        # 已告警过的缺失 cgroup 路径，避免每个采样间隔重复刷日志
        self._missing_cgroup_warned: set = set()

        logger.debug("初始化系统指标场景 %s: %s, 参数: %s", scene_id, scene_name, parameters)

    def setup(self) -> None:
        """一次性全局设置。proc 模式无需 sudo；sched_agent 模式才创建 askpass。"""

        if self.collection_mode == "sched_agent":
            self._create_askpass_script()

    def prepare(self) -> None:
        """每轮运行前重置样本与输出文件，避免复制旧 JSON。"""

        os.makedirs(self.output_dir, exist_ok=True)
        self.start_time = None
        self.end_time = None
        self.effective_mode = self._select_effective_mode()
        self.collected_data = {}
        with self.samples_lock:
            self.samples = []
        self._missing_cgroup_warned = set()

        for path in [self.output_file, self.agent_output_file]:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError as exc:
                logger.warning("删除旧系统指标输出失败: %s (%s)", path, exc)

        logger.info(
            "系统指标监测准备完成: mode=%s, output=%s, interval=%.3fs",
            self.effective_mode,
            self.output_file,
            self.sample_interval,
        )
        if self.cgroup_paths:
            logger.info("cgroup cpu.stat 采样目标: %s", ", ".join(self.cgroup_paths))

    def _run(self) -> None:
        """启动监测并等待 duration 或 stop 信号。"""

        self.start_time = time.time()
        self.effective_mode = self.effective_mode or self._select_effective_mode()
        logger.info(
            "开始系统指标监测: mode=%s, duration=%s",
            self.effective_mode,
            "until-stop" if self.duration <= 0 else f"{self.duration:g}s",
        )

        try:
            if self.effective_mode == "sched_agent":
                self._start_system_monitoring()
                self._wait_until_done()
            else:
                self._run_proc_sampler()
        finally:
            self.end_time = time.time()
            logger.info("系统指标监测结束，采样数: %d", len(self._snapshot_samples()))

    def end(self) -> None:
        """结束监测；可被 controller 多次调用。"""

        self._should_stop.set()
        if self.agent_process and self.agent_process.poll() is None:
            pid = self.agent_process.pid
            logger.info("终止 sched_agent 系统指标进程 (PID: %s)", pid)
            try:
                self.agent_process.terminate()
                self.agent_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("sched_agent 未响应 TERM，发送 KILL")
                self.agent_process.kill()
                self.agent_process.wait(timeout=3)
            finally:
                self.agent_process = None

        if self.end_time is None:
            self.end_time = time.time()

    def cleanup(self) -> None:
        """每轮运行后清理。保留 JSON 输出供 controller 复制。"""

        self.end()
        logger.info("系统指标监测 cleanup 完成")

    def teardown(self) -> None:
        """一次性清理临时 askpass 脚本。"""

        self.end()
        if self.temp_askpass_script and os.path.exists(self.temp_askpass_script):
            os.remove(self.temp_askpass_script)
            logger.debug("已删除临时 SUDO_ASKPASS 脚本: %s", self.temp_askpass_script)
        self.temp_askpass_script = None

    def collect(self) -> Dict[str, Any]:
        """收集结构化 CPU/内存指标摘要。"""

        data = self._build_result()
        summary = data["summary"]
        if self.require_samples and self.effective_mode != "sched_agent":
            if summary.get("sample_count", 0) <= 0:
                raise RuntimeError("system_metric completed without /proc samples")
            if not summary.get("cpu_usage_observed"):
                raise RuntimeError("system_metric completed without a valid CPU usage delta")
            if not summary.get("memory_observed"):
                raise RuntimeError("system_metric completed without memory samples")
        self.collected_data = data
        return data

    def stat(self) -> List[str]:
        """写出 JSON，并返回 controller 需要复制的结果文件列表。"""

        data = self.collected_data or self._build_result()
        os.makedirs(os.path.dirname(self.output_file), exist_ok=True)
        with open(self.output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        files = [self.output_file]
        if self.effective_mode == "sched_agent" and self.agent_output_file and os.path.exists(self.agent_output_file):
            files.append(self.agent_output_file)
        logger.info("系统指标结果已写入: %s", files)
        return files

    def deserialize(self, data_path: str) -> None:
        """从 JSON 结果恢复 enough state for score/evaluator。"""

        with open(data_path, "r", encoding="utf-8") as f:
            self.collected_data = json.load(f)
        self.output_file = data_path
        self.effective_mode = self.collected_data.get("metadata", {}).get("effective_mode")
        with self.samples_lock:
            self.samples = list(self.collected_data.get("samples", []))

    def score(self) -> float:
        """系统监控场景只提供证据，不参与 workload 评分。"""

        return 0.0

    def _select_effective_mode(self) -> str:
        if self.collection_mode == "sched_agent":
            return "sched_agent"
        if self.collection_mode == "auto":
            return "proc"
        return "proc"

    def _wait_until_done(self) -> None:
        if self.duration <= 0:
            self._should_stop.wait()
            return
        self._should_stop.wait(timeout=self.duration)

    def _run_proc_sampler(self) -> None:
        previous_cpu: Optional[Dict[str, Tuple[int, int]]] = None
        previous_disk: Optional[Dict[str, Dict[str, int]]] = None
        while not self.should_stop():
            now = time.time()
            if self.duration > 0 and self.start_time is not None and now - self.start_time > self.duration:
                break

            sample, previous_cpu, previous_disk = self._collect_proc_sample(previous_cpu, previous_disk)
            self._append_sample(sample)

            if self._should_stop.wait(timeout=self.sample_interval):
                break

    def _collect_proc_sample(
        self,
        previous_cpu: Optional[Dict[str, Tuple[int, int]]],
        previous_disk: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Tuple[int, int]], Dict[str, Dict[str, int]]]:
        cpu_totals = _read_proc_cpu_totals()
        cpu_total, cpu_idle = cpu_totals["cpu"]
        cpu_usage_percent = _cpu_usage_percent(cpu_totals.get("cpu"), (previous_cpu or {}).get("cpu"))
        per_core = {
            cpu: usage
            for cpu, totals in cpu_totals.items()
            if cpu != "cpu"
            for usage in [_cpu_usage_percent(totals, (previous_cpu or {}).get(cpu))]
            if usage is not None
        }

        memory = _read_proc_meminfo()
        load = _read_proc_loadavg()
        mem_total = memory.get("MemTotal", 0) * 1024
        mem_available = _mem_available_kb(memory) * 1024
        mem_used = max(0, mem_total - mem_available)
        swap_total = memory.get("SwapTotal", 0) * 1024
        swap_free = memory.get("SwapFree", 0) * 1024
        swap_used = max(0, swap_total - swap_free)

        disk_totals = _read_proc_diskstats(self.disk_devices)
        disk_usage = _disk_usage_percent(disk_totals, previous_disk or {}, self.sample_interval)

        sample = {
            "timestamp": time.time(),
            "cpu": {
                "total_jiffies": cpu_total,
                "idle_jiffies": cpu_idle,
                "usage_percent": cpu_usage_percent,
                "usage_valid": cpu_usage_percent is not None,
                "usage_per_core": per_core,
            },
            "memory": {
                "total_bytes": mem_total,
                "available_bytes": mem_available,
                "used_bytes": mem_used,
                "used_percent": _percent(mem_used, mem_total),
            },
            "swap": {
                "total_bytes": swap_total,
                "free_bytes": swap_free,
                "used_bytes": swap_used,
                "used_percent": _percent(swap_used, swap_total),
            },
            "loadavg": load,
        }
        if disk_totals:
            sample["disk"] = {
                "devices": disk_usage,
                "observed": any(entry.get("usage_valid") for entry in disk_usage.values()),
            }
        if self.cgroup_paths:
            sample["cgroups"] = self._collect_cgroup_cpu_stats()
        return sample, cpu_totals, disk_totals

    def _collect_cgroup_cpu_stats(self) -> Dict[str, Dict[str, Any]]:
        """采样每个配置 cgroup 的 cpu.stat；路径缺失时记录 null 并只告警一次。"""

        cgroups: Dict[str, Dict[str, Any]] = {}
        for cgroup_path in self.cgroup_paths:
            entry = _read_cgroup_cpu_stat(cgroup_path)
            if not entry["exists"] and cgroup_path not in self._missing_cgroup_warned:
                logger.warning("cgroup cpu.stat 不存在或不可读，本样本记录 null: %s", entry["cpu_stat_path"])
                self._missing_cgroup_warned.add(cgroup_path)
            cgroups[cgroup_path] = entry
        return cgroups

    def _append_sample(self, sample: Dict[str, Any]) -> None:
        with self.samples_lock:
            self.samples.append(sample)
            if len(self.samples) > self.max_samples:
                self.samples = self.samples[-self.max_samples :]

    def _snapshot_samples(self) -> List[Dict[str, Any]]:
        with self.samples_lock:
            return [sample.copy() for sample in self.samples]

    def _build_result(self) -> Dict[str, Any]:
        samples = self._snapshot_samples()
        effective_end = self.end_time or time.time()
        duration = effective_end - self.start_time if self.start_time else 0.0
        return {
            "metadata": {
                "scene_id": self.scene_id,
                "scene_name": self.scene_name,
                "collection_mode": self.collection_mode,
                "effective_mode": self.effective_mode or self._select_effective_mode(),
                "start_time": self.start_time,
                "end_time": self.end_time,
                "duration": duration,
                "sample_interval": self.sample_interval,
                "require_samples": self.require_samples,
                "output_file": self.output_file,
                "agent_path": self.agent_path,
                "agent_output_file": self.agent_output_file,
            },
            "summary": _summarize_samples(samples),
            "samples": samples,
        }

    def _create_askpass_script(self) -> None:
        if self.privilege_method != "sudo_askpass" or self.askpass_script_path:
            return
        fd, self.temp_askpass_script = tempfile.mkstemp(suffix=".sh")
        os.close(fd)
        with open(self.temp_askpass_script, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "${self.password_env_var}"\n')
        os.chmod(self.temp_askpass_script, 0o700)
        self.askpass_script_path = self.temp_askpass_script

    def _run_with_privilege(self, cmd: List[str]) -> subprocess.Popen[str]:
        if self.privilege_method == "sudo_askpass":
            env = os.environ.copy()
            env["SUDO_ASKPASS"] = self.askpass_script_path
            return subprocess.Popen(["sudo", "-E", "-A"] + cmd, env=env, text=True)
        if self.privilege_method == "sudo_cache":
            return subprocess.Popen(["sudo", "-E"] + cmd, text=True)
        if self.privilege_method == "sudo_nopasswd":
            return subprocess.Popen(["sudo", "-E"] + cmd, text=True)
        return subprocess.Popen(cmd, text=True)

    def _start_system_monitoring(self) -> None:
        if not os.path.exists(self.agent_path):
            raise FileNotFoundError(f"系统指标监控工具不存在: {self.agent_path}")
        if not os.path.exists(self.config_file):
            raise FileNotFoundError(f"系统指标监控配置文件不存在: {self.config_file}")
        self.agent_process = self._run_with_privilege([self.agent_path, self.config_file])
        logger.info("sched_agent 系统指标监控启动成功，PID=%s", self.agent_process.pid)


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


def _read_proc_cpu_totals() -> Dict[str, Tuple[int, int]]:
    result: Dict[str, Tuple[int, int]] = {}
    with open("/proc/stat", "r", encoding="utf-8") as f:
        for line in f:
            fields = line.split()
            if not fields or not fields[0].startswith("cpu"):
                if result:
                    break
                continue
            values = [int(part) for part in fields[1:]]
            total = sum(values)
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            result[fields[0]] = (total, idle)
    if "cpu" not in result:
        raise RuntimeError("/proc/stat missing aggregate cpu line")
    return result


def _cpu_usage_percent(
    current: Optional[Tuple[int, int]], previous: Optional[Tuple[int, int]]
) -> Optional[float]:
    if current is None or previous is None:
        return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))


def _read_proc_meminfo() -> Dict[str, int]:
    result: Dict[str, int] = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as f:
        for line in f:
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            fields = rest.strip().split()
            if not fields:
                continue
            try:
                result[key] = int(fields[0])
            except ValueError:
                continue
    return result


def _read_proc_loadavg() -> Dict[str, float]:
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as f:
            fields = f.read().split()
        return {"load1": float(fields[0]), "load5": float(fields[1]), "load15": float(fields[2])}
    except (OSError, ValueError, IndexError):
        return {"load1": 0.0, "load5": 0.0, "load15": 0.0}


def _mem_available_kb(meminfo: Dict[str, int]) -> int:
    if "MemAvailable" in meminfo:
        return meminfo["MemAvailable"]
    return meminfo.get("MemFree", 0) + meminfo.get("Buffers", 0) + meminfo.get("Cached", 0)


def _percent(used: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return (used / total) * 100.0


def _read_proc_diskstats(devices: List[str]) -> Dict[str, Dict[str, int]]:
    if not devices:
        return {}
    wanted = set(devices)
    result: Dict[str, Dict[str, int]] = {}
    try:
        with open("/proc/diskstats", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 14 or parts[2] not in wanted:
                    continue
                result[parts[2]] = {
                    "reads_completed": int(parts[3]),
                    "sectors_read": int(parts[5]),
                    "read_ms": int(parts[6]),
                    "writes_completed": int(parts[7]),
                    "sectors_written": int(parts[9]),
                    "write_ms": int(parts[10]),
                    "ios_in_progress": int(parts[11]),
                    "io_ms": int(parts[12]),
                    "weighted_io_ms": int(parts[13]),
                }
    except OSError:
        return {}
    return result


def _disk_usage_percent(
    current: Dict[str, Dict[str, int]],
    previous: Dict[str, Dict[str, int]],
    interval_sec: float,
) -> Dict[str, Dict[str, Any]]:
    interval_ms = max(1.0, interval_sec * 1000.0)
    result: Dict[str, Dict[str, Any]] = {}
    for device, stats in current.items():
        prev = previous.get(device) or {}
        delta_io_ms = stats["io_ms"] - int(prev.get("io_ms", stats["io_ms"]))
        usage = max(0.0, min(100.0, (delta_io_ms / interval_ms) * 100.0)) if prev else None
        read_sectors_delta = stats["sectors_read"] - int(prev.get("sectors_read", stats["sectors_read"]))
        write_sectors_delta = stats["sectors_written"] - int(prev.get("sectors_written", stats["sectors_written"]))
        result[device] = {
            "io_ms": stats["io_ms"],
            "ios_in_progress": stats["ios_in_progress"],
            "read_sectors_delta": read_sectors_delta if prev else 0,
            "write_sectors_delta": write_sectors_delta if prev else 0,
            "util_percent": usage,
            "usage_valid": usage is not None,
        }
    return result


_CGROUP_CPU_STAT_FIELDS = (
    "usage_usec",
    "user_usec",
    "system_usec",
    "nr_periods",
    "nr_throttled",
    "throttled_usec",
)


def _read_cgroup_cpu_stat(cgroup_path: str) -> Dict[str, Any]:
    """读取单个 cgroup v2 目录的 cpu.stat。

    文件缺失或读取失败时 exists=False 且各字段记录为 null，调用方写入
    占位样本即可，绝不能让采样循环中断。
    """
    stat_path = os.path.join(cgroup_path, "cpu.stat")
    snapshot: Dict[str, Any] = {
        "path": cgroup_path,
        "cpu_stat_path": stat_path,
        "exists": os.path.isfile(stat_path),
    }
    counters: Dict[str, int] = {}
    if snapshot["exists"]:
        try:
            with open(stat_path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) != 2:
                        continue
                    try:
                        counters[parts[0]] = int(parts[1])
                    except ValueError:
                        continue
        except OSError:
            snapshot["exists"] = False
    for field in _CGROUP_CPU_STAT_FIELDS:
        snapshot[field] = counters.get(field)
    return snapshot


def _stats(values: List[float]) -> Dict[str, float]:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return {"count": 0, "avg": 0.0, "min": 0.0, "max": 0.0, "last": 0.0}
    return {
        "count": len(clean),
        "avg": sum(clean) / len(clean),
        "min": min(clean),
        "max": max(clean),
        "last": clean[-1],
    }


def _summarize_samples(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    cpu_values = [sample["cpu"]["usage_percent"] for sample in samples if sample.get("cpu", {}).get("usage_valid")]
    mem_percent = [sample["memory"]["used_percent"] for sample in samples if "memory" in sample]
    mem_bytes = [sample["memory"]["used_bytes"] for sample in samples if "memory" in sample]
    swap_percent = [sample["swap"]["used_percent"] for sample in samples if "swap" in sample]
    load1 = [sample["loadavg"]["load1"] for sample in samples if "loadavg" in sample]
    latest = samples[-1] if samples else {}
    per_core_names = sorted(
        {
            cpu
            for sample in samples
            for cpu in sample.get("cpu", {}).get("usage_per_core", {})
        }
    )
    per_core = {
        cpu: _stats(
            [
                sample.get("cpu", {}).get("usage_per_core", {}).get(cpu)
                for sample in samples
                if cpu in sample.get("cpu", {}).get("usage_per_core", {})
            ]
        )
        for cpu in per_core_names
    }

    disk_devices = sorted(
        {
            device
            for sample in samples
            for device in (sample.get("disk") or {}).get("devices", {})
        }
    )
    disk_util = {
        device: _stats(
            [
                sample.get("disk", {}).get("devices", {}).get(device, {}).get("util_percent")
                for sample in samples
                if sample.get("disk", {}).get("devices", {}).get(device, {}).get("usage_valid")
            ]
        )
        for device in disk_devices
    }
    disk_ios = {
        device: _stats(
            [
                float(sample.get("disk", {}).get("devices", {}).get(device, {}).get("ios_in_progress", 0))
                for sample in samples
                if device in (sample.get("disk") or {}).get("devices", {})
            ]
        )
        for device in disk_devices
    }

    cgroup_names = sorted(
        {
            cgroup
            for sample in samples
            for cgroup in (sample.get("cgroups") or {})
        }
    )
    cgroups = {
        cgroup: _summarize_cgroup_cpu_stat(samples, cgroup)
        for cgroup in cgroup_names
    }

    summary = {
        "sample_count": len(samples),
        "cpu_usage_observed": bool(cpu_values),
        "memory_observed": bool(mem_percent),
        "cpu_usage_percent": _stats(cpu_values),
        "cpu_usage_per_core": per_core,
        "memory_used_percent": _stats(mem_percent),
        "memory_used_bytes": _stats(mem_bytes),
        "swap_used_percent": _stats(swap_percent),
        "load1": _stats(load1),
        "disk_util_percent": disk_util,
        "disk_ios_in_progress": disk_ios,
        "latest_memory_total_bytes": latest.get("memory", {}).get("total_bytes", 0),
        "latest_memory_available_bytes": latest.get("memory", {}).get("available_bytes", 0),
    }
    if cgroups:
        summary["cgroups"] = cgroups
    return summary


def _summarize_cgroup_cpu_stat(samples: List[Dict[str, Any]], cgroup: str) -> Dict[str, Any]:
    """汇总单个 cgroup 的 cpu.stat 样本。

    各字段是单调递增计数器：usage_usec_total 为监控窗口内的增量；
    utilization_usec_per_sample 为相邻样本增量的统计（每个采样间隔消耗
    的 CPU 微秒数）；nr_periods/nr_throttled/throttled_usec 增量用于判断
    cgroup 是否被 CPU 限额压制（RQ5 共置归因：恶化必须来自块设备而非 CPU）。
    """
    entries = [
        (sample.get("cgroups") or {}).get(cgroup)
        for sample in samples
        if isinstance(sample.get("cgroups"), dict) and cgroup in sample["cgroups"]
    ]
    valid = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("exists") and entry.get("usage_usec") is not None
    ]
    summary: Dict[str, Any] = {
        "path": next((entry.get("path") for entry in valid if entry.get("path")), cgroup),
        "observed": bool(valid),
        "sample_count": len(valid),
    }
    if not valid:
        return summary
    first, last = valid[0], valid[-1]
    usage_deltas = [
        float(cur["usage_usec"] - prev["usage_usec"])
        for prev, cur in zip(valid, valid[1:])
        if cur["usage_usec"] >= prev["usage_usec"]
    ]

    def _counter_delta(field: str) -> int:
        return max(0, int(last.get(field) or 0) - int(first.get(field) or 0))

    summary.update(
        {
            "usage_usec_total": max(0, last["usage_usec"] - first["usage_usec"]),
            "usage_usec_first": first["usage_usec"],
            "usage_usec_last": last["usage_usec"],
            "user_usec_total": _counter_delta("user_usec"),
            "system_usec_total": _counter_delta("system_usec"),
            "nr_periods_delta": _counter_delta("nr_periods"),
            "nr_throttled_delta": _counter_delta("nr_throttled"),
            "throttled_usec_delta": _counter_delta("throttled_usec"),
            "utilization_usec_per_sample": _stats(usage_deltas),
        }
    )
    return summary


def setup_logging(log_level: str = "INFO") -> None:
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"无效的日志级别: {log_level}")
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="系统 CPU/内存指标监测")
    parser.add_argument("--id", default="system_metric", help="场景ID")
    parser.add_argument("--name", default="系统指标监测", help="场景名称")
    parser.add_argument("--duration", type=float, default=5.0, help="监控持续时间(秒)，0 表示直到停止")
    parser.add_argument("--collection-mode", choices=["proc", "sched_agent", "auto"], default="proc")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="输出目录")
    parser.add_argument("--sample-interval", type=float, default=1.0, help="采样间隔(秒)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], default="INFO")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    setup_logging(args.log_level)
    scene = SystemMetricScene(
        args.id,
        args.name,
        {
            "duration": args.duration,
            "collection_mode": args.collection_mode,
            "output_dir": args.output_dir,
            "sample_interval": args.sample_interval,
        },
    )
    try:
        scene.setup()
        scene.prepare()
        scene.start()
        scene.collect()
        logger.info("结果文件路径: %s", scene.stat())
    finally:
        scene.cleanup()
        scene.teardown()
