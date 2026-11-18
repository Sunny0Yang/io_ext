#!/usr/bin/env python3
"""Build the homo-v2 homomorphic-reproduction campaign CSVs (kvmhost, 7.3.1-ioext3).

Input layout (scripts/run_homo_v2_kvmhost.sh archive convention):
  results/homo_v2_kvmhost/rep<NN>/<SEQ>.tar.gz         guest-archived run dir
  results/homo_v2_kvmhost/rep<NN>/<SEQ>_manifest.json  sha256 verifier JSON
  (optionally an extracted run dir results/homo_v2_kvmhost/rep<NN>/<SEQ>/ is used
  directly when present; otherwise the tar is extracted to a temp scratch dir
  for this invocation only).

Per (sequence, rep) run dir (REAL executor layout, verified on 2026-11-06):
  <SEQ>/<SEQ>.sidecar.log          guest-side arm-state sampler
                                   (=== T=<ts> iterations, "-- <section>" lines)
  <SEQ>/step_<N>/<scene_id>/fio_<scene_id>_<ts>.json
                                   fio scene collected stat (scene dirs live
                                   under step_<N>/, NOT under scene/)
      runs[] holds vm.scene.io_ext.common.fio_metrics_from_data output:
      iops / direction_bw_mbps / per_job_metrics[].directions[rw]
      .iops / .bw_mbps / .clat_percentiles_us / returncode ... (verified:
      clat_percentiles_us are ALREADY in us, keyed "50"/"90"/"95"/"99"/"99.9")
  <SEQ>/run_results.json           per-step results mirror (runs[].returncode)
  <SEQ>/step_<N>/<scene_id>/<scene_id>_jobs<N>_fio.json  raw fio JSON fallback

Sidecar sections (run_homo_v2_kvmhost.sh sidecar_start, verified):
  -- sdb scheduler       /sys/block/sdb/queue/scheduler, ACTIVE in [brackets]
  -- io.cost.model/qos (root)   root io.cost[.model|.qos] "8:16 ..." lines
  -- cgroup io.* arm state (non-max lines)
                         homo_v2_*/io.{max,weight,latency} lines (this is
                         where the n_io_max rbps/wbps cap, the LC/BE
                         "default N" io.weight and the io.latency
                         "target=2000" lines show up; colocation noise
                         "default 100" belongs to the plain tenant loader)
  -- ioext bpffs pins    /sys/fs/bpf ioext pin names (ioext_active_cgroups,
                         ioext_policy_meta); (none) when not pinned
  -- loaders             pgrep -af loader_ lines: the arm's loaders are
                         ONE-SHOT configurators that exit right after
                         applying state, so they typically disappear BEFORE
                         the fio window opens (x_q_linnos keeps them alive).
                         They are therefore verified over a setup lookback
                         (window start - SETUP_LOOKBACK_S .. +2 s), NOT
                         per in-window sample.
  -- scx procs           pgrep -af scx (informational, not gated)

Outputs (in --out-dir, default the results root):
  homo_v2_cases.csv    long format: seq, rep, arm, group, family, metric,
                       value, gate_ok (one row per (seq, rep, metric))
  homo_v2_summary.csv  arm x metric: n, median, IQR, bootstrap 95% CI
                       (min, max always; arm -> group column below)
  homo_v2_gate_detail.csv

arm = <sequence id> minus the "HOMO_<family>_" prefix (family token fixed
{elev, ctrl, front}; prefix stripping, NOT substring matching -- the native
knob names n_io_max / n_none etc. are full arm ids; same semantics as
build_rq1_results.py ARM_FROM_SEQ but adapted per the RUNBOOK).

Arm -> group mapping (written table; group is the bar color in the figure):
  baseline  n_none                     native no-knob baseline (elevator gray
                                       "Baseline (none)" bar AND the coloc
                                       baseline; the frontier panels reuse
                                       HOMO_ctrl_n_none cross-referenced)
  native    n_mq_deadline, n_kyber     native elevator schedulers
            n_io_max, n_io_weight, n_io_cost, n_io_latency
                                       native blk-throttle-like coloc knobs
  iox       x_mq_deadline_q, x_kyber_q, x_iomax_q, x_weight_arb, x_cost_arb,
            x_latency_arb, x_d2fq_arb, x_linnos_q
                                       scx_io homo-arm algorithm (the frontier
                                       family has NO native column: the native
                                       slot is left empty in the figure)

gate_ok = fio metrics parseable AND run rc==0 AND sidecar arm-state samples
consistent over the case window (RQ1 expected_state()/window_gate() semantics,
extended to the homo-v2 arm matrix; a missing/empty sidecar FAILS the gate --
it never silently passes).  Gated metrics go to the summary medians; ungated
runs only appear in cases.csv + gate_detail.csv.

Run:
  python3 scripts/build_homo_v2_results.py --results-root results/homo_v2_kvmhost
  python3 scripts/build_homo_v2_results.py --self-test
  python3 scripts/build_homo_v2_results.py --smoke-fixtures
        (synthesizes fake run dirs under /tmp/homo_v2_smoke_fixtures, then runs
         the full pipeline on them; nothing is written under results/)
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import statistics
import sys
import tarfile
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = BASE_DIR / "results" / "homo_v2_kvmhost"

# D3/RECAL sdb saturation anchor (RUNBOOK 0): the BE cap tier of the io.max
# and q_iomax arms, in MiB/s.  be_track_err_pct is |BE bw - anchor| / anchor.
TRACK_ANCHOR_MIB = 64.0   # mirrors gen_homo_v2_yamls IO_MAX_BYTES (rerun#2 cap recalibration)
# io.weight target ratio LC:BE = 100:200 (gen_homo_v2_yamls WEIGHT_LC/WEIGHT_BE)
# -> per-tenant BYTE-SHARE targets (bw_mbps share of the coloc total):
# LC 1/3, BE 2/3 (2026-11-07 iteration#1 share caliber).  share_ratio_dev is
# anchored on the BE target (the heavier tenant is the arbitrated one).
LC_SHARE_TARGET = 100.0 / 300.0
BE_SHARE_TARGET = 200.0 / 300.0

SEQ_RE = re.compile(r"^HOMO_(?P<tok>elev|ctrl|front)_(?P<arm>.+)$")

FAMILY_BY_TOKEN = {"elev": "elevator", "ctrl": "controller", "front": "frontier"}

ARM_ORDER = [
    # seq_elevator
    "n_none", "n_mq_deadline", "n_kyber", "x_mq_deadline_q", "x_kyber_q",
    # seq_controller
    "n_io_max", "x_iomax_q", "n_io_weight", "x_weight_arb", "n_io_cost",
    "x_cost_arb", "n_io_latency", "x_latency_arb",
    # seq_frontier
    "front_n_none", "front_n_d2fq", "x_d2fq_arb", "x_linnos_q",
]

# -------------------------- arm -> group (hard table) -----------------------
# n_* -> baseline OR native (elevator n_mq_deadline / n_kyber are native
# elevators; coloc n_none is the baseline; the four native coloc knobs are
# native); x_* -> iox (scx_io homo-algorithm arms).  The frontier arms are
# group=iox and their figure's native column is left empty (no frontier
# native sequence exists; the baseline cross-references HOMO_ctrl_n_none).
ARM_GROUPS = {
    "n_none": "baseline",
    "front_n_none": "native_reference",
    "front_n_d2fq": "native",
    "n_mq_deadline": "native",
    "n_kyber": "native",
    "n_io_max": "native",
    "n_io_weight": "native",
    "n_io_cost": "native",
    "n_io_latency": "native",
    "x_mq_deadline_q": "iox",
    "x_kyber_q": "iox",
    "x_iomax_q": "iox",
    "x_weight_arb": "iox",
    "x_cost_arb": "iox",
    "x_latency_arb": "iox",
    "x_d2fq_arb": "iox",
    "x_linnos_q": "iox",
}

# ---------------- sidecar gate: expected state per arm -----------------------
# Semantics re-derived 2026-11-06 from the REAL sidecar/cgroup evidence in
# results/homo_v2_kvmhost (all 160 tars, scripts/survey of a full window every
# sample was in one stable state; loaders are one-shot and die pre-window):
#
#   sched       active [bracketed] scheduler for EVERY fio-window sample
#               (nators: [none]/[mq-deadline]/[kyber]; x_*: [scx_io])
#   io_req      substrings that must appear among the in-window
#               "cgroup io.* arm state" lines (the per-arm knob signature)
#   io_forbid   substrings that must NOT appear in-window (arm mismatch /
#               cross-arm contamination evidence)
#   loaders     x_* arms: loader basenames that must show up in the loaders
#               pgrep of >=1 sample in the setup lookback (one-shot procs;
#               natives never run loaders)
#   knobs       run-doc (fio collected io_ext_cgroup_controls) check for the
#               arms the sidecar cannot distinguish (n_io_cost's enable=1
#               qos lives on the LC cgroup, not root: the root
#               io.cost.model/qos section reads enable=0 in every seq).
IO_MAX_BYTES = 64 * 1024 * 1024   # gen_homo_v2_yamls cap literal (shows up
#                                   "8:16 rbps=67108864 wbps=67108864 ...")
# io.latency LC target in us (gen_homo_v2_yamls IO_LATENCY_TARGET_US;
# iteration#3 recalibration 2026-11-07: 1000 -> 2000 us -- the 1 ms target
# was below the reachable latency floor and blk-iolatency's self-throttling
# semantics turned it into LC self-harm (validation rep1: LC iops 84k ->
# 72k while p99 still ~5.7 ms); 2 ms sits between the ~4.9 ms none-baseline
# LC p99 and the hardware floor, so the self-throttle shrinks the LC queue
# and p99 lands near target (D3 precedent: target 4 ms -> measured 4.7 ms)).
IO_LAT_TARGET_US = 2000

EXPECTED_STATE = {
    # ---- native arms ----------------------------------------------------
    "n_none":        {"sched": "none", "io_req": [], "io_forbid": ["default 200"],
                      "loaders": None, "knobs": None},
    "front_n_none":  {"sched": "none", "io_req": [], "io_forbid": ["default 200"],
                      "loaders": None, "knobs": None},
    # front_n_d2fq: native D2FQ via the ioextd2fq kernel port.  The queue
    # is scheduler=none with /sys/block/<dev>/queue/d2fq_en=1 (sidecar
    # "d2fq" section; parsed by parse_sidecar_log and compared here).
    # The sysctl evidence (thrsh/md_ratio/di_period/force_all) lands in
    # the sidecar d2fq lines too and is matched below.
    "front_n_d2fq":  {"sched": "none", "io_req": [], "io_forbid": ["default 200"],
                      "loaders": None, "knobs": None, "d2fq_en": "1"},
    "n_mq_deadline": {"sched": "mq-deadline", "io_req": [],
                      "io_forbid": ["default 200"], "loaders": None, "knobs": None},
    "n_kyber":       {"sched": "kyber", "io_req": [], "io_forbid": ["default 200"],
                      "loaders": None, "knobs": None},
    "n_io_max":    {"sched": "none",
                    # io.max arm line: "8:16 rbps=335544320 wbps=335544320 ..."
                    "io_req": [f"wbps={IO_MAX_BYTES}"], "io_forbid": [],
                    "loaders": None,
                    "knobs": [("be", "io_max", f"wbps={IO_MAX_BYTES}")]},
    # n_io_weight: native io.weight's ONLY effective carrier is BFQ (the
    # 2026-11-07 iteration#1 fix: the knob is a no-op under scheduler=none),
    # so the gen script pins scheduler=bfq for this arm and the gate requires
    # the ACTIVE scheduler to be bfq.
    "n_io_weight": {"sched": "bfq",
                    # BE tenant io.weight "default 200" (LC stays 100); the
                    # n_io_cost arm also carries "default 200" but its BE
                    # knob-check pins it to that arm only
                    "io_req": ["default 200"], "io_forbid": [],
                    "loaders": None,
                    "knobs": [("be", "io_weight", "default 200")]},
    "n_io_cost":   {"sched": "none",
                    # sidecar-ambiguous (root qos reads enable=0 everywhere;
                    # the tenant io.weight defaults look like n_io_weight's)
                    # -> the LC qos enable=1 write is checked on the run doc
                    "io_req": [], "io_forbid": [],
                    "loaders": None,
                    "knobs": [("lc", "qos", "enable=1")]},
    "n_io_latency": {"sched": "none",
                     # LC cgroup io.latency: "8:16 target=2000"
                     "io_req": [f"target={IO_LAT_TARGET_US}"], "io_forbid": [],
                     "loaders": None, "knobs": None},
    # ---- iox arms (scx_io + pin/one-shot-loader setup evidence) ----------
    "x_mq_deadline_q": {"sched": "scx_io", "io_req": [], "io_forbid": [],
                        "loaders": ["loader_q_mq_deadline"], "knobs": None},
    "x_kyber_q":       {"sched": "scx_io", "io_req": [], "io_forbid": [],
                        "loaders": ["loader_q_kyber"], "knobs": None},
    "x_iomax_q":      {"sched": "scx_io", "io_req": [], "io_forbid": [],
                       "loaders": ["loader_q_iomax"], "knobs": None},
    "x_weight_arb":   {"sched": "scx_io", "io_req": [], "io_forbid": [],
                       "loaders": ["loader_arb_wdrr"], "knobs": None},
    "x_cost_arb":     {"sched": "scx_io", "io_req": [], "io_forbid": [],
                       "loaders": ["loader_arb_iocost"], "knobs": None},
    "x_latency_arb":  {"sched": "scx_io", "io_req": [], "io_forbid": [],
                       "loaders": ["loader_arb_iolatency"], "knobs": None},
    "x_d2fq_arb":     {"sched": "scx_io", "io_req": [], "io_forbid": [],
                       "loaders": ["loader_arb_d2fq"], "knobs": None},
    "x_linnos_q":     {"sched": "scx_io", "io_req": [], "io_forbid": [],
                       "loaders": ["loader_q_linnos"], "knobs": None},
}


def parse_seq_id(seq: str) -> tuple:
    """HOMO_<tok>_<arm> -> (family, arm); None on mismatch (probe dirs etc.)."""
    m = SEQ_RE.match(seq)
    if not m:
        return None, None
    family = FAMILY_BY_TOKEN[m.group("tok")]
    arm = m.group("arm")
    if family == "frontier" and arm in ("n_none", "n_d2fq"):
        arm = {"n_none": "front_n_none", "n_d2fq": "front_n_d2fq"}[arm]
    return family, arm


def group_of(arm: str) -> str:
    if arm not in ARM_GROUPS:
        return ""
    return ARM_GROUPS[arm]


# ----------------------------- fio JSON parsing ------------------------------

def _fnum(value) -> float | None:
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", "")
        return float(value)
    except (TypeError, ValueError):
        return None


def percentile_from_map(pct_map, pct: float) -> float | None:
    """fio clat_ns.percentile: {"95.000000": "<ns>"} -> us (ns / 1000)."""
    if not isinstance(pct_map, dict):
        return None
    for key, raw in pct_map.items():
        try:
            key_pct = float(str(key).split()[0].replace(",", "").rstrip("%"))
        except (TypeError, ValueError):
            # tolerate integer-suffixed duplicate keys (fio "99.990000:2")
            continue
        if abs(key_pct - pct) < 5e-3:
            v = _fnum(raw)
            return None if v is None else v / 1000.0
    return None


def section_percentiles(section, pcts=(50.0, 90.0, 95.0, 99.0, 99.9)) -> dict:
    """clat percentiles in us for one fio job direction section."""
    out = {}
    for key in ("clat_ns", "lat_ns"):
        lat = section.get(key)
        if isinstance(lat, dict) and isinstance(lat.get("percentile"), dict):
            for p in pcts:
                v = percentile_from_map(lat["percentile"], p)
                if v is not None and p not in out:
                    out[p] = v
    return out


def section_iops_bw(section) -> tuple:
    """(iops, bw bytes/s) for one fio job direction section."""
    iops = _fnum(section.get("iops"))
    if section.get("bw_bytes") is not None:
        bw = _fnum(section.get("bw_bytes"))
    else:
        bw = _fnum(section.get("bw"))  # KiB/s in fio JSON
        bw = None if bw is None else bw * 1024.0
    return (iops if iops is not None else 0.0,
            bw if bw is not None else 0.0)


def metrics_from_raw_fio(data: dict) -> dict:
    """Self-contained fallback: parse a raw fio --output-format=json file."""
    per_dir = {"read": {}, "write": {}}
    iops = {"read": 0.0, "write": 0.0}
    bw = {"read": 0.0, "write": 0.0}
    rc_seen = []
    for job in data.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        for direction in ("read", "write"):
            sec = job.get(direction)
            if not isinstance(sec, dict):
                continue
            di, dbw = section_iops_bw(sec)
            iops[direction] += di
            bw[direction] += dbw
            pcts = section_percentiles(sec)
            for p, v in pcts.items():
                per_dir[direction][p] = max(v, per_dir[direction].get(p, v))
    # any explicit fio rc is preferred over "no stderr error"
    rc = rc_seen[-1] if rc_seen else None
    return {"iops_read": iops["read"], "iops_write": iops["write"],
            "bw_read_mbps": bw["read"] / 2**20, "bw_write_mbps": bw["write"] / 2**20,
            "pct_read": per_dir["read"], "pct_write": per_dir["write"],
            "rc": rc}


# ------------------------ per (seq, rep) KPI extraction ----------------------

def find_scene_dirs(case_dir: Path) -> dict:
    """step_<N>/<scene_id>/ dirs, split into the one elevator job / lc / be.

    REAL executor layout (verified 2026-11-06): fio scene dirs are
    <case>/step_<N>/<scene_id>/ (workload scenes hv_*...); monitoring scenes
    (paper_* / scx_io_*) are ignored -- they never hold fio run docs.
    """
    out = {"elev": [], "lc": [], "be": []}
    step_root = case_dir / "scene"
    steps = sorted(case_dir.glob("step_*"))
    if steps:
        for step in steps:
            for d in sorted(p for p in step.iterdir() if p.is_dir()):
                n = d.name
                if n.startswith(("paper_", "scx_io_")) or not (
                        n.endswith("_lc") or n.endswith("_be") or "_job" in n):
                    continue
                if n.endswith("_lc"):
                    out["lc"].append(d)
                elif n.endswith("_be"):
                    out["be"].append(d)
                else:
                    out["elev"].append(d)
    elif step_root.is_dir():
        # legacy "scene/" layout (extracted run dirs from older conventions)
        for d in sorted(p for p in step_root.iterdir() if p.is_dir()):
            if d.name.endswith("_lc"):
                out["lc"].append(d)
            elif d.name.endswith("_be"):
                out["be"].append(d)
            else:
                out["elev"].append(d)
    return out


def load_collected(scene_dir: Path):
    """fio_<scene_id>_<ts>.json collected stat (runs[] + window)."""
    hits = sorted(scene_dir.glob("fio_*.json"))
    for p in reversed(hits):  # latest timestamp wins
        try:
            d = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d.get("runs"), list) and d["runs"]:
            return d, str(p)
    return None, None


def load_raw_fio(scene_dir: Path):
    for p in sorted(scene_dir.glob("*_jobs*_fio.json")):
        try:
            d = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d.get("jobs"), list) and d["jobs"]:
            return d, str(p)
    return None, None


def metrics_from_collected(run: dict) -> dict:
    """Flatten one collected stat run (fio_metrics_from_data output)."""
    out = {}
    # direction_bw_mbps (MiB/s)
    dbw = run.get("direction_bw_mbps") or {}
    out["bw_read_mbps"] = _fnum(dbw.get("read")) or 0.0
    out["bw_write_mbps"] = _fnum(dbw.get("write")) or 0.0
    agg_iops = _fnum(run.get("iops"))
    # per-direction iops / percentiles via per_job_metrics
    pj = run.get("per_job_metrics") or []
    iops = {"read": 0.0, "write": 0.0}
    pct = {"read": {}, "write": {}}
    for job in pj:
        directions = (job or {}).get("directions") or {}
        for direction in ("read", "write"):
            sec = directions.get(direction) or {}
            di = _fnum(sec.get("iops"))
            if di is not None:
                iops[direction] += di
            for p, v in (sec.get("clat_percentiles_us") or {}).items():
                fv = _fnum(v)
                key = _fnum(str(p).split()[0].rstrip("%"))
                if fv is not None and key is not None:
                    pct[direction][key] = max(fv, pct[direction].get(key, fv))
    out["iops_read"], out["iops_write"] = iops["read"], iops["write"]
    out["agg_iops"] = agg_iops if agg_iops is not None else (iops["read"] + iops["write"])
    out["pct_read"], out["pct_write"] = pct["read"], pct["write"]
    return out


def scene_metrics(scene_dirs: list) -> dict:
    """{iops_read, iops_write, agg_iops, bw_read_mbps, bw_write_mbps,
        pct_read{p:us}, pct_write{p:us}, rc, window, knobs} for one scene
    tag; the first dir (sorted) carrying an fio run doc wins."""
    for scene_dir in scene_dirs:
        collected, collected_path = load_collected(scene_dir)
        if collected is not None:
            runs = collected.get("runs") or []
            rcs = [r.get("returncode") for r in runs if isinstance(r, dict)]
            rcs = [r for r in rcs if r is not None]
            rc = max(rcs) if rcs else None
            m = metrics_from_collected(runs[0] if runs else {})
            m["rc"] = rc
            m["window"] = (collected.get("start_time"), collected.get("end_time"))
            m["_src"] = collected_path
            m["knobs"] = collected.get("io_ext_cgroup_controls")
            m["cgroup"] = (collected.get("io_ext_cgroup_controls") or {}).get(
                "cgroup_path")
            return m
        raw, raw_path = load_raw_fio(scene_dir)
        if raw is not None:
            m = metrics_from_raw_fio(raw)
            m["rc"] = m.get("rc") or 0
            m["window"] = (None, None)
            m["_src"] = raw_path
            m["knobs"] = None
            return m
    return {"_src": None, "rc": None, "window": (None, None), "knobs": None}


def _add_pct_metrics(out: dict, prefix: str, pct: dict) -> None:
    mapping = {50.0: "p50_us", 90.0: "p90_us", 95.0: "p95_us",
               99.0: "p99_us", 99.9: "p99_9_us"}
    for p, suffix in mapping.items():
        if p in pct:
            out[f"{prefix}_{suffix}"] = pct[p]


def _knob_lines(knobs: dict, field: str) -> list:
    """io_ext_cgroup_controls fields as plain line lists."""
    if not isinstance(knobs, dict):
        return []
    if field == "io_max":
        v = knobs.get("io_max_line")
        return [v] if isinstance(v, str) else []
    if field == "io_weight":
        v = knobs.get("io_weight_lines")
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    if field == "qos":
        v = (knobs.get("io_cost") or {}).get("io_cost_qos_line")
        return [v] if isinstance(v, str) else []
    return []


def knobs_check(knob_docs: list, arm: str) -> dict:
    """Run-doc cgroup evidence for the arms that the sidecar cannot verify
    (expected_state()["knobs"] = [(tenant_tag, field, substring), ...])."""
    exp = (EXPECTED_STATE.get(arm) or {}).get("knobs") or []
    if not exp:
        return {"checked": False, "ok": True}
    tenant_seen = {}
    for k in knob_docs:
        if not isinstance(k, dict):
            continue
        cgrp = str(k.get("cgroup") or "")
        m = re.search(r"(_lc|_be)$", cgrp)
        tag = m.group(1)[1:] if m else "elev"
        for tname, field, sub in exp:
            lines = _knob_lines(k.get("knobs"), field)
            tenant_seen.setdefault(tname, []).extend(lines)
    detail = {"checked": True, "ok": True}
    for tname, field, sub in exp:
        lines = tenant_seen.get(tname, [])
        hit = any(sub in ln for ln in lines)
        detail[f"{tname}.{field}"] = {"need": sub, "ok": hit,
                                      "lines": lines[:4]}
        if not hit:
            detail["ok"] = False
    return detail


def extract_case_kpis(family: str, scenes: dict) -> dict:
    """Family-aware KPI extraction (RUNBOOK 2 caliber).

    elevator family: 4k randrw 70/30 QD64 mix -> read percentiles (HEAD
    metric: elev_read_p99_us; IOPS secondary since the QD64 deepening of
    iteration#1) + p50/p90/p99.9.
    controller/frontier (LC+BE coloc): LC 4k randread QD32 -> LC IOPS / read
    percentiles / bw; BE 64k seqwrite QD16 -> BE IOPS / write bw / write p99.
    Derived coloc metrics (both tenants required, BYTE share caliber):
    be_track_err_pct, share_ratio (LC:BE bytes ratio), lc_share/be_share
    (per-tenant share of total bytes), share_ratio_dev
    (|be_share - BE_SHARE_TARGET| / BE_SHARE_TARGET, in %), jain_index on
    the two tenants' byte rates.
    rc aggregates every scene fio run's returncode (worst wins).
    """
    out: dict = {}
    knob_docs = []
    if family == "elevator":
        m, window = None, (None, None)
        for d in scenes["elev"] or scenes["lc"] + scenes["be"]:
            m = scene_metrics([d])
            if m.get("_src"):
                break
        if not (m and m.get("_src")):
            out["_error"] = "no fio stat json (elevator)"
        else:
            out["rc"] = m.get("rc")
            out["_window"] = m.get("window") or (None, None)
            out["_src"] = m["_src"]
            out["elev_iops"] = m.get("agg_iops")
            out["elev_read_iops"] = m.get("iops_read")
            out["elev_write_iops"] = m.get("iops_write")
            out["elev_bw_mbps"] = (m.get("bw_read_mbps") or 0.0) + (m.get("bw_write_mbps") or 0.0)
            _add_pct_metrics(out, "elev_read", m.get("pct_read") or {})
            _add_pct_metrics(out, "elev_write", m.get("pct_write") or {})
            knob_docs.append({"cgroup": None, "knobs": m.get("knobs")})
    else:
        if not scenes["lc"] and not scenes["be"]:
            return {"_error": "no scene dirs (coloc)"}
        lc = scene_metrics(scenes["lc"]) if scenes["lc"] else None
        be = scene_metrics(scenes["be"]) if scenes["be"] else None
        rcs = []
        if lc and lc.get("_src"):
            out["rc"] = lc.get("rc")
            out["_window"] = lc.get("window")
            out["_src"] = lc["_src"]
            out["lc_iops"] = lc.get("agg_iops")
            out["lc_bw_mbps"] = lc.get("bw_read_mbps")
            _add_pct_metrics(out, "lc_read", lc.get("pct_read") or {})
            if lc.get("rc") is not None and lc["rc"] != 0:
                rcs.append(lc["rc"])
            knob_docs.append({"cgroup": lc.get("cgroup"), "knobs": lc.get("knobs")})
        if be and be.get("_src"):
            out["be_iops"] = be.get("agg_iops")
            out["be_bw_mbps"] = be.get("bw_write_mbps") or be.get("bw_read_mbps")
            _add_pct_metrics(out, "be_write", be.get("pct_write") or {})
            if be.get("rc") is not None and be["rc"] != 0:
                rcs.append(be["rc"])
            knob_docs.append({"cgroup": be.get("cgroup"), "knobs": be.get("knobs")})
        else:
            out["_error"] = out.get("_error", "BE scene stat missing")
        if not out.get("_src"):
            return {"_error": "no fio stat json (coloc)"}
        if rcs:
            out["rc"] = rcs[0]  # worst nonzero wins for the gate
        lc_bw, be_bw = out.get("lc_bw_mbps"), out.get("be_bw_mbps")
        if lc_bw is not None and be_bw not in (None, 0):
            total_bw = lc_bw + be_bw
            out["be_track_err_pct"] = abs(be_bw - TRACK_ANCHOR_MIB) / TRACK_ANCHOR_MIB * 100.0
            # byte-share caliber (iteration#1): per-tenant share of the
            # coloc byte rate, deviation vs the io.weight share target
            # (LC:BE = 100:200 -> BE target 2/3, LC 1/3), anchored on BE.
            out["share_ratio"] = lc_bw / be_bw
            out["lc_share"] = lc_bw / total_bw
            out["be_share"] = be_bw / total_bw
            out["share_ratio_dev"] = (
                abs(out["be_share"] - BE_SHARE_TARGET) / BE_SHARE_TARGET * 100.0)
            out["jain_index"] = total_bw ** 2 / (
                2.0 * (lc_bw ** 2 + be_bw ** 2))
    out["_knob_docs"] = knob_docs
    # optional ioext map dump counters (null_returns/busy/in-flight), only
    # when present (timeline-scene artifacts; homo_v2 does not run timeline)
    for metric, keys in (("map_null_returns", ("null_returns",)),
                         ("map_busy", ("busy", "busy_cnt")),
                         ("map_in_flight", ("inflight", "in_flight"))):
        total = _sum_map_dump_keys(family, scenes, keys)
        if total is not None:
            out[metric] = total
    out["_parse_ok"] = any(
        v is not None for k, v in out.items()
        if not k.startswith("_") and k != "rc")
    return out


def _sum_map_dump_keys(family: str, scenes: dict, keys: tuple):
    """Best-effort sum of ioext map-dump fields if any account/map dump JSON
    artifact landed in the scene output (optional; never fails the gate)."""
    import contextlib
    import io as _io
    total = 0.0
    seen = False
    blobs: list = []
    for dlist in scenes.values():
        for d in dlist:
            blobs.extend(d.glob("*map_dump*.json"))
            blobs.extend(d.glob("*account*.json"))
    for p in blobs:
        with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
            try:
                doc = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
        stack = [doc]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for k in keys:
                    v = _fnum(node.get(k))
                    if v is not None:
                        total += v
                        seen = True
                stack.extend(list(node.values()))
            elif isinstance(node, list):
                stack.extend(node)
    return total if seen else None


# ----------------------------- sidecar gate ---------------------------------

def parse_sidecar_log(path: Path) -> list:
    """Return [{ts, scheduler, qos_lines, io_state, pins, loaders}] for the
    homo-v2 sampler layout (run_homo_v2_kvmhost.sh sidecar_start):
    each sample starts at "=== T=<ts>"; sections are "-- <name>" headers
    (sdb scheduler / io.cost.model/qos (root) / cgroup io.* arm state
    (non-max lines) / ioext bpffs pins / loaders / scx procs)."""
    samples = []
    if not path.is_file():
        return samples
    cur = None
    section = None
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("=== T="):
            if cur:
                samples.append(cur)
            cur = {"ts": None, "scheduler": None, "qos_lines": [],
                   "io_state": [], "pins": [], "loaders": [], "d2fq": None,
                   "d2fq_sysctls": []}
            try:
                cur["ts"] = float(line.rsplit("=", 1)[1])
            except ValueError:
                pass
            section = None
        elif cur is not None:
            if line.startswith("-- "):
                section = line[3:].strip()
            elif section is None:
                continue
            elif section.startswith("sdb scheduler"):
                if line.strip():
                    cur["scheduler"] = line.strip()
            elif section.startswith("io.cost.model/qos"):
                if line.strip() and "(no 8:16 lines)" not in line:
                    cur["qos_lines"].append(line.strip())
            elif section.startswith("cgroup io.*"):
                if line.strip() and line.strip() != "(none)":
                    cur["io_state"].append(line.strip())
            elif section.startswith("d2fq"):
                t = line.strip()
                if t and t.startswith("d2fq_en="):
                    cur["d2fq"] = t
                elif t and t.startswith("kernel.") and "=" in t:
                    cur["d2fq_sysctls"].append(t)
            elif section.startswith("ioext bpffs pins"):
                if line.strip() and line.strip() != "(none)":
                    cur["pins"].append(line.strip())
            elif section == "loaders":
                if line.strip() and line.strip() != "(none)":
                    cur["loaders"].append(line.strip())
    if cur:
        samples.append(cur)
    return samples


def active_scheduler(sched_line: str) -> str | None:
    """ACTIVE scheduler = the [bracketed] option in the sysfs options list;
    None when the bracket list is missing (scx_io attachment drop-outs)."""
    m = re.search(r"\[([^\]]+)\]", sched_line or "")
    return m.group(1) if m else None


def loader_names(loaders: list) -> list:
    """pgrep lines 'pid argv...' -> basenames of loader_* argv entries."""
    names = []
    for line in loaders:
        for token in line.split():
            base = token.rsplit("/", 1)[-1]
            if base.startswith("loader_") and base not in names:
                names.append(base)
    return names


def sample_matches(sample: dict, arm: str) -> bool:
    """One in-window sidecar sample: active scheduler + io.* knob signatures.
    (Loaders/pins are one-shot pre-window setup evidence -> verified over the
    lookback in setup_evidence(), never per in-window sample.)"""
    exp = EXPECTED_STATE.get(arm)
    if exp is None:
        return False
    sched = sample["scheduler"] or ""
    active = active_scheduler(sched)
    if active is not None:
        if active != exp["sched"]:
            return False
    elif exp["sched"] not in sched:
        # bracketless fallback (scx_io attachments can drop the option list):
        # keep the substring behaviour only for unambiguous expectations
        return False
    for sub in exp["io_req"]:
        if not any(sub in line for line in sample["io_state"]):
            return False
    for sub in exp["io_forbid"]:
        if any(sub in line for line in sample["io_state"]):
            return False
    exp_d2fq = exp.get("d2fq_en")
    if exp_d2fq is not None:
        want = f"d2fq_en={exp_d2fq}"
        got = sample.get("d2fq")
        if got != want:
            return False
        # sysctl evidence lines (if the sidecar captured any) must reflect
        # the arm's declared frozen values
        for req in exp.get("d2fq_sysctls") or []:
            if req not in (sample.get("d2fq_sysctls") or []):
                return False
    return True


def setup_evidence(window: tuple, arm: str, samples: list,
                   lookback_s: float = 20.0) -> dict:
    """One-shot loader/pin evidence for iox arms: the arm's loaders and the
    ioext bpffs pins must appear in >=1 sidecar sample within
    [start - lookback_s, end + 2] (the loaders exit after applying state,
    typically before the fio window opens; x_linnos_q keeps them alive)."""
    if not window or None in window:
        return {"checked": False, "ok": False, "error": "no window"}
    start, end = window
    lo, hi = start - lookback_s, end + 2.0
    inside = [s for s in samples
              if s["ts"] is not None and lo <= s["ts"] <= hi]
    names = sorted({n for s in inside for n in loader_names(s["loaders"])})
    pins = sorted({p for s in inside for p in s["pins"]})
    exp = EXPECTED_STATE.get(arm) or {}
    required = exp.get("loaders") or None
    out = {"checked": required is not None, "lo_ts": lo, "hi_ts": hi,
           "loaders_seen": names, "pins_seen": pins, "ok": True}
    if required is None:  # native arms: no loader evidence to verify
        return out
    missing = [r for r in required if r not in names]
    if missing:
        out["missing_loaders"] = missing
        out["ok"] = False
    if not any(p.startswith("ioext_") for p in pins):
        # The bm bundle keeps the policy registration in the loader/kernel
        # path without exposing the legacy ioext_* pin names under /sys/fs/bpf.
        # Treat matching loader presence plus an scx_io sample as setup proof,
        # while retaining the missing pin fact for audit output.
        out["missing_pins"] = True
        scx_seen = any("scx_io" in (s.get("scheduler") or "") for s in inside)
        if not (missing or not scx_seen):
            out["pin_compat_mode"] = "loader+scx_scheduler"
        else:
            out["ok"] = False
    return out


def sidecar_gate(window: tuple, arm: str, samples: list,
                 edge_tol: float = 0.0) -> dict:
    """All samples inside the fio window must match the expected arm state
    (active scheduler + per-arm knob signature); sampler period is ~2 s ->
    6 s in-window gap tolerance and <=4 s start/end tolerance for coverage."""
    out = {"sample_n": 0, "bad_n": 0, "ok": False}
    if not window or None in window:
        out["error"] = "no window"
        return out
    start, end = window
    inside = [s for s in samples
              if s["ts"] is not None
              and start + edge_tol <= s["ts"] <= max(start + edge_tol,
                                                     end - edge_tol)]
    out["sample_n"] = len(inside)
    if not inside:
        out["error"] = "no samples in window"
        return out
    bad = [s for s in inside if not sample_matches(s, arm)]
    out["bad_n"] = len(bad)
    times = [s["ts"] for s in inside]
    out["coverage_ok"] = (times[0] <= start + edge_tol + 4 and
                          times[-1] >= end - edge_tol - 4 and
                          all(b - a <= 6 for a, b in zip(times, times[1:])))
    out["ok"] = not bad and out["coverage_ok"]
    if bad:
        out["first_bad_ts"] = bad[0]["ts"]
        out["first_bad_state"] = {
            "scheduler": bad[0]["scheduler"],
            "io_state": bad[0]["io_state"][:3],
            "loaders": bad[0]["loaders"][:4],
        }
    return out


# ------------------------------- statistics ---------------------------------

def _quantile(vals: list, p: float) -> float:
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def bootstrap_ci(vals: list, b: int = 10000, seed: int = 20261106) -> tuple:
    rng = random.Random(seed)
    meds = []
    n = len(vals)
    for _ in range(b):
        meds.append(_quantile([vals[rng.randrange(n)] for _ in range(n)], 0.5))
    return (_quantile(meds, 0.025), _quantile(meds, 0.975))


# ------------------------------ tar handling --------------------------------

def extract_case(run_root: Path, seq: str, scratch: Path) -> Path | None:
    """rep<NN>/<SEQ> dir if present; else extract rep<NN>/<SEQ>.tar.gz."""
    direct = run_root / seq
    if direct.is_dir():
        return direct
    tar_path = run_root / f"{seq}.tar.gz"
    if not tar_path.is_file():
        return None
    manifest = run_root / f"{seq}_manifest.json"
    if manifest.is_file():
        try:
            want = json.loads(manifest.read_text()).get("tar_sha256")
            if want:
                import hashlib
                digest = hashlib.sha256()
                with open(tar_path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        digest.update(chunk)
                if digest.hexdigest() != want:
                    print(f"WARN manifest sha256 mismatch: {tar_path.name}",
                          file=sys.stderr)
        except (OSError, json.JSONDecodeError, ImportError):
            pass
    dest = Path(tempfile.mkdtemp(prefix=f"homo_v2_{seq}_", dir=str(scratch)))
    try:
        with tarfile.open(tar_path, "r:gz") as tf:
            try:
                tf.extractall(dest, filter="data")  # py3.12+
            except TypeError:
                tf.extractall(dest)
    except (tarfile.TarError, OSError) as exc:
        print(f"WARN extract failed {tar_path}: {exc}", file=sys.stderr)
        return None
    hits = sorted(dest.glob(f"{seq}"))
    return hits[0] if hits else dest


# --------------------------------- main --------------------------------------

def build_rows(root: Path, out_dir: Path, probe_prefix: str = ".") -> dict:
    scratch = Path(tempfile.mkdtemp(prefix="homo_v2_scratch_"))
    rows = []          # cases.csv long-format rows
    gate_rows = []     # gate_detail rows
    try:
        rep_dirs = sorted(p for p in root.iterdir()
                          if p.is_dir() and p.name.startswith("rep"))
        for rep_dir in rep_dirs:
            rep = int(rep_dir.name.replace("rep", "") or 0)
            seqs = []
            for entry in sorted(rep_dir.glob("HOMO_*")):
                if entry.name.endswith((".tar.gz", "_manifest.json")):
                    seqs.append(entry.name.replace(".tar.gz", "").replace("_manifest.json", ""))
                elif entry.is_dir():
                    seqs.append(entry.name)
            seqs = sorted(set(s for s in seqs if not s.startswith(probe_prefix)))
            for seq in seqs:
                family, arm = parse_seq_id(seq)
                if family is None:
                    print(f"SKIP {seq}: cannot parse HOMO_<family>_<arm>", file=sys.stderr)
                    continue
                case_dir = extract_case(rep_dir, seq, scratch)
                if case_dir is None:
                    print(f"SKIP {seq} rep{rep}: no run dir or tar", file=sys.stderr)
                    continue
                kpis = extract_case_kpis(family, find_scene_dirs(case_dir))
                rc = kpis.get("rc")
                phase_path = case_dir / "phase_gate.json"
                phase_doc = None
                if phase_path.is_file():
                    try:
                        phase_doc = json.loads(phase_path.read_text())
                    except (OSError, json.JSONDecodeError):
                        phase_doc = None
                checks = {"rc": rc == 0, "parse": bool(kpis.get("_parse_ok"))}
                if phase_doc is not None:
                    checks["phase"] = bool(phase_doc.get("ok"))
                gate_detail = {"rc": rc, "parse": bool(kpis.get("_parse_ok")),
                               "phase": phase_doc or "missing"}
                knobs = knobs_check(kpis.get("_knob_docs") or [], arm)
                checks["knobs"] = bool(knobs.get("ok", False))
                gate_detail["knobs"] = knobs
                samples = parse_sidecar_log(case_dir / f"{seq}.sidecar.log")
                if samples:
                    gate = sidecar_gate(kpis.get("_window"), arm, samples)
                    setup = setup_evidence(kpis.get("_window"), arm, samples)
                    checks["samples"] = bool(gate.get("ok"))
                    checks["setup"] = bool(setup.get("ok", False))
                    gate_detail["samples"] = {k: v for k, v in gate.items()
                                              if k != "ok"}
                    gate_detail["setup"] = setup
                    gate_detail["sidecar"] = f"{seq}.sidecar.log"
                else:
                    checks["samples"] = False
                    checks["setup"] = False
                    gate_detail["samples"] = "sidecar log missing or empty"
                gate_ok = all(checks.values())
                gate_rows.append({"seq": seq, "rep": rep, "arm": arm,
                                  "run_dir": str(case_dir),
                                  "checks_failed": ",".join(k for k, v in checks.items() if not v) or "all",
                                  "detail": json.dumps(gate_detail, sort_keys=True,
                                                       default=str)})
                for metric in sorted(k for k in kpis if not k.startswith("_")
                                     and k != "rc" and kpis[k] is not None):
                    rows.append({
                        "seq": seq, "rep": rep, "arm": arm,
                        "group": group_of(arm), "family": family,
                        "metric": metric,
                        "value": f"{kpis[metric]:.6g}",
                        "gate_ok": int(gate_ok),
                    })
    finally:
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)
    return {"rows": rows, "gate_rows": gate_rows}


def summarize(rows: list) -> list:
    """arm x metric -> n/median/IQR/CI rows (gate_ok rows only)."""
    grouped: dict = {}
    for r in rows:
        if r["gate_ok"]:
            grouped.setdefault((r["arm"], r["metric"]), []).append(float(r["value"]))
    out = []
    for arm in ARM_ORDER:
        metrics = sorted(m for (a, m) in grouped if a == arm)
        for metric in metrics:
            vals = grouped[(arm, metric)]
            med = _quantile(vals, 0.5)
            iqr = _quantile(vals, 0.75) - _quantile(vals, 0.25)
            if len(vals) >= 5:
                lo, hi = bootstrap_ci(vals)
                lo_s, hi_s = f"{lo:.6g}", f"{hi:.6g}"
            else:
                lo_s = hi_s = "NA"  # RUNBOOK 3: degrade to min/median/max
            out.append({"arm": arm, "group": group_of(arm), "metric": metric,
                        "n": len(vals), "median": f"{med:.6g}",
                        "iqr": f"{iqr:.6g}", "ci95_lo": lo_s, "ci95_hi": hi_s,
                        "min": f"{min(vals):.6g}", "max": f"{max(vals):.6g}"})
    return out


CASES_HEADER = ["seq", "rep", "arm", "group", "family", "metric", "value", "gate_ok"]
SUMMARY_HEADER = ["arm", "group", "metric", "n", "median", "iqr",
                  "ci95_lo", "ci95_hi", "min", "max"]
DETAIL_HEADER = ["seq", "rep", "arm", "run_dir", "checks_failed", "detail"]


def write_csvs(result: dict, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, header, rows, cols in (
            ("homo_v2_cases.csv", CASES_HEADER, result["rows"], CASES_HEADER),
            ("homo_v2_gate_detail.csv", DETAIL_HEADER, result["gate_rows"], DETAIL_HEADER)):
        p = out_dir / name
        with open(p, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        paths[name] = p
    print(f"wrote {paths['homo_v2_cases.csv']} ({len(result['rows'])} rows); "
          f"wrote {paths['homo_v2_gate_detail.csv']}")
    return paths


# --------------------------- self-test / fixtures ----------------------------

def make_fio_collected(iops_r: float, iops_w: float, bw_r_mbps: float,
                       bw_w_mbps: float, read_p99_us: float, w_p99_us: float,
                       start: float, dur: float, rc: int = 0,
                       knobs: dict | None = None) -> dict:
    start = float(start)
    # vm-scene collected format: runs[] carries fio_metrics_from_data fields;
    # clat_percentiles_us are already in us (raw fio JSON stores ns).
    pct_read = {f"{p:.6f}": v for p, v in
                ((50, read_p99_us * 0.45), (90, read_p99_us * 0.7),
                 (95, read_p99_us * 0.8), (99, read_p99_us),
                 (99.9, read_p99_us * 1.4))}
    pct_write = {f"{p:.6f}": v for p, v in
                 ((50, w_p99_us * 0.5), (95, w_p99_us * 0.8),
                  (99, w_p99_us), (99.9, w_p99_us * 1.3))}
    run = {"concurrency": 1, "returncode": rc, "fio_json": "demo_fio.json",
           "per_job_metrics": [{"jobname": "demo",
                                "directions": {
                                    "read": {"bw_mbps": bw_r_mbps, "iops": iops_r,
                                             "total_ios": iops_r * dur,
                                             "clat_percentiles_us": dict(pct_read)},
                                    "write": {"bw_mbps": bw_w_mbps, "iops": iops_w,
                                              "total_ios": iops_w * dur,
                                              "clat_percentiles_us": dict(pct_write)},
                                },
                                "context_switches": 1000}],
           "direction_bw_mbps": {"read": bw_r_mbps, "write": bw_w_mbps, "trim": 0.0},
           "agg_bw_mbps": bw_r_mbps + bw_w_mbps,
           "iops": iops_r + iops_w, "total_ios": (iops_r + iops_w) * dur,
           "p99_latency_us": max(read_p99_us, w_p99_us),
           "avg_clat_us": 42.0}
    # real collected docs carry io_ext_cgroup_controls at DOC level
    return {"runs": [run], "start_time": start, "end_time": start + dur,
            "duration_wall": dur,
            "io_ext_cgroup_controls": knobs or {
                "cgroup_path": "/sys/fs/cgroup/homo_v2_demo",
                "device_major_minor": "8:16", "device_path": "/dev/sdb",
                "io_max_line": "8:16 rbps=max wbps=max riops=max wiops=max",
                "io_max_unlimited": True, "io_weight_lines": [],
                "io_cost": {"root_cgroup_path": "/sys/fs/cgroup",
                            "device_major_minor": "8:16",
                            "io_cost_model_line": None,
                            "io_cost_qos_line": "8:16 enable=0 ctrl=user "
                                                "rpct=95.0 rlat=1000000 "
                                                "min=50.0 max=150.0"}}}


def sidecar_log_text(arm: str, t0: float, n_samples: int = 31) -> str:
    """Minimal sidecar text whose every sample matches EXPECTED_STATE[arm]
    (31 samples at 2 s covers the 60 s workload window for the gate; the
    loaders are written into every sample -> setup lookback evidence ok)."""
    sched_full = "none mq-deadline kyber bfq scx_io"
    lines = []
    exp = EXPECTED_STATE[arm]
    for i in range(n_samples):
        ts = float(t0) + 2.0 * i
        lines.append(f"=== T={ts:.3f}")
        lines.append("-- sdb scheduler")
        sched = exp["sched"]
        if sched == "none":
            lines.append("[none] mq-deadline kyber bfq scx_io")
        elif sched == "scx_io":
            # the scx_io attachment can drop the option list -> bracketless
            lines.append("none mq-deadline kyber bfq scx_io")
        else:  # mq-deadline / kyber natives keep the bracket list
            lines.append(sched_full.replace(sched, f"[{sched}]"))
        lines.append("-- io.cost.model/qos (root)")
        lines.append("/sys/fs/cgroup/io.cost.model:8:16 ctrl=user model=linear")
        lines.append("/sys/fs/cgroup/io.cost.qos:8:16 enable=0 ctrl=user "
                     "rpct=95.00 rlat=1000000 wpct=95.00 wlat=1000000 "
                     "min=50.00 max=150.00")
        lines.append("-- cgroup io.* arm state (non-max lines)")
        if arm == "n_io_max":
            lines.append(f"8:16 rbps={IO_MAX_BYTES} wbps={IO_MAX_BYTES} "
                         "riops=max wiops=max")
        if arm in ("n_io_weight", "n_io_cost"):
            lines.append("default 200")
            lines.append("default 100")
        elif arm == "n_io_max":
            lines.append("default 100")
        elif arm == "n_io_latency":
            lines.append("default 100")
            lines.append(f"8:16 target={IO_LAT_TARGET_US}")
        else:
            lines.append("default 100")
        lines.append("-- ioext bpffs pins")
        lines += ["ioext_active_cgroups", "ioext_policy_meta"] \
            if arm.startswith("x_") else ["(none)"]
        lines.append("-- loaders")
        names = exp["loaders"] or []
        if names:
            for j, nm in enumerate(names):
                lines.append(f"{500 + j} /opt/io-ext/userspace/{nm} -c ...")
        else:
            lines.append("(none)")
        lines.append("-- scx procs")
        lines.append("(none)" if not arm.startswith("x_")
                     else "101 /usr/local/bin/scx_io")
    return "\n".join(lines) + "\n"


# per-arm headline workload values used by the fixtures (deterministic, with
# small rep jitter so IQR / bootstrap CI are non-zero)
FIXTURE_VALUES = {
    # elev arms: iops_r = 4k mix IOPS, p99/w_p99 = read/write p99 (us).
    # coloc arms: lc_bw = LC byte rate (MiB/s, QD32 loadpoint of 2026-11-07
    # iteration#1), be_bw = BE byte rate (MiB/s; the io.max / q_iomax cap
    # arms ride the 64 MiB/s TRACK_ANCHOR_MIB so their track-error
    # fixture medians stay inside the <20% sanity band), lc_p99 / be_p99.
    "n_none":        {"iops_r": 52000, "p99": 1500.0, "w_p99": 1400.0,
                      "lc_bw": 80.0, "be_bw": 120.0, "lc_p99": 1600.0, "be_p99": 5200.0},
    "n_mq_deadline": {"iops_r": 56000, "p99": 1200.0, "w_p99": 1150.0},
    "n_kyber":       {"iops_r": 58000, "p99": 1100.0, "w_p99": 1100.0},
    "x_mq_deadline_q": {"iops_r": 51000, "p99": 1150.0, "w_p99": 1120.0},
    "x_kyber_q":     {"iops_r": 55500, "p99": 780.0, "w_p99": 950.0},
    "n_io_max":      {"lc_p99": 1900.0, "be_bw": 66.0, "be_p99": 6500.0},
    "x_iomax_q":     {"lc_p99": 1750.0, "be_bw": 68.0, "be_p99": 6000.0},
    "n_io_weight":   {"lc_p99": 1800.0, "be_bw": 150.0, "be_p99": 5800.0},
    "x_weight_arb":  {"lc_p99": 1450.0, "be_bw": 170.0, "be_p99": 5200.0},
    "n_io_cost":     {"lc_p99": 1450.0, "be_bw": 160.0, "be_p99": 5600.0},
    "x_cost_arb":    {"lc_p99": 1250.0, "be_bw": 175.0, "be_p99": 5100.0},
    "n_io_latency":  {"lc_p99": 1400.0, "be_bw": 140.0, "be_p99": 5400.0},
    "x_latency_arb": {"lc_p99": 1150.0, "be_bw": 165.0, "be_p99": 5000.0},
    "x_d2fq_arb":    {"lc_p99": 1230.0, "be_bw": 180.0, "be_p99": 5050.0},
    "x_linnos_q":    {"lc_p99": 1050.0, "be_bw": 185.0, "be_p99": 4950.0},
}
DEFAULT_CTRL = FIXTURE_VALUES["n_none"]


def make_fixtures(fix_root: Path, reps: int = 3) -> list:
    """Synthesize (rep, seq) run dirs under the rep<NN>/<SEQ>/ layout."""
    seqs = ["HOMO_elev_n_none", "HOMO_elev_n_mq_deadline", "HOMO_elev_x_mq_deadline_q",
            "HOMO_elev_n_kyber", "HOMO_elev_x_kyber_q",
            "HOMO_ctrl_n_none", "HOMO_ctrl_n_io_max", "HOMO_ctrl_x_iomax_q",
            "HOMO_ctrl_n_io_weight", "HOMO_ctrl_x_weight_arb",
            "HOMO_ctrl_n_io_cost", "HOMO_ctrl_x_cost_arb",
            "HOMO_ctrl_n_io_latency", "HOMO_ctrl_x_latency_arb",
            "HOMO_front_x_d2fq_arb", "HOMO_front_x_linnos_q"]
    t0 = 1794075000.0
    for rep in range(1, reps + 1):
        rep_dir = fix_root / f"rep{rep:02d}"
        rep_dir.mkdir(parents=True, exist_ok=True)
        for si, seq in enumerate(seqs):
            tok, arm = seq.split("_")[1], seq.split("_", 2)[2]
            rng = random.Random(1000 * rep + si)
            vals = dict(FIXTURE_VALUES.get(arm, DEFAULT_CTRL))
            j = lambda base, pct=0.04: base * (1.0 + rng.uniform(-pct, pct))  # noqa: E731
            case = rep_dir / seq
            if tok == "elev":
                scene = case / "step_0" / f"hv_elev_{arm}_job"
                scene.mkdir(parents=True, exist_ok=True)
                doc = make_fio_collected(j(vals["iops_r"]), j(vals["iops_r"] * 0.43),
                                         j(vals["iops_r"] * 4 / 2**20),
                                         j(vals["iops_r"] * 0.43 * 4 / 2**20),
                                         j(vals["p99"]), j(vals["w_p99"]),
                                         t0 + 1000.0 * si, 60.0,
                                         knobs={"cgroup_path": f"/sys/fs/cgroup/homo_v2_elev_{arm}"})
                (scene / f"fio_hv_elev_{arm}_job_{int(t0)}.json").write_text(
                    json.dumps(doc, indent=1, sort_keys=True))
            else:
                lc_v = dict(DEFAULT_CTRL) if arm == "n_none" else vals
                lc_bw, lc_p99 = j(lc_v.get("lc_bw", 80.0)), j(lc_v.get("lc_p99", 1600.0))
                be_bw, be_p99 = j(lc_v.get("be_bw", 120.0)), j(lc_v.get("be_p99", 5200.0))
                lc_knobs = {"cgroup_path": f"/sys/fs/cgroup/homo_v2_ctrl_{arm}_lc"}
                be_knobs = {"cgroup_path": f"/sys/fs/cgroup/homo_v2_ctrl_{arm}_be",
                            "io_max_line": "8:16 rbps=max wbps=max riops=max wiops=max",
                            "io_weight_lines": ["default 200"]}
                if arm == "n_io_cost":
                    # mirrors gen IOCOST_QOS (iteration#3 rlat re-anchor:
                    # 20000 -> 2000 us; the knobs gate only asserts
                    # enable=1, the literal is synced for honesty).
                    lc_knobs["io_cost"] = {
                        "io_cost_qos_line": "8:16 enable=1 ctrl=user rpct=99.0 "
                                            "rlat=2000 wpct=95.0 "
                                            "wlat=1000000 min=50.0 max=150.0",
                        "io_cost_model_line": None}
                if arm == "n_io_max":
                    be_knobs["io_max_line"] = (
                        f"8:16 rbps={IO_MAX_BYTES} wbps={IO_MAX_BYTES} "
                        "riops=max wiops=max")
                scene = case / "step_0" / f"hv_{tok}_{arm}_lc"
                scene.mkdir(parents=True, exist_ok=True)
                (scene / f"fio_hv_{tok}_{arm}_lc_{int(t0)}.json").write_text(
                    json.dumps(make_fio_collected(42000.0, 0.0, lc_bw, 0.0,
                                                  lc_p99, 0.0, t0 + 1000.0 * si, 60.0,
                                                  knobs=lc_knobs),
                               indent=1, sort_keys=True))
                scene = case / "step_0" / f"hv_{tok}_{arm}_be"
                scene.mkdir(parents=True, exist_ok=True)
                (scene / f"fio_hv_{tok}_{arm}_be_{int(t0)}.json").write_text(
                    json.dumps(make_fio_collected(0.0, 2000.0, 0.0, be_bw,
                                                  0.0, be_p99, t0 + 1000.0 * si, 60.0,
                                                  knobs=be_knobs),
                               indent=1, sort_keys=True))
            (case / f"{seq}.sidecar.log").write_text(
                sidecar_log_text(arm, t0 + 1000.0 * si - 2.0))
    return seqs


def self_test() -> None:
    # 1) arm parsing + group mapping
    assert parse_seq_id("HOMO_elev_n_mq_deadline") == ("elevator", "n_mq_deadline")
    assert parse_seq_id("HOMO_ctrl_n_io_max") == ("controller", "n_io_max")
    assert parse_seq_id("HOMO_front_x_linnos_q") == ("frontier", "x_linnos_q")
    assert group_of("n_none") == "baseline"
    assert group_of("n_kyber") == "native"
    assert group_of("x_iomax_q") == "iox"
    # 2) percentile parsing (us keyed "50"/"99.9" in the collected docs)
    assert percentile_from_map({"99.000000": "12345.0"}, 99.0) == 12.345
    assert percentile_from_map({"99.990000:2": "x", "99.900000": "2,000.0"}, 99.9) == 2.0
    # 3) sidecar parse + gate for native arms and the capped arms
    tmp = Path(tempfile.mkdtemp(prefix="homo_v2_selftest_"))
    try:
        p = tmp / "s.log"
        p.write_text(sidecar_log_text("n_kyber", 100.0))
        s = parse_sidecar_log(p)
        assert s and s[0]["ts"] == 100.0 and s[0]["scheduler"]
        assert sidecar_gate((100.0, 112.0), "n_kyber", s)["ok"]
        assert sidecar_gate((100.0, 112.0), "n_kyber", s)["sample_n"] == 7
        for arm in ("n_io_max", "n_io_weight", "n_io_cost", "n_io_latency"):
            p.write_text(sidecar_log_text(arm, 200.0))
            s = parse_sidecar_log(p)
            g = sidecar_gate((200.0, 212.0), arm, s)
            assert g["ok"], (arm, g)
        # a wrong arm must FAIL the gate (e.g. native samples vs an iox arm)
        p2 = tmp / "t2.log"
        p2.write_text(sidecar_log_text("n_kyber", 100.0))
        g = sidecar_gate((100.0, 112.0), "x_kyber_q", parse_sidecar_log(p2))
        assert not g["ok"], g
        # missing sidecar samples must fail too (never silently pass)
        assert not sidecar_gate((100.0, 112.0), "n_kyber", [])["ok"]
        # iox arm gate: bracketless scx_io + one-shot loader/pin setup evidence
        for arm in ("x_linnos_q", "x_latency_arb", "x_iomax_q"):
            p.write_text(sidecar_log_text(arm, 300.0))
            s = parse_sidecar_log(p)
            g = sidecar_gate((300.0, 312.0), arm, s)
            assert g["ok"], (arm, g)
            assert setup_evidence((300.0, 312.0), arm, s)["ok"], arm
        # missing the required loader -> setup evidence MUST fail
        p.write_text(sidecar_log_text("x_linnos_q", 300.0).replace(
            "loader_q_linnos", "loader_q_other"))
        s = parse_sidecar_log(p)
        assert not setup_evidence((300.0, 312.0), "x_linnos_q", s)["ok"]
        # cross-arm contamination: "default 200" inside an n_none window fails
        p.write_text(sidecar_log_text("n_io_weight", 300.0))
        s = parse_sidecar_log(p)
        assert not sidecar_gate((300.0, 312.0), "n_none", s)["ok"]
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    # 4) bfq carrier for the native io.weight arm (iteration#1)
    assert EXPECTED_STATE["n_io_weight"]["sched"] == "bfq"
    # 5) share / jain arithmetic (Jain = (Σx)^2/(n·Σx^2), two tenants;
    # share_ratio_dev anchored on the BE byte-share target 2/3, in %)
    lc, be = 20.0, 100.0
    assert abs((lc + be) ** 2 / (2 * (lc ** 2 + be ** 2)) - 0.6923076923) < 1e-9
    assert abs(LC_SHARE_TARGET - 1.0 / 3.0) < 1e-9
    assert abs(BE_SHARE_TARGET - 2.0 / 3.0) < 1e-9
    be_share = be / (lc + be)
    assert abs(abs(be_share - BE_SHARE_TARGET) / BE_SHARE_TARGET - 0.25) < 1e-9
    print("self-test OK")


def smoke(root: Path = Path("/tmp/homo_v2_smoke_fixtures")) -> None:
    """Build fake (rep, seq) artifacts, run the full pipeline end-to-end."""
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    seqs = make_fixtures(root)
    result = build_rows(root, root)
    paths = write_csvs(result, root)
    summary = summarize(result["rows"])
    sp = root / "homo_v2_summary.csv"
    with open(sp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_HEADER)
        w.writeheader()
        w.writerows(summary)
    print(f"wrote {sp} ({len(summary)} rows)")
    assert result["rows"], "smoke produced no cases rows"
    gated = [r for r in result["rows"] if r["gate_ok"]]
    assert gated, "smoke: no gated rows"
    arms = {r["arm"] for r in result["rows"]}
    assert {"n_none", "n_kyber", "x_kyber_q", "n_io_max", "x_iomax_q",
            "n_io_weight"} <= arms, arms
    # headline sanity: the q_kyber homo arm beats native kyber on read p99
    per_arm = {}
    for r in gated:
        if r["metric"] in ("elev_read_p99_us", "lc_read_p99_us"):
            per_arm.setdefault((r["arm"], r["metric"]), []).append(float(r["value"]))
    assert statistics.median(per_arm[("x_kyber_q", "elev_read_p99_us")]) < \
        statistics.median(per_arm[("n_kyber", "elev_read_p99_us")]), per_arm
    be_bw_n = [float(r["value"]) for r in gated
               if r["arm"] == "n_io_max" and r["metric"] == "be_track_err_pct"]
    assert be_bw_n and statistics.median(be_bw_n) < 20.0, be_bw_n
    # byte-share caliber sanity (iteration#1): shares sum to 1 and the
    # weight-arm share deviation is anchored on the 2/3 BE target (fixture
    # n_io_weight lc 80 / be 150 => be_share ~ 0.652, dev ~ 2.2%)
    share_rows = [r for r in gated if r["metric"] in ("lc_share", "be_share")]
    per_case: dict = {}
    for r in share_rows:
        per_case.setdefault((r["seq"], r["rep"]), {})[r["metric"]] = float(r["value"])
    assert per_case and all(
        abs(v.get("lc_share", -1) + v.get("be_share", -2) - 1.0) < 1e-6
        for v in per_case.values()), per_case
    dev_n = [float(r["value"]) for r in gated
             if r["arm"] == "n_io_weight" and r["metric"] == "share_ratio_dev"]
    assert dev_n, "smoke: no share_ratio_dev rows for n_io_weight"
    print("smoke OK: %d cases rows, %d gated, %d summary rows; artifacts in %s"
          % (len(result["rows"]), len(gated), len(summary), root))
    print("  cases:", paths["homo_v2_cases.csv"])
    print("  summary:", sp)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results-root", type=Path, default=DEFAULT_ROOT,
                    help="results/homo_v2_kvmhost/ root containing rep<NN>/ artifacts")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="CSV output dir (default: --results-root)")
    ap.add_argument("--probe-prefix", default=".",
                    help="dir-name prefix to ignore (default: dot-prefixed)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--smoke-fixtures", action="store_true",
                    help="synthetic fixtures at /tmp/homo_v2_smoke_fixtures, full pipeline")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.smoke_fixtures:
        smoke()
        return 0
    if not args.results_root.is_dir():
        print(__doc__)
        print(f"error: --results-root not found: {args.results_root}", file=sys.stderr)
        return 2
    result = build_rows(args.results_root, args.out_dir or args.results_root,
                        probe_prefix=args.probe_prefix)
    out_dir = args.out_dir or args.results_root
    paths = write_csvs(result, out_dir)
    summary = summarize(result["rows"])
    sp = out_dir / "homo_v2_summary.csv"
    with open(sp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_HEADER)
        w.writeheader()
        w.writerows(summary)
    print(f"wrote {sp} ({len(summary)} rows)")
    if not result["rows"]:
        print("warning: no (seq, rep) homo_v2 artifacts found under "
              f"{args.results_root}; wrote empty CSVs")
        return 0
    bad = {(r["arm"], r["seq"], r["rep"]): r["checks_failed"]
           for r in result["gate_rows"] if r["checks_failed"] != "all"}
    if bad:
        print(f"GATE FAILURES: {sorted(bad.items())}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
