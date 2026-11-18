from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from vm.scene.io_ext.common import fio_json_metrics


def load_json(path: str | Path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    return data if isinstance(data, dict) else {}


def complete_result_dirs(root: str | Path) -> Iterable[Path]:
    for path in sorted(Path(root).iterdir() if Path(root).is_dir() else []):
        if not path.is_dir() or path.name.startswith("_") or path.name == "analysis":
            continue
        metadata = load_json(path / "metadata.json")
        if metadata and not metadata.get("failed_scenes") and metadata.get("steps"):
            yield path


def raw_fio_metrics(result_dir: str | Path, step_index: int = 0) -> List[Tuple[str, Dict[str, Any], Path]]:
    result_dir = Path(result_dir)
    metrics: List[Tuple[str, Dict[str, Any], Path]] = []
    step_dir = result_dir / f"step_{step_index}"
    for scene_dir in sorted(step_dir.iterdir() if step_dir.is_dir() else []):
        if not scene_dir.is_dir():
            continue
        for path in scene_dir.glob("*.json"):
            data = load_json(path)
            if not isinstance(data.get("jobs"), list) or not data["jobs"]:
                continue
            parsed = fio_json_metrics(str(path))
            if parsed:
                metrics.append((scene_dir.name, parsed, path))
    return metrics


def monitoring_summary(result_dir: str | Path, scene_id: str, step_index: int = 0) -> Dict[str, Any]:
    scene_dir = Path(result_dir) / f"step_{step_index}" / scene_id
    for path in scene_dir.glob("*.json") if scene_dir.is_dir() else []:
        data = load_json(path)
        if "summary" in data or "counters" in data:
            return data
    return {}
