#!/usr/bin/env python3
"""d1_analyze_bm.py — D1 framework-overhead probe analyzer.
Parses per-IO fio lat logs (slat/clat/lat) + fio json for each probe config dir.
Usage: python3 -I d1_analyze_bm.py <probe_root> <out_csv>
"""
import json, os, sys, glob, csv, math

def pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    # Nearest-rank percentile, with endpoints clamped to the sample range.
    k = max(0, min(len(sorted_vals) - 1, math.ceil((p / 100.0) * len(sorted_vals)) - 1))
    return sorted_vals[k]

def parse_latlog(path):
    vals = []
    with open(path) as f:
        for line in f:
            parts = line.split(",")
            if len(parts) >= 2:
                try:
                    vals.append(float(parts[1]))
                except ValueError:
                    pass
    vals.sort()
    return vals

def summarize(vals):
    vals = sorted(vals)
    n = len(vals)
    if n == 0:
        return dict(n=0)
    return dict(
        n=n,
        mean=sum(vals) / n,
        p50=pct(vals, 50), p90=pct(vals, 90), p99=pct(vals, 99),
        p999=pct(vals, 99.9), p9999=pct(vals, 99.99), mx=vals[-1],
        # fio 3.41 per-IO latency logs contain nanoseconds.
        frac_gt500us=sum(1 for v in vals if v > 500_000) / n,
        frac_gt1ms=sum(1 for v in vals if v > 1_000_000) / n,
        frac_gt2ms=sum(1 for v in vals if v > 2_000_000) / n,
        frac_gt4ms=sum(1 for v in vals if v > 4_000_000) / n,
    )

def fio_json_stats(d):
    out = {}
    for jp in glob.glob(os.path.join(d, "*_fio.json")):
        try:
            j = json.load(open(jp))
        except Exception:
            continue
        for job in j.get("jobs", []):
            r = job.get("read", {})
            out["iops"] = r.get("iops")
            out["bw_kib"] = r.get("bw")
            for key in ("slat_ns", "clat_ns", "lat_ns"):
                dd = r.get(key, {})
                pl = dd.get("percentile", {})
                for pp, vv in pl.items():
                    out[f"json_{key}_{pp}"] = float(vv)
            # runtime window
            out["runtime_ms"] = (job.get("run") or {}).get("runtime") or j.get("jobs", [{}])[0].get("job runtime", {}).get("total_ms")
    return out

def analyze_dir(d):
    row = {"config": os.path.basename(d.rstrip("/")), "dir": d}
    row.update(fio_json_stats(d))
    for kind in ("slat", "clat", "lat"):
        logs = glob.glob(os.path.join(d, f"*_{kind}.*.log"))
        if not logs:
            continue
        vals = []
        for lg in logs:
            vals.extend(parse_latlog(lg))
        s = summarize(vals)
        for k, v in s.items():
            row[f"{kind}_{k}"] = v
    # per-IO lat-slat gap approximated by distribution deltas only (no pairing)
    return row

def main():
    root, outcsv = sys.argv[1], sys.argv[2]
    dirs = []
    for sub in sorted(os.listdir(root)):
        p = os.path.join(root, sub)
        if os.path.isdir(p) and glob.glob(os.path.join(p, "*.json")) or glob.glob(os.path.join(p, "*", "*_slat.*.log")):
            dirs.append(p)
    # also nested cg dirs inside B_* dirs: one row per tenant dir
    rows = []
    for d in dirs:
        if any(glob.glob(os.path.join(d, f"*_{k}.*.log")) for k in ("slat", "clat", "lat")):
            rows.append(analyze_dir(d))
        else:
            for cgd in sorted(os.listdir(d)):
                cdp = os.path.join(d, cgd)
                if os.path.isdir(cdp) and glob.glob(os.path.join(cdp, "*_slat.*.log")):
                    r = analyze_dir(cdp)
                    r["config"] = f"{os.path.basename(d)}/{cgd}"
                    rows.append(r)
    cols = ["config"]
    extra = ["iops", "bw_kib",
             "slat_n", "slat_mean", "slat_p50", "slat_p99", "slat_p999", "slat_max",
             "clat_n", "clat_mean", "clat_p50", "clat_p99", "clat_p999", "clat_max",
             "lat_n", "lat_mean", "lat_p50", "lat_p99", "lat_p999", "lat_max",
             "clat_frac_gt500us", "clat_frac_gt1ms", "clat_frac_gt2ms", "clat_frac_gt4ms",
             "slat_frac_gt500us", "slat_frac_gt1ms",
             "json_slat_ns_99.000000", "json_clat_ns_99.000000", "json_lat_ns_99.000000"]
    for r in rows:
        for c in r:
            if c not in cols:
                cols.append(c)
    with open(outcsv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"wrote {outcsv} with {len(rows)} rows")
    for r in rows:
        print(f"{r['config']:40s} iops={r.get('iops')} "
              f"slat_p99={r.get('slat_p99')} clat_p99={r.get('clat_p99')} lat_p99={r.get('lat_p99')} "
              f"clat_p50={r.get('clat_p50')} slat_p50={r.get('slat_p50')}")

if __name__ == "__main__":
    main()
