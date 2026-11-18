#!/usr/bin/env python3
"""arb_slo reference model — executable specification of the SLO-aware
DRR arbiter (gate 0 of the arb_slo CI loop).

Mirrors userspace/ioext_arb_common.h (arb_drr_pick with the get_boost_fn
injection point, arb_note_* hooks, arb_slo_update_window FSM) and the
arb_slo.bpf.c hook wiring (dispatch-time BYTES charge — same unit as
arb_wdrr —, per-tenant boost via slo_get_boost, IOSCHED_ARB_STRICT mode,
per-hctx inflight_count map, hold_dispatch LC gate with the slo_hold_cnt
observability counter).  Per ARB_POLICY_DEV_GUIDE.md §1 + §7 the model
expresses the specification BEFORE the BPF is trusted: any change to the
BPF DRR core / SLO FSM must be mirrored here and vice versa.  The
acceptance script tests/arb_slo_accept.sh runs this self-test as its
first gate (`timeout 120 python3 tests/arb_slo_model.py`).
Exit 0 = all PASS.

Model contract (identical to ioext_arb_common.h / ARB_POLICY_DEV_GUIDE.md §2):
  - eligibility: pending > 0 only (arbiter's own count from inserted_request;
    never policy_meta.queue_depth)
  - turn grant: deficit <= 0 and not yet refilled this round
    (refilled_round != current_round) → arm round budget
    quantum * effective_weight (cap 2x budget) and serve immediately
    (refill IS the turn grant — classic DRR; at most one grant per round)
  - round budget floor: max(MIN_BUDGET, last_round_max_charge) — the
    workload-adaptive part (arb_hctx_state) rolls at every round advance
    (arb_note_charge_max fires from dispatched_request; charge unit is
    bytes for SLO)
  - effective_weight = base_weight * per-tenant boost, consumed at REFILL
    time only (mid-turn boost changes do not re-arm the current turn)
  - serve: deficit > 0 → keep selecting the same tenant; each dispatch
    charges bytes = nr_sectors * 512 (dispatch time, arb_wdrr's unit)
  - exhaust: deficit <= 0 after this round's refill → cursor advances
  - round boundary: full scan, nobody servable → current_round += 1,
    return 0 (single NULL dispatch; blk-mq re-kicks on next completion)
  - empty tenant: pending == 0 → skip WITHOUT refill (no credit hoarding)
  - SLO FSM (arb_slo_update_window): 100ms windows, >= 20 samples required,
    violation ⇔ window_over_target * 100 > window_total (>1% of samples
    over target — NO integer truncation), ladder NORMAL→PRESSURE(boost 4)
    →RECOVERY(boost 2, 3 consecutive good windows)→NORMAL, SEVERE
    escalation (boost 8) while >10% of samples violate, boost ∈ [1, 16]
  - boost is strictly PER-TENANT (refine.md POL-02: a global max_boost
    multiplier applied to everyone is a spec failure, not a tuning)
  - hold_dispatch: true iff ANY active LC tenant (arb_prio <= 2) has
    slo_state == SLO_PRESSURE AND per-hctx inflight_count >=
    protect_inflight; each hold decision bumps slo_hold_cnt[hctx]
  - inflight lifecycle: dispatched_request ++ (tenant + per-hctx count),
    completed_request -- (both), requeue_refund -- (both) and NEVER
    pending++ (guide §6 pitfall 4: the kernel re-inserts via
    blk_mq_insert_request → inserted_request)
  - dispatch_failed: pending-- only (guide §6 pitfall 3)
  - BW/BE tenants (arb_prio >= 3) have latency target 0: no window
    updates, boost stays 1 forever (mirror of the arb_slo.bpf.c wiring
    that only calls arb_slo_update_window when lat_target > 0)

Acceptance encoded (refine.md §3 SLO acceptance + POL-02):
  Case 1  A=LC with sustained ~5% violations, B=BE normal →
          simultaneously A.boost > 1 AND B.boost == 1, and long-run
          bytes_A/bytes_B ≈ boost_A/boost_B (4:1 with boost 4 vs 1).
  Case 2  inflight lifecycle: dispatch ++, completion --, requeue --
          with pending untouched by the refund path.
  Case 3  hold gate with protect_inflight=2, QD=8: engages at the
          threshold while LC is in PRESSURE, releases as completions
          drain inflight below it, never fires without LC pressure.

Structurally cloned from tests/arb_cost_model.py (same Tenant/Arbiter
shape, drive_backlogged harness, check() printer, MODEL PASS/FAIL + exit
convention; that file in turn cloned arb_wdrr_model.py).  Unlike the
cost model no fast-forward collapsing is needed: every drive runs
per-request — which the SLO FSM requires anyway (each completion feeds
the window with its own latency), and the S6 bytes-domain budget
(quantum 64B * weight, floored at max(4096, last_round_max_charge))
keeps turns at 1-2 requests anyway.
"""
import sys

# ---------------------------------------------------------------------------
# Constants (encode ioext_arb_common.h + arb_slo.bpf.c verbatim)
# ---------------------------------------------------------------------------

SECTOR = 512                  # charge = nr_sectors * 512 (bytes, like wdrr)
QUANTUM = 64                  # ARB_WDRR_QUANTUM_BYTES (S6: binds per
                              # visit): bytes per unit of effective weight
CAP_MULT = 2                  # ARB_WDRR_IDLE_CAP_MULT: refill cap = 2x budget
MIN_BUDGET = 4096             # ARB_WDRR_MIN_BUDGET_BYTES (S6: one 4K page)
MAX_TENANTS = 16              # ARB_MAX_TENANTS

WINDOW_NS = 100 * 1000 * 1000     # ARB_SLO_WINDOW_NS (100ms)
RECOVERY_WINDOWS = 3              # ARB_SLO_RECOVERY_WINDOWS
MIN_SAMPLES = 20                  # ARB_SLO_MIN_SAMPLES

BOOST_NORMAL = 1              # ARB_SLO_BOOST_NORMAL
BOOST_MILD = 2                # ARB_SLO_BOOST_MILD
BOOST_HIGH = 4                # ARB_SLO_BOOST_HIGH
BOOST_SEVERE = 8              # ARB_SLO_BOOST_SEVERE
BOOST_MAX = 16                # ARB_SLO_BOOST_MAX

LATENCY_TARGET_NS = 100 * 1000    # ARB_SLO_DEFAULT_LATENCY_TARGET_NS (100us)
PROTECT_INFLIGHT = 32             # ARB_SLO_DEFAULT_PROTECT_INFLIGHT

SLO_NORMAL = 0               # enum arb_slo_state
SLO_PRESSURE = 1
SLO_RECOVERY = 2

# Fixture latencies for the FSM drives (target = 100us):
#   GOOD stays under the target, BAD is 2x over it.
GOOD_LAT_NS = 50 * 1000
BAD_LAT_NS = 200 * 1000

# arb_slo_update_window treats window_start_ns == 0 as "not started"
# (ioext_arb_common.h); ktime never returns 0, so the model clock must
# start non-zero too or every sample would re-init the window.
BASE_NS = 1

R4K = 8          # nr_sectors fixtures (bytes = sectors * 512)
R256K = 512
MAX_REQ_BYTES = R256K * SECTOR   # 256KB: largest fixture charge (overshoot)


def slo_is_lc(arb_prio):
    """Mirror arb_slo.bpf.c slo_is_lc(): prio 0-2 are latency-critical."""
    return arb_prio <= 2


def slo_latency_target(arb_prio):
    """Mirror arb_slo.bpf.c slo_latency_target(): LC tenants get the
    default 100us target; BW (3-5) / BE (6-7) get none (0 disables the
    window FSM entirely — the hook wiring skips the update then)."""
    return LATENCY_TARGET_NS if arb_prio <= 2 else 0


def slo_update_window(ts, now_ns, latency_target_ns, max_boost,
                      total_latency_ns):
    """Mirror ioext_arb_common.h arb_slo_update_window() verbatim.

    Called once per completed LC request.  Rolls the 100ms window at
    `now - window_start >= WINDOW_NS`, evaluates the violation rule
    (over * 100 > total, >= MIN_SAMPLES, target > 0 — no integer
    truncation anywhere), advances the NORMAL/PRESSURE/RECOVERY ladder,
    clamps boost into [BOOST_NORMAL, max_boost], then counts the current
    completion into the fresh window and updates the latency EWMA (7/8).
    """
    if ts.window_start_ns == 0:
        ts.window_start_ns = now_ns
        ts.window_total = 0
        ts.window_over_target = 0

    if now_ns - ts.window_start_ns >= WINDOW_NS:
        violation = False

        if ts.window_total >= MIN_SAMPLES and latency_target_ns > 0:
            if ts.window_over_target * 100 > ts.window_total:
                violation = True

        if ts.slo_state == SLO_NORMAL:
            if violation:
                ts.slo_state = SLO_PRESSURE
                ts.boost = BOOST_HIGH
                ts.good_windows = 0
        elif ts.slo_state == SLO_PRESSURE:
            if not violation:
                ts.slo_state = SLO_RECOVERY
                ts.good_windows = 1
                ts.boost = BOOST_MILD
            elif ts.window_total > 0 and \
                    ts.window_over_target * 10 > ts.window_total:
                ts.boost = BOOST_SEVERE
            else:
                ts.boost = BOOST_HIGH
        elif ts.slo_state == SLO_RECOVERY:
            if violation:
                ts.slo_state = SLO_PRESSURE
                ts.boost = BOOST_HIGH
                ts.good_windows = 0
            else:
                ts.good_windows += 1
                if ts.good_windows >= RECOVERY_WINDOWS:
                    ts.slo_state = SLO_NORMAL
                    ts.boost = BOOST_NORMAL

        if ts.boost > max_boost:
            ts.boost = max_boost
        if ts.boost < BOOST_NORMAL:
            ts.boost = BOOST_NORMAL

        ts.window_start_ns = now_ns
        ts.window_total = 0
        ts.window_over_target = 0

    ts.window_total += 1
    if latency_target_ns > 0 and total_latency_ns > latency_target_ns:
        ts.window_over_target += 1

    if ts.ewma_latency_ns == 0:
        ts.ewma_latency_ns = total_latency_ns
    else:
        ts.ewma_latency_ns = (ts.ewma_latency_ns * 7 + total_latency_ns) // 8
    ts.last_latency_ns = total_latency_ns


# ---------------------------------------------------------------------------
# Tenant / Arbiter (mirror arb_tenant_state + arb_drr_pick + arb_note_*)
# ---------------------------------------------------------------------------

class Tenant:
    def __init__(self, cgid, weight, prio):
        self.cgid = cgid
        self.weight = weight
        self.prio = prio
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
        self.wrap_selects = 0      # struct parity; current arb_drr_pick never
                                   # increments it (C parity — keep at 0)
        self.skip_pending_zero = 0
        # SLO FSM fields (arb_tenant_state tail — decoded by TS_FIELDS)
        self.boost = BOOST_NORMAL
        self.slo_state = SLO_NORMAL
        self.window_start_ns = 0
        self.window_total = 0
        self.window_over_target = 0
        self.good_windows = 0
        self.last_latency_ns = 0
        self.ewma_latency_ns = 0

    def budget(self, floor_max=0):
        """Round budget = QUANTUM * effective_weight, effective_weight =
        weight * boost (the get_boost_fn injection point — arb_drr_pick
        consults slo_get_boost at REFILL time only), floored at
        max(MIN_BUDGET, floor_max = last_round_max_charge)."""
        ew = self.weight * self.boost
        if ew == 0:
            ew = 1
        floor = max(MIN_BUDGET, floor_max)
        if QUANTUM * ew < floor:
            ew = (floor + QUANTUM - 1) // QUANTUM
        return QUANTUM * ew


class SloArbiter:
    """Single-hctx SLO-DRR core: slots[0..15] hold cgids (0 = empty),
    cursor points at the last selected slot.  Round semantics mirror
    arb_drr_pick: refill == turn grant, at most once per round; boost is
    consumed by Tenant.budget() at refill time only.  The budget floor is
    workload-adaptive (arb_hctx_state): max(MIN_BUDGET,
    last_round_max_charge), rolled at every round advance.

    hctx_inflight models the per-hctx inflight_count ARRAY map (counts
    dispatched-not-completed across ALL tenants on the hctx — exactly
    what BPF hold_dispatch reads); hold_cnt models slo_hold_cnt."""

    def __init__(self):
        self.slots = [0] * MAX_TENANTS
        self.tenants = {}
        self.cursor = 0
        self.current_round = 0
        self.round_max_charge = 0
        self.last_round_max_charge = 0
        self.hctx_inflight = 0
        self.hold_cnt = 0

    def register(self, tenant, slot):
        self.slots[slot] = tenant.cgid
        self.tenants[tenant.cgid] = tenant

    def pick(self):
        base = self.cursor
        for step in range(MAX_TENANTS):
            idx = (base + step) % MAX_TENANTS
            cgid = self.slots[idx]
            if cgid == 0:
                continue
            ts = self.tenants[cgid]
            if ts.pending == 0:
                ts.skip_pending_zero += 1
                continue
            if ts.deficit > 0:
                # Turn in progress: keep serving this tenant.
                self.cursor = idx
                ts.serve_selects += 1
                return cgid
            if ts.refilled_round != self.current_round:
                # New turn: grant this round's budget (weight * boost at
                # refill time — the slo_get_boost injection) and serve it
                # (refill IS the turn grant).
                budget = ts.budget(self.last_round_max_charge)
                ts.deficit = min(ts.deficit + budget, budget * CAP_MULT)
                ts.refilled_round = self.current_round
                ts.refill_arms += 1
                self.cursor = idx
                return cgid
            # Already had its turn this round: move the cursor past it.
            self.cursor = (idx + 1) % MAX_TENANTS
        # Round complete (or nothing eligible): advance the round and
        # roll the adaptive floor window (arb_drr_pick round advance).
        self.last_round_max_charge = self.round_max_charge
        self.round_max_charge = 0
        self.current_round += 1
        return 0

    def hold_dispatch(self, protect_inflight=PROTECT_INFLIGHT):
        """Mirror arb_slo.bpf.c hold_dispatch(): true iff any active LC
        tenant is in SLO_PRESSURE and the per-hctx inflight count has
        reached protect_inflight (the gate reads per-HCTX inflight, not
        per-tenant).  Each true decision bumps hold_cnt (the slo_hold_cnt
        observability counter the VM acceptance asserts on)."""
        for idx in range(MAX_TENANTS):
            cgid = self.slots[idx]
            if cgid == 0:
                continue
            ts = self.tenants[cgid]
            if not slo_is_lc(ts.prio):
                continue
            if ts.slo_state == SLO_PRESSURE and \
                    self.hctx_inflight >= protect_inflight:
                self.hold_cnt += 1
                return True
        return False

    def advance_cursor(self):
        """Mirror arb_drr_advance_cursor (dispatch_failed hook wiring)."""
        self.cursor = (self.cursor + 1) % MAX_TENANTS

    # framework hook handlers (mirror arb_note_*)

    def note_inserted(self, cgid):
        self.tenants[cgid].pending += 1

    def note_dispatched(self, cgid, nbytes):
        """Mirror arb_note_dispatched_bytes: dispatch-time bytes charge
        (arb_slo's fairness unit — same as wdrr)."""
        ts = self.tenants[cgid]
        ts.deficit -= nbytes
        if ts.pending > 0:
            ts.pending -= 1
        ts.inflight += 1
        self.hctx_inflight += 1
        ts.completed_bytes += nbytes
        ts.dispatches += 1
        ts.dispatch_log.append(nbytes)
        if nbytes > self.round_max_charge:
            self.round_max_charge = nbytes

    def note_requeue(self, cgid, refund):
        """Mirror arb_note_requeue: refund only + inflight--, never
        pending++ (the kernel re-inserts the request, re-firing
        inserted_request — guide §6 pitfall 4).  Refund mirrors the
        dispatched charge (bytes)."""
        ts = self.tenants[cgid]
        if ts.inflight > 0:
            ts.inflight -= 1
        if self.hctx_inflight > 0:
            self.hctx_inflight -= 1
        if refund:
            ts.deficit += refund
            ts.requeued_bytes += refund
        ts.requeues += 1

    def note_completed(self, cgid):
        ts = self.tenants[cgid]
        if ts.inflight > 0:
            ts.inflight -= 1
        if self.hctx_inflight > 0:
            self.hctx_inflight -= 1

    def note_dispatch_failed(self, cgid):
        """Mirror arb_note_dispatch_failed: retire one pending entry only
        (the framework popped the rq before validating it — guide §6
        pitfall 3)."""
        ts = self.tenants[cgid]
        if ts.pending > 0:
            ts.pending -= 1

    # BPF hook wiring (arb_slo.bpf.c)

    def dispatched_request(self, cgid, nr_sectors):
        self.note_dispatched(cgid, nr_sectors * SECTOR)

    def requeue_refund(self, cgid, nr_sectors):
        self.note_requeue(cgid, nr_sectors * SECTOR)

    def completed_request(self, cgid, now_ns, total_latency_ns):
        """Mirror arb_slo.bpf.c completed_request: inflight-- (tenant +
        per-hctx count), then the SLO window update for LC tenants only
        (latency target 0 for BW/BE disables the FSM — B's boost can
        never move, which is what makes boost asymmetry provable)."""
        ts = self.tenants[cgid]
        self.note_completed(cgid)
        lat_target = slo_latency_target(ts.prio)
        if lat_target > 0:
            slo_update_window(ts, now_ns, lat_target, BOOST_MAX,
                              total_latency_ns)

    def dispatch_failed(self, cgid):
        """arb_slo.bpf.c dispatch_failed = arb_note_dispatch_failed
        + arb_drr_advance_cursor."""
        self.note_dispatch_failed(cgid)
        self.advance_cursor()


# ---------------------------------------------------------------------------
# Saturated drive
# ---------------------------------------------------------------------------

def drive_backlogged(arb, requests, rounds=None, total=None,
                     lat=None, step_ns=None):
    """Keep every tenant backlogged at exactly one pending request
    (re-insert after each dispatch, like the framework re-queueing issued
    IO).  `requests` maps cgid -> nr_sectors; every request of a tenant
    inside one drive is identical and charges nr_sectors * 512 bytes —
    exactly what arb_slo.bpf.c charges.

    Stops when `rounds` DRR rounds fully completed (the round-boundary
    NULL pick fired) or `total` dispatches were made (one of the two must
    be given).  A pick returning 0 is a round boundary: blk-mq retries on
    the next completion kick, so the model retries too.

    lat: optional per-cgid completion latency in ns (constant or callable
    f(now_ns) -> latency).  When given, each dispatch is completed through
    arb.completed_request(now, lat) — the SLO window FSM runs live and a
    virtual clock advances `step_ns` per completed request (default 1ms:
    100 completions per 100ms window).  When lat is None completions
    bypass the FSM (arb_note_completed only), like the cost/wdrr drives.
    """
    if rounds is None and total is None:
        raise ValueError("drive_backlogged needs a rounds or total stop condition")
    for cg in requests:
        # backlog invariant: exactly one pending request per listed tenant
        # (idempotent top-up so a drive may resume on a non-empty arbiter)
        if arb.tenants[cg].pending == 0:
            arb.note_inserted(cg)
    served = {cg: 0 for cg in requests}
    now = BASE_NS
    if step_ns is None:
        step_ns = 1000 * 1000            # 1ms: ~100 samples per window
    done = 0
    while (rounds is None or arb.current_round < rounds) and \
            (total is None or done < total):
        cg = arb.pick()
        if cg == 0:
            continue            # round boundary; retry like blk-mq's next kick
        arb.dispatched_request(cg, requests[cg])
        if lat is not None:
            lv = lat[cg]
            lval = lv(now) if callable(lv) else lv
            arb.completed_request(cg, now, lval)
            now += step_ns
        else:
            arb.note_completed(cg)
        served[cg] += 1
        done += 1
        if arb.tenants[cg].pending == 0:
            arb.note_inserted(cg)
    return served


def drive_shallow_queues(arb, requests, total_dispatches, queue_cap=16,
                         device_latency=8, jitter=15, seed=0):
    """Hardware saturated regime (session6_20260920 W4 dump), bytes-charged
    variant of arb_wdrr_model.drive_shallow_queues.  Each dispatched
    request returns to the queue after device_latency + jitter slots
    (completion round-trip; the jitter models completion batching across
    the 48 (tenant, hctx) pipeline queues).  fio keeps outstanding
    (queued + in transit) at queue_cap, so the framework queue is the
    RESIDUAL behind the transit pipeline and pending hits zero
    periodically: visits end pending-bound unless the round budget is
    small enough to bind first.  Completions bypass the SLO window FSM
    (no completed_request calls — boost stays whatever the tenants
    carry, mirroring the wdrr reference drive), locking the pure DRR
    weight regime independently of the latency machinery."""
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
            if arb.tenants[e[1]].pending < queue_cap:
                arb.note_inserted(e[1])
        cg = arb.pick()
        if cg == 0:
            if all(arb.tenants[t].pending == 0 for t in requests):
                idle_slots += 1
                if idle_slots > 6 * queue_cap:
                    raise RuntimeError("drive_shallow_queues starved")
            continue
        arb.dispatched_request(cg, requests[cg])
        returns.append([clock + device_latency + rng.randint(0, jitter), cg])
        served[cg] += 1
        n += 1
        idle_slots = 0
    return served


def check(name, cond, detail=""):
    if cond:
        print(f"PASS: {name}")
        return True
    print(f"FAIL: {name} {detail}")
    return False


# ---------------------------------------------------------------------------
# Window-FSM helper: feed a whole window through the real slo_update_window
# ---------------------------------------------------------------------------

def feed_window(ts, start_ns, total, over,
                lat_target=LATENCY_TARGET_NS, max_boost=BOOST_MAX):
    """Feed `total` in-window completions (`over` of them violating) at
    1us spacing starting at start_ns, then one completion exactly
    WINDOW_NS after the window opened: the window rolls and the FSM
    consumes (window_total=total, window_over_target=over).  The rolling
    completion itself lands in the fresh window (it seeds the NEXT
    window's stats — continued windows see total+1), mirroring the C.
    Returns the new window start (start_ns + WINDOW_NS).
    """
    now = start_ns
    for i in range(total):
        lat = BAD_LAT_NS if i < over else GOOD_LAT_NS
        slo_update_window(ts, now, lat_target, max_boost, lat)
        now += 1000                     # 1us steps: stays inside the window
    slo_update_window(ts, start_ns + WINDOW_NS, lat_target, max_boost,
                      GOOD_LAT_NS)
    return start_ns + WINDOW_NS


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_slo_class_helpers():
    """prio → SLO class / latency target (arb_slo.bpf.c slo_is_lc /
    slo_latency_target): 0-2 LC with the 100us target, 3-7 no target."""
    ok = check("slo_lc_prio_classes",
               all(slo_is_lc(p) for p in (0, 1, 2)) and
               not any(slo_is_lc(p) for p in (3, 4, 5, 6, 7)),
               "LC must be exactly prio 0-2")
    ok &= check("slo_latency_targets",
                slo_latency_target(1) == LATENCY_TARGET_NS and
                all(slo_latency_target(p) == 0 for p in (3, 5, 7)),
                f"target(LC)={LATENCY_TARGET_NS} target(BE)=0")
    return ok


def test_case1_boost_asymmetry():
    """THE POL-02 acceptance case (refine.md §3 Case 1).

    A = LC (prio 1, weight 100) with ~5% of completions violating the
    100us target — enough to breach the 1% violation rule every window,
    never enough to escalate to SEVERE (>10%) — so A sits in PRESSURE
    with boost exactly 4.  B = BE (prio 5, weight 100) whose completions
    never even feed a window (target 0).  Expected, simultaneously:
    A.boost > 1 (== 4), B.boost == 1, and — because boost enters
    effective_weight = weight * boost — the long-run service ratio
    bytes_A/bytes_B ≈ boost_A/boost_B = 4 with equal weights.

    The FORBIDDEN alternative (refine.md POL-02) is a global max_boost:
    A and B multiplied by the same factor.  B.boost == 1 here proves the
    boost is per-tenant.
    """
    arb = SloArbiter()
    a = Tenant(100, 100, prio=1)     # LC randread stand-in
    b = Tenant(200, 100, prio=5)    # BE randwrite stand-in
    arb.register(a, 0)
    arb.register(b, 1)

    # ~5% violations for A: every 20th A completion misses the target.
    # Deterministic, and inside (1%, 10%]: every 100-sample window holds
    # exactly 5 violations (500 > 100 → violation) but never trips the
    # severe line (50 <= 100 → boost stays 4, not 8).
    n_a = [0]

    def lat_a(_now):
        n_a[0] += 1
        return BAD_LAT_NS if n_a[0] % 20 == 0 else GOOD_LAT_NS

    # warm-up: with the S6 quantum a boost-1 round serves only 1-2
    # dispatches (deficit carry alternates 2,2,1,2,1... → ~3 completions
    # per round at the 1ms step), so A's 100ms window needs ~32 rounds
    # to gather its first ~50 samples → PRESSURE/boost 4; rounds=40
    # covers the roll plus several refills armed at boost 4 (round
    # budget 64B*100*4 = 25.6KB → ~6 dispatches per round).
    drive_backlogged(arb, {100: R4K, 200: R4K}, rounds=40,
                     lat={100: lat_a, 200: GOOD_LAT_NS})

    ok = check("case1_lc_pressure_boost",
               a.boost == BOOST_HIGH and a.slo_state == SLO_PRESSURE,
               f"A boost={a.boost} state={a.slo_state} "
               f"(expected {BOOST_HIGH}/PRESSURE)")
    ok &= check("case1_be_boost_untouched",
                b.boost == BOOST_NORMAL and b.slo_state == SLO_NORMAL
                and b.window_total == 0,
                f"B boost={b.boost} state={b.slo_state} "
                f"window_total={b.window_total} (BE never updates)")
    ok &= check("case1_boost_asymmetry_simultaneous",
                a.boost > 1 and b.boost == 1,
                f"A.boost={a.boost} B.boost={b.boost} — a global "
                f"max_boost would have lifted both (POL-02 violation)")

    # measurement: 7 more rounds (rounds= is absolute: 40 + 7) from a
    # round boundary with boost 4 armed — long-run bytes converge to the
    # budget ratio 4 (round budget 25.6KB vs 6.4KB; per-round overshoot
    # < one 4K request).
    bytes_a0, bytes_b0 = a.completed_bytes, b.completed_bytes
    drive_backlogged(arb, {100: R4K, 200: R4K}, rounds=47,
                     lat={100: lat_a, 200: GOOD_LAT_NS})
    dfa = a.completed_bytes - bytes_a0
    dfb = b.completed_bytes - bytes_b0
    ratio = dfa / dfb if dfb else 0
    ok &= check("case1_boost_service_ratio_4", 3.5 <= ratio <= 4.5,
                f"bytes A/B={ratio:.3f} (A={dfa} B={dfb}, expected ~4.0: "
                f"boost 4 vs 1 with equal weights)")
    ok &= check("case1_boost_holds_steady",
                a.boost == BOOST_HIGH and a.slo_state == SLO_PRESSURE
                and b.boost == BOOST_NORMAL,
                f"A boost={a.boost} state={a.slo_state} "
                f"(5% violations must stay PRESSURE/4, not SEVERE/8)")
    return ok


def test_boost_consumed_at_refill():
    """Boost enters effective_weight at REFILL time only (ew = weight *
    boost inside arb_drr_pick's refill branch).  Mid-turn boost changes
    must NOT re-arm the current turn; the next round's refill picks up
    the new boost."""
    arb = SloArbiter()
    a = Tenant(100, 100, prio=1)
    arb.register(a, 0)
    served = drive_backlogged(arb, {100: R4K}, rounds=1)
    # boost 1: round budget 64*100 = 6400B → 2 dispatches (one full 4K
    # plus one overshoot; deficit ends -1792)
    b1 = 2 * 4096
    ok = check("boost_refill_round1_budget",
               served[100] * 4096 == b1 and a.refill_arms == 1,
               f"served={served[100] * 4096} expected={b1} "
               f"refills={a.refill_arms}")
    a.boost = 8                               # boost change between turns
    served = drive_backlogged(arb, {100: R4K}, rounds=2)
    # boost 8: round budget 64*800 = 51200B (armed at 49408 after the
    # -1792 carry) → 13 dispatches (12 full plus overshoot)
    b8 = 13 * 4096
    ok &= check("boost_refill_uses_new_boost",
                served[100] * 4096 == b8 and a.refill_arms == 2,
                f"served={served[100] * 4096} expected={b8} "
                f"refills={a.refill_arms}")
    # mid-turn change: arm one request's worth of budget, drop the boost,
    # dispatch once — no refill may fire mid-round (refill_arms stays 2)
    # and the charge still comes out of the armed budget.
    a.boost = 1
    a.deficit = 4096
    a.refilled_round = arb.current_round      # already granted this round
    served = drive_backlogged(arb, {100: R4K}, total=1)
    ok &= check("boost_midturn_no_rearm",
                served[100] == 1 and a.deficit == 0
                and a.refill_arms == 2,
                f"served={served[100]} deficit={a.deficit} "
                f"refills={a.refill_arms} (mid-turn boost change must "
                f"not add budget)")
    return ok


def test_slo_window_violation_math():
    """Violation rule: window_over_target * 100 > window_total — the
    over-fraction must exceed 1% with NO integer truncation (refine.md
    POL-02 item 4: `over * 100 / total > 1` truncates and misses real
    violations)."""

    def rolled_state(total, over):
        ts = Tenant(100, 100, prio=1)
        feed_window(ts, BASE_NS, total, over)
        return ts.slo_state

    # 1.33% over: violation.  A truncating implementation computes
    # 2*100/150 = 1 (integer) and tests 1 > 1 → false: it would MISS
    # this violation; the spec formula (200 > 150) catches it.
    ok = check("violation_math_2_of_150",
               rolled_state(150, 2) == SLO_PRESSURE,
               "over*100=200 > total=150 → PRESSURE; truncating "
               "200/150=1 > 1 would miss it")
    # 0.67% over: below the 1% line → NOT a violation (100 <= 150).
    ok &= check("violation_math_1_of_150_no_violation",
                rolled_state(150, 1) == SLO_NORMAL,
                "over*100=100 <= total=150 → NORMAL")
    # 1/99 ≈ 1.01%: the smallest integral pair above the 1% line.
    ok &= check("violation_math_1_of_99",
                rolled_state(99, 1) == SLO_PRESSURE,
                "over*100=100 > total=99 → PRESSURE")
    # exactly 1%: strict inequality → NOT a violation.
    ok &= check("violation_math_boundary_strict",
                rolled_state(100, 1) == SLO_NORMAL,
                "over*100=100 > total=100 is false → NORMAL")
    # zero violations can never trip it.
    ok &= check("violation_math_zero_over",
                rolled_state(100, 0) == SLO_NORMAL,
                "over*100=0 > total=100 is false")
    return ok


def test_slo_min_samples():
    """window_total < ARB_SLO_MIN_SAMPLES (20) suppresses the violation
    declaration entirely (refine.md POL-02 item 4: don't judge thin
    windows); 20 samples with all 20 violating is the first decidable
    window.  latency_target 0 (BW/BE) never declares violations either."""
    ts = Tenant(100, 100, prio=1)
    feed_window(ts, BASE_NS, MIN_SAMPLES - 1, MIN_SAMPLES - 1)  # 19/19 bad
    ok = check("min_samples_19_suppressed",
               ts.slo_state == SLO_NORMAL,
               f"state={ts.slo_state} (19 samples, all violating → "
               f"suppressed below MIN_SAMPLES={MIN_SAMPLES})")
    ts = Tenant(100, 100, prio=1)
    feed_window(ts, BASE_NS, MIN_SAMPLES, MIN_SAMPLES)        # 20/20 bad
    ok &= check("min_samples_20_decided",
                ts.slo_state == SLO_PRESSURE,
                f"state={ts.slo_state} (20 samples, all violating → "
                f"PRESSURE)")
    ts = Tenant(100, 100, prio=5)      # BE: target 0 → FSM disabled
    feed_window(ts, BASE_NS, 100, 100, lat_target=0)
    # C parity: window_total++ is unconditional even for target 0, but
    # window_over_target is gated by target > 0, so no violation can ever
    # be declared.  The production wiring skips the update entirely for
    # BE (proven through the wiring by case1_be_boost_untouched).
    ok &= check("min_samples_no_target_never",
                ts.slo_state == SLO_NORMAL and ts.boost == BOOST_NORMAL
                and ts.window_over_target == 0,
                f"state={ts.slo_state} over={ts.window_over_target} "
                f"(latency target 0 → window FSM disabled)")
    return ok


def test_slo_ladder_recovery():
    """The NORMAL→PRESSURE(4)→RECOVERY(2)→NORMAL(1) ladder with 3 good
    windows to exit, RECOVERY relapse back to PRESSURE(4), the SEVERE(8)
    escalation above 10% violations with its boundary, and the
    [1, max_boost] boost clamp.  Continued windows carry one seeded
    sample from the previous roll (feed_window contract), so totals are
    the fed count + 1 — all margins below survive that ±1."""
    ts = Tenant(100, 100, prio=1)

    # NORMAL --violation--> PRESSURE boost 4
    t = feed_window(ts, BASE_NS, 100, 5)
    ok = check("ladder_normal_to_pressure",
               ts.slo_state == SLO_PRESSURE and ts.boost == BOOST_HIGH,
               f"state={ts.slo_state} boost={ts.boost}")

    # PRESSURE, sustained ~5% (over*10 <= total) → stays boost 4
    t = feed_window(ts, t, 100, 5)
    ok &= check("ladder_pressure_holds_4",
                ts.slo_state == SLO_PRESSURE and ts.boost == BOOST_HIGH,
                f"state={ts.slo_state} boost={ts.boost}")

    # PRESSURE, sustained ~50% (over*10 > total) → SEVERE boost 8
    t = feed_window(ts, t, 100, 50)
    ok &= check("ladder_severe_escalation",
                ts.slo_state == SLO_PRESSURE and ts.boost == BOOST_SEVERE,
                f"state={ts.slo_state} boost={ts.boost}")

    # SEVERE boundary is strict: <=10% over → back to boost 4; >10% → 8
    ts2 = Tenant(100, 100, prio=1)
    t2 = feed_window(ts2, BASE_NS, 100, 50)   # enter PRESSURE, severe
    t2 = feed_window(ts2, t2, 100, 10)       # ~10%: 10*10 !> 101
    ok &= check("ladder_severe_boundary_10pct",
                ts2.boost == BOOST_HIGH,
                f"boost={ts2.boost} (over*10=100 <= total → not severe)")
    t2 = feed_window(ts2, t2, 100, 11)       # ~11%: 11*10 > 101 → severe
    ok &= check("ladder_severe_boundary_11pct",
                ts2.boost == BOOST_SEVERE,
                f"boost={ts2.boost} (over*10=110 > total → severe)")

    # good window: PRESSURE → RECOVERY boost 2 (good_windows = 1)
    t = feed_window(ts, t, 100, 0)
    ok &= check("ladder_pressure_to_recovery",
                ts.slo_state == SLO_RECOVERY and ts.boost == BOOST_MILD
                and ts.good_windows == 1,
                f"state={ts.slo_state} boost={ts.boost} "
                f"good={ts.good_windows}")

    # relapse: RECOVERY + violation → PRESSURE boost 4, good reset
    t = feed_window(ts, t, 100, 5)
    ok &= check("ladder_recovery_relapse",
                ts.slo_state == SLO_PRESSURE and ts.boost == BOOST_HIGH
                and ts.good_windows == 0,
                f"state={ts.slo_state} boost={ts.boost} "
                f"good={ts.good_windows}")

    # full recovery: 3 consecutive good windows → NORMAL boost 1
    t = feed_window(ts, t, 100, 0)          # → RECOVERY, good=1
    t = feed_window(ts, t, 100, 0)          # good=2
    ok &= check("ladder_recovery_two_good",
                ts.slo_state == SLO_RECOVERY and ts.boost == BOOST_MILD
                and ts.good_windows == 2,
                f"state={ts.slo_state} good={ts.good_windows}")
    t = feed_window(ts, t, 100, 0)          # good=3 → NORMAL
    ok &= check("ladder_recovery_to_normal",
                ts.slo_state == SLO_NORMAL and ts.boost == BOOST_NORMAL
                and ts.good_windows == RECOVERY_WINDOWS,
                f"state={ts.slo_state} boost={ts.boost} "
                f"good={ts.good_windows}")

    # max_boost clamp: the ladder's ceiling is capped at max_boost
    # (production passes ARB_SLO_BOOST_MAX=16 which never binds the
    # 8-ladder; the clamp is still part of the contract).
    ts3 = Tenant(100, 100, prio=1)
    feed_window(ts3, BASE_NS, 100, 5, max_boost=2)
    ok &= check("ladder_max_boost_clamp",
                ts3.boost == 2 and ts3.slo_state == SLO_PRESSURE,
                f"boost={ts3.boost} (clamped to max_boost=2)")
    ok &= check("ladder_boost_floor",
                ts3.boost >= BOOST_NORMAL,
                f"boost={ts3.boost} >= {BOOST_NORMAL}")
    return ok


def test_case3_hold_gate():
    """THE spec hold case (refine.md §3 Case 3): protect_inflight=2,
    QD=8.  The gate engages once the per-hctx inflight count reaches the
    threshold while an LC tenant is in PRESSURE, releases as completions
    drain inflight below it, and NEVER fires without LC pressure —
    neither for a BE-only workload nor for an LC tenant in NORMAL."""

    def pick_until(arb):
        cg = arb.pick()
        while cg == 0:
            cg = arb.pick()             # round boundary: retry
        return cg

    # precondition: drive the LC tenant into PRESSURE through the real
    # window FSM (25 samples, 5 violating → 500 > 25).
    arb = SloArbiter()
    a = Tenant(100, 100, prio=1)     # LC
    b = Tenant(200, 100, prio=5)    # BE
    arb.register(a, 0)
    arb.register(b, 1)
    feed_window(a, BASE_NS, 25, 5)
    ok = check("hold_precondition_lc_pressure",
               a.slo_state == SLO_PRESSURE,
               f"state={a.slo_state} (setup: A must be in PRESSURE)")

    # QD=8 queued; dispatch 2 → per-hctx inflight == protect_inflight
    # → hold engages (counted in slo_hold_cnt).
    for _ in range(8):
        arb.note_inserted(100)
    for _ in range(2):
        arb.dispatched_request(pick_until(arb), R4K)
    ok &= check("hold_engages_at_threshold",
                arb.hctx_inflight == 2 and arb.hold_dispatch(2) is True
                and arb.hold_cnt == 1,
                f"inflight={arb.hctx_inflight} hold_cnt={arb.hold_cnt} "
                f"(protect_inflight=2)")
    # one completion drops inflight to 1 → hold releases.
    arb.completed_request(100, BASE_NS + WINDOW_NS, GOOD_LAT_NS)
    ok &= check("hold_releases_after_completion",
                arb.hctx_inflight == 1 and arb.hold_dispatch(2) is False,
                f"inflight={arb.hctx_inflight} (below threshold → no hold)")
    # dispatch back to the threshold → engages again.
    arb.dispatched_request(pick_until(arb), R4K)
    ok &= check("hold_reengages_when_refilled",
                arb.hold_dispatch(2) is True and arb.hold_cnt == 2,
                f"inflight={arb.hctx_inflight} hold_cnt={arb.hold_cnt}")

    # (a) BE-only workload: 8 in flight (>= threshold), LC idle → the
    # gate must never fire (no LC tenant is in PRESSURE).
    arb2 = SloArbiter()
    a2 = Tenant(100, 100, prio=1)
    b2 = Tenant(200, 100, prio=5)
    arb2.register(a2, 0)
    arb2.register(b2, 1)
    for _ in range(8):
        arb2.note_inserted(200)
        arb2.dispatched_request(pick_until(arb2), R4K)
    ok &= check("hold_never_without_pressure_be_only",
                arb2.hctx_inflight == 8 and arb2.hold_dispatch(2) is False
                and arb2.hold_cnt == 0,
                f"inflight={arb2.hctx_inflight} hold_cnt={arb2.hold_cnt} "
                f"(BE saturating, LC idle: no PRESSURE → no hold)")
    # (b) LC present but NORMAL: inflight at the threshold, no pressure.
    arb3 = SloArbiter()
    a3 = Tenant(100, 100, prio=1)
    arb3.register(a3, 0)
    for _ in range(2):
        arb3.note_inserted(100)
        arb3.dispatched_request(pick_until(arb3), R4K)
    ok &= check("hold_never_with_lc_normal",
                arb3.hctx_inflight == 2 and arb3.hold_dispatch(2) is False
                and arb3.hold_cnt == 0,
                f"inflight={arb3.hctx_inflight} state={a3.slo_state} "
                f"(LC NORMAL at threshold → no hold)")
    return ok


def test_case4_shallow_queue_weight_ratio():
    """The hardware saturated regime (20260919 W4 run), SLO variant:
    shallow queues, completion-latency-bound refill, latency-target
    machinery dormant (drive_shallow_queues never feeds the window FSM
    → boost stays BOOST_NORMAL for both).  Pre-S6 constants (32KiB
    quantum, 256KB floor) degenerate this to equal share; the S6 quantum
    must keep the budget binding per visit so service splits by weight
    (byte ratio == dispatch ratio: uniform 4K requests)."""
    arb = SloArbiter()
    a = Tenant(100, 100, prio=1)     # LC class, FSM dormant in this drive
    b = Tenant(300, 300, prio=5)     # BE class, boost can never move
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_shallow_queues(arb, {100: R4K, 300: R4K}, 40000)
    ba, bb = served[100] * 4096, served[300] * 4096
    ratio = bb / ba
    ok = check("case4_shallow_queue_weight_ratio", 2.0 <= ratio <= 4.0,
               f"ratio={ratio:.3f} expected~3 (window [2.0,4.0])")
    ok &= check("case4_boost_static",
                a.boost == BOOST_NORMAL and b.boost == BOOST_NORMAL
                and a.slo_state == SLO_NORMAL and b.slo_state == SLO_NORMAL,
                f"A boost={a.boost} B boost={b.boost} "
                f"(no window machinery active in this drive)")
    return ok


def test_case2_inflight_lifecycle():
    """THE spec lifecycle case (refine.md §3 Case 2): dispatched_request
    inflight++ (tenant + per-hctx count), completed_request inflight--,
    requeue_refund inflight-- with pending NEVER touched by the refund
    (guide §6 pitfall 4 — the kernel re-insert re-fires inserted_request).
    Net conservation at rest: inflight == 0 everywhere."""
    arb = SloArbiter()
    a = Tenant(100, 100, prio=1)
    arb.register(a, 0)

    # 4 inserts, 4 dispatches: pending 4→0, inflight 0→4 (both counters)
    for _ in range(4):
        arb.note_inserted(100)
    for _ in range(4):
        cg = arb.pick()
        while cg == 0:
            cg = arb.pick()
        arb.dispatched_request(cg, R4K)
    ok = check("lifecycle_dispatch_increments",
               a.pending == 0 and a.inflight == 4 and arb.hctx_inflight == 4,
               f"pending={a.pending} inflight={a.inflight} "
               f"hctx={arb.hctx_inflight}")

    # 2 completions (through the full hook wiring): 4→2 both counters
    arb.completed_request(100, BASE_NS, GOOD_LAT_NS)
    arb.completed_request(100, BASE_NS + 1000, GOOD_LAT_NS)
    ok &= check("lifecycle_completion_decrements",
                a.inflight == 2 and arb.hctx_inflight == 2,
                f"inflight={a.inflight} hctx={arb.hctx_inflight}")

    # requeue the third: refund bytes + inflight-- on BOTH counters, and
    # pending must stay 0 (no pending++ in the refund path).
    armed = a.deficit
    arb.requeue_refund(100, R4K)
    ok &= check("lifecycle_requeue_decrements_no_pending",
                a.inflight == 1 and arb.hctx_inflight == 1
                and a.pending == 0,
                f"inflight={a.inflight} hctx={arb.hctx_inflight} "
                f"pending={a.pending} (must stay 0)")
    # refund returned the exact charged bytes to the deficit
    ok &= check("lifecycle_requeue_refunds_charge",
                a.deficit == armed + 4096 and a.requeued_bytes == 4096
                and a.requeues == 1,
                f"deficit={a.deficit} armed={armed} "
                f"requeued_bytes={a.requeued_bytes}")

    # kernel re-insert restores pending through inserted_request only
    arb.note_inserted(100)
    ok &= check("lifecycle_reinsert_restores_pending",
                a.pending == 1 and a.inflight == 1,
                f"pending={a.pending} inflight={a.inflight}")

    # drain: dispatch the re-inserted request + complete both in flight
    # → conservation at rest (pending/inflight/hctx all zero)
    cg = arb.pick()
    while cg == 0:
        cg = arb.pick()
    arb.dispatched_request(cg, R4K)
    arb.completed_request(100, BASE_NS + 2000, GOOD_LAT_NS)
    arb.completed_request(100, BASE_NS + 3000, GOOD_LAT_NS)
    ok &= check("lifecycle_conservation_at_rest",
                a.pending == 0 and a.inflight == 0
                and arb.hctx_inflight == 0,
                f"pending={a.pending} inflight={a.inflight} "
                f"hctx={arb.hctx_inflight}")
    return ok


def test_dispatch_failed_pending():
    """Invalid-rq path: the framework popped the rq before validating it,
    so dispatch_failed retires one pending entry (only).  An emptied
    tenant must never be picked — strict mode returns NULL instead."""
    arb = SloArbiter()
    a = Tenant(100, 100, prio=1)
    arb.register(a, 0)
    arb.note_inserted(100)
    cg = arb.pick()
    assert cg == 100
    armed = a.deficit
    arb.dispatch_failed(100)         # note_dispatch_failed + advance
    ok = check("dispatch_failed_retires_pending",
               a.pending == 0 and a.deficit == armed,
               f"pending={a.pending} deficit={a.deficit} armed={armed} "
               f"(dispatch_failed must touch pending ONLY)")
    ok &= check("dispatch_failed_empty_not_picked", arb.pick() == 0,
                "empty tenant must not be picked (strict NULL)")
    return ok


def test_drr_invariants():
    """DRR core invariants under the bytes charge + boost injection:
    overshoot bounded by one max request, empty tenants hoard no credit
    and are never refilled, each tenant refills at most once per round,
    and the idle credit cap (2x budget) holds."""
    # overshoot: charging can push deficit negative only within one
    # max request (pick selects strictly at deficit > 0).
    arb = SloArbiter()
    a = Tenant(100, 100, prio=1)
    arb.register(a, 0)
    worst = 0
    for _ in range(500):
        arb.note_inserted(100)
        cg = arb.pick()
        while cg == 0:
            cg = arb.pick()
        arb.dispatched_request(cg, R256K)
        arb.note_completed(100)
        worst = min(worst, a.deficit)
    ok = check("drr_overshoot_bound",
               worst >= -MAX_REQ_BYTES,
               f"worst deficit={worst} bound=-{MAX_REQ_BYTES}")

    # empty tenant: B idle for 3 full rounds while A runs → no refill,
    # no credit, no dispatch; A gets everything (work conservation) and
    # A's deficit never exceeds 2x budget (idle credit cap).
    arb = SloArbiter()
    a = Tenant(100, 100, prio=1)
    b = Tenant(200, 100, prio=5)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: R4K}, rounds=3)
    ok &= check("drr_empty_tenant_no_credit",
                b.deficit == 0 and b.refill_arms == 0
                and b.dispatches == 0 and b.pending == 0,
                f"B deficit={b.deficit} refills={b.refill_arms}")
    # 3 rounds x boost-1 budget 6400B = 19200B ≈ 4.7 requests → 5
    # dispatches (20480B; overshoot < one 4K request, like the C)
    expected = 3 * 100 * QUANTUM
    ok &= check("drr_work_conservation",
                abs(served[100] * 4096 - expected) < 4096
                and a.dispatches == served[100],
                f"A served={served[100]} bytes={served[100] * 4096} "
                f"(~{expected}, |skew| < one 4K request)")
    ok &= check("drr_idle_credit_cap",
                a.deficit <= a.budget() * CAP_MULT,
                f"A deficit={a.deficit} cap={a.budget() * CAP_MULT}")

    # refill once per round: both backlogged, 3 rounds → exactly 3
    # refills each (the refill IS the once-per-round turn grant).
    arb = SloArbiter()
    a = Tenant(100, 100, prio=1)
    b = Tenant(200, 100, prio=5)
    arb.register(a, 0)
    arb.register(b, 1)
    drive_backlogged(arb, {100: R4K, 200: R4K}, rounds=3)
    ok &= check("drr_refill_once_per_round",
                a.refill_arms == 3 and b.refill_arms == 3
                and a.refilled_round == arb.current_round - 1
                and b.refilled_round == arb.current_round - 1,
                f"A refills={a.refill_arms} B refills={b.refill_arms} "
                f"round={arb.current_round}")
    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ok = True
    ok &= test_slo_class_helpers()
    ok &= test_case1_boost_asymmetry()
    ok &= test_boost_consumed_at_refill()
    ok &= test_slo_window_violation_math()
    ok &= test_slo_min_samples()
    ok &= test_slo_ladder_recovery()
    ok &= test_case3_hold_gate()
    ok &= test_case4_shallow_queue_weight_ratio()
    ok &= test_case2_inflight_lifecycle()
    ok &= test_dispatch_failed_pending()
    ok &= test_drr_invariants()
    print("MODEL " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()