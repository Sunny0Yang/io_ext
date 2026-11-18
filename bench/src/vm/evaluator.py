"""
测试评估器模块

该模块提供了对测试序列结果进行评分的功能，可以对单个场景、步骤或整个测试序列计算评分。
"""

import os
import json
import logging
import importlib
import random
from typing import Dict, List, Any, Optional, Tuple, Type, Callable

from .scene.base_scene import Scene
from .config import ConfigParser, ConfigurationError

# 配置日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 系统指标类场景不参与评分计算；但只要出现在 metadata.steps 中，status 仍然必须为 success。
EXCLUDED_SCENE_IDS = [
    "bpftrace_metric",
    "command_metric",
    "system_metric",
    "system_metric_keepalive",
    "paper_system_metric",
    "bpf_space_overhead",
    "paper_bpf_space_overhead",
    "frame_bpf_space_overhead",
    "block_mq_latency",
    "block_mq_latency_keepalive",
    "paper_block_mq_latency",
    "io_stack_cpu_cost",
    "io_stack_cpu_cost_keepalive",
    "paper_io_stack_cpu_cost",
    "io_metric",
    "io_metric_keepalive",
    "paper_io_metric",
    "block_io_latency_cost",
    "scx_io_policy_switch",
    "scx_io_bypass_fallback",
    "scx_io_bpf_hook_cost",
    "scx_io_lock_contention",
    "scx_io_monitor_keepalive",
    "ioext_policy_transition",
    "frame_hot_background_fio",
    "frame_hot_detach_transition",
    "frame_hot_switch_transition",
    "frame_hot_system_metric",
    "frame_hot_block_io_latency_cost",
    "frame_hot_policy_switch",
    "frame_hot_bypass_fallback",
    "frame_hot_fallback_backlog",
    "frame_hot_switch_timeline",
    "frame_hot_bpf_space_overhead",
]


class ScoreAggregationStrategy:
    """
    评分聚合策略类，提供不同的方式组合多个场景评分为一个步骤评分
    """

    @staticmethod
    def average(scores: List[float]) -> float:
        """计算平均评分"""
        if not scores:
            return 0.0
        return sum(scores) / len(scores)

    @staticmethod
    def weighted_average(scores: List[float], weights: List[float]) -> float:
        """计算加权平均评分"""
        if not scores or not weights or len(scores) != len(weights):
            return 0.0

        total_weight = sum(weights)
        if total_weight == 0:
            return 0.0

        weighted_sum = sum(score * weight for score, weight in zip(scores, weights))
        return weighted_sum / total_weight

    @staticmethod
    def minimum(scores: List[float]) -> float:
        """取最小评分"""
        if not scores:
            return 0.0
        return min(scores)

    @staticmethod
    def maximum(scores: List[float]) -> float:
        """取最大评分"""
        if not scores:
            return 0.0
        return max(scores)


class Evaluator:
    """测试评估器类"""

    def __init__(self, config_path: Optional[str] = None, random_seed: Optional[int] = None):
        """
        初始化评估器

        Args:
            config_path: 配置文件路径，用于加载场景类
            random_seed: 随机数种子，用于生成随机评分
        """
        self.scene_classes: Dict[str, Type[Scene]] = {}  # 缓存已加载的场景类
        self.metadata: Optional[Dict[str, Any]] = None
        self.config: Dict[str, Any] = {}
        self.config_path = config_path
        self.config_parser: Optional[ConfigParser] = None

        # 初始化随机数生成器
        if random_seed is not None:
            random.seed(random_seed)

        # 如果提供了配置文件路径，加载配置
        if config_path:
            self.load_config(config_path)

    def load_config(self, config_path: str) -> Dict[str, Any]:
        """
        加载配置文件

        Args:
            config_path: 配置文件路径

        Returns:
            Dict[str, Any]: 解析后的配置
        """
        try:
            self.config_parser = ConfigParser(config_path)
            self.config = self.config_parser.parse()
            logger.info(f"已加载配置: {self.config.get('benchmark_name', '未命名')}")

            # 加载所有场景类
            self.load_scenes()

            return self.config
        except ConfigurationError as e:
            logger.error(f"加载配置失败: {str(e)}")
            raise

    def load_scenes(self) -> None:
        """
        加载所有场景类（不创建实例）

        Raises:
            ConfigurationError: 场景加载失败
        """
        if not self.config or "scenes" not in self.config:
            logger.warning("配置中没有场景定义，无法加载场景类")
            return

        for scene_id, scene_config in self.config["scenes"].items():
            try:
                scene_class = self._load_scene_class(scene_config["module_path"], scene_config["class_name"])

                self.scene_classes[scene_id] = scene_class
                logger.info(f"已加载场景类: {scene_id}")

            except (ImportError, AttributeError, TypeError) as e:
                logger.error(f"加载场景 {scene_id} 失败: {str(e)}")

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

    def _create_scene_instance(self, scene_id: str, scene_info: Dict[str, Any]) -> Optional[Scene]:
        """
        创建场景实例

        Args:
            scene_id: 场景ID
            scene_info: 场景信息

        Returns:
            Optional[Scene]: 场景实例，如果无法创建则返回None
        """
        # 仅从配置中查找场景类
        if self.config and "scenes" in self.config and scene_id in self.config["scenes"]:
            scene_config = self.config["scenes"][scene_id]

            if scene_id in self.scene_classes:
                scene_class = self.scene_classes[scene_id]

                return scene_class(
                    scene_id=scene_id,
                    scene_name=scene_info.get("name", scene_id),
                    parameters=scene_config.get("parameters", {}),
                )
            else:
                logger.warning(f"场景 {scene_id} 在配置中定义但未成功加载")
                return None
        else:
            logger.warning(f"场景 {scene_id} 未在配置中定义，无法创建实例")
            return None

    def _determine_scene_type(self, scene_id: str, scene_info: Dict[str, Any]) -> str:
        """
        根据场景信息确定场景类型

        Args:
            scene_id: 场景ID
            scene_info: 场景信息

        Returns:
            str: 场景类型 (interactive, batch, streaming, system)
        """
        # 首先从配置中查找场景类型
        if self.config and "scenes" in self.config and scene_id in self.config["scenes"]:
            scene_config = self.config["scenes"][scene_id]
            if "type" in scene_config:
                return scene_config["type"]

        # 如果找不到配置，从场景信息中获取
        return scene_info.get("type", "unknown")

    def _generate_random_score(self) -> float:
        """
        生成一个随机评分

        Returns:
            float: 随机评分 (0.0-100.0)
        """
        raise NotImplementedError("不再允许错误时随机评分")

    def load_metadata(self, metadata_path: str) -> Dict[str, Any]:
        """
        加载测试元数据

        Args:
            metadata_path: 元数据文件路径

        Returns:
            Dict[str, Any]: 测试元数据
        """
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata: Dict[str, Any] = json.load(f)
            self.metadata = metadata
            return metadata
        except Exception as e:
            logger.error(f"加载元数据失败: {e}")
            raise

    def evaluate_scene(
        self, scene_id: str, scene_info: Dict[str, Any], result_base_dir: str, step_index: int
    ) -> float:
        """
        评估单个场景

        Args:
            scene_id: 场景ID
            scene_info: 场景信息
            result_base_dir: 结果基础目录
            step_index: 步骤索引

        Returns:
            float: 场景评分
        """
        # 如果是系统指标类场景，跳过评分
        if scene_id in EXCLUDED_SCENE_IDS:
            logger.info(f"场景 {scene_id} 是系统指标类，不参与评分")
            return 0.0

        # 检查场景是否成功
        if scene_info.get("status", "") != "success":
            logger.warning(f"场景 {scene_id} 执行失败，评分为0")
            return 0.0

        try:
            # 创建场景实例
            scene = self._create_scene_instance(scene_id, scene_info)

            # 如果无法创建场景实例，使用随机评分
            if scene is None:
                random_score = self._generate_random_score()
                logger.warning(f"无法创建场景 {scene_id} 实例，使用随机评分: {random_score}")
                return random_score

            # 记录场景类信息，便于调试
            logger.debug(f"场景 {scene_id} 使用类: {scene.__class__.__name__}")

            # 构建场景数据目录路径
            scene_data_dir = os.path.join(result_base_dir, f"step_{step_index}", scene_id)

            # 找到场景数据文件（通常是一个 JSON 文件）
            data_files = [f for f in os.listdir(scene_data_dir) if f.endswith(".json")]
            if not data_files:
                logger.error(f"未找到场景 {scene_id} 的数据文件")
                return 0.0

            # 使用找到的第一个数据文件
            data_file = os.path.join(scene_data_dir, data_files[0])

            # 反序列化加载场景数据
            scene.deserialize(data_file)

            try:
                # 计算并返回场景评分
                score = scene.score()
                logger.info(f"场景 {scene_id} ({scene.__class__.__name__}) 评分: {score}")
                return score
            except NotImplementedError:
                # 如果评分方法未实现，生成随机评分
                random_score = self._generate_random_score()
                logger.warning(
                    f"场景 {scene_id} ({scene.__class__.__name__}) 评分方法未实现，使用随机评分: {random_score}"
                )
                return random_score

        except Exception as e:
            logger.error(f"评估场景 {scene_id} 失败: {e}")
            # 发生错误时，生成随机评分
            random_score = self._generate_random_score()
            logger.warning(f"使用随机评分: {random_score}")
            return random_score

    def is_step_successful(self, step_info: Dict[str, Any]) -> bool:
        """
        检查步骤是否成功执行

        Args:
            step_info: 步骤信息

        Returns:
            bool: 如果步骤成功则返回True，否则返回False
        """
        # 检查所有场景是否都成功。EXCLUDED_SCENE_IDS 只表示“不参与评分”，不表示失败可忽略。
        for scene_info in step_info.get("scenes", []):
            if scene_info.get("status", "") != "success":
                logger.warning(
                    f"步骤 {step_info.get('step_index')} 中的场景 {scene_info.get('scene_id')} 执行失败"
                )
                return False
        return True

    def is_global_scene_successful(self) -> bool:
        """检查 sequence 级 global_scene 是否成功；缺省表示该序列没有 global_scene。"""

        global_scene = self.metadata.get("global_scene") if self.metadata else None
        if not global_scene:
            return True
        if global_scene.get("status", "") != "success":
            logger.warning(
                f"全局场景 {global_scene.get('scene_id')} 执行失败: {global_scene.get('error', '')}"
            )
            return False
        return True

    def evaluate_step(
        self,
        step_info: Dict[str, Any],
        result_base_dir: str,
        aggregation_strategy: Callable[[List[float]], float] = ScoreAggregationStrategy.average,
    ) -> Tuple[float, List[Dict[str, Any]]]:
        """
        评估单个步骤

        Args:
            step_info: 步骤信息
            result_base_dir: 结果基础目录
            aggregation_strategy: 评分聚合策略

        Returns:
            Tuple[float, List[Dict[str, Any]]]: 步骤评分和场景评分详情
        """
        step_index = step_info["step_index"]

        # 检查步骤是否成功执行
        if not self.is_step_successful(step_info):
            logger.warning(f"步骤 {step_index}: {step_info['name']} 执行失败，评分为0")
            return 0.0, []

        scenes = step_info["scenes"]

        # 收集所有场景评分
        scene_scores = []
        scene_details = []

        for scene_info in scenes:
            scene_id = scene_info["scene_id"]
            scene_type = self._determine_scene_type(scene_id, scene_info)

            # 跳过系统指标类场景，不添加到结果中
            if scene_id in EXCLUDED_SCENE_IDS:
                logger.debug(f"跳过系统指标类场景 {scene_id}，不添加到结果中")
                continue

            scene_score = self.evaluate_scene(scene_id, scene_info, result_base_dir, step_index) * 100.0

            # 只有非系统指标类场景才计入评分
            scene_scores.append(scene_score)

            # 记录场景评分详情
            scene_details.append(
                {
                    "scene_id": scene_id,
                    "name": scene_info.get("name", scene_id),
                    "type": scene_type,
                    "score": scene_score,
                    "success": scene_info.get("status", "") == "success",
                }
            )

        # 使用指定的聚合策略计算步骤评分
        step_score = aggregation_strategy(scene_scores) if scene_scores else 0.0
        logger.info(f"步骤 {step_index}: {step_info['name']} 评分: {step_score}")

        return step_score, scene_details

    def evaluate_sequence(
        self,
        result_dir: str,
        scene_aggregation_strategy: Callable[[List[float]], float] = ScoreAggregationStrategy.average,
        include_failed_steps: bool = False,
    ) -> Dict[str, Any]:
        """
        评估整个测试序列

        Args:
            result_dir: 结果目录，应包含metadata.json
            scene_aggregation_strategy: 场景评分聚合策略
            include_failed_steps: 是否包含失败的步骤在最终评分中

        Returns:
            Dict[str, Any]: 评估结果
        """
        # 加载元数据
        metadata_path = os.path.join(result_dir, "metadata.json")
        self.load_metadata(metadata_path)
        if self.metadata is None:
            raise RuntimeError(f"元数据加载失败: {metadata_path}")
        metadata = self.metadata

        # 确保结果目录存在
        result_base_dir = os.path.abspath(result_dir)
        if not os.path.exists(result_base_dir):
            logger.error(f"结果目录不存在: {result_base_dir}")
            raise FileNotFoundError(f"结果目录不存在: {result_base_dir}")

        global_scene_success = self.is_global_scene_successful()

        # 评估每个步骤
        step_details = []

        for step_info in metadata["steps"]:
            step_score, scene_details = self.evaluate_step(
                step_info=step_info,
                result_base_dir=result_base_dir,
                aggregation_strategy=scene_aggregation_strategy,
            )

            step_success = self.is_step_successful(step_info) and global_scene_success
            if not global_scene_success:
                step_score = 0.0
                scene_details = []

            # 收集步骤详细信息
            step_details.append(
                {
                    "step_index": step_info["step_index"],
                    "name": step_info["name"],
                    "description": step_info.get("description", ""),
                    "score": step_score,
                    "success": step_success,
                    "scenes": scene_details,
                }
            )

        # 构建结果
        result = {
            "benchmark_name": metadata["benchmark_name"],
            "sequence_id": metadata["sequence_id"],
            "sequence_name": metadata["sequence_name"],
            "timestamp": metadata["timestamp"],
            "step_scores": step_details,
            "successful_steps": sum(1 for step in step_details if step["success"]),
            "total_steps": len(step_details),
            "global_scene": metadata.get("global_scene"),
            "global_scene_success": global_scene_success,
        }

        logger.info(f"评估完成 (成功步骤: {result['successful_steps']}/{result['total_steps']})")
        return result

    def save_evaluation_result(self, result: Dict[str, Any], output_path: str) -> str:
        """
        保存评估结果

        Args:
            result: 评估结果
            output_path: 输出路径

        Returns:
            str: 输出文件路径
        """
        # 确保输出目录存在
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # 保存结果
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        logger.info(f"评估结果已保存到: {output_path}")
        return output_path


def main():
    """主函数"""
    import argparse

    # 解析命令行参数
    parser = argparse.ArgumentParser(description="ioext-bench 评估工具")
    parser.add_argument("result_dir", help="测试结果目录，包含metadata.json")
    parser.add_argument("--config", "-c", help="配置文件路径，用于加载场景类", required=True)
    parser.add_argument("--output", "-o", default="./evaluation_result.json", help="评估结果输出文件路径")
    parser.add_argument(
        "--scene-strategy",
        choices=["average", "minimum", "maximum"],
        default="average",
        help="场景评分聚合策略",
    )
    parser.add_argument("--include-failed", action="store_true", help="是否包含失败的步骤在最终评分中")
    parser.add_argument("--random-seed", type=int, default=None, help="随机数种子，用于生成随机评分")

    args = parser.parse_args()

    # 选择聚合策略
    strategy_map = {
        "average": ScoreAggregationStrategy.average,
        "minimum": ScoreAggregationStrategy.minimum,
        "maximum": ScoreAggregationStrategy.maximum,
    }

    scene_strategy = strategy_map[args.scene_strategy]

    # 创建评估器并运行评估
    evaluator = Evaluator(config_path=args.config, random_seed=args.random_seed)
    result = evaluator.evaluate_sequence(
        result_dir=args.result_dir,
        scene_aggregation_strategy=scene_strategy,
        include_failed_steps=args.include_failed,
    )

    # 保存结果
    evaluator.save_evaluation_result(result, args.output)

    print(f"评估完成，结果已保存到: {args.output}")
    print(f"成功步骤: {result['successful_steps']}/{result['total_steps']}")


if __name__ == "__main__":
    main()
