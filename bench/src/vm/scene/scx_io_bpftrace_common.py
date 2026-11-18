"""
scx_io / block bpftrace monitoring scenes with marker-based JSON summaries.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .bpftrace_block_base import BpftraceBlockLayerScene, setup_logging

logger = logging.getLogger("bpftrace_block")

DEFAULT_RESULT_ROOT = "./test_results/block_bpf"


class MarkerBpftraceScene(BpftraceBlockLayerScene):
    """Shared scene implementation for bpftrace scripts that print END summaries."""

    SCRIPT_BASENAME = ""
    DEFAULT_OUTPUT_DIR = ""
    START_MARKER = ""
    SUMMARY_BEGIN_MARKER = ""
    SUMMARY_END_MARKER = ""
    SCALAR_MAPS: Sequence[str] = ()
    STATS_MAPS: Sequence[str] = ()
    HISTOGRAM_MAPS: Sequence[str] = ()
    REQUIRED_MAPS: Sequence[str] = ()
    # Per-direction attribution maps (printed as @name["read"]/["write"] beside
    # the keyless total). Parsed leniently: absent => zero-filled, never an
    # error; only keyless totals stay REQUIRED.
    DIRECTION_KEYS: Sequence[str] = ()
    DIRECTION_SCALAR_MAPS: Sequence[str] = ()
    DIRECTION_STATS_MAPS: Sequence[str] = ()
    DIRECTION_HISTOGRAM_MAPS: Sequence[str] = ()
    # count() map keyed by integer ids (e.g. task-cgroup id reference).
    CGID_MAP: str = ""
    RAW_TAIL_LINES = 120

    def __init__(self, scene_id: str, scene_name: str, parameters: Optional[Dict[str, Any]] = None):
        if not self.SCRIPT_BASENAME:
            raise ValueError(f"{self.__class__.__name__}.SCRIPT_BASENAME is required")
        parameters = dict(parameters or {})
        output_dir = _expand_path(parameters.get("output_dir", self.DEFAULT_OUTPUT_DIR or _default_output_dir(scene_id)))
        parameters.setdefault("output_file", os.path.join(output_dir, f"{scene_id}.bpftrace.log"))
        super().__init__(scene_id, scene_name, self.SCRIPT_BASENAME, parameters)
        self.output_file = _expand_path(self.output_file)
        self.output_dir = os.path.dirname(self.output_file) or output_dir
        self.summary_file = _expand_path(
            parameters.get("summary_file", os.path.join(self.output_dir, f"{scene_id}_summary.json"))
        )
        self.require_events = _bool_param(parameters.get("require_events", False))

    def setup(self) -> None:
        _validate_env_var_name(str(self.password_env_var))
        if self.privilege_method == "sudo_askpass":
            self._create_askpass_script()

    def prepare(self) -> None:
        if not os.path.isfile(self.script_path):
            raise FileNotFoundError(f"bpftrace script not found: {self.script_path}")
        if not _which_bpftrace():
            raise FileNotFoundError("bpftrace executable not found")
        os.makedirs(self.output_dir, exist_ok=True)
        self.start_time = None
        self.end_time = None
        self.collected_data = {}
        for path in [self.output_file, self.summary_file]:
            if os.path.exists(path):
                os.remove(path)

    def collect(self) -> Dict[str, Any]:
        data = self._build_collection_data(enforce_requirements=True)
        self.collected_data = data
        return data

    def stat(self):
        data = self.collected_data or self._build_collection_data(enforce_requirements=self.require_events)
        os.makedirs(os.path.dirname(self.summary_file), exist_ok=True)
        with open(self.summary_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        files = [self.summary_file]
        if os.path.exists(self.output_file):
            files.append(self.output_file)
        return files

    def deserialize(self, data_path: str) -> None:
        if data_path.endswith(".json"):
            with open(data_path, "r", encoding="utf-8") as f:
                self.collected_data = json.load(f)
            self.summary_file = data_path
        else:
            super().deserialize(data_path)

    def score(self) -> float:
        return 0.0

    def _build_collection_data(self, enforce_requirements: bool) -> Dict[str, Any]:
        raw_text = _read_text_if_exists(self.output_file)
        summary = self._parse_output(raw_text)
        data = {
            "metadata": {
                "scene_id": self.scene_id,
                "scene_name": self.scene_name,
                "script": self.script_basename,
                "script_path": self.script_path,
                "output_file": self.output_file,
                "summary_file": self.summary_file,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "duration": (self.end_time - self.start_time) if self.end_time and self.start_time else 0,
                "require_events": self.require_events,
                "returncode": self.agent_process.returncode if self.agent_process else None,
            },
            "summary": summary,
            "raw_tail": _last_lines(raw_text, self.RAW_TAIL_LINES),
        }
        if enforce_requirements and self.require_events:
            self._validate_required_summary(summary)
        return data

    def _parse_output(self, text: str) -> Dict[str, Any]:
        scalar_maps = {name: _parse_scalar_map(text, name) for name in self.SCALAR_MAPS}
        stats_maps = {name: _parse_stats_map(text, name) for name in self.STATS_MAPS}
        histogram_maps = {name: f"@{name}" in text for name in self.HISTOGRAM_MAPS}
        summary = {
            "start_marker_seen": bool(self.START_MARKER and self.START_MARKER in text),
            "summary_begin_seen": bool(self.SUMMARY_BEGIN_MARKER and self.SUMMARY_BEGIN_MARKER in text),
            "summary_end_seen": bool(self.SUMMARY_END_MARKER and self.SUMMARY_END_MARKER in text),
            "scalar_maps": scalar_maps,
            "stats_maps": stats_maps,
            "histogram_maps": histogram_maps,
            "line_count": len(text.splitlines()),
        }
        if self.DIRECTION_KEYS:
            summary["direction_stats"] = self._parse_direction_stats(text)
        if self.CGID_MAP:
            summary["cgroup_id_counts"] = _parse_keyed_count_map(text, self.CGID_MAP)
        return summary

    def _parse_direction_stats(self, text: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for direction in self.DIRECTION_KEYS:
            entry: Dict[str, Any] = {}
            for name in self.DIRECTION_SCALAR_MAPS:
                entry[name] = _parse_keyed_scalar_map(text, name, direction)
            for name in self.DIRECTION_STATS_MAPS:
                entry[name] = _parse_keyed_stats_map(text, name, direction)
            for name in self.DIRECTION_HISTOGRAM_MAPS:
                entry[name] = (
                    f'@{name}[{direction}]' in text or f'@{name}["{direction}"]' in text
                )
            result[direction] = entry
        return result

    def _validate_required_summary(self, summary: Dict[str, Any]) -> None:
        missing = []
        for key in ["start_marker_seen", "summary_begin_seen", "summary_end_seen"]:
            if not summary.get(key):
                missing.append(key)
        scalar_maps = summary.get("scalar_maps", {})
        stats_maps = summary.get("stats_maps", {})
        histogram_maps = summary.get("histogram_maps", {})
        for map_name in self.REQUIRED_MAPS:
            if map_name in scalar_maps and scalar_maps.get(map_name, 0) <= 0:
                missing.append(map_name)
            elif map_name in stats_maps and stats_maps.get(map_name, {}).get("count", 0) <= 0:
                missing.append(f"{map_name}.count")
            elif map_name in histogram_maps and not histogram_maps.get(map_name):
                missing.append(map_name)
            elif map_name not in scalar_maps and map_name not in stats_maps and map_name not in histogram_maps:
                missing.append(map_name)
        if missing:
            raise RuntimeError(f"{self.scene_id} missing required BPF evidence: {', '.join(missing)}")

    def _create_askpass_script(self) -> None:
        if self.askpass_script_path:
            return
        fd, self.temp_askpass_script = tempfile.mkstemp(suffix=".sh")
        os.close(fd)
        with open(self.temp_askpass_script, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "${{{self.password_env_var}}}"\n')
        os.chmod(self.temp_askpass_script, 0o700)
        self.askpass_script_path = self.temp_askpass_script

    def _run_with_privilege(self, cmd: List[str], **popen_kwargs: Any) -> subprocess.Popen:
        if self.privilege_method == "sudo_askpass":
            env = {
                "PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
                "SUDO_ASKPASS": self.askpass_script_path,
            }
            if self.password_env_var in os.environ:
                env[self.password_env_var] = os.environ[self.password_env_var]
            return subprocess.Popen(["sudo", "-A"] + cmd, env=env, **popen_kwargs)
        if self.privilege_method in {"sudo_cache", "sudo_nopasswd"}:
            return subprocess.Popen(["sudo"] + cmd, **popen_kwargs)
        return subprocess.Popen(cmd, **popen_kwargs)


class BlockIoLatencyCostScene(MarkerBpftraceScene):
    """Merged block_mq_latency + io_stack_cpu_cost monitor."""

    SCRIPT_BASENAME = "block_io_latency_cost.bt"
    DEFAULT_OUTPUT_DIR = f"{DEFAULT_RESULT_ROOT}/block_io_latency_cost"
    START_MARKER = "BLOCK_IO_LATENCY_COST_START"
    SUMMARY_BEGIN_MARKER = "BLOCK_IO_LATENCY_COST_SUMMARY_BEGIN"
    SUMMARY_END_MARKER = "BLOCK_IO_LATENCY_COST_SUMMARY_END"
    SCALAR_MAPS = (
        "biolc_start_event_count",
        "biolc_insert_count",
        "biolc_issue_count",
        "biolc_complete_count",
        "biolc_stack_exact_match_count",
        "biolc_stack_approx_match_count",
        "biolc_stack_sample_count",
        "biolc_queue_sample_count",
        "biolc_exact_service_count",
        "biolc_approx_service_count",
        "biolc_service_sample_count",
        "biolc_e2e_sample_count",
    )
    STATS_MAPS = ("biolc_stack_path_ns", "biolc_queue_ns", "biolc_service_ns", "biolc_e2e_ns")
    HISTOGRAM_MAPS = (
        "biolc_stack_path_us",
        "biolc_stack_path_us_cpu",
        "biolc_queue_us",
        "biolc_service_us",
        "biolc_e2e_us",
    )
    REQUIRED_MAPS = ("biolc_issue_count", "biolc_complete_count", "biolc_stack_sample_count", "biolc_service_sample_count")


class M1PatternSamplerScene(MarkerBpftraceScene):
    """M1 solo load-pattern per-request sampler (m1_pattern_sampler.bt).

    Task B (P0-2 / M1) telemetry: per-request thinned dump lines (``M1P``)
    live in the raw bpftrace log; exact per-second count maps and per-direction
    size lhist are printed at END and parsed from the raw log by
    results/motivation_m1/tools/m1_metrics.py (this summary JSON only carries
    the keyless totals + e2e stats). ``require_events`` stays False: a quiet
    window (F6 idle lead-in / settle gaps) is legitimate sampler output, not
    a probe failure.
    """

    SCRIPT_BASENAME = "m1_pattern_sampler.bt"
    DEFAULT_OUTPUT_DIR = f"{DEFAULT_RESULT_ROOT}/m1_pattern_sampler"
    START_MARKER = "M1_SAMPLER_START"
    SUMMARY_BEGIN_MARKER = "M1_SAMPLER_SUMMARY_BEGIN"
    SUMMARY_END_MARKER = "M1_SAMPLER_SUMMARY_END"
    SCALAR_MAPS = (
        "m1_start_count",
        "m1_ins_count",
        "m1_first_ins_count",
        "m1_issue_count",
        "m1_complete_count",
        "m1_flush_issue_count",
        "m1_bytes_r",
        "m1_bytes_w",
        "m1_bytes_other",
        "m1_anchored_count",
        "m1_ins_anchor_count",
        "m1_start_anchor_count",
        "m1_anchor_missing_count",
        "m1_dump_count",
    )
    STATS_MAPS = ("m1_lat_ns",)
    HISTOGRAM_MAPS = ("m1_size_r", "m1_size_w")
    REQUIRED_MAPS = ("m1_issue_count", "m1_complete_count")


class BlockIoDwellScene(MarkerBpftraceScene):
    """Queue-dwell (first insert→issue) + service distribution monitor (dweller.bt).

    REQUIRED_MAPS intentionally omits dwell samples: on `none`/low iodepth
    the insert tracepoint is bypassed (blk_mq_try_issue_directly), so zero
    dwell samples with nonzero dw_bypass_count is the expected evidence, not
    a probe failure.

    dweller.bt v2 (DESIGN_TOPO.md §4.1) additionally attributes every
    dwell/service/e2e/path_q statistic and the issue tri-state by I/O
    direction in separate *_dir maps keyed "read"/"write" (keyless totals
    keep v1 names and semantics byte-identical; bpftrace 0.25.0 cannot mix
    keyless and keyed entries in one map). Direction is the tenant proxy
    (READ=train / WRITE=ckpt): ckpt buffered writes go through cgroup
    writeback (flusher kworkers in root cgroup), so task-cgroup attribution
    would misattribute them; cgroup_id_counts is informative-only. Direction
    maps are validated leniently (absent => zero-filled), totals stay
    REQUIRED; read+write issue counts must equal the total dw_issue_count.
    """

    SCRIPT_BASENAME = "dweller.bt"
    DEFAULT_OUTPUT_DIR = f"{DEFAULT_RESULT_ROOT}/dweller"
    START_MARKER = "DWELLER_START"
    SUMMARY_BEGIN_MARKER = "DWELLER_SUMMARY_BEGIN"
    SUMMARY_END_MARKER = "DWELLER_SUMMARY_END"
    SCALAR_MAPS = (
        "dw_start_count",
        "dw_insert_count",
        "dw_first_insert_count",
        "dw_issue_count",
        "dw_complete_count",
        "dw_dwell_sample_count",
        "dw_bypass_count",
        "dw_key_mismatch_count",
        "dw_path_q_sample_count",
        "dw_service_sample_count",
        "dw_e2e_sample_count",
    )
    STATS_MAPS = ("dw_dwell_ns", "dw_path_q_ns", "dw_service_ns", "dw_e2e_ns")
    HISTOGRAM_MAPS = ("dw_dwell_us", "dw_path_q_us", "dw_service_us", "dw_e2e_us")
    REQUIRED_MAPS = ("dw_issue_count", "dw_complete_count", "dw_service_sample_count")
    DIRECTION_KEYS = ("read", "write")
    DIRECTION_SCALAR_MAPS = (
        "dw_issue_count_dir",
        "dw_complete_count_dir",
        "dw_dwell_sample_count_dir",
        "dw_bypass_count_dir",
        "dw_key_mismatch_count_dir",
        "dw_path_q_sample_count_dir",
        "dw_service_sample_count_dir",
        "dw_e2e_sample_count_dir",
    )
    DIRECTION_STATS_MAPS = ("dw_dwell_ns_dir", "dw_path_q_ns_dir", "dw_service_ns_dir", "dw_e2e_ns_dir")
    DIRECTION_HISTOGRAM_MAPS = ("dw_dwell_us_dir", "dw_path_q_us_dir", "dw_service_us_dir", "dw_e2e_us_dir")
    CGID_MAP = "dw_cgid_count"


class ScxIoPolicySwitchScene(MarkerBpftraceScene):
    """Monitor policy replacement/detach agility and evacuation drain speed."""

    SCRIPT_BASENAME = "scx_io_policy_switch.bt"
    DEFAULT_OUTPUT_DIR = f"{DEFAULT_RESULT_ROOT}/scx_io_policy_switch"
    START_MARKER = "SCX_IO_POLICY_SWITCH_START"
    SUMMARY_BEGIN_MARKER = "SCX_IO_POLICY_SWITCH_SUMMARY_BEGIN"
    SUMMARY_END_MARKER = "SCX_IO_POLICY_SWITCH_SUMMARY_END"
    SCALAR_MAPS = (
        "sps_reg_ctx_count",
        "sps_unreg_ctx_count",
        "sps_prepare_count",
        "sps_prepare_detach_count",
        "sps_prepare_replace_count",
        "sps_prepare_offline_count",
        "sps_prepare_ok_count",
        "sps_schedule_count",
        "sps_workfn_count",
        "sps_workfn_return_count",
        "sps_policy_free_count",
        "sps_bypass_after_free_count",
        "sps_quiescing_dispatch_count",
    )
    STATS_MAPS = (
        "sps_reg_ctx_us",
        "sps_prepare_us",
        "sps_prepare_to_work_us",
        "sps_workfn_us",
        "sps_prepare_to_free_us",
        "sps_detach_unreg_to_prepare_us",
        "sps_detach_unreg_to_free_us",
        "sps_reg_to_free_us",
        "sps_free_to_reg_us",
        "sps_free_to_first_bypass_us",
        "sps_inflight_at_prepare",
        "sps_inflight_at_work",
        "sps_inflight_before_quiescing_dispatch",
        "sps_stall_rounds_at_prepare",
        "sps_stall_rounds_at_work",
    )
    HISTOGRAM_MAPS = ("sps_prepare_to_work_us_hist", "sps_workfn_us_hist", "sps_prepare_to_free_us_hist")
    REQUIRED_MAPS = ("sps_prepare_count", "sps_workfn_count")


class ScxIoBypassFallbackScene(MarkerBpftraceScene):
    """Monitor fallback/bypass reasons, direct bypass path latency, and BPF vs fallback dispatch origin."""

    SCRIPT_BASENAME = "scx_io_bypass_fallback.bt"
    DEFAULT_OUTPUT_DIR = f"{DEFAULT_RESULT_ROOT}/scx_io_bypass_fallback"
    START_MARKER = "SCX_IO_BYPASS_FALLBACK_START"
    SUMMARY_BEGIN_MARKER = "SCX_IO_BYPASS_FALLBACK_SUMMARY_BEGIN"
    SUMMARY_END_MARKER = "SCX_IO_BYPASS_FALLBACK_SUMMARY_END"
    SCALAR_MAPS = (
        "sbf_insert_requests_count",
        "sbf_bio_should_bypass_count",
        "sbf_bio_bypass_true_count",
        "sbf_bio_bypass_false_count",
        "sbf_bypass_request_count",
        "sbf_no_policy_count",
        "sbf_policy_unavailable_count",
        "sbf_policy_rejected_count",
        "sbf_quiesced_count",
        "sbf_pop_fallback_count",
        "sbf_pop_fallback_nonzero_count",
        "sbf_bpf_insert_count",
        "sbf_bpf_normal_dispatch_count",
        "sbf_bpf_dispatch_count",
        "sbf_dispatch_request_count",
        "sbf_blk_mq_dispatch_count",
    )
    STATS_MAPS = (
        "sbf_insert_requests_us",
        "sbf_bio_should_bypass_us",
        "sbf_bypass_request_us",
        "sbf_no_policy_us",
        "sbf_policy_unavailable_us",
        "sbf_policy_rejected_us",
        "sbf_quiesced_us",
        "sbf_pop_fallback_us",
        "sbf_bpf_in_flight_at_bypass",
        "sbf_fallback_in_flight_at_bypass",
        "sbf_dispatch_request_us",
        "sbf_blk_mq_dispatch_us",
    )
    HISTOGRAM_MAPS = (
        "sbf_bio_should_bypass_us_hist",
        "sbf_bypass_request_us_hist",
        "sbf_dispatch_request_us_hist",
    )
    REQUIRED_MAPS = ("sbf_bypass_request_count", "sbf_bio_bypass_true_count")

    def _validate_required_summary(self, summary: Dict[str, Any]) -> None:
        missing = []
        for key in ["start_marker_seen", "summary_begin_seen", "summary_end_seen"]:
            if not summary.get(key):
                missing.append(key)
        scalar_maps = summary.get("scalar_maps", {})
        request_bypass = scalar_maps.get("sbf_bypass_request_count", 0)
        bio_bypass = scalar_maps.get("sbf_bio_bypass_true_count", 0)
        if request_bypass <= 0 and bio_bypass <= 0:
            missing.append("sbf_bypass_request_count or sbf_bio_bypass_true_count")
        if missing:
            raise RuntimeError(f"scx_io_bypass_fallback missing required BPF evidence: {', '.join(missing)}")


class ScxIoBpfHookCostScene(MarkerBpftraceScene):
    """Approximate kernel↔BPF struct_ops boundary overhead in core scx_io hooks."""

    SCRIPT_BASENAME = "scx_io_bpf_hook_cost.bt"
    DEFAULT_OUTPUT_DIR = f"{DEFAULT_RESULT_ROOT}/scx_io_bpf_hook_cost"
    START_MARKER = "SCX_IO_BPF_HOOK_COST_START"
    SUMMARY_BEGIN_MARKER = "SCX_IO_BPF_HOOK_COST_SUMMARY_BEGIN"
    SUMMARY_END_MARKER = "SCX_IO_BPF_HOOK_COST_SUMMARY_END"
    SCALAR_MAPS = (
        "sbh_insert_requests_count",
        "sbh_dispatch_request_count",
        "sbh_policy_dispatch_rqptr_count",
        "sbh_completed_request_count",
        "sbh_prepare_request_count",
        "sbh_finish_request_count",
        "sbh_allow_merge_count",
        "sbh_bio_merge_count",
    )
    STATS_MAPS = (
        "sbh_insert_requests_ns",
        "sbh_dispatch_request_ns",
        "sbh_policy_dispatch_rqptr_ns",
        "sbh_completed_request_ns",
        "sbh_prepare_request_ns",
        "sbh_finish_request_ns",
        "sbh_allow_merge_ns",
        "sbh_bio_merge_ns",
    )
    HISTOGRAM_MAPS = (
        "sbh_insert_requests_us",
        "sbh_dispatch_request_us",
        "sbh_policy_dispatch_rqptr_us",
        "sbh_completed_request_us",
    )
    REQUIRED_MAPS = ("sbh_insert_requests_count", "sbh_dispatch_request_count")


class ScxIoLockContentionScene(MarkerBpftraceScene):
    """Filter lock contention down to scx_io-related lock classes."""

    SCRIPT_BASENAME = "scx_io_lock_contention.bt"
    DEFAULT_OUTPUT_DIR = f"{DEFAULT_RESULT_ROOT}/scx_io_lock_contention"
    START_MARKER = "SCX_IO_LOCK_CONTENTION_START"
    SUMMARY_BEGIN_MARKER = "SCX_IO_LOCK_CONTENTION_SUMMARY_BEGIN"
    SUMMARY_END_MARKER = "SCX_IO_LOCK_CONTENTION_SUMMARY_END"
    SCALAR_MAPS = (
        "slc_lock_contended_count",
        "slc_data_lock_contended_count",
        "slc_policy_lock_contended_count",
        "slc_hctx_lock_contended_count",
        "slc_iosched_sem_contended_count",
        "slc_arbiter_lock_contended_count",
        "slc_lock_acquired_after_contention_count",
    )
    STATS_MAPS = ("slc_wait_us", "slc_data_lock_wait_us", "slc_policy_lock_wait_us", "slc_hctx_lock_wait_us")
    HISTOGRAM_MAPS = ("slc_wait_us_hist",)
    REQUIRED_MAPS = ("slc_lock_contended_count",)


class ScxIoFallbackBacklogScene(MarkerBpftraceScene):
    """Monitor per-second fallback backlog depth and policy lifecycle events."""

    SCRIPT_BASENAME = "scx_io_fallback_backlog.bt"
    DEFAULT_OUTPUT_DIR = f"{DEFAULT_RESULT_ROOT}/scx_io_fallback_backlog"
    START_MARKER = "SCX_IO_FALLBACK_BACKLOG_START"
    SUMMARY_BEGIN_MARKER = "SCX_IO_FALLBACK_BACKLOG_SUMMARY_BEGIN"
    SUMMARY_END_MARKER = "SCX_IO_FALLBACK_BACKLOG_SUMMARY_END"
    SCALAR_MAPS = (
        "sbf_insert_requests_count",
        "sbf_bio_should_bypass_count",
        "sbf_bio_bypass_true_count",
        "sbf_bio_bypass_false_count",
        "sbf_bypass_request_count",
        "sbf_no_policy_count",
        "sbf_policy_unavailable_count",
        "sbf_policy_rejected_count",
        "sbf_quiesced_count",
        "sbf_dispatch_request_count",
        "sbf_fallback_dispatch_count",
        "sbf_bpf_normal_dispatch_count",
        "sbf_bpf_dispatch_count",
        "sbf_blk_mq_dispatch_count",
        "sbf_prepare_count",
        "sbf_prepare_detach_count",
        "sbf_prepare_replace_count",
        "sbf_free_count",
    )
    STATS_MAPS = (
        "sbf_bypass_func_us",
        "sbf_bio_should_bypass_us",
        "sbf_fallback_residency_us",
        "sbf_dispatch_func_us",
        "sbf_blk_mq_dispatch_us",
    )
    HISTOGRAM_MAPS = (
        "sbf_bypass_request_us_hist",
        "sbf_pop_fallback_us_hist",
        "sbf_bio_should_bypass_us_hist",
    )
    REQUIRED_MAPS = ("sbf_bypass_request_count",)

    # Timeline sample parsing
    TIMELINE_MARKER = "TIMELINE_SAMPLE"

    def _parse_output(self, text: str) -> dict:
        summary = super()._parse_output(text)
        # Parse TIMELINE_SAMPLE lines. Current scripts emit 100 ms samples,
        # while older result logs may contain one sample per second.
        samples = []
        for line in text.splitlines():
            if line.startswith("TIMELINE_SAMPLE "):
                try:
                    sample = _parse_timeline_sample(line)
                    if sample:
                        samples.append(sample)
                except Exception:
                    pass
        summary["samples"] = samples
        summary["sample_count"] = len(samples)
        if samples:
            summary["backlog_peak"] = max(
                s.get("fallback_dispatch", 0) for s in samples
            )
            summary["backlog_final"] = samples[-1].get("fallback_dispatch", 0)
        else:
            summary["backlog_peak"] = 0
            summary["backlog_final"] = 0
        return summary


class ScxIoSwitchTimelineScene(MarkerBpftraceScene):
    """Bypass/dispatch timeline with policy lifecycle markers."""

    SCRIPT_BASENAME = "scx_io_switch_timeline.bt"
    DEFAULT_OUTPUT_DIR = f"{DEFAULT_RESULT_ROOT}/scx_io_switch_timeline"
    START_MARKER = "SCX_IO_SWITCH_TIMELINE_START"
    SUMMARY_BEGIN_MARKER = "SCX_IO_SWITCH_TIMELINE_SUMMARY_BEGIN"
    SUMMARY_END_MARKER = "SCX_IO_SWITCH_TIMELINE_SUMMARY_END"
    SCALAR_MAPS = (
        "sbt_insert_requests_count",
        "sbt_bio_should_bypass_count",
        "sbt_bio_bypass_true_count",
        "sbt_bio_bypass_false_count",
        "sbt_bypass_request_count",
        "sbt_no_policy_count",
        "sbt_policy_unavailable_count",
        "sbt_policy_rejected_count",
        "sbt_quiesced_count",
        "sbt_dispatch_request_count",
        "sbt_blk_mq_dispatch_count",
        "sbt_prepare_count",
        "sbt_prepare_detach_count",
        "sbt_prepare_replace_count",
        "sbt_free_count",
        "sbt_bypass_after_free_count",
    )
    STATS_MAPS = (
        "sbt_insert_requests_us",
        "sbt_bio_should_bypass_us",
        "sbt_bypass_request_us",
        "sbt_dispatch_request_us",
        "sbt_blk_mq_dispatch_us",
        "sbt_residency_us",
        "sbt_prepare_us",
        "sbt_bpf_in_flight_at_bypass",
        "sbt_fallback_in_flight_at_bypass",
        "sbt_free_to_first_bypass_us",
    )
    HISTOGRAM_MAPS = (
        "sbt_bio_should_bypass_us_hist",
        "sbt_residency_us_hist",
    )
    REQUIRED_MAPS = ("sbt_bypass_request_count", "sbt_bio_bypass_true_count")

    TIMELINE_MARKER = "TIMELINE_SAMPLE"

    def _parse_output(self, text: str) -> dict:
        summary = super()._parse_output(text)
        samples = []
        for line in text.splitlines():
            if line.startswith("TIMELINE_SAMPLE "):
                try:
                    sample = _parse_timeline_sample(line)
                    if sample:
                        samples.append(sample)
                except Exception:
                    pass
        summary["samples"] = samples
        summary["sample_count"] = len(samples)
        return summary


def _parse_timeline_sample(line: str) -> dict:
    """Parse a TIMELINE_SAMPLE line into a dict of metric values."""
    import re as _re
    result = {}
    for match in _re.finditer(r'(\w+)=(\d+)', line):
        key = match.group(1)
        if key == "ts":
            result[key] = int(match.group(2))
        else:
            result[key] = int(match.group(2))
    return result


def _which_bpftrace() -> str:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, "bpftrace")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    for candidate in ["/usr/bin/bpftrace", "/usr/local/bin/bpftrace"]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _default_output_dir(scene_id: str) -> str:
    return os.path.join(DEFAULT_RESULT_ROOT, scene_id)


def _expand_path(path: Any) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(str(path))))


def _bool_param(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _validate_env_var_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"invalid environment variable name: {name}")


def _read_text_if_exists(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _last_lines(text: str, limit: int) -> List[str]:
    return text.splitlines()[-limit:]


def _parse_scalar_map(text: str, name: str) -> int:
    matches = re.findall(rf"@{re.escape(name)}:\s*([0-9]+)", text)
    return int(matches[-1]) if matches else 0


def _parse_stats_map(text: str, name: str) -> Dict[str, float]:
    """Parse bpftrace stats() output: { .count = N, .average = A, .total = T }"""
    line_match = re.findall(rf"@{re.escape(name)}:\s*{{([^}}]+)}}", text)
    if not line_match:
        return {"count": 0, "avg": 0.0, "total": 0.0}
    inner = line_match[-1]
    return {
        "count": _extract_labeled_number(inner, ["count"]),
        "avg": _extract_labeled_number(inner, ["average", "avg"]),
        "total": _extract_labeled_number(inner, ["total"]),
    }


def _parse_keyed_scalar_map(text: str, name: str, key: str) -> int:
    # bpftrace 0.25 prints string keys unquoted (@name[read]); older/other
    # builds may print them quoted — accept both.
    matches = re.findall(rf'@{re.escape(name)}\["?{re.escape(key)}"?\]:\s*([0-9]+)', text)
    return int(matches[-1]) if matches else 0


def _parse_keyed_stats_map(text: str, name: str, key: str) -> Dict[str, float]:
    """Parse keyed stats() output: @name[key]: { .count = N, ... }"""
    matches = re.findall(rf'@{re.escape(name)}\["?{re.escape(key)}"?\]:\s*{{([^}}]+)}}', text)
    if not matches:
        return {"count": 0, "avg": 0.0, "total": 0.0}
    inner = matches[-1]
    return {
        "count": _extract_labeled_number(inner, ["count"]),
        "avg": _extract_labeled_number(inner, ["average", "avg"]),
        "total": _extract_labeled_number(inner, ["total"]),
    }


def _parse_keyed_count_map(text: str, name: str) -> Dict[str, int]:
    """Parse count() entries keyed by integer ids: @name[<id>]: N

    Last occurrence per id wins: bpftrace auto-prints uncleaned maps again
    after END, so each id appears in both the summary block and the exit
    dump with identical values.
    """
    result: Dict[str, int] = {}
    for key, value in re.findall(rf'@{re.escape(name)}\[([0-9]+)\]:\s*([0-9]+)', text):
        result[key] = int(value)
    return result


def _extract_labeled_number(text: str, labels: Sequence[str]) -> float:
    for label in labels:
        match = re.search(rf"\.?{label}\s*(?:=)?\s*([0-9]+(?:\.[0-9]+)?)", text)
        if match:
            return float(match.group(1))
    return 0.0


def _parse_common_args(default_id: str, default_name: str, default_output_dir: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=default_name)
    parser.add_argument("--id", default=default_id, help="场景 ID")
    parser.add_argument("--name", default=default_name, help="场景名称")
    parser.add_argument("--duration", type=int, default=300, help="采集时长(秒)，0 表示直到进程被中断")
    parser.add_argument("--output-dir", default=default_output_dir, help="输出目录")
    parser.add_argument("--output-file", default="", help="bpftrace 标准输出日志路径")
    parser.add_argument("--summary-file", default="", help="结构化 summary JSON 路径")
    parser.add_argument("--require-events", action="store_true", help="collect 阶段强制校验证据")
    parser.add_argument(
        "--privilege-method",
        choices=["sudo_askpass", "sudo_cache", "sudo_nopasswd", "none"],
        default="sudo_nopasswd",
        help="权限方式，none 为直接运行 bpftrace",
    )
    parser.add_argument("--askpass-script", default="", help="SUDO_ASKPASS 脚本路径")
    parser.add_argument("--password-env", default="SUDO_PASSWORD", help="密码环境变量名")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
    )
    return parser.parse_args()


def run_standalone(scene_cls: type, default_id: str, default_name: str) -> None:
    args = _parse_common_args(default_id, default_name, scene_cls.DEFAULT_OUTPUT_DIR)
    setup_logging(args.log_level)
    params: Dict[str, Any] = {
        "duration": args.duration,
        "output_dir": args.output_dir,
        "require_events": args.require_events,
        "privilege": {
            "method": args.privilege_method,
            "askpass_script": args.askpass_script,
            "env_var": args.password_env,
        },
    }
    if args.output_file:
        params["output_file"] = args.output_file
    if args.summary_file:
        params["summary_file"] = args.summary_file

    scene = scene_cls(args.id, args.name, params)
    try:
        scene.setup()
        scene.prepare()
        scene.start()
        scene.end()
        scene.collect()
        scene.stat()
        scene.cleanup()
        scene.teardown()
    except KeyboardInterrupt:
        logger.info("用户中断")
        scene.end()
        scene.cleanup()
        scene.teardown()
    except Exception:
        logger.exception("运行失败")
        scene.end()
        scene.cleanup()
        scene.teardown()
        raise
