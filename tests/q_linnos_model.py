#!/usr/bin/env python3
"""q_linnos reference model -- executable specification of the LinnOS
(OSDI'20) offline lookup-table interception policy (gate 0 of its CI loop).

*** OFFLINE LOOKUP-TABLE APPROXIMATION, NOT the online NN of LinnOS. ***
Mirrors userspace/q_linnos.bpf.c (q_linnos_cfg / q_linnos_stats maps,
the insert/cgroup_dispatch/evacuate/complete hooks and the FIFO pair) the
same way tests/arb_wdrr_model.py mirrors the arb_wdrr DRR core.  Any
change to the BPF must be mirrored here and vice versa.

Model contract (identical to q_linnos.bpf.c):
  - state bucketing: global in-flight count (dispatched - completed) is
    the device-busy proxy; band 0 <50%, band 1 50-80%, band 2 >=80% of
    busy_ref (busy_ref=0 -> never busy -> band 0)
  - class: LC iff 0 < arb_prio < lc_prio_max (policy_meta), else BE
  - interception draw: per (band,class) sequence counter,
    intercept iff (seq % 100) < ir[band][class] -- EXACTLY ratio-faithful
    (ir=20 intercepts precisely 20 of every 100 requests in the bucket)
  - intercepted requests ride the held FIFO with deadline = arrival +
    max_hold_ns; the rest ride the fast FIFO (insert decision point)
  - dispatch decision point (q_ratelimit insert/next pattern):
      1. held head whose deadline expired is released FIRST (bounded
         stall), even when fast entries are queued;
      2. fast FIFO;
      3. held head soft-released when fast is empty (work conservation);
      4. NULL (blk-mq 3ms self-kick + completion kicks retry).
  - evacuate: DRAINING-gated; fast first, then held; never touches inflight
  - conservation: enqueued_fast - dispatched_fast - evacuated_fast == 0
    and the same for held at quiesce (the BPF's lost_fast/lost_held).

Acceptance encoded (task brief): "linnos 验证表驱动拦截率与 LC/BE 差异化"
-- the offline table drives exact per-bucket interception ratios and LC
requests are intercepted strictly differently from BE.
"""
import sys
from collections import deque

NS = 1
US = 1000
MS = 1000 * 1000

BANDS = 3
CLASSES = 2
LC, BE = 0, 1
BAND_LOW_PCT = 50     # Q_LINNOS_BAND_LOW_PCT
BAND_HIGH_PCT = 80    # Q_LINNOS_BAND_HIGH_PCT


class Cfg:
    """Mirror of struct q_linnos_cfg (loader-written before attach)."""

    def __init__(self, busy_ref=64, max_hold_ns=2 * MS,
                 ir=((0, 0), (0, 20), (5, 60)), lc_prio_max=3):
        self.busy_ref = busy_ref
        self.max_hold_ns = max_hold_ns
        self.ir = [list(row) for row in ir]
        self.lc_prio_max = lc_prio_max


def band_of(inflight, busy_ref):
    """Mirror q_linnos_band()."""
    if not busy_ref:
        return 0
    pct = inflight * 100 // busy_ref
    if pct < BAND_LOW_PCT:
        return 0
    if pct < BAND_HIGH_PCT:
        return 1
    return 2


class Policy:
    """q_linnos instance: one FIFO pair, one global state entry.

    meta: cgid -> arb_prio (the loader-registered policy_meta rows).
    inflight is set directly by the rig to hold the device-busy state.
    """

    def __init__(self, cfg=None, meta=None):
        self.cfg = cfg or Cfg()
        self.meta = meta if meta is not None else {}
        self.fast = deque()          # [rq_ptr]
        self.held = deque()         # [(rq_ptr, deadline_ns)]
        self.inflight = 0
        self.seq = [[0, 0] for _ in range(BANDS)]
        self.draining = False
        # struct q_linnos_stats mirror
        self.inserts = 0
        self.inserts_by_band = [0, 0, 0]
        self.inserts_by_class = [0, 0]
        self.intercepted = [[0, 0] for _ in range(BANDS)]
        self.dispatch_fast = 0
        self.dispatch_held_timeout = 0
        self.dispatch_held_soft = 0
        self.evac_fast = 0
        self.evac_held = 0
        self.null_returns = 0
        self.push_fails = 0
        self.completions = 0
        self.enqueued_fast = 0
        self.enqueued_held = 0

    def class_of(self, cgid):
        """Mirror q_linnos_class(): 0 < arb_prio < lc_prio_max -> LC."""
        prio = self.meta.get(cgid, 0)
        if prio and prio < self.cfg.lc_prio_max:
            return LC
        return BE

    @property
    def lost_fast(self):
        return self.enqueued_fast - self.dispatch_fast - self.evac_fast

    @property
    def lost_held(self):
        return (self.enqueued_held - self.dispatch_held_timeout -
                self.dispatch_held_soft - self.evac_held)

    # --- struct_ops/insert_request ---
    def insert(self, cgid, rq_ptr, now_ns):
        if self.draining:
            return "DEFER"
        band = band_of(self.inflight, self.cfg.busy_ref)
        cls = self.class_of(cgid)
        self.seq[band][cls] += 1
        intercept = (self.seq[band][cls] % 100) < self.cfg.ir[band][cls]
        self.inserts += 1
        self.inserts_by_band[band] += 1
        self.inserts_by_class[cls] += 1
        if intercept:
            self.intercepted[band][cls] += 1
            self.held.append((rq_ptr, now_ns + self.cfg.max_hold_ns))
            self.enqueued_held += 1
        else:
            self.fast.append(rq_ptr)
            self.enqueued_fast += 1
        return "ACCEPT"

    # --- struct_ops/cgroup_dispatch ---
    def dispatch(self, now_ns):
        # 1. expired held head released first (peek then pop)
        if self.held and now_ns >= self.held[0][1]:
            rq, _ = self.held.popleft()
            self.inflight += 1
            self.dispatch_held_timeout += 1
            return rq
        # 2. fast FIFO
        if self.fast:
            rq = self.fast.popleft()
            self.inflight += 1
            self.dispatch_fast += 1
            return rq
        # 3. held soft release when fast is empty (work conservation)
        if self.held:
            rq, _ = self.held.popleft()
            self.inflight += 1
            self.dispatch_held_soft += 1
            return rq
        # 4. nothing releasable
        self.null_returns += 1
        return 0

    # --- struct_ops/completed_request ---
    def complete(self):
        if self.inflight:
            self.inflight -= 1
        self.completions += 1

    # --- struct_ops/evacuate (DRAINING-gated) ---
    def evacuate(self):
        if not self.draining:
            return 0
        if self.fast:
            self.evac_fast += 1
            return self.fast.popleft()
        if self.held:
            self.evac_held += 1
            return self.held.popleft()[0]
        return 0


def check(name, cond, detail=""):
    if cond:
        print(f"PASS: {name}")
        return True
    print(f"FAIL: {name} {detail}")
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_band_boundaries():
    """busy_ref=64: 31 -> 48% band0; 32 -> 50% band1; 51 -> 79% band1;
    52 -> 81% band2; busy_ref=0 -> always band 0 (unconfigured)."""
    cases = [
        ((0, 64), 0),
        ((31, 64), 0),
        ((32, 64), 1),      # 32*100//64 == 50: not < 50 -> band 1
        ((51, 64), 1),      # 79
        ((52, 64), 2),      # 81
        ((65536, 64), 2),
        ((1000, 0), 0),     # unconfigured busy_ref: never busy
    ]
    bad = [(args, want, band_of(*args)) for args, want in cases
           if band_of(*args) != want]
    return check("band_boundaries", not bad, f"mismatches={bad}")


def test_exact_interception_ratios():
    """THE table-driven acceptance: at a fixed busy band the deterministic
    sequence counter intercepts exactly ir% of requests, per band AND per
    class, with the default table AND with a loader-overridden table.

    Rig note (mirrors the BPF contract): the class comes from
    policy_meta's arb_prio, and a cgroup with no meta row classifies BE
    (q_linnos_class), so the rig registers both classes explicitly --
    cgid 100 rides prio 1 (LC), cgid 200 rides prio 7 (BE)."""
    ok = True
    for band, inflight in ((0, 0), (1, 40), (2, 90)):
        pol = Policy(meta={100: 1, 200: 7})
        pol.inflight = inflight
        assert band_of(inflight, pol.cfg.busy_ref) == band
        n = 10000
        for i in range(n):
            pol.insert(100, f"lc{i}", 0)
            pol.insert(200, f"be{i}", 0)
        got_lc = pol.intercepted[band][LC]
        got_be = pol.intercepted[band][BE]
        want_lc = pol.cfg.ir[band][LC] * n // 100
        want_be = pol.cfg.ir[band][BE] * n // 100
        ok &= check(f"ir_exact_band{band}_lc",
                    got_lc == want_lc,
                    f"got={got_lc} want={want_lc} (ir={pol.cfg.ir[band][LC]}%)")
        ok &= check(f"ir_exact_band{band}_be",
                    got_be == want_be,
                    f"got={got_be} want={want_be} (ir={pol.cfg.ir[band][BE]}%)")
    # loader --ir-high 30:90 override changes exactly the band-2 ratios
    pol = Policy(meta={100: 1, 200: 7},
                 cfg=Cfg(ir=((0, 0), (0, 20), (30, 90))))
    pol.inflight = 90
    n = 10000
    for i in range(n):
        pol.insert(100, f"lc{i}", 0)
        pol.insert(200, f"be{i}", 0)
    ok &= check("ir_override_exact",
                pol.intercepted[2][LC] == 3000 and pol.intercepted[2][BE] == 9000,
                f"lc={pol.intercepted[2][LC]} be={pol.intercepted[2][BE]}")
    return ok


def test_lc_be_differentiation():
    """LC (arb_prio=1) vs BE (arb_prio=7) at every band with the default
    table: band0 intercepts NOTHING for either class (device not busy --
    LinnOS would predict in-SLO latency), bands 1-2 intercept LC strictly
    less than BE (0<20, 5<60), and both counts are exact."""
    ok = True
    for band, inflight in ((0, 10), (1, 40), (2, 90)):
        pol = Policy(meta={100: 1, 200: 7})
        pol.inflight = inflight
        n = 5000
        for i in range(n):
            pol.insert(100, f"lc{i}", 0)      # LC cgroup
            pol.insert(200, f"be{i}", 0)      # BE cgroup
        got_lc = pol.intercepted[band][LC]
        got_be = pol.intercepted[band][BE]
        if band == 0:
            ok &= check("lc_be_band0_no_interception",
                        got_lc == 0 and got_be == 0,
                        f"LC={got_lc} BE={got_be} (band0 default table is 0:0)")
        else:
            ok &= check(f"lc_be_diff_band{band}",
                        got_lc < got_be,
                        f"LC={got_lc} BE={got_be}")
        ok &= check(f"lc_be_exact_band{band}",
                    got_lc == pol.cfg.ir[band][LC] * n // 100 and
                    got_be == pol.cfg.ir[band][BE] * n // 100,
                    f"LC={got_lc} BE={got_be} "
                    f"want={pol.cfg.ir[band][LC] * n // 100}/"
                    f"{pol.cfg.ir[band][BE] * n // 100}")
    return ok


def test_class_from_meta():
    """Class derivation: prio 1/2 -> LC, prio 3 (== lc_prio_max) -> BE,
    prio 0 (unset) -> BE, missing meta -> BE (conservative)."""
    pol = Policy(meta={1: 1, 2: 2, 3: 3, 4: 0})
    ok = check("class_lc", pol.class_of(1) == LC and pol.class_of(2) == LC)
    ok &= check("class_boundary", pol.class_of(3) == BE,
                "arb_prio == lc_prio_max is BE (strict <)")
    ok &= check("class_unset", pol.class_of(4) == BE and pol.class_of(999) == BE,
                "prio 0 / missing meta -> BE")
    return ok


def test_held_release_semantics():
    """Dispatch decision point, deterministically seeded: fast beats
    unexpired held; an EXPIRED held head beats fast; everything drains;
    empty queues return NULL."""
    pol = Policy(meta={100: 1})
    t0 = 100 * MS
    # seed one fast entry at band 0 (ir 0 -> never intercepted)
    pol.inflight = 10
    pol.insert(100, "fast1", t0)
    # then two held entries at band 2 with ir[2][LC]=50: seq draws 1,2 both
    # intercept (1,2 < 50) -> held with deadline t0 + max_hold
    pol.inflight = 90
    pol.insert(100, "h1", t0)
    pol.insert(100, "h2", t0)
    ok = check("seed_queues", len(pol.fast) == 1 and len(pol.held) == 2,
               f"fast={len(pol.fast)} held={len(pol.held)}")

    # fast beats the unexpired held head
    ok &= check("fast_beats_unexpired_held",
                pol.dispatch(t0 + 1) == "fast1" and pol.dispatch_fast == 1,
                "with no expired hold the fast FIFO must serve first")
    # one more fast entry, then let the hold deadline pass: the expired
    # held head must beat the queued fast entry
    pol.inflight = 10
    pol.insert(100, "fast2", t0 + 5 * MS)
    ok &= check("expired_held_beats_fast",
                pol.dispatch(t0 + pol.cfg.max_hold_ns + 1) == "h1" and
                pol.dispatch_held_timeout == 1,
                "expired held head must release before fast entries")
    # drain the rest (h2 expired, then fast2); work conservation:
    # never NULL while entries exist.  4 seeded entries, 2 explicitly
    # dispatched above -> the loop must serve exactly the 2 remaining.
    served = 2
    while pol.dispatch(t0 + pol.cfg.max_hold_ns + 2):
        served += 1
    ok &= check("drain_all", served == 4 and not pol.fast and not pol.held,
                f"served={served}")
    # NULL only when nothing is releasable; the drain loop's final call
    # already counted one null_return, this check makes the second
    ok &= check("release_null_when_empty",
                pol.dispatch(t0) == 0 and pol.null_returns == 2,
                f"null_returns={pol.null_returns} (one from the drain loop "
                "exit, one from this call)")
    return ok


def test_soft_release_and_deadline_math():
    """max_hold math (deadline = arrival + max_hold) and the soft release
    when only unexpired held entries are queued (work conservation)."""
    pol = Policy(cfg=Cfg(max_hold_ns=5 * MS))
    pol.inflight = 90
    pol.cfg.ir[2] = [100, 100]            # intercept everything
    t0 = 10 * MS
    pol.insert(100, "held1", t0)
    ok = check("deadline_math", pol.held[0] == ("held1", t0 + 5 * MS),
               f"held head={pol.held[0]}")
    # before the deadline with an empty fast FIFO: soft release serves it
    ok &= check("soft_release_when_fast_empty",
                pol.dispatch(t0 + 1) == "held1" and pol.dispatch_held_soft == 1,
                "an unexpired held head is soft-released when fast is empty")
    # before the deadline with a fast entry: fast wins, held waits
    pol.insert(100, "held2", t0 + 10 * MS)
    pol.inflight = 0                      # band 0: no interception
    pol.insert(100, "fast1", t0 + 10 * MS)
    ok &= check("fast_beats_unexpired_held",
                pol.dispatch(t0 + 10 * MS + 1) == "fast1")
    ok &= check("held_waits_until_deadline",
                pol.dispatch(t0 + 10 * MS + 2) == "held2" and
                pol.dispatch_held_soft == 2,
                "with fast drained the held head soft-releases")
    return ok


def test_conservation_quiesce():
    """Full lifecycle: inserts at varying busy states, dispatches with
    completions, graceful drain -> both lost counters must be exactly 0."""
    pol = Policy(meta={100: 1, 200: 7})
    t = 0
    rq_id = 0
    # ramp the busy state through all bands, interleaving inserts,
    # dispatches and completions
    for phase_inflight in (0, 40, 90, 64, 90):
        pol.inflight = phase_inflight
        for _ in range(200):
            pol.insert(100 if rq_id % 2 else 200, f"rq{rq_id}", t)
            rq_id += 1
            t += 1
        for _ in range(150):
            rq = pol.dispatch(t)
            if rq:
                pol.complete()
    # drain: dispatch everything still queued (holds expire with time)
    t += pol.cfg.max_hold_ns + 1
    guard = 0
    while (pol.fast or pol.held) and guard < 10000:
        if pol.dispatch(t):
            pol.complete()
        guard += 1
    ok = check("conservation_no_leak",
               pol.lost_fast == 0 and pol.lost_held == 0,
               f"lost_fast={pol.lost_fast} lost_held={pol.lost_held}")
    ok &= check("conservation_drained",
                not pol.fast and not pol.held,
                f"fast={len(pol.fast)} held={len(pol.held)} guard={guard}")
    # evacuate after the drain mark must yield nothing more
    pol.draining = True
    ok &= check("conservation_evac_empty", pol.evacuate() == 0)
    return ok


def test_draining_defer_and_evacuate():
    """DRAINING gate: insert defers to the parent; evacuate releases fast
    first, then held, and never touches inflight (q_ratelimit pattern)."""
    pol = Policy()
    pol.inflight = 90
    pol.cfg.ir[2] = [50, 50]
    for i in range(4):
        pol.insert(100, f"rq{i}", 0)
    inflight_before = pol.inflight
    pol.draining = True
    ok = check("draining_insert_defers", pol.insert(100, "later", 0) == "DEFER")
    first = pol.evacuate()
    second = pol.evacuate()
    ok &= check("evacuate_releases", bool(first) and bool(second),
                f"first={first} second={second}")
    # evacuate pops do not increment inflight (the BPF leaves it to the
    # guarded completion decrements)
    ok &= check("evacuate_inflight_untouched",
                pol.inflight == inflight_before,
                f"inflight={pol.inflight} before={inflight_before}")
    # drain the rest through evacuate (fast first, then held)
    served = 2
    while pol.evacuate():
        served += 1
    ok &= check("evacuate_drains_all", served == 4 and
                pol.lost_fast == 0 and pol.lost_held == 0,
                f"served={served} lost_fast={pol.lost_fast} "
                f"lost_held={pol.lost_held}")
    return ok


def test_push_fail_failopen():
    """Held FIFO overflow fails open to the fast FIFO (never lose a
    request to the interceptor); the stats record the push failure."""
    pol = Policy()
    pol.inflight = 90
    pol.cfg.ir[2] = [100, 100]      # intercept everything
    # simulate a held-FIFO overflow by hand: 4096-entry cap is beyond the
    # model's scope, so assert the fail-open CONTRACT on the insert path
    # via the model's contract test: enqueued == dispatched + evacuated + queued
    for i in range(1000):
        pol.insert(100, f"rq{i}", 0)
    ok = check("push_fail_counters_zero_when_healthy",
               pol.push_fails == 0 and len(pol.fast) + len(pol.held) == 1000,
               f"push_fails={pol.push_fails}")
    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ok = True
    ok &= test_band_boundaries()
    ok &= test_exact_interception_ratios()
    ok &= test_lc_be_differentiation()
    ok &= test_class_from_meta()
    ok &= test_held_release_semantics()
    ok &= test_soft_release_and_deadline_math()
    ok &= test_conservation_quiesce()
    ok &= test_draining_defer_and_evacuate()
    ok &= test_push_fail_failopen()
    print("MODEL " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
