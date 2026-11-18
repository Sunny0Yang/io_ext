"""
配置解析器模块

负责解析YAML配置文件，并将配置信息转换为Python对象。
支持场景配置文件引用与参数覆盖。
"""

from typing import Any, Dict, List, Optional
import copy
import yaml
from pathlib import Path
import os

from .explore_tier import apply_explore_tier


NO_GLOBAL_POLICY_IDS = {"", "none", "no_policy", "native", "native_none"}
NO_GLOBAL_POLICY_MODES = {
    "none",
    "no_policy",
    "native",
    "native_none",
    "spdk_internal_bdev_null",
    "spdk_internal_bdev_malloc",
    "spdk_bdev_null",
    "spdk_bdev_malloc",
}


class ConfigurationError(Exception):
    """配置错误异常类"""

    pass


class ConfigParser:
    """配置解析器类"""

    def __init__(self, config_path: str, base_dir: Optional[Path] = None):
        """
        初始化配置解析器

        Args:
            config_path: YAML配置文件路径
            base_dir: 配置文件基础目录，用于解析相对路径
        """
        self.config_path = Path(config_path)
        self.base_dir = base_dir or self.config_path.parent

        # 计算配置根目录（src/vm/configs/）
        if str(self.config_path).find("src/vm/configs") != -1:
            config_root_parts = str(self.config_path).split("src/vm/configs")
            self.config_root = Path(config_root_parts[0] + "src/vm/configs")
        else:
            self.config_root = self.base_dir

        self.config: Dict[str, Any] = {}
        self.scene_cache: Dict[str, Dict[str, Any]] = {}  # 缓存已加载的场景配置
        self.expanded_tenant_groups: Dict[str, List[Dict[str, Any]]] = {}

    def parse(self) -> Dict[str, Any]:
        """
        解析配置文件

        Returns:
            Dict[str, Any]: 解析后的配置字典

        Raises:
            ConfigurationError: 配置文件格式错误或缺少必要字段
        """
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                loaded_config = yaml.safe_load(f) or {}
            if not isinstance(loaded_config, dict):
                raise ConfigurationError("配置文件顶层必须是一个字典")
            self.config = loaded_config

            # 先把论文/多租户需要的高层配置语法编译成现有 controller 已支持的
            # concrete scenes + global_scene + step scenes。这样运行期仍保持普通 Scene 生命周期，
            # 不把“容器复制/策略 profile”语义散落到 controller 中。
            self._expand_high_level_config()

            # 处理场景继承和参数覆盖
            self._process_scenes()

            self._validate_config()
            self.config = apply_explore_tier(self.config)
            return self.config

        except yaml.YAMLError as e:
            raise ConfigurationError(f"YAML解析错误: {str(e)}")
        except FileNotFoundError:
            raise ConfigurationError(f"配置文件不存在: {self.config_path}")

    def _derive_module_path(self, scene_type: str, scene_id: str) -> str:
        """
        根据场景类型和ID推导模块路径

        Args:
            scene_type: 场景类型
            scene_id: 场景ID

        Returns:
            str: 推导出的模块路径
        """
        return f"vm.scene.{scene_type}.{scene_id}"

    def _derive_class_name(self, scene_id: str) -> str:
        """
        根据场景ID推导类名

        Args:
            scene_id: 场景ID

        Returns:
            str: 推导出的类名，采用驼峰命名+Scene后缀
        """
        # 将下划线格式转换为驼峰命名，并添加Scene后缀
        parts = scene_id.split("_")
        return "".join(part.capitalize() for part in parts) + "Scene"

    def _extract_id_from_path(self, base_scene_path: str) -> str:
        """
        从基础场景路径中提取场景ID

        Args:
            base_scene_path: 场景配置文件路径

        Returns:
            str: 提取出的场景ID
        """
        # 从路径中提取文件名（不含扩展名）作为场景ID
        return os.path.splitext(os.path.basename(base_scene_path))[0]

    def _load_base_scene(self, base_scene_path: str) -> Dict[str, Any]:
        """
        加载基础场景配置文件

        Args:
            base_scene_path: 基础场景配置文件路径

        Returns:
            Dict[str, Any]: 场景配置字典

        Raises:
            ConfigurationError: 加载基础场景配置失败
        """
        # 检查缓存中是否已有该场景配置
        if base_scene_path in self.scene_cache:
            return self.scene_cache[base_scene_path]

        try:
            # 处理路径：
            # 1. 如果是绝对路径，直接使用
            # 2. 如果是相对路径，优先从固定的配置根目录开始查找
            full_path = None

            if Path(base_scene_path).is_absolute():
                # 绝对路径
                full_path = Path(base_scene_path)
            else:
                # 相对路径 - 首先尝试从配置根目录查找
                root_based_path = self.config_root / base_scene_path
                if root_based_path.exists():
                    full_path = root_based_path
                else:
                    # 如果不存在，尝试从当前配置文件目录查找
                    full_path = self.base_dir / base_scene_path

                    # 如果还是不存在，尝试在scene子目录下查找
                    if not full_path.exists():
                        scene_path = self.config_root / "scene" / base_scene_path
                        if scene_path.exists():
                            full_path = scene_path

            with open(full_path, "r", encoding="utf-8") as f:
                scene_config = yaml.safe_load(f)

            # 如果配置中没有指定id，使用文件名作为id
            if "id" not in scene_config:
                scene_config["id"] = self._extract_id_from_path(base_scene_path)

            # 保存到缓存
            self.scene_cache[base_scene_path] = scene_config
            return scene_config

        except yaml.YAMLError as e:
            raise ConfigurationError(f"场景配置YAML解析错误: {str(e)}")
        except FileNotFoundError:
            # 更详细的错误信息，指示在哪些路径尝试查找
            if Path(base_scene_path).is_absolute():
                error_msg = f"场景配置文件不存在: {base_scene_path}"
            else:
                root_path = self.config_root / base_scene_path
                dir_path = self.base_dir / base_scene_path
                scene_path = self.config_root / "scene" / base_scene_path
                error_msg = (
                    f"场景配置文件不存在，尝试查找路径:\n1. {root_path}\n2. {dir_path}\n3. {scene_path}"
                )
            raise ConfigurationError(error_msg)
        except Exception as e:
            raise ConfigurationError(f"加载基础场景配置失败: {str(e)}")

    def _complete_scene_config(self, scene_config: Dict[str, Any], scene_id: str) -> Dict[str, Any]:
        """
        补全场景配置信息，包括自动推导module_path和class_name

        Args:
            scene_config: 场景配置字典
            scene_id: 场景ID

        Returns:
            Dict[str, Any]: 补全后的场景配置字典
        """
        # 复制一份以避免修改原始数据
        completed_config = scene_config.copy()

        # 获取场景ID，优先使用配置中的id，其次使用传入的scene_id
        config_id = completed_config.get("id", scene_id)

        # 如果没有指定module_path但指定了type，则自动推导
        if "module_path" not in completed_config and "type" in completed_config:
            completed_config["module_path"] = self._derive_module_path(completed_config["type"], config_id)

        # 如果没有指定class_name，则自动推导
        if "class_name" not in completed_config:
            completed_config["class_name"] = self._derive_class_name(config_id)

        return completed_config

    def _expand_high_level_config(self) -> None:
        """Expand declarative tenant/policy helpers before base_scene processing.

        Supported high-level keys are intentionally compiled away:
        - ``tenant_groups``: generate N concrete workload scene entries.
        - ``io_ext_policy_profiles``/``policy_profiles`` + sequence ``io_ext_policy``:
          generate a concrete ``scene/io_ext/loader.yml`` global scene and assign it to
          ``sequence.global_scene``.

        The controller/evaluator then only see the traditional ioext-bench shape.
        """

        self.expanded_tenant_groups = {}
        if not isinstance(self.config, dict):
            return
        self.config.setdefault("scenes", {})
        self._expand_tenant_groups()
        self._expand_step_group_references()
        self._expand_sequence_io_ext_policies()

    def _expand_tenant_groups(self) -> None:
        tenant_groups = self.config.get("tenant_groups") or {}
        if not tenant_groups:
            return
        if not isinstance(tenant_groups, dict):
            raise ConfigurationError("tenant_groups 必须是一个字典")

        scenes = self.config.setdefault("scenes", {})
        if not isinstance(scenes, dict):
            raise ConfigurationError("scenes 必须是一个字典")
        scene_combinations = self.config.setdefault("scene_combinations", {})
        if not isinstance(scene_combinations, dict):
            raise ConfigurationError("scene_combinations 必须是一个字典")

        for group_id, group_cfg in tenant_groups.items():
            if not isinstance(group_cfg, dict):
                raise ConfigurationError(f"tenant_groups.{group_id} 必须是一个字典")
            count = self._positive_int(
                group_cfg.get("count", group_cfg.get("tenant_count", group_cfg.get("replicas", 0))),
                f"tenant_groups.{group_id}.count",
            )
            template = copy.deepcopy(group_cfg.get("template"))
            if template is None:
                template = {
                    key: copy.deepcopy(value)
                    for key, value in group_cfg.items()
                    if key
                    not in {
                        "count",
                        "tenant_count",
                        "replicas",
                        "template",
                        "per_tenant",
                        "scene_id_template",
                        "id_template",
                        "name_template",
                    }
                }
            if not isinstance(template, dict):
                raise ConfigurationError(f"tenant_groups.{group_id}.template 必须是一个字典")

            id_template = str(group_cfg.get("scene_id_template") or group_cfg.get("id_template") or f"{group_id}_{{idx}}")
            name_template = group_cfg.get("name_template")
            per_tenant = group_cfg.get("per_tenant") or []
            execution_mode = str(group_cfg.get("execution_mode") or group_cfg.get("collapse_mode") or "").strip().lower()
            generated_scene_ids: List[str] = []
            generated_meta: List[Dict[str, Any]] = []
            cgroup_paths: Dict[str, str] = {}

            # fio_subjob mode: collect per-tenant data for later collapse into 1 scene.
            subjob_tenants: List[Dict[str, Any]] = []
            subjob_tenant_cfgs: List[Dict[str, Any]] = []

            for idx in range(count):
                base_context = {
                    "group_id": str(group_id),
                    "idx": idx,
                    "idx1": idx + 1,
                    "count": count,
                }
                scene_id = self._format_template(id_template, base_context)
                context = {**base_context, "tenant_id": scene_id, "scene_id": scene_id}
                tenant_cfg = self._apply_templates(copy.deepcopy(template), context)
                if not isinstance(tenant_cfg, dict):
                    raise ConfigurationError(f"tenant_groups.{group_id} 生成的 scene 必须是字典")

                override = self._tenant_override(per_tenant, idx, scene_id)
                if override:
                    tenant_cfg = self._deep_merge(tenant_cfg, self._apply_templates(override, context))

                tenant_cfg["id"] = str(tenant_cfg.get("id") or scene_id)
                if name_template and "name" not in tenant_cfg:
                    tenant_cfg["name"] = self._format_template(str(name_template), context)

                cgroup_path = self._extract_scene_cgroup_path(tenant_cfg)
                if cgroup_path:
                    if cgroup_path in cgroup_paths:
                        raise ConfigurationError(
                            f"tenant_groups.{group_id} 生成了重复 cgroup path: {cgroup_path} "
                            f"({cgroup_paths[cgroup_path]} 与 {scene_id})"
                        )
                    cgroup_paths[cgroup_path] = scene_id
                generated_meta.append({"scene_id": scene_id, "idx": idx, "idx1": idx + 1, "cgroup_path": cgroup_path})

                if execution_mode == "fio_subjob":
                    # Collect per-tenant data; don't create individual scenes.
                    subjob_tenant_cfgs.append(tenant_cfg)
                    subjob_tenants.append({
                        "job_name": scene_id,
                        "cgroup_path": cgroup_path or "",
                        "idx": idx,
                        "idx1": idx + 1,
                    })
                    continue

                if scene_id in scenes:
                    raise ConfigurationError(f"tenant_groups.{group_id} 生成了重复场景ID: {scene_id}")
                scenes[scene_id] = tenant_cfg
                generated_scene_ids.append(scene_id)

            if execution_mode == "fio_subjob":
                # Collapse N tenants into 1 scene with N fio subjobs.
                collapsed_scene_id = str(group_cfg.get("collapsed_scene_id") or group_cfg.get("scene_id") or f"{group_id}_subjob")
                if collapsed_scene_id in scenes:
                    raise ConfigurationError(f"tenant_groups.{group_id} 折叠场景ID冲突: {collapsed_scene_id}")
                collapsed_scene = self._build_subjob_scene(group_id, template, subjob_tenant_cfgs, subjob_tenants, count)
                collapsed_scene["id"] = collapsed_scene_id
                if name_template:
                    collapsed_scene.setdefault("name", self._format_template(str(name_template), {"group_id": str(group_id), "count": count}))
                scenes[collapsed_scene_id] = collapsed_scene
                generated_scene_ids = [collapsed_scene_id]
                # expanded_tenant_groups retains N metas for cgroup_policies_from_groups.

            scene_combinations.setdefault(group_id, generated_scene_ids)
            self.expanded_tenant_groups[group_id] = generated_meta

    def _build_subjob_scene(
        self,
        group_id: str,
        template: Dict[str, Any],
        tenant_cfgs: List[Dict[str, Any]],
        subjob_tenants: List[Dict[str, Any]],
        count: int,
    ) -> Dict[str, Any]:
        """Collapse N tenant templates into 1 scene with N fio subjobs.

        Merges shared parameters (fio_global, scheduler, target_device) from the
        first tenant; collects per-tenant cgroup paths and io controls into
        ``fio_subjob_tenants`` and ``fio_jobs`` for IoExtFioScene.
        """
        if not tenant_cfgs:
            raise ConfigurationError(f"tenant_groups.{group_id} fio_subjob 模式没有 tenant 可折叠")
        base_tenant = copy.deepcopy(tenant_cfgs[0])
        base_params = copy.deepcopy(base_tenant.get("parameters") or {})
        base_io_ext = copy.deepcopy(base_params.get("io_ext") or {})
        base_cgroup = copy.deepcopy(base_io_ext.get("cgroup") or {})
        base_fio_global = copy.deepcopy(base_params.get("fio_global") or {})

        fio_jobs: List[Dict[str, Any]] = []
        tenant_cgroups: List[Dict[str, Any]] = []
        for idx, tenant_cfg in enumerate(tenant_cfgs):
            params = tenant_cfg.get("parameters") or {}
            io_ext = params.get("io_ext") or {}
            cgroup = io_ext.get("cgroup") or {}
            cgroup_path = cgroup.get("path") or io_ext.get("cgroup_path") or ""
            fio_cgroup_path = cgroup_path
            if fio_cgroup_path.startswith("/sys/fs/cgroup/"):
                fio_cgroup_path = fio_cgroup_path[len("/sys/fs/cgroup/"):]
            elif fio_cgroup_path == "/sys/fs/cgroup":
                fio_cgroup_path = ""
            tenant_meta = subjob_tenants[idx] if idx < len(subjob_tenants) else {}
            job_name = str(tenant_meta.get("job_name") or f"{group_id}_t{idx + 1}")

            job_options: Dict[str, Any] = {}
            if fio_cgroup_path:
                job_options["cgroup"] = fio_cgroup_path
                job_options["cgroup_nodelete"] = 1
            # Per-tenant fio_global overrides (mixed_* class split, unfair_output
            # rate) must land on the job section: all subjobs share one [global].
            tenant_fio = params.get("fio_global") or {}
            for key in ("bs", "rw", "iodepth", "rate", "rwmixread"):
                if key in tenant_fio and tenant_fio[key] != base_fio_global.get(key):
                    job_options[key] = tenant_fio[key]
            fio_jobs.append({"name": job_name, **job_options})

            tenant_cgroups.append({
                "job_name": job_name,
                "cgroup_path": cgroup_path,
                "io_weight": cgroup.get("io_weight"),
                "io_max": cgroup.get("io_max") or cgroup.get("io.max"),
                "io_latency": cgroup.get("io_latency") or cgroup.get("io.latency"),
                "io_prio_class": cgroup.get("io_prio_class") or cgroup.get("io.prio.class"),
                "io_bfq_weight": cgroup.get("io_bfq_weight") or cgroup.get("io.bfq.weight"),
            })

        merged_params = copy.deepcopy(base_params)
        merged_params["fio_jobs"] = fio_jobs
        merged_params["fio_subjob_tenants"] = tenant_cgroups
        merged_params["execution_mode"] = "fio_subjob"
        merged_params["tenant_count"] = count
        if base_cgroup.get("path"):
            merged_params.setdefault("io_ext", base_io_ext)
            merged_params["io_ext"].setdefault("cgroup", {})
            merged_params["io_ext"]["cgroup"]["path"] = None
        if "group_reporting" in base_fio_global:
            base_fio_global["group_reporting"] = False
        merged_params["fio_global"] = base_fio_global
        return {
            "base_scene": base_tenant.get("base_scene", "scene/io_ext/fio.yml"),
            "parameters": merged_params,
        }

    def _expand_step_group_references(self) -> None:
        scene_combinations = self.config.get("scene_combinations") or {}
        sequences = self.config.get("test_sequences") or []
        if not isinstance(sequences, list):
            return
        for sequence in sequences:
            if not isinstance(sequence, dict):
                continue
            for step in sequence.get("steps", []) or []:
                if not isinstance(step, dict):
                    continue
                refs: List[Any] = []
                for key in ("tenant_group", "tenant_groups", "scene_group", "scene_groups", "scene_combination", "scene_combinations"):
                    refs.extend(self._as_list(step.get(key)))
                if not refs:
                    continue
                scenes = list(step.get("scenes", []) or [])
                for ref in refs:
                    ref_name = str(ref)
                    if isinstance(scene_combinations, dict) and ref_name in scene_combinations:
                        scenes.extend(str(scene_id) for scene_id in self._as_list(scene_combinations[ref_name]))
                    elif ref_name in self.expanded_tenant_groups:
                        scenes.extend(meta["scene_id"] for meta in self.expanded_tenant_groups[ref_name])
                    else:
                        raise ConfigurationError(f"步骤引用了未知 scene/tenant group: {ref_name}")
                step["scenes"] = self._dedupe_preserve_order(scenes)

    def _expand_sequence_io_ext_policies(self) -> None:
        profiles = self.config.get("io_ext_policy_profiles") or self.config.get("policy_profiles") or {}
        if profiles and not isinstance(profiles, dict):
            raise ConfigurationError("io_ext_policy_profiles/policy_profiles 必须是一个字典")
        scenes = self.config.setdefault("scenes", {})
        sequences = self.config.get("test_sequences") or []
        if not isinstance(sequences, list):
            return

        for sequence in sequences:
            if not isinstance(sequence, dict):
                continue
            policy_spec = sequence.get("io_ext_policy", sequence.get("policy_profile"))
            if policy_spec is None:
                continue
            sequence_id = str(sequence.get("sequence_id") or "sequence")
            policy_id, profile = self._resolve_policy_profile(policy_spec, profiles)
            if profile is None:
                continue
            if sequence.get("global_scene"):
                raise ConfigurationError(
                    f"测试序列 {sequence_id} 同时设置了 global_scene 与 io_ext_policy；请只保留高层策略或显式 global_scene 之一"
                )

            scene_id = str(profile.get("scene_id") or profile.get("generated_scene_id") or f"{sequence_id}_{policy_id}_global_policy")
            context = {"sequence_id": sequence_id, "policy_id": policy_id, "scene_id": scene_id}
            scene_cfg = self._policy_profile_to_scene(scene_id, policy_id, profile, context)
            scene_cfg = self._apply_templates(scene_cfg, context)
            if scene_id in scenes:
                raise ConfigurationError(f"io_ext_policy 为测试序列 {sequence_id} 生成了重复场景ID: {scene_id}")
            scenes[scene_id] = scene_cfg
            sequence["global_scene"] = scene_id

    def _resolve_policy_profile(self, policy_spec: Any, profiles: Dict[str, Any]) -> tuple[str, Optional[Dict[str, Any]]]:
        if isinstance(policy_spec, str):
            policy_id = policy_spec.strip()
            if policy_id.lower() in NO_GLOBAL_POLICY_IDS:
                return policy_id or "none", None
            if policy_id not in profiles:
                raise ConfigurationError(f"未知 io_ext_policy profile: {policy_id}")
            profile = copy.deepcopy(profiles[policy_id])
            if not isinstance(profile, dict):
                raise ConfigurationError(f"io_ext_policy profile {policy_id} 必须是字典")
            if profile.get("enabled") is False or str(profile.get("mode", "")).lower() in NO_GLOBAL_POLICY_MODES:
                return policy_id, None
            return policy_id, profile
        if isinstance(policy_spec, dict):
            profile = copy.deepcopy(policy_spec)
            if profile.get("enabled") is False or str(profile.get("mode", "")).lower() in NO_GLOBAL_POLICY_MODES:
                return str(profile.get("id", "inline_policy")), None
            return str(profile.get("id") or profile.get("name") or "inline_policy"), profile
        raise ConfigurationError("io_ext_policy 必须是字符串或字典")

    def _policy_profile_to_scene(
        self, scene_id: str, policy_id: str, profile: Dict[str, Any], context: Dict[str, Any]
    ) -> Dict[str, Any]:
        if "scene" in profile:
            scene_cfg = copy.deepcopy(profile["scene"])
            if not isinstance(scene_cfg, dict):
                raise ConfigurationError(f"io_ext_policy {policy_id}.scene 必须是字典")
        elif "parameters" in profile or "base_scene" in profile:
            scene_cfg = {
                key: copy.deepcopy(value)
                for key, value in profile.items()
                if key
                not in {
                    "enabled",
                    "mode",
                    "scene_id",
                    "generated_scene_id",
                    "cgroup_policies_from_groups",
                    "cgroup_policy_groups",
                }
            }
        else:
            parameters: Dict[str, Any] = {}
            for key in ("target_device", "scheduler", "output_dir", "duration"):
                if key in profile:
                    parameters[key] = copy.deepcopy(profile[key])
            parameters.setdefault("output_dir", f"./test_results/io_ext/policies/{scene_id}")
            parameters.setdefault("duration", 0)
            io_ext = copy.deepcopy(profile.get("io_ext") or {})
            parameters["io_ext"] = io_ext
            scene_cfg = {
                "base_scene": profile.get("base_scene", "scene/io_ext/loader.yml"),
                "name": profile.get("name", f"io-ext global policy {policy_id}"),
                "parameters": parameters,
            }

        scene_cfg.setdefault("base_scene", "scene/io_ext/loader.yml")
        scene_cfg["id"] = scene_id
        scene_cfg.setdefault("name", f"io-ext global policy {policy_id}")
        parameters = scene_cfg.setdefault("parameters", {})
        if not isinstance(parameters, dict):
            raise ConfigurationError(f"io_ext_policy {policy_id}.parameters 必须是字典")
        parameters.setdefault("output_dir", f"./test_results/io_ext/policies/{scene_id}")
        parameters.setdefault("duration", 0)
        io_ext = parameters.setdefault("io_ext", {})
        if not isinstance(io_ext, dict):
            raise ConfigurationError(f"io_ext_policy {policy_id}.parameters.io_ext 必须是字典")
        self._expand_cgroup_policies_from_groups(io_ext, profile, context)
        return scene_cfg

    def _expand_cgroup_policies_from_groups(
        self, io_ext: Dict[str, Any], profile: Dict[str, Any], context: Dict[str, Any]
    ) -> None:
        group_specs = profile.get("cgroup_policies_from_groups") or profile.get("cgroup_policy_groups") or io_ext.pop(
            "cgroup_policies_from_groups", io_ext.pop("cgroup_policy_groups", [])
        )
        if not group_specs:
            return
        cgroup_policies = list(io_ext.get("cgroup_policies") or [])
        for group_spec in self._as_list(group_specs):
            if not isinstance(group_spec, dict):
                raise ConfigurationError("cgroup_policies_from_groups 的每一项必须是字典")
            group_id = str(group_spec.get("tenant_group") or group_spec.get("group") or group_spec.get("scene_group") or "")
            if group_id not in self.expanded_tenant_groups:
                raise ConfigurationError(f"cgroup_policies_from_groups 引用了未知 tenant_group: {group_id}")
            for meta in self.expanded_tenant_groups[group_id]:
                cgroup_path = meta.get("cgroup_path")
                if not cgroup_path:
                    raise ConfigurationError(f"tenant_group {group_id} 的场景 {meta['scene_id']} 没有可用于 policy 的 cgroup path")
                tenant_context = {**context, **meta, "group_id": group_id, "tenant_id": meta["scene_id"]}
                entry = {
                    key: copy.deepcopy(value)
                    for key, value in group_spec.items()
                    if key not in {"tenant_group", "group", "scene_group", "name_template"}
                }
                entry.setdefault("name", group_spec.get("name_template", f"{group_id}_{meta['idx']}_policy"))
                entry.setdefault("policy_cgroup_path", cgroup_path)
                cgroup_policies.append(self._apply_templates(entry, tenant_context))
        io_ext["cgroup_policies"] = cgroup_policies

    @staticmethod
    def _positive_int(value: Any, field_name: str) -> int:
        try:
            count = int(value)
        except (TypeError, ValueError):
            raise ConfigurationError(f"{field_name} 必须是正整数")
        if count <= 0:
            raise ConfigurationError(f"{field_name} 必须是正整数")
        return count

    @staticmethod
    def _as_list(value: Any) -> List[Any]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        return [value]

    @staticmethod
    def _dedupe_preserve_order(values: List[Any]) -> List[str]:
        seen = set()
        deduped: List[str] = []
        for value in values:
            text = str(value)
            if text in seen:
                continue
            seen.add(text)
            deduped.append(text)
        return deduped

    @staticmethod
    def _tenant_override(per_tenant: Any, idx: int, scene_id: str) -> Dict[str, Any]:
        if isinstance(per_tenant, list) and idx < len(per_tenant) and isinstance(per_tenant[idx], dict):
            return copy.deepcopy(per_tenant[idx])
        if isinstance(per_tenant, dict):
            for key in (scene_id, str(idx), str(idx + 1)):
                value = per_tenant.get(key)
                if isinstance(value, dict):
                    return copy.deepcopy(value)
        return {}

    def _extract_scene_cgroup_path(self, scene_cfg: Dict[str, Any]) -> Optional[str]:
        parameters = scene_cfg.get("parameters") or {}
        if not isinstance(parameters, dict):
            return None
        io_ext = parameters.get("io_ext") or {}
        if isinstance(io_ext, dict):
            cgroup = io_ext.get("cgroup") or {}
            if isinstance(cgroup, dict) and cgroup.get("path"):
                return str(cgroup["path"])
            if io_ext.get("cgroup_path"):
                return str(io_ext["cgroup_path"])
        system = parameters.get("system") or {}
        if isinstance(system, dict) and system.get("cgroup_path"):
            return str(system["cgroup_path"])
        return None

    def _apply_templates(self, value: Any, context: Dict[str, Any]) -> Any:
        if isinstance(value, str):
            return self._format_template(value, context)
        if isinstance(value, list):
            return [self._apply_templates(item, context) for item in value]
        if isinstance(value, dict):
            return {key: self._apply_templates(item, context) for key, item in value.items()}
        return value

    @staticmethod
    def _format_template(value: str, context: Dict[str, Any]) -> str:
        class _TemplateContext(dict):
            def __missing__(self, key: str) -> str:
                return "{" + key + "}"

        try:
            return value.format_map(_TemplateContext(context))
        except (KeyError, IndexError, ValueError):
            return value

    def _deep_merge(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        result = copy.deepcopy(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    def _process_scenes(self) -> None:
        """
        处理场景继承和参数覆盖
        """
        if "scenes" not in self.config:
            return

        processed_scenes = {}
        for scene_id, scene_config in self.config["scenes"].items():
            if "base_scene" in scene_config:
                # 加载基础场景配置
                base_config = self._load_base_scene(scene_config["base_scene"])

                # 深拷贝基础配置以避免修改原始数据
                processed_config = {}
                for key, value in base_config.items():
                    if isinstance(value, dict):
                        processed_config[key] = value.copy()
                    else:
                        processed_config[key] = value

                # 合并非参数字段
                for key, value in scene_config.items():
                    if key not in ["base_scene", "parameters"]:
                        processed_config[key] = value

                # 合并参数 - 修复参数覆盖逻辑
                if "parameters" in scene_config:
                    if "parameters" not in processed_config:
                        processed_config["parameters"] = {}
                    processed_config["parameters"] = self._deep_merge(
                        processed_config["parameters"], scene_config["parameters"]
                    )

                # 补全场景配置信息（自动推导module_path和class_name）
                processed_config = self._complete_scene_config(processed_config, scene_id)

                processed_scenes[scene_id] = processed_config
            else:
                # 直接定义的场景，补全配置信息
                processed_scenes[scene_id] = self._complete_scene_config(scene_config.copy(), scene_id)

        self.config["scenes"] = processed_scenes

    def _validate_config(self) -> None:
        """
        验证配置文件的格式和必要字段

        Raises:
            ConfigurationError: 配置验证失败
        """
        required_fields = ["benchmark_name", "scenes", "test_sequences"]

        for field in required_fields:
            if field not in self.config:
                raise ConfigurationError(f"配置文件缺少必要字段: {field}")

        # 验证场景配置
        if not isinstance(self.config["scenes"], dict):
            raise ConfigurationError("scenes 必须是一个字典")

        for scene_id, scene_config in self.config["scenes"].items():
            if not isinstance(scene_config, dict):
                raise ConfigurationError(f"场景配置 {scene_id} 必须是一个字典")

            # 由于我们现在可以自动推导module_path和class_name，只需要验证type字段
            if "type" not in scene_config:
                raise ConfigurationError(f"场景 {scene_id} 缺少必要字段: type")

            # 如果显式指定了module_path或class_name，则仍需验证这些字段存在
            required_scene_fields = []
            if "module_path" in scene_config:
                required_scene_fields.append("module_path")
            if "class_name" in scene_config:
                required_scene_fields.append("class_name")

            for field in required_scene_fields:
                if not field in scene_config:
                    raise ConfigurationError(f"场景 {scene_id} 缺少必要字段: {field}")

        # 验证测试序列配置
        if not isinstance(self.config["test_sequences"], list):
            raise ConfigurationError("test_sequences 必须是一个列表")

        for sequence in self.config["test_sequences"]:
            if not isinstance(sequence, dict):
                raise ConfigurationError("每个测试序列必须是一个字典")

            required_sequence_fields = ["sequence_id", "steps"]
            for field in required_sequence_fields:
                if field not in sequence:
                    raise ConfigurationError(f"测试序列缺少必要字段: {field}")

            # 验证全局场景配置（可选）
            if "global_scene" in sequence:
                global_scene_id = sequence["global_scene"]
                if not isinstance(global_scene_id, str):
                    raise ConfigurationError(
                        f"测试序列 {sequence['sequence_id']} 的 global_scene 必须是字符串"
                    )

                # 检查全局场景是否在scenes配置中存在
                if global_scene_id not in self.config["scenes"]:
                    raise ConfigurationError(
                        f"测试序列 {sequence['sequence_id']} 的全局场景 {global_scene_id} 不存在于场景配置中"
                    )

                # 检查全局场景不能同时出现在任何步骤的场景列表中
                for step in sequence["steps"]:
                    step_scenes = step.get("scenes", [])
                    monitoring_scenes = step.get("monitoring_scenes", [])

                    if global_scene_id in step_scenes:
                        raise ConfigurationError(
                            f"测试序列 {sequence['sequence_id']} 的全局场景 {global_scene_id} 不能同时出现在步骤的scenes列表中"
                        )

                    if global_scene_id in monitoring_scenes:
                        raise ConfigurationError(
                            f"测试序列 {sequence['sequence_id']} 的全局场景 {global_scene_id} 不能同时出现在步骤的monitoring_scenes列表中"
                        )

    def get_scene_config(self, scene_id: str) -> Dict[str, Any]:
        """
        获取指定场景的配置

        Args:
            scene_id: 场景ID

        Returns:
            Dict[str, Any]: 场景配置字典

        Raises:
            ConfigurationError: 场景ID不存在
        """
        if scene_id not in self.config["scenes"]:
            raise ConfigurationError(f"场景ID不存在: {scene_id}")

        return self.config["scenes"][scene_id]

    def get_test_sequence(self, sequence_id: str) -> Optional[Dict[str, Any]]:
        """
        获取指定测试序列的配置

        Args:
            sequence_id: 测试序列ID

        Returns:
            Optional[Dict[str, Any]]: 测试序列配置字典，如果未找到则返回None
        """
        for sequence in self.config["test_sequences"]:
            if sequence["sequence_id"] == sequence_id:
                return sequence
        return None
