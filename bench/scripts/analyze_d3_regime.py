#!/usr/bin/env python3
"""D3 regime-metrics extractor for the rig track (supplementary to analyze_rig_full.py).

Extracts one row per D3 sequence result directory (``D3LC_rread_4k_<knob>`` native arm,
``D3LC_rread_4k_iox_<policy>[_hetero]`` iox arm) from one or more result roots and
emits:

* ``d3_regime_metrics.csv`` — one row per sequence run, all sources pooled.
* ``d3_regime_metrics.md``  — one table block per source (variant ordering matches the
  D3 table in FULL_BASELINE_SUMMARY.md: native knobs first, then iox variants
  alphabetically) plus a grand-summary section that lists the three known bad
  arbiters' attractor values for cross-checking.

The metrics capture the "regime" of each run: LC tenant completion-latency
percentiles (p50/p90/p99/p99.9 from the RAW fio jobs JSON), BE per-tenant bandwidth
and fairness, sdb in-flight depth, the ``@biolc_service_us`` bpftrace service-time
histogram tail ratios (>=16ms / >=64ms) and the scx_io bypass-fallback counters
(``sbf_*``, iox arm only).

Data conventions (known unit quirks, do not "fix"):
* ``fio_metrics_from_data``'s ``agg_bw_mbps`` is actually MiB/s (bytes/1048576);
  BE aggregate BW in GiB/s = sum(per-tenant MiB/s) / 1024.
* fio clat percentile dicts are in µs; keys arrive as strings like "99.000000" and
  are normalized to %g form ("50", "90", "99", "99.9").
* Only the ``*_jobs1_fio.json`` files (top-level "jobs" list) are fio job data; the
  sibling ``fio_*.json`` only has "runs" and is ignored (handled by
  ``raw_fio_metrics``).
* The biolc summary JSON only carries presence booleans for the histogram — the
  ``@biolc_service_us`` block in ``block_io_latency_cost.bpftrace.log`` (LAST block)
  is the real histogram; buckets use K (=1024) / M (=1048576) suffixes, values in µs.

Flags: ATTRACTOR (BE agg BW within ±3% of 0.0389 GiB/s), INCOMPLETE (no fio jobs
JSON), PARTIAL_DATA (LC or BE scene missing), HIST_MISMATCH (histogram total differs
from the summary's biolc_service_sample_count by >0.1%), SCENE_FAILURE (failed scenes
in metadata), PARSE_ERROR (per-dir exception; the run never aborts the batch).

CLI:
  python3 scripts/analyze_d3_regime.py \
      --results-root <DIR> --source <LABEL> [--results-root <DIR2> --source <LABEL2> ...] \
      --output-dir <OUT>
``--archive-dir <DIR>`` is accepted as a single-root convenience (label "archive").
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(WORKSPACE_ROOT / "src"))

from src.lib.isol_bench.metrics import jains_fairness_index
from src.lib.isol_bench.result_metrics import load_json, monitoring_summary, raw_fio_metrics

SCENE_SYSTEM_METRIC = "ib_system_metric"
SCENE_BIOLC = "ib_block_io_latency_cost"
SCENE_SBF = "ib_scx_io_bypass_fallback"
BIOLC_LOG_NAME = "block_io_latency_cost.bpftrace.log"
SBF_LOG_NAME = "scx_io_bypass_fallback.bpftrace.log"
TARGET_DISK = "sdb"
STEP_INDEX = 0

# Variant ordering mirrors the D3 table in the campaign FULL_BASELINE_SUMMARY.md.
NATIVE_KNOBS = (
    "none",
    "kyber",
    "bfq",
    "mq_deadline",
    "io_max",
    "io_latency",
    "io_cost",
    "io_prio_class",
    "io_bfq_weight",
)

# The three known bad arbiters from the 20260916 baseline campaign.
BAD_ARBITERS = ("arb_cost_hetero", "arb_noop_hetero", "arb_sp_hetero")

# Little's-law attractor: bad arbiters collapse BE agg BW to ~0.0389 GiB/s
# (~10.6K IOPS). ATTRACTOR flag = BE agg BW within ±3% of this value.
ATTRACTOR_BE_BW_GIB_S = 0.0389
ATTRACTOR_TOL = 0.03

GE16_MS_US = 16384.0
GE64_MS_US = 65536.0
HIST_TOTAL_TOL = 0.001  # 0.1% sanity tolerance vs biolc_service_sample_count

CSV_COLUMNS = (
    "source",
    "sequence",
    "variant",
    "arm",
    "hetero",
    "execution_time",
    "lc_p50_us",
    "lc_p90_us",
    "lc_p99_us",
    "lc_p999_us",
    "lc_bw_mib_s",
    "lc_iops",
    "be_bw_agg_gib_s",
    "be_bw_per_tenant_mib_s",
    "be_jain",
    "be_p50_min_us",
    "be_p50_max_us",
    "be_p99_min_us",
    "be_p99_max_us",
    "inflight_avg",
    "inflight_max",
    "biolc_service_avg_us",
    "biolc_service_sample_count",
    "biolc_ge16ms_ratio",
    "biolc_ge64ms_ratio",
    "biolc_issue_count",
    "biolc_complete_count",
    "sbf_insert_requests_count",
    "sbf_bpf_dispatch_count",
    "sbf_bpf_normal_dispatch_count",
    "sbf_dispatch_request_count",
    "sbf_blk_mq_dispatch_count",
    "sbf_bypass_request_count",
    "sbf_no_policy_count",
    "sbf_policy_rejected_count",
    "flags",
)

SBF_SUMMARY_COUNTERS = (
    "sbf_insert_requests_count",
    "sbf_bpf_dispatch_count",
    "sbf_bpf_normal_dispatch_count",
    "sbf_dispatch_request_count",
    "sbf_blk_mq_dispatch_count",
    "sbf_bypass_request_count",
    "sbf_no_policy_count",
    "sbf_policy_rejected_count",
)

_D3_RE = re.compile(r"D3LC_rread_4k_(?P<variant>[A-Za-z0-9_.]+?)(?:_p(?P<pctl>\d+))?$")
_BIOLC_BLOCK_START_RE = re.compile(r"^@biolc_service_us\s*:")
# Bucket lines come in two shapes: ranges "[16, 32)            7045 |" /
# "[64K, 128K)        74458 |" and single-value buckets "[0]  1 |" / "[1]  0 |"
# (real logs open with them when the lhist covers sub-step values).
_BUCKET_RE = re.compile(
    r"^\[\s*(?P<lo>\d+(?:\.\d+)?)\s*(?P<lo_suf>[KM]?)"
    r"(?:\s*,\s*(?P<hi>\d+(?:\.\d+)?)\s*(?P<hi_suf>[KM]?)\s*\)|\s*\])"
    r"\s+(?P<count>\d+)"
)
_SBF_NORMAL_DISPATCH_RE = re.compile(r"@sbf_bpf_normal_dispatch_count\s*:\s*(\d+)")

_BUCKET_SUFFIX_MULT = {"": 1.0, "K": 1024.0, "M": 1024.0 * 1024.0}


@dataclass
class D3Record:
    path: Path
    seq_id: str
    source: str
    arm: str
    variant: str
    hetero: bool
    execution_time: str
    failed_scenes: int = 0
    row: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


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


def classify_d3(seq_id: str) -> tuple[str, str, bool] | None:
    """Return (arm, variant, hetero) for a D3 sequence id, else None.

    Same conventions as analyze_rig_full: the iox arm carries an ``iox`` token in
    the variant (e.g. ``iox_arb_sp_hetero``); native D3 is class-aware
    (hetero-style) by definition, iox variants only when the id says ``_hetero``.
    """
    match = _D3_RE.fullmatch(seq_id)
    if match is None:
        return None
    variant = match.group("variant")
    tokens = variant.split("_")
    if "iox" in tokens:
        arm = "iox"
        knob = "_".join(token for token in tokens if token != "iox")
    else:
        arm = "native"
        knob = variant
    if match.group("pctl"):
        knob += "_p" + match.group("pctl")
    hetero = "hetero" in seq_id.lower() or arm == "native"
    return arm, knob, hetero


def _variant_sort_key(arm: str, variant: str) -> tuple[int, int, str]:
    """Native knobs first (NATIVE_KNOBS order), then iox variants alphabetically."""
    if arm == "native":
        try:
            return (0, NATIVE_KNOBS.index(variant), variant)
        except ValueError:
            return (0, len(NATIVE_KNOBS), variant)
    return (1, 0, variant)


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
        if isinstance(failed, dict):
            count = len(failed)
        elif isinstance(failed, (list, tuple, set)):
            count = len(failed)
    return count


def _pick_percentile(percentiles: Any, target: float) -> float | None:
    """Fetch a clat percentile (µs) by numeric key match ("99.000000" → 99.0)."""
    if not isinstance(percentiles, dict):
        return None
    for key, value in percentiles.items():
        number = _f(key)
        if number is not None and abs(number - target) <= 1e-6:
            return _f(value)
    return None


def _first_direction(job_metrics: dict[str, Any]) -> dict[str, Any] | None:
    directions = _mapping(job_metrics.get("directions"))
    for name in ("read", "write", "trim"):
        direction = _mapping(directions.get(name))
        if direction:
            return direction
    return None


def _split_lc_be(tenants: list[tuple[str, dict[str, Any], Path]]) -> tuple[Any, list[Any]]:
    """LC = first fio scene dir whose lowercased name contains 'lc';
    BE = all fio scene dirs whose lowercased name contains 'be'."""
    lc = None
    be: list[Any] = []
    for name, metrics, _path in tenants:
        low = name.lower()
        if lc is None and "lc" in low:
            lc = (name, metrics)
        elif "be" in low:
            be.append((name, metrics))
    return lc, be


def _pick_device(block: Any) -> dict[str, Any] | None:
    """Pick the target disk (sdb) entry from a per-device summary mapping."""
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


def _bucket_bound(value: str, suffix: str) -> float:
    return float(value) * _BUCKET_SUFFIX_MULT.get(suffix, 1.0)


def parse_biolc_service_hist(log_text: str) -> list[tuple[float, float, int]] | None:
    """Parse the LAST ``@biolc_service_us`` block of a biolc bpftrace log.

    Returns a list of ``(lower_us, upper_us, count)`` buckets, or None when the
    log carries no service-time block at all.
    """
    blocks: list[list[tuple[float, float, int]]] = []
    current: list[tuple[float, float, int]] | None = None
    for line in log_text.splitlines():
        if _BIOLC_BLOCK_START_RE.match(line):
            current = []
            blocks.append(current)
            continue
        if current is None:
            continue
        match = _BUCKET_RE.match(line)
        if match is None:
            current = None  # first non-bucket line ends the block
            continue
        lo = _bucket_bound(match.group("lo"), match.group("lo_suf"))
        if match.group("hi") is None:
            hi = lo  # single-value bucket "[N]"
        else:
            hi = _bucket_bound(match.group("hi"), match.group("hi_suf"))
        current.append((lo, hi, int(match.group("count"))))
    if not blocks:
        return None
    return blocks[-1]


# Generalized histogram-block header: "@<map_name>:" (unkeyed, e.g.
# "@biolc_service_us:") or "@<map_name>[<key>]:" (keyed, e.g. "@biolc_stack_path_us_cpu[5]:").
# A header line carries NOTHING after the colon — scalar lines like
# "@biolc_stack_sample_count: 2375703" or stats lines like
# "@biolc_service_ns: { .count = ... }" therefore do not match.
_HIST_BLOCK_START_RE = re.compile(r"^@(?P<name>[A-Za-z0-9_]+)(?P<key>\[[^\]]*\])?:\s*$")


def parse_biolc_hist_blocks(log_text: str) -> dict[str, list[tuple[float, float, int]]]:
    """Parse ALL log2-histogram map blocks of a biolc bpftrace log (generalized).

    Returns a mapping of map name (keyed maps keep their key suffix, e.g.
    ``biolc_stack_path_us_cpu[5]``) to the LAST ``(lower_us, upper_us, count)``
    bucket list printed for that map — bpftrace may print the same map more
    than once (e.g. once inside and once after ``BLOCK_IO_LATENCY_COST_SUMMARY_END``),
    and the last block is the final histogram.

    Bucket shapes (values in µs, K=1024, M=1048576):
    * ranges: ``[16, 32)    7045 |@@`` / ``[64K, 128K)  74458 |``
    * single-value buckets (lo==hi) at the start: ``[0]  2629280 |``

    Per-CPU keyed blocks (``@biolc_stack_path_us_cpu[N]:``) are stored with
    their key-suffixed names; consumers that only need the aggregate maps
    (``biolc_service_us`` / ``biolc_e2e_us`` / ``biolc_queue_us``) simply look
    those keys up.  Semantics are identical to ``parse_biolc_service_hist``
    restricted to the ``biolc_service_us`` key: same bucket regex, same
    last-block-wins rule, same single-value-bucket handling.
    """
    hists: dict[str, list[tuple[float, float, int]]] = {}
    current: list[tuple[float, float, int]] | None = None
    for line in log_text.splitlines():
        match = _BUCKET_RE.match(line)
        if match is not None and current is not None:
            lo = _bucket_bound(match.group("lo"), match.group("lo_suf"))
            if match.group("hi") is None:
                hi = lo  # single-value bucket "[N]"
            else:
                hi = _bucket_bound(match.group("hi"), match.group("hi_suf"))
            current.append((lo, hi, int(match.group("count"))))
            continue
        header = _HIST_BLOCK_START_RE.match(line)
        if header is not None:
            name = header.group("name") + (header.group("key") or "")
            current = []
            # last block per map name wins: overwrite any earlier block
            hists[name] = current
            continue
        current = None  # any other line ends the current block
    return hists


def _analyze_fio(record: D3Record) -> None:
    row = record.row
    tenants = raw_fio_metrics(record.path, step_index=STEP_INDEX)
    if not tenants:
        record.flags.append("INCOMPLETE")
        return
    lc, be_scenes = _split_lc_be(tenants)

    if lc is not None:
        _, metrics = lc
        percentiles = metrics.get("clat_percentiles_us")
        row["lc_p50_us"] = _pick_percentile(percentiles, 50.0)
        row["lc_p90_us"] = _pick_percentile(percentiles, 90.0)
        row["lc_p99_us"] = _pick_percentile(percentiles, 99.0)
        row["lc_p999_us"] = _pick_percentile(percentiles, 99.9)
        # Unit quirk: agg_bw_mbps is actually MiB/s (bytes/1048576).
        row["lc_bw_mib_s"] = _f(metrics.get("agg_bw_mbps"))
        row["lc_iops"] = _f(metrics.get("iops"))

    per_tenant: list[float] = []
    p50s: list[float] = []
    p99s: list[float] = []
    for _name, metrics in be_scenes:
        for job in metrics.get("per_job_metrics") or []:
            if not isinstance(job, dict):
                continue
            direction = _first_direction(job)
            if direction is None:
                continue
            bw = _f(direction.get("bw_mbps"))
            if bw is not None:
                per_tenant.append(bw)
            percentiles = direction.get("clat_percentiles_us")
            p50 = _pick_percentile(percentiles, 50.0)
            p99 = _pick_percentile(percentiles, 99.0)
            if p50 is not None:
                p50s.append(p50)
            if p99 is not None:
                p99s.append(p99)

    if per_tenant:
        row["be_bw_per_tenant_mib_s"] = ";".join(f"{value:.3f}" for value in per_tenant)
        row["be_bw_agg_gib_s"] = sum(per_tenant) / 1024.0
        try:
            row["be_jain"] = jains_fairness_index(per_tenant)  # equal-weight Jain
        except ValueError:
            pass
    if p50s:
        row["be_p50_min_us"] = min(p50s)
        row["be_p50_max_us"] = max(p50s)
    if p99s:
        row["be_p99_min_us"] = min(p99s)
        row["be_p99_max_us"] = max(p99s)

    if lc is None or not be_scenes:
        record.flags.append("PARTIAL_DATA")


def _analyze_system_metric(record: D3Record) -> str | None:
    system = monitoring_summary(record.path, SCENE_SYSTEM_METRIC, step_index=STEP_INDEX)
    if not system:
        return SCENE_SYSTEM_METRIC
    summary = _mapping(system.get("summary"))
    inflight = _pick_device(summary.get("disk_ios_in_progress"))
    if inflight is not None:
        record.row["inflight_avg"] = _f(inflight.get("avg"))
        record.row["inflight_max"] = _f(inflight.get("max"))
    return None


def _analyze_biolc(record: D3Record) -> str | None:
    row = record.row
    sample_count: int | None = None
    biolc = monitoring_summary(record.path, SCENE_BIOLC, step_index=STEP_INDEX)
    if biolc:
        summary = _mapping(biolc.get("summary"))
        scalars = _mapping(summary.get("scalar_maps"))
        stats = _mapping(summary.get("stats_maps"))
        row["biolc_issue_count"] = _i(scalars.get("biolc_issue_count"))
        row["biolc_complete_count"] = _i(scalars.get("biolc_complete_count"))
        sample_count = _i(scalars.get("biolc_service_sample_count"))
        row["biolc_service_sample_count"] = sample_count
        service_ns = _mapping(stats.get("biolc_service_ns"))
        avg_ns = _f(service_ns.get("avg"))
        if avg_ns is not None:
            row["biolc_service_avg_us"] = avg_ns / 1000.0
    else:
        return SCENE_BIOLC

    # The real histogram lives in the bpftrace log, not the summary JSON.
    log_path = record.path / f"step_{STEP_INDEX}" / SCENE_BIOLC / BIOLC_LOG_NAME
    log_text = _read_text(log_path)
    if log_text is None:
        return None
    buckets = parse_biolc_service_hist(log_text)
    if not buckets:
        return None
    total = sum(count for _lo, _hi, count in buckets)
    if total <= 0:
        return None
    row["biolc_ge16ms_ratio"] = sum(c for lo, _hi, c in buckets if lo >= GE16_MS_US) / total
    row["biolc_ge64ms_ratio"] = sum(c for lo, _hi, c in buckets if lo >= GE64_MS_US) / total
    if sample_count and abs(total - sample_count) > HIST_TOTAL_TOL * sample_count:
        record.flags.append("HIST_MISMATCH")
    return None


def _analyze_sbf(record: D3Record) -> str | None:
    """scx_io bypass-fallback counters — iox arm only (native rows stay empty)."""
    if record.arm != "iox":
        return None
    row = record.row
    sbf = monitoring_summary(record.path, SCENE_SBF, step_index=STEP_INDEX)
    if not sbf:
        return SCENE_SBF
    scalars = _mapping(_mapping(sbf.get("summary")).get("scalar_maps"))
    for key in SBF_SUMMARY_COUNTERS:
        row[key] = _i(scalars.get(key))
    # Summary value wins when present; old archived summaries lack
    # sbf_bpf_normal_dispatch_count, so fall back to the last
    # "@sbf_bpf_normal_dispatch_count: <N>" line in the bpftrace log.
    if row.get("sbf_bpf_normal_dispatch_count") is None:
        log_path = record.path / f"step_{STEP_INDEX}" / SCENE_SBF / SBF_LOG_NAME
        log_text = _read_text(log_path)
        if log_text is not None:
            matches = _SBF_NORMAL_DISPATCH_RE.findall(log_text)
            if matches:
                row["sbf_bpf_normal_dispatch_count"] = int(matches[-1])
    return None


def analyze_record(record: D3Record) -> None:
    _analyze_fio(record)
    missing: list[str] = []
    for extractor in (_analyze_system_metric, _analyze_biolc, _analyze_sbf):
        scene = extractor(record)
        if scene:
            missing.append(scene)
    if missing:
        record.flags.append("MONITOR_MISSING")

    agg_bw = _f(record.row.get("be_bw_agg_gib_s"))
    if agg_bw is not None and abs(agg_bw - ATTRACTOR_BE_BW_GIB_S) <= ATTRACTOR_TOL * ATTRACTOR_BE_BW_GIB_S:
        record.flags.append("ATTRACTOR")
    if record.failed_scenes > 0:
        record.flags.append("SCENE_FAILURE")


def scan_result_root(
    root: Path,
    label: str,
    seen: set[Path],
    records: list[D3Record],
) -> tuple[int, int]:
    """Append D3 records found under root; returns (d3_found, non_d3_skipped)."""
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
            continue  # not a sequence result dir
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
        info = classify_d3(seq_id)
        if info is None:
            skipped += 1
            continue
        arm, variant, hetero = info
        record = D3Record(
            path=path,
            seq_id=seq_id,
            source=label,
            arm=arm,
            variant=variant,
            hetero=hetero,
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
                "hetero": record.hetero,
                "execution_time": record.execution_time,
            }
        )
        record.row["flags"] = ";".join(dict.fromkeys(record.flags))
        records.append(record)
        found += 1
    return found, skipped


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(round(value, 4))
    return str(value)


def write_csv(path: Path, records: list[D3Record]) -> None:
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
    return f"{number:.4f}"


def _fmt_pair(first: Any, second: Any, digits: int = 1) -> str:
    if _f(first) is None and _f(second) is None:
        return "—"
    return f"{_fmt(first, digits)} / {_fmt(second, digits)}"


def _fmt_bw(value: Any) -> str:
    number = _f(value)
    if number is None:
        return "—"
    return f"{number:.3f}"


def _grouped(records: list[D3Record]) -> list[tuple[str, str, list[D3Record]]]:
    """Group by (arm, variant); representative = earliest execution_time."""
    groups: dict[tuple[str, str], list[D3Record]] = {}
    for record in records:
        groups.setdefault((record.arm, record.variant), []).append(record)
    for runs in groups.values():
        runs.sort(key=lambda r: (r.execution_time, r.seq_id, str(r.path)))
    ordered = sorted(groups.items(), key=lambda item: _variant_sort_key(item[0][0], item[0][1]))
    return [(arm, variant, runs) for (arm, variant), runs in ordered]


def _source_table(source: str, root: Path, records: list[D3Record]) -> list[str]:
    lines = [
        f"### source: {source} (`{root}`) — {len(records)} D3 run(s)",
        "",
        "| variant | arm | hetero | runs | LC p50 (µs) | LC p90 (µs) | LC p99 (µs) | LC p99.9 (µs) | LC BW (MiB/s) | LC IOPS | BE agg BW (GiB/s) | BE per-tenant (MiB/s) | BE Jain | BE p50 min/max (µs) | BE p99 min/max (µs) | inflight avg/max | biolc svc avg (µs) | biolc ≥16ms | biolc ≥64ms | biolc iss/comp | flags |",
        "|---------|-----|---------|------|-------------|-------------|-------------|---------------|--------------|---------|------------------|----------------------|---------|-------------------|-------------------|-----------------|--------------------|-------------|-------------|---------------|-------|",
    ]
    for arm, variant, runs in _grouped(records):
        rep = runs[0]
        row = rep.row
        per_tenant = str(row.get("be_bw_per_tenant_mib_s") or "")
        issue = _i(row.get("biolc_issue_count"))
        complete = _i(row.get("biolc_complete_count"))
        biolc_cell = "—"
        if issue is not None or complete is not None:
            biolc_cell = f"{issue if issue is not None else '—'} / {complete if complete is not None else '—'}"
        lines.append(
            f"| {variant} | {arm} | {'yes' if row.get('hetero') else 'no'} | {len(runs)} | "
            f"{_fmt(row.get('lc_p50_us'))} | {_fmt(row.get('lc_p90_us'))} | {_fmt(row.get('lc_p99_us'))} | "
            f"{_fmt(row.get('lc_p999_us'))} | {_fmt_bw(row.get('lc_bw_mib_s'))} | {_fmt(row.get('lc_iops'))} | "
            f"{_fmt_bw(row.get('be_bw_agg_gib_s'))} | {per_tenant or '—'} | {_fmt_ratio(row.get('be_jain'))} | "
            f"{_fmt_pair(row.get('be_p50_min_us'), row.get('be_p50_max_us'))} | "
            f"{_fmt_pair(row.get('be_p99_min_us'), row.get('be_p99_max_us'))} | "
            f"{_fmt_pair(row.get('inflight_avg'), row.get('inflight_max'))} | "
            f"{_fmt(row.get('biolc_service_avg_us'))} | {_fmt_ratio(row.get('biolc_ge16ms_ratio'))} | "
            f"{_fmt_ratio(row.get('biolc_ge64ms_ratio'))} | {biolc_cell} | {row.get('flags') or ''} |"
        )
    lines.append("")
    return lines


def _bad_arbiter_section(by_source: list[tuple[str, Path, list[D3Record]]]) -> list[str]:
    low = ATTRACTOR_BE_BW_GIB_S * (1.0 - ATTRACTOR_TOL)
    high = ATTRACTOR_BE_BW_GIB_S * (1.0 + ATTRACTOR_TOL)
    lines = [
        "## Grand summary — known bad arbiters (attractor cross-check)",
        "",
        f"Known attractor (20260916 baseline): BE agg BW ≈ {ATTRACTOR_BE_BW_GIB_S:.4f} GiB/s "
        f"(Little's-law collapse, ~10.6K IOPS). ATTRACTOR flag = BE agg BW within "
        f"±{ATTRACTOR_TOL * 100:.0f}% → band [{low:.5f}, {high:.5f}] GiB/s.",
        "",
        "| source | arbiter | LC p99 (µs) | BE agg BW (GiB/s) | biolc ≥64ms | ATTRACTOR | flags |",
        "|--------|---------|------------|------------------|------------|-----------|-------|",
    ]
    for source, _root, records in by_source:
        for arbiter in BAD_ARBITERS:
            runs = [
                r
                for r in records
                if r.arm == "iox" and r.variant == arbiter
            ]
            if not runs:
                lines.append(f"| {source} | {arbiter} | _missing_ | _missing_ | _missing_ | — | |")
                continue
            rep = sorted(runs, key=lambda r: (r.execution_time, r.seq_id))[0]
            row = rep.row
            attractor = "yes" if "ATTRACTOR" in rep.flags else "no"
            lines.append(
                f"| {source} | {arbiter} | {_fmt(row.get('lc_p99_us'))} | "
                f"{_fmt_bw(row.get('be_bw_agg_gib_s'))} | {_fmt_ratio(row.get('biolc_ge64ms_ratio'))} | "
                f"{attractor} | {row.get('flags') or ''} |"
            )
    lines.append("")
    return lines


def build_markdown(
    out_dir: Path,
    by_source: list[tuple[str, Path, list[D3Record]]],
    non_d3_total: dict[str, int],
) -> str:
    lines: list[str] = [
        "# D3 Regime Metrics",
        "",
        f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Target disk**: {TARGET_DISK}",
        "",
        "### Inputs",
        "",
        "| source | dir | D3 runs | non-D3 skipped |",
        "|--------|-----|---------|-----------------|",
    ]
    for source, root, records in by_source:
        non_d3 = non_d3_total.get(source, 0)
        lines.append(f"| {source} | {root} | {len(records)} | {non_d3} |")
    lines.append("")
    for source, root, records in by_source:
        lines.extend(_source_table(source, root, records))
    lines.extend(_bad_arbiter_section(by_source))
    report = "\n".join(lines) + "\n"
    (out_dir / "d3_regime_metrics.md").write_text(report, encoding="utf-8")
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract D3 regime metrics (LC clat percentiles, BE fairness, in-flight depth, "
            "biolc service-time histogram tail ratios, sbf counters) for every D3 sequence "
            "result directory under one or more result roots."
        ),
        epilog=(
            "Outputs (under --output-dir): d3_regime_metrics.csv and d3_regime_metrics.md. "
            "Repeatable --results-root/--source pairs are matched by position. "
            "--archive-dir is a single-root convenience (source label 'archive'). "
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
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=None,
        help="single-root convenience: analyzed with source label 'archive'",
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="directory for the CSV/MD outputs")
    args = parser.parse_args(argv)
    if len(args.results_root) != len(args.source):
        parser.error(
            f"--results-root and --source must be paired (got {len(args.results_root)} root(s) "
            f"and {len(args.source)} source label(s))"
        )
    if not args.results_root and args.archive_dir is None:
        parser.error("no inputs: pass --results-root/--source pairs and/or --archive-dir")
    return args


def main() -> int:
    args = parse_args()
    pairs: list[tuple[str, Path]] = [
        (label, Path(root)) for label, root in zip(args.source, args.results_root)
    ]
    if args.archive_dir is not None:
        pairs.append(("archive", args.archive_dir))

    records: list[D3Record] = []
    seen: set[Path] = set()
    by_source: list[tuple[str, Path, list[D3Record]]] = []
    non_d3_total: dict[str, int] = {}
    for label, root in pairs:
        batch: list[D3Record] = []
        found, skipped = scan_result_root(root, label, seen, batch)
        non_d3_total[label] = skipped
        by_source.append((label, root, batch))
        records.extend(batch)
        if found == 0:
            warn(f"{label}: no D3 sequence result dirs under {root}")

    # CSV ordering: source order, then native-knob-first/iox-alphabetical variant order.
    source_order = {label: index for index, (label, _root, _recs) in enumerate(by_source)}
    records.sort(
        key=lambda r: (
            source_order.get(r.source, 0),
            _variant_sort_key(r.arm, r.variant),
            r.execution_time,
            r.seq_id,
        )
    )

    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "d3_regime_metrics.csv", records)

    build_markdown(out_dir, by_source, non_d3_total)

    attractor_rows = [
        record
        for record in records
        if record.arm == "iox" and record.variant in BAD_ARBITERS
    ]
    print(f"D3 runs analyzed: {len(records)} across {len(by_source)} source(s)")
    for label, root, batch in by_source:
        print(f"  {label}: {len(batch)} D3 run(s) under {root} ({non_d3_total[label]} non-D3 skipped)")
    if attractor_rows:
        for record in attractor_rows:
            print(
                f"  bad arbiter [{record.source}] {record.seq_id}: "
                f"lc_p99_us={_fmt(record.row.get('lc_p99_us'))} "
                f"be_bw_agg_gib_s={_fmt_bw(record.row.get('be_bw_agg_gib_s'))} "
                f"biolc_ge64ms_ratio={_fmt_ratio(record.row.get('biolc_ge64ms_ratio'))} "
                f"flags={record.row.get('flags') or ''}"
            )
    print(f"Wrote {out_dir}/d3_regime_metrics.csv")
    print(f"Wrote {out_dir}/d3_regime_metrics.md")
    if not records:
        warn("no D3 sequence result dirs found in any input — nothing to analyze")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())