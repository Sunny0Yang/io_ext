#!/usr/bin/env python3
"""arb_iocost reference model — executable specification of the io.cost
homomorphic arbiter (gate 0 of the arb_iocost CI loop).

io.cost 同态复现 (HOMOMORPHIC REPRODUCTION): linear COST model + qos
VRATE FEEDBACK.  Mirrors userspace/arb_iocost.bpf.c:

  - arb_iocost_estimate(): per-request cost from the loader-pushed cfg
    struct (quantum, min_budget, cost_read[5], cost_write[5], qos tail);
    a zero entry or a missing cfg falls back to the compiled-in
    byte-linear table (the model's DEFAULT_TABLE)
  - the qos VRATE state machine: LC-role completions (prio <= 2, the
    arb_iolatency口径) feed ONE global 100ms window (>= 20 samples);
    a violating window (fraction over rlat > (100 - rpct)% == the exact
    p(100-rpct) > rlat percentile condition) counts late; 2 consecutive
    late windows cut vrate x0.80 (256ths fp ladder:
    256,205,164,131,104,83,66,64, clamp [vrate_min, vrate_max]); 2
    consecutive comfortable windows (window mean < rlat/2) raise vrate
    x1.05
  - the vrate APPLICATION: eff_quantum = ceil(quantum * vrate) scales
    EVERY cgroup's nominal round budget together (io.cost's global
    throttle -- the uniform component), and iocost_get_boost applies the
    LC inuse margin clamp(ceil(256^2 / vrate_fp^2), 1, 16) on top
    (io.cost's per-target debt/margin layer): the cut lands on BE (the
    surplus consumer), the LC:BE issuance ratio moves 1/vrate^2 LC-ward
    -- the native io.cost observable (LC keeps ~uncapped throughput, BE
    throttled below its weight share)
  - vrate == 1.0 (never-fed zero state, or qos disabled via rlat 0) is
    BIT-IDENTICAL to the previous byte-fair arb_iocost: margin 1,
    eff_quantum == quantum, eff_min_budget == min_budget
  - arb_drr_pick() with the CURRENT full core (F2 serve-no-progress
    rotation + S10 strict round hold + S11.1 cgroup-outstanding demand
    accrual, cloned from tests/arb_wdrr_model.py — the canonical
    spec of the shared C core; the pre-F2/S10 simplification in
    arb_cost_model.py's pick is stale and must not be cloned)
  - arb_note_dispatched_cost / arb_note_requeue / arb_note_completed /
    arb_note_dispatch_failed hook semantics (charge unit = COST)
  - IOSCHED_ARB_STRICT; boost is the vrate margin (iocost_get_boost)

Round budget = eff_quantum * arb_weight * margin * ew_mult cost units;
floor = max(min_budget, uniform ew_mult lift) — the absolute floor stays
UNSCALED by vrate (a per-visit liveness floor, not a share knob) —
rolled at every round advance (arb_note_charge_max fires per dispatch).

Self-test convention: cloned from tests/arb_cost_model.py /
arb_wdrr_model.py (check() printer, MODEL PASS/FAIL + exit code).
Exit 0 = all PASS.
"""
import sys

# ---------------------------------------------------------------------------
# Constants (encode ioext_arb_common.h + arb_iocost.bpf.c verbatim)
# ---------------------------------------------------------------------------

SECTOR = 512                  # arb_cost_size_bucket: bytes = nr_sectors * 512
CAP_MULT = 2                  # ARB_WDRR_IDLE_CAP_MULT: refill cap = 2x budget
MAX_TENANTS = 16              # ARB_MAX_TENANTS
SERVE_NO_PROGRESS_MAX = 3     # ARB_SERVE_NO_PROGRESS_MAX (F2 liveness)
DISPATCH_FAIL_GHOST_RESET = 4  # ARB_DISPATCH_FAIL_GHOST_RESET (F2 liveness)
ROUND_IDLE_RELEASE_US = 20000  # ARB_ROUND_IDLE_RELEASE_NS / 1000 (S10)
MAX_EW_MULT = 32              # ARB_WDRR_MAX_EW_MULT (S11 lift cap)
MODEL_CLOCK_US_PER_PICK = 1    # 1 us per pick() call (model clock)

# Compiled-in defaults (arb_iocost.bpf.c S11 recalibration): BYTE-LINEAR
# table (cost = bytes/64, read == write) + quantum 8.  Rationale (see
# arb_iocost.bpf.c's cost-domain comment): io.cost's linear model on
# SSD-class devices charges ~equal cost per byte (campaign device's
# io.cost.model: rbps=488MB/s vs wbps=427MB/s, a 1.14x write factor, not
# 4x), so byte-linear is the faithful fixed approximation and cost-fair
# service == byte-fair service: weight ratios reproduce io.weight's
# byte-share contract (homo-v2 coloc weights 100:200 -> be_share 0.667).
# quantum 8: budget(w) = 8*w cost = w/8 4K-read dispatches (w=100 ->
# 12.5/round, arb_wdrr's per-visit shape; every w >= 8 keeps its budget
# above one 64K request's cost, so the uniform lift stays at 1 here).
DEFAULT_QUANTUM = 8            # ARB_IOCOST_QUANTUM
DEFAULT_MIN_BUDGET = 64        # ARB_IOCOST_MIN_BUDGET (one 4K read)
DEFAULT_TABLE = (
    (64, 256, 1024, 2048, 4096),      # READ  buckets: 4K/16K/64K/128K/>128K
    (64, 256, 1024, 2048, 4096),      # WRITE buckets: byte-linear, read == write
)

# ---------------------------------------------------------------------------
# QoS vrate feedback constants (mirror arb_iocost.bpf.c)
# ---------------------------------------------------------------------------

QOS_WINDOW_NS = 100_000_000     # ARB_SLO_WINDOW_NS (100 ms)
QOS_MIN_SAMPLES = 20            # ARB_SLO_MIN_SAMPLES
QOS_HYST = 2                    # ARB_IOCOST_QOS_HYST (consecutive windows)
DEFAULT_QOS_RLAT_NS = 2_000_000  # ARB_IOCOST_QOS_RLAT_NS_DEFAULT (2 ms)
DEFAULT_QOS_RPCT = 99            # ARB_IOCOST_QOS_RPCT_DEFAULT
PRIO_LC_MAX = 2                 # ARB_IOCOST_PRIO_LC_MAX (arb_iolatency口径)
PRIO_BE_DEFAULT = 7             # BE band default for unmarked tenants
VRATE_FP_ONE = 256              # ARB_IOCOST_VRATE_FP_ONE (1.0 in 256ths)
VRATE_MIN_FP_DEFAULT = 64       # 0.25
VRATE_MAX_FP_DEFAULT = 1024     # 4.0
VRATE_DOWN_NUM, VRATE_DOWN_DEN = 205, 256   # ~x0.80 per late step
VRATE_UP_NUM, VRATE_UP_DEN = 269, 256       # ~x1.05 per good step
MARGIN_MAX = 16                 # ARB_IOCOST_MARGIN_MAX

ARB_COST_OP_READ = 0
ARB_COST_OP_WRITE = 1
N_BUCKETS = 5

# Request fixtures: (opf, nr_sectors).  opf low byte: 0 = READ, 1 = WRITE.
R4K = (0, 8)       # bucket 0
W4K = (1, 8)       # bucket 0
R64K = (0, 128)    # bucket 2
W64K = (1, 128)    # bucket 2
R128K = (0, 256)   # bucket 3
W256K = (1, 512)   # bucket 4


def arb_cost_size_bucket(nr_sectors):
    nbytes = nr_sectors * SECTOR
    if nbytes <= 4096:
        return 0
    if nbytes <= 16384:
        return 1
    if nbytes <= 65536:
        return 2
    if nbytes <= 131072:
        return 3
    return 4


def arb_cost_op_type(opf):
    return ARB_COST_OP_WRITE if (opf & 0xFF) == 1 else ARB_COST_OP_READ


def arb_iocost_estimate(opf, nr_sectors, cfg=None):
    """Mirror arb_iocost.bpf.c arb_iocost_estimate(): cfg-table cost with
    per-entry zero fallback to the compiled-in arb_cost_estimate()."""
    op = arb_cost_op_type(opf)
    bucket = arb_cost_size_bucket(nr_sectors)
    if cfg is not None:
        row = cfg.cost_write if op == ARB_COST_OP_WRITE else cfg.cost_read
        cost = row[bucket]
        if cost:
            return cost
    return DEFAULT_TABLE[op][bucket]


class IocostCfg:
    """Mirror of struct arb_iocost_cfg (arb_iocost.bpf.c) as pushed by
    loader_arb_iocost.c.  cost_table = (read_row, write_row) or None for
    the compiled-in defaults.  qos_rlat_ns=0 disables the vrate feedback
    (vrate pinned at 1.0 == pure byte-fair)."""

    def __init__(self, quantum=DEFAULT_QUANTUM, min_budget=DEFAULT_MIN_BUDGET,
                 cost_table=None, qos_rlat_ns=DEFAULT_QOS_RLAT_NS,
                 qos_rpct=DEFAULT_QOS_RPCT,
                 vrate_min_fp=VRATE_MIN_FP_DEFAULT,
                 vrate_max_fp=VRATE_MAX_FP_DEFAULT):
        self.quantum = quantum
        self.min_budget = min_budget
        table = cost_table if cost_table is not None else DEFAULT_TABLE
        self.cost_read = list(table[0])
        self.cost_write = list(table[1])
        self.qos_rlat_ns = qos_rlat_ns
        self.qos_rpct = min(max(qos_rpct, 1), 100)
        self.vrate_min_fp = max(vrate_min_fp, 1)
        self.vrate_max_fp = max(vrate_max_fp, self.vrate_min_fp)
        self.runtime_eff_quantum = 0   # BPF roller-owned cache; 0 = vrate 1.0


class QosState:
    """Mirror of struct arb_iocost_qos (the BPF's single global FSM slot;
    zero-init == never-fed, read back as vrate 1.0)."""

    def __init__(self):
        self.window_start_ns = 0
        self.window_sum_ns = 0
        self.window_total = 0
        self.window_over = 0
        self.late_windows = 0
        self.good_windows = 0
        self.vrate_fp = 0          # 0 = never fed (reads as VRATE_FP_ONE)
        self.down_steps = 0
        self.up_steps = 0
        self.last_violation = 0
        self.last_comfortable = 0
        self.last_sample_ns = 0


# ---------------------------------------------------------------------------
# Tenant / Arbiter (mirror arb_tenant_state + arb_drr_pick + arb_note_*)
# ---------------------------------------------------------------------------

class Tenant:
    def __init__(self, cgid, weight, prio=PRIO_BE_DEFAULT):
        self.cgid = cgid
        self.weight = weight
        self.prio = prio          # arb role band (q-side -p); <=2 = LC
        self.boost = 1            # margin cache: arb_ensure_tenant inits
                                  # ts->boost = ARB_SLO_BOOST_NORMAL == 1
        self.deficit = 0
        self.refilled_round = -1
        self.pending = 0
        self.inflight = 0
        self.dispatches = 0
        self.completed_bytes = 0
        self.completed_cost = 0
        self.requeues = 0
        self.requeued_bytes = 0   # shared struct field name; COST units here
        self.dispatch_log = []
        self.serve_selects = 0
        self.refill_arms = 0
        self.skip_pending_zero = 0
        self.last_serve_dispatches = 0
        self.serve_no_progress = 0
        self.dispatch_fails = 0
        self.last_active_us = 0
        # S11: largest charge since the last round advance (feeds the
        # uniform ew_mult lift, COST units).
        self.own_max_charge = 0
        # S11.1 cgroup-level demand signal (C side: cgroup_outstanding
        # map; single-hctx model collapses it onto the tenant).
        self.outstanding = 0
        self.out_activity_us = -ROUND_IDLE_RELEASE_US


class IocostArbiter:
    """Per-hctx fixed-cost DRR core (full F2/S10/S11 core, cloned from
    tests/arb_wdrr_model.py WdrrArbiter — the canonical spec of the
    shared arb_drr_pick): slots[0..15] hold cgids (0 = empty), cursor
    points at the last selected slot, refill == turn grant at most once
    per round (S11: for any key with outstanding work, including
    device-buried inflight-deep keys), round held while any key with
    unconsumed credit was active recently or still has requests inflight
    (S10/S11), F2 rotation on serve-no-progress, and the S11 uniform
    ew_mult lift recomputed at every round advance from the keys'
    own_max_charge.

    cfg=None models the BPF's missing-cfg-map fallback (compiled-in
    quantum/min_budget/table)."""

    def __init__(self, cfg=None):
        self.slots = [0] * MAX_TENANTS
        self.tenants = {}
        self.cursor = 0
        self.current_round = 0
        self.ew_mult = 1
        self.clock_us = 0
        self.round_holds = 0
        self.cfg = cfg
        self.qos = QosState()      # mirror of the BPF's iocost_qos slot

    def register(self, tenant, slot):
        self.slots[slot] = tenant.cgid
        self.tenants[tenant.cgid] = tenant

    # -- vrate application (mirror arb_iocost.bpf.c) -----------------------

    def vrate_fp(self):
        """iocost_vrate_fp(): never-fed zero state reads as 1.0."""
        return self.qos.vrate_fp if self.qos.vrate_fp else VRATE_FP_ONE

    def margin(self, ts):
        """iocost_get_boost(): reads the per-tenant margin CACHE
        (arb_tenant_state.boost, the framework's weight-injection
        channel, iolat_get_boost's exact pattern).  The feed
        (qos_feed) writes ts.boost = clamp(ceil(256^2/vrate_fp^2), 1,
        MARGIN_MAX) on every LC completion; BE tenants are never written
        (boost stays 1: the BE floor); a never-fed arbiter leaves every
        boost at 1 (byte-fair)."""
        return ts.boost

    def _update_eff_quantum(self, cfg, vrate_fp):
        """Mirror of iocost_update_eff_quantum (the BPF roller's cache
        write after every vrate step; single writer)."""
        if vrate_fp == VRATE_FP_ONE:
            cfg.runtime_eff_quantum = 0
            return
        quantum = cfg.quantum or DEFAULT_QUANTUM
        cfg.runtime_eff_quantum = (quantum * vrate_fp +
                                   VRATE_FP_ONE - 1) // VRATE_FP_ONE

    def eff_quantum(self):
        """pick_cgroup's uniform vrate application: reads the ROLLER
        cache (cfg->runtime_eff_quantum; 0 == vrate 1.0 == byte-fair,
        exact) — the pick pays one branch, no qos lookup (verifier fit)."""
        if self.cfg is not None and self.cfg.runtime_eff_quantum:
            return self.cfg.runtime_eff_quantum
        return (self.cfg.quantum if self.cfg else DEFAULT_QUANTUM) \
            or DEFAULT_QUANTUM

    def eff_min_budget(self):
        """The absolute floor stays UNSCALED by vrate (a per-visit
        liveness floor, not a share knob) — mirrors the BPF pick."""
        return self.cfg.min_budget if self.cfg is not None \
            else DEFAULT_MIN_BUDGET

    def _budget(self, ts):
        """Round budget = eff_quantum * eff_weight * ew_mult where
        eff_weight = weight * margin (iocost_get_boost), with the absolute
        eff_min_budget floor, mirroring arb_drr_arm_grant's arithmetic and
        the BPF's zero-fallbacks (quantum==0 -> default)."""
        quantum = self.eff_quantum()
        min_budget = self.eff_min_budget()
        ew = ts.weight * self.margin(ts)   # * iocost_get_boost(ts.boost)
        if ew == 0:
            ew = 1
        if quantum * ew < min_budget:
            ew = (min_budget + quantum - 1) // quantum
        return quantum * ew * self.ew_mult

    def _arm(self, ts, accrual=False):
        """Once-per-round turn grant (refilled_round latch).  accrual=True
        (S11.1 dry path) skips the last_active refresh: accrual is not
        service."""
        budget = self._budget(ts)
        ts.deficit = min(ts.deficit + budget, budget * CAP_MULT)
        ts.refilled_round = self.current_round
        ts.refill_arms += 1
        if not accrual:
            ts.last_active_us = self.clock_us

    def pick(self):
        self.clock_us += MODEL_CLOCK_US_PER_PICK
        base = self.cursor
        for step in range(MAX_TENANTS):
            idx = (base + step) % MAX_TENANTS
            cgid = self.slots[idx]
            if cgid == 0:
                continue
            ts = self.tenants[cgid]
            if ts.pending == 0:
                # S11.1 arm-on-dry: a dry key whose CGROUP still has a
                # live outstanding set accrues this round's grant (never
                # selected — the pop would be empty; accrual does not
                # refresh recency).
                if (ts.outstanding > 0 and ts.deficit <= 0
                        and self.clock_us - ts.out_activity_us
                            < ROUND_IDLE_RELEASE_US
                        and ts.refilled_round != self.current_round):
                    self._arm(ts, accrual=True)
                ts.skip_pending_zero += 1
                continue
            if ts.deficit > 0:
                # Turn in progress (F2: detect empty-pop wedges ourselves)
                if ts.dispatches == ts.last_serve_dispatches:
                    ts.serve_no_progress += 1
                else:
                    ts.serve_no_progress = 0
                ts.last_serve_dispatches = ts.dispatches
                if ts.serve_no_progress > SERVE_NO_PROGRESS_MAX:
                    ts.serve_no_progress = 0
                    ts.refilled_round = self.current_round
                    ts.deficit = 0
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
            self.cursor = (idx + 1) % MAX_TENANTS
        # scan complete: hold while any credit-holding key was active
        # recently OR still has requests inflight (S10/S11); accumulate
        # the next round's uniform lift k from the keys' own_max_charge.
        k = 1
        for step in range(MAX_TENANTS):
            idx = (base + step) % MAX_TENANTS
            cgid = self.slots[idx]
            if cgid == 0:
                continue
            ts = self.tenants[cgid]
            if ts.deficit > 0:
                if self.clock_us - ts.last_active_us < ROUND_IDLE_RELEASE_US:
                    self.round_holds += 1
                    return 0
                # S11.1: banked credit of a live-but-stalled cgroup
                # survives the release; only a stopped tenant forfeits.
                if not (ts.outstanding > 0
                        and self.clock_us - ts.out_activity_us
                            < ROUND_IDLE_RELEASE_US):
                    ts.deficit = 0
            # vrate application: the lift need is derived from the SAME
            # eff_quantum the BPF passes into arb_drr_pick (the uniform
            # component; the margin is deliberately excluded from the lift
            # base — boost only grows real budgets, the shared core's
            # conservative choice).
            quantum = self.eff_quantum()
            min_budget = self.eff_min_budget()
            base_ew = ts.weight if ts.weight else 1
            if quantum * base_ew < min_budget:
                base_ew = (min_budget + quantum - 1) // quantum
            need = -(-ts.own_max_charge // (quantum * base_ew))  # ceil
            if need > k:
                k = need
        self.ew_mult = min(k, MAX_EW_MULT)
        for step in range(MAX_TENANTS):
            cgid = self.slots[(base + step) % MAX_TENANTS]
            if cgid:
                self.tenants[cgid].own_max_charge = 0
        self.current_round += 1
        return 0

    def advance_cursor(self):
        self.cursor = (self.cursor + 1) % MAX_TENANTS

    # framework hook handlers (mirror arb_note_*; charge unit = COST)

    def note_inserted(self, cgid):
        self.tenants[cgid].pending += 1

    def note_dispatched(self, cgid, cost):
        ts = self.tenants[cgid]
        ts.deficit -= cost
        if ts.pending > 0:
            ts.pending -= 1
        ts.inflight += 1
        ts.completed_cost += cost
        ts.dispatches += 1
        ts.dispatch_fails = 0
        ts.serve_no_progress = 0
        ts.dispatch_log.append(cost)
        # S11: largest charge of the current round window (COST units)
        if cost > ts.own_max_charge:
            ts.own_max_charge = cost

    def note_requeue(self, cgid, refund):
        ts = self.tenants[cgid]
        if ts.outstanding > 0:
            ts.outstanding -= 1
        ts.out_activity_us = self.clock_us
        if ts.inflight > 0:
            ts.inflight -= 1
        if refund:
            ts.deficit += refund
            ts.requeued_bytes += refund
        ts.requeues += 1

    def note_completed(self, cgid):
        ts = self.tenants[cgid]
        if ts.outstanding > 0:
            ts.outstanding -= 1
        ts.out_activity_us = self.clock_us
        if ts.inflight > 0:
            ts.inflight -= 1

    def note_dispatch_failed(self, cgid):
        ts = self.tenants[cgid]
        if ts.outstanding > 0:
            ts.outstanding -= 1
        ts.out_activity_us = self.clock_us
        ts.dispatch_fails += 1
        if ts.dispatch_fails >= DISPATCH_FAIL_GHOST_RESET:
            ts.pending = 0
            ts.deficit = 0
            ts.dispatch_fails = 0
            ts.serve_no_progress = 0
            return
        if ts.pending > 0:
            ts.pending -= 1

    # qos vrate feedback (mirror arb_iocost.bpf.c completed_request ->
    # iocost_qos_feed): LC completions feed the ONE global window; the
    # model is single-threaded so the BPF's roller CAS collapses to a
    # plain roll.  now_ns/lat_ns are nanoseconds like the BPF ctx.

    def qos_feed(self, cgid, now_ns, lat_ns):
        rlat = self.cfg.qos_rlat_ns if self.cfg is not None \
            else DEFAULT_QOS_RLAT_NS
        if rlat == 0:
            return                       # qos disabled: vrate pinned 1.0
        ts = self.tenants.get(cgid)
        if ts is None or ts.prio > PRIO_LC_MAX:
            return                       # BE completions carry no target
        qs = self.qos
        if qs.window_start_ns == 0:
            qs.window_start_ns = now_ns
        if now_ns - qs.window_start_ns >= QOS_WINDOW_NS:
            qs.window_start_ns = now_ns          # the roller won
            total, over, wsum = qs.window_total, qs.window_over, \
                qs.window_sum_ns
            rpct = self.cfg.qos_rpct if (self.cfg is not None
                                          and self.cfg.qos_rpct) \
                else DEFAULT_QOS_RPCT
            vmin = self.cfg.vrate_min_fp if self.cfg is not None \
                else VRATE_MIN_FP_DEFAULT
            vmax = self.cfg.vrate_max_fp if self.cfg is not None \
                else VRATE_MAX_FP_DEFAULT
            if not vmin:
                vmin = VRATE_MIN_FP_DEFAULT
            if not vmax:
                vmax = VRATE_MAX_FP_DEFAULT
            vmax = max(vmax, vmin)

            violation = 0
            comfortable = 0
            if total >= QOS_MIN_SAMPLES:
                # p(100-rpct) > rlat  <=>  over-fraction > (100-rpct)%
                if over * 100 > total * (100 - rpct):
                    violation = 1
                elif wsum * 2 < total * rlat:
                    comfortable = 1     # window mean < rlat/2

            qs.last_violation = violation
            qs.last_comfortable = comfortable
            if violation:
                qs.late_windows += 1
                qs.good_windows = 0
                if qs.late_windows >= QOS_HYST:
                    fp = qs.vrate_fp or VRATE_FP_ONE
                    fp = fp * VRATE_DOWN_NUM // VRATE_DOWN_DEN
                    qs.vrate_fp = max(fp, vmin)
                    if self.cfg is not None:
                        self._update_eff_quantum(self.cfg, qs.vrate_fp)
                    qs.late_windows = 0
                    qs.down_steps += 1
            else:
                qs.late_windows = 0
                if comfortable:
                    qs.good_windows += 1
                    if qs.good_windows >= QOS_HYST:
                        fp = qs.vrate_fp or VRATE_FP_ONE
                        fp = fp * VRATE_UP_NUM // VRATE_UP_DEN
                        qs.vrate_fp = min(fp, vmax)
                        if self.cfg is not None:
                            self._update_eff_quantum(self.cfg, qs.vrate_fp)
                        qs.good_windows = 0
                        qs.up_steps += 1
                else:
                    qs.good_windows = 0
            qs.window_total = 0
            qs.window_over = 0
            qs.window_sum_ns = 0
        qs.window_total += 1
        qs.window_sum_ns += lat_ns
        if lat_ns > rlat:
            qs.window_over += 1
        qs.last_sample_ns = now_ns
        # Refresh the LC margin cache (mirror of the BPF feed tail):
        # margin = clamp(ceil(256^2 / vrate_fp^2), 1, MARGIN_MAX),
        # recomputed per LC completion so a stale boost survives at most
        # one completion interval.  ts here is the LC tenant (role gate
        # above); the model collapses the BPF's per-(cgid,hctx) key onto
        # the tenant (single-hctx model).
        fp = qs.vrate_fp or VRATE_FP_ONE
        den = fp * fp
        margin = min(-(-(VRATE_FP_ONE * VRATE_FP_ONE) // den), MARGIN_MAX)
        ts.boost = max(margin, 1)

    # BPF hook wiring (arb_iocost.bpf.c): both sides recompute the
    # estimate from (opf, nr_sectors) so charge/refund share the unit.

    def dispatched_request(self, cgid, opf, nr_sectors):
        self.note_dispatched(cgid, arb_iocost_estimate(opf, nr_sectors, self.cfg))

    def requeue_refund(self, cgid, opf, nr_sectors):
        self.note_requeue(cgid, arb_iocost_estimate(opf, nr_sectors, self.cfg))

    def dispatch_failed(self, cgid):
        self.note_dispatch_failed(cgid)
        self.advance_cursor()


# ---------------------------------------------------------------------------
# Saturated drive (mirror arb_cost_model.drive_backlogged, per-request)
# ---------------------------------------------------------------------------

def drive_backlogged(arb, requests, rounds=None, total=None):
    """Keep every listed tenant backlogged at exactly one pending request
    (re-insert after each dispatch).  requests maps cgid -> (opf,
    nr_sectors); every request of a tenant charges
    arb_iocost_estimate(opf, nr_sectors, arb.cfg).  Stops after `rounds`
    completed DRR rounds or `total` dispatches (one must be given)."""
    if rounds is None and total is None:
        raise ValueError("drive_backlogged needs a rounds or total stop condition")
    costs = {cg: arb_iocost_estimate(opf, sec, arb.cfg)
             for cg, (opf, sec) in requests.items()}
    for cg in requests:
        if arb.tenants[cg].pending == 0:
            arb.note_inserted(cg)
    served = {cg: 0 for cg in requests}
    done = 0
    while (rounds is None or arb.current_round < rounds) and \
            (total is None or done < total):
        cg = arb.pick()
        if cg == 0:
            continue            # round boundary; retry like blk-mq's next kick
        arb.note_dispatched(cg, costs[cg])
        arb.note_completed(cg)
        served[cg] += 1
        done += 1
        if arb.tenants[cg].pending == 0:
            arb.note_inserted(cg)
    return served


def check(name, cond, detail=""):
    if cond:
        print(f"PASS: {name}")
        return True
    print(f"FAIL: {name} {detail}")
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_default_cfg_and_estimate():
    """Default cfg == the compiled-in byte-linear table + arb_iocost.bpf.c's
    fallback constants (S11); estimate fixtures (bucket boundaries, op flags)."""
    cases = [
        ((0, 1), 64),        # 512B read  -> bucket 0
        ((0, 8), 64),        # 4K read    -> bucket 0 boundary
        ((0, 9), 256),       # 4608B read -> bucket 1
        ((0, 128), 1024),    # 64K read
        ((0, 256), 2048),    # 128K read
        ((0, 512), 4096),    # 256K read  -> bucket 4
        ((1, 8), 64),        # 4K write  (byte-linear: read == write)
        ((1, 32), 256),      # 16K write
        ((1, 512), 4096),    # 256K write -> max charge
        ((0x101, 8), 64),    # opf flags: op = opf & 0xff == 1 -> WRITE
    ]
    bad = [(rq, want, arb_iocost_estimate(*rq)) for rq, want in cases
           if arb_iocost_estimate(*rq) != want]
    ok = check("default_estimate_table", not bad, f"mismatches={bad}")
    cfg = IocostCfg()
    ok &= check("default_cfg_constants",
                cfg.quantum == DEFAULT_QUANTUM == 8 and
                cfg.min_budget == DEFAULT_MIN_BUDGET == 64 and
                cfg.cost_read == list(DEFAULT_TABLE[0]) and
                cfg.cost_write == list(DEFAULT_TABLE[1]) and
                cfg.cost_write == cfg.cost_read,
                f"quantum={cfg.quantum} min_budget={cfg.min_budget}")
    ok &= check("cfg_none_falls_back_compiled_in",
                all(arb_iocost_estimate(*rq, None) == want for rq, want in cases),
                "cfg=None must use DEFAULT_TABLE")
    return ok


def test_case1_cost_not_request_fairness():
    """THE cost-awareness case (homo shape): equal weights, A = 4K READs
    (cost 64), B = 64K WRITEs (cost 1024).  budget(w) = 8*w = 2048 cost
    per round (32 vs 2 requests), so charged COST runs 1:1 while requests
    run ~16:1 (byte/request fairness would be a spec failure of the cost
    model, never of this — with the byte-linear default the BYTES also
    split 1:1 here, which is what makes weight ratios reproduce
    io.weight byte shares)."""
    arb = IocostArbiter(IocostCfg())
    a, b = Tenant(100, 256), Tenant(200, 256)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: R4K, 200: W64K}, rounds=3)
    ca, cb = a.completed_cost, b.completed_cost
    cost_ratio = ca / cb
    req_ratio = served[100] / served[200]
    ok = check("case1_cost_ratio_1to1", abs(cost_ratio - 1.0) <= 0.05,
               f"charged_cost ratio={cost_ratio:.3f} A={ca} B={cb}")
    ok &= check("case1_request_ratio_16", 14 <= req_ratio <= 18,
                f"request ratio={req_ratio:.3f}")
    return ok


def test_case2_weight_ratio():
    """Weights 256 vs 768 (both above the floor), 4K reads: budget(w) =
    w cost units -> per-round cost 256 vs 768 -> ratio 3.0 exactly."""
    arb = IocostArbiter(IocostCfg())
    a, b = Tenant(100, 256), Tenant(200, 768)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: R4K, 200: R4K}, rounds=2)
    cost_ratio = b.completed_cost / a.completed_cost
    req_ratio = served[200] / served[100]
    ok = check("case2_weight_cost_ratio_3", 2.9 <= cost_ratio <= 3.1,
               f"B/A charged_cost={cost_ratio:.3f}")
    ok &= check("case2_weight_request_ratio_3", 2.9 <= req_ratio <= 3.1,
                f"B/A request ratio={req_ratio:.3f}")
    return ok


def test_size_bucket_cost_ratio():
    """Cost fairness across size buckets: equal weights, A = 4K reads
    (cost 64), B = 64K reads (cost 1024).  budget(w) = 8*256 = 2048 cost:
    A serves 32 x 64, B serves 2 x 1024 per round — cost-fair 1:1 with
    requests 16:1 (no lift needed: every budget already affords one max
    charge; the S11 uniform lift only engages when it does not)."""
    arb = IocostArbiter(IocostCfg())
    a, b = Tenant(100, 256), Tenant(200, 256)
    arb.register(a, 0)
    arb.register(b, 1)
    drive_backlogged(arb, {100: R4K, 200: R64K}, rounds=2)
    ca0, cb0 = a.completed_cost, b.completed_cost
    served = drive_backlogged(arb, {100: R4K, 200: R64K}, rounds=5)
    dca = a.completed_cost - ca0
    dcb = b.completed_cost - cb0
    cost_ratio = dca / dcb
    req_ratio = served[100] / served[200]
    ok = check("size_bucket_cost_ratio_1to1", abs(cost_ratio - 1.0) <= 0.1,
               f"cost ratio={cost_ratio:.3f} A={dca} B={dcb}")
    ok &= check("size_bucket_request_ratio_16", 14 <= req_ratio <= 18,
                f"request ratio={req_ratio:.1f} (4K vs 64K, cost 64 vs 1024)")
    return ok


def test_custom_table_flips_fairness():
    """The loader-pushed table is LIVE policy: make a 4K write cost 8x a
    4K read (512 vs 64).  Equal weights then serve A (reads) 8 requests
    per B's 1 — cost-fair at 1:1, request-unfair at 8:1.  budget(w) =
    8*256 = 2048 cost serves A 32 and B 4 per round (no lift needed:
    2048 >= the 512 max charge)."""
    table = ((64, 256, 1024, 2048, 4096),
             (512, 2048, 8192, 16384, 32768))
    cfg = IocostCfg(cost_table=table)
    arb = IocostArbiter(cfg)
    a, b = Tenant(100, 256), Tenant(200, 256)
    arb.register(a, 0)
    arb.register(b, 1)
    drive_backlogged(arb, {100: R4K, 200: W4K}, rounds=2)
    ca0, cb0 = a.completed_cost, b.completed_cost
    served = drive_backlogged(arb, {100: R4K, 200: W4K}, rounds=5)
    dca = a.completed_cost - ca0
    dcb = b.completed_cost - cb0
    req_ratio = served[100] / served[200]
    ok = check("custom_table_cost_ratio_1to1",
               abs(dca / dcb - 1.0) <= 0.1,
               f"cost ratio={dca / dcb:.3f} A={dca} B={dcb}")
    ok &= check("custom_table_request_ratio_8", 7 <= req_ratio <= 9,
                f"request ratio={req_ratio:.2f}")
    return ok


def test_custom_quantum_scaling():
    """The quantum knob exists because the table and the budget share the
    cost domain: recalibrate the table x64 (4K read = 4096) and the
    quantum must scale with it (4096), or the adaptive floor inflates
    every budget to one max charge and weights stop separating.  With
    quantum 4096: budget(w) = 4096*w cost units -> weights 100 vs 300
    split service 1:3 exactly (100 vs 300 dispatches of a 4K read)."""
    table = ((4096, 16384, 65536, 131072, 262144),
             (16384, 65536, 262144, 524288, 1048576))
    cfg = IocostCfg(quantum=4096, min_budget=4096, cost_table=table)
    arb = IocostArbiter(cfg)
    a, b = Tenant(100, 100), Tenant(200, 300)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: R4K, 200: R4K}, rounds=2)
    ratio = served[200] / served[100]
    return check("custom_quantum_weight_ratio_3", 2.95 <= ratio <= 3.05,
                 f"request ratio={ratio:.3f}")


def test_zero_entry_fallback():
    """A zero cfg entry falls back to the compiled-in default for that
    (op, bucket) only (mirror of the BPF's `cost == 0 -> byte-linear
    switch` fallback)."""
    table = ((64, 0, 1024, 2048, 4096),          # 16K read -> fallback 256
             (512, 2048, 8192, 16384, 32768))     # 16K write stays custom
    cfg = IocostCfg(cost_table=table)
    ok = check("zero_entry_read_fallback",
               arb_iocost_estimate(0, 32, cfg) == DEFAULT_TABLE[0][1],
               f"16K read cost={arb_iocost_estimate(0, 32, cfg)} "
               f"(fallback {DEFAULT_TABLE[0][1]})")
    ok &= check("nonzero_entry_stays_custom",
                arb_iocost_estimate(1, 32, cfg) == 2048 and
                arb_iocost_estimate(0, 8, cfg) == 64,
                "custom entries must not be overridden")
    return ok


def test_requeue_refund_parity():
    """Requeue refunds the exact charged cost (recomputed from the same
    cfg) and never touches pending (guide §6 pitfall 4)."""
    arb = IocostArbiter(IocostCfg())
    a = Tenant(100, 256)
    arb.register(a, 0)
    arb.note_inserted(100)
    cg = arb.pick()
    while cg == 0:
        cg = arb.pick()
    armed = a.deficit
    charge = arb_iocost_estimate(*W4K, arb.cfg)
    arb.dispatched_request(cg, *W4K)     # charge = estimate(W4K)
    pending_after_charge = a.pending
    arb.requeue_refund(cg, *W4K)        # refund the same cost, NO pending++
    pending_after_requeue = a.pending
    arb.note_inserted(cg)               # kernel re-insert restores pending
    arb.dispatched_request(arb.pick(), *W4K)
    arb.note_completed(cg)
    net = a.completed_cost - a.requeued_bytes
    return check("requeue_refund_parity",
                 pending_after_charge == 0 and pending_after_requeue == 0
                 and a.inflight == 0 and net == 2 * charge - charge
                 and a.deficit == armed - 2 * charge + charge
                 and a.requeues == 1 and a.requeued_bytes == charge,
                 f"pending={a.pending} inflight={a.inflight} net={net} "
                 f"deficit={a.deficit} armed={armed} charge={charge}")


def test_min_budget_floor():
    """Weights below min_budget/quantum = 64/8 = 8 floor-compress to the
    same budget (the absolute one-4K-read floor, spec)."""
    arb = IocostArbiter(IocostCfg())
    a, b = Tenant(100, 4), Tenant(200, 6)
    arb.register(a, 0)
    arb.register(b, 1)
    drive_backlogged(arb, {100: R4K, 200: R4K}, rounds=1)
    ratio = a.completed_cost / b.completed_cost
    return check("min_budget_floor_equal_budgets",
                 arb._budget(a) == arb._budget(b) == DEFAULT_MIN_BUDGET
                 and abs(ratio - 1.0) <= 0.05,
                 f"A budget={arb._budget(a)} B budget={arb._budget(b)} "
                 f"ratio={ratio:.3f}")


def test_overshoot_bound():
    """deficit may go transiently negative, bounded by ONE max charge
    (4096 = byte-linear bucket-4 cost; the uniform lift keeps budget >=
    the tenant's own max charge, so the bound is the charge itself)."""
    arb = IocostArbiter(IocostCfg())
    a = Tenant(100, 256)
    arb.register(a, 0)
    worst = 0
    pattern = [W256K, R4K, R128K, W4K, R64K]
    for i in range(800):
        arb.note_inserted(100)
        cg = arb.pick()
        while cg == 0:
            cg = arb.pick()
        arb.dispatched_request(cg, *pattern[i % len(pattern)])
        arb.note_completed(cg)
        worst = min(worst, a.deficit)
    return check("overshoot_bound", worst >= -4096,
                 f"worst deficit={worst} bound=-4096")


def test_dispatch_failed_lifecycle():
    """Invalid-rq path retires one pending (only); consecutive failures
    hard-reset a crystallized ghost (F2).  An emptied tenant must never
    be picked (strict NULL)."""
    arb = IocostArbiter(IocostCfg())
    a = Tenant(100, 256)
    arb.register(a, 0)
    arb.note_inserted(100)
    cg = arb.pick()
    assert cg == 100
    armed = a.deficit
    arb.dispatch_failed(100)
    ok = check("dispatch_failed_retires_pending",
               a.pending == 0 and a.deficit == armed,
               f"pending={a.pending} deficit={a.deficit} armed={armed}")
    ok &= check("dispatch_failed_empty_not_picked", arb.pick() == 0,
                "empty tenant must not be picked (strict NULL)")
    for _ in range(5):
        arb.note_inserted(100)
    arb.pick()
    for _ in range(4):
        arb.dispatch_failed(100)
    ok &= check("dispatch_failed_ghost_reset",
                a.pending == 0 and a.deficit == 0,
                f"pending={a.pending} deficit={a.deficit}")
    return ok


def test_work_conservation_and_round_invariants():
    """Idle tenants get no credit; the active tenant's deficit is capped;
    each tenant refills at most once per round; the S11 uniform lift
    engages exactly when a key's own charge exceeds its weight budget
    (W256K costs 4096 > budget 2048 -> k=2) and multiplies EVERY budget
    (ratio preserved)."""
    arb = IocostArbiter(IocostCfg())
    a, b = Tenant(100, 256), Tenant(200, 256)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: R4K}, rounds=3)
    ok = check("work_conservation",
               b.dispatches == 0 and b.completed_cost == 0 and b.deficit == 0
               and b.refill_arms == 0,
               f"B dispatches={b.dispatches} deficit={b.deficit} "
               f"refills={b.refill_arms}")
    ok &= check("idle_credit_cap",
                a.deficit <= arb._budget(a) * CAP_MULT,
                f"A deficit={a.deficit} cap={arb._budget(a) * CAP_MULT}")

    arb = IocostArbiter(IocostCfg())
    a, b = Tenant(100, 256), Tenant(200, 256)
    arb.register(a, 0)
    arb.register(b, 1)
    drive_backlogged(arb, {100: R4K, 200: W64K}, rounds=3)
    ok &= check("refill_once_per_round",
                a.refill_arms == 3 and b.refill_arms == 3,
                f"A refills={a.refill_arms} B refills={b.refill_arms}")
    ok &= check("uniform_lift_idle_when_budgets_afford",
                arb.ew_mult == 1,
                f"ew_mult={arb.ew_mult} (2048-cost budgets cover the "
                f"1024-cost max charge; no lift)")
    drive_backlogged(arb, {100: R4K, 200: W256K}, rounds=4)
    ok &= check("uniform_lift_engages_and_scales_all",
                arb.ew_mult == 2,
                f"ew_mult={arb.ew_mult} (4096-cost charge > 2048 budget "
                f"-> k=2 lifts every budget)")
    return ok


def test_s11_homo_coloc_byte_share():
    """THE homo-v2 acceptance shape for the cost arm (task contract):
    LC 4K randread QD32 w100 vs BE 64K seqwrite QD16 w200 under the
    DEFAULT byte-linear table must split served BYTES 1:2
    (be_share 0.667 +- 5%) in the saturated coloc regime where both
    tenants are periodically device-buried.  Pre-S11 hardware measured
    be_share 0.78 (HOMO_ctrl_x_cost_arb, 20261106) while BE re-armed at
    completion-kick cadence and LC lost every round boundary; with the
    4x-write-penalty default table cost-fairness itself was byte-unfair
    (2:1 LC-ward).  Costs: LC 4K read = 64, BE 64K write = 1024
    (byte-linear); budgets 800 vs 1600 cost (12.5 vs 1.56 requests)."""
    import arb_wdrr_model as core
    arb = IocostArbiter(IocostCfg())
    lc, be = Tenant(100, 100), Tenant(200, 200)
    arb.register(lc, 0)
    arb.register(be, 1)
    served = core.drive_coloc(arb, {100: 64, 200: 1024},   # charge units
                              caps={100: 32, 200: 16},
                              lat_us={100: 250, 200: 600},
                              total_dispatches=60000)
    blc = served[100] * 4096
    bbe = served[200] * 65536
    share = bbe / (blc + bbe)
    ok = check("s11_homo_coloc_byte_share", 0.617 <= share <= 0.717,
               f"be_share={share:.3f} expected 0.667 "
               f"(window 0.667+-0.05)")
    ok &= check("s11_homo_coloc_arms_balanced",
                0.75 <= be.refill_arms / max(1, lc.refill_arms) <= 1.33,
                f"arms lc={lc.refill_arms} be={be.refill_arms}")
    return ok


# ---------------------------------------------------------------------------
# QoS vrate feedback tests (mirror arb_iocost.bpf.c's iocost_qos_feed +
# iocost_get_boost + pick_cgroup's eff_quantum application)
# ---------------------------------------------------------------------------

RLAT = DEFAULT_QOS_RLAT_NS          # 2 ms default, campaign-aligned
LC_PRIO = 2                         # LC band (arb_iolatency口径)
BE_PRIO = 5
VIOL = 3 * RLAT                     # the measured byte-fair p99 shape
                                    # (homo-v2 iter#4: lc_read_p99 11.6ms
                                    # vs rlat 2ms -> >>rlat samples)
CLEAN = RLAT // 4                   # deep-comfort sample (< rlat/2 mean)
MID = int(RLAT * 0.75)              # NEUTRAL window: none over rlat,
                                    # mean >= rlat/2 (neither gate fires)


def feed_seq(arb, cgid, win_lats, samples=25, start_ns=0):
    """Feed one burst of `samples` LC completions per entry of win_lats
    (constant or per-sample list), bursts spaced QOS_WINDOW_NS apart.
    Burst k+1's first sample ROLLS (evaluates) burst k's window, so
    win_lats[0..n-1] are the n EVALUATED windows and win_lats[n] only
    seeds the pending one.  Sample timestamps start at +1000ns: the
    window_start_ns==0 sentinel means t=0 cannot open a window (the
    BPF never sees ktime 0; keep the model's synthetic clock off it).
    Returns the next start_ns."""
    for w, lat in enumerate(win_lats):
        for i in range(samples):
            l = lat if not isinstance(lat, (list, tuple)) \
                else lat[i % len(lat)]
            arb.qos_feed(cgid, start_ns + w * QOS_WINDOW_NS + (i + 1) * 1000,
                         l)
    return start_ns + len(win_lats) * QOS_WINDOW_NS


def make_lc_be_arb(**cfg_kw):
    arb = IocostArbiter(IocostCfg(**cfg_kw))
    lc = Tenant(100, 100, prio=LC_PRIO)
    be = Tenant(200, 200, prio=BE_PRIO)
    arb.register(lc, 0)
    arb.register(be, 1)
    return arb, lc, be


def test_qos_vrate_one_byte_fair_regression():
    """vrate==1 byte-fair invariance: the never-fed zero state reads as
    fp 256, so eff_quantum == quantum, eff_min_budget == min_budget and
    the margin is 1 for BOTH role bands — the qos machinery must not
    distort the byte-fair budgets (backward compatibility)."""
    arb, lc, be = make_lc_be_arb()
    ok = check("qos_vrate_one_never_state",
               arb.vrate_fp() == VRATE_FP_ONE and arb.qos.vrate_fp == 0,
               f"vrate_fp={arb.vrate_fp()}")
    ok &= check("qos_vrate_one_eff_exact",
                arb.eff_quantum() == DEFAULT_QUANTUM and
                arb.eff_min_budget() == DEFAULT_MIN_BUDGET,
                f"eff_q={arb.eff_quantum()} eff_min={arb.eff_min_budget()}")
    ok &= check("qos_vrate_one_margin_one_both_roles",
                arb.margin(lc) == 1 and arb.margin(be) == 1,
                f"margin_lc={arb.margin(lc)} margin_be={arb.margin(be)}")
    ok &= check("qos_vrate_one_budgets_byte_fair",
                arb._budget(lc) == DEFAULT_QUANTUM * 100 and
                arb._budget(be) == DEFAULT_QUANTUM * 200,
                f"budget_lc={arb._budget(lc)} budget_be={arb._budget(be)}")
    # qos disabled (rlat 0): violating feeds must not move vrate at all.
    arb2, lc2, _ = make_lc_be_arb(qos_rlat_ns=0)
    feed_seq(arb2, 100, [VIOL] * 6)
    ok &= check("qos_disabled_pins_vrate",
                arb2.vrate_fp() == VRATE_FP_ONE and arb2.qos.down_steps == 0,
                f"vrate_fp={arb2.qos.vrate_fp} down={arb2.qos.down_steps}")
    return ok


def test_qos_down_ladder_uniform_shrink_and_margin():
    """LC 超标 -> vrate 下调: two consecutive violating windows cut vrate
    x0.80; the cut is applied as eff_quantum = ceil(quantum*vrate) — the
    UNIFORM component shrinks EVERY cgroup's nominal round budget
    together (BE/LC 同缩 at the application layer); the LC inuse margin
    (io.cost's debt/margin layer) rebates the target holder so the NET
    cut lands on BE (LC 优先): BE net budget shrinks below byte-fair,
    LC net budget stays >= byte-fair.  Ladder down to the vrate_min
    floor."""
    arb, lc, be = make_lc_be_arb()

    # Hysteresis: two violating windows SEPARATED by a neutral window
    # never step (the neutral window resets the late streak).
    feed_seq(arb, 100, [MID, VIOL, MID, VIOL, MID])
    ok = check("qos_down_hysteresis_streak_reset",
               arb.vrate_fp() == VRATE_FP_ONE and arb.qos.down_steps == 0
               and arb.qos.late_windows == 1,
               f"vrate={arb.qos.vrate_fp} late={arb.qos.late_windows}")
    # 2 consecutive violating windows: the first x0.8 cut (fp 205).
    feed_seq(arb, 100, [VIOL, VIOL, VIOL], start_ns=5 * QOS_WINDOW_NS)
    ok &= check("qos_down_first_step_fp205",
                arb.vrate_fp() == 205 and arb.qos.down_steps == 1,
                f"vrate={arb.qos.vrate_fp} down={arb.qos.down_steps}")
    # The uniform component (eff_quantum) shrank for BOTH roles...
    ok &= check("qos_down_uniform_component_shrinks_all",
                arb.eff_quantum() == 7 < DEFAULT_QUANTUM,
                f"eff_q={arb.eff_quantum()} (was {DEFAULT_QUANTUM})")
    # ...the margin rebates LC; the NET cut lands on BE.
    ok &= check("qos_down_margin_rebate_lc",
                arb.margin(lc) == 2 and arb.margin(be) == 1,
                f"margin_lc={arb.margin(lc)} margin_be={arb.margin(be)}")
    ok &= check("qos_down_net_cut_lands_on_be",
                arb._budget(be) == 7 * 200 and          # 1400 < 1600
                arb._budget(lc) == 7 * 100 * 2,           # 1400 >= 800
                f"budget_be={arb._budget(be)} budget_lc={arb._budget(lc)}")

    # Sustained violation: the full ladder to the vrate_min floor.
    feed_seq(arb, 100, [VIOL] * 16, start_ns=10 * QOS_WINDOW_NS)
    ok &= check("qos_down_floor_clamp",
                arb.vrate_fp() == VRATE_MIN_FP_DEFAULT and
                arb.margin(lc) == MARGIN_MAX and arb.margin(be) == 1,
                f"vrate={arb.qos.vrate_fp} margin_lc={arb.margin(lc)} "
                f"down_steps={arb.qos.down_steps}")
    ok &= check("qos_down_floor_eff_and_budgets",
                arb.eff_quantum() == 2 and arb._budget(be) == 400 and
                arb._budget(lc) == 2 * 100 * MARGIN_MAX,
                f"eff_q={arb.eff_quantum()} budget_be={arb._budget(be)} "
                f"budget_lc={arb._budget(lc)}")
    return ok


def test_qos_down_share_shift_vs_byte_fair():
    """The acceptance shape at the vrate floor: with weights 100:200,
    LC 4K reads (cost 64) and BE 64K writes (cost 1024), the byte-fair
    regime serves ~1:2 BYTES; the qos-bound regime (vrate floor) moves
    the share deep LC-ward (LC 优先完成) while BE keeps its per-turn
    floor (>=1 64K write per round — the S11 uniform-lift re-floor, no
    starvation; long-run BE service == budget_BE/1024 writes/round)."""
    # byte-fair reference (never-fed vrate == 1.0)
    arb0 = IocostArbiter(IocostCfg())
    lc0, be0 = Tenant(100, 100, prio=LC_PRIO), Tenant(200, 200, prio=BE_PRIO)
    arb0.register(lc0, 0)
    arb0.register(be0, 1)
    served0 = drive_backlogged(arb0, {100: R4K, 200: W64K}, rounds=3)
    share0 = served0[200] * 65536 / (served0[100] * 4096 + served0[200] * 65536)
    ok = check("byte_fair_reference_share",
               0.60 <= share0 <= 0.80,
               f"be_share={share0:.3f} served={served0}")

    # qos-bound: drive vrate to the floor, then run the SAME drive.
    arb, lc, be = make_lc_be_arb()
    feed_seq(arb, 100, [VIOL] * 16)
    ok &= check("share_shift_premise_vrate_floor",
                arb.vrate_fp() == VRATE_MIN_FP_DEFAULT,
                f"vrate={arb.qos.vrate_fp}")
    served = drive_backlogged(arb, {100: R4K, 200: W64K}, rounds=3)
    share = served[200] * 65536 / (served[100] * 4096 + served[200] * 65536)
    ok &= check("qos_bound_share_moves_lc_ward",
                share <= 0.30 and share < share0 / 2,
                f"be_share={share:.3f} (byte-fair {share0:.3f})")
    ok &= check("qos_bound_lc_service_multiplied",
                served[100] >= 8 * served0[100],
               f"lc served={served[100]} vs byte-fair {served0[100]}")
    ok &= check("qos_bound_be_floor_no_starvation",
                served[200] >= 3,
                f"be served={served[200]} (>=1 64K write per round)")
    return ok


def test_qos_up_recovery_and_clamp():
    """恢复 -> 回升: comfortable windows (mean < rlat/2, none over rlat)
    raise vrate x1.05 per 2 windows; the margin decays back to 1 as
    vrate recovers through 1.0; deep comfort clamps at vrate_max."""
    arb, lc, be = make_lc_be_arb()
    feed_seq(arb, 100, [VIOL] * 16)                    # down to the floor
    ok = check("up_premise_floor", arb.vrate_fp() == VRATE_MIN_FP_DEFAULT,
               f"vrate={arb.qos.vrate_fp}")

    # Not-comfortable, not-violating windows (mean in [rlat/2, rlat)):
    # vrate holds (no move in either direction).
    hold_v = arb.vrate_fp()
    feed_seq(arb, 100, [int(RLAT * 0.6)] * 4, start_ns=20 * QOS_WINDOW_NS)
    ok &= check("up_midband_holds",
                arb.vrate_fp() == hold_v and arb.qos.up_steps == 0,
                f"vrate={arb.qos.vrate_fp} up={arb.qos.up_steps}")

    # Comfortable windows: x1.05 per 2 windows, vrate recovers PAST the
    # byte-fair point (io.cost raises above 100% on deep comfort) and
    # the margin decays back to 1.
    feed_seq(arb, 100, [CLEAN] * 80, start_ns=40 * QOS_WINDOW_NS)
    ok &= check("up_recovers_past_one",
                arb.vrate_fp() >= VRATE_FP_ONE and arb.qos.up_steps >= 29,
                f"vrate={arb.qos.vrate_fp} up={arb.qos.up_steps}")
    ok &= check("up_margin_decays_to_one",
                arb.margin(lc) == 1 and arb.margin(be) == 1,
                f"margin_lc={arb.margin(lc)}")
    # Deep comfort: clamp at vrate_max (400%); byte-fair ratio restored
    # (margin 1, eff_quantum scaled uniformly: 8 -> 32).
    feed_seq(arb, 100, [CLEAN] * 150, start_ns=200 * QOS_WINDOW_NS)
    ok &= check("up_clamps_at_max",
                arb.vrate_fp() == VRATE_MAX_FP_DEFAULT,
                f"vrate={arb.qos.vrate_fp}")
    ok &= check("up_max_byte_fair_ratio",
                arb.margin(lc) == 1 and arb.eff_quantum() == 32 and
                arb._budget(lc) * 2 == arb._budget(be),
                f"eff_q={arb.eff_quantum()} lc={arb._budget(lc)} "
                f"be={arb._budget(be)}")
    return ok


def test_qos_window_gates():
    """Window gates: <20 samples never steps; the rpct percentile
    condition is exact (over-fraction > (100-rpct)%); the up gate needs
    mean < rlat/2; BE completions never feed the window."""
    # Under-sampled windows never move vrate.
    arb, lc, be = make_lc_be_arb()
    for w in range(5):
        for i in range(5):
            arb.qos_feed(100, w * QOS_WINDOW_NS + (i + 1) * 1000, VIOL)
    ok = check("qos_min_samples_gate",
               arb.vrate_fp() == VRATE_FP_ONE and arb.qos.down_steps == 0,
               f"vrate={arb.qos.vrate_fp} down={arb.qos.down_steps}")

    # rpct 99: 1 of 30 samples over rlat (3.3% > 1%) violates.
    lats = [VIOL] + [int(RLAT * 0.9)] * 29
    feed_seq(arb, 100, [lats, lats, lats], samples=30,
             start_ns=10 * QOS_WINDOW_NS)
    ok &= check("qos_rpct99_one_of_30_violates",
                arb.vrate_fp() == 205 and arb.qos.down_steps == 1,
                f"vrate={arb.qos.vrate_fp}")

    # rpct 90: 3 of 30 over (10%) is exactly AT the allowed fraction —
    # strict > keeps it legal (no step).
    arb2, _, _ = make_lc_be_arb(qos_rpct=90)
    lats2 = [VIOL] * 3 + [int(RLAT * 0.9)] * 27
    feed_seq(arb2, 100, [lats2] * 5, samples=30)
    ok &= check("qos_rpct90_boundary_legal",
                arb2.vrate_fp() == VRATE_FP_ONE and arb2.qos.down_steps == 0,
                f"vrate={arb2.qos.vrate_fp} down={arb2.qos.down_steps}")

    # All under rlat but mean >= rlat/2: neither gate fires (hold).
    arb3, _, _ = make_lc_be_arb()
    feed_seq(arb3, 100, [int(RLAT * 0.6)] * 5)
    ok &= check("qos_mean_gate_midband_hold",
                arb3.vrate_fp() == VRATE_FP_ONE and arb3.qos.up_steps == 0
                and arb3.qos.last_comfortable == 0,
                f"vrate={arb3.qos.vrate_fp} up={arb3.qos.up_steps} "
                f"comfortable={arb3.qos.last_comfortable}")

    # BE completions never feed the window (role filter, iolatency口径).
    arb4, _, _ = make_lc_be_arb()
    for w in range(10):
        for i in range(25):
            arb4.qos_feed(200, w * QOS_WINDOW_NS + (i + 1) * 1000, 10 * RLAT)
    ok &= check("qos_be_completions_filtered",
                arb4.qos.window_total == 0 and arb4.vrate_fp() == VRATE_FP_ONE,
                f"window_total={arb4.qos.window_total}")
    return ok


def test_qos_coloc_feedback_loop():
    """Closed-loop shape: a coloc drive where LC completions report the
    byte-fair-regime latency (>> rlat) drives vrate DOWN mid-run and the
    served mix shifts LC-ward — the feedback loop actuates on simulated
    latency, not on a scripted vrate."""
    arb, lc, be = make_lc_be_arb()

    # Phase 1: drive one round at the CURRENT budgets, then feed one
    # window of LC completions at the measured byte-fair latency shape.
    # Each phase's window evaluates the previous phase's; ~9 evaluated
    # violating windows -> 4 down steps (2-window hysteresis).
    t_ns = 0
    for phase in range(10):
        drive_backlogged(arb, {100: R4K, 200: W64K}, rounds=1)
        for i in range(25):
            t_ns += QOS_WINDOW_NS // 25
            arb.qos_feed(100, t_ns, VIOL)
    ok = check("coloc_loop_vrate_falls",
               arb.vrate_fp() < VRATE_FP_ONE and arb.qos.down_steps >= 1,
               f"vrate={arb.qos.vrate_fp} down={arb.qos.down_steps}")

    # Phase 2: sustained violation floors vrate; the drive now serves
    # the LC-ward mix (share far off the constant byte-fair 2:1).
    feed_seq(arb, 100, [VIOL] * 10, start_ns=t_ns + QOS_WINDOW_NS)
    ok &= check("coloc_loop_floor_reached",
                arb.vrate_fp() == VRATE_MIN_FP_DEFAULT,
                f"vrate={arb.qos.vrate_fp}")
    served = drive_backlogged(arb, {100: R4K, 200: W64K}, rounds=2)
    share = served[200] * 65536 / (served[100] * 4096 + served[200] * 65536)
    ok &= check("coloc_loop_share_not_two_to_one",
                share < 0.45,
                f"be_share={share:.3f} (byte-fair design 0.667)")
    ok &= check("coloc_loop_lc_first",
                served[100] * 4096 > served[200] * 65536,
                f"lc_bytes={served[100] * 4096} be_bytes={served[200] * 65536}")
    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ok = True
    ok &= test_default_cfg_and_estimate()
    ok &= test_case1_cost_not_request_fairness()
    ok &= test_case2_weight_ratio()
    ok &= test_size_bucket_cost_ratio()
    ok &= test_custom_table_flips_fairness()
    ok &= test_custom_quantum_scaling()
    ok &= test_zero_entry_fallback()
    ok &= test_requeue_refund_parity()
    ok &= test_min_budget_floor()
    ok &= test_overshoot_bound()
    ok &= test_dispatch_failed_lifecycle()
    ok &= test_work_conservation_and_round_invariants()
    ok &= test_s11_homo_coloc_byte_share()
    ok &= test_qos_vrate_one_byte_fair_regression()
    ok &= test_qos_down_ladder_uniform_shrink_and_margin()
    ok &= test_qos_down_share_shift_vs_byte_fair()
    ok &= test_qos_up_recovery_and_clamp()
    ok &= test_qos_window_gates()
    ok &= test_qos_coloc_feedback_loop()
    print("MODEL " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
