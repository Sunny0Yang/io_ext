#!/usr/bin/env python3
"""arb_wdrr reference model — executable specification of the DRR core.

Mirrors userspace/ioext_arb_common.h (arb_drr_pick + arb_note_* hooks)
and the arb_wdrr.bpf.c hook wiring.  Any change to the BPF DRR core must
be mirrored here and vice versa; the acceptance script
tests/arb_wdrr_accept.sh runs this self-test as its first gate.

Model contract (identical to ioext_arb_common.h):
  - eligibility for SELECTION: pending > 0 only
  - serve: cursor stays on a tenant while deficit > 0 and pending > 0
  - arm: a key receives its round budget (quantum * eff_weight * ew_mult,
    capped at 2x) at most once per round via the refilled_round latch.
    S11.1: the grant fires for ANY key whose CGROUP has outstanding work
    with recent activity -- pending > 0 (selected immediately) or a dry
    queue with a live outstanding set (accrued as credit, never selected:
    the local queue is empty; the accrual does NOT refresh last_active:
    accrual is not service).  A device-buried key therefore banks its
    per-round turn instead of silently losing it at every round boundary
    (homo-v2 burst evidence: BE dispatched at ~4x its weight share while
    LC sat inflight-deep at pending==0; the signal must be cgroup-level
    because on multi-hctx devices the outstanding requests live on the
    HOME keys while the pick runs on the DECISION hctx).
  - exhaust: on visit with deficit <= 0 the key stays skipped until the
    next round's arm
  - new round (S10): the round advances ONLY when every key that
    received this round's grant consumed it (deficit <= 0).  A mid-turn
    key that is transiently drained holds the round while it SERVED
    recently.  S11.1: a buried key's banked credit does NOT pin the
    round (accrual never refreshed recency) -- rounds pass, the bank
    carries, the sibling keeps its work-conserving pace, and the bank
    drains whenever the buried key's completions return.
  - void turn (S10): a turn consumed by the serve-no-progress rotation
    forfeits its leftover credit (deficit = 0); the S11 dry-arm re-arms
    (and re-pins) within one sibling round grant.
  - S13 corpse reset (criterion fix 20261110): at the round-advance scan
    a key whose CGROUP OUTSTANDING SET IS EMPTY (count == 0) while its
    pending count leaked nonzero (io_uring exit cancels retire the set
    through completed_request but never pop, BUG-005) is a corpse --
    zero its pending and deficit so it can neither arm nor serve.  The
    test is the LEVEL of the outstanding set, NOT its recent activity:
    a live tenant in a backfill gap (set quiet longer than the release
    window, queue loaded, fast device) must never be reset -- the
    pre-fix activity test hard-zeroed its pending forever (bare-metal fio
    livelock: pick -> skip_pending_zero).
  - uniform floor lift (S11): at each round advance the next round's
    budgets all scale by ew_mult = max over keys of
    ceil(own_max_charge / (quantum * base_ew)), capped at
    ARB_WDRR_MAX_EW_MULT -- every key's budget affords one of its own
    max-size requests without compressing the weight ratio (the pre-S11
    global last_round_max_charge floor lifted only the keys below it).
  - charge: deficit -= bytes at dispatch (own_max_charge tracks the
    largest charge since the last round advance); requeue refunds the
    charge and drops inflight but never touches pending (the framework
    re-inserts requeued requests through inserted_request)
"""
import sys

SECTOR = 512
QUANTUM = 512                 # ARB_WDRR_QUANTUM_BYTES (F1 sweep 20261021)
CAP_MULT = 2                  # ARB_WDRR_IDLE_CAP_MULT
MIN_BUDGET = 4096             # ARB_WDRR_MIN_BUDGET_BYTES (one 4K page)
MAX_EW_MULT = 32              # ARB_WDRR_MAX_EW_MULT (S11 lift cap)
MAX_TENANTS = 16              # ARB_MAX_TENANTS
SERVE_NO_PROGRESS_MAX = 3    # ARB_SERVE_NO_PROGRESS_MAX (F2 liveness)
DISPATCH_FAIL_GHOST_RESET = 4  # ARB_DISPATCH_FAIL_GHOST_RESET (F2 liveness)
VOID_GHOST_RESET = 4          # ARB_VOID_GHOST_RESET (B3 heal, tune3)
# S10 (20261026) round-hold fix: a round may only advance when every key
# that received this round's turn grant has consumed it (deficit <= 0).
# Before S10 the round counter spun ahead on every all-drain scan
# (~246/s vs ~8/s full-consumption rounds on cg16 hardware), refilled_round
# went permanently stale, turn grants degenerated to "refill whenever
# deficit <= 0", the grant cadence became inversely proportional to the
# budget (measured refill_arms 11377@w100 vs 1556@w1600) and service
# self-cancelled to equal share (JainW 0.64 at cg16).
# S11 (20261107): the hold also covers a credit-holder with inflight > 0
# regardless of the idle window, and a dry key with inflight > 0 still
# receives its round grant (see the module docstring).  Model clock:
# 1 us per pick() call.
ROUND_IDLE_RELEASE_US = 20000  # ARB_ROUND_IDLE_RELEASE_NS / 1000

# S14 strict-liveness fuse gauge (mirror of ARB_STRICT_FUSE_BACKLOG ==
# kernel SCX_IO_STRICT_NULL_MIN_INFLIGHT): a pick NULL taken while the
# aggregate queued backlog exceeds this arms the ioext4v2-rev2 strict
# breaker's one-way fuse (second such NULL >= 1s later degrades the hctx
# to the uncharged active-pd fallback; see ioext_arb_common.h liveness
# bullet).  Above it the hold/round-advance exits overflow-serve the
# stranded key instead; below it the pre-S14 NULL exits are kept
# byte-identical so weighted fairness is untouched.
STRICT_FUSE_BACKLOG = 32


class Tenant:
    def __init__(self, cgid, weight):
        self.cgid = cgid
        self.weight = weight
        self.deficit = 0
        self.refilled_round = -1
        self.pending = 0
        self.inflight = 0
        self.dispatches = 0
        self.completed_bytes = 0
        self.requeues = 0
        self.requeued_bytes = 0
        self.dispatch_log = []
        self.serve_selects = 0
        self.refill_arms = 0
        self.wrap_selects = 0
        self.skip_pending_zero = 0
        self.last_serve_dispatches = 0
        self.serve_no_progress = 0
        self.dispatch_fails = 0
        self.void_turns = 0
        self.last_active_us = 0
        # S11: largest charge booked against this key's deficit since the
        # last round advance (feeds the uniform ew_mult lift).
        self.own_max_charge = 0
        # S11.1 cgroup-level demand signal (the C side keeps this in the
        # per-arbiter cgroup_outstanding map keyed by cgroup_id; the
        # single-hctx model collapses it onto the tenant).  outstanding =
        # pending + inflight; activity = the last insert/complete/fail/
        # requeue (the four hooks that move a request in or out of the
        # set; dispatch only moves it pending -> inflight).
        self.outstanding = 0
        self.out_activity_us = -ROUND_IDLE_RELEASE_US

    def budget(self, ew_mult=1):
        """Round budget = quantum * eff_weight * ew_mult where eff_weight
        = max(weight, min_budget/quantum) (the absolute one-page floor).
        The S11 uniform lift multiplies every key's budget together (see
        WdrrArbiter.pick's advance path) so large-request streams keep
        byte fairness without compressing the weight ratio."""
        ew = self.weight
        if ew == 0:
            ew = 1
        if QUANTUM * ew < MIN_BUDGET:
            ew = (MIN_BUDGET + QUANTUM - 1) // QUANTUM
        return QUANTUM * ew * ew_mult


class WdrrArbiter:
    """WDRR core (S12: the decision state is cgroup-level with ONE round
    counter; the model's single-hctx, one-slot-per-cgroup world is exactly
    that, which is why the C core converged to this shape): slots[0..15]
    hold cgids (0 = empty), cursor points at the last selected slot.
    Round semantics mirror arb_drr_pick: refill == turn grant, at most
    once per round (for any cgroup with outstanding work, S11.1).  The
    uniform lift ew_mult is recomputed at every round advance from the
    cgroups' own_max_charge."""

    def __init__(self):
        self.slots = [0] * MAX_TENANTS
        self.tenants = {}
        self.cursor = 0
        self.current_round = 0
        self.ew_mult = 1
        # S14 gauge mirror (arb_global_state.total_pending): maintained
        # +1/-1 by every pending mutation, exactly like the C side.  The
        # invariant sum(ts.pending) == total_pending is asserted by
        # _gauge_ok() (test_s14_gauge_invariant drives every mutation
        # site; the C-side v5 tot_pend wrap was a RACE at the zero sites,
        # fixed there by XCHG-exact zeroing -- the single-threaded model
        # asserts the DESIGN invariant, i.e. that every site mirrors).
        self.total_pending = 0
        self.clock_us = 0
        self.round_holds = 0

    def register(self, tenant, slot):
        self.slots[slot] = tenant.cgid
        self.tenants[tenant.cgid] = tenant

    def _arm(self, ts, accrual=False):
        """Once-per-round turn grant (the refilled_round latch): add the
        round budget to the deficit, capped at 2x.  Mirrors
        arb_drr_arm_grant.  accrual=True (the S11.1 dry path) skips the
        last_active refresh: accrual is not service."""
        budget = ts.budget(self.ew_mult)
        ts.deficit = min(ts.deficit + budget, budget * CAP_MULT)
        ts.refilled_round = self.current_round
        ts.refill_arms += 1
        if not accrual:
            ts.last_active_us = self.clock_us

    def pick(self):
        """One arbiter pick call == one hctx dispatch attempt (== 1 us).

        S10 strict round semantics: the round may only advance when every
        key holding this round's turn grant has consumed it (deficit <= 0).
        A key that is mid-turn but transiently drained (pending == 0 with
        leftover credit) HOLDS the round until its inserts return or the
        idle release expires.  S11: a dry key with inflight > 0 still
        receives (accrues) its round grant and its on-device requests
        count as activity for the hold, so a device-buried tenant neither
        loses turns nor gets its credit forfeited behind a write-flush
        stall.

        S14 strict liveness (20261111, ioext4v2-rev2 breaker forensics):
        a NULL pick taken while the aggregate queued backlog exceeds
        STRICT_FUSE_BACKLOG (= the kernel breaker's fuse threshold) is a
        fuse tick -- the second such NULL >= 1s later permanently degrades
        the hctx to the active-pd fallback, whose pops skip
        dispatched_request (pending leaks) and whose completions carry an
        unresolvable cgroup id (outstanding leaks).  The S10 hold exit and
        the round-advance exit therefore overflow-serve the stranded key
        instead of returning 0 whenever the backlog exceeds the
        threshold; below it the pre-S14 NULL exits are byte-identical,
        so steady-state weighted fairness is untouched.  The overflow
        serve clamps the deficit at 0 pre-serve (pay-as-you-go, the D1
        fastpath v2 precedent) so the post-serve debt is bounded by one
        max-request overshoot; the S10 pin stands (the round does not
        advance during a hold serve).  Mechanism note (tune5 verifier-fit
        rework): the C maintains the backlog as a global counter
        (arb_global_state.total_pending, +1/-1 on every pending_total
        mutation -- loop-carried accumulators doubled the verifier's
        state exploration to -E2BIG) and stores the serve candidate in a
        per-hctx scratch field; this model computes the same two values
        per scan instead (behaviorally identical; the counter's
        invariant tot_pend == pend_sum is asserted live by the loader
        diag on the target)."""
        self.clock_us += 1
        base = self.cursor
        stranded_cgid = 0
        backlog_total = 0
        for step in range(MAX_TENANTS):
            idx = (base + step) % MAX_TENANTS
            cgid = self.slots[idx]
            if cgid == 0:
                continue
            ts = self.tenants[cgid]
            if ts.pending == 0:
                # S11.1 arm-on-dry: a dry key whose CGROUP still has a
                # live outstanding set (count > 0, activity within the
                # release window) is NOT idle -- its requests are on the
                # device and completions WILL return.  Accrue this round's
                # budget (never select: the pop would be an empty one).
                if (ts.outstanding > 0 and ts.deficit <= 0
                        and self.clock_us - ts.out_activity_us
                            < ROUND_IDLE_RELEASE_US
                        and ts.refilled_round != self.current_round):
                    self._arm(ts, accrual=True)
                ts.skip_pending_zero += 1
                continue
            # S14: aggregate the queued backlog (fuse gauge) and remember
            # the first pending key (the overflow-serve candidate).  A
            # serve-branch key captured here is harmless -- the pick
            # returns before the epilogue whenever the scan serves.
            backlog_total += ts.pending
            if not stranded_cgid:
                stranded_cgid = cgid
            if ts.deficit > 0:
                if ts.dispatches == ts.last_serve_dispatches:
                    ts.serve_no_progress += 1
                else:
                    ts.serve_no_progress = 0
                ts.last_serve_dispatches = ts.dispatches
                if ts.serve_no_progress > SERVE_NO_PROGRESS_MAX:
                    # empty-pop wedge: consume the turn, rotate the cursor
                    # so the scan reaches the other tenants (F2).  S10: the
                    # voided turn also forfeits its leftover credit -- a
                    # crystallized no-progress key must not hold the round.
                    # S11: the dry re-arm bounds the damage to one sibling
                    # round grant.
                    ts.serve_no_progress = 0
                    ts.refilled_round = self.current_round
                    ts.deficit = 0
                    # B3 heal (tune3, STALL_FORENSICS.md leak B3): four
                    # consecutive voided turns with zero intervening
                    # dispatches mean the queue is REALLY empty while
                    # pending is stuck > 0 (evacuate-invalid / BUG-005
                    # crystallized residue that no hook ever retires) --
                    # hard-zero it, mirroring the C-side void branch.  The
                    # model's queues are truthful, so this can only fire
                    # when a test constructs crystallized pending on
                    # purpose.
                    ts.void_turns += 1
                    if ts.void_turns >= VOID_GHOST_RESET:
                        ts.void_turns = 0
                        self.total_pending -= ts.pending
                        ts.pending = 0
                    self.cursor = (idx + 1) % MAX_TENANTS
                    continue
                self.cursor = idx
                ts.last_active_us = self.clock_us
                ts.serve_selects += 1
                return cgid
            if ts.refilled_round != self.current_round:
                # new turn: grant this round's budget and serve it
                self._arm(ts)
                self.cursor = idx
                return cgid
            # already had its turn this round: move on
            self.cursor = (idx + 1) % MAX_TENANTS
        # scan complete with nobody selectable.  Hold the round while any
        # key holds unconsumed credit (from ANY round -- S10.1) and was
        # active recently OR still has requests inflight (S11: on-device
        # work is activity).  The same pass accumulates the next round's
        # uniform lift k = max ceil(own_max_charge / (quantum*base_ew)).
        k = 1
        for step in range(MAX_TENANTS):
            idx = (base + step) % MAX_TENANTS
            cgid = self.slots[idx]
            if cgid == 0:
                continue
            ts = self.tenants[cgid]
            # S13 corpse reset (criterion fix 20261110): fio/io_uring exit
            # cancels still-queued requests WITHOUT a dispatch-fail and
            # WITHOUT a pop, so `pending` leaks on the exited tenant
            # (BUG-005: bm f1 T6 stale B key, pending=64 forever; the
            # single-hctx model's `pending` collapses the C `pending_total`
            # shared-queue count).  The cancel DOES retire the outstanding
            # set (completed_request -> note_completed -> outstanding-1),
            # so the corpse test is the LEVEL of the set (count == 0; the
            # C side's no-signal case -- missing map/entry -- also reads
            # as empty), NOT its recent activity: a live tenant keeps
            # count > 0 across any backfill gap, so it is never
            # misjudged (the pre-fix activity test hard-zeroed live
            # tenants on fast disks -> permanent skip_pending_zero ->
            # fio livelock).
            if ts.pending and ts.outstanding == 0:
                self.total_pending -= ts.pending
                ts.pending = 0
                ts.deficit = 0
            if ts.deficit > 0:
                if self.clock_us - ts.last_active_us < ROUND_IDLE_RELEASE_US:
                    self.round_holds += 1
                    # S14 liveness: the S10 pin stands only while the
                    # fuse cannot trip (queued backlog at/below the
                    # breaker threshold).  With a deep stranded queue,
                    # a NULL hands those entries to the kernel's breaker
                    # fallback (uncharged pop, unresolvable completion
                    # id -- both ledger counters leak).  Overflow-serve
                    # the stranded key and return straight from here
                    # (mirror of the C direct return -- verifier-fit,
                    # no loop-carried flag; the remaining keys'
                    # corpse/forfeit passes run on the next pick, both
                    # idempotent); the round does NOT advance (the pin
                    # is preserved, grants cannot spin).
                    if backlog_total > STRICT_FUSE_BACKLOG:
                        return self._overflow_serve(stranded_cgid)
                    return 0
                # S11.1: a stale credit-holder whose outstanding set is
                # still live KEEPS its bank (rounds pass it by; it serves
                # the bank when its completions return).  Only a genuinely
                # stopped tenant forfeits (S10.3 self-healing release).
                if not (ts.outstanding > 0
                        and self.clock_us - ts.out_activity_us
                            < ROUND_IDLE_RELEASE_US):
                    ts.deficit = 0
            base_ew = ts.weight if ts.weight else 1
            if QUANTUM * base_ew < MIN_BUDGET:
                base_ew = (MIN_BUDGET + QUANTUM - 1) // QUANTUM
            need = -(-ts.own_max_charge // (QUANTUM * base_ew))  # ceil
            if need > k:
                k = need
        # S14 liveness, hold variant is handled by the direct return
        # above (no loop-carried flag -- verifier-fit mirror).
        # round complete (or nothing eligible): advance the round, roll
        # the uniform lift window, reset each key's own-max-charge window
        self.ew_mult = min(k, MAX_EW_MULT)
        for step in range(MAX_TENANTS):
            cgid = self.slots[(base + step) % MAX_TENANTS]
            if cgid:
                self.tenants[cgid].own_max_charge = 0
        self.current_round += 1
        # S14 liveness, round-advance variant: the barrier rolled while
        # the queued backlog exceeds the breaker threshold -- the
        # pre-S14 "one NULL per round" exit was a fuse tick under
        # ioext4v2-rev2.  Serve the stranded key charged (clamped); its
        # next arm pays the debt.
        if backlog_total > STRICT_FUSE_BACKLOG:
            return self._overflow_serve(stranded_cgid)
        return 0

    def _overflow_serve(self, cgid):
        """S14 overflow serve (mirror of arb_drr_overflow_serve): serve a
        round-stranded key outside the turn machinery when the aggregate
        queued backlog exceeds the strict breaker's fuse threshold.  The
        deficit is clamped at 0 pre-serve (pay-as-you-go, the D1 fastpath
        v2 precedent: an unclamped charge accumulates one unbudgeted
        request per round boundary and the debt outpaces the arm grant);
        the post-serve deficit is bounded by -bytes >= -max_request, the
        DRR contract's own overshoot rule.  Cursor/last_selected stay
        untouched (D1 v1 lesson).

        v6 note (bare-metal v6 oracle forensics): the overflow serve carries NO
        duplicate no-progress/void machinery -- a ghost candidate heals
        through the serve branch's own cycle (the round advance
        re-latches refilled_round, the next pick arms it, and the 4-turn
        void heal runs there).  Duplicating the machinery here pushed
        the C pick_cgroup past the verifier's 1M processed-insn limit
        (oracle-measured, both inlined and as __noinline subprograms);
        the XCHG-exact gauge (arb_pending_zero_gauged) removed the
        corruption that made v5's overflow serves miss in the first
        place."""
        ts = self.tenants[cgid]
        if ts.deficit < 0:
            ts.deficit = 0
        ts.serve_selects += 1
        ts.last_active_us = self.clock_us
        return cgid

    def _gauge_ok(self):
        """S14 gauge invariant: the maintained counter equals the sum of
        the per-cgroup pending counts (arb_global_state.total_pending ==
        sum(pending_total); the loader diag asserts the live form as
        tot_pend vs pend_sum)."""
        return self.total_pending == sum(ts.pending for ts in self.tenants.values())

    # framework hook handlers (mirror arb_note_*)

    def note_inserted(self, cgid):
        ts = self.tenants[cgid]
        ts.outstanding += 1
        ts.out_activity_us = self.clock_us
        ts.pending += 1
        self.total_pending += 1

    def note_dispatched(self, cgid, nbytes):
        ts = self.tenants[cgid]
        ts.deficit -= nbytes
        if ts.pending > 0:
            ts.pending -= 1
            self.total_pending -= 1
        ts.inflight += 1
        ts.completed_bytes += nbytes
        ts.dispatches += 1
        ts.dispatch_fails = 0
        ts.serve_no_progress = 0
        ts.void_turns = 0
        ts.dispatch_log.append(nbytes)
        # S11: track the largest charge of the current round window (the
        # advance path derives the uniform ew_mult lift from it)
        if nbytes > ts.own_max_charge:
            ts.own_max_charge = nbytes

    def note_dispatch_failed(self, cgid):
        # invalid-rq retire; consecutive failures hard-reset a ghost pending
        # entry (F2, ARB_DISPATCH_FAIL_GHOST_RESET)
        ts = self.tenants[cgid]
        if ts.outstanding > 0:
            ts.outstanding -= 1
        ts.out_activity_us = self.clock_us
        ts.dispatch_fails += 1
        if ts.dispatch_fails >= DISPATCH_FAIL_GHOST_RESET:
            self.total_pending -= ts.pending
            ts.pending = 0
            ts.deficit = 0
            ts.dispatch_fails = 0
            ts.serve_no_progress = 0
            ts.void_turns = 0
            return
        if ts.pending > 0:
            ts.pending -= 1
            self.total_pending -= 1

    def note_requeue(self, cgid, nbytes):
        # refund only; the re-insert fires note_inserted again (and adds
        # the request back to the outstanding set there).  This mirrors
        # the ELEVATOR requeue variant only (RQF_DONTPREP clear: the
        # kernel re-inserts through the scheduler).  The bypass variant
        # (RQF_DONTPREP / flush -> hctx->dispatch, no re-insert) is a
        # kernel-side routing fact the model never reproduces; the C side
        # compensates it by pairing requeue/insert/complete by rq_ptr
        # (requeue_replay, STALL_FORENSICS.md leak B2), which nets the
        # same per-request balance this model computes directly.
        ts = self.tenants[cgid]
        if ts.outstanding > 0:
            ts.outstanding -= 1
        ts.out_activity_us = self.clock_us
        if ts.inflight > 0:
            ts.inflight -= 1
        ts.deficit += nbytes
        ts.requeued_bytes += nbytes
        ts.requeues += 1

    def note_completed(self, cgid):
        ts = self.tenants[cgid]
        if ts.outstanding > 0:
            ts.outstanding -= 1
        ts.out_activity_us = self.clock_us
        if ts.inflight > 0:
            ts.inflight -= 1


def drive_backlogged(arb, requests, total_dispatches):
    """Keep every tenant backlogged at exactly one pending request and
    dispatch `total_dispatches` requests, completing each immediately.
    A pick that returns 0 is a round boundary: blk-mq retries on the
    next completion kick, so the model retries too."""
    served = {cg: 0 for cg in requests}
    for cg in requests:
        arb.note_inserted(cg)
    n = 0
    while n < total_dispatches:
        cg = arb.pick()
        if cg == 0:
            continue  # round boundary; retry like blk-mq's next kick
        arb.note_dispatched(cg, requests[cg])
        arb.note_completed(cg)
        served[cg] += 1
        n += 1
        if arb.tenants[cg].pending == 0:
            arb.note_inserted(cg)
    return served


def drive_shallow_queues(arb, requests, total_dispatches, queue_cap=16,
                         device_latency=8, jitter=15, seed=0):
    """Hardware saturated regime (session6_20260920 W4 dump).  Each
    dispatched request returns to the queue after device_latency + jitter
    slots (completion round-trip; the jitter models completion batching
    across the 48 (tenant, hctx) pipeline queues).  fio keeps outstanding
    (queued + in transit) at queue_cap, so the framework queue is the
    RESIDUAL behind the transit pipeline and pending hits zero
    periodically: visits end pending-bound unless the round budget is
    small enough to bind first.  The pre-S6 32KiB quantum degenerated
    exactly this regime on hardware (refills settled at ~1/w, weighted
    Jain 0.63); the S6 quantum must keep the budget binding per visit."""
    served = {cg: 0 for cg in requests}
    returns = []           # [due_at, cgid] completions back into the queue
    rng = __import__("random").Random(seed)

    for cg in requests:
        for _ in range(queue_cap):
            arb.note_inserted(cg)

    clock = 0
    n = 0
    idle_slots = 0
    while n < total_dispatches:
        clock += 1
        for e in [e for e in returns if e[0] <= clock]:
            returns.remove(e)
            arb.note_completed(e[1])     # in-flight drains (S11 reads it)
            if arb.tenants[e[1]].pending < queue_cap:
                arb.note_inserted(e[1])
        cg = arb.pick()
        if cg == 0:
            if all(arb.tenants[t].pending == 0 for t in requests):
                idle_slots += 1
                if idle_slots > 6 * queue_cap:
                    raise RuntimeError("drive_shallow_queues starved")
            continue
        arb.note_dispatched(cg, requests[cg])
        returns.append([clock + device_latency + rng.randint(0, jitter), cg])
        served[cg] += 1
        n += 1
        idle_slots = 0
    return served


def drive_coloc(arb, requests, caps, lat_us, total_dispatches):
    """Hardware coloc regime (homo-v2 ctrl arms, kvmhost sdb): per-tenant
    request sizes, queue-depth caps and completion latencies; fio keeps
    outstanding (pending + in-flight) at caps[cg], every completion kicks
    dispatch (blk-mq) and every insert kicks dispatch (plug flush).  A
    dispatch loop pops ONE request per pick; a pick that returns 0 (hold
    or round boundary) ends the loop until the next kick.  The queue is
    the residual behind the device pipeline, so pending hits zero
    periodically and visits end pending-bound -- the regime where the
    pre-S11 core lost turns at round boundaries (BE dispatched at ~4x its
    weight share while LC sat device-buried)."""
    import heapq
    served = {cg: 0 for cg in requests}
    done = []              # (due_us, cgid) completion queue
    heapq.heapify(done)
    kick = []              # due times of dispatch kicks
    heapq.heappush(kick, 1)

    def submit(cg, t):
        arb.note_inserted(cg)
        heapq.heappush(kick, t + 0.5)

    for cg in requests:
        for _ in range(caps[cg]):
            submit(cg, 0)

    clock = 0.0
    n = 0
    while n < total_dispatches:
        next_done = done[0][0] if done else float("inf")
        next_kick = kick[0] if kick else float("inf")
        if not done and not kick:
            break
        t = min(next_done, next_kick)
        clock = max(clock, t)
        # retire every completion due now, then run every kick due now
        while done and done[0][0] <= clock:
            _, cg = heapq.heappop(done)
            arb.note_completed(cg)
            ts = arb.tenants[cg]
            if ts.pending + ts.inflight < caps[cg]:
                submit(cg, clock)
        while kick and kick[0] <= clock:
            heapq.heappop(kick)
            while n < total_dispatches:
                cg = arb.pick()
                if cg == 0:
                    break
                ts = arb.tenants[cg]
                if ts.pending == 0:
                    break           # empty pop: silent NULL ends the loop
                arb.note_dispatched(cg, requests[cg])
                served[cg] += 1
                n += 1
                heapq.heappush(done, (clock + lat_us[cg], cg))
    return served


def check(name, cond, detail=""):
    if cond:
        print(f"PASS: {name}")
        return True
    print(f"FAIL: {name} {detail}")
    return False


def test_case1_weight_ratio():
    arb = WdrrArbiter()
    a, b = Tenant(100, 100), Tenant(300, 300)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: 4096, 300: 4096}, 32000)
    ba, bb = served[100] * 4096, served[300] * 4096
    ratio = bb / ba
    # thousands of rounds at budget(100)=6400B (2 dispatches per round
    # after overshoot) make the ratio converge; window is generous
    return check("case1_weight_ratio", 2.7 <= ratio <= 3.3,
                 f"ratio={ratio:.3f} expected~3 (window [2.7,3.3])")


def test_case3_shallow_queue_weight_ratio():
    """The hardware saturated regime (20260919 W4 run): shallow queues,
    completion-latency-bound refill.  Pre-S6 constants (32KiB quantum)
    degenerate this to equal share; the S6 quantum must keep the
    budget binding per visit so service splits by weight."""
    arb = WdrrArbiter()
    a, b = Tenant(100, 100), Tenant(300, 300)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_shallow_queues(arb, {100: 4096, 300: 4096}, 40000)
    ba, bb = served[100] * 4096, served[300] * 4096
    ratio = bb / ba
    return check("case3_shallow_queue_weight_ratio", 2.0 <= ratio <= 4.0,
                 f"ratio={ratio:.3f} expected~3 (window [2.0,4.0])")


def test_case2_byte_fairness():
    arb = WdrrArbiter()
    a, b = Tenant(100, 100), Tenant(200, 100)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: 4096, 200: 131072}, 32000)
    ba, bb = served[100] * 4096, served[200] * 131072
    ratio = ba / bb
    req_ratio = served[100] / served[200]
    ok = check("case2_byte_fairness", 0.8 <= ratio <= 1.25, f"bytes ratio={ratio:.3f}")
    ok &= check("case2_not_request_fair", req_ratio > 8,
                f"request ratio={req_ratio:.1f} (expected ~32, not ~1)")
    return ok


def test_work_conservation_and_idle_cap():
    arb = WdrrArbiter()
    a, b = Tenant(100, 100), Tenant(200, 100)
    arb.register(a, 0)
    arb.register(b, 1)
    # only A active: A must get everything, B's deficit must not grow
    for _ in range(3000):
        arb.note_inserted(100)
        cg = arb.pick()
        while cg == 0:
            cg = arb.pick()  # round boundary: retry like blk-mq
        assert cg == 100, "idle tenant must never be picked"
        arb.note_dispatched(cg, 4096)
        arb.note_completed(cg)
    ok = check("work_conservation", b.dispatches == 0 and b.deficit == 0,
               f"B dispatches={b.dispatches} deficit={b.deficit}")
    # A goes idle with whatever deficit remains; cap holds
    ok &= check("idle_credit_cap", a.deficit <= a.budget() * CAP_MULT,
                f"A deficit={a.deficit} cap={a.budget() * CAP_MULT}")
    return ok


def test_requeue_refund_no_leak():
    arb = WdrrArbiter()
    a = Tenant(100, 100)
    arb.register(a, 0)
    arb.note_inserted(100)
    cg = arb.pick()
    assert cg == 100
    armed = a.deficit
    arb.note_dispatched(cg, 4096)           # charge 4096, pending 0
    arb.note_requeue(cg, 4096)              # refund, no pending change
    arb.note_inserted(cg)                    # framework re-insert
    arb.note_dispatched(arb.pick(), 4096)    # re-dispatch, charge again
    arb.note_completed(cg)
    net = a.completed_bytes - a.requeued_bytes
    return check("requeue_refund_no_leak",
                 a.pending == 0 and a.inflight == 0 and net == 4096
                 and a.deficit == armed - 4096,
                 f"pending={a.pending} inflight={a.inflight} net={net}")


def test_overshoot_bound():
    arb = WdrrArbiter()
    a = Tenant(100, 100)
    arb.register(a, 0)
    worst = 0
    for _ in range(500):
        arb.note_inserted(100)
        cg = arb.pick()
        while cg == 0:
            cg = arb.pick()  # round boundary: retry like blk-mq
        arb.note_dispatched(cg, 128 * 1024)  # large requests
        arb.note_completed(100)
        worst = min(worst, a.deficit)
    return check("overshoot_bound", worst >= -(128 * 1024),
                 f"worst deficit={worst} bound=-131072")


def test_dispatch_failed_pending():
    arb = WdrrArbiter()
    a = Tenant(100, 100)
    arb.register(a, 0)
    arb.note_inserted(100)
    arb.pick()
    # invalid-rq path retires the pending entry
    a.pending -= 1
    return check("dispatch_failed_pending", arb.pick() == 0,
                 "empty tenant must not be picked")


def test_empty_pop_wedge_rotation():
    # F2 wedge regression (treesub 20260919): tenant B keeps pending=1 with a
    # deficit that never exhausts while its pop stays empty; before the fix
    # every pick returned B and backlogged A starved forever.
    arb = WdrrArbiter()
    a = Tenant(100, 100)
    b = Tenant(200, 2600)
    arb.register(a, 0)
    arb.register(b, 1)
    for _ in range(100):
        arb.note_inserted(100)
    arb.note_inserted(200)
    # arm both turns
    first = arb.pick()
    served = []
    while first == 200:
        # pop for B returns nothing (no note_dispatched call)
        served.append(arb.pick())
        first = served[-1]
        if served.count(100) > 0:
            break
        if len(served) > 64:
            return check("empty_pop_wedge_rotation", False,
                         "cursor never rotated to tenant A")
    return check("empty_pop_wedge_rotation", 100 in served or first == 100,
                 "cursor must rotate to the backlogged tenant")


def test_dispatch_failed_ghost_reset():
    arb = WdrrArbiter()
    a = Tenant(100, 100)
    arb.register(a, 0)
    for _ in range(5):
        arb.note_inserted(100)
    arb.pick()
    # four consecutive invalid-rq dispatches with no successful dispatch in
    # between: the crystallized pending must be hard-reset, not guarded-dec
    for _ in range(4):
        arb.note_dispatch_failed(100)
    return check("dispatch_failed_ghost_reset", a.pending == 0 and a.deficit == 0,
                 f"pending={a.pending} deficit={a.deficit} after 4 fails")


def test_s11_coloc_byte_share():
    """THE homo-v2 acceptance shape (task contract: io.weight byte-share):
    LC 4K randread QD32 w100 vs BE 64K seqwrite QD16 w200 must split
    served bytes 1:2 (be_share 0.667) in the saturated coloc regime where
    both tenants are periodically device-buried (pending == 0 with the
    whole QD in flight).  Pre-S11 hardware measured be_share 0.765
    (x_weight_arb) / 0.78 (x_cost_arb) -- BE re-armed at completion-kick
    cadence while LC lost every round boundary."""
    arb = WdrrArbiter()
    lc, be = Tenant(100, 100), Tenant(200, 200)
    arb.register(lc, 0)
    arb.register(be, 1)
    served = drive_coloc(arb, {100: 4096, 200: 65536},
                         caps={100: 32, 200: 16},
                         lat_us={100: 250, 200: 600},
                         total_dispatches=60000)
    blc = served[100] * 4096
    bbe = served[200] * 65536
    share = bbe / (blc + bbe)
    ok = check("s11_coloc_byte_share", 0.62 <= share <= 0.72,
               f"be_share={share:.3f} expected 0.667 (window [0.62,0.72])")
    ok &= check("s11_coloc_arms_balanced",
                0.75 <= be.refill_arms / max(1, lc.refill_arms) <= 1.33,
                f"arms lc={lc.refill_arms} be={be.refill_arms} "
                f"(pre-S11 collapsed to ~0.4)")
    return ok


def test_s11_arm_on_dry_accrual():
    """S11.1 demand accrual for a device-buried key (dry queue, live
    outstanding set):
      - while the buried key's completions keep the outstanding set
        active, its arms keep pace with the backlogged sibling's (~1:1)
        even though the round is NOT pinned by the accrual -- the banked
        credit is served as completions return;
      - when the outstanding set goes stale (no completion/insert within
        the release window), accrual stops and any banked credit
        forfeits at the next scan-complete (S10.3 self-healing release);
      - a genuinely idle key (nothing outstanding) never arms."""
    arb = WdrrArbiter()
    lc, be = Tenant(100, 100), Tenant(200, 200)
    arb.register(lc, 0)
    arb.register(be, 1)
    for _ in range(4):
        arb.note_inserted(200)
    # LC buried: 2 requests on the device, queue dry, completions flowing
    lc.inflight = 2
    lc.outstanding = 2
    lc.out_activity_us = 0

    def drive_until_hold(max_picks=200):
        n = 0
        while n < max_picks:
            cg = arb.pick()
            if cg == 0:
                return
            arb.note_dispatched(cg, 4096)
            arb.note_completed(cg)
            if cg == 200 and arb.tenants[200].pending == 0:
                arb.note_inserted(200)
            n += 1

    # bury-drain cycles: LC's completions return (slowly), fio resubmits
    for _ in range(60):
        arb.note_completed(100)          # one buried request returns...
        arb.note_inserted(100)           # ...fio resubmits it
        drive_until_hold()                # sibling drains what it can
    ok = check("s11_dry_accrual_arms_balanced",
               abs(be.refill_arms - lc.refill_arms) <= 2,
               f"arms lc={lc.refill_arms} be={be.refill_arms} "
               f"(must stay ~1:1; pre-S11 be ran away at kick cadence)")
    ok &= check("s11_dry_bank_served", lc.dispatches > 0,
                f"lc dispatches={lc.dispatches} (banked credit must serve)")
    # outstanding set goes stale: no more accrual, bank forfeits
    for _ in range(ROUND_IDLE_RELEASE_US + 100):
        arb.pick()
    banked = lc.deficit
    for _ in range(100):
        arb.pick()
    ok &= check("s11_stale_outstanding_stops_accrual",
                lc.deficit == banked or banked <= 0,
                f"deficit={lc.deficit} after stale (was {banked})")
    lc.outstanding = 0                    # fully drained: genuinely idle
    arms0 = lc.refill_arms
    for _ in range(50):
        arb.pick()
    ok &= check("s11_idle_key_never_arms", lc.refill_arms == arms0,
                f"idle arms={lc.refill_arms}")
    return ok


def test_s11_hold_and_bank_semantics():
    """S10.1 hold unchanged: a mid-turn key with unconsumed credit that
    SERVED recently pins the round.  S11.1: a buried key's banked credit
    (accrued, never served recently) does NOT pin the round -- but it
    survives the S10.3 forfeit while the cgroup's outstanding set stays
    active, and forfeits once the set goes stale."""
    arb = WdrrArbiter()
    lc, be = Tenant(100, 100), Tenant(200, 200)
    arb.register(lc, 0)
    arb.register(be, 1)
    # mid-turn, dry queue, served THIS round: hold must fire (S10.1)
    lc.deficit = 20480
    lc.refilled_round = arb.current_round
    lc.last_active_us = arb.clock_us
    be.refilled_round = arb.current_round
    for _ in range(4):
        arb.note_inserted(200)
    holds0 = arb.round_holds
    held = all(arb.pick() == 0 for _ in range(5))
    ok = check("s10_midturn_recent_credit_holds",
               held and arb.round_holds > holds0,
               f"held={held} holds={arb.round_holds - holds0}")
    # buried bank: credit, stale recency, live outstanding set
    lc.last_active_us = arb.clock_us - (ROUND_IDLE_RELEASE_US + 10)
    lc.outstanding = 4
    lc.out_activity_us = arb.clock_us - 100     # activity well within window
    rounds0 = arb.current_round
    banked = lc.deficit
    for _ in range(64):
        arb.pick()
    ok &= check("s11_bank_does_not_pin_round",
                arb.current_round > rounds0,
                f"rounds={arb.current_round} (was {rounds0}; banked credit "
                f"must let the round pass)")
    ok &= check("s11_bank_survives_while_active", lc.deficit == banked,
                f"deficit={lc.deficit} (banked {banked} must survive)")
    # outstanding set goes stale: forfeit at the next scan-complete
    lc.out_activity_us = arb.clock_us - (ROUND_IDLE_RELEASE_US + 10)
    for _ in range(64):
        arb.pick()
    ok &= check("s11_bank_forfeits_when_stale", lc.deficit == 0,
                f"deficit={lc.deficit} (stale outstanding must forfeit)")
    return ok


def test_s11_uniform_lift_preserves_ratio():
    """S11 uniform floor lift: when a key's weight-proportional budget
    cannot afford one of its OWN max-size requests (128K requests on
    w200 -> budget 102400 < 131072), the round advance scales EVERY
    budget by the same k=2 instead of lifting only the needy key, so the
    weight ratio survives the lift (the pre-S11 global max-charge floor
    compressed 100:200 to 1:1.5625 in this situation)."""
    arb = WdrrArbiter()
    lc, be = Tenant(100, 100), Tenant(200, 200)
    arb.register(lc, 0)
    arb.register(be, 1)
    served = drive_backlogged(arb, {100: 4096, 200: 131072}, 12000)
    ratio = (served[200] * 131072) / (served[100] * 4096)
    ok = check("s11_lift_ratio_preserved", 1.7 <= ratio <= 2.3,
               f"bytes ratio={ratio:.3f} expected 2.0 (window [1.7,2.3])")
    ok &= check("s11_lift_active", arb.ew_mult == 2,
                f"ew_mult={arb.ew_mult} (ceil(131072/102400)=2)")
    return ok


def test_s13_corpse_reset():
    """S13 corpse reset (criterion fix 20261110): the corpse test is the
    LEVEL of the cgroup's outstanding set, NOT its recent activity.
      - corpse (BUG-005, bm f1 T6): fio/io_uring exit cancels retire the
        outstanding set through completed_request but never pop the
        queued entries -- `pending` (== C pending_total) leaks at 64
        while outstanding == 0.  The advance pass must zero the leak so
        the ghost can neither arm nor serve, even with FRESH activity
        (only the count may declare death);
      - livelock regression (bare-metal fio backfill on the fast NVMe): a LIVE tenant
        mid backfill gap -- queue loaded, outstanding > 0, no
        insert/complete for far longer than the release window -- must
        NOT be reset; the pre-fix activity test hard-zeroed its pending
        and the pick skipped it forever (skip_pending_zero livelock)."""
    arb = WdrrArbiter()
    a, b = Tenant(100, 100), Tenant(200, 200)
    arb.register(a, 0)
    arb.register(b, 1)
    # both keys already had this round's turn, so the first pick falls
    # through the scan into the advance pass (where the reset lives)
    a.refilled_round = arb.current_round
    b.refilled_round = arb.current_round
    # corpse: 64 leaked entries, set drained by the exit cancels,
    # activity FRESH (only a count test may call it dead)
    b.pending = 64
    b.outstanding = 0
    b.out_activity_us = arb.clock_us
    # live sibling mid backfill gap: queue loaded, set quiet >> window
    a.pending = 4
    a.outstanding = 4
    a.out_activity_us = arb.clock_us - (ROUND_IDLE_RELEASE_US + 1000)
    arb.pick()
    ok = check("s13_corpse_reset_on_empty_set",
               b.pending == 0 and b.deficit == 0,
               f"b.pending={b.pending} deficit={b.deficit} "
               f"(count==0 corpse must reset despite fresh activity)")
    ok &= check("s13_live_gap_keeps_pending", a.pending == 4,
                f"a.pending={a.pending} (live tenant mid backfill gap "
                f"must keep its queued entries)")
    # the reset corpse can never arm again (dry-arm needs outstanding > 0)
    for _ in range(200):
        arb.pick()
    ok &= check("s13_corpse_never_arms", b.refill_arms == 0,
                f"b.refill_arms={b.refill_arms} (a reset corpse must not "
                f"arm: dry-arm requires outstanding > 0)")
    return ok


def test_s14_strict_liveness_no_null_on_deep_backlog():
    """S14 (20261111, ioext4v2-rev2 breaker forensics): while the aggregate
    queued backlog exceeds the strict breaker's fuse threshold, pick must
    NEVER return 0 -- every such NULL is a fuse tick (the kernel's breaker
    degrades the second one >= 1s after the first, permanently, and its
    active-pd fallback pops the q map without dispatched_request and
    completes with an unresolvable cgroup id, drifting BOTH ledger
    counters).  At/below the threshold the pre-S14 NULL exits stand
    (the fuse cannot trip there) and weighted fairness is untouched.
      - round-advance boundary with a deep stranded queue: serve, and
        the served request charges (pending retires, deficit clamped
        to one overshoot -- no debt spiral);
      - S10 hold pin with a deep stranded queue: serve WITHOUT advancing
        the round (the pin stands, grants cannot spin);
      - shallow backlog: the pre-S14 NULL exits are preserved;
      - long deep-backlog solo run: no NULL while the backlog is over
        the threshold, and the deficit never leaves one-overshoot."""
    arb = WdrrArbiter()
    a, b = Tenant(100, 100), Tenant(200, 200)
    arb.register(a, 0)
    arb.register(b, 1)
    # --- round-advance boundary: B armed+exhausted, 64 deep stranded ---
    for _ in range(64):
        arb.note_inserted(200)
    cg = arb.pick()                    # arms B's turn
    assert cg == 200
    while b.deficit > 0:               # exhaust B's round budget
        cg = arb.pick()
        assert cg == 200
        arb.note_dispatched(cg, 4096)
    assert b.pending > STRICT_FUSE_BACKLOG
    served = arb.pick()                # round boundary with backlog > fuse
    ok = check("s14_advance_serves_deep_backlog", served == 200,
               f"pick={served} (must serve the stranded key, not NULL)")
    pend0 = b.pending
    arb.note_dispatched(served, 4096)
    ok &= check("s14_overflow_serve_charges",
                b.pending == pend0 - 1 and b.dispatches > 0,
                f"pending={b.pending} (was {pend0}; dispatched_request "
                f"retires it)")
    # --- S10 hold pin with a deep stranded queue: pin + serve, no NULL ---
    # A holds unconsumed credit, served recently, queue dry; B deep.
    a.deficit = 20480
    a.refilled_round = arb.current_round
    a.last_active_us = arb.clock_us
    b.refilled_round = arb.current_round
    rounds0 = arb.current_round
    holds0 = arb.round_holds
    picks = [arb.pick() for _ in range(8)]
    ok &= check("s14_hold_serves_deep_backlog",
                all(c != 0 for c in picks) and arb.round_holds > holds0
                and arb.current_round == rounds0,
                f"picks={picks[:4]}... holds={arb.round_holds - holds0} "
                f"rounds={arb.current_round - rounds0} (pin must hold the "
                f"round while serving the stranded key, never NULL)")
    # --- shallow backlog: the pre-S14 NULL exits are preserved ---
    arb2 = WdrrArbiter()
    c, d = Tenant(300, 100), Tenant(400, 100)
    arb2.register(c, 0)
    arb2.register(d, 1)
    # both had this round's turn; c keeps a shallow (sub-threshold) queue
    for _ in range(4):
        arb2.note_inserted(300)
    c.refilled_round = arb2.current_round
    d.refilled_round = arb2.current_round
    saw_null = any(arb2.pick() == 0 for _ in range(64))
    ok &= check("s14_shallow_backlog_keeps_null_exits", saw_null,
                "a sub-threshold backlog must keep the round-boundary NULL "
                "(pre-S14 semantics; the fuse cannot trip there)")
    # --- long deep solo run: never NULL over threshold, debt bounded ---
    arb3 = WdrrArbiter()
    s = Tenant(500, 100)
    arb3.register(s, 0)
    for _ in range(64):                # keep the queue DEEP (over threshold)
        arb3.note_inserted(500)
    worst = 0
    nulls = 0
    for _ in range(2000):
        arb3.note_inserted(500)
        cg = arb3.pick()
        if cg == 0:
            nulls += 1
            continue
        arb3.note_dispatched(cg, 128 * 1024)
        arb3.note_completed(500)
        worst = min(worst, s.deficit)
    ok &= check("s14_deep_backlog_no_nulls", nulls == 0,
                f"NULLs over the fuse threshold: {nulls}")
    ok &= check("s14_overflow_debt_bounded",
                worst >= -(128 * 1024),
                f"worst deficit={worst} bound=-131072 (clamped pre-serve)")
    return ok


def test_s14_gauge_invariant():
    """S14 gauge invariant (v5 forensics: bm measured tot_pend wrapped to
    -2, i.e. an over-decrement path existed): the maintained global
    counter must equal sum(pending) after EVERY mutation site --
    insert, guarded dispatch dec, dispatch-fail ghost reset, void heal
    (serve branch AND overflow serve), S13 corpse reset.  The C-side v5
    wrap was a RACE at the zero sites (read -> sub -> store against a
    concurrent insert/dec; fixed by XCHG-exact zeroing) -- the
    single-threaded model cannot race, but it asserts the DESIGN
    invariant: every mutation site carries its mirror, so any future
    site that forgets one fails here immediately."""
    arb = WdrrArbiter()
    a, b = Tenant(100, 100), Tenant(200, 200)
    arb.register(a, 0)
    arb.register(b, 1)
    ok = True

    def inv(site):
        nonlocal ok
        if not arb._gauge_ok():
            ok = False
            check("s14_gauge_invariant", False,
                  f"gauge={arb.total_pending} vs sum(pending) diverged at {site}")

    # inserts (the +1 mirror)
    for _ in range(10):
        arb.note_inserted(100)
        arb.note_inserted(200)
    inv("note_inserted")
    # guarded dispatch decs (real pops)
    for _ in range(4):
        arb.note_dispatched(100, 4096)
        arb.note_dispatched(200, 4096)
    inv("note_dispatched")
    # guarded dec at 0 must NOT mirror (the v5 suspect #1)
    p0 = arb.total_pending
    for _ in range(5):
        arb.note_dispatched(100, 4096)   # drives pending below its inserts
    inv("note_dispatched_at_zero")
    # dispatch-fail ghost reset (the -whole-value mirror): crystallize the
    # ghost through the REAL insert path so the gauge stays mirrored up
    # to the reset (ad-hoc pending writes would desync it by construction)
    arb.tenants[200].pending = 0
    arb.total_pending -= 6              # b's 6 live entries leave the ledger
    # (the queued entries vanish without a dispatch -- the BUG-005 shape)
    for _ in range(7):                    # the ghost's queued entries
        arb.note_inserted(200)
    arb.tenants[200].dispatch_fails = DISPATCH_FAIL_GHOST_RESET - 1
    arb.note_dispatch_failed(200)
    inv("note_dispatch_failed_ghost_reset")
    # overflow-serve void machinery + heal (drive: deep backlog, exhaust,
    # let the overflow serves run with an empty-queue candidate)
    arb2 = WdrrArbiter()
    c = Tenant(300, 100)
    arb2.register(c, 0)
    for _ in range(64):                   # leftover pending > fuse threshold
        arb2.note_inserted(300)
    while arb2.tenants[300].deficit > 0:
        cg = arb2.pick()
        assert cg == 300
        arb2.note_dispatched(cg, 4096)
    served = arb2.pick()                  # round boundary, backlog > fuse
    assert served == 300
    inv("overflow_serve")
    # The ghost candidate heals through the SERVE branch's cycle: the
    # round advance re-latches refilled_round, the next pick arms it,
    # and the no-progress void turns run there (4 turns -> heal, XCHG
    # gauge mirror).  Drive it: no dispatches are ever booked for the
    # candidate, so every arm->serve is a no-progress void.
    picks = 0
    while arb2.tenants[300].pending > 0:
        got = arb2.pick()
        assert got != 0, "deep-backlog overflow serves must not NULL"
        picks += 1
        assert picks < 400, "ghost pend never healed by the void cycle"
    inv("serve_branch_void_heal")
    healed = arb2.tenants[300].pending
    ok &= check("s14_gauge_invariant", ok and healed == 0
                and arb2.total_pending == 0,
                f"pending after void cycle={healed} "
                f"gauge={arb2.total_pending} (ghost must heal, gauge exact)")
    return ok


def main():
    ok = True
    ok &= test_case1_weight_ratio()
    ok &= test_case2_byte_fairness()
    ok &= test_case3_shallow_queue_weight_ratio()
    ok &= test_work_conservation_and_idle_cap()
    ok &= test_requeue_refund_no_leak()
    ok &= test_overshoot_bound()
    ok &= test_dispatch_failed_pending()
    ok &= test_empty_pop_wedge_rotation()
    ok &= test_dispatch_failed_ghost_reset()
    ok &= test_s11_coloc_byte_share()
    ok &= test_s11_arm_on_dry_accrual()
    ok &= test_s11_hold_and_bank_semantics()
    ok &= test_s11_uniform_lift_preserves_ratio()
    ok &= test_s13_corpse_reset()
    ok &= test_s14_strict_liveness_no_null_on_deep_backlog()
    ok &= test_s14_gauge_invariant()
    print("MODEL " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()