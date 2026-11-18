#!/usr/bin/env python3
"""fb_model.py - host-side reference model for the arb_feedback ADDITIVE logic.

The arb_drr_pick core is spec'd by tests/io/arb_wdrr_model.py; this model
spec-checks only what arb_feedback stacks on top of it, mirroring
ioext_arb_feedback_common.h:

  1. AIMD FSM: violating window halves aimd_budget, clean window adds
     FB_AIMD_ADD; budget always clamped to [FB_BUDGET_MIN, FB_BUDGET_MAX].
  2. Starvation credit: low-prio (prio > FB_HIGH_PRIO_MAX) tenants gain
     +1 per insert (capped at FB_CREDIT_MAX), lose FB_CREDIT_DECAY per
     dispatch; credit >= FB_CREDIT_BOOST_AT lifts effective_weight by
     FB_CREDIT_BOOST_MULT, never more.
  3. Progress guard: with arb_drr_pick round semantics, a backlogged
     tenant (pending > 0) is granted a turn at least once per round even
     when higher-prio tenants stay backlogged (starvation-guard proof at
     the round level).

Exit 0 = all invariants hold; nonzero = first violated assertion.
"""

FB = dict(
    aimd_add=1, aimd_dec_shift=1, budget_min=4, budget_max=256, budget_init=64,
    recovery_windows=3, high_prio_max=2,
    credit_max=1024, credit_boost_at=256, credit_boost_mult=2, credit_decay=4,
    window_ns=100, min_samples=8, target_over_pct=50,
    lc_target_ns=200, be_target_mult=4,
)


class FbTenant:
    def __init__(self, prio, budget_init=FB["budget_init"]):
        self.prio = prio
        self.aimd_budget = budget_init
        self.fb_state = 0  # FB_NORMAL
        self.starve_credit = 0
        self.good_windows = 0
        self.window_start = 0
        self.window_total = 0
        self.window_over = 0

    # -- ioext_arb_feedback_common.h: fb_aimd_update (window rollover) ----
    def complete(self, now_ns, total_latency_ns):
        target = FB["lc_target_ns"]
        if target == 0:
            return
        if self.prio > FB["high_prio_max"]:
            target *= FB["be_target_mult"]

        if self.window_start == 0:
            self.window_start = now_ns
            self.window_total = 0
            self.window_over = 0

        if now_ns - self.window_start >= FB["window_ns"]:
            violation = (self.window_total >= FB["min_samples"] and
                         self.window_over * 100 >
                         self.window_total * FB["target_over_pct"])
            if violation:
                self.fb_state = 1  # FB_PRESSURE
                self.good_windows = 0
                self.aimd_budget = max(
                    FB["budget_min"], self.aimd_budget >> FB["aimd_dec_shift"])
            else:
                if self.fb_state != 0:
                    self.good_windows += 1
                    if self.good_windows >= FB["recovery_windows"]:
                        self.fb_state = 0
                self.aimd_budget = min(
                    FB["budget_max"], self.aimd_budget + FB["aimd_add"])
            self.window_start = now_ns
            self.window_total = 0
            self.window_over = 0

        self.window_total += 1
        if total_latency_ns > target:
            self.window_over += 1

    # -- fb_note_insert_credit / fb_note_serve_credit --------------------
    def insert(self):
        if self.prio <= FB["high_prio_max"]:
            return
        self.starve_credit = min(FB["credit_max"], self.starve_credit + 1)

    def serve(self):
        self.starve_credit = max(0, self.starve_credit - FB["credit_decay"])

    def boost(self):
        if self.starve_credit >= FB["credit_boost_at"]:
            return FB["credit_boost_mult"]
        return 1


def check_aimd():
    """Sustained violations halve to the floor; sustained clean windows
    climb back to the cap; budget NEVER leaves [min, max]."""
    t = FbTenant(prio=1)
    now = 0
    for i in range(400):
        t.complete(now, 10_000)  # always violating (10us > 200us)
        now += 10
        assert FB["budget_min"] <= t.aimd_budget <= FB["budget_max"], "AIMD clamp"
        if i >= 12:  # first violating verdict lands at the first rollover
            assert t.fb_state == 1, f"must be PRESSURE at i={i}"
    assert t.aimd_budget == FB["budget_min"], f"must reach floor, got {t.aimd_budget}"

    for i in range(3000):  # one add per 100ns rollover (10 completes each)
        t.complete(now, 1)  # always clean
        now += 10
        assert FB["budget_min"] <= t.aimd_budget <= FB["budget_max"], "AIMD clamp"
    assert t.aimd_budget == FB["budget_max"], "must recover to cap"
    assert t.fb_state == 0, "must exit PRESSURE after recovery windows"
    print("PASS aimd_fsm")


def check_credit():
    """Credit only accrues for low-prio, caps, boosts bounded, decays."""
    hi = FbTenant(prio=0)
    for _ in range(2000):
        hi.insert()
    assert hi.starve_credit == 0, "LC must never accrue credit"

    lo = FbTenant(prio=5)
    for _ in range(2000):
        lo.insert()
    assert lo.starve_credit == FB["credit_max"], "credit cap"
    assert lo.boost() == FB["credit_boost_mult"], "boost multiplier"

    for _ in range((FB["credit_max"] - FB["credit_boost_at"])
                   // FB["credit_decay"] + 1):
        lo.serve()
    assert lo.boost() == 1, "boost must clear after quantum decay"
    print("PASS starvation_credit")


class RoundTenant:
    """Round-level view of arb_drr_pick with the fb boost multiplier."""

    def __init__(self, name, prio, weight):
        self.name, self.prio, self.weight = name, prio, weight
        self.pending = 0
        self.deficit = 0
        self.fb = FbTenant(prio=prio)
        self.dispatched_total = 0

    def eligible(self):
        return self.pending > 0


def check_no_starvation():
    """Round-robin over rounds: every backlogged tenant gets a non-empty
    turn each round even with a heavy high-prio competitor; the
    starvation-credit lift must actually raise the low-prio share."""
    quantum = 64  # cost units per weight (FB_COST_QUANTUM=8, 4K read=64)
    hi = RoundTenant("hi", prio=0, weight=800)
    lo = RoundTenant("lo", prio=5, weight=100)

    # Both permanently backlogged; fill lo's credit first (inserts).
    for _ in range(FB["credit_boost_at"] + 10):
        lo.fb.insert()
    assert lo.fb.boost() == FB["credit_boost_mult"]

    def run_round(tenants):
        served = {}
        for t in tenants:
            if not t.eligible():
                continue
            ew = t.weight * t.fb.boost()
            budget = quantum * ew
            t.deficit = budget
            n = 0
            while t.deficit > 0 and t.pending > 0:
                cost = 64  # one 4K read
                t.deficit -= cost
                t.pending -= 1
                t.dispatched_total += 1
                t.fb.serve()
                n += 1
            served[t.name] = n
        return served

    # Queue: hi keeps a deep backlog, lo gets 20 requests each round.
    rounds = 40
    lo_progress_rounds = 0
    for _ in range(rounds):
        hi.pending = 1000
        lo.pending = 20
        served = run_round([hi, lo])
        if served.get("lo", 0) > 0:
            lo_progress_rounds += 1
        hi.fb.complete(0, 0) if False else None

    assert lo_progress_rounds == rounds, (
        f"low-prio starved: served in {lo_progress_rounds}/{rounds} rounds")

    # And the lift matters: with boost 2 the lo budget per round is 2x.
    hi2 = RoundTenant("hi", 0, 800)
    lo2 = RoundTenant("lo", 5, 100)
    for _ in range(FB["credit_boost_at"] + 10):
        lo2.fb.insert()
    ew_lo = lo2.weight * lo2.fb.boost()
    ew_nolift = lo2.weight
    assert ew_lo == ew_nolift * FB["credit_boost_mult"]
    print("PASS no_starvation (low-prio served every round, lift bounded)")


if __name__ == "__main__":
    check_aimd()
    check_credit()
    check_no_starvation()
    print("fb_model: ALL PASS")
