#!/usr/bin/env python3
"""Merge D3 kvmhost calibration + discovery runs into one ledger with anomaly tags.

Outputs:
  results/d3_kvmhost/analysis/d3_kvmhost_all_runs_ledger.csv
  experiments/d3_kvmhost/D3_kvmhost_DATA_LEDGER.md
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANCHOR = ROOT / "results/d3_kvmhost/calibration/analysis/SLO_and_B_none.json"
CALIB_DIR = ROOT / "results/d3_kvmhost/calibration/analysis"
DISC = [
    ("discovery", ROOT / "results/d3_kvmhost/discovery/analysis/discovery_runs.csv"),
    ("discovery2", ROOT / "results/d3_kvmhost/discovery2/analysis/discovery_runs.csv"),
]
OUT_CSV = ROOT / "results/d3_kvmhost/analysis/d3_kvmhost_all_runs_ledger.csv"
OUT_MD = ROOT / "experiments/d3_kvmhost/D3_kvmhost_DATA_LEDGER.md"

FLOOR_FRAC = 0.20
BUSY_PAUSE_HEAVY = 20  # seconds under 20% B_none in one run (phase audit)


def fnum(x: str | float | None) -> float | None:
    if x is None or x == "":
        return None
    return float(x)


def truthy(s: str | bool | None) -> bool:
    if isinstance(s, bool):
        return s
    return str(s).strip().lower() in ("true", "1", "yes")


HARD_ANOMALY = {
    "POLICY_NOT_ENGAGED", "INCOMPLETE", "MANIFEST_BAD", "PARSE_ERROR",
    "DEVICE_PAUSE", "HEAVY_BUSY_STALL", "SOLO_P99_OUT_OF_BAND",
    "EXCLUDED_FROM_SOLO_MEDIAN",
}


def tag_discovery(row: dict, anchor: dict) -> tuple[list[str], list[str]]:
    """Return (hard_anomalies, gate_flags) — gate failures are not data anomalies."""
    hard: list[str] = []
    gates: list[str] = []
    status = row.get("status", "")
    if status == "policy-not-engaged":
        hard.append("POLICY_NOT_ENGAGED")
    elif status != "ok":
        hard.append("INCOMPLETE")
    if not truthy(row.get("manifest_ok")):
        hard.append("MANIFEST_BAD")
    if row.get("errors"):
        hard.append("PARSE_ERROR")
    if not truthy(row.get("be_valid")):
        hard.append("DEVICE_PAUSE")
    busy_u = row.get("busy_under_floor20")
    if busy_u not in (None, ""):
        n = int(float(busy_u))
        if n >= BUSY_PAUSE_HEAVY:
            hard.append("HEAVY_BUSY_STALL")
        elif n > 0:
            gates.append("BUSY_UNDER_FLOOR20")
    if truthy(row.get("lc_flip")):
        gates.append("LC_FLIP_FLAG")
    p99 = fnum(row.get("lc_p99_ms"))
    solo = anchor["solo_p99_ms_median"]
    slo = anchor["slo_batch_ms"]
    if p99 is not None:
        if p99 > slo:
            gates.append("SLO_FAIL")
        if p99 > 10 * solo and row.get("arm") != "none":
            gates.append("LC_TAIL_EXTREME")
    ret_c = fnum(row.get("ret_pct_calib"))
    if ret_c is not None and ret_c < 60:
        gates.append("RET_BELOW_60")
    return hard, gates


def tag_calib_solo(row: dict, anchor: dict) -> tuple[list[str], list[str]]:
    hard: list[str] = []
    gates: list[str] = []
    if not truthy(row.get("within_band")):
        hard.append("SOLO_P99_OUT_OF_BAND")
    if row["run_id"] not in anchor.get("valid_solo_run_ids", []):
        hard.append("EXCLUDED_FROM_SOLO_MEDIAN")
    return hard, gates


def tag_calib_cont(row: dict) -> tuple[list[str], list[str]]:
    hard: list[str] = []
    gates: list[str] = []
    bu = row.get("busy_under_floor20")
    if bu not in (None, "") and int(float(bu)) > 0:
        gates.append("BUSY_UNDER_FLOOR20")
    return hard, gates


def pareto_family(arm: str) -> str:
    if arm in ("bfq", "bfqf"):
        return "BFQ"
    if arm == "none":
        return "none"
    if arm.startswith("qrl_k065"):
        return "qrl_k065"
    if arm.startswith("qrl_k075"):
        return "qrl_k075"
    if arm.startswith("qrl_k085"):
        return "qrl_k085"
    if arm.startswith("qrl_k095"):
        return "qrl_k095"
    if arm.startswith("qrl_k100"):
        return "qrl_k100"
    if arm == "arbonly":
        return "arbonly"
    return arm


def clean_for_pareto(row: dict) -> bool:
    return (
        row.get("batch") in ("discovery", "discovery2")
        and row.get("status") == "ok"
        and truthy(row.get("policy_engaged"))
        and truthy(row.get("be_valid"))
    )


def load_rows() -> list[dict]:
    anchor = json.loads(ANCHOR.read_text())
    solo_med = anchor["solo_p99_ms_median"]
    b_none = anchor["b_none_mb_s"]
    be_floor = FLOOR_FRAC * b_none
    rows: list[dict] = []

    with (CALIB_DIR / "lc_solo_runs.csv").open(newline="") as f:
        for r in csv.DictReader(f):
            hard, gates = tag_calib_solo(r, anchor)
            p99 = fnum(r["lc_p99_ms"])
            rows.append({
                "run_id": r["run_id"],
                "batch": "calibration",
                "phase": "calib_lc_solo",
                "arm": "lc_solo",
                "arm_display": "LC solo (none sched)",
                "pareto_family": "—",
                "run": r["run_id"].rsplit("_run", 1)[-1],
                "status": r["status"],
                "policy_engaged": "",
                "lc_p99_ms": p99,
                "lc_x_solo": round(p99 / solo_med, 3) if p99 else None,
                "be_agg_mb_s": fnum(r.get("lc_bw_mb_s")),
                "ret_pct_calib": None,
                "be_valid": "",
                "slo_pass": p99 <= anchor["slo_batch_ms"] if p99 else None,
                "ret_pass": None,
                "busy_under_floor20": r.get("busy_under_floor20") or "",
                "anomaly_tags": "|".join(hard) if hard else "",
                "gate_flags": "|".join(gates) if gates else "",
                "is_anomaly": bool(hard),
                "clean_for_pareto": False,
                "notes": "solo anchor run",
            })

    with (CALIB_DIR / "none_contention_runs.csv").open(newline="") as f:
        for r in csv.DictReader(f):
            hard, gates = tag_calib_cont(r)
            p99 = fnum(r["lc_p99_ms"])
            be = fnum(r["be_agg_mb_s"])
            rows.append({
                "run_id": r["run_id"],
                "batch": "calibration",
                "phase": "calib_none_contention",
                "arm": "none",
                "arm_display": "none (calib batch)",
                "pareto_family": "none",
                "run": r["run_id"].rsplit("_run", 1)[-1],
                "status": r["status"],
                "policy_engaged": "True",
                "lc_p99_ms": p99,
                "lc_x_solo": round(p99 / solo_med, 3) if p99 else None,
                "be_agg_mb_s": be,
                "ret_pct_calib": round(be / b_none * 100, 2) if be else None,
                "be_valid": be >= be_floor if be else False,
                "slo_pass": p99 <= anchor["slo_batch_ms"] if p99 else None,
                "ret_pass": None,
                "busy_under_floor20": r.get("busy_under_floor20") or "",
                "anomaly_tags": "|".join(hard) if hard else "",
                "gate_flags": "|".join(gates) if gates else "",
                "is_anomaly": bool(hard),
                "clean_for_pareto": False,
                "notes": "calib B_none denominator; not discovery pareto",
            })

    for batch, path in DISC:
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                hard, gates = tag_discovery(r, anchor)
                p99 = fnum(r["lc_p99_ms"])
                be = fnum(r["be_agg_mb_s"])
                rec = {
                    "run_id": r["run_id"],
                    "batch": batch,
                    "phase": "discovery",
                    "arm": r["arm"],
                    "arm_display": r["arm"],
                    "pareto_family": pareto_family(r["arm"]),
                    "run": r["run"],
                    "status": r["status"],
                    "policy_engaged": r.get("policy_engaged", ""),
                    "lc_p99_ms": p99,
                    "lc_x_solo": round(p99 / solo_med, 3) if p99 else None,
                    "be_agg_mb_s": be,
                    "ret_pct_calib": fnum(r.get("ret_pct_calib")),
                    "be_valid": r.get("be_valid"),
                    "slo_pass": r.get("slo_pass"),
                    "ret_pass": r.get("ret_pass"),
                    "busy_under_floor20": r.get("busy_under_floor20") or "",
                    "anomaly_tags": "|".join(hard) if hard else "",
                    "gate_flags": "|".join(gates) if gates else "",
                    "is_anomaly": bool(hard),
                    "clean_for_pareto": False,
                    "notes": "",
                }
                rec["clean_for_pareto"] = clean_for_pareto(rec) and not rec["is_anomaly"]
                rows.append(rec)

    return rows, anchor


def pareto_aggregate(rows: list[dict], anchor: dict) -> list[dict]:
    """Per pareto_family clean-run stats for scatter design."""
    solo = anchor["solo_p99_ms_median"]
    b_none = anchor["b_none_mb_s"]
    families: dict[str, list[dict]] = {}
    for r in rows:
        if not r["clean_for_pareto"]:
            continue
        fam = r["pareto_family"]
        families.setdefault(fam, []).append(r)

    out = []
    for fam in sorted(families):
        rs = families[fam]
        p99s = [r["lc_p99_ms"] for r in rs if r["lc_p99_ms"]]
        bes = [r["be_agg_mb_s"] for r in rs if r["be_agg_mb_s"]]
        import statistics

        out.append({
            "pareto_family": fam,
            "n_clean": len(rs),
            "lc_p99_ms_med": statistics.median(p99s),
            "lc_x_solo_med": statistics.median(p99s) / solo,
            "be_mb_s_med": statistics.median(bes),
            "ret_pct_calib_med": statistics.median(bes) / b_none * 100,
            "run_ids": ", ".join(r["run_id"] for r in rs),
        })
    return out


def fmt_md_table(headers: list[str], data: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in data:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def write_md(rows: list[dict], anchor: dict, agg: list[dict]) -> None:
    n_anom = sum(1 for r in rows if r["is_anomaly"])
    n_clean = sum(1 for r in rows if r["clean_for_pareto"])
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def row_line(r: dict) -> list[str]:
        flag = "⚠ " + r["anomaly_tags"] if r["is_anomaly"] else ""
        gates = r.get("gate_flags") or ""
        clean = "✓" if r["clean_for_pareto"] else ""
        return [
            r["run_id"],
            r["batch"],
            r["arm_display"],
            f"{r['lc_p99_ms']:.4g}" if r["lc_p99_ms"] is not None else "—",
            f"{r['lc_x_solo']:.3g}" if r["lc_x_solo"] is not None else "—",
            f"{r['be_agg_mb_s']:.2f}" if r["be_agg_mb_s"] is not None else "—",
            f"{r['ret_pct_calib']:.2f}" if r["ret_pct_calib"] is not None else "—",
            r["status"],
            clean,
            flag,
            gates,
        ]

    sections = [
        ("标定 — LC solo（5 run）", [r for r in rows if r["phase"] == "calib_lc_solo"]),
        ("标定 — none contention（5 run）", [r for r in rows if r["phase"] == "calib_none_contention"]),
        ("发现 v1 — discovery", [r for r in rows if r["batch"] == "discovery"]),
        ("发现 v2 — discovery2", [r for r in rows if r["batch"] == "discovery2"]),
    ]

    hdr = [
        "run_id", "batch", "arm", "LC p99 (ms)", "LC×solo", "BE (MB/s)",
        "ret%@calib", "status", "pareto_clean", "anomaly", "gate_flags",
    ]
    body_parts = [
        f"# D3 kvmhost — 全量 run 数据表（含异常标记）",
        "",
        f"生成：`scripts/summarize_d3_kvmhost_ledger.py` · {ts}",
        "",
        "## 判据锚（只读 `SLO_and_B_none.json`）",
        "",
        "| 量 | 值 |",
        "|---|---|",
        f"| LC solo p99 median | **{anchor['solo_p99_ms_median']} ms** |",
        f"| SLO_batch (2×solo) | **{anchor['slo_batch_ms']} ms** |",
        f"| B_none (calib median) | **{anchor['b_none_mb_s']} MB/s** |",
        f"| BE 数据失效线 (20% B_none) | **{FLOOR_FRAC * anchor['b_none_mb_s']:.2f} MB/s** |",
        "",
        f"合计 **{len(rows)}** run；**{n_anom}** 条数据异常（应排除出散点）；"
        f"**{n_clean}** 条 Pareto 可用（ok + policy_engaged + be_valid）。",
        "",
        "**异常**（`anomaly_tags`）= 数据不可用或臂作废；**门控**（`gate_flags`）= 双门未过但点数仍可信。",
        "",
        "### 异常标签（hard）",
        "",
        "| 标签 | 含义 |",
        "|---|---|",
        "| `POLICY_NOT_ENGAGED` | metadata 调度器与臂不符（v1 bfq 三 run 整臂作废） |",
        "| `DEVICE_PAUSE` | BE 聚合 < 20% 标定 B_none（virtio 静默窗） |",
        "| `SOLO_P99_OUT_OF_BAND` | solo p99 超出 frozen band |",
        "| `HEAVY_BUSY_STALL` | ≥20 秒 busy < 20% B_none |",
        "",
        "### 门控标签（gate_flags，非异常）",
        "",
        "| 标签 | 含义 |",
        "|---|---|",
        "| `SLO_FAIL` / `RET_BELOW_60` | 冻结双门未过（Pareto 上仍作实测点） |",
        "| `BUSY_UNDER_FLOOR20` / `LC_FLIP_FLAG` | 审计提示 |",
        "",
        "## 按批逐 run",
        "",
    ]

    for title, subset in sections:
        body_parts.append(f"### {title}")
        body_parts.append("")
        body_parts.append(fmt_md_table(hdr, [row_line(r) for r in subset]))
        body_parts.append("")

    body_parts.append("## Pareto 族 — 仅 clean run 聚合（散点图用）")
    body_parts.append("")
    agg_hdr = [
        "family", "n_clean", "LC p99 med", "LC×solo", "BE med", "ret%@calib", "run_ids",
    ]
    agg_rows = []
    for a in agg:
        agg_rows.append([
            a["pareto_family"],
            str(a["n_clean"]),
            f"{a['lc_p99_ms_med']:.4g}",
            f"{a['lc_x_solo_med']:.3f}",
            f"{a['be_mb_s_med']:.2f}",
            f"{a['ret_pct_calib_med']:.1f}",
            a["run_ids"],
        ])
    body_parts.append(fmt_md_table(agg_hdr, agg_rows))
    body_parts.append("")
    body_parts.append("## 主图建议非支配集（手工口径，与 HANDOFF 一致）")
    body_parts.append("")
    body_parts.append(
        "BFQ ← `bfqf` v2 n=3 clean；qrl_k065 ← v1×3 + v2 burst b100/b250×2 clean；"
        "qrl_k075/k085 ← v1 各 n=1 clean；none ← v2 run2–3 clean（run1 为 DEVICE_PAUSE）。"
        "v1 `none`/`bfq` 与 v2 重复臂以批次标注，出图时勿混用分母。"
    )
    body_parts.append("")
    body_parts.append(f"机器可读全表：`results/d3_kvmhost/analysis/d3_kvmhost_all_runs_ledger.csv`")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(body_parts) + "\n", encoding="utf-8")


def main() -> int:
    rows, anchor = load_rows()
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "run_id", "batch", "phase", "arm", "arm_display", "pareto_family", "run",
        "status", "policy_engaged", "lc_p99_ms", "lc_x_solo", "be_agg_mb_s",
        "ret_pct_calib", "be_valid", "slo_pass", "ret_pass", "busy_under_floor20",
        "anomaly_tags", "gate_flags", "is_anomaly", "clean_for_pareto", "notes",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})

    agg = pareto_aggregate(rows, anchor)
    write_md(rows, anchor, agg)
    print("Wrote", OUT_CSV)
    print("Wrote", OUT_MD)
    print(f"runs={len(rows)} anomalies={sum(1 for r in rows if r['is_anomaly'])} "
          f"pareto_clean={sum(1 for r in rows if r['clean_for_pareto'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())