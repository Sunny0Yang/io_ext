#!/usr/bin/env python3
"""Tests for the D1 v3 degraded-device rerun derivation (d1_ladder_v2_092145).

Reads the derived CSVs in results/D1_ladder_v3_figures/ and independently
re-parses a sample of raw fio JSONs from
results/D1_overhead_bm_data/d1_ladder_v2_092145/ to recompute cpu_us_per_io.
Run:  python3 -I scripts/test_d1_ladder_v3_bm.py
"""

import csv
import json
import math
from pathlib import Path

import statistics as st

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
RUN_DIR = BASE / "results" / "D1_overhead_bm_data" / "d1_ladder_v2_092145"
OUT_DIR = BASE / "results" / "D1_ladder_v3_figures"

LADDER_ARMS = ("native", "bare", "q_noop", "q_fifo", "qfifo_arb")
E4A_LAYOUTS = ("16on1cg", "8+8", "4x4", "2x8", "1x16")
E4A_STACKS = ("native", "q_noop")
E4B_N = (1, 2, 4, 8, 16)
E6_ARMS = ("q_noop", "q_fifo")


def read_csv(name):
    with open(OUT_DIR / name, newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def recompute_cpu_us_per_io(fio_path):
    """Independent reimplementation of the CPU-per-IO formula from raw json."""
    job = json.loads(Path(fio_path).read_text())["jobs"][0]
    read = job["read"]
    runtime_s = float(job["job_runtime"]) / 1000.0
    cpu_frac = (float(job["usr_cpu"]) + float(job["sys_cpu"])) / 100.0
    return cpu_frac * runtime_s / float(read["total_ios"]) * 1e6


def test_ladder_inventory():
    rows = read_csv("ladder.csv")
    assert len(rows) == 25, len(rows)
    seen = {(r["arm"], int(r["rep"])) for r in rows}
    assert seen == {(a, r) for a in LADDER_ARMS for r in range(1, 6)}
    for r in rows:
        assert float(r["iops"]) > 0 and float(r["cpu_us_per_io"]) > 0
        assert float(r["clat_p99_us"]) > float(r["clat_p50_us"]) > 0
    print("PASS ladder inventory: 25 rows, 5 arms x 5 reps")


def test_e4a_inventory_and_aggregate():
    rows = read_csv("fixedload16.csv")
    assert len(rows) == 50 * 16, len(rows)
    groups = {}
    for r in rows:
        key = (r["layout"], r["stack"], int(r["rep"]))
        groups.setdefault(key, []).append(r)
    assert set(groups) == {(l, s, rep) for l in E4A_LAYOUTS
                           for s in E4A_STACKS for rep in range(1, 6)}
    for key, jobs in groups.items():
        assert len(jobs) == 16, (key, len(jobs))
        assert sorted(int(j["job"]) for j in jobs) == list(range(1, 17))
        agg = sum(float(j["job_iops"]) for j in jobs)
        declared = float(jobs[0]["agg_iops"])
        assert abs(agg - declared) < 0.05, (key, agg, declared)
        # every row of the group carries the same aggregate
        assert all(abs(float(j["agg_iops"]) - declared) < 0.05 for j in jobs)
    print("PASS e4a inventory: 50 runs x 16 jobs; agg_iops == sum of job IOPS")


def test_e4b_inventory():
    rows = read_csv("idle.csv")
    assert len(rows) == 50, len(rows)
    seen = {(int(r["n_registered"]), r["stack"], int(r["rep"])) for r in rows}
    assert seen == {(n, s, rep) for n in E4B_N for s in E4A_STACKS
                    for rep in range(1, 6)}, "N=1..16 must all be present"
    for r in rows:
        assert 40 < float(r["clat_p99_us"]) < 100
    print("PASS e4b inventory: 50 rows, N_registered 1/2/4/8/16 all present")


def test_e6_inventory():
    pricing = read_csv("e6_pricing.csv")
    assert len(pricing) == 20, len(pricing)
    seen = {(r["arm"], r["instr"], int(r["rep"])) for r in pricing}
    assert seen == {(a, i, r) for a in E6_ARMS for i in ("off", "on")
                    for r in range(1, 6)}
    segs = read_csv("e6_segments.csv")
    assert len(segs) == 10, len(segs)
    assert {(r["arm"], int(r["rep"])) for r in segs} == {
        (a, r) for a in E6_ARMS for r in range(1, 6)}
    print("PASS e6 inventory: 20 pricing rows, 10 instr_on segment rows")


def test_e6_segments_support():
    for r in read_csv("e6_segments.csv"):
        for seg in ("insert_span", "dispatch_span", "residence"):
            cnt = int(r[f"{seg}_cnt"])
            assert cnt > 0, (r["arm"], r["rep"], seg)
            mean = float(r[f"{seg}_mean_ns"])
            p50, p99 = float(r[f"{seg}_p50_ns_lb"]), float(r[f"{seg}_p99_ns_lb"])
            # mean must lie inside the log2 histogram support implied by the
            # reported bucket lower bounds: 2^b <= span < 2^(b+1)
            assert 2 ** math.floor(math.log2(p50)) <= p50 <= p99, r
            assert p50 <= mean <= 2 * p99 or mean >= p50, r
            # stronger: mean within [p50_lb, 2*(p99_lb)) support window
            assert p50 <= mean < 2 * p99, (r["arm"], r["rep"], seg, mean, p50, p99)
        for drop in ("insert_drop", "dispatch_spurious", "residence_drop"):
            assert int(r[f"{drop}_cnt"]) >= 0
    print("PASS e6 segments: cnt>0 and mean inside histogram support")


def test_e6_pricing_pairs():
    by = {}
    for r in read_csv("e6_pricing.csv"):
        by.setdefault((r["arm"], int(r["rep"])), {})[r["instr"]] = r
    for (arm, rep), pair in by.items():
        diff = (float(pair["on"]["cpu_us_per_io"])
                - float(pair["off"]["cpu_us_per_io"]))
        declared = float(pair["on"]["paired_cpu_diff_on_minus_off"])
        assert abs(diff - declared) < 1e-3, (arm, rep, diff, declared)
        assert abs(float(pair["off"]["paired_cpu_diff_on_minus_off"])
                   - declared) < 1e-3
    print("PASS e6 pricing: paired on/off diffs consistent")


def test_cpu_us_per_io_recompute():
    """Independently recompute cpu_us_per_io for 3 sampled runs (the CSVs that
    carry the column: ladder.csv and e6_pricing.csv)."""
    samples = [
        (RUN_DIR / "ladder" / "qfifo_arb_r3" / "ta_fio.json", "ladder.csv",
         ("arm", "qfifo_arb", "rep", "3")),
        (RUN_DIR / "ladder" / "native_r5" / "ta_fio.json", "ladder.csv",
         ("arm", "native", "rep", "5")),
        (RUN_DIR / "e6" / "q_fifo_instr_on_r5" / "act_fio.json", "e6_pricing.csv",
         ("arm", "q_fifo", "instr", "on")),
    ]
    rows = {name: read_csv(name) for name in ("ladder.csv", "e6_pricing.csv")}
    for fio, csv_name, (kcol, kval, rcol, rval) in samples:
        want = recompute_cpu_us_per_io(fio)
        row = next(r for r in rows[csv_name]
                   if r[kcol] == kval and r[rcol] == rval
                   and (csv_name != "e6_pricing.csv" or r["rep"] == "5"))
        got = float(row["cpu_us_per_io"])
        assert abs(got - want) < max(1e-4, 1e-6 * want), (fio, got, want)
    # cross-check one e4a job: recomputed IOPS matches fixedload16.csv
    job = json.loads(
        (RUN_DIR / "e4a_ext" / "8+8_native_r2" / "cg7" / "j7_fio.json")
        .read_text())["jobs"][0]
    want_iops = float(job["read"]["iops"])
    row = next(r for r in read_csv("fixedload16.csv")
               if r["layout"] == "8+8" and r["stack"] == "native"
               and int(r["rep"]) == 2 and int(r["job"]) == 7)
    assert abs(float(row["job_iops"]) - want_iops) < 1e-2, (row, want_iops)
    print("PASS cpu_us_per_io independent recompute (3 sampled runs) "
          "+ e4a job IOPS cross-check")


def main():
    test_ladder_inventory()
    test_e4a_inventory_and_aggregate()
    test_e4b_inventory()
    test_e6_inventory()
    test_e6_segments_support()
    test_e6_pricing_pairs()
    test_cpu_us_per_io_recompute()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
