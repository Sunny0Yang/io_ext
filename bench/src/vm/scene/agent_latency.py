"""
智能体延迟监测场景模块

负责使用sched_agent收集调度决策延迟数据。
"""

import os
import time
import logging
import subprocess
import tempfile
import argparse
import struct
import glob
from typing import Dict, Any, List, Optional

from ..base_scene import Scene

# 获取logger实例
logger = logging.getLogger("agent_latency")


class AgentLatencyScene(Scene):
    """智能体延迟监测场景类"""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        """
        初始化智能体延迟监测场景

        Args:
            scene_id: 场景ID
            scene_name: 场景名称
            parameters: 场景参数，可选包含：
                - duration: 监控持续时间(秒) (默认300)
                - privilege: 权限管理配置
                  * method: 权限获取方法 (默认sudo_askpass)
                  * askpass_script: SUDO_ASKPASS脚本路径
                  * env_var: 密码环境变量名 (默认SUDO_PASSWORD)
        """
        super().__init__(scene_id, scene_name, parameters)

        # 设置默认参数
        parameters = parameters or {}

        # 系统监控配置
        self.agent_path = "/home/anon/ioext-bench/src/vm/assets/system/system_metric/build/sched_agent"
        self.output_file = "/home/anon/ioext-bench/src/vm/assets/system/system_metric/binary_output.bin"
        self.config_file = "/home/anon/ioext-bench/src/vm/assets/system/system_metric/config/config.json"
        self.duration = parameters.get("duration", 300)

        # 权限管理配置
        self.privilege_config = parameters.get("privilege", {})
        self.privilege_method = self.privilege_config.get("method", "sudo_askpass")
        self.askpass_script_path = self.privilege_config.get("askpass_script", "")
        self.password_env_var = self.privilege_config.get("env_var", "SUDO_PASSWORD")

        # 运行状态跟踪
        self.start_time = None
        self.end_time = None
        self.agent_process = None
        self.temp_askpass_script = None

        # 延迟数据相关
        self.latency_data = None
        self.latency_output_files = []

        logger.debug(f"初始化场景 {scene_id}: {scene_name}, 参数: {parameters}")

    def prepare(self) -> None:
        """
        每轮运行前准备方法

        无操作
        """
        logger.info("准备智能体延迟监测环境...")
        logger.info("智能体延迟监测环境准备完成")

    def _create_askpass_script(self) -> None:
        """创建临时SUDO_ASKPASS脚本"""
        if self.privilege_method == "sudo_askpass":
            if not self.askpass_script_path:
                # 创建临时脚本文件
                fd, self.temp_askpass_script = tempfile.mkstemp(suffix=".sh")
                os.close(fd)

                # 写入脚本内容
                with open(self.temp_askpass_script, "w") as f:
                    f.write(f'#!/bin/bash\necho "${self.password_env_var}"\n')

                # 设置可执行权限
                os.chmod(self.temp_askpass_script, 0o700)
                self.askpass_script_path = self.temp_askpass_script
                logger.debug(f"创建临时SUDO_ASKPASS脚本: {self.temp_askpass_script}")
            else:
                logger.debug(f"使用指定的SUDO_ASKPASS脚本: {self.askpass_script_path}")

    def _find_latency_files(self) -> List[str]:
        """
        查找延迟数据文件

        Returns:
            List[str]: 找到的延迟数据文件路径列表
        """
        search_pattern = "/tmp/sched_agent_latency_*.bin"
        files = glob.glob(search_pattern)

        # 按修改时间过滤，只返回监控期间生成的文件
        if self.start_time and files:
            filtered_files = []
            for file_path in files:
                file_mtime = os.path.getmtime(file_path)
                if file_mtime >= self.start_time:
                    filtered_files.append(file_path)
            return filtered_files

        return files

    def _parse_latency_data(self, file_path: str) -> Dict[str, Any]:
        """
        解析延迟数据二进制文件

        Args:
            file_path: 延迟数据文件路径

        Returns:
            Dict[str, Any]: 解析出的延迟数据
        """
        try:
            with open(file_path, "rb") as f:
                # 读取文件头
                header_data = f.read(32)  # 12+4+8+4+4字节
                if len(header_data) < 32:
                    logger.error(f"延迟数据文件头不完整: {file_path}")
                    return {}

                magic = header_data[:12]
                if magic != b"SCHEDLATENCY":
                    logger.error(f"延迟数据文件格式错误: {file_path}")
                    return {}

                version = struct.unpack("<I", header_data[12:16])[0]
                record_count = struct.unpack("<Q", header_data[16:24])[0]

                logger.debug(f"延迟文件 {file_path}: version={version}, records={record_count}")

                # 读取决策记录
                records = []
                for i in range(record_count):
                    record_data = f.read(32)  # 每个记录32字节
                    if len(record_data) < 32:
                        logger.warning(f"记录 {i} 数据不完整，停止解析")
                        break

                    t1_ns, t2_ns, t4_ns, decision_id = struct.unpack("<QQQQ", record_data)
                    if decision_id > 0:  # 有效记录
                        records.append(
                            {"decision_id": decision_id, "t1_ns": t1_ns, "t2_ns": t2_ns, "t4_ns": t4_ns}
                        )

                # 计算延迟统计
                if records:
                    inference_latencies = [
                        (r["t2_ns"] - r["t1_ns"]) / 1e6 for r in records if r["t2_ns"] > r["t1_ns"]
                    ]
                    switch_latencies = [
                        (r["t4_ns"] - r["t2_ns"]) / 1e6 for r in records if r["t4_ns"] > r["t2_ns"]
                    ]
                    total_latencies = [
                        (r["t4_ns"] - r["t1_ns"]) / 1e6 for r in records if r["t4_ns"] > r["t1_ns"]
                    ]

                    def calc_stats(latencies):
                        if not latencies:
                            return {"avg": 0, "max": 0, "min": 0, "count": 0}
                        return {
                            "avg": sum(latencies) / len(latencies),
                            "max": max(latencies),
                            "min": min(latencies),
                            "count": len(latencies),
                        }

                    return {
                        "file_path": file_path,
                        "total_decisions": len(records),
                        "inference_latency": calc_stats(inference_latencies),
                        "switch_latency": calc_stats(switch_latencies),
                        "total_latency": calc_stats(total_latencies),
                    }
                else:
                    return {
                        "file_path": file_path,
                        "total_decisions": 0,
                        "inference_latency": {"avg": 0, "max": 0, "min": 0, "count": 0},
                        "switch_latency": {"avg": 0, "max": 0, "min": 0, "count": 0},
                        "total_latency": {"avg": 0, "max": 0, "min": 0, "count": 0},
                    }

        except Exception as e:
            logger.error(f"解析延迟数据文件失败 {file_path}: {e}")
            return {}

    def _run(self) -> None:
        """
        智能体延迟监测的核心逻辑

        启动sched_agent监控进程
        """
        self.start_time = time.time()
        logger.info(
            f"开始智能体延迟监测，预计持续 {self.duration} 秒"
            if self.duration > 0
            else "开始智能体延迟监测，将持续到收到停止信号"
        )

        # 启动系统指标监控
        self._start_system_monitoring()

        # 等待监控完成或收到停止信号
        if self.duration <= 0:
            # 无限等待，直到收到停止信号
            self._should_stop.wait()
            logger.info("收到停止信号，结束智能体延迟监测")
        else:
            # 等待固定时间或收到停止信号
            was_stopped = self._should_stop.wait(timeout=self.duration)
            if was_stopped:
                logger.info("收到停止信号，提前结束智能体延迟监测")
            else:
                logger.info(f"达到设定的最大监测时间 {self.duration} 秒，结束智能体延迟监测")

    def _run_with_privilege(self, cmd: List[str]) -> subprocess.Popen:
        """
        使用权限管理方式运行命令

        Args:
            cmd: 要运行的命令

        Returns:
            subprocess.Popen: 命令进程
        """
        if self.privilege_method == "sudo_askpass":
            # 设置SUDO_ASKPASS环境变量
            env = os.environ.copy()
            env["SUDO_ASKPASS"] = self.askpass_script_path

            # 添加sudo -E -A前缀
            sudo_cmd = ["sudo", "-E", "-A"] + cmd
            logger.debug(f"使用sudo -E -A运行命令: {' '.join(sudo_cmd)}")

            return subprocess.Popen(sudo_cmd, env=env)

        elif self.privilege_method == "sudo_cache":
            # 使用sudo运行命令
            sudo_cmd = ["sudo", "-E"] + cmd
            logger.debug(f"使用sudo -E运行命令: {' '.join(sudo_cmd)}")

            return subprocess.Popen(sudo_cmd)

        elif self.privilege_method == "sudo_nopasswd":
            # 使用sudo运行命令
            sudo_cmd = ["sudo", "-E"] + cmd
            logger.debug(f"使用sudo -E(免密)运行命令: {' '.join(sudo_cmd)}")

            return subprocess.Popen(sudo_cmd)

        else:
            # 直接运行命令
            logger.debug(f"直接运行命令: {' '.join(cmd)}")
            return subprocess.Popen(cmd)

    def _start_system_monitoring(self) -> None:
        """启动系统指标监控"""
        logger.info("启动智能体延迟监控")

        # 检查sched_agent是否存在
        if not os.path.exists(self.agent_path):
            logger.error(f"智能体延迟监控工具不存在: {self.agent_path}")
            return

        # 检查配置文件是否存在
        if not os.path.exists(self.config_file):
            logger.error(f"智能体延迟监控配置文件不存在: {self.config_file}")
            return

        # 启动sched_agent
        cmd = [self.agent_path, self.config_file]

        try:
            self.agent_process = self._run_with_privilege(cmd)
            logger.info(f"智能体延迟监控启动成功，进程ID: {self.agent_process.pid}")
        except Exception as e:
            logger.error(f"智能体延迟监控启动失败: {e}")

    def end(self) -> None:
        """
        结束智能体延迟监测

        终止sched_agent进程
        """
        logger.info("结束智能体延迟监测")

        # 终止sched_agent进程
        if self.agent_process and self.agent_process.poll() is None:
            pid = self.agent_process.pid
            logger.info(f"终止智能体延迟监控进程 (PID: {pid})")
            try:
                # 使用kill -TERM命令发送终止信号
                kill_cmd = ["kill", "-TERM", str(pid)]
                self._run_with_privilege(kill_cmd)

                # 等待进程终止
                timeout = 30
                start_time = time.time()
                while time.time() - start_time < timeout:
                    if self.agent_process.poll() is not None:
                        logger.info("智能体延迟监控进程已成功终止")
                        break
                    time.sleep(0.5)

                # 如果进程仍在运行，发送KILL信号强制终止
                if self.agent_process.poll() is None:
                    logger.warning("智能体延迟监控进程未响应TERM信号，发送KILL信号强制终止")
                    kill_cmd = ["kill", "-KILL", str(pid)]
                    self._run_with_privilege(kill_cmd)
                    self.agent_process.wait(timeout=3)
            except Exception as e:
                logger.error(f"终止智能体延迟监控进程时发生错误: {e}")

        # 收集延迟数据文件路径
        logger.info("开始收集延迟数据文件")
        # 等待一段时间确保C++端写入延迟文件完成
        time.sleep(2)

        latency_files = self._find_latency_files()
        if latency_files:
            self.latency_output_files = latency_files
            logger.info(f"找到延迟数据文件: {latency_files}")
        else:
            logger.warning("未找到延迟数据文件")
            self.latency_output_files = []

        self.end_time = time.time()
        logger.info(f"智能体延迟监测已结束，总持续时间: {self.end_time - self.start_time:.2f}秒")

    def cleanup(self) -> None:
        """
        每轮运行后清理方法

        无操作（保留SUDO_ASKPASS脚本）
        """
        logger.info("清理智能体延迟监测资源")

    def collect(self) -> Dict[str, Any]:
        """
        收集监测数据

        Returns:
            Dict[str, Any]: 收集到的监测数据
        """
        logger.info("收集智能体延迟监测数据")

        # 收集基本信息
        data = {
            "metadata": {
                "scene_id": self.scene_id,
                "scene_name": self.scene_name,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "duration": self.end_time - self.start_time if self.end_time and self.start_time else 0,
                "latency_files": self.latency_output_files,
            }
        }

        # 存储收集到的数据
        self.collected_data = data

        logger.info("智能体延迟监测数据收集完成")
        return data

    def stat(self) -> List[str]:
        """
        返回结果文件路径列表

        Returns:
            List[str]: 结果文件路径列表
        """
        result_files = []

        # 如果没有延迟文件或文件不存在，尝试重新收集
        if not self.latency_output_files or not any(os.path.exists(f) for f in self.latency_output_files):
            logger.warning("延迟数据文件缺失，尝试重新收集...")
            if self.start_time:  # 确保有有效的启动时间
                latency_files = self._find_latency_files()
                if latency_files:
                    self.latency_output_files = latency_files
                    logger.info(f"重新找到延迟数据文件: {latency_files}")
                else:
                    logger.warning("重新收集后仍未找到延迟数据文件")

        # 添加存在的延迟数据文件
        if self.latency_output_files:
            for latency_file in self.latency_output_files:
                if os.path.exists(latency_file):
                    result_files.append(latency_file)
                    logger.info(f"延迟数据文件: {latency_file}")
                else:
                    logger.warning(f"延迟数据文件不存在: {latency_file}")

        if not result_files:
            logger.warning("未找到任何延迟数据文件")

        return result_files

    def deserialize(self, data_path: str) -> None:
        """
        反序列化场景数据

        Args:
            data_path: 数据文件路径
        """
        try:
            # 只读取和保存元数据
            self.collected_data = {
                "metadata": {
                    "scene_id": self.scene_id,
                    "scene_name": self.scene_name,
                    "latency_file": data_path,
                }
            }

            # 设置延迟文件路径
            self.latency_output_files = [data_path]

            logger.info(f"成功反序列化元数据，延迟数据文件: {data_path}")
        except Exception as e:
            logger.error(f"反序列化数据失败: {e}")
            raise

    def score(self) -> float:
        """
        计算场景得分

        Returns:
            float: 场景得分
        """
        # 实现计算得分的逻辑，基于收集到的数据
        # 这里只是一个占位符
        return 0.0

    def setup(self) -> None:
        """
        一次性全局设置方法

        创建临时SUDO_ASKPASS脚本
        """
        # 创建临时SUDO_ASKPASS脚本
        self._create_askpass_script()

    def teardown(self) -> None:
        """
        一次性全局清理方法

        删除临时SUDO_ASKPASS脚本
        """
        # 删除临时SUDO_ASKPASS脚本
        if (
            hasattr(self, "temp_askpass_script")
            and self.temp_askpass_script
            and os.path.exists(self.temp_askpass_script)
        ):
            os.remove(self.temp_askpass_script)
            logger.debug(f"已删除临时SUDO_ASKPASS脚本: {self.temp_askpass_script}")


def setup_logging(log_level: str = "INFO") -> None:
    """
    设置日志配置

    Args:
        log_level: 日志级别，默认为INFO
    """
    # 将字符串日志级别转换为对应的常量
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"无效的日志级别: {log_level}")

    # 配置根日志记录器
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="智能体延迟监测")

    parser.add_argument("--id", default="agent_latency_scene", help="场景ID")
    parser.add_argument("--name", default="智能体延迟监测", help="场景名称")
    parser.add_argument("--duration", type=int, default=300, help="监控持续时间(秒)")

    # 权限管理选项
    parser.add_argument(
        "--privilege-method",
        choices=["sudo_askpass", "sudo_cache", "sudo_nopasswd"],
        default="sudo_askpass",
        help="权限获取方法",
    )
    parser.add_argument("--askpass-script", default="", help="SUDO_ASKPASS脚本路径")
    parser.add_argument("--password-env", default="SUDO_PASSWORD", help="密码环境变量名")

    # 日志选项
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="日志级别",
    )

    return parser.parse_args()


if __name__ == "__main__":
    # 解析命令行参数
    args = parse_args()

    # 设置日志级别
    setup_logging(args.log_level)

    try:
        # 构建场景参数
        parameters = {
            "duration": args.duration,
            "privilege": {
                "method": args.privilege_method,
                "askpass_script": args.askpass_script,
                "env_var": args.password_env,
            },
        }

        # 创建并运行场景
        logger.info(f"创建智能体延迟监测场景: {args.id} - {args.name}")
        scene = AgentLatencyScene(args.id, args.name, parameters)

        logger.info("准备场景环境...")
        scene.prepare()

        logger.info("启动监测...")
        scene.start()  # 这会调用_run()方法

        logger.info("结束监测...")
        scene.end()

        logger.info("收集数据...")
        data = scene.collect()

        # 获取结果文件路径列表
        result_files = scene.stat()
        logger.info(f"结果文件路径: {result_files}")

        # 清理资源
        logger.info("清理资源...")
        scene.cleanup()

        logger.info("监测完成")

    except KeyboardInterrupt:
        logger.info("监测被用户中断")
        if "scene" in locals():
            scene.end()
            scene.cleanup()
    except Exception as e:
        logger.error(f"监测过程中发生错误: {e}", exc_info=True)
        if "scene" in locals():
            scene.end()
            scene.cleanup()
