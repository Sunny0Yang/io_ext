#!/usr/bin/env python3
# sc_analyze_bm.py — S-CHAOS gate checks + latency statistics + report generator.
# Runs on the workstation against pulled result dirs:
#   python3 sc_analyze_bm.py results/safety_chaos_S1_r1_* [more dirs ...]
# Emits SAFETY_REPORT.md + summary.json inside the first dir (per run) and, for
# multiple dirs, a campaign summary on stdout.
import csv
import glob
import json
import math
import os
import re
import sys

TEARDOWN_RE = re.compile(
    r"\[\s*(\d+\.\d+)\]\s+ioext: teardown complete policy='([^']+)' "
    r"cgroup_id=(\d+) reason=(\w+) drained=(\d+)")

CRITICAL_PATTERNS = [
    r"kernel BUG", r"BUG: ", r"Oops", r"Kernel panic", r"general protection",
    r"paging request", r"double fault", r"list corruption", r"slab corruption",
    r"use-after-free", r"KASAN", r"hung task", r"blocked for more than",
    r"INFO: task .* blocked", r"watchdog: BUG", r"rcu.*stall", r"Rebooting",
]
WARN_PATTERNS = [
    r"WARNING: ", r"lockdep", r"blk_update_request.*error", r"virtio.*error",
    r"I/O error", r"scx_io.*fallback.*fail", r"evacuate stall",
]

def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)

def stats(vals):
    if not vals:
        return {"n": 0}
    s = sorted(vals)
    return {"n": len(s), "min": s[0], "p50": percentile(s, 50),
            "p90": percentile(s, 90), "p99": percentile(s, 99),
            "max": s[-1], "mean": sum(s) / len(s)}

def analyze_run(d):
    r = {"dir": os.path.basename(d), "gates": {}, "errors": []}

    # ---- run meta ----
    try:
        r["meta"] = json.load(open(os.path.join(d, "run_meta.json")))
    except Exception as e:
        r["errors"].append("run_meta.json missing: %s" % e)
        r["meta"] = {}

    # ---- G1: fio integrity ----
    fio_rc = r["meta"].get("fio_rc", None)
    chaos_json = os.path.join(d, "chaos.json")
    g1, g6 = None, None
    total_ios = err = write_ios = 0
    size_g = r["meta"].get("size_g_total", 8)
    expected_ios = size_g * 262144 * 0.8   # 4k blocks/GiB, 20% slack
    if os.path.exists(chaos_json):
        try:
            fj = json.load(open(chaos_json))
            jobs = fj.get("jobs", [])
            # fio 3.41 JSON: per-direction "total_ios" lives in jobs[].write/read
            write_ios = sum(int(j.get("write", {}).get("total_ios", 0)) for j in jobs)
            read_ios = sum(int(j.get("read", {}).get("total_ios", 0)) for j in jobs)
            total_ios = write_ios + read_ios
            err = sum(j.get("error", 0) for j in jobs)
            g1 = (fio_rc == 0 and err == 0)
            g6 = write_ios >= expected_ios
        except Exception as e:
            r["errors"].append("chaos.json parse: %s" % e)
    r["fio"] = {"rc": fio_rc, "total_ios": total_ios, "write_ios": write_ios,
                "error_count": err}
    r["gates"]["G1_fio_integrity"] = g1
    r["gates"]["G6_volume_ge_80pct_of_size"] = g6

    # ---- G2: kernel log forbidden patterns ----
    crit_hits, warn_hits = [], []
    dl = os.path.join(d, "dmesg_tap.log")
    teardown_lines, drained_total = [], 0
    fallback_summaries = 0
    if os.path.exists(dl):
        for line in open(dl, errors="replace"):
            if any(re.search(p, line) for p in CRITICAL_PATTERNS):
                crit_hits.append(line.strip()[:200])
            elif any(re.search(p, line) for p in WARN_PATTERNS):
                warn_hits.append(line.strip()[:200])
            m = TEARDOWN_RE.search(line)
            if m:
                teardown_lines.append(m.groups())
                drained_total += int(m.group(5))
            if "fallback bypass summary" in line:
                fallback_summaries += 1
    r["kernel_log"] = {"critical_hits": crit_hits[:10], "critical_n": len(crit_hits),
                       "warn_hits": warn_hits[:10], "warn_n": len(warn_hits),
                       "teardown_lines": len(teardown_lines),
                       "drained_total": drained_total,
                       "fallback_summaries": fallback_summaries}
    r["gates"]["G2_no_kernel_fault"] = (os.path.exists(dl) and len(crit_hits) == 0)

    # ---- events / G4 / G5 ----
    ev_path = os.path.join(d, "events.csv")
    events = []
    if os.path.exists(ev_path):
        with open(ev_path) as f:
            events = list(csv.DictReader(f))
    missing_td = [e for e in events
                  if e["kind"] in ("local_switch", "fault_null", "fault_stall",
                                   "final_detach")
                  and not (e.get("ts_boot_free") or e.get("ts_boot_meta_clean"))]
    lat_by_kind, drain_by_kind = {}, {}
    for e in events:
        if e.get("e2e_ms"):
            lat_by_kind.setdefault(e["kind"], []).append(
                (float(e["e2e_ms"]), e.get("fault_param", "")))
        if e.get("drain_kernel_ms"):
            drain_by_kind.setdefault(e["kind"], []).append(
                float(e["drain_kernel_ms"]))
    r["events"] = {"n": len(events), "missing_teardown": len(missing_td),
                   "kinds": {k: len(v) for k, v in lat_by_kind.items()}}
    r["gates"]["G4_every_switch_has_teardown"] = (len(events) > 0 and
                                                  len(missing_td) == 0)

    # stall-window expectation: e2e <= stall_ms + 15000
    stall_viol = []
    for e in events:
        if e["kind"] == "fault_stall" and e.get("e2e_ms"):
            ms = int(e["fault_param"].split("=")[1])
            if float(e["e2e_ms"]) > ms + 15000:
                stall_viol.append(e["seq"])
    r["gates"]["G5_stall_within_window"] = (len(stall_viol) == 0)

    lat_stats = {}
    for k, v in lat_by_kind.items():
        vals = [x[0] for x in v]
        s = stats(vals)
        if k == "fault_stall":
            s["by_param"] = {p: stats([x[0] for x in v if x[1] == p])
                             for p in sorted({x[1] for x in v})}
        lat_stats[k] = s
    r["latency_e2e_ms"] = lat_stats

    # non-fault gates (provisional thresholds)
    normal = lat_stats.get("local_switch", {})
    r["gates"]["G5_e2e_p99_lt_10ms(provisional)"] = (
        normal.get("n", 0) > 0 and normal.get("p99", 1e9) < 10.0)

    # ---- kernel drain duration (kprobe prepare->free join) ----
    if drain_by_kind:
        r["drain_kernel_ms"] = {k: stats(v) for k, v in drain_by_kind.items()}
        all_drains = sorted(x for v in drain_by_kind.values() for x in v)
        r["gates"]["G5_drain_p99_lt_5ms(provisional)"] = (
            len(all_drains) > 0 and percentile(all_drains, 99) < 5.0)

    # ---- G3: inflight sampling ----
    sp = os.path.join(d, "sampler.csv")
    g3, infl_max, plateaus = None, None, []
    if os.path.exists(sp):
        rows = []
        with open(sp) as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 3:
                    continue
                try:
                    infl = parts[1].split("+")
                    r_, w_ = (int(infl[0]), int(infl[1]))
                    rows.append((float(parts[0]), r_ + w_))
                except Exception:
                    continue
        if rows:
            infl_max = max(x[1] for x in rows)
            nr = r["meta"].get("nr_requests", 2048)
            # sustained nonzero plateaus (>5s) near run end (>=90% of span)
            span_end = rows[-1][0]
            nonzero_tail = [x for x in rows if x[0] >= span_end * 0.98 and x[1] > 0]
            plateau_tail = len(nonzero_tail) > 20   # 20Hz * 1s
            g3 = (infl_max <= nr) and (not plateau_tail)
            r["sampler"] = {"n": len(rows), "inflight_max": infl_max,
                            "nr_requests": nr,
                            "nonzero_at_tail_98pct": len(nonzero_tail)}
            r["gates"]["G3_inflight_bounds"] = g3
    infl_end = str(r["meta"].get("inflight_at_end", "")).split()
    r["gates"]["G3_inflight_end_zero"] = (
        infl_end == ["0", "0"])

    verdict = all(v is True for k, v in r["gates"].items()
                  if not k.endswith("(provisional)"))
    r["verdict"] = "PASS" if verdict and not r["errors"] else "FAIL"
    return r

def fmt_stats(s, unit=""):
    if not s or s.get("n", 0) == 0:
        return "n=0"
    return ("n=%(n)d min=%(min).2f p50=%(p50).2f p90=%(p90).2f "
            "p99=%(p99).2f max=%(max).2f%(u)s mean=%(mean).2f%(u)s"
            % dict(s, u=unit))

def write_report(d, r):
    md = ["# S-CHAOS run report — %s" % r["dir"], "",
          "arm=%(arm)s rep=%(rep)s runtime=%(runtime_s)ss size=%(size_g_total)G kernel=%(kernel)s"
          % r["meta"], "",
          "## Verdict: **%s**" % r["verdict"], ""]
    md.append("## Gates")
    for k, v in sorted(r["gates"].items()):
        md.append("- %s: %s" % (k, "PASS" if v else "FAIL"))
    md.append("")
    md.append("## fio: %d I/O completed, error_count=%d (rc=%s)"
              % (r["fio"]["total_ios"], r["fio"]["error_count"], r["fio"]["rc"]))
    md.append("")
    md.append("## Kernel log: teardown_lines=%d drained_total=%d critical=%d warn=%d"
              % (r["kernel_log"]["teardown_lines"], r["kernel_log"]["drained_total"],
                 r["kernel_log"]["critical_n"], r["kernel_log"]["warn_n"]))
    for h in r["kernel_log"]["critical_hits"]:
        md.append("  - CRIT: `%s`" % h)
    md.append("")
    md.append("## Switch latency (T_switch_e2e, ms)")
    for k, s in r["latency_e2e_ms"].items():
        md.append("- %s: %s" % (k, fmt_stats(s)))
        if "by_param" in s:
            for p, ss in s["by_param"].items():
                md.append("    - %s: %s" % (p, fmt_stats(ss)))
    if "drain_kernel_ms" in r:
        md.append("")
        md.append("## Kernel quiesce+drain duration (T_drain_kernel, ms, kprobe prepare->free)")
        for k, s in r["drain_kernel_ms"].items():
            md.append("- %s: %s" % (k, fmt_stats(s)))
    if r["errors"]:
        md.append("")
        md.append("## Errors")
        md.extend("- %s" % e for e in r["errors"])
    out = os.path.join(d, "SAFETY_REPORT.md")
    open(out, "w").write("\n".join(md) + "\n")
    json.dump(r, open(os.path.join(d, "summary.json"), "w"), indent=1)
    return out

def main():
    dirs = []
    for pat in sys.argv[1:]:
        dirs.extend(sorted(glob.glob(pat)) if any(c in pat for c in "*?[") else [pat])
    if not dirs:
        print("usage: sc_analyze_bm.py results/safety_chaos_* [...]", file=sys.stderr)
        sys.exit(2)
    results = [analyze_run(d) for d in dirs]
    for d, r in zip(dirs, results):
        path = write_report(d, r)
        print("%-60s %s  gates_fail=%s  io=%s  e2e_p99(local)=%s" % (
            r["dir"], r["verdict"],
            [k for k, v in r["gates"].items() if v is False] or "-",
            r["fio"]["total_ios"],
            r["latency_e2e_ms"].get("local_switch", {}).get("p99", "-")))
        print("   report: %s" % path)
    if len(results) > 1:
        ok = sum(1 for r in results if r["verdict"] == "PASS")
        io_total = sum(r["fio"]["total_ios"] for r in results)
        sw_total = sum(r["events"]["n"] for r in results)
        drained = sum(r["kernel_log"]["drained_total"] for r in results)
        print("\nCAMPAIGN: %d/%d runs PASS, total_io=%d, switches=%d, drained_total=%d"
              % (ok, len(results), io_total, sw_total, drained))

if __name__ == "__main__":
    main()
