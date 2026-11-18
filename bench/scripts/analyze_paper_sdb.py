#!/usr/bin/env python3
"""Analyze the paper_sdb dress-rehearsal campaign (kvmhost, sdb track).

Scans an extracted-results root (one directory per sequence, as produced by
extract_paper_sdb.sh) and emits one row per sequence into:

  paper_sdb_all.csv       — every sequence: family, arm, axis, primary metrics,
                            monitors, completeness/flags. Failure/incomplete
                            rows are kept (PARSE_ERROR / SCENE_MISSING) — the
                            rehearsal verdict needs them.
  PAPER_SDB_SUMMARY.md    — per-family tables + pass/fail accounting.

Sequence families (paper_sdb track):
  D1Q1_{arm}_n{N}                    D1-Q1 latency scaling (19 arms x n)
  D1Q2_{arm}_n{N}                    D1-Q2 bandwidth scaling
  D2_saturated_weighted_{arm}_cg{N}_iter{i}
  D3LC_rread_4k_{knob}[_pNNN]        D3-LC Pareto (native) + iox homo
  D3LC_rread_4k_iox_{policy}[_hetero]
  D4LC_{knob} / D4LC_iox_{policy}    D4 burst (LC lat-log response time)
  D1Q3_{arm}_{track}                 framework-tax ladder
  D1Q4_kyber|mq_deadline_{native|iox}_{read|write}
  D1Q5_iox_{arm}_n{N}                fixed-load tenant scaling
  D1Q6_d{depth}_{arm}                hierarchy-depth scaling
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(WORKSPACE_ROOT / "src"))

from src.lib.isol_bench.result_metrics import load_json  # noqa: E402

FAMILIES = (
    ("D1Q1", re.compile(r"^D1Q1_(?P<arm>.+)_n(?P<n>\d+)$")),
    ("D1Q2", re.compile(r"^D1Q2_(?P<arm>.+)_n(?P<n>\d+)$")),
    ("D2", re.compile(r"^D2_saturated_weighted_(?P<arm>.+)_cg(?P<ncg>\d+)_iter(?P<it>\d+)$")),
    ("D3", re.compile(r"^D3LC_rread_4k_(?P<arm>.+)$")),
    ("D4", re.compile(r"^D4LC_(?P<arm>.+)$")),
    ("D1Q3", re.compile(r"^D1Q3_(?P<arm>.+)_(?P<track>lc|batch)$")),
    ("D1Q4", re.compile(r"^D1Q4_(?P<arm>.+)$")),
    ("D1Q5", re.compile(r"^D1Q5_(?P<arm>.+)_n(?P<n>\d+)$")),
    ("D1Q6", re.compile(r"^D1Q6_d(?P<depth>\d+)_(?P<arm>.+)$")),
)


def classify(seq_id: str):
    for fam, rx in FAMILIES:
        m = rx.match(seq_id)
        if m:
            return fam, m.groupdict()
    return "?", {}


def fio_jobs(seq_dir: Path):
    """Yield (json_path, parsed) for every fio json under step_*."""
    for p in sorted(seq_dir.glob("step_*/*/*fio.json")):
        try:
            d = load_json(p)
        except Exception:
            continue
        if not d:
            continue
        yield p, d


def per_job_reads(seq_dir: Path):
    """[(jobname, bw_bytes, clat_pct dict, scene_dir)] for all jobs."""
    out = []
    for p, d in fio_jobs(seq_dir):
        scene = p.parent.name
        for j in d.get("jobs") or []:
            r = j.get("read") or {}
            clat = ((r.get("clat_ns") or {}).get("percentile")) or {}
            out.append({
                "scene": scene,
                "job": j.get("jobname") or "",
                "bw": r.get("bw_bytes") or 0,
                "p50_us": clat.get("50.000000", 0) / 1000 if clat.get("50.000000") else None,
                "p99_us": clat.get("99.000000", 0) / 1000 if clat.get("99.000000") else None,
                "p999_us": clat.get("99.900000", 0) / 1000 if clat.get("99.900000") else None,
                "iodepth": (j.get("job options") or {}).get("iodepth"),
                "scene": scene,
            })
    return out


def weighted_jain(bws, weights=None):
    n = len(bws)
    if n == 0:
        return None
    w = weights or list(range(1, n + 1))
    if len(w) != n:
        return None
    pairs = sorted(zip(bws, w))
    ratios = [b / wi for b, wi in pairs]
    return sum(ratios) ** 2 / (n * sum(x * x for x in ratios))


def spearman(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def burst_response_ms(seq_dir: Path):
    """LC write_lat_log clat series -> response time (rig analyzer convention)."""
    logs = [p for p in sorted(seq_dir.glob("step_*/*/*_clat.*.log"))
            if "lc" in p.parent.name.lower()]
    if not logs:
        return None
    try:
        from src.lib.isol_bench.burst_metrics import compute_burst_response_ms as _cr
        burst = _cr(logs[0], mode="latency", burst_start_ms=0.0)
        return burst.get("response_ms")
    except Exception:
        return None


def monitors(seq_dir: Path):
    out: dict[str, Any] = {}
    sm = sorted(seq_dir.glob("step_*/ib_system_metric/*summary*.json")) or sorted(seq_dir.glob("**/system_metric*summary*.json"))
    if sm:
        try:
            d = load_json(sm[0]) or {}
            disks = d.get("disks") or d.get("per_disk") or {}
            if isinstance(disks, dict):
                first = next(iter(disks.values()), {})
                out["disk_busy_avg_pct"] = first.get("util_avg_pct") or first.get("busy_avg_pct")
        except Exception:
            pass
    biolc = sorted(seq_dir.glob("**/block_io_latency_cost_summary.json"))
    if biolc:
        try:
            d = load_json(biolc[0]) or {}
            out["biolc_issued"] = d.get("issued") or d.get("issue_count")
            out["biolc_completed"] = d.get("completed") or d.get("complete_count")
            out["biolc_svc_avg_us"] = (d.get("service_ns_avg") or d.get("service_avg_ns") or 0) / 1000 or None
        except Exception:
            pass
    by = sorted(seq_dir.glob("**/scx_io_bypass_fallback_summary.json"))
    if by:
        try:
            d = load_json(by[0]) or {}
            out["bypass_true"] = d.get("bypass_true_count") or d.get("true_count")
        except Exception:
            pass
    return out


def analyze(seq_dir: Path):
    seq_id = seq_dir.name.replace("rig_", "").replace("paper_sdb_", "")
    fam, ax = classify(seq_id)
    row: dict[str, Any] = {"sequence_id": seq_id, "family": fam, "arm": ax.get("arm", ""),
                           "flags": "", "results_dir": str(seq_dir)}
    row.update({k: v for k, v in ax.items() if k != "arm"})
    jobs = per_job_reads(seq_dir)
    if not jobs:
        row["flags"] = "NO_FIO_JSON"
        return row
    bws = [j["bw"] / 1e6 for j in jobs]
    p99s = [j["p99_us"] for j in jobs if j["p99_us"]]
    p50s = [j["p50_us"] for j in jobs if j["p50_us"]]
    row["jobs"] = len(jobs)
    row["agg_bw_mb_s"] = round(sum(bws), 2)
    row["p50_avg_us"] = round(sum(p50s) / len(p50s), 1) if p50s else None
    row["p99_avg_us"] = round(sum(p99s) / len(p99s), 1) if p99s else None
    row["p99_max_us"] = round(max(p99s), 1) if p99s else None

    if fam == "D2":
        ncg = int(ax.get("ncg", len(jobs)))
        w = list(range(1, ncg + 1))
        jw = weighted_jain(bws, w)
        row["jain_weighted"] = round(jw, 4) if jw else None
        sp = spearman(sorted(bws), sorted(w)) if len(bws) == ncg else None
        row["spearman"] = round(sp, 4) if sp is not None else None
        row["bw_max_min"] = round(max(bws) / min(bws), 3) if bws and min(bws) > 0 else None
        row["per_tenant_bw"] = ";".join(str(round(b, 2)) for b in bws)
    if fam in ("D3", "D4"):
        # fio_subjob scenes omit job options from the JSON; classify by the
        # scene dir name instead (…_be_subjob / …_be vs everything else).
        be_jobs = [j for j in jobs if "_be_" in j.get("scene", "") or j.get("scene", "").endswith("_be")]
        lc_jobs = [j for j in jobs if j not in be_jobs]
        lc_p99 = [j["p99_us"] for j in lc_jobs if j["p99_us"]]
        lc_p50 = [j["p50_us"] for j in lc_jobs if j["p50_us"]]
        row["lc_p99_us"] = round(sum(lc_p99) / len(lc_p99), 1) if lc_p99 else None
        row["lc_p50_us"] = round(sum(lc_p50) / len(lc_p50), 1) if lc_p50 else None
        row["be_bw_mb_s"] = round(sum(j["bw"] / 1e6 for j in be_jobs), 2)
        row["lc_bw_mb_s"] = round(sum(j["bw"] / 1e6 for j in lc_jobs), 2)
        if fam == "D4":
            resp = burst_response_ms(seq_dir)
            row["response_ms"] = round(resp, 1) if resp is not None else None
    row.update(monitors(seq_dir))
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extracted-root", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows, unknown = [], []
    for seq_dir in sorted(args.extracted_root.iterdir()):
        if not seq_dir.is_dir():
            continue
        sid = seq_dir.name.replace("rig_", "").replace("paper_sdb_", "")
        fam, _ = classify(sid)
        if fam == "?":
            unknown.append(sid)
            continue
        try:
            rows.append(analyze(seq_dir))
        except Exception as exc:
            rows.append({"sequence_id": sid, "family": "?", "flags": f"PARSE_ERROR:{type(exc).__name__}",
                         "results_dir": str(seq_dir)})
    fieldnames: list[str] = []
    for r in rows:
        for k in r:
            if k not in fieldnames:
                fieldnames.append(k)
    with open(args.out_dir / "paper_sdb_all.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    # summary
    fams = {}
    for r in rows:
        fams.setdefault(r.get("family"), []).append(r)
    lines = ["# paper_sdb rehearsal — analysis summary", ""]
    total = sum(len(v) for v in fams.values())
    bad = sum(1 for r in rows if "flags" in r and r["flags"])
    lines.append(f"sequences analyzed: {total} (flagged: {bad}); unrecognized: {len(unknown)}")
    for fam in ("D1Q1", "D1Q2", "D2", "D3", "D4", "D1Q3", "D1Q4", "D1Q5", "D1Q6"):
        rs = fams.get(fam) or []
        if not rs:
            continue
        lines.append(f"\n## {fam} ({len(rs)} rows)")
        if fam == "D2":
            for r in sorted(rs, key=lambda x: -(x.get("jain_weighted") or 0)):
                lines.append(f"- {r['sequence_id']}: JainW={r.get('jain_weighted')} spear={r.get('spearman')} "
                              f"max/min={r.get('bw_max_min')} agg={r.get('agg_bw_mb_s')}MB/s {r.get('flags','')}")
        else:
            for r in sorted(rs, key=lambda x: (x.get("arm", ""), str(x.get("n") or x.get("depth") or ""))):
                lines.append(f"- {r['sequence_id']}: p99={r.get('p99_avg_us')}us agg={r.get('agg_bw_mb_s')}MB/s "
                             f"{'resp=' + str(r.get('response_ms')) + 'ms ' if r.get('response_ms') else ''}{r.get('flags','')}")
    (args.out_dir / "PAPER_SDB_SUMMARY.md").write_text("\n".join(lines) + "\n")
    print(f"analyzed {total} sequences -> {args.out_dir}/paper_sdb_all.csv")


if __name__ == "__main__":
    main()