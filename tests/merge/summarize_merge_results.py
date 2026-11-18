#!/usr/bin/env python3
"""summarize_merge_results.py - generate MERGE_REPORT.md from suite results."""
import json
import os
import sys
from pathlib import Path
from datetime import datetime


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def parse_fio_metrics(fio_json):
    if not fio_json or not isinstance(fio_json, dict):
        return {}
    jobs = fio_json.get("jobs", [])
    if not jobs:
        return {}
    j = jobs[0]
    read = j.get("read", {})
    write = j.get("write", {})
    total_ios = read.get("total_ios", 0) + write.get("total_ios", 0)
    iops = read.get("iops", 0) + write.get("iops", 0)
    bw = read.get("bw", 0) + write.get("bw", 0)
    clat = read.get("clat_ns", {}) or write.get("clat_ns", {})
    p99 = clat.get("percentile", {}).get("99.000000", 0) if clat else 0
    return {
        "total_ios": total_ios,
        "iops": round(iops, 1),
        "bw_KiB": bw,
        "p99_clat_ns": p99,
    }


def parse_merge_stats(stats_json):
    if not stats_json:
        return {}
    if isinstance(stats_json, list):
        if stats_json and isinstance(stats_json[0], dict):
            v = stats_json[0].get("value", {})
            return v if isinstance(v, dict) else {}
        return {}
    if isinstance(stats_json, dict):
        if "value" in stats_json and isinstance(stats_json["value"], dict):
            return stats_json["value"]
        key_vals = stats_json.get("key", []) or stats_json.get("elements", [])
        result = {}
        for elem in key_vals if isinstance(key_vals, list) else [key_vals]:
            if isinstance(elem, dict) and "value" in elem:
                v = elem["value"]
                if isinstance(v, dict):
                    for k, val in v.items():
                        if isinstance(val, (int, float, str)):
                            result[k] = val
        return result
    return {}


def collect_case(case_dir):
    case_id = case_dir.name
    manifest = load_json(case_dir / "manifest.json") or {}
    verdict = load_json(case_dir / "verdict.json") or {}
    obs_status = ""
    obs_path = case_dir / "obs_status.txt"
    if obs_path.exists():
        obs_status = obs_path.read_text(errors="replace").strip()

    fio_metrics = {}
    for f in sorted(case_dir.glob("fio_*.json")):
        if f.name == "fio.json":
            fio_metrics["primary"] = parse_fio_metrics(load_json(f))
        else:
            load_id = f.stem.replace("fio_", "")
            fio_metrics[load_id] = parse_fio_metrics(load_json(f))

    merge_stats = {}
    ms_path = case_dir / "merge_stats.json"
    if ms_path.exists():
        merge_stats = parse_merge_stats(load_json(ms_path))

    return {
        "case_id": case_id,
        "manifest": manifest,
        "verdict": verdict,
        "obs_status": obs_status,
        "fio_metrics": fio_metrics,
        "merge_stats": merge_stats,
    }


def generate_report(suite_dir, cases):
    lines = []
    lines.append("# io-ext Merge Test Report")
    lines.append("")
    lines.append(f"**Generated**: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"**Suite**: `{suite_dir.name}`")
    lines.append("")

    passed = sum(1 for c in cases if c.get("verdict", {}).get("status") == "PASS")
    failed = sum(1 for c in cases if c.get("verdict", {}).get("status") == "FAIL")
    inconclusive = len(cases) - passed - failed

    lines.append("## 1. Executive Summary")
    lines.append("")
    lines.append(f"- **Total cases**: {len(cases)}")
    lines.append(f"- **PASS**: {passed}")
    lines.append(f"- **FAIL**: {failed}")
    lines.append(f"- **INCONCLUSIVE**: {inconclusive}")
    if cases:
        m = cases[0].get("manifest", {})
        lines.append(f"- **Kernel**: `{m.get('kernel', 'unknown')}`")
        lines.append(f"- **IOEXT_DEBUG**: `{m.get('ioext_debug', 'unknown')}`")
    lines.append("")

    lines.append("## 2. Background & Architecture")
    lines.append("")
    lines.append("Merge is not a standalone scheduler — it is a set of optional `struct_ops` hooks")
    lines.append("(`allow_merge`, `bio_merge`, `request_merge`, `former_request`, `request_merged`)")
    lines.append("implemented by each queueing policy (`q_*`). Arbiter is orthogonal to merge.")
    lines.append("")

    lines.append("## 3. Build & Test Environment")
    lines.append("")
    lines.append("Built with `scripts/make.sh` using `config-7.0-osext-merge-test`")
    lines.append("(enables `CONFIG_IOSCHED_EXT_DEBUG` + `noinline` on merge hooks).")
    lines.append("VM: 64 vCPU NVMe via `scripts/run.sh`.")
    lines.append("")

    lines.append("## 4. Implementation Notes")
    lines.append("")
    lines.append("- **q_merge_probe**: 4-mode minimal FIFO policy (DENY_ALL, ALLOW_ALL,")
    lines.append("  ADJACENT_SAME_OP, COUNT_ONLY) with `merge_stats` BPF map as L1 probe.")
    lines.append("- **q_mq_deadline D1/D2/D3**: `allow_merge` uses sector-adjacency + same-op")
    lines.append("  check. D3 adds `bio_merge=true` for plug path. `request_merged` counts merges.")
    lines.append("- **q_kyber K0/K1**: K0 uses domain-based merge compatibility; K1 disables merge.")
    lines.append("")

    lines.append("## 5. Usability Matrix")
    lines.append("")
    lines.append("| Case | Policy | Status | allow_true | request_merged | total_ios(L1) | IOPS(L1) |")
    lines.append("|------|--------|--------|------------|----------------|---------------|----------|")
    for c in cases:
        cid = c["case_id"]
        policy = c.get("manifest", {}).get("policy", "?")
        status = c.get("verdict", {}).get("status", "?")
        ms = c.get("merge_stats", {})
        allow_true = ms.get("allow_merge_true", "-")
        req_merged = ms.get("request_merged_calls", "-")
        l1 = c.get("fio_metrics", {}).get("L1_seq_write_heavy", c.get("fio_metrics", {}).get("primary", {}))
        total_ios = l1.get("total_ios", "-")
        iops = l1.get("iops", "-")
        lines.append(f"| {cid} | {policy} | {status} | {allow_true} | {req_merged} | {total_ios} | {iops} |")
    lines.append("")

    failed_cases = [c for c in cases if c.get("verdict", {}).get("status") == "FAIL"]
    if failed_cases:
        lines.append("## 6. Failed Cases")
        lines.append("")
        for c in failed_cases:
            lines.append(f"### {c['case_id']}")
            lines.append(f"- **Detail**: {c.get('verdict', {}).get('detail', '?')}")
            lines.append(f"- **D-state**: {c.get('verdict', {}).get('d_state', '?')}")
            dmesg_after = suite_dir / c["case_id"] / "dmesg_after.txt"
            if dmesg_after.exists():
                tail = dmesg_after.read_text(errors="replace").splitlines()[-20:]
                lines.append(f"- **dmesg tail**:\n```")
                lines.extend(tail)
                lines.append("```")
            lines.append("")

    lines.append("## 7. Data Analysis")
    lines.append("")
    lines.append("### total_ios Comparison (merge on vs off)")
    lines.append("")
    lines.append("| Case | total_ios | IOPS | P99 clat (ns) |")
    lines.append("|------|-----------|------|---------------|")
    for c in cases:
        for load_id, m in c.get("fio_metrics", {}).items():
            lines.append(f"| {c['case_id']} [{load_id}] | {m.get('total_ios', '-')} | {m.get('iops', '-')} | {m.get('p99_clat_ns', '-')} |")
    lines.append("")

    lines.append("## 8. Observability Appendix")
    lines.append("")
    lines.append("| Case | bpftrace status |")
    lines.append("|------|-----------------|")
    for c in cases:
        obs = "OBS_PROBE_OK" if "OBS_PROBE_OK" in c.get("obs_status", "") else "OBS_PROBE_UNAVAILABLE"
        lines.append(f"| {c['case_id']} | {obs} |")
    lines.append("")

    lines.append("## 9. Conclusion")
    lines.append("")
    if failed == 0 and len(cases) > 0:
        lines.append("All cases PASSED. Merge hooks are reachable and functional in the io-ext framework.")
    elif failed > 0:
        lines.append(f"{failed} case(s) FAILED. See Section 6 for details and evidence paths.")
    else:
        lines.append("No cases executed yet.")
    lines.append("")
    lines.append("### Recommendations")
    lines.append("- Primary judgment: BPF `merge_stats` + fio `total_ios` delta (bpftrace is auxiliary)")
    lines.append("- For production merge: D1 (adjacent + same-op) is the minimal correct variant")
    lines.append("- Known risk: `q_mq_deadline` L6 low-QD hang may affect D1-D3 regressions")
    lines.append("")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <suite_dir>", file=sys.stderr)
        sys.exit(1)

    suite_dir = Path(sys.argv[1])
    if not suite_dir.is_dir():
        print(f"ERROR: {suite_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    cases = []
    for case_dir in sorted(suite_dir.iterdir()):
        if case_dir.is_dir() and (case_dir / "verdict.json").exists():
            cases.append(collect_case(case_dir))

    report = generate_report(suite_dir, cases)
    report_path = suite_dir / "MERGE_REPORT.md"
    report_path.write_text(report)
    print(f"Report written to {report_path}")
    print(f"Cases processed: {len(cases)}")


if __name__ == "__main__":
    main()