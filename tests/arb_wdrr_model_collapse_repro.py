#!/usr/bin/env python3
"""Reproduce the P1 cg16 collapse against the CURRENT reference model.

Simulates the measured hardware regime (D2 cg16 20261026 telemetry):
16 tenants, weights 100..1600, shallow per-key queues (queue_cap ~20,
device-pipeline holds the rest), completion-driven refills.  The hctx
round counter spins on all-drain scans, so refilled_round goes stale and
turn grants degenerate to 'refill whenever deficit <= 0'.  Expected
failure signature (matches the tenant_state map dumps): served ratios
compress from 16:1 to ~2:1 and refill_arms scale ~1/w.

Run against BOTH the pre-fix and post-fix model: pre-fix must FAIL the
ratio gate, post-fix must PASS.
"""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import importlib.util

spec = importlib.util.spec_from_file_location(
    "arb_wdrr_model", os.path.join(os.path.dirname(os.path.abspath(__file__)), "arb_wdrr_model.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

import random


def drive_cg16(regime):
    """16 tenants w=100..1600, shallow queues, completion-driven inserts.

    regime='prefix': model as-is (round spins on all-drain scans).
    """
    arb = m.WdrrArbiter()
    tenants = []
    for i in range(1, 17):
        t = m.Tenant(1000 + i, i * 100)
        arb.register(t, i - 1)
        tenants.append(t)

    QUEUE_CAP = 8           # per-(tenant,hctx) pools are tiny on hardware
                            # (measured pending ~0-5 at snapshot instants)
    DEVICE_LAT = 60         # completion round-trip in pick slots
    JITTER = 60
    KICKS = 4               # completion-driven kicks fire picks even while
                            # the local pools are dry -> all-drain scans
                            # -> round spin (measured 246 rounds/s vs ~8/s
                            # full-consumption rounds on cg16 hardware)
    TOTAL = 60000
    rng = random.Random(42)

    for t in tenants:
        for _ in range(QUEUE_CAP):
            arb.note_inserted(t.cgid)

    served = {t.cgid: 0 for t in tenants}
    returns = []
    clock = 0
    n = 0
    idle = 0
    while n < TOTAL:
        clock += 1
        for e in [e for e in returns if e[0] <= clock]:
            returns.remove(e)
            if arb.tenants[e[1]].pending < QUEUE_CAP:
                arb.note_inserted(e[1])
        for _ in range(KICKS):
            cg = arb.pick()
            if cg == 0:
                if all(arb.tenants[t.cgid].pending == 0 for t in tenants):
                    idle += 1
                    if idle > 6 * QUEUE_CAP * 16 * KICKS:
                        raise RuntimeError("drive_cg16 starved")
                continue
            arb.note_dispatched(cg, 4096)
            returns.append([clock + DEVICE_LAT + rng.randint(0, JITTER), cg])
            served[cg] += 1
            n += 1
            idle = 0
            break
    return served, arb


def main():
    served, arb = drive_cg16("prefix")
    w = {t.cgid: t.weight for t in arb.tenants.values()}
    cgs = sorted(w, key=lambda c: w[c])
    lo = served[cgs[0]]          # w=100
    hi = served[cgs[-1]]         # w=1600
    ratio = hi / lo
    refills = {c: arb.tenants[c].refill_arms for c in cgs}
    refill_ratio = refills[cgs[0]] / max(refills[cgs[-1]], 1)
    print(f"served w100={lo} w1600={hi} ratio={ratio:.2f} (expected ~16)")
    print(f"refills w100={refills[cgs[0]]} w1600={refills[cgs[-1]]} "
          f"ratio={refill_ratio:.1f} (collapse signature: >>1)")
    ok = True
    if ratio < 8.0:
        print(f"FAIL: service ratio {ratio:.2f} << 16: weighted fairness collapsed")
        ok = False
    else:
        print(f"PASS: service ratio {ratio:.2f} close to 16")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()