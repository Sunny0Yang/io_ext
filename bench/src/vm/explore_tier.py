"""Apply hardware-tier workload scaling from workload_tiers.yaml.

When ``EXPLORE_TIER`` is set (e.g. ``large``), overlay tailbench QPS/MAXREQS and
fio numjobs/iodepth/runtime onto explore_scene_combo configs.  Step durations are
bumped so fio can finish without the controller SIGINT-killing noise workloads.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_STEP_DURATION_MARGIN_SEC = 45
_TAILBENCH_DURATION_MARGIN_SEC = 30


def _tiers_yaml_candidates() -> List[Path]:
    env_path = os.environ.get("EXPLORE_TIERS_YAML", "").strip()
    candidates: List[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    workspace = os.environ.get("PERF_ARENA_WORKSPACE", "").strip()
    if workspace:
        candidates.append(Path(workspace) / "scripts/explore/workload_tiers.yaml")
    repo = Path(__file__).resolve().parents[3]
    candidates.append(repo / "scripts/explore/workload_tiers.yaml")
    return candidates


def load_tier(name: str) -> Dict[str, Any]:
    for path in _tiers_yaml_candidates():
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tiers = data.get("tiers") or {}
        if name not in tiers:
            raise KeyError(f"tier {name!r} not found in {path}")
        tier = dict(tiers[name])
        tier["_source"] = str(path)
        return tier
    raise FileNotFoundError(
        "workload_tiers.yaml not found; set EXPLORE_TIERS_YAML or PERF_ARENA_WORKSPACE"
    )


def _scene_parameters(scene_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    params = scene_cfg.get("parameters")
    return params if isinstance(params, dict) else None


def _is_tailbench_scene(params: Dict[str, Any]) -> bool:
    return isinstance(params.get("tailbench"), dict)


def _is_fio_scene(params: Dict[str, Any]) -> bool:
    return isinstance(params.get("fio_global"), dict) or isinstance(params.get("fio_jobs"), list)


def _apply_tailbench_tier(params: Dict[str, Any], tier_tb: Dict[str, Any]) -> None:
    tailbench = params.setdefault("tailbench", {})
    if not isinstance(tailbench, dict):
        return
    env = tailbench.setdefault("env", {})
    if not isinstance(env, dict):
        env = {}
        tailbench["env"] = env
    for key in ("TBENCH_MAXREQS", "TBENCH_QPS", "TBENCH_WARMUPREQS", "TBENCH_IO_FILE", "TBENCH_IO_MODE"):
        if key in tier_tb:
            env[key] = str(tier_tb[key])
    # Only set default SLO when the scene did not declare an explicit slo group.
    if "slo_deadline_us" not in tailbench and "slo_deadline_us" in tier_tb:
        tailbench["slo_deadline_us"] = tier_tb["slo_deadline_us"]


def _apply_fio_tier(params: Dict[str, Any], tier_fio: Dict[str, Any], role: str) -> None:
    fio_global = params.setdefault("fio_global", {})
    if not isinstance(fio_global, dict):
        return
    role_cfg = tier_fio.get(role) or tier_fio.get("key") or {}
    if not isinstance(role_cfg, dict):
        role_cfg = {}
    runtime_sec = int(tier_fio.get("runtime_sec", fio_global.get("runtime_sec", 60)))
    fio_global["runtime_sec"] = runtime_sec
    fio_global["runtime"] = runtime_sec
    if "numjobs" in role_cfg:
        fio_global["numjobs"] = int(role_cfg["numjobs"])
    if "iodepth" in role_cfg:
        fio_global["iodepth"] = int(role_cfg["iodepth"])
    levels = params.get("concurrency_levels")
    if isinstance(levels, list) and levels:
        params["concurrency_levels"] = [int(fio_global.get("numjobs", levels[0]))]
    params["duration"] = runtime_sec + _STEP_DURATION_MARGIN_SEC


def _estimate_tailbench_wall_sec(tier_tb: Dict[str, Any]) -> int:
    maxreqs = int(str(tier_tb.get("TBENCH_MAXREQS", "100")))
    qps = max(1, int(str(tier_tb.get("TBENCH_QPS", "1"))))
    warmup = int(str(tier_tb.get("TBENCH_WARMUPREQS", "10")))
    return int(warmup / qps + maxreqs / qps) + _TAILBENCH_DURATION_MARGIN_SEC


def _apply_block_contention_tier(config: Dict[str, Any], tier: Dict[str, Any]) -> None:
    block = tier.get("block_contention") or {}
    if not isinstance(block, dict) or not block:
        return
    nr = block.get("nr_requests")
    drop_caches = block.get("drop_caches")

    def _patch_params(params: Dict[str, Any]) -> None:
        if not isinstance(params, dict):
            return
        if nr is not None:
            system = params.setdefault("system", {})
            if isinstance(system, dict):
                system["nr_requests"] = int(nr)
        if drop_caches:
            params["drop_caches_before_run"] = True

    for group_cfg in (config.get("tenant_groups") or {}).values():
        if not isinstance(group_cfg, dict):
            continue
        template = group_cfg.get("template") or {}
        if isinstance(template, dict):
            _patch_params(template.setdefault("parameters", {}))

    # tenant_groups templates are expanded before apply_explore_tier; patch concrete scenes too.
    for scene_cfg in (config.get("scenes") or {}).values():
        if not isinstance(scene_cfg, dict):
            continue
        params = scene_cfg.get("parameters")
        if isinstance(params, dict):
            _patch_params(params)


def apply_explore_tier(config: Dict[str, Any]) -> Dict[str, Any]:
    tier_name = os.environ.get("EXPLORE_TIER", "").strip()
    if not tier_name:
        return config

    tier = load_tier(tier_name)
    tier_fio = tier.get("fio") or {}
    tier_tb = tier.get("tailbench") or {}
    if not isinstance(tier_fio, dict) or not isinstance(tier_tb, dict):
        raise ValueError(f"tier {tier_name} missing fio/tailbench sections")

    result = copy.deepcopy(config)
    _apply_block_contention_tier(result, tier)
    scenes = result.get("scenes") or {}
    fio_runtime = int(tier_fio.get("runtime_sec", 60))
    step_duration = int(tier.get("step_duration_sec") or 0)
    if step_duration <= 0:
        step_duration = max(fio_runtime + _STEP_DURATION_MARGIN_SEC, _estimate_tailbench_wall_sec(tier_tb))
        if str(tier_tb.get("TBENCH_IO_MODE", "memory")).lower() not in ("", "memory"):
            # Sync disk I/O under fio noise can stretch tailbench well beyond QPS pacing.
            step_duration = max(step_duration, fio_runtime + 90)

    for scene_id, scene_cfg in scenes.items():
        if not isinstance(scene_cfg, dict):
            continue
        params = _scene_parameters(scene_cfg)
        if not params:
            continue
        if _is_tailbench_scene(params):
            _apply_tailbench_tier(params, tier_tb)
            tb_wall = _estimate_tailbench_wall_sec(tier_tb)
            if str(tier_tb.get("TBENCH_IO_MODE", "memory")).lower() not in ("", "memory"):
                tb_wall = max(tb_wall, fio_runtime + 90)
            params["duration"] = max(int(params.get("duration", 60)), tb_wall)
        elif _is_fio_scene(scene_cfg.get("parameters") or {}):
            role = "noise" if "noise" in scene_id or "randwrite" in scene_id else "key"
            _apply_fio_tier(params, tier_fio, role)

    for sequence in result.get("test_sequences") or []:
        if not isinstance(sequence, dict):
            continue
        for step in sequence.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if step.get("duration") is not None:
                step["duration"] = max(int(step["duration"]), step_duration)

    result.setdefault("explore_tier_applied", tier_name)
    return result
