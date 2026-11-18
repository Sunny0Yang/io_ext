"""
智能体开销监测场景模块

负责使用sched_agent收集调度智能体的运行开销数据。
"""

import os
import time
import json
import logging
import subprocess
import tempfile
import argparse
import threading
import shutil
from typing import Dict, Any, List, Optional

from ..base_scene import Scene

# 获取logger实例
logger = logging.getLogger("agent_cost")


class AgentCostScene(Scene):
    """智能体开销监测场景类"""

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        """
        初始化智能体开销监测场景

        Args:
            scene_id: 场景ID
            scene_name: 场景名称
            parameters: 场景参数，可选包含：
                - duration: 监控持续时间(秒) (默认300)
                - overhead_interval: 开销采样间隔(秒) (默认5)
                - privilege: 权限管理配置
                  * method: 权限获取方法 (默认sudo_askpass)
                  * askpass_script: SUDO_ASKPASS脚本路径
                  * env_var: 密码环境变量名 (默认SUDO_PASSWORD)
        """
        super().__init__(scene_id, scene_name, parameters)

        # 设置默认参数
        parameters = parameters or {}

        # 系统监控配置
        self.agent_build_dir = "/home/anon/ioext-bench/src/vm/assets/system/system_metric/build"
        self.agent_source_dir = "/home/anon/ioext-bench/src/vm/assets/system/system_metric"
        self.agent_path = os.path.join(self.agent_build_dir, "sched_agent")
        self.output_file = "/home/anon/ioext-bench/src/vm/assets/system/system_metric/binary_output.bin"
        self.config_file = "/home/anon/ioext-bench/src/vm/assets/system/system_metric/config/config.json"
        self.duration = parameters.get("duration", 300)

        # 权限管理配置
        self.privilege_config = parameters.get("privilege", {})
        self.privilege_method = self.privilege_config.get("method", "sudo_askpass")
        self.askpass_script_path = self.privilege_config.get("askpass_script", "")
        self.password_env_var = self.privilege_config.get("env_var", "SUDO_PASSWORD")

        # 开销监控配置
        self.overhead_interval = parameters.get("overhead_interval", 5)

        # 运行状态跟踪
        self.start_time = None
        self.end_time = None
        self.agent_process = None
        self.temp_askpass_script = None

        # 开销监控相关
        self.overhead_monitor_thread = None
        self.overhead_monitor_running = False
        self.overhead_data = []
        self.overhead_lock = threading.Lock()
        self.overhead_output_file = (
            "/home/anon/ioext-bench/src/vm/assets/system/system_metric/overhead_metrics.json"
        )

        logger.debug(f"初始化场景 {scene_id}: {scene_name}, 参数: {parameters}")

    def prepare(self) -> None:
        """
        每轮运行前准备方法
        """
        # 重新编译智能体
        self._rebuild_agent()
        logger.info("准备智能体开销监测环境...")
        logger.info("智能体开销监测环境准备完成")

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

    def _identify_target_processes(self) -> Dict[str, List[int]]:
        """
        识别需要监控的目标进程

        Returns:
            Dict[str, List[int]]: 按类型分类的进程PID列表
        """
        processes = {"agent": [], "bpftrace": [], "auxiliary": [], "scx_schedulers": []}

        try:
            # 获取所有进程信息
            for proc_dir in os.listdir("/proc"):
                if not proc_dir.isdigit():
                    continue

                pid = int(proc_dir)
                try:
                    with open(f"/proc/{pid}/comm", "r") as f:
                        comm = f.read().strip()

                    with open(f"/proc/{pid}/cmdline", "r") as f:
                        cmdline = f.read().replace("\0", " ")

                    # 分类进程
                    if "sched_agent" in comm or "sched_agent" in cmdline:
                        processes["agent"].append(pid)
                    elif "bpftrace" in comm or "bpftrace" in cmdline:
                        processes["bpftrace"].append(pid)
                    elif "focus_pid_helper" in cmdline:
                        processes["auxiliary"].append(pid)
                    elif comm.startswith("scx_") or any(
                        sched in cmdline
                        for sched in [
                            "scx_bpfland",
                            "scx_lavd",
                            "scx_p2dq",
                            "scx_nest",
                            "scx_simple",
                            "scx_flash",
                            "scx_rusty",
                        ]
                    ):
                        processes["scx_schedulers"].append(pid)

                except (FileNotFoundError, PermissionError, ValueError):
                    continue

        except Exception as e:
            logger.error(f"识别目标进程失败: {e}")

        return processes

    def _collect_process_metrics(self, pids: List[int]) -> Dict[str, float]:
        """
        收集进程的CPU和内存指标

        Args:
            pids: 进程PID列表

        Returns:
            Dict[str, float]: 指标数据
        """
        total_cpu_time = 0
        total_memory = 0
        valid_pids = 0

        for pid in pids:
            try:
                # 读取CPU时间 (utime + stime)
                with open(f"/proc/{pid}/stat", "r") as f:
                    stat_data = f.read().split()
                    if len(stat_data) >= 15:
                        utime = int(stat_data[13])  # 用户态CPU时间
                        stime = int(stat_data[14])  # 内核态CPU时间
                        total_cpu_time += utime + stime

                # 读取内存使用 (RSS)
                with open(f"/proc/{pid}/status", "r") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            rss_kb = int(line.split()[1])
                            total_memory += rss_kb * 1024  # 转换为字节
                            break

                valid_pids += 1

            except (FileNotFoundError, PermissionError, ValueError):
                continue

        return {"cpu_time": total_cpu_time, "memory_bytes": total_memory, "process_count": valid_pids}

    def _overhead_monitor_loop(self) -> None:
        """
        开销监控循环线程
        """
        logger.debug("开销监控线程启动")
        last_metrics = {}

        while self.overhead_monitor_running:
            try:
                current_time = time.time()
                target_processes = self._identify_target_processes()

                current_metrics = {}
                for proc_type, pids in target_processes.items():
                    if pids:
                        current_metrics[proc_type] = self._collect_process_metrics(pids)

                        # 计算CPU使用率（如果有上一次的数据）
                        if proc_type in last_metrics:
                            time_diff = current_time - last_metrics[proc_type].get("timestamp", current_time)
                            if time_diff > 0:
                                cpu_diff = (
                                    current_metrics[proc_type]["cpu_time"]
                                    - last_metrics[proc_type]["cpu_time"]
                                )
                                cpu_percent = (cpu_diff / (time_diff * 100)) * 100  # 转换为百分比
                                current_metrics[proc_type]["cpu_percent"] = cpu_percent
                            else:
                                current_metrics[proc_type]["cpu_percent"] = 0
                        else:
                            current_metrics[proc_type]["cpu_percent"] = 0

                        current_metrics[proc_type]["timestamp"] = current_time
                    else:
                        current_metrics[proc_type] = {
                            "cpu_time": 0,
                            "memory_bytes": 0,
                            "process_count": 0,
                            "cpu_percent": 0,
                            "timestamp": current_time,
                        }

                # 保存数据
                with self.overhead_lock:
                    self.overhead_data.append({"timestamp": current_time, "metrics": current_metrics.copy()})

                    # 限制数据数量，避免内存过度使用
                    if len(self.overhead_data) > 720:  # 保留1小时的数据（5秒间隔）
                        self.overhead_data.pop(0)

                last_metrics = current_metrics

                # 等待下一个采样周期
                time.sleep(self.overhead_interval)

            except Exception as e:
                logger.error(f"开销监控线程异常: {e}")
                time.sleep(self.overhead_interval)

        logger.debug("开销监控线程结束")

    def _start_overhead_monitoring(self) -> None:
        """
        启动开销监控
        """
        if self.overhead_monitor_running:
            return

        logger.info("启动开销监控")
        self.overhead_monitor_running = True
        self.overhead_monitor_thread = threading.Thread(target=self._overhead_monitor_loop, daemon=True)
        self.overhead_monitor_thread.start()

    def _stop_overhead_monitoring(self) -> None:
        """
        停止开销监控
        """
        if not self.overhead_monitor_running:
            logger.info("开销监控已经停止")
            return

        logger.info("停止开销监控")
        self.overhead_monitor_running = False

        if self.overhead_monitor_thread and self.overhead_monitor_thread.is_alive():
            logger.info("等待开销监控线程结束...")
            self.overhead_monitor_thread.join(timeout=10)
            if self.overhead_monitor_thread.is_alive():
                logger.warning("开销监控线程未在超时时间内结束")
            else:
                logger.info("开销监控线程已结束")

        logger.info(f"开销监控已停止，共收集 {len(self.overhead_data)} 个数据点")

    def _calculate_overhead_stats(self) -> Dict[str, Any]:
        """
        计算开销统计数据

        Returns:
            Dict[str, Any]: 开销统计结果
        """
        if not self.overhead_data:
            return {}

        with self.overhead_lock:
            data = self.overhead_data.copy()

        stats = {"cpu_usage": {}, "memory_usage": {}}

        # 按进程类型分组计算统计
        proc_types = ["agent", "bpftrace", "auxiliary", "scx_schedulers"]

        for proc_type in proc_types:
            cpu_values = []
            memory_values = []

            for entry in data:
                if proc_type in entry["metrics"]:
                    cpu_values.append(entry["metrics"][proc_type]["cpu_percent"])
                    memory_values.append(entry["metrics"][proc_type]["memory_bytes"])

            if cpu_values:
                stats["cpu_usage"][proc_type] = {
                    "avg": sum(cpu_values) / len(cpu_values),
                    "peak": max(cpu_values),
                }
            else:
                stats["cpu_usage"][proc_type] = {"avg": 0, "peak": 0}

            if memory_values:
                stats["memory_usage"][proc_type] = {
                    "avg": sum(memory_values) / len(memory_values),
                    "peak": max(memory_values),
                }
            else:
                stats["memory_usage"][proc_type] = {"avg": 0, "peak": 0}

        # 计算总计
        total_cpu_avg = sum(stats["cpu_usage"][pt]["avg"] for pt in proc_types)
        total_cpu_peak = sum(stats["cpu_usage"][pt]["peak"] for pt in proc_types)
        total_mem_avg = sum(stats["memory_usage"][pt]["avg"] for pt in proc_types)
        total_mem_peak = sum(stats["memory_usage"][pt]["peak"] for pt in proc_types)

        stats["cpu_usage"]["total"] = {"avg": total_cpu_avg, "peak": total_cpu_peak}
        stats["memory_usage"]["total"] = {"avg": total_mem_avg, "peak": total_mem_peak}

        return stats

    def _write_overhead_data(self) -> None:
        """
        将完整的开销数据（原始数据+统计数据）写入JSON文件
        """
        if not self.overhead_data:
            logger.warning("没有开销数据可写入，跳过开销数据文件生成")
            return

        try:
            with self.overhead_lock:
                raw_data = self.overhead_data.copy()

            stats = self._calculate_overhead_stats()

            output_data = {
                "metadata": {
                    "scene_id": self.scene_id,
                    "scene_name": self.scene_name,
                    "start_time": self.start_time,
                    "end_time": self.end_time,
                    "duration": self.end_time - self.start_time if self.end_time and self.start_time else 0,
                    "collection_interval": self.overhead_interval,
                    "sample_count": len(raw_data),
                },
                "raw_data": raw_data,  # 完整的时间序列数据
                "statistics": stats,  # 聚合统计数据
            }

            # 确保输出目录存在
            os.makedirs(os.path.dirname(self.overhead_output_file), exist_ok=True)

            with open(self.overhead_output_file, "w", encoding="utf-8") as f:
                json.dump(output_data, f, indent=2, ensure_ascii=False)

            logger.info(f"开销数据已写入: {self.overhead_output_file} (原始数据: {len(raw_data)} 个采样点)")

        except Exception as e:
            logger.error(f"写入开销数据失败: {e}")

    def _rebuild_agent(self) -> None:
        """
        重新编译智能体
        """
        logger.info("开始重新编译智能体...")

        try:
            # 删除build目录
            if os.path.exists(self.agent_build_dir):
                logger.info(f"删除现有build目录: {self.agent_build_dir}")
                shutil.rmtree(self.agent_build_dir)

            # 创建build目录
            logger.info(f"创建build目录: {self.agent_build_dir}")
            os.makedirs(self.agent_build_dir, exist_ok=True)

            # 进入build目录
            original_dir = os.getcwd()
            os.chdir(self.agent_build_dir)

            try:
                # 运行cmake
                logger.info("运行cmake配置...")
                cmake_cmd = ["cmake", "-DENABLE_SCHED_SWITCH=ON", "-DENABLE_SCHEDULER_OUTPUT_SUPPRESS=ON", ".."]
                result = subprocess.run(cmake_cmd, check=True, capture_output=True, text=True)
                logger.debug(f"cmake输出: {result.stdout}")

                # 运行make
                logger.info("运行make编译...")
                make_cmd = ["make", f"-j{os.cpu_count()}"]
                result = subprocess.run(make_cmd, check=True, capture_output=True, text=True)
                logger.debug(f"make输出: {result.stdout}")

                logger.info("智能体重新编译完成")

            finally:
                # 恢复原始目录
                os.chdir(original_dir)

        except subprocess.CalledProcessError as e:
            logger.error(f"编译失败: {e.stderr}")
            raise
        except Exception as e:
            logger.error(f"重新编译智能体时发生错误: {e}")
            raise

    def _run(self) -> None:
        """
        智能体开销监测的核心逻辑

        启动sched_agent监控进程
        """
        self.start_time = time.time()
        logger.info(
            f"开始智能体开销监测，预计持续 {self.duration} 秒"
            if self.duration > 0
            else "开始智能体开销监测，将持续到收到停止信号"
        )

        # 启动开销监控
        self._start_overhead_monitoring()

        # 启动系统指标监控
        self._start_system_monitoring()

        # 等待监控完成或收到停止信号
        if self.duration <= 0:
            # 无限等待，直到收到停止信号
            self._should_stop.wait()
            logger.info("收到停止信号，结束智能体开销监测")
        else:
            # 等待固定时间或收到停止信号
            was_stopped = self._should_stop.wait(timeout=self.duration)
            if was_stopped:
                logger.info("收到停止信号，提前结束智能体开销监测")
            else:
                logger.info(f"达到设定的最大监测时间 {self.duration} 秒，结束智能体开销监测")

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
        logger.info("启动智能体开销监控")

        # 检查sched_agent是否存在
        if not os.path.exists(self.agent_path):
            logger.error(f"智能体开销监控工具不存在: {self.agent_path}")
            return

        # 检查配置文件是否存在
        if not os.path.exists(self.config_file):
            logger.error(f"智能体开销监控配置文件不存在: {self.config_file}")
            return

        # 启动sched_agent
        cmd = [self.agent_path, self.config_file]

        try:
            self.agent_process = self._run_with_privilege(cmd)
            logger.info(f"智能体开销监控启动成功，进程ID: {self.agent_process.pid}")
        except Exception as e:
            logger.error(f"智能体开销监控启动失败: {e}")

    def end(self) -> None:
        """
        结束智能体开销监测

        终止sched_agent进程
        """
        logger.info("结束智能体开销监测")

        # 终止sched_agent进程
        if self.agent_process and self.agent_process.poll() is None:
            pid = self.agent_process.pid
            logger.info(f"终止智能体开销监控进程 (PID: {pid})")
            try:
                # 使用kill -TERM命令发送终止信号
                kill_cmd = ["kill", "-TERM", str(pid)]
                self._run_with_privilege(kill_cmd)

                # 等待进程终止
                timeout = 30
                start_time = time.time()
                while time.time() - start_time < timeout:
                    if self.agent_process.poll() is not None:
                        logger.info("智能体开销监控进程已成功终止")
                        break
                    time.sleep(0.5)

                # 如果进程仍在运行，发送KILL信号强制终止
                if self.agent_process.poll() is None:
                    logger.warning("智能体开销监控进程未响应TERM信号，发送KILL信号强制终止")
                    kill_cmd = ["kill", "-KILL", str(pid)]
                    self._run_with_privilege(kill_cmd)
                    self.agent_process.wait(timeout=3)
            except Exception as e:
                logger.error(f"终止智能体开销监控进程时发生错误: {e}")

        # 停止开销监控并写入数据
        logger.info("开始停止开销监控...")
        self._stop_overhead_monitoring()
        logger.info("开始写入开销数据...")
        self._write_overhead_data()
        logger.info("开销数据写入完成")

        self.end_time = time.time()
        logger.info(f"智能体开销监测已结束，总持续时间: {self.end_time - self.start_time:.2f}秒")

    def cleanup(self) -> None:
        """
        每轮运行后清理方法

        无操作（保留SUDO_ASKPASS脚本）
        """
        logger.info("清理智能体开销监测资源")

    def collect(self) -> Dict[str, Any]:
        """
        收集监测数据

        Returns:
            Dict[str, Any]: 收集到的监测数据
        """
        logger.info("收集智能体开销监测数据")

        # 收集基本信息
        data = {
            "metadata": {
                "scene_id": self.scene_id,
                "scene_name": self.scene_name,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "duration": self.end_time - self.start_time if self.end_time and self.start_time else 0,
                "overhead_file": self.overhead_output_file,
            }
        }

        # 存储收集到的数据
        self.collected_data = data

        logger.info("智能体开销监测数据收集完成")
        return data

    def stat(self) -> List[str]:
        """
        返回结果文件路径列表

        Returns:
            List[str]: 结果文件路径列表
        """
        result_files = []

        # 如果开销文件不存在，尝试重新写入开销数据
        if not os.path.exists(self.overhead_output_file):
            logger.warning("开销数据文件缺失，尝试重新生成...")
            if self.overhead_data:  # 确保有开销数据
                self._write_overhead_data()
                logger.info("重新生成开销数据文件")
            else:
                logger.warning("没有开销数据可以写入")

        # 添加存在的开销数据文件
        if os.path.exists(self.overhead_output_file):
            result_files.append(self.overhead_output_file)
            logger.info(f"开销数据文件: {self.overhead_output_file}")
        else:
            logger.warning(f"开销数据文件不存在: {self.overhead_output_file}")

        if not result_files:
            logger.warning("未找到任何开销数据文件")

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
                    "overhead_file": data_path,
                }
            }

            # 设置开销文件路径
            self.overhead_output_file = data_path

            logger.info(f"成功反序列化元数据，开销数据文件: {data_path}")
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

        重新编译智能体并创建临时SUDO_ASKPASS脚本
        """

        # 创建临时SUDO_ASKPASS脚本
        self._create_askpass_script()

    def teardown(self) -> None:
        """
        一次性全局清理方法

        停止开销监控并删除临时SUDO_ASKPASS脚本
        """
        # 确保开销监控已停止
        self._stop_overhead_monitoring()

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
    parser = argparse.ArgumentParser(description="智能体开销监测")

    parser.add_argument("--id", default="agent_cost_scene", help="场景ID")
    parser.add_argument("--name", default="智能体开销监测", help="场景名称")
    parser.add_argument("--duration", type=int, default=300, help="监控持续时间(秒)")
    parser.add_argument("--overhead-interval", type=int, default=5, help="开销采样间隔(秒)")

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
            "overhead_interval": args.overhead_interval,
            "privilege": {
                "method": args.privilege_method,
                "askpass_script": args.askpass_script,
                "env_var": args.password_env,
            },
        }

        # 创建并运行场景
        logger.info(f"创建智能体开销监测场景: {args.id} - {args.name}")
        scene = AgentCostScene(args.id, args.name, parameters)

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
