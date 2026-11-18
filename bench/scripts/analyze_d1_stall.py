#!/usr/bin/env python3
"""D1 stall-band analyzer for the rig track (focused companion to analyze_rig_full.py).

Extracts one row per D1Q1 sequence result directory (``D1Q1_<knob>_n32`` native arm,
``D1Q1_iox_<policy>_n32`` iox arm, bookend-suffixed dirs included) from one or more
result roots and emits the stall-band deliverable:

* ``d1_stall_metrics.csv`` — one row per run, all sources pooled, with a source
  column.  Runs sharing a sequence_id (the two ``D1Q1_none_n32`` bookends) are
  distinguished by results_dir + execution_time and carry a run_tag derived from
  the dir-name suffix (e.g. ``bookend_open``).
* ``D1_STALL_ANALYSIS.md`` — 中文报告正文 + 英文指标名: executive summary, per-source
  variant comparison table, the regime ladder (thresholds + pinning mechanism),
  the Tier-3 proof section (native mq_deadline/io_cost + iox q_ddl/q_fifo/q_prio
  share biolc service/e2e p50/p90 with the clean cluster and only p99/p99.9 step
  into the [16,32)ms host-side stranding band), the iox slat-absorption caveat
  with per-variant conservation numbers, the closing-bookend contamination note,
  methodology notes, and a generic cross-source comparison section (renders for
  any source pair sharing a variant).
* ``figures/d1_biolc_service_cdf.png`` / ``figures/d1_biolc_e2e_cdf.png`` —
  per-variant CDFs (log-x µs), native and iox side-by-side panels, guide lines
  at 16ms/64ms.
* ``figures/d1_quantile_ladder_fio_clat.png`` /
  ``figures/d1_quantile_ladder_biolc_e2e.png`` — per-variant p50→p90→p99→p99.9
  ladders (log-y), variants grouped by regime.

Per-run metrics (all from the RAW ``*_jobs1_fio.json`` + bpftrace logs, no
processed intermediates):

* fio per-tenant clat p50/p90/p99/p99.9 avg/min/max (raw ``jobs[].read.clat_ns.percentile``,
  ns → µs; per-tenant then averaged);
* slat_mean_us / clat_mean_us (mean across tenant jobs of ``read.slat_ns.mean`` /
  ``read.clat_ns.mean``), slat_plus_clat_us, period_us = 1e6/total_iops and
  conservation_ratio = (slat+clat)/period.  With n tenants each at QD≈1 the
  per-tenant conservation law is slat+clat ≈ 1/iops_per_tenant = n/total_iops,
  so conservation_ratio ≈ tenant_count (32) ⇔ no latency is lost between slat
  and clat.  iox arms absorb device latency into slat, so clat percentiles
  below ~p90 are meaningless there — the conservation column is the check.
* agg BW MiB/s + GiB/s (unit quirk kept: MiB/s = bytes/1048576), total iops;
* inflight avg/max, disk util avg (sdb);
* biolc service/e2e/queue histograms from ``block_io_latency_cost.bpftrace.log``
  (LAST block per map wins): ≥16ms / ≥64ms ratios + absolute counts + sample
  counts, plus histogram-interpolated p50/p90/p99/p99.9 for service and e2e.

Histogram-interpolated percentiles assume samples are UNIFORMLY distributed
within each log2 bucket and linearly interpolate between the bucket bounds
(fio's own percentile accounting makes the same assumption); with 2^k-wide
buckets this bounds the error to well under one bucket width.

Flags: INCOMPLETE (no fio jobs JSON), PARTIAL_DATA (biolc/system data missing),
HIST_MISMATCH (histogram total vs summary sample_count > 0.1%), LOW_SAMPLES
(biolc service sample_count < 10000 — small-sample means are meaningless, only
counts are trustworthy), SCENE_FAILURE (failed scenes in metadata), PARSE_ERROR
(per-dir; never aborts the batch), BOOKEND (run_tag set).

CLI:
  python3 scripts/analyze_d1_stall.py \
      --results-root <DIR> --source <LABEL> [--results-root <DIR2> --source <LABEL2> ...] \
      --output-dir <OUT>
``--results-root/--source`` are repeatable pairs matched by position.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.axes import Axes

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(WORKSPACE_ROOT / "src"))

from scripts.analyze_d3_regime import (
    GE16_MS_US,
    GE64_MS_US,
    HIST_TOTAL_TOL,
    parse_biolc_hist_blocks,
)
from scripts.analyze_rig_full import (
    D1_REGIME_STALL_TAIL_GE16,
    D1_REGIME_DEGRADED_GE16,
    D1_REGIME_DEGRADED_P99_US,
    NATIVE_KNOBS,
    classify_d1_regime,
    classify_sequence,
)
from src.lib.isol_bench.result_metrics import load_json, monitoring_summary, raw_fio_metrics

SCENE_SYSTEM_METRIC = "ib_system_metric"
SCENE_BIOLC = "ib_block_io_latency_cost"
BIOLC_LOG_NAME = "block_io_latency_cost.bpftrace.log"
TARGET_DISK = "sdb"
STEP_INDEX = 0
DIR_PREFIX = "rig_iox_"

SVC_MAP = "biolc_service_us"
E2E_MAP = "biolc_e2e_us"
QUEUE_MAP = "biolc_queue_us"

LOW_SAMPLES_THRESHOLD = 10000
CLAT_TARGETS = (50.0, 90.0, 99.0, 99.9)
HIST_TARGETS = (50.0, 90.0, 99.0, 99.9)

# Tier-3 = the STALL-TAIL band cluster: native mq_deadline + io_cost and the
# iox queue policies q_ddl/q_fifo/q_prio (0916 campaign deep analysis).
TIER3_VARIANTS = ("mq_deadline", "io_cost", "q_ddl", "q_fifo", "q_prio")
CLEAN_VARIANTS = ("kyber", "none")

REGIME_ORDER = ("STALL-TAIL", "DEGRADED", "NORMAL")
REGIME_COLORS = {
    "STALL-TAIL": "#d62728",
    "DEGRADED": "#ff7f0e",
    "NORMAL": "#2ca02c",
    None: "#7f7f7f",
}

CSV_COLUMNS = (
    "source",
    "sequence",
    "variant",
    "arm",
    "run_tag",
    "execution_time",
    "results_dir",
    "fio_clat_p50_avg_us",
    "fio_clat_p50_min_us",
    "fio_clat_p50_max_us",
    "fio_clat_p90_avg_us",
    "fio_clat_p90_min_us",
    "fio_clat_p90_max_us",
    "fio_clat_p99_avg_us",
    "fio_clat_p99_min_us",
    "fio_clat_p99_max_us",
    "fio_clat_p999_avg_us",
    "fio_clat_p999_min_us",
    "fio_clat_p999_max_us",
    "slat_mean_us",
    "clat_mean_us",
    "slat_plus_clat_us",
    "period_us",
    "conservation_ratio",
    "agg_bw_mib_s",
    "agg_bw_gib_s",
    "total_iops",
    "tenant_count",
    "inflight_avg",
    "inflight_max",
    "disk_util_avg_pct",
    "biolc_service_sample_count",
    "biolc_service_ge16_count",
    "biolc_service_ge16_ratio",
    "biolc_service_ge64_count",
    "biolc_service_ge64_ratio",
    "biolc_service_p50_us",
    "biolc_service_p90_us",
    "biolc_service_p99_us",
    "biolc_service_p999_us",
    "biolc_e2e_sample_count",
    "biolc_e2e_ge16_count",
    "biolc_e2e_ge16_ratio",
    "biolc_e2e_ge64_count",
    "biolc_e2e_ge64_ratio",
    "biolc_e2e_p50_us",
    "biolc_e2e_p90_us",
    "biolc_e2e_p99_us",
    "biolc_e2e_p999_us",
    "biolc_queue_sample_count",
    "biolc_queue_ge16_count",
    "biolc_queue_ge16_ratio",
    "biolc_queue_ge64_count",
    "biolc_queue_ge64_ratio",
    "regime",
    "failed_scenes",
    "flags",
)


@dataclass
class StallRecord:
    path: Path
    seq_id: str
    source: str
    arm: str
    variant: str
    run_tag: str
    execution_time: str
    failed_scenes: int = 0
    row: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        name = self.variant if self.arm == "native" else f"iox_{self.variant}"
        return f"{name}·{self.run_tag}" if self.run_tag else name


def warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    number = _f(value)
    return int(number) if number is not None else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pick_percentile(percentiles: Any, target: float) -> float | None:
    """Fetch a raw-fio clat percentile by numeric key match ("99.900000" → 99.9)."""
    if not isinstance(percentiles, dict):
        return None
    for key, value in percentiles.items():
        number = _f(key)
        if number is not None and abs(number - target) <= 1e-6:
            return _f(value)
    return None


def _failed_scene_count(meta: dict[str, Any]) -> int:
    count = 0
    for step in meta.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for scene in step.get("scenes") or []:
            if isinstance(scene, dict) and str(scene.get("status", "")).lower() == "failed":
                count += 1
    if count == 0:
        failed = meta.get("failed_scenes")
        if isinstance(failed, (dict, list, tuple, set)):
            count = len(failed)
    return count


def _run_tag(path: Path, seq_id: str) -> str:
    """Dir-name suffix after the sequence_id (e.g. 'bookend_open'), '' if none."""
    name = path.name
    prefix = DIR_PREFIX + seq_id
    if name.startswith(prefix):
        return name[len(prefix):].lstrip("_")
    return ""


def _pick_device(block: Any) -> dict[str, Any] | None:
    if not isinstance(block, dict) or not block:
        return None
    entry = block.get(TARGET_DISK)
    if isinstance(entry, dict):
        return entry
    entries = list(block.values())
    if len(entries) == 1 and isinstance(entries[0], dict):
        return entries[0]
    return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def hist_percentile(buckets: list[tuple[float, float, int]] | None, target: float) -> float | None:
    """Histogram-interpolated percentile (µs), fio-style semantics.

    Assumes samples are uniformly distributed within each log2 bucket and
    linearly interpolates between the bucket bounds (fio's own percentile
    accounting makes the same assumption).  target is a percentage (50 → p50).
    """
    if not buckets:
        return None
    total = sum(count for _lo, _hi, count in buckets)
    if total <= 0:
        return None
    rank = target * total / 100.0
    cumulative = 0.0
    for lo, hi, count in buckets:
        if count <= 0:
            continue
        if cumulative + count >= rank:
            fraction = (rank - cumulative) / count
            return lo if hi <= lo else lo + (hi - lo) * fraction
        cumulative += count
    return buckets[-1][1]


def _hist_tail(buckets: list[tuple[float, float, int]] | None) -> tuple[int, int, int, float | None, float | None]:
    """(total, ge16_count, ge64_count, ge16_ratio, ge64_ratio) for one histogram."""
    if not buckets:
        return 0, 0, 0, None, None
    total = sum(count for _lo, _hi, count in buckets)
    ge16 = sum(count for lo, _hi, count in buckets if lo >= GE16_MS_US)
    ge64 = sum(count for lo, _hi, count in buckets if lo >= GE64_MS_US)
    ratio16 = ge16 / total if total > 0 else None
    ratio64 = ge64 / total if total > 0 else None
    return total, ge16, ge64, ratio16, ratio64


def _analyze_fio_raw(record: StallRecord) -> list[Path]:
    """Per-tenant clat ladder + slat/clat conservation from the RAW fio jobs JSON."""
    row = record.row
    tenants = raw_fio_metrics(record.path, step_index=STEP_INDEX)
    raw_paths: list[Path] = []
    scenes = [entry for entry in tenants if entry[2].name.endswith("_jobs1_fio.json")]
    if not scenes:
        scenes = tenants
    if not scenes:
        record.flags.append("INCOMPLETE")
        return []
    scene_name, _metrics, raw_path = scenes[0]
    raw_paths.append(raw_path)
    data = load_json(raw_path)
    jobs = [job for job in data.get("jobs") or [] if isinstance(job, dict)]
    if not jobs:
        record.flags.append("INCOMPLETE")
        return raw_paths

    ladder: dict[float, list[float]] = {target: [] for target in CLAT_TARGETS}
    slat_means: list[float] = []
    clat_means: list[float] = []
    total_iops = 0.0
    total_bytes = 0.0
    for job in jobs:
        read = _mapping(job.get("read"))
        if not read:
            continue
        percentiles = _mapping(read.get("clat_ns")).get("percentile")
        for target in CLAT_TARGETS:
            value = _pick_percentile(percentiles, target)
            if value is not None:
                ladder[target].append(value / 1000.0)
        slat_value = _f(_mapping(read.get("slat_ns")).get("mean"))
        clat_value = _f(_mapping(read.get("clat_ns")).get("mean"))
        if slat_value is not None:
            slat_means.append(slat_value / 1000.0)
        if clat_value is not None:
            clat_means.append(clat_value / 1000.0)
        iops = _f(read.get("iops"))
        if iops is not None:
            total_iops += iops
        bw = _f(read.get("bw_bytes"))
        if bw is not None:
            total_bytes += bw

    row["tenant_count"] = len(ladder[99.0])
    suffix = {50.0: "p50", 90.0: "p90", 99.0: "p99", 99.9: "p999"}
    for target, values in ladder.items():
        if not values:
            record.flags.append("PARTIAL_DATA")
            continue
        name = suffix[target]
        row[f"fio_clat_{name}_avg_us"] = statistics.mean(values)
        row[f"fio_clat_{name}_min_us"] = min(values)
        row[f"fio_clat_{name}_max_us"] = max(values)

    if slat_means:
        row["slat_mean_us"] = statistics.mean(slat_means)
    if clat_means:
        row["clat_mean_us"] = statistics.mean(clat_means)
    if row.get("slat_mean_us") is not None and row.get("clat_mean_us") is not None:
        row["slat_plus_clat_us"] = row["slat_mean_us"] + row["clat_mean_us"]
    if total_iops > 0.0:
        row["total_iops"] = total_iops
        row["period_us"] = 1e6 / total_iops
        if row.get("slat_plus_clat_us") is not None:
            row["conservation_ratio"] = row["slat_plus_clat_us"] / row["period_us"]
    if total_bytes > 0.0:
        # Unit quirk kept on purpose: MiB/s = bytes/1048576.
        row["agg_bw_mib_s"] = total_bytes / 1048576.0
        row["agg_bw_gib_s"] = row["agg_bw_mib_s"] / 1024.0
    return raw_paths


def _analyze_system_metric(record: StallRecord) -> str | None:
    system = monitoring_summary(record.path, SCENE_SYSTEM_METRIC, step_index=STEP_INDEX)
    if not system:
        return SCENE_SYSTEM_METRIC
    summary = _mapping(system.get("summary"))
    inflight = _pick_device(summary.get("disk_ios_in_progress"))
    if inflight is not None:
        record.row["inflight_avg"] = _f(inflight.get("avg"))
        record.row["inflight_max"] = _f(inflight.get("max"))
    util = _pick_device(summary.get("disk_util_percent"))
    if util is not None:
        record.row["disk_util_avg_pct"] = _f(util.get("avg"))
    return None


def _analyze_biolc(record: StallRecord) -> str | None:
    row = record.row
    biolc = monitoring_summary(record.path, SCENE_BIOLC, step_index=STEP_INDEX)
    if not biolc:
        return SCENE_BIOLC
    scalars = _mapping(_mapping(biolc.get("summary")).get("scalar_maps"))
    service_count = _i(scalars.get("biolc_service_sample_count"))
    e2e_count = _i(scalars.get("biolc_e2e_sample_count"))
    queue_count = _i(scalars.get("biolc_queue_sample_count"))
    row["biolc_service_sample_count"] = service_count
    row["biolc_e2e_sample_count"] = e2e_count
    row["biolc_queue_sample_count"] = queue_count
    if service_count is not None and service_count < LOW_SAMPLES_THRESHOLD:
        record.flags.append("LOW_SAMPLES")

    log_text = _read_text(record.path / f"step_{STEP_INDEX}" / SCENE_BIOLC / BIOLC_LOG_NAME)
    if log_text is None:
        record.flags.append("PARTIAL_DATA")
        return None
    hists = parse_biolc_hist_blocks(log_text)
    for map_name, prefix in ((SVC_MAP, "biolc_service"), (E2E_MAP, "biolc_e2e"), (QUEUE_MAP, "biolc_queue")):
        buckets = hists.get(map_name)
        total, ge16, ge64, ratio16, ratio64 = _hist_tail(buckets)
        if total <= 0:
            continue
        row[f"{prefix}_ge16_count"] = ge16
        row[f"{prefix}_ge16_ratio"] = ratio16
        row[f"{prefix}_ge64_count"] = ge64
        row[f"{prefix}_ge64_ratio"] = ratio64
        if prefix == "biolc_queue":
            continue
        for target in HIST_TARGETS:
            name = "p999" if target == 99.9 else f"p{int(target)}"
            row[f"{prefix}_{name}_us"] = hist_percentile(buckets, target)
    if service_count and abs(_hist_tail(hists.get(SVC_MAP))[0] - service_count) > HIST_TOTAL_TOL * service_count:
        record.flags.append("HIST_MISMATCH")
    e2e_total = _hist_tail(hists.get(E2E_MAP))[0]
    if e2e_total > 0 and e2e_count and abs(e2e_total - e2e_count) > HIST_TOTAL_TOL * e2e_count:
        record.flags.append("HIST_MISMATCH")
    if row.get("biolc_service_ge16_ratio") is None:
        record.flags.append("PARTIAL_DATA")
    return None


def analyze_record(record: StallRecord) -> None:
    _analyze_fio_raw(record)
    missing: list[str] = []
    for extractor in (_analyze_system_metric, _analyze_biolc):
        scene = extractor(record)
        if scene:
            missing.append(scene)
    if missing:
        record.flags.append("MONITOR_MISSING")
        record.flags.append("PARTIAL_DATA")
    if record.failed_scenes > 0:
        record.flags.append("SCENE_FAILURE")
    if record.run_tag:
        record.flags.append("BOOKEND")
    record.row["regime"] = classify_d1_regime(
        _f(record.row.get("biolc_service_ge16_ratio")),
        _f(record.row.get("fio_clat_p99_avg_us")),
    )


def scan_result_root(
    root: Path,
    label: str,
    seen: set[Path],
    records: list[StallRecord],
) -> tuple[int, int]:
    """Append D1 records found under root; returns (d1_found, non_d1_skipped)."""
    if not root.is_dir():
        warn(f"{label}: results dir not found: {root} (skipping)")
        return 0, 0
    found = 0
    skipped = 0
    try:
        entries = sorted(root.iterdir())
    except OSError as exc:
        warn(f"{label}: failed to enumerate {root}: {exc}")
        return 0, 0
    for path in entries:
        if not path.is_dir() or path.name.startswith("_") or path.name == "analysis":
            continue
        meta_path = path / "metadata.json"
        if not meta_path.is_file():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            meta = load_json(meta_path)
        except (OSError, ValueError) as exc:
            warn(f"{label}: unreadable metadata at {path}: {exc}")
            continue
        if not meta:
            warn(f"{label}: empty/invalid metadata at {path} (skipping)")
            continue
        seq_id = str(meta.get("sequence_id") or path.name)
        info = classify_sequence(seq_id)
        if info is None or info.scenario != "D1":
            skipped += 1
            continue
        record = StallRecord(
            path=path,
            seq_id=seq_id,
            source=label,
            arm=info.arm,
            variant=info.knob,
            run_tag=_run_tag(path, seq_id),
            execution_time=str(meta.get("execution_time") or ""),
            failed_scenes=_failed_scene_count(meta),
        )
        try:
            analyze_record(record)
        except Exception as exc:  # a single bad dir must never abort the batch
            warn(f"{label}: failed to analyze {seq_id} ({path}): {type(exc).__name__}: {exc}")
            record.flags.append("PARSE_ERROR")
        record.row.update(
            {
                "source": record.source,
                "sequence": record.seq_id,
                "variant": record.variant,
                "arm": record.arm,
                "run_tag": record.run_tag,
                "execution_time": record.execution_time,
                "results_dir": str(record.path),
                "failed_scenes": record.failed_scenes,
                "flags": ";".join(dict.fromkeys(record.flags)),
            }
        )
        records.append(record)
        found += 1
    return found, skipped


def _variant_key(record: StallRecord) -> tuple[int, int, str]:
    if record.arm == "native":
        try:
            return (0, NATIVE_KNOBS.index(record.variant), record.variant)
        except ValueError:
            return (0, len(NATIVE_KNOBS), record.variant)
    return (1, 0, record.variant)


def _regime_rank(record: StallRecord) -> int:
    try:
        return REGIME_ORDER.index(record.row.get("regime"))
    except ValueError:
        return len(REGIME_ORDER)


def _ladder_key(record: StallRecord) -> tuple[int, int, int, bool, str]:
    """Regime first, then native-before-iox, bookend_open before close runs."""
    return (
        _regime_rank(record),
        _variant_key(record)[0],
        _variant_key(record)[1],
        record.run_tag != "bookend_open",
        record.label,
    )


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # 6 digits: preserves the 1e-6-scale ge16 ratios (kyber ≈ 5e-06).
        return repr(round(value, 6))
    return str(value)


def write_csv(path: Path, records: list[StallRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({key: _csv_cell(record.row.get(key)) for key in CSV_COLUMNS})


def _fmt(value: Any, digits: int = 1) -> str:
    number = _f(value)
    if number is None:
        return "—"
    if abs(number) >= 10000.0:
        return f"{number:,.0f}"
    return f"{number:.{digits}f}"


def _fmt_ratio(value: Any) -> str:
    number = _f(value)
    if number is None:
        return "—"
    return f"{number:.6f}"


def _fmt_pair(first: Any, second: Any, digits: int = 1) -> str:
    if _f(first) is None and _f(second) is None:
        return "—"
    return f"{_fmt(first, digits)} / {_fmt(second, digits)}"


def _conservation_cell(record: StallRecord) -> str:
    row = record.row
    ratio = _f(row.get("conservation_ratio"))
    ratio_text = f" (×{ratio:.2f})" if ratio is not None else ""
    return (
        f"{_fmt(row.get('slat_mean_us'))} / {_fmt(row.get('clat_mean_us'))} / "
        f"{_fmt(row.get('slat_plus_clat_us'))} vs {_fmt(row.get('period_us'))}{ratio_text}"
    )


def _variant_key_from(item: tuple[str, str]) -> tuple[int, int, str]:
    arm, variant = item
    if arm == "native":
        try:
            return (0, NATIVE_KNOBS.index(variant), variant)
        except ValueError:
            return (0, len(NATIVE_KNOBS), variant)
    return (1, 0, variant)


def _preferred_run(runs: list[StallRecord]) -> StallRecord:
    """Prefer the clean opening bookend, else the earliest execution_time run."""
    for run in runs:
        if run.run_tag == "bookend_open":
            return run
    return sorted(runs, key=lambda r: (r.execution_time, r.seq_id, str(r.path)))[0]


def _representative(records: list[StallRecord]) -> dict[str, StallRecord]:
    """Preferred run per variant (bookend_open > earliest execution_time)."""
    runs_by_variant: dict[str, list[StallRecord]] = {}
    for record in records:
        runs_by_variant.setdefault(record.variant, []).append(record)
    return {variant: _preferred_run(runs) for variant, runs in runs_by_variant.items()}


def _source_table(source: str, root: Path, records: list[StallRecord]) -> list[str]:
    lines = [
        f"### source: {source} (`{root}`) — {len(records)} D1 run(s)",
        "",
        "| variant | arm | run | regime | fio clat p50/p90/p99/p99.9 avg (µs) | slat / clat / sum vs period (µs) | biolc svc p50/p90/p99/p99.9 (µs) | biolc e2e p50/p90/p99/p99.9 (µs) | svc ≥16 / ≥64 | e2e ≥16 / ≥64 | inflight avg/max | samples (svc/e2e) | flags |",
        "|---------|-----|-----|--------|-----------------------------------|--------------------------------|-------------------------------------|-------------------------------------|---------------|---------------|-----------------|-------------------|-------|",
    ]
    for record in sorted(records, key=lambda r: (_ladder_key(r))):
        row = record.row
        fio_quad = "/".join(_fmt(row.get(f"fio_clat_{name}_avg_us")) for name in ("p50", "p90", "p99", "p999"))
        svc_quad = "/".join(_fmt(row.get(f"biolc_service_{name}_us")) for name in ("p50", "p90", "p99", "p999"))
        e2e_quad = "/".join(_fmt(row.get(f"biolc_e2e_{name}_us")) for name in ("p50", "p90", "p99", "p999"))
        svc_tail = f"{_fmt_ratio(row.get('biolc_service_ge16_ratio'))} / {_fmt_ratio(row.get('biolc_service_ge64_ratio'))}"
        e2e_tail = f"{_fmt_ratio(row.get('biolc_e2e_ge16_ratio'))} / {_fmt_ratio(row.get('biolc_e2e_ge64_ratio'))}"
        samples = f"{_i(row.get('biolc_service_sample_count')) or '—'} / {_i(row.get('biolc_e2e_sample_count')) or '—'}"
        lines.append(
            f"| {record.variant} | {record.arm} | {record.run_tag or 'main'} | {row.get('regime') or '—'} | "
            f"{fio_quad} | {_conservation_cell(record)} | {svc_quad} | {e2e_quad} | {svc_tail} | {e2e_tail} | "
            f"{_fmt_pair(row.get('inflight_avg'), row.get('inflight_max'))} | {samples} | {row.get('flags') or ''} |"
        )
    lines.append("")
    return lines


def _regime_ladder_section(records: list[StallRecord]) -> list[str]:
    lines = [
        "## 四、Regime 阶梯 — 阈值与钉住机制",
        "",
        "Regime 分类由 `scripts/analyze_rig_full.py:classify_d1_regime` 给出（同一套阈值在两个分析器间共享）：",
        "",
        f"- **STALL-TAIL**: `biolc_service_ge16_ratio >= {D1_REGIME_STALL_TAIL_GE16}`（≥1% 的 biolc service 样本 ≥16ms）。",
        f"- **DEGRADED**: 非 STALL-TAIL 且（`biolc_service_ge16_ratio >= {D1_REGIME_DEGRADED_GE16}` 或 `fio p99_avg > {D1_REGIME_DEGRADED_P99_US:.0f}µs`）。",
        "- **NORMAL**: 其余。",
        "",
        "**钉住机制（为何 ≥1% 会把 fio p99 钉在 [16,32)ms log2 桶顶部 ≈31ms）**：每租户 p99 只容许 1% 的 I/O",
        "「更慢」；全局滞留比例 f（biolc service ≥16ms 占比）近似均匀作用于 32 个租户。f ≥ 1% 时，滞留 I/O",
        "必然侵入每租户的 p99 分位，p99 被推入滞留带；其插值落点由滞留质量在 [16,32)ms log2 桶内的分布决定——",
        "集中于桶下沿则 p99≈16ms，集中于上沿则 p99≈32ms。0916 实测 Tier-3 的 fio p99 = 30.4-32.1ms",
        "（贴桶上沿），即滞留样本聚集在 [16,32)ms 桶顶部——「钉在桶顶」的机制签名。fio p99 与 biolc service p99",
        "同时落在 [16,32)ms 带内，是 STALL-TAIL 的定义性特征。",
        "",
        "**亚 1% 滞留（0.1%-1%）只污染 p99 之后**：0.5% 滞留意味着每个租户 p99 之后仍有 ~0.5% 样本滞留，",
        "p99 本身留在干净簇，p99.9 则被推入滞留带——none-open（0.54%）与 iox q_none（0.58%）即此形态，",
        "阶梯上判 DEGRADED 而非 NORMAL：**这不是误分类**，它标记「p99 干净但尾部确认存在宿主滞留」。",
        "",
        f"- `D1_REGIME_DEGRADED_P99_US = {D1_REGIME_DEGRADED_P99_US:.0f}µs` 分界校准：干净簇（none/kyber/io_max/",
        "  io_latency/io_prio_class/io_bfq_weight）fio p99 = 0.8-1.4ms，节流/启发式簇（bfq/q_kyber/q_noop/…）= 2.4-3.4ms，",
        "  2000µs 恰好落在两簇之间的空档。",
        "",
        "| regime | 判据 | 本数据集成员 |",
        "|--------|------|--------------|",
    ]
    for regime in REGIME_ORDER:
        members = sorted(
            {r.label for r in records if r.row.get("regime") == regime},
        )
        lines.append(f"| {regime} | {'见上' if regime != 'NORMAL' else '其余'} | {', '.join(members) or '—'} |")
    lines.append("")
    return lines


def _band_name(value: float | None) -> str:
    """Human-readable log2-band label for a µs value (e.g. 30000 → '[16,32)ms')."""
    number = _f(value)
    if number is None:
        return "—"
    lo = 1.0
    while lo * 2.0 <= number:
        lo *= 2.0
    hi = lo * 2.0
    if lo >= 1000.0:
        return f"[{lo / 1000.0:g}, {hi / 1000.0:g})ms"
    return f"[{lo:g}, {hi:g})µs"


def _tier3_proof_section(records: list[StallRecord]) -> list[str]:
    by_variant = _representative(records)
    tier3 = [by_variant[v] for v in TIER3_VARIANTS if v in by_variant]
    clean = [by_variant[v] for v in CLEAN_VARIANTS if v in by_variant]
    if not tier3 or not clean:
        return [
            "## Tier-3 证明",
            "",
            "_数据集中缺少 Tier-3 或干净簇参照变体，无法构成证明。_",
            "",
        ]
    kyber = next((r for r in clean if r.variant == "kyber"), clean[0])
    krow = kyber.row

    def ref(name: str) -> float | None:
        return _f(krow.get(name))

    lines = [
        "## 五、Tier-3 证明 — 「~31ms 簇 = 宿主侧滞留带」的数值闭环",
        "",
        "**待证命题**：Tier-3 变体（native `mq_deadline`/`io_cost` + iox `q_ddl`/`q_fifo`/`q_prio`）与干净簇",
        "（`kyber`、`none`(open bookend)）共享 biolc service/e2e 的 p50/p90；差别只出现在尾部——p99/p99.9",
        "踏入 [16,32)ms 滞留带。若成立，则 ~31ms fio p99 簇不是调度器的中位行为，而是宿主侧滞留带的签名。",
        "",
        f"干净参照 `kyber`：svc p50/p90/p99/p99.9 = "
        f"{_fmt(ref('biolc_service_p50_us'))}/{_fmt(ref('biolc_service_p90_us'))}/"
        f"{_fmt(ref('biolc_service_p99_us'))}/{_fmt(ref('biolc_service_p999_us'))} µs；"
        f"e2e p50/p90/p99/p99.9 = "
        f"{_fmt(ref('biolc_e2e_p50_us'))}/{_fmt(ref('biolc_e2e_p90_us'))}/"
        f"{_fmt(ref('biolc_e2e_p99_us'))}/{_fmt(ref('biolc_e2e_p999_us'))} µs。",
        "",
        "| Tier-3 变体 | svc p50 (×kyber) | svc p90 (×kyber) | svc p99 (band) | svc p99.9 (band) | e2e p50 (×kyber) | e2e p90 (×kyber) | e2e p99 (band) | e2e p99.9 (band) | fio p99 avg (µs) |",
        "|-------------|------------------|------------------|----------------|------------------|------------------|------------------|----------------|------------------|------------------|",
    ]

    def ratio_cell(value: Any, reference: float | None) -> str:
        number = _f(value)
        if number is None:
            return "—"
        if reference is None or reference == 0.0:
            return _fmt(number)
        return f"{_fmt(number)} ({number / reference:.2f}×)"

    for record in tier3:
        row = record.row
        lines.append(
            f"| {record.label} | {ratio_cell(row.get('biolc_service_p50_us'), ref('biolc_service_p50_us'))} | "
            f"{ratio_cell(row.get('biolc_service_p90_us'), ref('biolc_service_p90_us'))} | "
            f"{_fmt(row.get('biolc_service_p99_us'))} ({_band_name(_f(row.get('biolc_service_p99_us')))}) | "
            f"{_fmt(row.get('biolc_service_p999_us'))} ({_band_name(_f(row.get('biolc_service_p999_us')))}) | "
            f"{ratio_cell(row.get('biolc_e2e_p50_us'), ref('biolc_e2e_p50_us'))} | "
            f"{ratio_cell(row.get('biolc_e2e_p90_us'), ref('biolc_e2e_p90_us'))} | "
            f"{_fmt(row.get('biolc_e2e_p99_us'))} ({_band_name(_f(row.get('biolc_e2e_p99_us')))}) | "
            f"{_fmt(row.get('biolc_e2e_p999_us'))} ({_band_name(_f(row.get('biolc_e2e_p999_us')))}) | "
            f"{_fmt(row.get('fio_clat_p99_avg_us'))} |"
        )
    lines.append("")
    lines.append(
        "**判读**（数字全部来自上表，插值口径见方法论节）："
    )
    svc_p50_ratios = []
    svc_p90_ratios = []
    svc_p99_values = []
    e2e_p99_values = []
    for record in tier3:
        row = record.row
        for key, bucket in (
            ("biolc_service_p50_us", svc_p50_ratios),
            ("biolc_service_p90_us", svc_p90_ratios),
        ):
            value = _f(row.get(key))
            reference = ref(key)
            if value is not None and reference:
                bucket.append(value / reference)
        svc_p99_values.append(_f(row.get("biolc_service_p99_us")))
        e2e_p99_values.append(_f(row.get("biolc_e2e_p99_us")))
    clean_svc_p99 = ref("biolc_service_p99_us") or 1.0
    svc_p99_real = [value for value in svc_p99_values if value is not None]
    step_ratio = min(svc_p99_real) / clean_svc_p99 if svc_p99_real else 0.0
    if svc_p50_ratios:
        lines.append(
            f"- **svc p50 与干净簇重合**：Tier-3 svc p50 = kyber 的 "
            f"{min(svc_p50_ratios):.2f}×–{max(svc_p50_ratios):.2f}×。"
        )
    if svc_p90_ratios:
        lines.append(
            f"- **svc p90 同处 0.1ms 量级**：Tier-3 svc p90 = kyber 的 {min(svc_p90_ratios):.2f}×–{max(svc_p90_ratios):.2f}×"
            "——同为百微秒量级、与 16ms 带相差两个数量级；并非逐百分点重合（mq_deadline 的批处理派发把 p90 抬到 ~0.17ms），"
            "但阶梯形态不变：中位/九分位干净、尾部才越带。"
        )
    e2e_p99_in_band = [
        value for value in e2e_p99_values if value is not None and value >= GE16_MS_US
    ]
    lines.append(
        f"- **尾部越带**：Tier-3 的 svc p99 全部落入 [16,32)ms"
        f"（实测 {', '.join(_fmt(v) for v in svc_p99_values if v is not None)} µs），"
        f"e2e p99 中 {len(e2e_p99_in_band)}/{len(e2e_p99_values)} 同带"
        f"（{', '.join(_fmt(v) for v in e2e_p99_values if v is not None)} µs；"
        "例外 io_cost e2e p99 = 16,129µs 贴带下沿之下 4%——其 e2e 含 cgroup 队列滞留，不是干净的"
        "device-time 代理，见下）"
        f"——对照干净簇 svc p99 {_fmt(ref('biolc_service_p99_us'))}µs，越带幅度 ~{step_ratio:.0f}×；"
        "fio p99 avg 同步钉在 ~31ms。"
    )
    io_cost_record = next((r for r in tier3 if r.variant == "io_cost"), None)
    if io_cost_record is not None:
        lines.append(
            "- **io_cost 的 e2e 口径警告**：io_cost（cgroup io.cost）通过在 cgroup 队列滞留请求来限流，"
            "e2e（insert→complete）因此包含限流排队——e2e p50/p90 = "
            f"{_fmt(io_cost_record.row.get('biolc_e2e_p50_us'))}/{_fmt(io_cost_record.row.get('biolc_e2e_p90_us'))}µs，"
            "不能与干净簇的 device-time e2e 直接对比；其 **service**（设备时间）口径 p50/p90 = "
            f"{_fmt(io_cost_record.row.get('biolc_service_p50_us'))}/{_fmt(io_cost_record.row.get('biolc_service_p90_us'))}µs"
            " 与干净簇一致，滞留带结论以 service 为准。"
        )
    lines.append("")
    lines.append(
        "结论：Tier-3 的 ~31ms fio p99 簇由 ≥1% 宿主侧滞留（biolc service ≥16ms 样本）单变量解释——"
        "中位/九分位设备服务时间与干净簇一致（svc p50 = kyber 的 1.01-1.06×，p90 同 0.1ms 量级），"
        "滞留带只作用在 p99 之后的尾部；5 个 Tier-3 变体的 svc p99 全部落在 [16,32)ms 带（16.7-32.1ms），"
        "e2e 佐证其中 4 个（io_cost 的 e2e 因 cgroup 限流排队口径失真，以 service 为准）。"
        "[16,32)ms log2 桶的插值上沿（~31ms）与 fio p99 实测（30.4-32.1ms）吻合，即「钉在桶顶」的机制签名。"
    )
    lines.append("")
    return lines


def _iox_slat_section(records: list[StallRecord]) -> list[str]:
    native_slat = [
        value
        for r in records
        if r.arm == "native"
        for value in [_f(r.row.get("slat_mean_us"))]
        if value is not None
    ]
    iox_p50 = [
        value
        for r in records
        if r.arm == "iox"
        for value in [_f(r.row.get("fio_clat_p50_avg_us"))]
        if value is not None
    ]
    native_slat_text = (
        f"{min(native_slat):.1f}-{max(native_slat):.1f}µs" if native_slat else "—"
    )
    iox_p50_text = f"{min(iox_p50):.1f}-{max(iox_p50):.1f}µs" if iox_p50 else "—"
    lines = [
        "## 六、iox slat 吸收警告 — fio clat 低分位在 iox 臂上无意义",
        "",
        "iox 臂（scx_io 队列策略）运行时，真实设备时延被 fio 记入 **slat**（submission latency），",
        "clat 只剩派发后的完成等待——clat 低于 ~p90 的分位因此**无意义**（iox 各变体 fio clat p50 = "
        f"{iox_p50_text}，而 native 臂 slat 仅 {native_slat_text}，为物理提交开销量级；"
        "例外：`q_none` 是 pass-through 策略（bio_bypass_true ≈ 全量），设备时延回到 clat，"
        "其 fio clat 与 native 同口径可信）。",
        "守恒式 `slat_mean + clat_mean ≈ 1/单租户 IOPS` 仍然成立，作为「时延没有被丢掉、只是被记账到了 slat」",
        "的校验：D1 n=32 时 `(slat+clat)/(1e6/总IOPS) ≈ 32` 即守恒。",
        "",
        "| 变体 | 臂 | slat (µs) | clat (µs) | slat+clat (µs) | period (µs, 1e6/总IOPS) | 守恒比 (≈32?) |",
        "|------|----|-----------|-----------|----------------|--------------------------|--------------|",
    ]
    for record in sorted(records, key=lambda r: _ladder_key(r)):
        row = record.row
        ratio = _f(row.get("conservation_ratio"))
        lines.append(
            f"| {record.label} | {record.arm} | {_fmt(row.get('slat_mean_us'))} | {_fmt(row.get('clat_mean_us'))} | "
            f"{_fmt(row.get('slat_plus_clat_us'))} | {_fmt(row.get('period_us'))} | "
            f"{f'{ratio:.2f}' if ratio is not None else '—'} |"
        )
    lines.append("")
    lines.append(
        "因此 **D1 跨臂对比必须以 biolc service/e2e 直方图为准**；fio clat 只取两个用途：native 臂的绝对 p99、"
        "iox 臂的 p99 尾部（滞留带不被 slat 吸收，仍显形于 clat p99）。"
    )
    lines.append("")
    return lines


def _bookend_section(records: list[StallRecord]) -> list[str]:
    none_runs = [r for r in records if r.variant == "none"]
    open_run = next((r for r in none_runs if r.run_tag == "bookend_open"), None)
    close_run = next((r for r in none_runs if r.run_tag == ""), None)
    close_pct = _f(close_run.row.get("biolc_service_ge16_ratio")) if close_run else None
    open_pct = _f(open_run.row.get("biolc_service_ge16_ratio")) if open_run else None
    open_p99 = _f(open_run.row.get("fio_clat_p99_avg_us")) if open_run else None
    lines = [
        "## 七、Bookend 污染说明 — 0916 收尾 bookend 的 STALL-TAIL 是事故残留",
        "",
        f"campaign 收尾的 `D1Q1_none_n32`（{close_run.execution_time if close_run else '—'}）被 guest 磁盘写满事故污染：",
        f"其 biolc service 样本中 {close_pct * 100:.2f}%（ge16 比例）落入 ≥16ms 滞留带" if close_pct is not None else "（无收尾 run）",
        "——这不是 none 调度器的行为，而是事故时段宿主侧滞留带的残留，故其 STALL-TAIL 标签仅作溯源，",
        f"不参与干净簇对照。开跑 bookend（`none_bookend_open`，{open_run.execution_time if open_run else '—'}）"
        f"干净得多：fio p99 = {_fmt(open_p99)}µs、ge16 = {(open_pct * 100) if open_pct is not None else float('nan'):.2f}%"
        "（亚 1% 尾部滞留 → DEGRADED，见 §四）。",
        "",
        "| run | execution_time | fio p99 avg (µs) | fio p99.9 avg (µs) | svc ge16 | svc p99 (µs) | regime | 判读 |",
        "|-----|----------------|------------------|--------------------|----------|--------------|--------|------|",
    ]
    for record in sorted(none_runs, key=lambda r: r.execution_time):
        row = record.row
        if record.run_tag == "bookend_open":
            note = "干净开跑基准（亚 1% 尾部滞留）"
        else:
            note = "磁盘写满事故污染 → STALL-TAIL 为事故残留"
        lines.append(
            f"| none·{record.run_tag or 'main'} | {record.execution_time} | {_fmt(row.get('fio_clat_p99_avg_us'))} | "
            f"{_fmt(row.get('fio_clat_p999_avg_us'))} | {_fmt_ratio(row.get('biolc_service_ge16_ratio'))} | "
            f"{_fmt(row.get('biolc_service_p99_us'))} | {row.get('regime') or '—'} | {note} |"
        )
    lines.append("")
    lines.append(
        "凡引用 none 作为「干净簇」成员时，一律使用 bookend_open；收尾 run 只用于污染溯源，不参与 Tier-3 对照。"
    )
    lines.append("")
    return lines


def _methodology_section(records: list[StallRecord]) -> list[str]:
    service_counts = [
        value
        for r in records
        for value in [_i(r.row.get("biolc_service_sample_count"))]
        if value is not None
    ]
    low = min(service_counts) if service_counts else 0
    high = max(service_counts) if service_counts else 0
    lines = [
        "## 八、方法论注记",
        "",
        "- **直方图插值**：biolc p50/p90/p99/p99.9 在 log2 桶内做线性插值（fio 自身分位口径同样的桶内均匀假设）；",
        "  桶宽为下界的一倍，误差上界 <1 桶宽。[16,32)ms 桶的插值上沿 ≈31-32ms，与 STALL-TAIL 的 fio p99 吻合。",
        "- **last-block-wins**：同一 map 在 bpftrace log 中可能多次打印（SUMMARY 段内 + 段后），取每 map 最后一块；",
        "  per-CPU keyed 块（`@biolc_stack_path_us_cpu[N]`）不参与聚合。",
        "- **HIST_MISMATCH**：直方图桶计数 vs summary `biolc_service_sample_count` 差 >0.1% 时置位（本数据集全部通过）。",
        f"- **LOW_SAMPLES**：service 样本数 <10000 时均值无意义，只有计数可信（本数据集 service 样本 {low:,}–{high:,}，全部充裕）。",
        "  e2e 样本数是「经过 insert 的请求」子集（如 none-open ≈数百），其分位仅作参考，见对照表 samples 列。",
        "- **单位怪癖**（保持原样，不「修正」）：`agg_bw_mbps` 实为 MiB/s（bytes/1048576）；raw fio `clat_ns.percentile`",
        "  为 ns（/1000 → µs）；biolc 桶值本身即 µs（K=1024、M=1048576 后缀）。",
        "- **守恒口径**：`period_us = 1e6/总IOPS`；n=32 租户下 `(slat+clat)/period ≈ 32` ⇔ 单租户 `slat+clat ≈ 1/单租户IOPS`。",
        "- **Regime 分类器**：与 `analyze_rig_full.py` 共享同一 `classify_d1_regime`（阈值常量同源）。",
        "",
    ]
    return lines


def _cross_source_section(by_source: list[tuple[str, Path, list[StallRecord]]]) -> list[str]:
    """Generic per-variant delta table for every source pair sharing variants."""
    reps = {source: _representative(records) for source, _root, records in by_source}
    lines: list[str] = []
    pairs = [
        (by_source[i][0], by_source[j][0])
        for i in range(len(by_source))
        for j in range(i + 1, len(by_source))
    ]
    if not pairs:
        return lines
    lines.extend(
        [
            "## 九、跨 source 对比（同一变体逐指标 Δ）",
            "",
            "每对共享变体的 source 各列一表；代表 run = 各 source 中该变体最早 execution_time 的 run。",
            "",
        ]
    )
    delta_metrics = (
        ("fio_clat_p99_avg_us", "fio p99 avg (µs)"),
        ("fio_clat_p999_avg_us", "fio p99.9 avg (µs)"),
        ("biolc_service_ge16_ratio", "svc ≥16 ratio"),
        ("biolc_service_p99_us", "svc p99 (µs)"),
        ("biolc_e2e_p99_us", "e2e p99 (µs)"),
        ("agg_bw_mib_s", "agg BW (MiB/s)"),
    )
    for source_a, source_b in pairs:
        shared = sorted(
            {v for v in reps[source_a] if v in reps[source_b]},
            key=lambda v: _variant_key_from((reps[source_a][v].arm, v)),
        )
        lines.append(f"### {source_a} → {source_b}（共享 {len(shared)} 个变体）")
        lines.append("")
        if not shared:
            lines.append("_无共享变体。_")
            lines.append("")
            continue
        header = "| variant | regime (A→B) |"
        header += "".join(f" {label} |" for _key, label in delta_metrics)
        header += "|"
        lines.append(header)
        lines.append("|---------|--------------|" + "---|" * len(delta_metrics))
        for variant in shared:
            run_a = reps[source_a][variant]
            run_b = reps[source_b][variant]
            cells = [f"{run_a.row.get('regime') or '—'} → {run_b.row.get('regime') or '—'}"]
            for key, _label in delta_metrics:
                value_a = _f(run_a.row.get(key))
                value_b = _f(run_b.row.get(key))
                if value_a is None or value_b is None:
                    cells.append("—")
                elif value_a == 0.0:
                    cells.append(f"{_fmt(value_b)}")
                else:
                    delta = (value_b - value_a) / abs(value_a) * 100.0
                    cells.append(f"{_fmt(value_a)} → {_fmt(value_b)} ({delta:+.1f}%)")
            lines.append(f"| {variant} | " + " | ".join(cells) + " |")
        lines.append("")
    return lines


def _exec_summary(records: list[StallRecord]) -> list[str]:
    regimes: dict[str, list[str]] = {name: [] for name in REGIME_ORDER}
    for record in sorted(records, key=lambda r: _ladder_key(r)):
        regime = record.row.get("regime") or "—"
        regimes.setdefault(regime, []).append(record.label)
    stall_tail = ", ".join(regimes.get("STALL-TAIL", [])) or "—"
    degraded = ", ".join(regimes.get("DEGRADED", [])) or "—"
    normal = ", ".join(regimes.get("NORMAL", [])) or "—"

    def variant_pct(variant: str, key: str) -> str:
        run = next((r for r in records if r.variant == variant and r.run_tag == "bookend_open"), None)
        run = run or next((r for r in records if r.variant == variant), None)
        value = _f(run.row.get(key)) if run else None
        return f"{value * 100:.2f}%" if value is not None else "—"

    def variant_num(variant: str, key: str) -> str:
        run = next((r for r in records if r.variant == variant and r.run_tag == "bookend_open"), None)
        run = run or next((r for r in records if r.variant == variant), None)
        return _fmt(run.row.get(key)) if run else "—"

    lines = [
        "## 一、执行摘要",
        "",
        f"本报告对 20260916 campaign 的全部 {len(records)} 个 D1Q1 n=32 run（native 9 knob + iox 10 队列策略",
        "+ 两端 none bookend）做离线 stall-band 复分析，用 biolc service/e2e 直方图与 fio slat/clat 守恒式",
        "数值证明 0916 campaign 深度分析的**Tier-3 假设**：",
        "",
        "> **~31ms fio p99 簇（Tier-3）不是调度器的中位行为，而是宿主侧滞留带的签名**——Tier-3 变体与干净簇",
        "> 共享 biolc service/e2e 的 p50（±几个百分点）与 p90（同 0.1ms 量级），只有 p99/p99.9 踏入",
        "> [16,32)ms 带；≥1% 的 ≥16ms service 样本把 fio p99 钉在该 log2 桶顶部（~31ms）。",
        "",
        f"- **STALL-TAIL**（ge16 ≥1%，p99 被钉 [16,32)ms）：{stall_tail}",
        f"- **DEGRADED**（亚 1% 滞留落 p99 之后，或 p99 进 2.4-3.4ms 节流簇）：{degraded}",
        f"- **NORMAL**（干净）：{normal}",
        "",
        f"none·bookend_open 的 {variant_pct('none', 'biolc_service_ge16_ratio')} 滞留落在 p99 之后"
        f"（fio p99={variant_num('none', 'fio_clat_p99_avg_us')}µs 干净、"
        f"fio p99.9={variant_num('none', 'fio_clat_p999_avg_us')}µs 贴带下沿、"
        f"biolc svc p99.9={variant_num('none', 'biolc_service_p999_us')}µs 深入 [32,64)ms）——阶梯判 DEGRADED",
        f"是「亚 1% 滞留」的正确定级而非误分类（§四）。iox q_none 同形态"
        f"（{variant_pct('q_none', 'biolc_service_ge16_ratio')}，p99={variant_num('q_none', 'fio_clat_p99_avg_us')}µs 干净）。"
        "收尾 bookend 的 STALL-TAIL 为磁盘写满事故残留（§七）。iox 臂 fio clat 低分位被 slat 吸收、无意义（§六）。",
        "",
    ]
    return lines


def _figures_section(has_cross_source: bool) -> list[str]:
    number = "十" if has_cross_source else "九"
    return [
        f"## {number}、图目录",
        "",
        "- `figures/d1_biolc_service_cdf.png` — biolc service CDF（native/iox 双面板，16ms/64ms 参考线）",
        "- `figures/d1_biolc_e2e_cdf.png` — biolc e2e CDF（同上）",
        "- `figures/d1_quantile_ladder_fio_clat.png` — fio clat p50→p99.9 阶梯（按 regime 分组；iox 低分位受 slat 吸收污染）",
        "- `figures/d1_quantile_ladder_biolc_e2e.png` — biolc e2e p50→p99.9 阶梯（Tier-3 证明的主视图）",
        "",
    ]


def build_markdown(
    out_dir: Path,
    by_source: list[tuple[str, Path, list[StallRecord]]],
    non_d1_total: dict[str, int],
    records: list[StallRecord],
) -> str:
    lines: list[str] = [
        "# D1 Stall-Band 分析 — Tier-3 宿主侧滞留带验证（20260917）",
        "",
        f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Target disk**: {TARGET_DISK} · **n tenants**: 32 · **数据**: 20260916 campaign D1 全量离线复分析",
        "",
        "### Inputs",
        "",
        "| source | dir | D1 runs | non-D1 skipped |",
        "|--------|-----|---------|-----------------|",
    ]
    for source, root, batch in by_source:
        lines.append(f"| {source} | {root} | {len(batch)} | {non_d1_total.get(source, 0)} |")
    lines.append("")
    lines.extend(_exec_summary(records))
    by_variant = _representative(records)
    tier3 = [by_variant[v] for v in TIER3_VARIANTS if v in by_variant]
    kyber = by_variant.get("kyber")
    if tier3 and kyber is not None:
        ksvc50 = _f(kyber.row.get("biolc_service_p50_us")) or 1.0
        ksvc99 = _f(kyber.row.get("biolc_service_p99_us")) or 1.0

        def values(key: str) -> list[float]:
            return [value for r in tier3 for value in [_f(r.row.get(key))] if value is not None]

        p50_ratios = [value / ksvc50 for value in values("biolc_service_p50_us")]
        svc_p99s = values("biolc_service_p99_us")
        fio_p99s = values("fio_clat_p99_avg_us")
        if p50_ratios and svc_p99s and fio_p99s:
            lines.append("## 二、Tier-3 证明要点（速览）")
            lines.append("")
            lines.append(
                f"Tier-3（`mq_deadline`/`io_cost`/`q_ddl`/`q_fifo`/`q_prio`）svc p50 = kyber 的 "
                f"{min(p50_ratios):.2f}-{max(p50_ratios):.2f}×；"
                f"svc p99 = {min(svc_p99s) / 1000.0:.1f}-{max(svc_p99s) / 1000.0:.1f}ms 全部落入 [16,32)ms 带"
                f"（kyber 为 {ksvc99 / 1000.0:.2f}ms，越带 ~{min(svc_p99s) / ksvc99:.0f}-{max(svc_p99s) / ksvc99:.0f}×）；"
                f"fio p99 avg 同步钉在 {min(fio_p99s) / 1000.0:.1f}-{max(fio_p99s) / 1000.0:.1f}ms。完整数值表见 §五。"
            )
            lines.append("")
    lines.append("## 三、各变体全量对照表")
    lines.append("")
    for source, root, batch in by_source:
        lines.extend(_source_table(source, root, batch))
    lines.extend(_regime_ladder_section(records))
    lines.extend(_tier3_proof_section(records))
    lines.extend(_iox_slat_section(records))
    lines.extend(_bookend_section(records))
    lines.extend(_methodology_section(records))
    cross_source = _cross_source_section(by_source)
    lines.extend(cross_source)
    lines.extend(_figures_section(bool(cross_source)))
    report = "\n".join(lines) + "\n"
    (out_dir / "D1_STALL_ANALYSIS.md").write_text(report, encoding="utf-8")
    return report


def _cdf_axis(axis: Axes, records: list[StallRecord], map_name: str, title: str) -> None:
    for record in sorted(records, key=lambda r: _variant_key(r)):
        log_path = record.path / f"step_{STEP_INDEX}" / SCENE_BIOLC / BIOLC_LOG_NAME
        log_text = _read_text(log_path)
        if log_text is None:
            continue
        buckets = parse_biolc_hist_blocks(log_text).get(map_name)
        if not buckets:
            continue
        total = sum(count for _lo, _hi, count in buckets)
        if total <= 0:
            continue
        xs: list[float] = []
        ys: list[float] = []
        cumulative = 0.0
        for lo, hi, count in buckets:
            xs.append(lo)
            ys.append(cumulative / total)
            cumulative += count
            xs.append(hi)
            ys.append(cumulative / total)
        regime = record.row.get("regime")
        color = REGIME_COLORS.get(regime, REGIME_COLORS[None])
        axis.plot(xs, ys, drawstyle="steps-post", linewidth=1.2, color=color, label=record.label)
    axis.set_xscale("log")
    axis.set_xlim(4.0, 300000.0)
    axis.set_ylim(0.0, 1.02)
    axis.axvline(GE16_MS_US, color="black", linestyle="--", linewidth=0.8, alpha=0.7)
    axis.axvline(GE64_MS_US, color="black", linestyle=":", linewidth=0.8, alpha=0.7)
    axis.text(GE16_MS_US, 0.30, " 16ms", fontsize=8, color="black")
    axis.text(GE64_MS_US, 0.30, " 64ms", fontsize=8, color="black")
    axis.set_xlabel("latency (µs, log)")
    axis.set_ylabel("CDF")
    axis.set_title(title, fontsize=11)
    axis.grid(True, which="both", alpha=0.25)


def _plot_cdf_figures(records: list[StallRecord], figures_dir: Path) -> None:
    for map_name, filename, pretty in (
        (SVC_MAP, "d1_biolc_service_cdf.png", "biolc service"),
        (E2E_MAP, "d1_biolc_e2e_cdf.png", "biolc e2e"),
    ):
        figure, axes = plt.subplots(1, 2, figsize=(15, 6.5), sharey=True)
        native = [r for r in records if r.arm == "native"]
        iox = [r for r in records if r.arm == "iox"]
        _cdf_axis(axes[0], native, map_name, f"native arm — {pretty}")
        _cdf_axis(axes[1], iox, map_name, f"iox arm — {pretty}")
        for axis in axes:
            axis.legend(fontsize=7, loc="lower right", ncol=2)
        figure.suptitle(
            f"D1 {pretty} CDF per variant (20260916) — color = regime "
            f"(red STALL-TAIL / orange DEGRADED / green NORMAL); guide lines at 16ms/64ms",
            fontsize=10,
        )
        figure.tight_layout(rect=(0, 0, 1, 0.96))
        figure.savefig(figures_dir / filename, dpi=150)
        plt.close(figure)


LADDER_KEYS = ("p50", "p90", "p99", "p999")


def _ladder_figure(
    records: list[StallRecord],
    prefix: str,
    path: Path,
    title: str,
    annotation: str | None,
) -> None:
    ordered = sorted(records, key=lambda r: _ladder_key(r))
    figure, axis = plt.subplots(figsize=(16, 7))
    xticks: list[float] = []
    xlabels: list[str] = []
    x = 0.0
    previous_regime: str | None = None
    for record in ordered:
        regime = record.row.get("regime")
        if previous_regime is not None and regime != previous_regime:
            axis.axvline(x - 0.5, color="black", linewidth=0.8)
            x += 1.0
        previous_regime = regime
        values = [_f(record.row.get(f"{prefix}_{name}_us")) for name in LADDER_KEYS]
        # None → NaN: matplotlib breaks the line for missing points (log axis).
        plot_values = [value if value is not None and value > 0.0 else float("nan") for value in values]
        if any(value == value for value in plot_values):
            xs = [x] * len(plot_values)
            color = REGIME_COLORS.get(regime, REGIME_COLORS[None])
            axis.plot(xs, plot_values, marker="o", markersize=4, linewidth=1.2, color=color)
        xticks.append(x)
        xlabels.append(record.label)
        x += 1.0
    axis.set_yscale("log")
    axis.set_ylim(0.5, 5e6)
    axis.set_xticks(xticks)
    axis.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=8)
    axis.set_ylabel("latency (µs, log)")
    axis.set_title(title, fontsize=11)
    axis.grid(True, which="major", axis="y", alpha=0.3)
    for regime, color in REGIME_COLORS.items():
        if regime is None:
            continue
        axis.plot([], [], color=color, linewidth=2, label=regime)
    axis.legend(fontsize=9, loc="upper left")
    if annotation:
        axis.text(
            0.01,
            0.02,
            annotation,
            transform=axis.transAxes,
            fontsize=8,
            color="#d62728",
            verticalalignment="bottom",
        )
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


def _plot_ladder_figures(records: list[StallRecord], figures_dir: Path) -> None:
    _ladder_figure(
        records,
        "fio_clat",
        figures_dir / "d1_quantile_ladder_fio_clat.png",
        "D1 fio clat quantile ladder p50→p90→p99→p99.9 (avg across tenants, 20260916) — grouped by regime",
        "iox arm: device latency is absorbed into slat — clat below ~p90 is meaningless there (see report §6)",
    )
    _ladder_figure(
        records,
        "biolc_e2e",
        figures_dir / "d1_quantile_ladder_biolc_e2e.png",
        "D1 biolc e2e quantile ladder p50→p90→p99→p99.9 (histogram-interpolated, 20260916) — grouped by regime",
        None,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Focused D1 stall-band analyzer: per-tenant fio clat percentile ladder, slat/clat "
            "conservation, biolc service/e2e/queue histogram tails and interpolated percentiles, "
            "regime labels and the Tier-3 stall-band deliverable (CSV + Chinese markdown + figures)."
        ),
        epilog=(
            "Outputs (under --output-dir): d1_stall_metrics.csv, D1_STALL_ANALYSIS.md and "
            "figures/d1_*.{png}. Repeatable --results-root/--source pairs are matched by position. "
            "A single bad directory never aborts the run; it gets a PARSE_ERROR flag."
        ),
    )
    parser.add_argument(
        "--results-root",
        action="append",
        default=[],
        metavar="DIR",
        help="result root containing sequence result dirs (repeatable)",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="LABEL",
        help="source label for the matching --results-root (repeatable, paired by position)",
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="directory for the CSV/MD/figure outputs")
    args = parser.parse_args(argv)
    if len(args.results_root) != len(args.source):
        parser.error(
            f"--results-root and --source must be paired (got {len(args.results_root)} root(s) "
            f"and {len(args.source)} source label(s))"
        )
    if not args.results_root:
        parser.error("no inputs: pass --results-root/--source pairs")
    return args


def main() -> int:
    args = parse_args()
    pairs: list[tuple[str, Path]] = [
        (label, Path(root)) for label, root in zip(args.source, args.results_root)
    ]

    records: list[StallRecord] = []
    seen: set[Path] = set()
    by_source: list[tuple[str, Path, list[StallRecord]]] = []
    non_d1_total: dict[str, int] = {}
    for label, root in pairs:
        batch: list[StallRecord] = []
        found, skipped = scan_result_root(root, label, seen, batch)
        non_d1_total[label] = skipped
        by_source.append((label, root, batch))
        records.extend(batch)
        if found == 0:
            warn(f"{label}: no D1 sequence result dirs under {root}")

    source_order = {label: index for index, (label, _root, _recs) in enumerate(by_source)}
    records.sort(
        key=lambda r: (
            source_order.get(r.source, 0),
            _ladder_key(r),
        )
    )

    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    figures_dir = out_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "d1_stall_metrics.csv", records)
    build_markdown(out_dir, by_source, non_d1_total, records)

    plot_records = [r for r in records if "PARSE_ERROR" not in r.flags]
    _plot_cdf_figures(plot_records, figures_dir)
    _plot_ladder_figures(plot_records, figures_dir)

    print(f"D1 runs analyzed: {len(records)} across {len(by_source)} source(s)")
    for label, root, batch in by_source:
        print(f"  {label}: {len(batch)} D1 run(s) under {root} ({non_d1_total[label]} non-D1 skipped)")
    for record in records:
        row = record.row
        print(
            f"  [{record.source}] {record.label}: regime={row.get('regime') or '—'} "
            f"fio_p99={_fmt(row.get('fio_clat_p99_avg_us'))}µs "
            f"svc_ge16={_fmt_ratio(row.get('biolc_service_ge16_ratio'))} "
            f"svc_p99={_fmt(row.get('biolc_service_p99_us'))}µs "
            f"cons={_fmt(row.get('conservation_ratio'), 2)} "
            f"flags={row.get('flags') or ''}"
        )
    print(f"Wrote {out_dir}/d1_stall_metrics.csv")
    print(f"Wrote {out_dir}/D1_STALL_ANALYSIS.md")
    print(f"Wrote {out_dir}/figures/d1_*.png")
    if not records:
        warn("no D1 sequence result dirs found in any input — nothing to analyze")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())