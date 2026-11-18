#!/usr/bin/env python3

import argparse
import functools
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(WORKSPACE_ROOT / "src"))

from src.vm.config import ConfigParser


@functools.lru_cache(maxsize=16)
def _parse_config(config_path: str) -> Dict[str, Any]:
    return ConfigParser(config_path).parse()


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return data


def _raw_fio_jsons(scene_dir: Path) -> List[Path]:
    outputs: List[Path] = []
    for path in scene_dir.glob("*.json"):
        try:
            data = _load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        jobs = data.get("jobs")
        if isinstance(jobs, list) and jobs:
            outputs.append(path)
    return outputs


def _sequence_signature(sequence: Dict[str, Any]) -> Dict[str, Any]:
    signature = {
        key: sequence.get(key)
        for key in (
            "sequence_id",
            "io_ext_policy",
            "config_point",
            "knob_params",
            "workload",
            "isolation_ref",
        )
        if key in sequence
    }
    steps = []
    for step in sequence.get("steps") or []:
        if not isinstance(step, dict):
            continue
        monitoring = list(step.get("monitoring_scenes") or [])
        execution = [scene_id for scene_id in step.get("scenes") or [] if scene_id not in monitoring]
        step_signature: Dict[str, Any] = {"name": step.get("name"), "scenes": execution}
        if monitoring:
            step_signature["monitoring_scenes"] = monitoring
        steps.append(step_signature)
    signature["steps"] = steps
    return signature


def validate_result(config_path: Path, result_dir: Path, sequence_id: str | None = None) -> List[str]:
    errors: List[str] = []
    metadata_path = result_dir / "metadata.json"
    if not metadata_path.is_file():
        return [f"missing metadata.json: {metadata_path}"]

    metadata = _load_json(metadata_path)
    sequence_id = sequence_id or str(metadata.get("sequence_id") or "")
    if metadata.get("sequence_id") != sequence_id:
        errors.append(
            f"metadata sequence_id mismatch: expected {sequence_id!r}, got {metadata.get('sequence_id')!r}"
        )
    config = _parse_config(str(config_path.resolve()))
    sequence = next((item for item in config.get("test_sequences", []) if item.get("sequence_id") == sequence_id), None)
    if sequence is None:
        return [f"sequence {sequence_id!r} not found in {config_path}"]

    run_results_path = result_dir / "run_results.json"
    if not run_results_path.is_file():
        errors.append(f"missing run_results.json: {run_results_path}")
    else:
        run_results = _load_json(run_results_path)
        sequence_result = run_results.get(sequence_id)
        if not isinstance(sequence_result, dict):
            errors.append(f"run_results.json has no result object for sequence {sequence_id!r}")
        else:
            stored_sequence = sequence_result.get("sequence_config")
            if not isinstance(stored_sequence, dict):
                errors.append(f"run_results.json has no sequence_config for {sequence_id!r}")
            elif _sequence_signature(stored_sequence) != _sequence_signature(sequence):
                errors.append(f"run_results.json sequence signature differs from current config for {sequence_id!r}")

    isolation_ref = sequence.get("isolation_ref")
    if isolation_ref:
        reference_dir = result_dir.parent / str(isolation_ref)
        reference_errors = validate_result(config_path, reference_dir, str(isolation_ref))
        errors.extend(f"isolation_ref {isolation_ref}: {error}" for error in reference_errors)

    failed_scenes = metadata.get("failed_scenes") or {}
    if failed_scenes:
        errors.append(f"metadata contains {len(failed_scenes)} failed scene(s)")

    metadata_steps = metadata.get("steps") or []
    expected_steps = sequence.get("steps") or []
    if len(metadata_steps) != len(expected_steps):
        errors.append(f"step count mismatch: expected {len(expected_steps)}, got {len(metadata_steps)}")

    scene_configs = config.get("scenes") or {}
    for step_index, expected_step in enumerate(expected_steps):
        if step_index >= len(metadata_steps):
            break
        metadata_step = metadata_steps[step_index]
        scene_records = {
            record.get("scene_id"): record
            for record in metadata_step.get("scenes", [])
            if isinstance(record, dict) and record.get("scene_id")
        }
        expected_scene_ids = list(expected_step.get("scenes") or [])
        expected_scene_ids.extend(expected_step.get("monitoring_scenes") or [])
        for scene_id in expected_scene_ids:
            record = scene_records.get(scene_id)
            if record is None:
                errors.append(f"step {step_index}: missing scene record {scene_id}")
                continue
            if record.get("status") != "success":
                errors.append(f"step {step_index}: scene {scene_id} status={record.get('status')}")
                continue
            scene_cfg = scene_configs.get(scene_id) or {}
            if scene_cfg.get("module_path") != "vm.scene.io_ext.fio":
                continue
            scene_dir = result_dir / f"step_{step_index}" / scene_id
            raw_jsons = _raw_fio_jsons(scene_dir)
            if not raw_jsons:
                errors.append(f"step {step_index}: scene {scene_id} has no valid raw fio JSON")
                continue
            for raw_json in raw_jsons:
                data = _load_json(raw_json)
                jobs = data.get("jobs") or []
                total_ios = sum(
                    float(section.get("total_ios") or 0)
                    for job in jobs
                    if isinstance(job, dict)
                    for direction in ("read", "write", "trim")
                    for section in [job.get(direction)]
                    if isinstance(section, dict)
                )
                if total_ios <= 0:
                    errors.append(f"step {step_index}: {raw_json.name} contains zero completed IOs")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate one ioext-bench isol-bench result directory")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--result-dir", required=True, type=Path)
    parser.add_argument("--sequence")
    args = parser.parse_args()

    try:
        errors = validate_result(args.config, args.result_dir, args.sequence)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors = [str(exc)]
    if errors:
        print("INVALID")
        for error in errors:
            print(f"- {error}")
        return 1
    print("VALID")
    return 0


if __name__ == "__main__":
    sys.exit(main())
