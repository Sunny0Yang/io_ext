#!/usr/bin/env python3
"""Summarize a window-aligned YCSB-A/FIO M2 campaign and render pressure plots."""
from __future__ import annotations
import argparse, csv, json, math
from pathlib import Path
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def _log_window_avg_mibs(log_path: Path, begin: float, end: float):
    integral = duration = 0.0
    count = 0
    previous = None
    for line in log_path.read_text().splitlines():
        fields = [float(x.strip()) for x in line.split(",")]
        timestamp, rate = fields[0], fields[1]
        if previous is not None and timestamp <= previous:
            raise ValueError(f"Nonmonotonic FIO log: {log_path}")
        if previous is not None and previous >= begin and timestamp <= end:
            interval = (timestamp - previous) / 1000
            integral += (rate / 1024) * interval
            duration += interval
            count += 1
        previous = timestamp
    if duration <= 0:
        return 0.0, 0.0, 0
    return integral / duration, duration, count


def background_window(case: Path, data: dict):
    offered = data.get("offered_mib_s")
    if offered in (None, 0, 0.0):
        return 0.0, 1.0, 0
    begin = data["foreground_start_epoch"] * 1000
    end = data["foreground_end_epoch"] * 1000
    logs = sorted(case.glob("background_bw*.log"))
    if not logs:
        raise ValueError(f"Missing background bandwidth log(s): {case}")
    total_mibs = 0.0
    max_duration = 0.0
    sample_count = 0
    for log in logs:
        avg_mibs, duration, count = _log_window_avg_mibs(log, begin, end)
        total_mibs += avg_mibs
        max_duration = max(max_duration, duration)
        sample_count += count
    coverage = max_duration / ((end - begin) / 1000)
    if coverage < 0.7:
        raise ValueError(f"Insufficient bandwidth-window coverage {coverage:.3f}: {case}")
    return total_mibs, coverage, sample_count


def read_results(root: Path):
    rows=[]
    for p in sorted(root.glob("*/result.json")):
        d=json.loads(p.read_text())
        app=d.get("application") or {}
        def metric(op,key): return (app.get(op) or {}).get(key)
        achieved,coverage,log_samples=background_window(p.parent,d)
        rows.append({
            "case": p.parent.name,
            "status": d.get("status"),
            "scheduler": d.get("scheduler"),
            "offered_mib_s": float(d.get("offered_mib_s",0)),
            "fio_iodepth": d.get("fio_iodepth"),
            "fio_numjobs": d.get("fio_numjobs"),
            "repeat": int(d.get("repeat",0)),
            "achieved_mib_s": achieved,
            "background_window_samples": log_samples,
            "background_window_coverage": coverage,
            "read_p99_us": metric("READ","99thPercentileLatency(us)"),
            "update_p99_us": metric("UPDATE","99thPercentileLatency(us)"),
            "read_ops": metric("READ","Operations"),
            "update_ops": metric("UPDATE","Operations"),
            "ycsb_ops_s": (app.get("OVERALL") or {}).get("Throughput(ops/sec)"),
            "ycsb_runtime_ms": (app.get("OVERALL") or {}).get("RunTime(ms)"),
            "read_return_ok": metric("READ","Return=OK"),
            "update_return_ok": metric("UPDATE","Return=OK"),
            "run_start_epoch": d.get("foreground_start_epoch"),
            "run_end_epoch": d.get("foreground_end_epoch"),
            "error": d.get("error", ""),
        })
    return rows


def f(v): return "" if v is None else v


def write_csv(rows, path):
    fields=list(rows[0]) if rows else ["case"]
    with path.open("w",newline="") as h:
        w=csv.DictWriter(h,fieldnames=fields); w.writeheader(); w.writerows(rows)


def summary(rows):
    good=[r for r in rows if r["status"]=="success"]
    groups=defaultdict(list)
    for r in good:
        groups[(r["scheduler"], r["offered_mib_s"], r.get("fio_iodepth"), r.get("fio_numjobs"))].append(r)
    out=[]
    for (sched, offered, iodepth, numjobs), items in sorted(groups.items()):
        def vals(k): return np.array([float(x[k]) for x in items if x[k] is not None],dtype=float)
        rec={
            "scheduler": sched,
            "offered_mib_s": offered,
            "fio_iodepth": iodepth,
            "fio_numjobs": numjobs,
            "n": len(items),
            "cases": [x["case"] for x in items],
        }
        for key in ("achieved_mib_s","read_p99_us","update_p99_us","read_ops","update_ops","ycsb_ops_s","ycsb_runtime_ms"): 
            v=vals(key)
            rec[key+"_mean"]=float(np.mean(v)) if len(v) else None
            rec[key+"_sd"]=float(np.std(v,ddof=1)) if len(v)>1 else None
        out.append(rec)
    return out


def plot(summary_rows, output):
    output.mkdir(parents=True,exist_ok=True)
    groups=defaultdict(list)
    for r in summary_rows: groups[r["scheduler"]].append(r)
    fig,axes=plt.subplots(1,2,figsize=(12,4.8),constrained_layout=True)
    colors={"none":"#1f77b4","kyber":"#ff7f0e","mq-deadline":"#2ca02c","bfq":"#d62728"}
    for sched,items in sorted(groups.items()):
        items=sorted(items,key=lambda r:r["achieved_mib_s_mean"])
        x=np.array([r["achieved_mib_s_mean"] for r in items],float)
        for ax,key,label in ((axes[0],"read_p99_us","YCSB-A READ P99 (ms)"),(axes[1],"update_p99_us","YCSB-A UPDATE P99 (ms)")):
            y=np.array([r[key+"_mean"]/1000 for r in items],float)
            sd=np.array([r[key+"_sd"]/1000 if r[key+"_sd"] is not None else 0 for r in items],float)
            ax.errorbar(x,y,yerr=sd,marker="o",capsize=3,label=sched,color=colors.get(sched),linewidth=1.8)
            ax.set_xlabel("Background FIO achieved throughput (MiB/s)")
            ax.set_ylabel(label); ax.grid(alpha=.25)
    axes[0].set_title("Foreground read tail vs. measured background load")
    axes[1].set_title("Foreground update tail vs. measured background load")
    axes[0].legend(frameon=False,fontsize=9)
    fig.suptitle("M2 YCSB-A foreground under FIO background pressure (sample SD; observed points only)")
    fig.savefig(output/"m2_pressure_ycsb_p99.png",dpi=200,bbox_inches="tight")
    plt.close(fig)
    fig,ax=plt.subplots(figsize=(6.5,4.5),constrained_layout=True)
    def offered_x(row):
        offered = row["offered_mib_s"]
        return offered if offered >= 0 else max((r["achieved_mib_s_mean"] or 0 for r in summary_rows), default=0) * 1.05

    for sched,items in sorted(groups.items()):
        items=sorted(items,key=lambda r:(r["offered_mib_s"] if r["offered_mib_s"] >= 0 else 10**9, r["achieved_mib_s_mean"] or 0))
        xs = [offered_x(r) for r in items]
        ax.plot(xs, [r["achieved_mib_s_mean"] for r in items], marker="o", label=sched, color=colors.get(sched))
        for x, row in zip(xs, items):
            if row["offered_mib_s"] < 0:
                ax.annotate("max", (x, row["achieved_mib_s_mean"]), textcoords="offset points", xytext=(4, 4), fontsize=8)
    ax.set_xlabel("Offered FIO rate (MiB/s; max = unlimited)"); ax.set_ylabel("Achieved FIO throughput (MiB/s)")
    ax.set_title("Offered-to-achieved background throughput")
    ax.grid(alpha=.25); ax.legend(frameon=False)
    fig.savefig(output/"m2_pressure_achieved_throughput.png",dpi=200,bbox_inches="tight")
    plt.close(fig)
    fig,ax=plt.subplots(figsize=(6.5,4.5),constrained_layout=True)
    for sched,items in sorted(groups.items()):
        items=sorted(items,key=lambda r:r["achieved_mib_s_mean"])
        ax.errorbar([r["achieved_mib_s_mean"] for r in items],[r["ycsb_ops_s_mean"] for r in items],
                    yerr=[r["ycsb_ops_s_sd"] or 0 for r in items],marker="o",capsize=3,label=sched,color=colors.get(sched))
    ax.set_xlabel("Background FIO achieved throughput (MiB/s)"); ax.set_ylabel("YCSB-A throughput (ops/s)")
    ax.set_title("Foreground application throughput")
    ax.grid(alpha=.25); ax.legend(frameon=False)
    fig.savefig(output/"m2_pressure_ycsb_throughput.png",dpi=200,bbox_inches="tight")
    plt.close(fig)


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--input",type=Path,required=True); ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args(); a.output.mkdir(parents=True,exist_ok=True)
    rows=read_results(a.input); write_csv(rows,a.output/"m2_campaign_cases.csv")
    s=summary(rows); (a.output/"m2_campaign_summary.json").write_text(json.dumps(s,indent=2))
    if s: plot(s,a.output)
    print(f"cases={len(rows)} successful={sum(r['status']=='success' for r in rows)} summary_groups={len(s)}")

if __name__=="__main__": main()
