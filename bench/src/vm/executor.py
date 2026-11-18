#!/usr/bin/env python
"""
基准测试运行入口脚本

该脚本作为命令行工具，用于运行基准测试配置文件。
"""

import argparse
import importlib
import logging
import os
import sys
from types import ModuleType
from typing import Optional
from datetime import datetime
from pathlib import Path

from .controller import BenchmarkController


try:
    colorlog: Optional[ModuleType] = importlib.import_module("colorlog")
except ImportError:
    colorlog = None


def setup_logging(
    log_level: str = "INFO", log_file: Optional[str] = None, output_to_file: bool = True
) -> Optional[str]:
    """
    设置日志配置

    Args:
        log_level: 日志级别，默认为INFO
        log_file: 日志文件路径，如果提供则将日志输出到指定文件，否则输出到logs目录下的默认文件
        output_to_file: 是否将日志输出到文件，默认为True

    Returns:
        str: 日志文件的实际路径
    """
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"无效的日志级别: {log_level}")

    # 颜色映射配置
    color_mapping = {
        "DEBUG": "cyan",
        "INFO": "white",
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "bold_red",
    }

    # 创建彩色日志格式器 - 包含时间戳和name字段
    if colorlog is not None:
        colored_formatter = colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s - %(name)s - [%(levelname)s] %(message)s",
            log_colors=color_mapping,
            secondary_log_colors={},
            style="%",
            datefmt="%H:%M:%S",  # 使用简化的时间格式
        )
    else:
        colored_formatter = logging.Formatter(
            "%(asctime)s - %(name)s - [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
        )

    # 为文件日志创建标准格式器（无颜色）
    file_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 配置根日志器
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    root_logger.handlers = []  # 清除现有处理器

    # 创建并配置控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(colored_formatter)
    root_logger.addHandler(console_handler)

    # 确定日志文件路径
    actual_log_file = None
    if output_to_file:
        if log_file:
            # 使用用户指定的文件路径
            actual_log_file = log_file
        else:
            # 使用默认的日志目录和文件名
            logs_dir = Path("logs")
            logs_dir.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            actual_log_file = str(logs_dir / f"benchmark_{timestamp}.log")

        try:
            # 确保日志文件目录存在
            log_dir = os.path.dirname(actual_log_file)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir)

            file_handler = logging.FileHandler(actual_log_file, encoding="utf-8")
            file_handler.setFormatter(file_formatter)
            root_logger.addHandler(file_handler)
            root_logger.info(f"日志同时输出到文件: {actual_log_file}")
        except Exception as e:
            root_logger.error(f"设置日志文件失败: {e}")
            actual_log_file = None

    return actual_log_file


def main() -> None:
    """
    主函数，解析命令行参数并运行基准测试
    """
    parser = argparse.ArgumentParser(description="运行基准测试配置")
    parser.add_argument("--config", "-c", required=True, help="配置文件路径")
    parser.add_argument("--sequence", "-s", help="要运行的测试序列ID，不指定则使用配置中的默认序列")
    parser.add_argument(
        "--output",
        "-o",
        help="结果输出路径：以 .json 结尾时保存 legacy 汇总文件；否则作为完整任务序列结果目录",
    )
    parser.add_argument(
        "--log-level",
        "-l",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="日志级别 (默认: INFO)",
    )
    parser.add_argument("--log-file", "-f", help="日志文件路径，如果指定则将日志输出到指定文件")
    parser.add_argument(
        "--log-to-file", action="store_true", default=True, help="是否将日志输出到文件（默认开启）"
    )
    parser.add_argument("--no-log-to-file", action="store_false", dest="log_to_file", help="禁用日志文件输出")
    parser.add_argument(
        "--become-password", default="", help="设置用于提权的密码，将作为SUDO_PASSWORD环境变量"
    )
    parser.add_argument(
        "--monitoring",
        "-m",
        help=(
            "可选监控场景过滤器：指定本次运行只启用哪些监控场景。"
            "支持场景ID子串匹配（逗号分隔），例如："
            " --monitoring=bpf_hook,lock_contention"
            " 或 --monitoring=all（启用全部，默认行为）。"
            " 未列出的监控场景将被跳过（setup/prepare/run/collect 均不执行）。"
            " 工作负载场景（tenant_groups）不受此参数影响。"
        ),
    )

    args = parser.parse_args()

    # 设置日志
    log_file_path = setup_logging(args.log_level, args.log_file, args.log_to_file)
    logger = logging.getLogger("run_benchmark")

    # 检查配置文件是否存在
    if not os.path.exists(args.config):
        logger.error(f"配置文件不存在: {args.config}")
        sys.exit(1)

    try:
        # 如果提供了become_password，设置SUDO_PASSWORD环境变量
        if args.become_password:
            os.environ["SUDO_PASSWORD"] = args.become_password
            logger.debug("已设置SUDO_PASSWORD环境变量")

        # 初始化控制器
        controller = BenchmarkController(args.config)

        # 解析监控场景过滤器
        monitoring_filter = args.monitoring

        # 运行测试
        logger.info(f"开始运行基准测试，配置文件: {args.config}")
        if monitoring_filter:
            logger.info(f"监控场景过滤器: {monitoring_filter}")
        controller.run(
            sequence_id=args.sequence,
            output_path=args.output,
            log_file_path=log_file_path,
            monitoring_filter=monitoring_filter,
        )

        if controller.failed_scenes:
            logger.error("本次运行有 %d 个场景失败，结果不完整", len(controller.failed_scenes))
            sys.exit(1)

        logger.info("基准测试运行完成")
        logger.info("结果和日志已保存；若 --output 指向目录，则该目录就是本次任务序列结果目录")

    except Exception as e:
        logger.error(f"基准测试运行失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
