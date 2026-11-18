"""
块层 bpftrace 监测场景基类：运行 .bt 脚本并将输出写入日志文件。
"""

import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional

from ..base_scene import Scene

logger = logging.getLogger("bpftrace_block")

_ASSETS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets", "block_bpf"))


class BpftraceBlockLayerScene(Scene):
    """使用 bpftrace 采集块层 tracepoint 数据的场景基类。"""

    def __init__(
        self,
        scene_id: str,
        scene_name: str,
        script_basename: str,
        parameters: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(scene_id, scene_name, parameters)
        parameters = parameters or {}

        self.script_basename = script_basename
        self.script_path = os.path.join(_ASSETS_DIR, script_basename)
        self.duration = parameters.get("duration", 300)

        out = parameters.get("output_file")
        if out:
            self.output_file = out
        else:
            self.output_file = os.path.join(
                tempfile.gettempdir(), f"bpftrace_{scene_id}_{script_basename}.log"
            )

        self.privilege_config = parameters.get("privilege", {})
        self.privilege_method = self.privilege_config.get("method", "sudo_askpass")
        self.askpass_script_path = self.privilege_config.get("askpass_script", "")
        self.password_env_var = self.privilege_config.get("env_var", "SUDO_PASSWORD")

        self.start_time = None
        self.end_time = None
        self.agent_process = None
        self.temp_askpass_script = None
        self._output_handle = None

        logger.debug("初始化场景 %s: %s, script=%s, 参数: %s", scene_id, scene_name, script_basename, parameters)

    def prepare(self) -> None:
        if not os.path.isfile(self.script_path):
            logger.error("bpftrace 脚本不存在: %s", self.script_path)
            return
        bpftrace_bin = shutil.which("bpftrace")
        if not bpftrace_bin:
            logger.error("未找到 bpftrace，请安装后重试")
        else:
            logger.debug("使用 bpftrace: %s", bpftrace_bin)

    def _create_askpass_script(self) -> None:
        if self.privilege_method == "sudo_askpass":
            if not self.askpass_script_path:
                fd, self.temp_askpass_script = tempfile.mkstemp(suffix=".sh")
                os.close(fd)
                with open(self.temp_askpass_script, "w") as f:
                    f.write(f'#!/bin/bash\necho "${self.password_env_var}"\n')
                os.chmod(self.temp_askpass_script, 0o700)
                self.askpass_script_path = self.temp_askpass_script
                logger.debug("创建临时 SUDO_ASKPASS: %s", self.temp_askpass_script)
            else:
                logger.debug("使用指定 SUDO_ASKPASS: %s", self.askpass_script_path)

    def _run_with_privilege(self, cmd: List[str], **popen_kwargs: Any) -> subprocess.Popen:
        if self.privilege_method == "sudo_askpass":
            env = os.environ.copy()
            env["SUDO_ASKPASS"] = self.askpass_script_path
            sudo_cmd = ["sudo", "-E", "-A"] + cmd
            logger.debug("运行: %s", " ".join(sudo_cmd))
            return subprocess.Popen(sudo_cmd, env=env, **popen_kwargs)
        if self.privilege_method in ("sudo_cache", "sudo_nopasswd"):
            sudo_cmd = ["sudo", "-E"] + cmd
            logger.debug("运行: %s", " ".join(sudo_cmd))
            return subprocess.Popen(sudo_cmd, **popen_kwargs)
        logger.debug("运行: %s", " ".join(cmd))
        return subprocess.Popen(cmd, **popen_kwargs)

    def _start_bpftrace(self) -> None:
        if not os.path.isfile(self.script_path):
            logger.error("脚本不存在，跳过启动: %s", self.script_path)
            return
        out_dir = os.path.dirname(self.output_file)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        self._output_handle = open(self.output_file, "w", encoding="utf-8", errors="replace")
        cmd: List[str] = ["bpftrace", self.script_path]
        try:
            self.agent_process = self._run_with_privilege(
                cmd,
                stdout=self._output_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            logger.info("bpftrace 已启动, pid=%s, 日志=%s", self.agent_process.pid, self.output_file)
        except OSError as e:
            logger.error("启动 bpftrace 失败: %s", e)
            self._output_handle.close()
            self._output_handle = None

    def _run(self) -> None:
        self.start_time = time.time()
        if self.duration > 0:
            logger.info("开始块层 bpftrace（%s），持续 %s 秒", self.script_basename, self.duration)
        else:
            logger.info("开始块层 bpftrace（%s），直到停止信号", self.script_basename)
        self._start_bpftrace()
        if self.duration <= 0:
            self._should_stop.wait()
            logger.info("收到停止信号，结束采集")
        else:
            stopped = self._should_stop.wait(timeout=self.duration)
            if stopped:
                logger.info("收到停止信号，提前结束采集")
            else:
                logger.info("达到时长 %s 秒，结束采集", self.duration)

    def _stop_bpftrace(self) -> None:
        if not self.agent_process or self.agent_process.poll() is not None:
            return
        pid = self.agent_process.pid
        logger.info("停止 bpftrace (pid=%s)", pid)
        try:
            self.agent_process.send_signal(signal.SIGINT)
            self.agent_process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            logger.warning("bpftrace 未响应 SIGINT，发送 SIGTERM")
            try:
                self.agent_process.terminate()
                self.agent_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning("发送 SIGKILL")
                self.agent_process.kill()
                self.agent_process.wait(timeout=5)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.error("停止 bpftrace 时出错: %s", e)

        if self._output_handle:
            try:
                self._output_handle.flush()
            except OSError:
                pass
            self._output_handle.close()
            self._output_handle = None

    def end(self) -> None:
        logger.info("结束块层 bpftrace 场景")
        self._stop_bpftrace()
        self.end_time = time.time()
        if self.start_time:
            logger.info("采集持续时间: %.2f 秒", self.end_time - self.start_time)

    def cleanup(self) -> None:
        logger.info("清理块层 bpftrace 场景资源")

    def collect(self) -> Dict[str, Any]:
        data = {
            "metadata": {
                "scene_id": self.scene_id,
                "scene_name": self.scene_name,
                "script": self.script_basename,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "duration": (self.end_time - self.start_time) if self.end_time and self.start_time else 0,
                "log_file": self.output_file,
            }
        }
        self.collected_data = data
        return data

    def stat(self) -> Any:
        if os.path.isfile(self.output_file):
            logger.info("bpftrace 输出: %s", self.output_file)
            return self.output_file
        logger.warning("bpftrace 输出文件不存在: %s", self.output_file)
        return ""

    def deserialize(self, data_path: str) -> None:
        self.collected_data = {
            "metadata": {
                "scene_id": self.scene_id,
                "scene_name": self.scene_name,
                "log_file": data_path,
            }
        }
        self.output_file = data_path

    def score(self) -> float:
        return 0.0

    def setup(self) -> None:
        self._create_askpass_script()

    def teardown(self) -> None:
        if self.temp_askpass_script and os.path.isfile(self.temp_askpass_script):
            os.remove(self.temp_askpass_script)
            logger.debug("已删除临时 SUDO_ASKPASS: %s", self.temp_askpass_script)


def setup_logging(log_level: str = "INFO") -> None:
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"无效的日志级别: {log_level}")
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
