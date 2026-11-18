"""
控制器模块

负责管理场景的生命周期和测试序列的执行。
"""

import importlib
import json
import logging
import time
import os
import shutil
import shlex
import sys
import tarfile
import traceback  # 添加traceback模块的导入
import subprocess  # 添加subprocess模块的导入
import tempfile
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, Future, wait, FIRST_COMPLETED, ALL_COMPLETED
from pathlib import Path
from typing import Any, Dict, Optional, Type, List, Set, Tuple

from .config import ConfigParser, ConfigurationError
from .scene.base_scene import Scene

SUDO_BIN = "/usr/bin/sudo"
BASH_BIN = "/bin/bash"
PROCESS_SCHEDULER_NAME = "EEVDF/default"
PROCESS_SCHEDULER_ALIASES = {
    "default",
    "eevdf",
    "eevdf/default",
    "process-default",
    "process_default",
}


class BenchmarkController:
    """Benchmark控制器类"""

    def __init__(self, config_path: str):
        """
        初始化Benchmark控制器

        Args:
            config_path: 配置文件路径
        """
        self.config_parser = ConfigParser(config_path)
        self.config: Dict[str, Any] = {}
        self.scene_classes: Dict[str, Type[Scene]] = {}  # 存储场景类，而不是实例
        self.results: Dict[str, Any] = {}
        self.current_sequence_id: Optional[str] = None
        self.results_base_dir = Path("results")
        self.sequence_results_dir: Optional[Path] = None
        self.custom_sequence_results_dir: Optional[Path] = None
        self.summary_output_path: Optional[Path] = None
        self.metadata: Dict[str, Any] = {"steps": []}
        # 添加场景失败状态追踪
        self.failed_scenes: Dict[
            str, Dict[str, Any]
        ] = {}  # {scene_id: {'error': 错误信息, 'phase': 失败阶段}}
        # 存储日志文件路径
        self.log_file_path: Optional[str] = None
        # 存储结果时间戳
        self.result_timestamp: Optional[str] = None

        # 序列级场景实例管理
        self.sequence_scene_instances: Dict[str, Scene] = {}  # 存储序列级别的场景实例
        self.sequence_active_scenes: Set[str] = set()  # 记录当前序列中需要使用的场景ID
        self.setup_failed_scenes: Set[str] = set()  # 记录setup失败的场景ID

        # 全局场景管理
        self.global_scene_id: Optional[str] = None  # 全局场景ID
        self.global_scene_instance: Optional[Scene] = None  # 全局场景实例
        self.global_scene_thread: Optional[threading.Thread] = None
        self.global_scene_enabled: bool = False  # 是否启用全局场景

        # 调度器管理相关属性
        # ioext-bench 的自定义算法替换的是块设备 IO scheduler；controller 不再启动
        # 外部“进程调度器”可执行文件。进程调度器保持 Linux 默认（EEVDF）。
        self.process_scheduler_name = PROCESS_SCHEDULER_NAME
        self.scheduler_name = self.process_scheduler_name  # 兼容旧 metadata 字段
        self.io_scheduler_originals: Dict[str, str] = {}
        self.io_scheduler_changes: List[Dict[str, str]] = []
        self.temp_askpass_script: Optional[str] = None

        # 断点续测相关属性
        self.resume_mode = False  # 是否处于续测模式
        self.resume_start_step_index = 0  # 续测起始步骤索引

        # 监控场景过滤器：None 表示全部启用，非空集合表示只启用匹配的监控场景
        self.monitoring_filter: Optional[Set[str]] = None

        # 配置日志
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        self.logger = logging.getLogger("BenchmarkController")

    @staticmethod
    def _parse_monitoring_filter(filter_str: Optional[str]) -> Optional[Set[str]]:
        if filter_str is None or filter_str.strip() == "" or filter_str.strip().lower() == "all":
            return None
        return {part.strip().lower() for part in filter_str.split(",") if part.strip()}

    def _monitoring_scene_allowed(self, scene_id: str) -> bool:
        if self.monitoring_filter is None:
            return True
        scene_id_lower = scene_id.lower()
        return any(keyword in scene_id_lower for keyword in self.monitoring_filter)

    def _configure_output_path(self, output_path: Optional[str]) -> None:
        """Classify --output as either a legacy JSON summary or a result directory."""

        self.custom_sequence_results_dir = None
        self.summary_output_path = None
        if not output_path:
            return

        output = Path(output_path)
        if output.suffix.lower() == ".json":
            self.summary_output_path = output
            return

        self.custom_sequence_results_dir = output

    def _validate_custom_sequence_results_dir(self, path: Path) -> None:
        """Fail fast before writing into an unsafe custom result directory."""

        if path.exists() and not path.is_dir():
            raise ConfigurationError(f"自定义结果路径已存在且不是目录: {path}")
        if path.exists() and any(path.iterdir()):
            raise ConfigurationError(
                f"自定义结果目录非空，拒绝混合多次运行结果: {path}. "
                "请删除该目录、换一个新目录，或使用 .json 后缀只保存汇总文件。"
            )

        archive_path = path.parent / f"{path.name}.tar.gz"
        if archive_path.exists():
            raise ConfigurationError(f"自定义结果归档已存在，拒绝覆盖: {archive_path}")

    def _relative_sequence_path(self, path: Path) -> str:
        """Return a portable path relative to the current sequence result directory."""

        try:
            return str(path.relative_to(self._require_sequence_results_dir()))
        except ValueError:
            return str(path)

    def _create_askpass_script(self) -> str:
        """Create a temporary sudo askpass helper that reads SUDO_PASSWORD."""

        fd, temp_askpass_script = tempfile.mkstemp(suffix=".sh")
        os.close(fd)
        with open(temp_askpass_script, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$SUDO_PASSWORD"\n')
        os.chmod(temp_askpass_script, 0o700)
        self.temp_askpass_script = temp_askpass_script
        self.logger.debug(f"创建临时SUDO_ASKPASS脚本: {temp_askpass_script}")
        return temp_askpass_script

    def _run_privileged_shell(self, script: str) -> subprocess.CompletedProcess[str]:
        """Run a small privileged shell snippet for block-device sysfs writes."""

        env = os.environ.copy()
        cmd = [SUDO_BIN]
        if env.get("SUDO_PASSWORD"):
            if not self.temp_askpass_script:
                self._create_askpass_script()
            env["SUDO_ASKPASS"] = self.temp_askpass_script or ""
            cmd.append("-A")
        cmd.extend([BASH_BIN, "-lc", script])
        return subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_privileged_tee(self, path: str, content: str) -> subprocess.CompletedProcess[str]:
        """Write privileged sysfs content through tee to avoid shell-redirection permission traps."""

        env = os.environ.copy()
        cmd = [SUDO_BIN]
        if env.get("SUDO_PASSWORD"):
            if not self.temp_askpass_script:
                self._create_askpass_script()
            env["SUDO_ASKPASS"] = self.temp_askpass_script or ""
            cmd.append("-A")
        cmd.extend(["tee", path])
        return subprocess.run(
            cmd,
            input=content,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def _is_process_scheduler_alias(scheduler: object) -> bool:
        """Return True for values that mean "keep Linux's process scheduler default"."""

        if scheduler is None:
            return True
        return str(scheduler).strip().lower() in PROCESS_SCHEDULER_ALIASES

    @staticmethod
    def _parse_current_io_scheduler(scheduler_file: str) -> Optional[str]:
        """Extract the active scheduler from /sys/block/<dev>/queue/scheduler."""

        for token in scheduler_file.split():
            if token.startswith("[") and token.endswith("]"):
                return token[1:-1]
        return None

    @staticmethod
    def _validate_block_device_name(target_device: str) -> str:
        """Validate a sysfs block-device basename before building /sys/block paths."""

        device = str(target_device).strip()
        if not device or device in {".", ".."} or "/" in device:
            raise ConfigurationError(f"无效块设备名称: {target_device!r}")
        return device

    def _read_io_scheduler(self, target_device: str) -> Optional[str]:
        device = self._validate_block_device_name(target_device)
        sched_path = f"/sys/block/{device}/queue/scheduler"
        result = self._run_privileged_shell(f"cat {shlex.quote(sched_path)}")
        if result.returncode != 0:
            raise RuntimeError(
                f"读取块设备 {device} 的 IO 调度器失败: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        return self._parse_current_io_scheduler(result.stdout)

    def _write_io_scheduler(self, target_device: str, scheduler: str) -> None:
        device = self._validate_block_device_name(target_device)
        sched_path = f"/sys/block/{device}/queue/scheduler"
        result = self._run_privileged_tee(sched_path, f"{scheduler}\n")
        if result.returncode != 0:
            raise RuntimeError(
                f"设置块设备 {device} 的 IO 调度器为 {scheduler!r} 失败: "
                f"{(result.stderr or result.stdout).strip()}"
            )

    def _set_io_scheduler(self, target_device: str, scheduler: str) -> None:
        """Switch one block device to the task-requested IO scheduler."""

        device = self._validate_block_device_name(target_device)
        if device not in self.io_scheduler_originals:
            original = self._read_io_scheduler(device)
            if original:
                self.io_scheduler_originals[device] = original

        current = self._read_io_scheduler(device)
        if current == scheduler:
            self.logger.info(f"块设备 {device} 的 IO 调度器已是 {scheduler}")
        else:
            self._write_io_scheduler(device, scheduler)
            self.logger.info(
                f"已将块设备 {device} 的 IO 调度器切换为 {scheduler}"
                "（注意：elevator 切换会把 nr_requests 等设备级队列参数重置为默认值）"
            )

        self.io_scheduler_changes = [
            change for change in self.io_scheduler_changes if change.get("target_device") != device
        ]
        self.io_scheduler_changes.append({"target_device": device, "scheduler": scheduler})
        self.metadata["io_schedulers"] = list(self.io_scheduler_changes)
        self.metadata["process_scheduler"] = self.process_scheduler_name
        self.metadata["scheduler"] = self.process_scheduler_name
        self._save_metadata()

    def _scene_io_scheduler_request(self, scene_id: str) -> Optional[Tuple[str, str]]:
        """Return the task-bound IO scheduler request for a scene, if present."""

        scene_config = self.config["scenes"].get(scene_id)
        if not scene_config:
            raise ConfigurationError(f"场景ID不存在: {scene_id}")
        parameters = scene_config.get("parameters", {}) or {}
        if not isinstance(parameters, dict):
            return None
        io_ext = parameters.get("io_ext") or {}
        if not isinstance(io_ext, dict):
            io_ext = {}

        scheduler = io_ext.get("scheduler", parameters.get("scheduler"))
        if scheduler is None or str(scheduler).strip() == "":
            return None
        if self._is_process_scheduler_alias(scheduler):
            self.logger.debug(
                f"场景 {scene_id} 请求 {scheduler!r}，保持进程调度器默认 {self.process_scheduler_name}"
            )
            return None

        target_device = io_ext.get("target_device", parameters.get("target_device"))
        if not target_device:
            raise ConfigurationError(f"场景 {scene_id} 设置了 IO 调度器 {scheduler!r} 但缺少 target_device")
        return self._validate_block_device_name(str(target_device)), str(scheduler).strip()

    def _apply_step_io_schedulers(self, step: Dict[str, Any]) -> None:
        """Apply task-bound block-device IO schedulers before a step runs."""

        requests: Dict[str, str] = {}
        all_step_scene_ids = list(step.get("scenes", [])) + list(step.get("monitoring_scenes", []))
        # 过滤掉被跳过的监控场景，避免为不会运行的场景切换调度器
        if self.monitoring_filter is not None:
            all_step_scene_ids = [
                sid for sid in all_step_scene_ids
                if sid in step.get("scenes", []) or self._monitoring_scene_allowed(sid)
            ]
        for scene_id in all_step_scene_ids:
            request = self._scene_io_scheduler_request(scene_id)
            if not request:
                continue
            target_device, scheduler = request
            existing = requests.get(target_device)
            if existing and existing != scheduler:
                raise ConfigurationError(
                    f"同一步骤中块设备 {target_device} 被请求切换到多个 IO 调度器: "
                    f"{existing!r} 与 {scheduler!r}"
                )
            requests[target_device] = scheduler

        if not requests:
            self.metadata["process_scheduler"] = self.process_scheduler_name
            self.metadata["scheduler"] = self.process_scheduler_name
            self.metadata.setdefault("io_schedulers", [])
            return

        for target_device, scheduler in requests.items():
            self._set_io_scheduler(target_device, scheduler)

    def _restore_io_schedulers(self) -> None:
        """Restore block-device IO schedulers changed by the controller."""

        if not self.io_scheduler_originals:
            return

        for target_device, original_scheduler in list(self.io_scheduler_originals.items()):
            try:
                self._write_io_scheduler(target_device, original_scheduler)
                self.logger.info(
                    f"已恢复块设备 {target_device} 的 IO 调度器为 {original_scheduler}"
                    "（注意：elevator 切换会重置 nr_requests 等设备级队列参数）"
                )
            except Exception as exc:
                self.logger.warning(f"恢复块设备 {target_device} 的 IO 调度器失败: {exc}")
        self.io_scheduler_originals.clear()

    def _cleanup_scheduler(self) -> None:
        """Compatibility wrapper: controller scheduler cleanup now restores IO schedulers."""

        self._restore_io_schedulers()
        if self.temp_askpass_script and os.path.exists(self.temp_askpass_script):
            os.remove(self.temp_askpass_script)
            self.logger.debug(f"已删除临时SUDO_ASKPASS脚本: {self.temp_askpass_script}")
        self.temp_askpass_script = None

    def _detect_and_load_resume_state(self, sequence_id: str) -> Optional[int]:
        """
        检测是否需要断点续测并加载续测状态

        Args:
            sequence_id: 测试序列ID

        Returns:
            Optional[int]: 续测起始步骤索引，如果不需要续测则返回None
        """
        # 扫描results目录查找匹配的未完成测试
        if not self.results_base_dir.exists():
            return None

        matching_dirs = []
        for result_dir in self.results_base_dir.iterdir():
            if result_dir.is_dir() and result_dir.name.startswith(f"{sequence_id}_"):
                metadata_file = result_dir / "metadata.json"
                if metadata_file.exists():
                    matching_dirs.append(result_dir)

        if not matching_dirs:
            return None

        # 选择时间戳最新的测试目录
        latest_dir = max(matching_dirs, key=lambda d: d.name.split("_")[-1])
        metadata_file = latest_dir / "metadata.json"

        # 尝试加载元数据
        try:
            with open(metadata_file, "r", encoding="utf-8") as f:
                existing_metadata = json.load(f)
        except Exception as e:
            self.logger.error(f"加载元数据文件失败，无法进行续测: {e}")
            sys.exit(1)

        # 获取当前配置的测试序列
        current_sequence = self.config_parser.get_test_sequence(sequence_id)
        if not current_sequence:
            return None

        # 检查当前配置是否包含全局场景，如果包含则禁用断点续测
        if "global_scene" in current_sequence:
            self.logger.info(
                f"检测到当前配置包含全局场景 {current_sequence['global_scene']}，禁用断点续测功能"
            )
            return None

        existing_steps = existing_metadata.get("steps", [])
        total_steps = len(current_sequence["steps"])

        # 检查是否已完成所有步骤
        if len(existing_steps) >= total_steps:
            # 检查是否有失败场景
            has_failed_scenes = False
            for step_meta in existing_steps:
                for scene_meta in step_meta.get("scenes", []):
                    if scene_meta.get("status") == "failed":
                        has_failed_scenes = True
                        break
                if has_failed_scenes:
                    break

            if not has_failed_scenes:
                self.logger.info(f"发现已完成的测试: {latest_dir.name}，无需续测")
                return None

        # 查找第一个包含失败场景的步骤
        resume_step_index = None
        for step_index, step_meta in enumerate(existing_steps):
            step_has_failed = False
            for scene_meta in step_meta.get("scenes", []):
                if scene_meta.get("status") == "failed":
                    step_has_failed = True
                    break
            if step_has_failed:
                resume_step_index = step_index
                break

        # 如果没有找到失败步骤但步骤数不足，从下一个步骤开始
        if resume_step_index is None and len(existing_steps) < total_steps:
            resume_step_index = len(existing_steps)

        if resume_step_index is not None:
            self.logger.info(f"检测到未完成的测试: {latest_dir.name}")
            self.logger.info(f"将从步骤 {resume_step_index} 开始续测")

            # 设置续测状态
            self.sequence_results_dir = latest_dir
            self.result_timestamp = latest_dir.name.split("_")[-1]
            self.metadata = existing_metadata
            self.failed_scenes = {}  # 重置失败场景追踪

            # 添加续测信息到元数据
            if "resume_info" not in self.metadata:
                self.metadata["resume_info"] = []
            self.metadata["resume_info"].append(
                {"resume_time": datetime.now().isoformat(), "resume_from_step": resume_step_index}
            )

            return resume_step_index

        return None

    def _identify_sequence_scenes(self, sequence: Dict[str, Any]) -> Set[str]:
        """
        识别测试序列中涉及的所有场景ID

        Args:
            sequence: 测试序列配置

        Returns:
            Set[str]: 序列中涉及的所有场景ID
        """
        scene_ids = set()
        for step in sequence["steps"]:
            scene_ids.update(step.get("scenes", []))
            # 只收集未被过滤掉的监控场景
            for msid in step.get("monitoring_scenes", []):
                if self._monitoring_scene_allowed(msid):
                    scene_ids.add(msid)
        return scene_ids

    def _setup_sequence_scenes(self, scene_ids: Set[str]) -> None:
        """
        为序列中的所有场景执行setup操作

        Args:
            scene_ids: 需要setup的场景ID集合
        """
        self.logger.info(f"开始为序列中的 {len(scene_ids)} 个场景执行setup")

        for scene_id in scene_ids:
            try:
                # 创建场景实例
                scene = self._create_scene_instance(scene_id)
                self.sequence_scene_instances[scene_id] = scene

                # 执行setup
                self.logger.info(f"执行场景 {scene_id} 的setup")
                scene.setup()
                self.logger.info(f"场景 {scene_id} setup完成")

            except Exception as e:
                self.logger.error(f"场景 {scene_id} setup失败: {str(e)}", exc_info=True)
                # 记录setup失败的场景
                self.setup_failed_scenes.add(scene_id)
                # 如果场景实例已创建，仍保留以便后续teardown
                if scene_id not in self.sequence_scene_instances:
                    try:
                        # 尝试创建实例以便teardown时清理
                        scene = self._create_scene_instance(scene_id)
                        self.sequence_scene_instances[scene_id] = scene
                    except Exception as create_error:
                        self.logger.error(f"为setup失败的场景 {scene_id} 创建实例也失败: {str(create_error)}")

        if self.setup_failed_scenes:
            self.logger.warning(
                f"共有 {len(self.setup_failed_scenes)} 个场景setup失败: {list(self.setup_failed_scenes)}"
            )
        else:
            self.logger.info("所有场景setup成功完成")

    def _teardown_sequence_scenes(self) -> None:
        """
        为序列中的所有场景执行teardown操作
        """
        if not self.sequence_scene_instances:
            return

        self.logger.info(f"开始为序列中的 {len(self.sequence_scene_instances)} 个场景执行teardown")

        for scene_id, scene in self.sequence_scene_instances.items():
            try:
                self.logger.info(f"执行场景 {scene_id} 的teardown")
                scene.teardown()
                self.logger.info(f"场景 {scene_id} teardown完成")
            except Exception as e:
                self.logger.error(f"场景 {scene_id} teardown失败: {str(e)}", exc_info=True)

        # 清理序列场景状态
        self.sequence_scene_instances.clear()
        self.sequence_active_scenes.clear()
        self.setup_failed_scenes.clear()

        self.logger.info("序列场景teardown完成")

    def _setup_global_scene(self, scene_id: str) -> bool:
        """
        设置并启动全局场景

        Args:
            scene_id: 全局场景ID

        Returns:
            bool: 是否成功启动全局场景
        """
        try:
            self.logger.info(f"开始设置全局场景: {scene_id}")

            # 创建全局场景实例
            self.global_scene_instance = self._create_scene_instance(scene_id)
            self.global_scene_id = scene_id

            # 执行setup
            self.logger.info(f"执行全局场景 {scene_id} 的setup")
            self.global_scene_instance.setup()

            # 执行prepare
            self.logger.info(f"执行全局场景 {scene_id} 的prepare")
            self.global_scene_instance.prepare()

            # 启动全局场景（在独立线程中运行）
            self.logger.info(f"启动全局场景 {scene_id}")

            # 为全局场景设置无限运行模式
            if hasattr(self.global_scene_instance, "duration"):
                setattr(self.global_scene_instance, "duration", 0)  # 设置为无限运行

            # 在独立线程中启动场景
            self.global_scene_thread = threading.Thread(
                target=self._run_global_scene, name=f"GlobalScene-{scene_id}"
            )
            self.global_scene_thread.daemon = True
            self.global_scene_thread.start()

            self.global_scene_enabled = True
            self.logger.info(f"全局场景 {scene_id} 启动成功")
            return True

        except Exception as e:
            self.logger.error(f"全局场景 {scene_id} 启动失败: {str(e)}", exc_info=True)
            # 清理部分初始化的状态
            self.global_scene_id = None
            self.global_scene_instance = None
            self.global_scene_enabled = False
            return False

    def _run_global_scene(self) -> None:
        """
        在独立线程中运行全局场景的辅助方法
        """
        if not self.global_scene_instance:
            return

        try:
            self.logger.info(f"全局场景 {self.global_scene_id} 开始运行")
            self.global_scene_instance.start()
            self.logger.info(f"全局场景 {self.global_scene_id} 运行完成")
        except Exception as e:
            self.logger.error(f"全局场景 {self.global_scene_id} 运行失败: {str(e)}", exc_info=True)

    def _stop_global_scene_runtime(self) -> None:
        """停止全局场景运行线程，但保留实例用于 collect/stat。"""

        if not self.global_scene_instance:
            return

        self.logger.info(f"停止全局场景运行: {self.global_scene_id}")
        self.global_scene_instance.stop()

        if self.global_scene_thread and self.global_scene_thread.is_alive():
            self.global_scene_thread.join(timeout=10)
            if self.global_scene_thread.is_alive():
                self.logger.warning("全局场景线程未在超时时间内结束")

    def _teardown_global_scene(self, stop_runtime: bool = True) -> None:
        """
        停止并清理全局场景
        """
        if not self.global_scene_enabled or not self.global_scene_instance:
            return

        try:
            self.logger.info(f"开始停止全局场景: {self.global_scene_id}")

            if stop_runtime:
                self._stop_global_scene_runtime()

            # 执行cleanup
            self.logger.info(f"执行全局场景 {self.global_scene_id} 的cleanup")
            self.global_scene_instance.cleanup()

            # 执行teardown
            self.logger.info(f"执行全局场景 {self.global_scene_id} 的teardown")
            self.global_scene_instance.teardown()

            self.logger.info(f"全局场景 {self.global_scene_id} teardown完成")

        except Exception as e:
            self.logger.error(f"全局场景 {self.global_scene_id} teardown失败: {str(e)}", exc_info=True)
        finally:
            # 清理全局场景状态
            self.global_scene_id = None
            self.global_scene_instance = None
            self.global_scene_enabled = False
        self.global_scene_thread = None

    def _collect_global_scene_data(self, global_start_time: float, global_end_time: float) -> Dict[str, Any]:
        """
        收集全局场景的元数据和结果文件

        Args:
            global_start_time: 全局场景开始时间戳
            global_end_time: 全局场景结束时间戳

        Returns:
            Dict[str, Any]: 全局场景元数据
        """
        if not self.global_scene_instance or not self.global_scene_id:
            return {}

        try:
            sequence_results_dir = self._require_sequence_results_dir()

            # 获取场景配置
            scene_config = self.config["scenes"][self.global_scene_id]

            # 确保全局场景结果目录存在
            global_dir = sequence_results_dir / "global"
            global_dir.mkdir(exist_ok=True)

            scene_result_dir = global_dir / self.global_scene_id
            scene_result_dir.mkdir(exist_ok=True)
            copied_files: List[Dict[str, str]] = []
            missing_files: List[str] = []

            # 收集场景数据
            collected_data = {}
            try:
                collected_data = self.global_scene_instance.collect()
            except Exception as e:
                self.logger.error(f"全局场景 {self.global_scene_id} 数据收集失败: {str(e)}")
                collected_data = {"error": str(e), "status": "failed"}

            # 保存场景结果文件
            try:
                stat_result = self.global_scene_instance.stat()

                # 处理stat返回的结果（支持单个文件或文件列表）
                files_to_copy = []
                if isinstance(stat_result, str):
                    # 单个文件路径
                    files_to_copy = [stat_result]
                elif isinstance(stat_result, (list, tuple)):
                    # 文件路径列表
                    files_to_copy = list(stat_result)
                elif stat_result is not None:
                    self.logger.warning(
                        f"全局场景 {self.global_scene_id} 的stat方法返回了不支持的类型: {type(stat_result)}"
                    )

                # 复制所有文件到结果目录
                for file_path in files_to_copy:
                    if file_path and os.path.exists(file_path):
                        target_file = scene_result_dir / os.path.basename(file_path)
                        # 如果目标文件已存在，添加数字后缀避免覆盖
                        counter = 1
                        original_target = target_file
                        while target_file.exists():
                            stem = original_target.stem
                            suffix = original_target.suffix
                            target_file = scene_result_dir / f"{stem}_{counter}{suffix}"
                            counter += 1

                        shutil.copy2(file_path, target_file)
                        copied_files.append(
                            {
                                "original_path": str(file_path),
                                "result_path": str(target_file),
                                "relative_path": self._relative_sequence_path(target_file),
                            }
                        )
                        self.logger.info(
                            f"已保存全局场景 {self.global_scene_id} 结果文件: {os.path.basename(file_path)} -> {target_file.name}"
                        )
                    elif file_path:
                        missing_files.append(str(file_path))
                        self.logger.warning(f"全局场景 {self.global_scene_id} 返回的文件不存在: {file_path}")

                if copied_files:
                    self.logger.info(
                        f"全局场景 {self.global_scene_id} 共保存了 {len(copied_files)} 个结果文件"
                    )

            except Exception as e:
                self.logger.error(f"保存全局场景 {self.global_scene_id} 结果文件失败: {str(e)}")

            # 构建全局场景元数据
            global_scene_metadata = {
                "scene_id": self.global_scene_id,
                "name": scene_config.get("name", self.global_scene_id),
                "type": scene_config.get("type", "general"),
                "start_time": datetime.fromtimestamp(global_start_time).isoformat(),
                "end_time": datetime.fromtimestamp(global_end_time).isoformat(),
                "duration": round(global_end_time - global_start_time, 2),
                "status": "success" if "error" not in collected_data else "failed",
            }
            if copied_files:
                global_scene_metadata["result_files"] = copied_files
            if missing_files:
                global_scene_metadata["missing_result_files"] = missing_files

            # 如果收集数据失败，添加错误信息
            if "error" in collected_data:
                global_scene_metadata["error"] = collected_data["error"]

            return global_scene_metadata

        except Exception as e:
            self.logger.error(f"全局场景 {self.global_scene_id} 元数据收集失败: {str(e)}")
            return {"scene_id": self.global_scene_id, "status": "failed", "error": str(e)}

    def load_config(self) -> None:
        """
        加载并解析配置文件

        Raises:
            ConfigurationError: 配置文件解析错误
        """
        self.config = self.config_parser.parse()
        self.logger.info(f"已加载配置: {self.config['benchmark_name']}")

    def _load_scene_class(self, module_path: str, class_name: str) -> Type[Scene]:
        """
        动态加载场景类

        Args:
            module_path: 模块路径
            class_name: 类名

        Returns:
            Type[Scene]: 场景类

        Raises:
            ImportError: 模块导入失败
            AttributeError: 类不存在
        """
        try:
            module = importlib.import_module(module_path)
            scene_class = getattr(module, class_name)

            if not issubclass(scene_class, Scene):
                raise TypeError(f"{class_name} 不是 Scene 的子类")

            return scene_class

        except ImportError as e:
            raise ImportError(f"无法导入模块 {module_path}: {str(e)}")
        except AttributeError:
            raise AttributeError(f"类 {class_name} 在模块 {module_path} 中不存在")

    def load_scenes(self) -> None:
        """
        加载所有场景类（不创建实例）

        Raises:
            ConfigurationError: 场景加载失败
        """
        for scene_id, scene_config in self.config["scenes"].items():
            try:
                scene_class = self._load_scene_class(scene_config["module_path"], scene_config["class_name"])

                self.scene_classes[scene_id] = scene_class
                self.logger.info(f"已加载场景类: {scene_id}")

            except (ImportError, AttributeError, TypeError) as e:
                raise ConfigurationError(f"加载场景 {scene_id} 失败: {str(e)}")

    def _require_sequence_results_dir(self) -> Path:
        if self.sequence_results_dir is None:
            raise RuntimeError("结果目录尚未初始化")
        return self.sequence_results_dir

    def _init_results_directory(self, sequence_id: str) -> None:
        """
        初始化结果目录结构（支持续测模式）

        Args:
            sequence_id: 测试序列ID
        """
        # 如果不是续测模式，创建新的结果目录
        if not self.resume_mode:
            # 生成时间戳，格式为yyyyMMdd_HHmmss
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # 存储时间戳供后续使用
            self.result_timestamp = timestamp

            if self.custom_sequence_results_dir is not None:
                self._validate_custom_sequence_results_dir(self.custom_sequence_results_dir)
                self.sequence_results_dir = self.custom_sequence_results_dir
                self.results_base_dir = self.sequence_results_dir.parent
            else:
                # 使用时间戳创建结果目录，格式为: results/sequence_id_yyyyMMdd_HHmmss
                self.sequence_results_dir = self.results_base_dir / f"{sequence_id}_{timestamp}"

                # 如果目录已存在，先清空。默认时间戳目录基本不会撞名，保留旧行为以兼容历史续测清理逻辑。
                if self.sequence_results_dir.exists():
                    shutil.rmtree(self.sequence_results_dir)

            # 创建结果目录
            self.sequence_results_dir.mkdir(parents=True, exist_ok=True)
            self.logger.info(f"已创建结果目录: {self.sequence_results_dir}")

            # 重置失败场景跟踪
            self.failed_scenes = {}

            # 初始化元数据
            sequence = self.config_parser.get_test_sequence(sequence_id)
            if sequence is None:
                raise ConfigurationError(f"测试序列不存在: {sequence_id}")
            self.metadata = {
                "benchmark_name": self.config.get("benchmark_name", "未命名基准测试"),
                "sequence_id": sequence_id,
                "sequence_name": sequence.get("name", sequence_id),
                "execution_time": datetime.now().isoformat(),
                "timestamp": timestamp,
                "results_dir": str(self.sequence_results_dir),
                "output_mode": "custom_directory" if self.custom_sequence_results_dir else "default_timestamped_directory",
                # 进程调度器保持 Linux 默认（EEVDF）；task 中的 scheduler 表示块设备 IO scheduler。
                "scheduler": self.scheduler_name,
                "process_scheduler": self.process_scheduler_name,
                "io_schedulers": [],
                "steps": [],
            }
        else:
            sequence_results_dir = self._require_sequence_results_dir()
            # 续测模式：目录和元数据已在_detect_and_load_resume_state中设置
            self.logger.info(f"续测模式：使用现有结果目录: {self.sequence_results_dir}")

            # 清理需要重新执行的步骤的目录和元数据
            steps_to_remove = []
            for i in range(self.resume_start_step_index, len(self.metadata.get("steps", []))):
                step_dir = sequence_results_dir / f"step_{i}"
                if step_dir.exists():
                    shutil.rmtree(step_dir)
                    self.logger.info(f"已清理步骤目录: {step_dir}")
                steps_to_remove.append(i)

            # 从元数据中移除需要重新执行的步骤
            if steps_to_remove:
                self.metadata["steps"] = self.metadata["steps"][: self.resume_start_step_index]
                self.logger.info(f"已从元数据中移除步骤 {self.resume_start_step_index} 及之后的数据")

    def _create_scene_instance(self, scene_id: str) -> Scene:
        """
        创建场景实例

        Args:
            scene_id: 场景ID

        Returns:
            Scene: 场景实例

        Raises:
            ConfigurationError: 创建场景实例失败
        """
        if scene_id not in self.scene_classes:
            raise ConfigurationError(f"场景类不存在: {scene_id}")

        scene_config = self.config["scenes"][scene_id]
        scene_class = self.scene_classes[scene_id]

        try:
            scene = scene_class(
                scene_id=scene_id,
                scene_name=scene_config.get("name", scene_id),
                parameters=scene_config.get("parameters", {}),
            )
            return scene
        except Exception as e:
            raise ConfigurationError(f"创建场景 {scene_id} 实例失败: {str(e)}")

    def _execute_step(self, step: Dict[str, Any], step_index: int) -> Dict[str, Any]:
        """
        执行单个测试步骤

        Args:
            step: 步骤配置字典
            step_index: 步骤索引

        Returns:
            Dict[str, Any]: 步骤执行结果
        """
        step_results = {}
        scene_ids = step.get("scenes", [])
        monitoring_scene_ids = step.get("monitoring_scenes", [])  # 获取监控场景ID列表
        # 应用监控场景过滤器：只保留匹配关键字的监控场景
        if self.monitoring_filter is not None:
            filtered_monitoring = [sid for sid in monitoring_scene_ids if self._monitoring_scene_allowed(sid)]
            skipped = set(monitoring_scene_ids) - set(filtered_monitoring)
            if skipped:
                self.logger.info(f"监控场景过滤器跳过: {sorted(skipped)}")
            monitoring_scene_ids = filtered_monitoring
        # 获取监控场景预启动时间（默认为3秒）
        monitoring_startup_time = step.get("monitoring_startup_time", 3)
        step_start_time = time.time()

        # 获取基准场景ID（如果设置）
        primary_scene_id = step.get("primary_scene")
        if primary_scene_id:
            if primary_scene_id not in scene_ids:
                self.logger.warning(
                    f"指定的基准场景 {primary_scene_id} 不在当前步骤的场景列表中，将忽略基准场景设置"
                )
                primary_scene_id = None
            elif primary_scene_id in monitoring_scene_ids:
                self.logger.warning(
                    f"基准场景 {primary_scene_id} 是监控场景，不建议将监控场景作为基准场景，但将继续执行"
                )
            else:
                self.logger.info(f"已设置基准场景: {primary_scene_id}，当此场景完成时其他执行场景将被停止")

        # 验证监控场景是否都在scenes列表中
        for scene_id in monitoring_scene_ids:
            if scene_id not in scene_ids:
                self.logger.info(f"监控场景 {scene_id} 已合并到场景列表")
                scene_ids.append(scene_id)

        # 创建步骤目录
        step_dir = self._require_sequence_results_dir() / f"step_{step_index}"
        step_dir.mkdir(exist_ok=True)

        # 为本步骤获取场景实例（从序列级实例中获取或跳过setup失败的场景）
        scenes = {}
        for scene_id in scene_ids:
            if scene_id in self.setup_failed_scenes:
                self.logger.warning(f"跳过setup失败的场景: {scene_id}")
                # 将失败信息记录到failed_scenes中，以便后续处理
                self.failed_scenes[scene_id] = {
                    "error": "Scene setup failed",
                    "phase": "setup",
                    "traceback": "Setup failed during sequence initialization",
                }
                continue

            if scene_id in self.sequence_scene_instances:
                scenes[scene_id] = self.sequence_scene_instances[scene_id]
            else:
                self.logger.error(f"场景 {scene_id} 未在序列实例中找到，跳过执行")
                self.failed_scenes[scene_id] = {
                    "error": "Scene instance not found in sequence instances",
                    "phase": "instance_lookup",
                    "traceback": "Scene was not properly initialized in sequence setup",
                }

        # 更新场景列表，移除跳过的场景
        scene_ids = [scene_id for scene_id in scene_ids if scene_id in scenes]
        monitoring_scene_ids = [scene_id for scene_id in monitoring_scene_ids if scene_id in scenes]

        # 如果没有任何场景需要执行，直接返回
        if not scene_ids:
            self.logger.warning("步骤中没有可执行的场景，跳过执行")
            step_metadata = {
                "step_index": step_index,
                "name": step.get("name", f"步骤 {step_index}"),
                "description": step.get("description", ""),
                "start_time": datetime.fromtimestamp(step_start_time).isoformat(),
                "end_time": datetime.fromtimestamp(time.time()).isoformat(),
                "duration": round(time.time() - step_start_time, 2),
                "scenes": [],
            }
            self.metadata["steps"].append(step_metadata)
            self._save_metadata()
            return {}

        # 为监控场景设置duration=0，确保它们会一直运行直到收到停止信号
        for scene_id in monitoring_scene_ids:
            if scene_id in scenes:
                if hasattr(scenes[scene_id], "duration"):
                    self.logger.info(f"为监控场景 {scene_id} 设置duration=0，将持续到收到停止信号")
                    scenes[scene_id].duration = 0
                else:
                    self.logger.warning(f"监控场景 {scene_id} 没有duration属性，无法设置为无限等待模式")
                # Sync warmup_time with monitoring_startup_time so monitoring scenes
                # label pre-workload samples as "warmup" instead of "during"
                if hasattr(scenes[scene_id], "warmup_time"):
                    scenes[scene_id].warmup_time = float(monitoring_startup_time)
                    self.logger.info(f"为监控场景 {scene_id} 设置warmup_time={monitoring_startup_time}秒")

        # Subjob mode: limit fio_jobs to step's tenant_count if specified
        step_tenant_count = step.get("tenant_count")
        if step_tenant_count is not None:
            for scene_id in scene_ids:
                scene = scenes.get(scene_id)
                if scene is not None and hasattr(scene, "fio_subjob_mode") and scene.fio_subjob_mode:
                    if hasattr(scene, "fio_jobs") and scene.fio_jobs:
                        if len(scene.fio_jobs) > step_tenant_count:
                            scene.fio_jobs = scene.fio_jobs[:step_tenant_count]
                            scene.tenant_count = step_tenant_count
                            if hasattr(scene, "_subjob_cgroup_paths"):
                                scene._subjob_cgroup_paths = scene._subjob_cgroup_paths[:step_tenant_count]
                            if hasattr(scene, "fio_subjob_tenants"):
                                scene.fio_subjob_tenants = scene.fio_subjob_tenants[:step_tenant_count]
                    self.logger.info(f"subjob 场景 {scene_id} 限制为前 {step_tenant_count} 个 tenant")

        # 准备阶段 - 所有场景都需要准备
        prepare_failures = []
        for scene_id in scene_ids:
            success = self._prepare_scene(scenes[scene_id])
            # 如果场景准备失败，记录日志但继续执行其他场景
            if not success:
                self.logger.warning(f"场景 {scene_id} 准备失败，但将继续执行其他场景")
                prepare_failures.append(scene_id)

        if prepare_failures and self.config.get("fail_fast_on_scene_error", False):
            self._save_metadata()
            raise RuntimeError(f"scene prepare failed: {', '.join(prepare_failures)}")

        # 启动阶段 - 分两步：先启动监控场景，等待一段时间，再启动执行场景
        futures: List[Tuple[str, Future[Any]]] = []
        scene_times: Dict[str, Tuple[float, Optional[float]]] = {}  # {scene_id: (start_time, end_time)}

        with ThreadPoolExecutor(max_workers=len(scene_ids)) as executor:
            # 第一步：启动所有监控场景
            monitoring_futures = []
            if monitoring_scene_ids:
                self.logger.info(f"先启动监控场景，预启动时间为 {monitoring_startup_time} 秒")
                for scene_id in monitoring_scene_ids:
                    scene_start_time = time.time()
                    scene_times[scene_id] = (scene_start_time, None)  # 初始化结束时间为None
                    future = executor.submit(self._run_scene, scenes[scene_id])
                    monitoring_futures.append((scene_id, future))
                    futures.append((scene_id, future))
                    self.logger.info(f"已提交监控场景 {scene_id} 执行")

                # 等待预启动时间
                self.logger.info(f"等待监控场景预启动时间 {monitoring_startup_time} 秒...")
                time.sleep(monitoring_startup_time)
                self.logger.info("监控场景预启动时间结束，开始启动执行场景")

            # 第二步：启动所有执行场景
            execution_futures = []
            for scene_id in scene_ids:
                if scene_id not in monitoring_scene_ids:  # 只启动非监控场景
                    scene_start_time = time.time()
                    scene_times[scene_id] = (scene_start_time, None)  # 初始化结束时间为None
                    future = executor.submit(self._run_scene, scenes[scene_id])
                    execution_futures.append((scene_id, future))
                    futures.append((scene_id, future))
                    self.logger.info(f"已提交执行场景 {scene_id} 执行")

            # 等待场景执行完成或达到超时时间
            completed_scene_ids: Set[str] = set()
            primary_scene_completed = False  # 跟踪基准场景是否已完成

            # 获取步骤持续时间作为超时
            step_duration = step.get("duration")
            if step_duration:
                self.logger.info(f"步骤最长持续时间: {step_duration} 秒")

                # 计算超时时间点（从所有场景都启动后开始计时）
                timeout_time = time.time() + step_duration

                # 循环等待直到所有执行场景完成或者达到超时时间或基准场景完成
                while execution_futures and time.time() < timeout_time and not primary_scene_completed:
                    # 计算剩余时间
                    remaining_time = max(0, timeout_time - time.time())

                    # 等待下一个完成的执行场景或超时
                    pending_execution_future_objects = [future for _, future in execution_futures]
                    done, _ = wait(
                        pending_execution_future_objects,
                        timeout=min(10, remaining_time),  # 每10秒检查一次状态，或者等待剩余时间
                        return_when=FIRST_COMPLETED,
                    )

                    # 处理已完成的执行场景
                    new_execution_futures = []
                    for scene_id, future in execution_futures:
                        if future in done:
                            scene_end_time = time.time()
                            scene_times[scene_id] = (scene_times[scene_id][0], scene_end_time)
                            completed_scene_ids.add(scene_id)
                            self.logger.info(f"执行场景 {scene_id} 已完成")

                            # 检查是否是基准场景完成
                            if primary_scene_id and scene_id == primary_scene_id:
                                self.logger.info(f"基准场景 {primary_scene_id} 已完成，将停止其他执行场景")
                                primary_scene_completed = True
                        else:
                            new_execution_futures.append((scene_id, future))

                    execution_futures = new_execution_futures

                    # 如果有场景完成，打印日志
                    if done:
                        self.logger.info(
                            f"已有 {len(completed_scene_ids)}/{len(scene_ids) - len(monitoring_scene_ids)} 个执行场景完成"
                        )

                    # 如果基准场景完成，停止所有其他执行场景
                    if primary_scene_completed and execution_futures:
                        self.logger.info(f"基准场景已完成，停止剩余的 {len(execution_futures)} 个执行场景")
                        for scene_id, future in execution_futures:
                            # 立即调用stop方法进行资源清理
                            success = self._stop_scene(scenes[scene_id])
                            if success:
                                self.logger.info(
                                    f"已调用执行场景 {scene_id} 的stop方法（由于基准场景已完成）"
                                )
                            else:
                                self.logger.warning(f"调用执行场景 {scene_id} 的stop方法失败，但将继续处理")

                            # 给场景一些时间来响应停止信号
                            time.sleep(1)

                            # 尝试取消未完成的Future
                            future.cancel()

                            # 设置结束时间为当前时间
                            scene_times[scene_id] = (scene_times[scene_id][0], time.time())
                            completed_scene_ids.add(scene_id)

                        # 清空执行场景队列
                        execution_futures = []

                # 如果超时但仍有执行场景未完成，记录警告并强制结束它们
                if execution_futures and not primary_scene_completed:
                    self.logger.warning(
                        f"达到最大运行时间 {step_duration} 秒，但仍有 {len(execution_futures)} 个执行场景未完成，"
                        f"将强制结束这些场景"
                    )
                    # 对于未完成的执行场景，立即调用stop方法并取消Future
                    for scene_id, future in execution_futures:
                        # 立即调用stop方法进行资源清理
                        success = self._stop_scene(scenes[scene_id])
                        if success:
                            self.logger.info(f"已调用超时执行场景 {scene_id} 的stop方法")
                        else:
                            self.logger.warning(f"调用超时执行场景 {scene_id} 的stop方法失败，但将继续处理")

                        # 给场景一些时间来响应停止信号
                        time.sleep(1)

                        # 尝试取消未完成的Future
                        future.cancel()

                        # 设置结束时间为当前时间
                        scene_times[scene_id] = (scene_times[scene_id][0], time.time())
                        completed_scene_ids.add(scene_id)

                # 所有执行场景完成后（或超时），结束所有监控场景
                self.logger.info("所有执行场景已完成或超时，现在结束监控场景")
                for scene_id, future in monitoring_futures:
                    # 调用stop方法结束监控场景
                    success = self._stop_scene(scenes[scene_id])
                    if success:
                        self.logger.info(f"已调用监控场景 {scene_id} 的stop方法")
                    else:
                        self.logger.warning(f"调用监控场景 {scene_id} 的stop方法失败，但将继续处理")

                    # 给场景一些时间来响应停止信号
                    time.sleep(1)

                    # 尝试取消未完成的Future
                    future.cancel()

                    # 设置监控场景的结束时间
                    scene_times[scene_id] = (scene_times[scene_id][0], time.time())
                    completed_scene_ids.add(scene_id)
            else:
                # 如果没有设置duration，等待所有执行场景完成或基准场景完成
                if primary_scene_id:
                    self.logger.info(f"未设置最大运行时间，等待基准场景 {primary_scene_id} 完成")
                else:
                    self.logger.info("未设置最大运行时间，等待所有执行场景完成")

                if execution_futures:
                    # 如果设置了基准场景，需要在循环中检查基准场景是否完成
                    if primary_scene_id:
                        primary_future = None
                        # 查找基准场景对应的future
                        for scene_id, future in execution_futures:
                            if scene_id == primary_scene_id:
                                primary_future = future
                                break

                        if primary_future:
                            # 只等待基准场景完成
                            wait([primary_future], return_when=ALL_COMPLETED)

                            # 设置基准场景结束时间
                            scene_times[primary_scene_id] = (scene_times[primary_scene_id][0], time.time())
                            completed_scene_ids.add(primary_scene_id)
                            self.logger.info(f"基准场景 {primary_scene_id} 已完成，将停止其他执行场景")

                            # 停止其他执行场景
                            for scene_id, future in execution_futures:
                                if scene_id != primary_scene_id:  # 跳过基准场景
                                    # 立即调用stop方法进行资源清理
                                    success = self._stop_scene(scenes[scene_id])
                                    if success:
                                        self.logger.info(
                                            f"已调用执行场景 {scene_id} 的stop方法（由于基准场景已完成）"
                                        )
                                    else:
                                        self.logger.warning(
                                            f"调用执行场景 {scene_id} 的stop方法失败，但将继续处理"
                                        )

                                    # 给场景一些时间来响应停止信号
                                    time.sleep(1)

                                    # 尝试取消未完成的Future
                                    future.cancel()

                                    # 设置结束时间为当前时间
                                    scene_times[scene_id] = (scene_times[scene_id][0], time.time())
                                    completed_scene_ids.add(scene_id)
                        else:
                            self.logger.warning(
                                f"未找到基准场景 {primary_scene_id} 对应的执行任务，将等待所有场景完成"
                            )
                            # 等待所有执行场景完成
                            execution_future_objects = [future for _, future in execution_futures]
                            wait(execution_future_objects, return_when=ALL_COMPLETED)

                            # 设置所有执行场景的结束时间
                            for scene_id, _ in execution_futures:
                                if scene_times[scene_id][1] is None:  # 如果还没有设置结束时间
                                    scene_times[scene_id] = (scene_times[scene_id][0], time.time())
                                completed_scene_ids.add(scene_id)
                    else:
                        # 等待所有执行场景完成
                        execution_future_objects = [future for _, future in execution_futures]
                        wait(execution_future_objects, return_when=ALL_COMPLETED)

                        # 设置所有执行场景的结束时间
                        for scene_id, _ in execution_futures:
                            if scene_times[scene_id][1] is None:  # 如果还没有设置结束时间
                                scene_times[scene_id] = (scene_times[scene_id][0], time.time())
                            completed_scene_ids.add(scene_id)

                # 所有执行场景完成后，结束所有监控场景
                self.logger.info("所有执行场景已完成，现在结束监控场景")
                for scene_id, future in monitoring_futures:
                    # 调用stop方法结束监控场景
                    success = self._stop_scene(scenes[scene_id])
                    if success:
                        self.logger.info(f"已调用监控场景 {scene_id} 的stop方法")
                    else:
                        self.logger.warning(f"调用监控场景 {scene_id} 的stop方法失败，但将继续处理")

                    # 给场景一些时间来响应停止信号
                    time.sleep(3)

                    # 尝试取消未完成的Future
                    future.cancel()

                    # 设置监控场景的结束时间
                    scene_times[scene_id] = (scene_times[scene_id][0], time.time())
                    completed_scene_ids.add(scene_id)

        # 收集场景数据和元数据
        step_metadata: Dict[str, Any] = {
            "step_index": step_index,
            "name": step.get("name", f"步骤 {step_index}"),
            "description": step.get("description", ""),
            "start_time": datetime.fromtimestamp(step_start_time).isoformat(),
            "end_time": datetime.fromtimestamp(time.time()).isoformat(),
            "duration": round(time.time() - step_start_time, 2),
            "scenes": [],
        }

        # 收集每个场景的数据
        for scene_id in scene_ids:
            scene_start_time, scene_end_time = scene_times[scene_id]
            if scene_end_time is None:
                scene_end_time = time.time()
                scene_times[scene_id] = (scene_start_time, scene_end_time)
            scene_metadata = self._collect_scene_data(
                scenes[scene_id], scene_id, step_index, step_start_time, scene_start_time, scene_end_time
            )

            # 安全地收集场景数据
            try:
                if scene_id not in self.failed_scenes:
                    step_results[scene_id] = scenes[scene_id].collect()
                else:
                    step_results[scene_id] = {
                        "error": self.failed_scenes[scene_id]["error"],
                        "phase": self.failed_scenes[scene_id]["phase"],
                        "status": "failed",
                    }
            except Exception as e:
                self.logger.error(f"场景 {scene_id} 收集数据失败: {str(e)}")
                step_results[scene_id] = {"error": str(e), "phase": "collect", "status": "failed"}
                if scene_id not in self.failed_scenes:
                    self.failed_scenes[scene_id] = {
                        "error": str(e),
                        "phase": "collect",
                        "traceback": traceback.format_exc(),
                    }

            # 添加场景元数据
            step_metadata["scenes"].append(scene_metadata)

        # 清理资源
        for scene_id in scene_ids:
            success = self._cleanup_scene(scenes[scene_id])
            # 如果场景清理失败，记录日志但继续执行其他场景的清理
            if not success:
                self.logger.warning(f"场景 {scene_id} 清理失败，但将继续清理其他场景")

        # 将步骤元数据添加到序列元数据中
        self.metadata["steps"].append(step_metadata)

        # 保存当前步骤的元数据（实时更新）
        self._save_metadata()

        # 额外安全检查：检查所有场景的线程执行状态
        # 注意：由于我们已经修改了Scene.start()方法来传播异常，这个检查主要是为了防止极少数情况下的异常未被捕获
        for scene_id, future in futures:
            if scene_id not in self.failed_scenes and future.done() and not future.cancelled():
                try:
                    # 尝试获取结果，如果有异常会抛出
                    future.result()
                except Exception as e:
                    self.logger.error(
                        f"场景 {scene_id} 线程执行中出现异常 (额外捕获): {str(e)}", exc_info=True
                    )
                    self.failed_scenes[scene_id] = {
                        "error": str(e),
                        "phase": "thread_execution",
                        "traceback": traceback.format_exc(),
                    }

        return step_results

    def _run_scene(self, scene: Scene) -> bool:
        """
        运行单个场景的辅助方法

        Args:
            scene: 场景实例

        Returns:
            bool: 场景是否成功运行
        """
        # 保存当前工作目录
        original_dir = os.getcwd()
        success = True
        try:
            scene_id = scene.scene_id
            self.logger.info(f"开始运行场景: {scene_id}")
            scene.start()
            self.logger.info(f"场景 {scene_id} 运行完成")
        except Exception as e:
            scene_id = scene.scene_id
            self.logger.error(f"场景 {scene_id} 运行失败: {str(e)}", exc_info=True)
            # 记录失败信息但不抛出异常
            self.failed_scenes[scene_id] = {
                "error": str(e),
                "phase": "run",
                "traceback": traceback.format_exc(),
            }
            success = False
        finally:
            # 无论场景执行成功或失败，都恢复原始工作目录
            current_dir = os.getcwd()
            if current_dir != original_dir:
                self.logger.warning(
                    f"场景 {scene.scene_id} 改变了工作目录，从 {original_dir} 到 {current_dir}，正在恢复..."
                )
                os.chdir(original_dir)
                self.logger.info(f"已恢复工作目录到: {original_dir}")
        return success

    def _save_metadata(self) -> None:
        """
        保存元数据到文件
        """
        if self.sequence_results_dir:
            # 添加失败场景摘要
            if self.failed_scenes:
                self.metadata["failed_scenes"] = {
                    scene_id: {"error": info["error"], "phase": info["phase"]}
                    for scene_id, info in self.failed_scenes.items()
                }
            else:
                # 如果没有失败场景，确保此字段存在且为空
                self.metadata["failed_scenes"] = {}

            metadata_file = self.sequence_results_dir / "metadata.json"
            with open(metadata_file, "w", encoding="utf-8") as f:
                json.dump(self.metadata, f, indent=2, ensure_ascii=False)
            self.logger.info(f"已更新元数据文件: {metadata_file}")

    def _create_archive(self) -> str:
        """
        创建打包文件

        Returns:
            str: 打包文件路径
        """
        if not self.sequence_results_dir or not self.current_sequence_id:
            raise ValueError("未初始化结果目录或序列ID")

        if self.custom_sequence_results_dir is not None:
            archive_path = self.sequence_results_dir.parent / f"{self.sequence_results_dir.name}.tar.gz"
            archive_root_name = self.sequence_results_dir.name
        else:
            # 使用与结果目录相同的时间戳命名压缩包
            archive_path = self.results_base_dir / f"{self.current_sequence_id}_{self.result_timestamp}.tar.gz"
            archive_root_name = f"{self.current_sequence_id}_{self.result_timestamp}"

        with tarfile.open(archive_path, "w:gz") as tar:
            # 添加结果目录，使用当前序列ID作为归档内目录名
            tar.add(self.sequence_results_dir, arcname=archive_root_name)

            # 添加日志文件(如果存在)
            if self.log_file_path and Path(self.log_file_path).exists():
                self.logger.info(f"将日志文件 {self.log_file_path} 添加到打包结果中")
                logs_dir_in_archive = f"{archive_root_name}/logs"
                # 在打包内创建logs目录
                logs_info = tarfile.TarInfo(logs_dir_in_archive)
                logs_info.type = tarfile.DIRTYPE
                logs_info.mode = 0o755
                logs_info.mtime = int(time.time())
                tar.addfile(logs_info)

                # 添加日志文件
                log_filename = Path(self.log_file_path).name
                log_arcname = f"{logs_dir_in_archive}/{log_filename}"
                tar.add(self.log_file_path, arcname=log_arcname)

        self.logger.info(f"已创建打包文件: {archive_path}")
        return str(archive_path)

    def run_sequence(self, sequence_id: Optional[str] = None) -> None:
        """
        运行测试序列

        Args:
            sequence_id: 要运行的测试序列ID，如果为None则运行默认序列

        Raises:
            ConfigurationError: 测试序列不存在或执行失败
        """
        if sequence_id is None:
            sequence_id = self.config.get("default_sequence")
            if sequence_id is None:
                raise ConfigurationError("未指定测试序列ID且无默认序列")

        sequence = self.config_parser.get_test_sequence(sequence_id)
        if sequence is None:
            raise ConfigurationError(f"测试序列不存在: {sequence_id}")

        self.current_sequence_id = sequence_id

        # 检测断点续测状态。自定义 --output 目录要求一次运行独占一个空目录，避免续测时混入旧文件。
        if self.custom_sequence_results_dir is None:
            resume_step_index = self._detect_and_load_resume_state(sequence_id)
            if resume_step_index is not None:
                self.resume_mode = True
                self.resume_start_step_index = resume_step_index
        else:
            self.logger.info("使用自定义结果目录，禁用默认 results/ 下的断点续测扫描")

        self._init_results_directory(sequence_id)

        # 识别序列中涉及的所有场景并执行setup
        sequence_scenes = self._identify_sequence_scenes(sequence)
        self.sequence_active_scenes = sequence_scenes
        self._setup_sequence_scenes(sequence_scenes)

        # 检查并启动全局场景（如果配置了）
        global_scene_start_time = None
        if "global_scene" in sequence:
            global_scene_id = sequence["global_scene"]
            self.logger.info(f"检测到全局场景配置: {global_scene_id}")
            global_scheduler_request = self._scene_io_scheduler_request(global_scene_id)
            if global_scheduler_request:
                target_device, scheduler = global_scheduler_request
                self.logger.info(
                    f"全局场景 {global_scene_id} 请求块设备 {target_device} 使用 IO 调度器 {scheduler}，"
                    "将在启动全局场景前切换"
                )
                self._set_io_scheduler(target_device, scheduler)
            global_scene_start_time = time.time()

            success = self._setup_global_scene(global_scene_id)
            if not success:
                self.logger.error(f"全局场景 {global_scene_id} 启动失败，终止整个测试序列")
                # 全局场景启动失败，直接抛出异常终止测试
                raise ConfigurationError(f"全局场景 {global_scene_id} 启动失败，无法继续执行测试序列")

        # controller 不再启动外部进程调度器。Linux 进程调度器保持默认 EEVDF；
        # 每个步骤开始前根据 task/scene 参数切换块设备 IO scheduler。
        self.metadata["scheduler"] = self.scheduler_name
        self.metadata["process_scheduler"] = self.process_scheduler_name
        self.metadata.setdefault("io_schedulers", [])
        self._save_metadata()
        self.logger.info(
            f"进程调度器保持默认 {self.process_scheduler_name}；"
            "将按 task 参数切换块设备 IO 调度器"
        )

        self.logger.info(f"开始执行测试序列: {sequence_id}")

        sequence_results = []
        for step_index, step in enumerate(sequence["steps"]):
            # 跳过已完成的步骤（续测模式）
            if self.resume_mode and step_index < self.resume_start_step_index:
                self.logger.info(f"跳过已完成的步骤 {step_index + 1}/{len(sequence['steps'])}")
                # 从元数据中恢复已完成步骤的结果
                if step_index < len(self.metadata.get("steps", [])):
                    sequence_results.append(
                        {
                            "step_index": step_index,
                            "step_config": step,
                            "results": {},  # 简化的结果，实际数据已在目录中保存
                        }
                    )
                continue

            self.logger.info(f"执行步骤 {step_index + 1}/{len(sequence['steps'])}")
            try:
                self._apply_step_io_schedulers(step)
                step_results = self._execute_step(step, step_index)
                sequence_results.append(
                    {"step_index": step_index, "step_config": step, "results": step_results}
                )
            except Exception as e:
                self.logger.error(f"步骤 {step_index + 1} 执行失败: {str(e)}")
                raise

        self.results[sequence_id] = {"sequence_config": sequence, "steps": sequence_results}

        # 在创建打包文件前添加失败场景统计
        if self.failed_scenes:
            self.logger.warning(f"测试序列中有 {len(self.failed_scenes)} 个场景失败:")
            for scene_id, info in self.failed_scenes.items():
                self.logger.warning(f"  - 场景 {scene_id} 在 {info['phase']} 阶段失败: {info['error']}")

            # 将失败场景信息添加到序列结果中
            self.results[sequence_id]["failed_scenes"] = {
                scene_id: {"error": info["error"], "phase": info["phase"]}
                for scene_id, info in self.failed_scenes.items()
            }

        # 停止全局场景并收集数据（如果启动了）
        if self.global_scene_enabled and global_scene_start_time is not None:
            self.logger.info("开始停止全局场景")

            # 先停止全局场景运行线程，再 collect/stat。否则 collect 可能读取到仍在写入的
            # 监控数据，stat() 写出的文件也可能早于最终 end_time。
            try:
                self._stop_global_scene_runtime()
            except Exception as e:
                self.logger.error(f"全局场景停止失败: {str(e)}", exc_info=True)

            global_scene_end_time = time.time()

            # 收集全局场景数据并更新元数据
            try:
                global_scene_metadata = self._collect_global_scene_data(
                    global_scene_start_time, global_scene_end_time
                )
                self.metadata["global_scene"] = global_scene_metadata
                self._save_metadata()  # 保存包含全局场景信息的元数据
            except Exception as e:
                self.logger.error(f"全局场景数据收集失败: {str(e)}")
                self.metadata["global_scene"] = {
                    "scene_id": self.global_scene_id,
                    "status": "failed",
                    "error": f"数据收集失败: {str(e)}",
                }

            # 停止并清理全局场景
            self._teardown_global_scene(stop_runtime=False)

        # 执行序列级teardown
        self._teardown_sequence_scenes()

        # 恢复 controller 在本序列中切换过的块设备 IO scheduler，避免污染后续实验。
        self._cleanup_scheduler()

        if self.custom_sequence_results_dir and self.sequence_results_dir:
            self.save_results(str(self.sequence_results_dir / "run_results.json"))

        # 创建打包文件
        archive_path = self._create_archive()

        self.logger.info(f"测试序列 {sequence_id} 执行完成，结果已打包到: {archive_path}")
        self.logger.info(f"结果目录: {self.sequence_results_dir} (时间戳: {self.result_timestamp})")

    def save_results(self, output_path: str) -> None:
        """
        保存测试结果

        Args:
            output_path: 输出文件路径
        """
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(self.results, f, indent=2, ensure_ascii=False)

        self.logger.info(f"结果已保存到: {output_file}")

    def run(
        self,
        sequence_id: Optional[str] = None,
        output_path: Optional[str] = None,
        log_file_path: Optional[str] = None,
        monitoring_filter: Optional[str] = None,
    ) -> None:
        """
        运行Benchmark

        Args:
            sequence_id: 要运行的测试序列ID
            output_path: 结果输出路径
            log_file_path: 日志文件路径，用于包含在结果中
            monitoring_filter: 可选监控场景过滤器（逗号分隔的子串匹配，或 "all"）
        """
        try:
            # 保存日志文件路径
            self.log_file_path = log_file_path
            self.monitoring_filter = self._parse_monitoring_filter(monitoring_filter)

            self.load_config()
            self.load_scenes()
            self._configure_output_path(output_path)
            self.run_sequence(sequence_id)

            if self.summary_output_path:
                self.save_results(str(self.summary_output_path))

        except Exception as e:
            self.logger.error(f"Benchmark执行失败: {str(e)}")

            # 确保在异常情况下清理全局场景资源
            if self.global_scene_enabled:
                self.logger.info("异常情况下清理全局场景资源")
                self._teardown_global_scene()

            # 执行序列级teardown确保资源清理
            self._teardown_sequence_scenes()

            # 确保在出现异常时恢复 controller 切换过的块设备 IO scheduler。
            self._cleanup_scheduler()
            raise

    def _prepare_scene(self, scene: Scene) -> bool:
        """
        准备单个场景的辅助方法，并保护工作目录

        Args:
            scene: 场景实例

        Returns:
            bool: 场景准备是否成功
        """
        # 保存当前工作目录
        original_dir = os.getcwd()
        success = True
        try:
            scene_id = scene.scene_id
            self.logger.info(f"开始准备场景: {scene_id}")
            scene.prepare()
            self.logger.info(f"场景 {scene_id} 准备完成")
        except Exception as e:
            scene_id = scene.scene_id
            self.logger.error(f"场景 {scene_id} 准备失败: {str(e)}", exc_info=True)
            # 记录失败信息但不抛出异常
            self.failed_scenes[scene_id] = {
                "error": str(e),
                "phase": "prepare",
                "traceback": traceback.format_exc(),
            }
            success = False
        finally:
            # 无论场景准备成功或失败，都恢复原始工作目录
            current_dir = os.getcwd()
            if current_dir != original_dir:
                self.logger.warning(
                    f"场景 {scene.scene_id} 在准备阶段改变了工作目录，从 {original_dir} 到 {current_dir}，正在恢复..."
                )
                os.chdir(original_dir)
                self.logger.info(f"已恢复工作目录到: {original_dir}")
        return success

    def _stop_scene(self, scene: Scene) -> bool:
        """
        停止单个场景的辅助方法，并保护工作目录

        Args:
            scene: 场景实例

        Returns:
            bool: 场景停止是否成功
        """
        # 保存当前工作目录
        original_dir = os.getcwd()
        success = True
        try:
            scene_id = scene.scene_id
            self.logger.info(f"停止场景: {scene_id}")
            scene.stop()
            self.logger.info(f"场景 {scene_id} 已停止")
        except Exception as e:
            scene_id = scene.scene_id
            self.logger.error(f"场景 {scene_id} 停止失败: {str(e)}", exc_info=True)
            # 记录失败信息但不抛出异常
            if scene_id not in self.failed_scenes:  # 如果之前没有失败记录
                self.failed_scenes[scene_id] = {
                    "error": str(e),
                    "phase": "stop",
                    "traceback": traceback.format_exc(),
                }
            success = False
        finally:
            # 无论场景停止成功或失败，都恢复原始工作目录
            current_dir = os.getcwd()
            if current_dir != original_dir:
                self.logger.warning(
                    f"场景 {scene.scene_id} 在停止阶段改变了工作目录，从 {original_dir} 到 {current_dir}，正在恢复..."
                )
                os.chdir(original_dir)
                self.logger.info(f"已恢复工作目录到: {original_dir}")
        return success

    def _cleanup_scene(self, scene: Scene) -> bool:
        """
        清理单个场景的辅助方法，并保护工作目录

        Args:
            scene: 场景实例

        Returns:
            bool: 场景清理是否成功
        """
        # 保存当前工作目录
        original_dir = os.getcwd()
        success = True
        try:
            scene_id = scene.scene_id
            self.logger.info(f"开始清理场景: {scene_id}")
            scene.cleanup()
            self.logger.info(f"场景 {scene_id} 清理完成")
        except Exception as e:
            scene_id = scene.scene_id
            self.logger.error(f"场景 {scene_id} 清理失败: {str(e)}", exc_info=True)
            # 记录失败信息但不抛出异常
            if scene_id not in self.failed_scenes:  # 如果之前没有失败记录
                self.failed_scenes[scene_id] = {
                    "error": str(e),
                    "phase": "cleanup",
                    "traceback": traceback.format_exc(),
                }
            success = False
        finally:
            # 无论场景清理成功或失败，都恢复原始工作目录
            current_dir = os.getcwd()
            if current_dir != original_dir:
                self.logger.warning(
                    f"场景 {scene.scene_id} 在清理阶段改变了工作目录，从 {original_dir} 到 {current_dir}，正在恢复..."
                )
                os.chdir(original_dir)
                self.logger.info(f"已恢复工作目录到: {original_dir}")
        return success

    def _collect_scene_data(
        self,
        scene: Scene,
        scene_id: str,
        step_index: int,
        step_start_time: float,
        scene_start_time: float,
        scene_end_time: float,
    ) -> Dict[str, Any]:
        """
        收集单个场景的元数据和保存结果文件，并保护工作目录

        Args:
            scene: 场景实例
            scene_id: 场景ID
            step_index: 步骤索引
            step_start_time: 步骤开始时间戳
            scene_start_time: 场景开始时间戳
            scene_end_time: 场景结束时间戳

        Returns:
            Dict[str, Any]: 场景元数据
        """
        # 保存当前工作目录
        original_dir = os.getcwd()
        try:
            # 执行原始数据收集逻辑
            scene_config = self.config["scenes"][scene_id]
            step_dir = self._require_sequence_results_dir() / f"step_{step_index}"

            # 确保存储场景结果的目录存在
            scene_result_dir = step_dir / scene_id
            scene_result_dir.mkdir(exist_ok=True)
            copied_files: List[Dict[str, str]] = []
            missing_files: List[str] = []

            try:
                # 调用场景的stat方法获取结果文件
                stat_result = scene.stat()

                # 处理stat返回的结果（支持单个文件或文件列表）
                files_to_copy = []
                if isinstance(stat_result, str):
                    # 单个文件路径
                    files_to_copy = [stat_result]
                elif isinstance(stat_result, (list, tuple)):
                    # 文件路径列表
                    files_to_copy = list(stat_result)
                elif stat_result is not None:
                    self.logger.warning(f"场景 {scene_id} 的stat方法返回了不支持的类型: {type(stat_result)}")

                # 复制所有文件到结果目录
                for file_path in files_to_copy:
                    if file_path and os.path.exists(file_path):
                        target_file = scene_result_dir / os.path.basename(file_path)
                        # 如果目标文件已存在，添加数字后缀避免覆盖
                        counter = 1
                        original_target = target_file
                        while target_file.exists():
                            stem = original_target.stem
                            suffix = original_target.suffix
                            target_file = scene_result_dir / f"{stem}_{counter}{suffix}"
                            counter += 1

                        shutil.copy2(file_path, target_file)
                        copied_files.append(
                            {
                                "original_path": str(file_path),
                                "result_path": str(target_file),
                                "relative_path": self._relative_sequence_path(target_file),
                            }
                        )
                        self.logger.info(
                            f"已保存场景 {scene_id} 结果文件: {os.path.basename(file_path)} -> {target_file.name}"
                        )
                    elif file_path:
                        missing_files.append(str(file_path))
                        self.logger.warning(f"场景 {scene_id} 返回的文件不存在: {file_path}")

                if copied_files:
                    self.logger.info(f"场景 {scene_id} 共保存了 {len(copied_files)} 个结果文件")

            except Exception as e:
                self.logger.error(f"保存场景 {scene_id} 结果失败: {str(e)}")
                if scene_id not in self.failed_scenes:  # 如果之前没有失败记录
                    self.failed_scenes[scene_id] = {
                        "error": str(e),
                        "phase": "stat",
                        "traceback": traceback.format_exc(),
                    }

            # 构建场景元数据
            scene_metadata = {
                "scene_id": scene_id,
                "name": scene_config.get("name", scene_id),
                "type": scene_config.get("type", "general"),
                "start_time": datetime.fromtimestamp(scene_start_time).isoformat(),
                "end_time": datetime.fromtimestamp(scene_end_time).isoformat(),
                "duration": round(scene_end_time - scene_start_time, 2),
                "step_offset": round(scene_start_time - step_start_time, 2),
            }

            # 添加场景失败状态信息
            if scene_id in self.failed_scenes:
                scene_metadata["status"] = "failed"
                scene_metadata["error"] = self.failed_scenes[scene_id]["error"]
                scene_metadata["error_phase"] = self.failed_scenes[scene_id]["phase"]
            else:
                scene_metadata["status"] = "success"
            if copied_files:
                scene_metadata["result_files"] = copied_files
            if missing_files:
                scene_metadata["missing_result_files"] = missing_files

            return scene_metadata
        finally:
            # 无论数据收集成功或失败，都恢复原始工作目录
            current_dir = os.getcwd()
            if current_dir != original_dir:
                self.logger.warning(
                    f"场景 {scene.scene_id} 在数据收集阶段改变了工作目录，从 {original_dir} 到 {current_dir}，正在恢复..."
                )
                os.chdir(original_dir)
                self.logger.info(f"已恢复工作目录到: {original_dir}")
