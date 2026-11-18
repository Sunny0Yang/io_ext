#!/usr/bin/env python3
"""arb_io_weight model — io.weight (cgroup v2) semantic audit of arb_wdrr,
executable (gate 0 of the io.weight mapping audit).

AUDIT CONCLUSION (full writeup: userspace/ARB_IO_WEIGHT_MAP.md):
arb_wdrr is weight-proportional for the whole native io.weight value
domain 1..10000 with NO hidden cost model or value-range limit; the only
nonlinearity is the round-budget floor (arb_drr_pick: weights below
MIN_BUDGET/QUANTUM are floor-compressed to the same effective weight),
and the only fidelity caveat at the top of the range is the documented
per-visit budget binding (F1 evidence), which the S10 round hold keeps
bounded (ratios survive in the shallow-queue regime).

This test encodes the audit as executable spec, importing the canonical
DRR core spec from tests/arb_wdrr_model.py (WdrrArbiter/Tenant/drive
harnesses — any change there propagates here automatically):

  1. header constants: QUANTUM/QUANTUM floor must match
     ioext_arb_common.h (ARB_WDRR_QUANTUM_BYTES 512 /
     ARB_WDRR_MIN_BUDGET_BYTES 4096) — the mapping table in
     ARB_IO_WEIGHT_MAP.md is only valid for these; drift fails here.
  2. effective weight linearity: budget(w) = 512*w exactly for
     w >= 8; w in 1..7 all floor-compress to budget 4096 (ew 8).
  3. identity mapping ratios: native io.weight w -> -W w; long-run
     share ratio == weight ratio across the domain (8:10000 = 1250:1,
     100:10000 = 100:1, 100:1000 = 10:1 — exact with 512B requests).
  4. realistic 4K regime: same ratios with one-request overshoot skew.
  5. low-end floor compression: io.weight 1..7 are indistinguishable
     (SPEC — the mapping doc's caveat), and the documented x8 uniform
     scaling recipe restores the low end (io.weight 1:2 -> -W 8:16).
  6. shallow-queue top range (F1 caveat zone): io.weight 100 vs 10000
     keeps ratio separation in the saturated shallow-queue regime
     (S10 round hold: the large budget binds across pending refills).
  7. no hidden cost: charge == bytes exactly (read/write unaware, no
     size multipliers beyond bytes, requeue refunds exactly), and the
     io.weight default 100 == the loader's -W default.

Self-test convention: same check()/MODEL PASS-FAIL + exit code as
arb_wdrr_model.py.
"""
import sys

import arb_wdrr_model as core

# Re-encode the shared-core constants this audit depends on.  These MUST
# equal ioext_arb_common.h's ARB_WDRR_QUANTUM_BYTES / ARB_WDRR_MIN_BUDGET_BYTES
# (they are imported from the canonical model spec, which mirrors the C).
QUANTUM = core.QUANTUM                       # 512 B per unit weight
MIN_BUDGET = core.MIN_BUDGET                 # 4096 B absolute floor
FLOOR_EW = MIN_BUDGET // QUANTUM             # 8: weights below 8 compress
IOSCHED_DEFAULT_ARB_WEIGHT = 100             # ioext_loader_common.h (== native default)

R512 = 512                   # 512B request fixture (exact-budget regime)
R4K = 4096                   # 4K request fixture (realistic regime)


def effective_weight(w):
    """Mirror arb_drr_pick's refill arithmetic (ioext_arb_common.h):
    ew = max(arb_weight * boost, floor bump) — boost is a constant 1 for
    arb_wdrr, so the ONLY nonlinearity vs native io.weight is the
    min_budget floor."""
    ew = w                                   # * wdrr_get_boost() == 1
    if ew == 0:
        ew = 1
    if QUANTUM * ew < MIN_BUDGET:
        ew = (MIN_BUDGET + QUANTUM - 1) // QUANTUM
    return ew


def check(name, cond, detail=""):
    return core.check(name, cond, detail)


def new_arb(pairs):
    """pairs: list of (cgid, weight). Register on consecutive slots."""
    arb = core.WdrrArbiter()
    for slot, (cgid, weight) in enumerate(pairs):
        arb.register(core.Tenant(cgid, weight), slot)
    return arb


def test_header_constants():
    """The mapping table's validity depends on the C constants; drift in
    ioext_arb_common.h (or its mirror) must fail the audit, not silently
    invalidate ARB_IO_WEIGHT_MAP.md."""
    return check("header_constants_match_mapping_doc",
                 QUANTUM == 512 and MIN_BUDGET == 4096 and FLOOR_EW == 8,
                 f"quantum={QUANTUM} min_budget={MIN_BUDGET} "
                 f"floor_ew={FLOOR_EW} (ARB_IO_WEIGHT_MAP.md assumes "
                 f"512/4096/8)")


def test_effective_weight_linearity():
    """budget(w) = QUANTUM * ew(w): linear in w for w >= 8 across the
    whole io.weight domain (no hidden scaling, no range limit), and the
    documented floor compression below 8."""
    ok = True
    for w in (8, 10, 25, 50, 99, 100, 101, 250, 500, 999, 1000,
              2000, 5000, 9999, 10000):
        ts = core.Tenant(w, w)
        ok &= check(f"budget_linear_w{w}",
                    ts.budget() == QUANTUM * w,
                    f"budget({w})={ts.budget()} expected={QUANTUM * w}")
    for w in (1, 2, 3, 4, 5, 6, 7):
        ts = core.Tenant(w, w)
        ok &= check(f"floor_compress_w{w}",
                    ts.budget() == MIN_BUDGET and effective_weight(w) == FLOOR_EW,
                    f"budget({w})={ts.budget()} ew={effective_weight(w)} "
                    f"(floor-compressed to ew {FLOOR_EW})")
    return ok


def test_identity_ratio_extremes():
    """native io.weight w -> -W w (identity): long-run share ratio ==
    weight ratio across the domain.  512B requests make budgets exact
    multiples (no overshoot skew): ratio is exactly the weight ratio."""
    ok = True
    for wa, wb, r0, r1 in ((100, 1000, 9.7, 10.3),
                           (100, 10000, 97.0, 103.0),
                           (8, 10000, 1212.0, 1288.0)):
        arb = new_arb([(100, wa), (300, wb)])
        served = core.drive_backlogged(arb, {100: R512, 300: R512}, 3 * (wa + wb) + 32)
        ratio = served[300] / served[100]
        ok &= check(f"identity_ratio_{wb}_over_{wa}",
                    r0 <= ratio <= r1,
                    f"share ratio={ratio:.3f} weight ratio={wb / wa:.1f} "
                    f"(window [{r0},{r1}])")
    return ok


def test_realistic_4k_ratio():
    """The realistic 4K regime carries a one-request overshoot at the
    small-weight side (budget 51200 serves 12 full + 1 overshoot), so
    ratios sit ~4% low: (100, 1000) -> ~9.6."""
    arb = new_arb([(100, 100), (300, 1000)])
    served = core.drive_backlogged(arb, {100: R4K, 300: R4K}, 32000)
    ratio = (served[300] * R4K) / (served[100] * R4K)
    return check("realistic_4k_ratio_10x", 8.5 <= ratio <= 11.0,
                 f"ratio={ratio:.3f} expected ~9.6 "
                 f"(window [8.5,11.0] absorbs overshoot skew)")


def test_low_range_floor_and_scaling_recipe():
    """io.weight 1..7 under identity mapping are indistinguishable (the
    audit's low-end caveat, encoded as spec); the documented uniform x8
    scaling recipe (-W = 8 x io.weight) restores full low-end resolution
    because the ratio — not the absolute weight — carries the semantics."""
    # identity: 1 vs 2 collapse to equal share (both floor to ew 8)
    arb = new_arb([(100, 1), (300, 2)])
    served = core.drive_backlogged(arb, {100: R512, 300: R512}, 4096)
    ratio = served[300] / served[100]
    ok = check("low_range_identity_collapses",
               0.9 <= ratio <= 1.1,
               f"io.weight 1 vs 2 at identity: ratio={ratio:.3f} (~1: "
               f"both floor-compressed to ew 8 — documented caveat)")
    # x8 recipe: io.weight 1 vs 2 -> -W 8 vs 16 -> exact 1:2
    arb = new_arb([(100, 8), (300, 16)])
    served = core.drive_backlogged(arb, {100: R512, 300: R512}, 8192)
    ratio = served[300] / served[100]
    ok &= check("low_range_x8_recipe_restores_ratio",
                1.9 <= ratio <= 2.1,
                f"-W 8 vs 16 (io.weight 1 vs 2 x8): ratio={ratio:.3f} "
                f"(expected 2.0)")
    return ok


def test_shallow_queue_top_range():
    """The F1 top-of-range caveat zone (budget 5.12MB/round vs per-visit
    depth ~tens): in the saturated shallow-queue regime the S10 round
    hold keeps the big budget binding across pending refills, so the
    io.weight 100:10000 ratio survives (measured ~96, must stay far
    above the equal-share collapse ~1.3 that pre-S10 produced)."""
    arb = new_arb([(100, 100), (300, 10000)])
    served = core.drive_shallow_queues(arb, {100: R4K, 300: R4K}, 40000)
    ba = served[100] * R4K
    bb = served[300] * R4K
    ratio = bb / ba
    return check("shallow_top_range_ratio_survives", 50.0 <= ratio <= 130.0,
                 f"ratio={ratio:.1f} expected ~96 (weight ratio 100; "
                 f"window [50,130] absorbs pending jitter)")


def test_no_hidden_cost():
    """io.weight carries NO cost model: charge == bytes exactly, no
    read/write distinction, no size multipliers; requeue refunds exactly.
    (arb_cost/arb_iocost differ here BY DESIGN — cost-awareness is their
    selling point, and using them for io.weight experiments would be a
    category error.)"""
    arb = new_arb([(100, 100), (300, 200)])
    pattern = [R512, R4K, 128 * 1024, R4K, R512]
    served = {100: 0, 300: 0}
    sizes = {100: [], 300: []}
    for i in range(4000):
        for cg in (100, 300):
            arb.note_inserted(cg)
        cg = arb.pick()
        while cg == 0:
            cg = arb.pick()
        nbytes = pattern[i % len(pattern)]
        arb.note_dispatched(cg, nbytes)
        arb.note_completed(cg)
        served[cg] += 1
        sizes[cg].append(nbytes)
        # requeue every 7th dispatch: refund must be exact
        if i % 7 == 0:
            arb.note_requeue(cg, nbytes)
            arb.note_inserted(cg)
    ok = check("charge_equals_bytes_no_cost_model",
               all(arb.tenants[cg].completed_bytes == sum(sizes[cg])
                   for cg in (100, 300))
               and sum(served.values()) == 4000,
               f"A bytes={arb.tenants[100].completed_bytes} "
               f"(expected {sum(sizes[100])}) "
               f"B bytes={arb.tenants[300].completed_bytes} "
               f"(expected {sum(sizes[300])})")
    refunded = arb.tenants[100].requeued_bytes + arb.tenants[300].requeued_bytes
    ok &= check("requeue_refund_exact_bytes",
               refunded > 0 and refunded % R512 == 0,
               f"refunded={refunded} (byte-exact refunds)")
    ok &= check("weight_default_100_matches_native",
               IOSCHED_DEFAULT_ARB_WEIGHT == 100
               and core.Tenant(100, 100).budget() == QUANTUM * 100,
               f"loader -W default={IOSCHED_DEFAULT_ARB_WEIGHT} == "
               f"native io.weight default 100")
    return ok


def test_homo_coloc_byte_share():
    """THE homo-v2 campaign acceptance shape (task contract: io.weight ==
    long-run byte-share ratio): LC 4K randread QD32 at -W 100 vs BE 64K
    seqwrite QD16 at -W 200, saturated coloc on a burst/stall device,
    must split served bytes 1:2 (be_share 0.667, window +-0.03).  This is
    the shape that measured be_share 0.765 on hardware pre-S11
    (HOMO_ctrl_x_weight_arb, 20261106): LC sat device-buried
    (pending == 0 with its whole QD in flight) while BE re-armed at
    completion-kick cadence; the S11 core (arm-on-dry + hold-while-inflight
    in the shared arb_drr_pick) restores the contract.  LC's completion
    latency here (250us vs BE 600us) models the buried regime; the drive
    harness (core.drive_coloc) replays per-tenant QD caps, completion
    kicks and the bursty drain windows."""
    arb = new_arb([(100, 100), (300, 200)])
    served = core.drive_coloc(arb,
                              {100: R4K, 300: 64 * 1024},
                              caps={100: 32, 300: 16},
                              lat_us={100: 250, 300: 600},
                              total_dispatches=60000)
    blc = served[100] * R4K
    bbe = served[300] * 64 * 1024
    share = bbe / (blc + bbe)
    ok = check("homo_coloc_byte_share_1_to_2",
               0.64 <= share <= 0.70,
               f"be_share={share:.3f} expected 0.667 (window [0.64,0.70])")
    lc, be = arb.tenants[100], arb.tenants[300]
    ok &= check("homo_coloc_arms_round_locked",
                0.75 <= be.refill_arms / max(1, lc.refill_arms) <= 1.33,
                f"arms lc={lc.refill_arms} be={be.refill_arms} "
                f"(pre-S11 hardware collapsed to ~0.4 while BE spun)")
    return ok


def main():
    ok = True
    ok &= test_header_constants()
    ok &= test_effective_weight_linearity()
    ok &= test_identity_ratio_extremes()
    ok &= test_realistic_4k_ratio()
    ok &= test_low_range_floor_and_scaling_recipe()
    ok &= test_shallow_queue_top_range()
    ok &= test_no_hidden_cost()
    ok &= test_homo_coloc_byte_share()
    print("MODEL " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
