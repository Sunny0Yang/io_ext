#!/usr/bin/env python3
"""kvmhost D2 saturated weighted — curated single panel (cg=4 only).

Per arm: fair-reference stack (weight colors, α≈0.35) + measured stack (same colors).
Bar top: JainW. Right axis: mean read p99 (µs) polyline (auxiliary).

Output:
  results/d2/figures/kvmhost_d2_bw_by_cg.{png,pdf}  — legacy all-in-one
  results/d2/figures/kvmhost_d2_cg4_twopanel.{png,pdf} — (a) BW stack (b) Jain+p99
"""

from __future__ import annotations

import csv
import json
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

W_SCENE_RX = re.compile(r"_w(\d+)_")
SCENE_CG_RX = re.compile(r"_cg(\d+)$")  # tenant weight index at end of scene dir name
CG_JOB_RX = re.compile(r"_cg(\d+)$")

D2_ROOT = Path(__file__).resolve().parents[1]
REPO = D2_ROOT.parents[1]
OUT = D2_ROOT / "figures"
DATA = D2_ROOT / "data"
CACHE = D2_ROOT / ".cache" / "kvmhost_d2"
RERUN = REPO / "results/d2_rerun_kvmhost"
RERUN_CSV = RERUN / "analysis/d2_rerun_by_arm.csv"
CONTENTION_REF_MB_S = {"2": 968.8, "4": 931.8, "8": 902.6, "16": 908.6}

CGS = (2, 4, 8, 16)
PLOT_CGS = (4,)  # curated slip: single panel only

# Curated arms for the ½-page figure (baseline first).
NATIVE_CURATED = ("none", "bfq", "io_max", "io_cost")
IOX_CURATED = ("iox_q_rl", "iox_arb_cost")  # a_cost ≈ best iox arb @ cg4

# Plot id → on-disk sequence arm (bfq slot = cgroup io.weight + BFQ)
SEQ_ARM_ALIASES: Dict[str, str] = {"bfq": "io_bfq_weight"}
# Curated slip uses the single best healthy iter (not cross-iter median).
BEST_ITER_ARMS = frozenset({"iox_q_rl", "bfq"})
PLOT_LABEL: Dict[str, str] = {"bfq": "bfq.w"}
IOX_QUARTET = ("iox_q_wedf", "iox_arb_wdrr", "iox_arb_slo", "iox_arb_cost")  # pick_iox fallback

# Formal PD2 saturated weighted arms (d2_fairness + addendum); baseline first.
NATIVE_ARMS = (
    "none",
    "mq_deadline",
    "bfq",
    "kyber",
    "io_max",
    "io_latency",
    "io_cost",
    "io_bfq_weight",
    "io_prio_class",
)
IOX_ARMS = (
    "iox_q_fifo",
    "iox_q_wedf",
    "iox_arb_wdrr",
    "iox_arb_cost",
    "iox_arb_slo",
    "iox_arb_sp",
    "iox_arb_mix",
    "iox_arb_noop",
    "iox_q_ddl_arb_wdrr",
    "iox_q_sp_edf_arb_wdrr",
)
ARM_ORDER = list(NATIVE_ARMS) + list(IOX_ARMS)

LABEL = {
    "none": "none",
    "mq_deadline": "mq-dl",
    "bfq": "bfq",
    "kyber": "kyber",
    "io_max": "io.max",
    "io_latency": "io.lat",
    "io_cost": "io.cost",
    "io_bfq_weight": "io.bfq.w",
    "io_prio_class": "io.prio",
    "iox_q_fifo": "q_fifo",
    "iox_q_wedf": "q_wedf",
    "iox_q_rl": "q_rl",
    "iox_arb_wdrr": "a_wdrr",
    "iox_arb_cost": "a_cost",
    "iox_arb_slo": "a_slo",
    "iox_arb_sp": "a_sp",
    "iox_arb_mix": "a_mix",
    "iox_arb_noop": "a_noop",
    "iox_q_ddl_arb_wdrr": "ddl+wdrr",
    "iox_q_sp_edf_arb_wdrr": "spedf+wdrr",
}

def short_arm(arm: str) -> str:
    if arm in PLOT_LABEL:
        return PLOT_LABEL[arm]
    return LABEL.get(arm, arm.replace("iox_", "")[:8])


def _seq_arm(plot_arm: str) -> str:
    return SEQ_ARM_ALIASES.get(plot_arm, plot_arm)


def _contention_ref(cg: int) -> float:
    try:
        calib = json.loads((RERUN / "calib.json").read_text())
        return float(calib["contention_ref_mb_s"][str(cg)])
    except Exception:
        return CONTENTION_REF_MB_S.get(str(cg), 0.0)


def ensure_seq(arm: str, cg: int, it: int) -> Optional[Path]:
    sid = f"D2_saturated_weighted_{_seq_arm(arm)}_cg{cg}_iter{it}"
    live = RERUN / sid
    return live if live.is_dir() and any(live.glob("step_*")) else None


def parse_seq_q_rl_run_results(seq_dir: Path, ncg: int) -> Optional[Tuple[List[float], float, Optional[float]]]:
    """q_rl: one fio per wK scene — sum run_results agg_bw_mbps (canonical analyzer path)."""
    rr_path = seq_dir / "run_results.json"
    if not rr_path.is_file():
        return None
    try:
        top = json.loads(rr_path.read_text())
    except Exception:
        return None
    sid = seq_dir.name
    if sid not in top:
        return None
    results = (top[sid].get("steps") or [{}])[0].get("results") or {}
    bws: List[float] = []
    p99s: List[float] = []
    for w in range(1, ncg + 1):
        key = f"ib_d2_iox_w{w}_ncg{ncg}_subjob"
        scene = results.get(key) or {}
        runs = scene.get("runs") or []
        if not runs:
            return None
        run = runs[-1]
        bws.append(float(run.get("agg_bw_mbps", 0)))
        for j in run.get("per_job_metrics") or []:
            dirs = (j.get("directions") or {}).get("read") or {}
            pv = (dirs.get("clat_percentiles_us") or {}).get("99")
            if pv is not None:
                p99s.append(float(pv))
                break
    ws = list(range(1, ncg + 1))
    jw = jain_windexed(ws, bws)
    p99 = statistics.mean(p99s) if p99s else None
    return bws, jw, p99


def parse_seq(seq_dir: Path, ncg: int) -> Optional[Tuple[List[float], float, Optional[float]]]:
    """Contention step: per-tenant MB/s, w-indexed JainW, mean read p99 (µs)."""
    by_w: Dict[int, float] = {}
    p99s: List[float] = []
    for scene_dir in sorted(seq_dir.glob("step_*/*")):
        if not scene_dir.is_dir():
            continue
        p = pick_scene_fio(scene_dir)
        if p is None:
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        jobs = data.get("jobs") or []
        if len(jobs) > 1:
            for j in jobs:
                mj = CG_JOB_RX.search(j.get("jobname") or "")
                if not mj:
                    continue
                w = int(mj.group(1))
                if w in by_w:
                    continue
                by_w[w] = (j.get("read") or {}).get("bw_bytes", 0) / 1e6
                pv = p99_us_from_job(j)
                if pv is not None:
                    p99s.append(pv)
            continue
        if not jobs:
            continue
        m = SCENE_CG_RX.search(scene_dir.name) or re.search(r"_w(\d+)_ncg", scene_dir.name)
        if not m:
            mj = CG_JOB_RX.search(jobs[0].get("jobname") or "")
            if not mj:
                continue
            w = int(mj.group(1))
        else:
            w = int(m.group(1))
        if w in by_w:
            continue
        j = jobs[0]
        by_w[w] = (j.get("read") or {}).get("bw_bytes", 0) / 1e6
        pv = p99_us_from_job(j)
        if pv is not None:
            p99s.append(pv)
    if len(by_w) != ncg:
        return None
    bws = [b for _, b in sorted(by_w.items())]
    ws = [w for w, _ in sorted(by_w.items())]
    jw = jain_windexed(ws, bws)
    p99 = statistics.mean(p99s) if p99s else None
    return bws, jw, p99


def jain_equal_byte_share_ref(cg: int) -> float:
    """w-indexed Jain if every tenant got the same byte rate (unweighted-fair reference)."""
    ws = list(range(1, cg + 1))
    return jain_windexed(ws, [1.0] * cg)


def jain_windexed(weights: List[int], bws: List[float]) -> Optional[float]:
    if len(weights) != len(bws) or not bws:
        return None
    ratios = [b / w for w, b in zip(weights, bws)]
    n = len(ratios)
    return sum(ratios) ** 2 / (n * sum(x * x for x in ratios))


def p99_us_from_job(j: dict) -> Optional[float]:
    clat = ((j.get("read") or {}).get("clat_ns") or {}).get("percentile") or {}
    p99 = clat.get("99.000000")
    return float(p99) / 1000.0 if p99 else None


def pick_scene_fio(scene_dir: Path) -> Optional[Path]:
    """One canonical fio JSON per scene (avoid duplicate fio_* + jobs1_fio)."""
    jobs1 = sorted(scene_dir.glob("*jobs1_fio.json"))
    if jobs1:
        return jobs1[0]
    cands = [p for p in sorted(scene_dir.glob("*fio.json")) if "stderr" not in p.name]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    # prefer jobs1-style name if multiple leftovers
    jobs = [p for p in cands if "jobs1" in p.name]
    return jobs[0] if jobs else cands[-1]


def weight_index(scene: str, jobname: str) -> Optional[int]:
    mw = W_SCENE_RX.search(scene)
    if mw:
        return int(mw.group(1))
    mc = SCENE_CG_RX.search(scene)
    if mc:
        return int(mc.group(1))
    mj = CG_JOB_RX.search(jobname)
    return int(mj.group(1)) if mj else None


def tenants_native(seq_dir: Path, ncg: int) -> Optional[Tuple[List[int], List[float], Optional[float]]]:
    """Per-scene canonical fio; w_i from scene suffix _cgK or _wK (matches D2 ladder)."""
    by_w: Dict[int, float] = {}
    p99s: List[float] = []
    for step in sorted(seq_dir.glob("step_*")):
        if not step.is_dir():
            continue
        for scene_dir in sorted(step.iterdir()):
            if not scene_dir.is_dir():
                continue
            p = pick_scene_fio(scene_dir)
            if p is None:
                continue
            try:
                data = json.loads(p.read_text())
            except Exception:
                continue
            jobs = data.get("jobs") or []
            if not jobs:
                continue
            scene = scene_dir.name
            j = jobs[0]
            w = weight_index(scene, j.get("jobname") or "")
            if w is None or w in by_w:
                continue
            by_w[w] = (j.get("read") or {}).get("bw_bytes", 0) / 1e6
            pv = p99_us_from_job(j)
            if pv is not None:
                p99s.append(pv)

    if len(by_w) == ncg:
        items = sorted(by_w.items())
        ws = [w for w, _ in items]
        bws = [b for _, b in items]
        p99 = statistics.mean(p99s) if p99s else None
        return ws, bws, p99

    # collapsed: single json with ncg jobs (no per-tenant scene dirs)
    for p in sorted(seq_dir.glob("step_*/*/*fio.json")):
        if "stderr" in p.name:
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        jobs = data.get("jobs") or []
        if len(jobs) != ncg:
            continue
        by_w = {}
        p99s = []
        for j in jobs:
            bw = (j.get("read") or {}).get("bw_bytes", 0) / 1e6
            job = j.get("jobname") or ""
            mc = CG_JOB_RX.search(job)
            w = int(mc.group(1)) if mc else len(by_w) + 1
            by_w[w] = bw
            pv = p99_us_from_job(j)
            if pv is not None:
                p99s.append(pv)
        items = sorted(by_w.items())
        ws = [w for w, _ in items]
        bws = [b for _, b in items]
        p99 = statistics.mean(p99s) if p99s else None
        if len(ws) == ncg:
            return ws, bws, p99
    return None


def steady_iters(fn, arm: str, cg: int) -> List[int]:
    """Prefer non–phase-collapsed iters; if all slow/collapsed, keep all parsed iters."""
    ref = _contention_ref(cg)
    good: List[int] = []
    all_iters: List[int] = []
    for it in (1, 2, 3):
        val = fn(arm, cg, it)
        if not val or not val[0]:
            continue
        all_iters.append(it)
        agg = sum(val[0])
        if ref <= 0 or agg >= 0.5 * ref:
            good.append(it)
    return good if good else all_iters


def best_iter_across(
    fn, arm: str, cg: int,
) -> Tuple[List[float], Optional[float], Optional[float]]:
    """Pick highest-aggregate iter; q_rl skips phase-collapsed runs only."""
    ref = _contention_ref(cg)
    require_healthy = arm == "iox_q_rl"
    best: Optional[Tuple[List[float], Optional[float], Optional[float]]] = None
    best_agg = -1.0
    for it in (1, 2, 3):
        val = fn(arm, cg, it)
        if not val or not val[0]:
            continue
        agg = sum(val[0])
        if require_healthy and ref > 0 and agg < 0.5 * ref:
            continue
        if agg > best_agg:
            best_agg = agg
            best = val
    if best:
        return best
    return [], None, None


def median_across_iters(
    fn, arm: str, cg: int,
) -> Tuple[List[float], Optional[float], Optional[float]]:
    """Median per-tenant MB/s + median p99; JainW from median columns (w_i=i)."""
    if arm in BEST_ITER_ARMS and cg in PLOT_CGS:
        return best_iter_across(fn, arm, cg)
    lists: List[List[float]] = []
    p99s: List[float] = []
    jains: List[float] = []
    for it in steady_iters(fn, arm, cg):
        val = fn(arm, cg, it)
        if val is None:
            continue
        bws, p99, jw = val
        if bws:
            lists.append(bws)
        if p99 is not None:
            p99s.append(p99)
        if jw is not None:
            jains.append(jw)
    if not lists:
        return [], (statistics.median(p99s) if p99s else None), None
    n = max(len(x) for x in lists)
    med: List[float] = []
    for i in range(n):
        col = [x[i] for x in lists if len(x) > i]
        if col:
            med.append(statistics.median(col))
    ws = list(range(1, len(med) + 1))
    jw = jain_windexed(ws, med) if len(med) == cg else (statistics.mean(jains) if jains else None)
    return med, (statistics.median(p99s) if p99s else None), jw


def native_iter(arm: str, cg: int, it: int) -> Optional[Tuple[List[float], Optional[float], Optional[float]]]:
    sd = ensure_seq(arm, cg, it)
    if not sd:
        return None
    if _seq_arm(arm) == "iox_q_rl":
        parsed = parse_seq_q_rl_run_results(sd, cg)
    else:
        parsed = parse_seq(sd, cg)
    if not parsed:
        return None
    bws, jw, p99 = parsed
    return bws, p99, jw


def load_jain() -> Tuple[Dict[Tuple[str, int], float], Dict[Tuple[str, int], float]]:
    w: Dict[Tuple[str, int], List[float]] = defaultdict(list)
    u: Dict[Tuple[str, int], List[float]] = defaultdict(list)
    with open(JW) as f:
        for r in csv.DictReader(f):
            key = (r["arm"], int(r["cg"]))
            w[key].append(float(r["jainw_new_windexed"]))
            if r.get("jainw_old_recomputed"):
                u[key].append(float(r["jainw_old_recomputed"]))
    return (
        {k: statistics.mean(v) for k, v in w.items()},
        {k: statistics.mean(v) for k, v in u.items()},
    )


def load_addendum_iox() -> Tuple[
    Dict[Tuple[str, int], List[List[float]]],
    Dict[Tuple[str, int], List[float]],
    Dict[Tuple[str, int], List[float]],
]:
    """per_tenant lists per iter, p99, jain (positional) from addendum extract."""
    bws: Dict[Tuple[str, int], List[List[float]]] = defaultdict(list)
    p99: Dict[Tuple[str, int], List[float]] = defaultdict(list)
    jain: Dict[Tuple[str, int], List[float]] = defaultdict(list)
    with open(ADD) as f:
        for r in csv.DictReader(f):
            if r.get("family") != "D2" or "saturated_weighted" not in r["sequence_id"]:
                continue
            arm = r["arm"]
            if not arm.startswith("iox_"):
                continue
            cg = int(r["ncg"])
            pt = r.get("per_tenant_bw") or ""
            if pt and ";" in pt:
                bws[(arm, cg)].append([float(x) for x in pt.split(";") if x])
            if r.get("p99_avg_us"):
                p99[(arm, cg)].append(float(r["p99_avg_us"]))
            if r.get("jain_weighted"):
                jain[(arm, cg)].append(float(r["jain_weighted"]))
    return bws, p99, jain


def aggregate_iox_tenants(
    raw: Dict[Tuple[str, int], List[List[float]]],
) -> Dict[Tuple[str, int], List[float]]:
    out: Dict[Tuple[str, int], List[float]] = {}
    for key, lists in raw.items():
        if not lists:
            continue
        n = max(len(x) for x in lists)
        med = []
        for i in range(n):
            col = [x[i] for x in lists if len(x) > i]
            med.append(statistics.median(col))
        out[key] = med
    return out


def build_matrix() -> Tuple[Dict[str, Any], List[str]]:
    """7 curated arms × 4 cg from `results/d2_rerun_kvmhost` (2026-11-14 rerun)."""
    plot_arms = list(NATIVE_CURATED) + list(IOX_CURATED)

    none_p99_ref: Dict[int, Optional[float]] = {}
    equal_share_jain: Dict[int, float] = {cg: jain_equal_byte_share_ref(cg) for cg in CGS}
    for cg in CGS:
        _, p99, _ = median_across_iters(native_iter, "none", cg)
        none_p99_ref[cg] = p99

    rows: List[Dict[str, Any]] = []
    has_tenant: List[str] = []

    for arm in plot_arms:
        for cg in CGS:
            tenant, p99, jw = median_across_iters(native_iter, arm, cg)
            juw = equal_share_jain.get(cg) if arm == "none" else None
            source = "d2_rerun_kvmhost" if tenant else "missing"

            if tenant and arm not in has_tenant:
                has_tenant.append(arm)

            p99_rel = None
            ref = none_p99_ref.get(cg)
            if p99 and ref and ref > 0:
                p99_rel = (p99 / ref - 1.0) * 100.0

            rows.append({
                "arm": arm,
                "label": short_arm(arm),
                "cg": cg,
                "jainw_windexed": jw,
                "jain_unweighted": juw,
                "tenant_bw_mb_s": ";".join(f"{x:.2f}" for x in tenant),
                "n_tenants": len(tenant),
                "agg_bw_mb_s": sum(tenant) if tenant else None,
                "p99_us_mean": p99,
                "p99_rel_none_pct": p99_rel,
                "tenant_source": source,
            })

    meta = {
        "formal_arms_n": len(plot_arms),
        "tenant_bw_arms_n": len(has_tenant),
        "missing_tenant_native": [a for a in NATIVE_CURATED if a not in has_tenant],
    }
    return meta, rows


def validate_against_rerun_csv(rows: List[Dict[str, Any]]) -> List[str]:
    if not RERUN_CSV.is_file():
        return []
    ref_jain: Dict[Tuple[str, int], float] = {}
    with open(RERUN_CSV) as f:
        for r in csv.DictReader(f):
            arm, cg = r["arm"], int(r["cg"])
            j = r.get("jain_healthy_med") or r.get("jainw_med")
            if j:
                ref_jain[(arm, cg)] = float(j)
    lines: List[str] = []
    plot_arms_val = list(NATIVE_CURATED) + list(IOX_CURATED)
    for arm in plot_arms_val:
        for cg in CGS:
            row = next((x for x in rows if x["arm"] == arm and x["cg"] == cg), None)
            if not row or row.get("jainw_windexed") is None:
                continue
            ref = ref_jain.get((arm, cg))
            if ref is None:
                continue
            got = float(row["jainw_windexed"])
            d = got - ref
            if abs(d) > 0.05:
                lines.append(f"  {arm} cg{cg}: plot={got:.3f} csv={ref:.3f} delta={d:+.3f}")
    return lines


def write_data(rows: List[Dict[str, Any]], meta: Dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / "kvmhost_d2_by_cg.csv"
    keys = list(rows[0].keys()) if rows else []
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    note = DATA / "kvmhost_d2_arm_coverage.txt"
    note.write_text(
        f"formal arms: {meta['formal_arms_n']}\n"
        f"with per-tenant BW: {meta['tenant_bw_arms_n']}\n"
        f"native missing raw (no tar on disk): {', '.join(meta['missing_tenant_native'])}\n"
        f"iox: same rerun tree as native (contention step)\n"
        f"Jain: w-indexed from median per-tenant BW; crosscheck {RERUN_CSV.name}\n",
        encoding="utf-8",
    )


# w1–w4 stack colors: match dynamic_tenant COLORS a→d (plot_kvmhost_timeseries_combo.py).
STACK_COLORS = ("#a83b86", "#218741", "#bc8b0a", "#1f6fb8")
# Distinct from w4 stack blue (#1f6fb8).
COLOR_JAIN = "#0d7680"
COLOR_P99 = "#333333"
FONT_PT = 7.5
# p99 right axis: in-view band tops here; tail draw-through + label above fold.
P99_VIEW_TOP_MS = 60.0


def weight_colors(n: int = 16) -> List[Any]:
    """Weight segment colors; first 4 use STACK_COLORS, rest muted extensions."""
    extra = ["#76b7b2", "#edc948", "#bab0ac", "#9c755f", "#af7aa1", "#ff9da7",
             "#8cd17d", "#b6992d", "#499894", "#d37295", "#79706e", "#a0a0a0"]
    base = list(STACK_COLORS) + extra
    return base[:n]


def _apply_paper_style() -> None:
    import matplotlib as mpl

    mpl.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["DejaVu Sans", "Arial", "Helvetica"],
        "font.size": FONT_PT,
        "axes.labelsize": FONT_PT,
        "xtick.labelsize": FONT_PT,
        "ytick.labelsize": FONT_PT,
        "legend.fontsize": FONT_PT,
        "axes.linewidth": 0.8,
    })


def _style_axis_grid(ax) -> None:
    ax.grid(axis="y", linestyle="--", alpha=0.3, linewidth=0.6, zorder=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def pick_iox_top3(rows: List[Dict[str, Any]]) -> List[str]:
    """Best 3 of IOX_QUARTET: high mean Jain, stable across cg, drop explosive p99."""
    scored: List[Tuple[float, str]] = []
    for arm in IOX_QUARTET:
        rs = [r for r in rows if r["arm"] == arm and r.get("jainw_windexed")]
        if len(rs) < len(CGS):
            continue
        js = [float(r["jainw_windexed"]) for r in rs]
        p99s = [float(r["p99_us_mean"]) for r in rs if r.get("p99_us_mean")]
        if not p99s or max(p99s) > 2_000_000:
            continue
        score = statistics.mean(js) - 0.35 * (statistics.pstdev(js) if len(js) > 1 else 0)
        scored.append((score, arm))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [a for _, a in scored[:3]]


def curated_arms(rows: List[Dict[str, Any]]) -> List[str]:
    want = list(NATIVE_CURATED) + list(IOX_CURATED)
    have_tenant = {r["arm"] for r in rows if r.get("tenant_bw_mb_s")}
    jain_ok = {r["arm"] for r in rows if r.get("jainw_windexed") is not None}
    order = [a for a in want if a in have_tenant or a in jain_ok]
    if "none" in order:
        order.remove("none")
        order = ["none"] + order
    return order


def _p99_axis_limits(p99_ms: List[float]) -> Tuple[float, float]:
    """Y top for p99 (ms). Scale from healthy-run band; clip only gross outliers (>80ms)."""
    vals = [v for v in p99_ms if v == v and v > 0]
    if not vals:
        return 50.0, 50.0
    in_band = [v for v in vals if v <= 80.0]
    if len(in_band) < 2:
        in_band = vals
    p99_hi = max(max(in_band) * 1.12, 16.0)
    return p99_hi, p99_hi


def _bw_yticks(ymax: float) -> List[float]:
    top = max(400.0, round(ymax / 100.0) * 100.0)
    if top <= 500:
        return [0.0, 200.0, 400.0]
    return [0.0, 400.0, top]


def _merged_left_ylabel(
    ax, xc: float = -0.155, yc: float = 0.5, *, font_pt: float = FONT_PT,
) -> None:
    """Single rotated left title on shared spine: blue JainW + black Bandwidth."""
    kw = dict(transform=ax.transAxes, rotation=90, va="center", fontsize=font_pt, clip_on=False)
    ax.text(xc, yc, "JainW", ha="right", color=COLOR_JAIN, zorder=20, **kw)
    ax.text(xc, yc, "/ Bandwidth (MB/s)", ha="left", color="0.12", zorder=20, **kw)


def _legend_text_colors(leg) -> None:
    for txt in leg.get_texts():
        if txt.get_text() == "JainW":
            txt.set_color(COLOR_JAIN)


def _weight_legend_label(weight_index: int) -> str:
    """w_k with cgroup io.weight hint: w1(100), w2(200), …"""
    return f"w{weight_index}({weight_index * 100})"


def sync_twin_axes_position(ax) -> None:
    """After set_position on host axes, twinx siblings keep the old bbox — align them."""
    pos = ax.get_position()
    for twin in ax.get_shared_x_axes().get_siblings(ax):
        twin.set_position(pos)


def draw_bw_cg_panel(
    ax,
    rows: List[Dict[str, Any]],
    arms_plot: List[str],
    *,
    cg: int = PLOT_CGS[0],
    font_pt: float = FONT_PT,
    ylabel_xc: float = -0.155,
    x_tick_rotation: float = 48,
) -> tuple[list, list]:
    """Stacked BW + JainW (left) + p99 (right); returns legend handles for fig-level placement."""
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    lookup = {(r["arm"], int(r["cg"])): r for r in rows}
    wcolors = weight_colors(16)
    x = np.arange(len(arms_plot))
    cols = wcolors[:cg]

    ymax = 0.0
    for xi, arm in enumerate(arms_plot):
        r = lookup.get((arm, cg))
        if not r or not r.get("tenant_bw_mb_s"):
            continue
        parts = [float(v) for v in r["tenant_bw_mb_s"].split(";") if v]
        parts = (parts + [0.0] * cg)[:cg]
        bottom = 0.0
        for ti, bw in enumerate(parts):
            if bw <= 0:
                continue
            ax.bar(
                xi, bw, bottom=bottom, width=0.7, color=cols[ti],
                alpha=0.9, edgecolor="white", linewidth=0.2, zorder=2,
            )
            bottom += bw
        ymax = max(ymax, bottom)

    jain_y: List[float] = []
    p99_y: List[float] = []
    for arm in arms_plot:
        r = lookup.get((arm, cg))
        jain_y.append(float(r["jainw_windexed"]) if r and r.get("jainw_windexed") else np.nan)
        pv = r.get("p99_us_mean") if r else None
        p99_y.append(float(pv) / 1000.0 if pv is not None else np.nan)

    p99_fold = P99_VIEW_TOP_MS
    bw_top = max(ymax * 1.06, 100)

    ax.set_ylim(0, bw_top)
    ax.set_yticks(_bw_yticks(ymax))
    ax.tick_params(axis="y", labelsize=font_pt, colors="0.12", pad=2)
    ax.set_xlim(-0.6, len(arms_plot) - 0.4)
    ax.set_ylabel("")

    ax_j = ax.twinx()
    ax_j.spines["right"].set_visible(False)
    ax_j.spines["left"].set_visible(False)
    ax_j.yaxis.set_ticks_position("left")
    ax_j.yaxis.set_label_position("left")
    ax_j.set_ylim(0, 1.02)
    ax_j.set_yticks([0.0, 0.2, 0.8])
    ax_j.tick_params(axis="y", labelsize=font_pt, colors=COLOR_JAIN, pad=2)
    ax_j.plot(x, jain_y, color=COLOR_JAIN, lw=1.4, marker="s", ms=4, zorder=15, clip_on=True)
    ax_j.axhline(
        jain_equal_byte_share_ref(cg), color="#d62728", lw=1.0, alpha=0.95,
        linestyle="-", zorder=4, clip_on=True,
    )
    ax_j.set_ylabel("")

    ax_p = ax.twinx()
    ax_p.spines["left"].set_visible(False)
    ax_p.spines["right"].set_linewidth(0.6)
    ax_p.spines["right"].set_color(COLOR_P99)
    ax_p.set_ylim(0, p99_fold)
    ax_p.set_yticks([0.0, 20.0, 40.0, p99_fold])
    ax_p.set_ylabel("p99 (ms)", fontsize=font_pt, color=COLOR_P99, labelpad=3)
    ax_p.tick_params(axis="y", labelsize=font_pt, colors=COLOR_P99, pad=1)

    p99_markers: list[tuple[float, bool]] = []
    p99_draw: list[float] = []
    for v in p99_y:
        if v != v:
            p99_draw.append(np.nan)
            p99_markers.append((np.nan, False))
        elif v <= p99_fold:
            p99_draw.append(v)
            p99_markers.append((v, False))
        else:
            p99_draw.append(p99_fold)
            p99_markers.append((p99_fold, True))

    p99_line = list(p99_draw)
    for xi in range(1, len(p99_y)):
        a, b = p99_y[xi - 1], p99_y[xi]
        if a == a and b == b and a > p99_fold and b > p99_fold:
            p99_line[xi - 1] = np.nan
            p99_line[xi] = np.nan

    ax_p.plot(
        x, p99_line, color=COLOR_P99, lw=1.0,
        linestyle="--", dashes=(4, 2), zorder=14, clip_on=True,
    )
    for xi, (yv, tail) in enumerate(p99_markers):
        if yv != yv:
            continue
        if tail:
            ax_p.plot(
                xi, yv, marker="o", ms=4.5, mfc="white", mec=COLOR_P99,
                mew=0.9, zorder=16, clip_on=True,
            )
            ax_p.annotate(
                f"{p99_y[xi]:.0f}",
                xy=(xi, yv),
                xytext=(0, 2.5),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=font_pt,
                color=COLOR_P99,
                clip_on=True,
                zorder=17,
            )
        else:
            ax_p.plot(
                xi, yv, marker="o", ms=4, mfc=COLOR_P99, mec=COLOR_P99,
                zorder=15, clip_on=True,
            )

    ax.set_zorder(1)
    ax_j.set_zorder(3)
    ax_p.set_zorder(5)
    ax.patch.set_visible(False)
    ax_j.patch.set_visible(False)
    ax_p.patch.set_visible(False)

    ax.set_xticks(x)
    ax.set_xticklabels(
        [short_arm(a) for a in arms_plot], rotation=x_tick_rotation, ha="right", fontsize=font_pt,
    )
    _style_axis_grid(ax)
    h_bw = [
        Patch(facecolor=cols[i], label=_weight_legend_label(i + 1)) for i in range(cg)
    ]
    h_meta = [
        Line2D([0], [0], color=COLOR_JAIN, marker="s", lw=1, label="JainW"),
        Line2D([0], [0], color="#d62728", lw=1, label="equal-share ref"),
        Line2D([0], [0], color=COLOR_P99, marker="o", ls="--", lw=1, label="p99 (ms)"),
    ]
    _merged_left_ylabel(ax, xc=ylabel_xc, font_pt=font_pt)
    sync_twin_axes_position(ax)
    return h_bw, h_meta


def read_rows_csv(path: Path = DATA / "kvmhost_d2_by_cg.csv") -> List[Dict[str, Any]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def plot_combined(rows: List[Dict[str, Any]], arms_plot: List[str]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    _apply_paper_style()
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(4.35, 4.25))
    h_bw, h_meta = draw_bw_cg_panel(ax, rows, arms_plot)
    leg_kw = dict(
        fontsize=FONT_PT,
        frameon=False,
        columnspacing=0.45,
        handlelength=0.75,
        handletextpad=0.35,
        borderaxespad=0.0,
        labelspacing=0.15,
    )
    leg_meta = fig.legend(
        handles=h_meta,
        loc="lower center",
        bbox_to_anchor=(0.52, 0.708),
        ncol=3,
        **leg_kw,
    )
    fig.add_artist(leg_meta)
    leg_bw = fig.legend(
        handles=h_bw,
        loc="lower center",
        bbox_to_anchor=(0.52, 0.768),
        ncol=PLOT_CGS[0],
        **leg_kw,
    )
    _legend_text_colors(leg_meta)
    fig.subplots_adjust(left=0.20, right=0.84, top=0.67, bottom=0.24)
    for ext in ("png", "pdf"):
        fig.savefig(
            OUT / f"kvmhost_d2_bw_by_cg.{ext}",
            dpi=200,
            bbox_inches="tight",
            pad_inches=0.08,
        )
    plt.close(fig)


def plot_twopanel(rows: List[Dict[str, Any]], arms_plot: List[str]) -> None:
    """Paper layout: top Jain+p99 (clean); bottom stacked throughput; shared X."""
    import matplotlib

    matplotlib.use("Agg")
    _apply_paper_style()
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    lookup = {(r["arm"], int(r["cg"])): r for r in rows}
    cg = PLOT_CGS[0]
    cols = list(STACK_COLORS[:cg])
    x = np.arange(len(arms_plot))

    jain_y, p99_y = [], []
    ymax = 0.0
    for arm in arms_plot:
        r = lookup.get((arm, cg))
        jain_y.append(float(r["jainw_windexed"]) if r and r.get("jainw_windexed") else np.nan)
        pv = r.get("p99_us_mean") if r else None
        p99_y.append(float(pv) / 1000.0 if pv is not None else np.nan)

    _, p99_hi = _p99_axis_limits(p99_y)
    p99_clip = 80.0
    jain_ref = jain_equal_byte_share_ref(cg)

    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(4.0, 4.0), sharex=True,
        gridspec_kw={"height_ratios": [1.0, 1.28], "hspace": 0.10},
    )

    # --- top: JainW + p99 (no bar clutter) ---
    ax_p = ax_top.twinx()
    _style_axis_grid(ax_top)
    ax_p.spines["top"].set_visible(False)

    line_j = ax_top.plot(
        x, jain_y, color=COLOR_JAIN, marker="s", ms=4, lw=1.35,
        label="JainW", zorder=4, clip_on=True,
    )
    line_ref = ax_top.axhline(
        jain_ref, color=COLOR_JAIN, ls=":", lw=1.0, alpha=0.85,
        label="equal-share ref", zorder=2,
    )

    p99_draw = [
        min(v, p99_hi) if v == v and v <= p99_clip else (p99_hi * 0.98 if v == v else np.nan)
        for v in p99_y
    ]
    line_p = ax_p.plot(
        x, p99_draw, color=COLOR_P99, marker="o", ms=4, lw=1.2,
        ls="--", dashes=(4, 2), label="p99 (ms)", zorder=5, clip_on=True,
    )
    for xi, raw in enumerate(p99_y):
        if raw == raw and raw > p99_clip:
            ax_p.plot(
                xi, p99_hi * 0.98, marker="o", ms=5, mfc="white",
                mec=COLOR_P99, mew=1.2, zorder=6, clip_on=True,
            )
            ax_p.annotate(
                f"{raw:.0f} ms",
                xy=(xi, p99_hi * 0.98),
                xytext=(xi - 0.15, p99_hi * 0.72),
                fontsize=7.5, color=COLOR_P99, ha="center",
                arrowprops=dict(arrowstyle="-", color=COLOR_P99, lw=0.6),
            )

    ax_top.set_ylabel("JainW", color=COLOR_JAIN)
    ax_top.tick_params(axis="y", colors=COLOR_JAIN)
    ax_top.set_ylim(0.62, 1.03)
    ax_top.set_yticks([0.65, 0.75, 0.85, 0.95, 1.0])

    ax_p.set_ylabel("P99 latency (ms)", color=COLOR_P99)
    ax_p.tick_params(axis="y", colors=COLOR_P99)
    ax_p.set_ylim(0, p99_hi)
    ax_p.spines["right"].set_linewidth(0.8)

    ax_top.text(0.02, 0.97, "(a) Fairness + tail latency", transform=ax_top.transAxes,
                fontsize=8.5, va="top", fontweight="medium")
    h_top = (
        [Line2D([0], [0], color=COLOR_JAIN, marker="s", lw=1.2, label="JainW")]
        + [Line2D([0], [0], color=COLOR_JAIN, ls=":", lw=1, label="equal-share ref")]
        + [Line2D([0], [0], color=COLOR_P99, marker="o", ls="--", lw=1, label="p99 (ms)")]
    )
    ax_top.legend(handles=h_top, loc="lower left", ncol=3, frameon=False,
                  handlelength=1.2, columnspacing=0.7, handletextpad=0.35)
    plt.setp(ax_top.get_xticklabels(), visible=False)

    # --- bottom: stacked throughput ---
    width = 0.58
    stacks = np.zeros((cg, len(arms_plot)))
    for xi, arm in enumerate(arms_plot):
        r = lookup.get((arm, cg))
        if not r or not r.get("tenant_bw_mb_s"):
            continue
        parts = [float(v) for v in r["tenant_bw_mb_s"].split(";") if v]
        parts = (parts + [0.0] * cg)[:cg]
        for ti in range(cg):
            stacks[ti, xi] = parts[ti]
        ymax = max(ymax, sum(parts))

    bottom = np.zeros(len(arms_plot))
    for ti in range(cg):
        ax_bot.bar(
            x, stacks[ti], width, bottom=bottom, label=f"w{ti + 1}",
            color=cols[ti], edgecolor="white", linewidth=0.45, zorder=2,
        )
        bottom += stacks[ti]

    _style_axis_grid(ax_bot)
    ax_bot.set_ylabel("Throughput (MB/s)")
    ax_bot.set_ylim(0, max(ymax * 1.06, 100))
    ax_bot.set_xticks(x)
    ax_bot.set_xticklabels([short_arm(a) for a in arms_plot], rotation=0, ha="center")
    ax_bot.text(0.02, 0.97, "(b) Per-workload throughput", transform=ax_bot.transAxes,
                fontsize=8.5, va="top", fontweight="medium")
    h_bw = [Patch(facecolor=cols[i], label=f"w{i + 1}") for i in range(cg)]
    ax_bot.legend(handles=h_bw, loc="upper right", ncol=4, frameon=False,
                  handlelength=0.9, columnspacing=0.55)

    fig.subplots_adjust(left=0.14, right=0.86, top=0.98, bottom=0.13)
    for ext in ("png", "pdf"):
        fig.savefig(OUT / f"kvmhost_d2_cg4_twopanel.{ext}", dpi=200)
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    meta, rows = build_matrix()
    write_data(rows, meta)
    drift = validate_against_rerun_csv(rows)
    if drift:
        ck = DATA / "kvmhost_jain_crosscheck.txt"
        ck.write_text(
            "Large delta vs d2_rerun_by_arm.csv (>|0.05|):\n"
            + "\n".join(drift)
            + "\n",
            encoding="utf-8",
        )
        print(f"Jain crosscheck: {len(drift)} rows differ (see {ck.name})")

    arms_plot = curated_arms(rows)
    plot_combined(rows, arms_plot)
    plot_twopanel(rows, arms_plot)

    print(f"plotted arms: {arms_plot}")
    print(f"iox arms: {IOX_CURATED}")
    print(f"missing native tar: {meta['missing_tenant_native']}")
    print(f"wrote {OUT}/kvmhost_d2_bw_by_cg.png")
    print(f"wrote {OUT}/kvmhost_d2_cg4_twopanel.png")


if __name__ == "__main__":
    main()
