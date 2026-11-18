#!/usr/bin/env python3
"""arb_d2fq reference model -- executable specification of the D2FQ
(FAST'21) homomorphic arbiter (gate 0 of its CI loop).

Mirrors userspace/arb_d2fq.bpf.c (d2fq_cost / d2fq_advance / d2fq_band /
the pick_cgroup two-pass scan / the inserted-dispatched-completed-requeue
hooks), cross-checked against the open-source reference kernel
skkucsl/d2fq reference tree (block/d2fq.c: __d2fq_update_vt / __d2fq_start_request /
d2fq_assign_dd deceptive idleness).  Any change to the BPF must be
mirrored here and vice versa (ARB_POLICY_DEV_GUIDE.md §1).

Model contract (identical to arb_d2fq.bpf.c):
  - charge at dispatch time: vtime[lane] += cost(op,size)*VT_NUM*100 /
    (type_weight[lane] * weight * (fast_share if LC else 1))
    (reference: tmp = (840/weight * dlen) >> 10 at completion; ours folds
    the >>10 into VT_NUM and charges at dispatched_request because the
    completion hook's cgroup attribution is broken in this kernel)
  - gvt = min vtime over eligible (pending>0, not in a no-progress skip
    window) lanes of registered tenants; refreshed every pick and cached
    globally (reference: gh_return_min over active flows).  State is
    CGROUP-level: the q policy holds a per-cgroup queue and any hctx may
    pop it (scx_io_validate_dispatch_rq only requires the same queue), so
    per-(cgroup,hctx) lanes would fragment the eligible set and collapse
    the weight semantics to ~1:1 (the 2026-11-14 d2fq campaign failure;
    fixed in arb_d2fq.bpf.c the same day, mirroring arb_wdrr's S12)
  - band = HIGH if gap <= md_range, MEDIUM if gap <= thrsh, else LOW;
    band is carried in the sort key and telemetry (with a common gvt the
    band order equals the min-vtime order -- the class WEIGHTS act through
    the fast_share charge divisor, the device WRR h:m:l translation)
  - LC fast share: lanes of tenants with 0 < arb_prio < lc_prio_max are
    charged 1/fast_share (default 6 = sysctl_d2fq_cls_base enabled value,
    the device WRR HIGH:LOW weight ratio)
  - pick = min (band, vtime) among eligible lanes -> returns the cgroup
  - deceptive idleness: insert after both lanes empty for > di_period_ns
    resets both vtimes to the cached gvt (no vtime banking)
  - F2-style no-progress guard: a tenant whose winning picks dispatched
    nothing PICK_NO_PROGRESS_MAX times in a row is skipped for
    NOPROGRESS_RETRY_NS (bounded, so a holding queue policy can still get
    its dispatch called on retry and release held requests)
  - requeue refund: exact inverse of the charge, never pending++
  - dispatch_failed: retire one pending on the last-picked lane; 4 in a
    row hard-reset the tenant (ghost)

Acceptance encoded (task brief): "d2fq 验证两类长期份额≈成本权重比" --
equal-weight tenants issuing different IO types reach charged-COST share
1:1 (not request-count fairness), weight ratios hold on the vtime scale,
type-weights and the LC fast share shift shares exactly by their ratios.
"""
import sys
from collections import deque

# ---------------------------------------------------------------------------
# Constants (encode arb_d2fq.bpf.c verbatim)
# ---------------------------------------------------------------------------

SECTOR = 512
VT_NUM = 840 << 10            # D2FQ_VT_NUM
THRSH = 64 * VT_NUM           # D2FQ_DEFAULT_THRSH
MD_RATIO = 70                 # D2FQ_DEFAULT_MD_RATIO
LC_PRIO_MAX = 3               # D2FQ_DEFAULT_LC_PRIO_MAX
FAST_SHARE = 6                # D2FQ_DEFAULT_FAST_SHARE (sysctl_d2fq_cls_base)
TYPE_WEIGHT = [100, 100]      # read, write (percent)
DI_PERIOD_NS = 200 * 1000 * 1000   # D2FQ_DEFAULT_DI_PERIOD_NS (0.2s ref)
PICK_NO_PROGRESS_MAX = 3      # D2FQ_PICK_NO_PROGRESS_MAX
NOPROGRESS_RETRY_NS = 1000 * 1000  # D2FQ_NOPROGRESS_RETRY_NS
GHOST_RESET = 4               # D2FQ_DISPATCH_FAIL_GHOST_RESET
MAX_TENANTS = 64              # ARB_MAX_TENANTS

LANE_READ, LANE_WRITE = 0, 1
BAND_HIGH, BAND_MEDIUM, BAND_LOW = 0, 1, 2

# d2fq_cost_table defaults (row 0 = READ, row 1 = WRITE), the arb_cost x64
# table; columns are the arb_cost_size_bucket boundaries.
COST_TABLE = (
    (64, 256, 1024, 2048, 4096),
    (256, 1024, 4096, 8192, 16384),
)


def size_bucket(nr_sectors):
    """Mirror ioext_arb_common.h arb_cost_size_bucket()."""
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


def op_type(opf):
    """Mirror arb_cost_op_type(): opf & 0xff == 1 -> WRITE."""
    return LANE_WRITE if (opf & 0xFF) == 1 else LANE_READ


def d2fq_cost(opf, nr_sectors):
    """Mirror arb_d2fq.bpf.c d2fq_cost()."""
    return COST_TABLE[op_type(opf)][size_bucket(nr_sectors)]


def d2fq_advance(cost, lane, lc, weight,
                 fast_share=FAST_SHARE, type_weight=None):
    """Mirror arb_d2fq.bpf.c d2fq_advance():
    cost * VT_NUM * 100 // (type_weight * weight * (fast_share if lc))."""
    if type_weight is None:
        type_weight = TYPE_WEIGHT
    if weight == 0:
        weight = 1
    tw = type_weight[lane] if type_weight[lane] else 100
    div = tw * weight
    if lc and fast_share > 1:
        div *= fast_share
    return cost * VT_NUM * 100 // div


# Request fixtures: (opf, nr_sectors).
R4K = (0, 8)
W4K = (1, 8)
R16K = (0, 32)
W16K = (1, 32)
R128K = (0, 256)
W256K = (1, 512)


# ---------------------------------------------------------------------------
# Tenant / Arbiter (mirror struct d2fq_tenant_state + pick_cgroup)
# ---------------------------------------------------------------------------

class Lane:
    def __init__(self):
        self.vtime = 0
        self.pending = 0
        self.charges = 0
        self.dispatches = 0
        self.picks = 0


class Tenant:
    def __init__(self, cgid, weight=100, prio=7):
        self.cgid = cgid
        self.weight = weight       # policy_meta arb_weight
        self.prio = prio           # policy_meta arb_prio
        self.lane = [Lane(), Lane()]
        self.inflight = 0
        self.inserts = 0
        self.requeues = 0
        self.refunded_cost = 0
        self.di_resets = 0
        self.last_active_ns = 0
        self.last_pick_ns = 0
        self.last_pick_dispatches = 0
        self.pick_no_progress = 0
        self.dispatch_fails = 0

    @property
    def lc(self):
        return bool(self.prio and self.prio < LC_PRIO_MAX)

    def total_dispatches(self):
        return self.lane[LANE_READ].dispatches + self.lane[LANE_WRITE].dispatches


class D2FQArbiter:
    """Cgroup-level D2FQ core (mirrors arb_d2fq.bpf.c since 2026-11-14:
    one vtime lane pair per cgroup, shared by every hctx -- the per-hctx
    lane split was the weight-collapse bug).  slots[0..63] hold cgids
    (0 = empty); the cached gvt and band telemetry mirror
    struct d2fq_hctx_state (gvt = slot 0's global cache)."""

    def __init__(self, thrsh=THRSH, md_ratio=MD_RATIO, lc_prio_max=LC_PRIO_MAX,
                 fast_share=FAST_SHARE, type_weight=None,
                 di_period_ns=DI_PERIOD_NS):
        self.slots = [0] * MAX_TENANTS
        self.tenants = {}
        self.gvt = 0             # cached min eligible lane vtime
        self.picks = 0
        self.band_picks = [0, 0, 0]
        self.last_lane = LANE_READ
        self.thrsh = thrsh
        self.md_ratio = md_ratio
        self.lc_prio_max = lc_prio_max
        self.fast_share = fast_share
        self.type_weight = type_weight or list(TYPE_WEIGHT)
        self.di_period_ns = di_period_ns

    def register(self, tenant, slot):
        self.slots[slot] = tenant.cgid
        self.tenants[tenant.cgid] = tenant

    def band(self, gap):
        """Mirror d2fq_band() (telemetry/sort key; order-equivalent to
        min-vtime because the band is monotone in vtime for a common gvt)."""
        md_range = self.thrsh * (100 - self.md_ratio) // 100
        if not self.thrsh:
            return BAND_HIGH
        if gap <= md_range:
            return BAND_HIGH
        if gap <= self.thrsh:
            return BAND_MEDIUM
        return BAND_LOW

    def _skipped(self, ts, now):
        return (ts.pick_no_progress > PICK_NO_PROGRESS_MAX and
                now - ts.last_pick_ns < NOPROGRESS_RETRY_NS)

    def pick(self, now):
        """Mirror pick_cgroup's two-pass scan (the BPF runs each pass as a
        bpf_loop over the 64 active-list slots; eligibility, order and
        tie-breaking are identical); returns cgid or 0."""
        gvt = None
        for idx in range(MAX_TENANTS):
            cgid = self.slots[idx]
            if not cgid:
                continue
            ts = self.tenants.get(cgid)
            if ts is None:
                continue
            if self._skipped(ts, now):
                continue
            for lane in (LANE_READ, LANE_WRITE):
                if ts.lane[lane].pending:
                    vtime = ts.lane[lane].vtime
                    if gvt is None or vtime < gvt:
                        gvt = vtime
        if gvt is None:
            return 0

        best = None   # (band, vtime, idx, lane, cgid)
        for idx in range(MAX_TENANTS):
            cgid = self.slots[idx]
            if not cgid:
                continue
            ts = self.tenants.get(cgid)
            if ts is None or self._skipped(ts, now):
                continue
            for lane in (LANE_READ, LANE_WRITE):
                if not ts.lane[lane].pending:
                    continue
                vtime = ts.lane[lane].vtime
                gap = vtime - gvt if vtime > gvt else 0
                band = self.band(gap)
                if best is None or band < best[0] or \
                        (band == best[0] and vtime < best[1]):
                    best = (band, vtime, idx, lane, cgid)
        if best is None:
            return 0

        band, _, _, lane, cgid = best
        ts = self.tenants[cgid]

        # F2-style empty-pop guard bookkeeping on the winning tenant
        total = ts.total_dispatches()
        if total == ts.last_pick_dispatches:
            ts.pick_no_progress += 1
        else:
            ts.pick_no_progress = 0
        ts.last_pick_dispatches = total
        ts.last_pick_ns = now

        self.gvt = gvt
        self.picks += 1
        self.band_picks[band] += 1
        self.last_lane = lane
        ts.lane[lane].picks += 1
        return cgid

    # ---- framework hook mirrors (arb_d2fq.bpf.c note_*) ----

    def note_inserted(self, cgid, opf, now):
        ts = self.tenants[cgid]
        if ts.lane[LANE_READ].pending == 0 and \
                ts.lane[LANE_WRITE].pending == 0 and ts.inflight == 0:
            # DI requires the tenant to hold NOTHING anywhere (reference
            # di_timer fires only with no requests in the device).  The
            # inflight==0 guard stops a throttled tenant whose queue is
            # momentarily fully dispatched from being re-levelled to gvt:
            # that reset would erase the vtime deficit WFQ throttling runs
            # on (2026-11-14 smoke: 0.72 share with ~1 reset/s without it).
            if ts.last_active_ns and \
                    now - ts.last_active_ns > self.di_period_ns:
                ts.lane[LANE_READ].vtime = self.gvt
                ts.lane[LANE_WRITE].vtime = self.gvt
                ts.di_resets += 1
        ts.pick_no_progress = 0
        ts.last_active_ns = now
        ts.lane[op_type(opf)].pending += 1
        ts.inserts += 1

    def note_dispatched(self, cgid, opf, nr_sectors, now):
        ts = self.tenants[cgid]
        lane = op_type(opf)
        cost = d2fq_cost(opf, nr_sectors)
        adv = d2fq_advance(cost, lane, ts.lc, ts.weight,
                           self.fast_share, self.type_weight)
        if ts.lane[lane].pending:
            ts.lane[lane].pending -= 1
        ts.inflight += 1
        ts.pick_no_progress = 0
        ts.dispatch_fails = 0
        ts.last_active_ns = now
        ts.lane[lane].vtime += adv
        ts.lane[lane].charges += cost
        ts.lane[lane].dispatches += 1
        return cost

    def note_requeue(self, cgid, opf, nr_sectors):
        """Exact inverse of the charge; never pending++ (guide pitfall 4)."""
        ts = self.tenants[cgid]
        lane = op_type(opf)
        cost = d2fq_cost(opf, nr_sectors)
        adv = d2fq_advance(cost, lane, ts.lc, ts.weight,
                           self.fast_share, self.type_weight)
        if ts.inflight:
            ts.inflight -= 1
        ts.lane[lane].vtime = ts.lane[lane].vtime - adv \
            if ts.lane[lane].vtime > adv else 0
        ts.refunded_cost += cost
        ts.requeues += 1

    def note_completed(self, cgid):
        ts = self.tenants[cgid]
        if ts.inflight:
            ts.inflight -= 1

    def note_dispatch_failed(self, cgid):
        """Mirror dispatch_failed: retire one pending on the last-picked
        lane; GHOST_RESET consecutive failures hard-reset the tenant."""
        ts = self.tenants[cgid]
        ts.dispatch_fails += 1
        if ts.dispatch_fails >= GHOST_RESET:
            ts.lane[LANE_READ].pending = 0
            ts.lane[LANE_WRITE].pending = 0
            ts.pick_no_progress = 0
            ts.dispatch_fails = 0
            return
        lane = self.last_lane
        if ts.lane[lane].pending:
            ts.lane[lane].pending -= 1


# ---------------------------------------------------------------------------
# Saturated drive (single hctx; keep every listed tenant backlogged)
# ---------------------------------------------------------------------------

def drive_backlogged(arb, requests, total, now0=1_000_000, now_step=1000):
    """Keep every listed tenant backlogged at exactly one pending request
    (re-insert after each dispatch).  requests: cgid -> (opf, nr_sectors).
    Returns served counts per cgid."""
    served = {cg: 0 for cg in requests}
    for cg in requests:
        if arb.tenants[cg].lane[op_type(requests[cg][0])].pending == 0:
            arb.note_inserted(cg, requests[cg][0], now0)
    done = 0
    now = now0
    while done < total:
        cg = arb.pick(now)
        if cg == 0:
            raise RuntimeError("drive_backlogged: pick returned 0 with work")
        opf, nr_sectors = requests[cg]
        arb.note_dispatched(cg, opf, nr_sectors, now)
        arb.note_completed(cg)
        served[cg] += 1
        done += 1
        now += now_step
        arb.note_inserted(cg, opf, now)
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

def test_cost_and_advance():
    cases = [
        ((0, 8), 64),       # 4K read
        ((0, 32), 256),     # 16K read
        ((0, 512), 4096),   # 256K read
        ((1, 8), 256),      # 4K write
        ((1, 512), 16384),  # 256K write (max charge)
        ((0x101, 8), 256),  # opf flags: op = opf & 0xff == 1 -> WRITE
    ]
    bad = [(rq, want, d2fq_cost(*rq)) for rq, want in cases
           if d2fq_cost(*rq) != want]
    ok = check("cost_table", not bad, f"mismatches={bad}")

    # advance formula spot values (floor semantics like the BPF integer div)
    adv_r = d2fq_advance(64, LANE_READ, False, 100)     # 4K read, w=100
    adv_w = d2fq_advance(256, LANE_WRITE, False, 100)   # 4K write, w=100
    adv_lc = d2fq_advance(64, LANE_READ, True, 100)     # LC fast share /6
    adv_tw = d2fq_advance(256, LANE_WRITE, False, 100,
                         type_weight=[100, 400])
    ok &= check("advance_read", adv_r == 64 * VT_NUM * 100 // 10000,
                f"adv_r={adv_r}")
    ok &= check("advance_write_4x", abs(adv_w - 4 * adv_r) <= 1,
                f"adv_w={adv_w} adv_r={adv_r} (write cost 4x read cost; "
                "+-1 is the shared integer-division floor)")
    ok &= check("advance_lc_fast_share", abs(adv_lc - adv_r // 6) <= 1,
                f"adv_lc={adv_lc} (fast_share=6)")
    ok &= check("advance_type_weight",
                adv_tw == adv_r,
                f"adv_tw={adv_tw} (tw=400 -> 4K write == 4K read advance)")
    return ok


def test_two_type_cost_fairness():
    """THE acceptance case: equal weights, A = 4K reads (cost 64) vs
    B = 4K writes (cost 256) -> long-run charged COST 1:1 while request
    counts run ~4:1 (cost-weighted fairness, not request-count or byte
    fairness)."""
    arb = D2FQArbiter()
    a, b = Tenant(100, weight=100), Tenant(200, weight=100)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: R4K, 200: W4K}, 20000)
    ca = a.lane[LANE_READ].charges
    cb = b.lane[LANE_WRITE].charges
    cost_ratio = ca / cb
    req_ratio = served[100] / served[200]
    ok = check("case1_cost_ratio_1to1", 0.95 <= cost_ratio <= 1.05,
               f"charged_cost ratio={cost_ratio:.4f} A={ca} B={cb}")
    ok &= check("case1_request_ratio_4", 3.5 <= req_ratio <= 4.5,
                f"request ratio={req_ratio:.3f} (4K reads vs 4K writes)")
    ok &= check("case1_not_request_fair", req_ratio > 3,
                f"request ratio={req_ratio:.3f} must NOT be ~1 "
                "(request-count fairness would be a spec failure)")
    # vtime equilibrium: the two lanes' vtimes track each other closely
    vt_gap = abs(a.lane[LANE_READ].vtime - b.lane[LANE_WRITE].vtime)
    ok &= check("case1_vtime_equilibrium",
                vt_gap <= 4 * d2fq_cost(*R4K) * VT_NUM // 100 * 100,
                f"|dvtime|={vt_gap}")
    return ok


def test_weight_ratio():
    """Weights 100 vs 300, same request type -> charged cost ratio ~3.0
    (weighted fairness on the vtime scale)."""
    arb = D2FQArbiter()
    a, b = Tenant(100, weight=100), Tenant(200, weight=300)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: R4K, 200: R4K}, 20000)
    ratio = b.lane[LANE_READ].charges / a.lane[LANE_READ].charges
    req_ratio = served[200] / served[100]
    ok = check("case2_weight_cost_ratio_3", 2.9 <= ratio <= 3.1,
               f"B/A charged cost={ratio:.4f} expected ~3.0")
    ok &= check("case2_weight_request_ratio_3", 2.9 <= req_ratio <= 3.1,
                f"B/A requests={req_ratio:.4f}")
    return ok


def test_type_weights_shift():
    """--type-weights 100:400: a 4K write (cost 256) with type weight 400
    advances the same vtime as a 4K read (cost 64) at 100 -> the write lane
    rides 4x its cost share (per-type service-share weight semantics)."""
    arb = D2FQArbiter(type_weight=[100, 400])
    a, b = Tenant(100, weight=100), Tenant(200, weight=100)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: R4K, 200: W4K}, 20000)
    req_ratio = served[100] / served[200]
    cost_ratio = a.lane[LANE_READ].charges / b.lane[LANE_WRITE].charges
    ok = check("case3_type_weight_request_ratio_1", 0.85 <= req_ratio <= 1.15,
               f"request ratio={req_ratio:.4f} expected ~1.0 "
               "(tw 400 cancels the 4x write cost)")
    ok &= check("case3_type_weight_cost_ratio_1_4", 0.2 <= cost_ratio <= 0.3,
                f"cost ratio A/B={cost_ratio:.4f} expected ~0.25")
    return ok


def test_lc_fast_share():
    """LC tenant (prio 1) vs BE (prio 7), equal weights, same request type:
    the LC lane is charged 1/fast_share -> charged cost ratio ~fast_share."""
    arb = D2FQArbiter()
    lc, be = Tenant(100, weight=100, prio=1), Tenant(200, weight=100, prio=7)
    arb.register(lc, 0)
    arb.register(be, 1)
    served = drive_backlogged(arb, {100: R4K, 200: R4K}, 20000)
    ratio = lc.lane[LANE_READ].charges / be.lane[LANE_READ].charges
    ok = check("case4_lc_fast_share_ratio_6", 5.4 <= ratio <= 6.6,
               f"LC/BE charged cost={ratio:.4f} expected ~6.0 "
               "(sysctl_d2fq_cls_base enabled value)")
    # fast_share=1 disables: back to plain weighted fairness
    arb2 = D2FQArbiter(fast_share=1)
    lc2, be2 = Tenant(100, weight=100, prio=1), Tenant(200, weight=100, prio=7)
    arb2.register(lc2, 0)
    arb2.register(be2, 1)
    drive_backlogged(arb2, {100: R4K, 200: R4K}, 20000)
    ratio2 = lc2.lane[LANE_READ].charges / be2.lane[LANE_READ].charges
    ok &= check("case4_fast_share_off", 0.95 <= ratio2 <= 1.05,
                f"LC/BE with fast_share=1: {ratio2:.4f} expected ~1.0")
    return ok


def test_band_telemetry():
    """Band math (reference __d2fq_start_request gap classes) and its
    telemetry; band in the sort key is order-equivalent to min-vtime, the
    asserts verify the vocabulary, not a different order."""
    arb = D2FQArbiter()
    md = THRSH * 30 // 100
    ok = check("band_high", arb.band(0) == BAND_HIGH and arb.band(md) == BAND_HIGH,
               f"md_range={md}")
    ok &= check("band_medium", arb.band(md + 1) == BAND_MEDIUM and
                arb.band(THRSH) == BAND_MEDIUM,
                f"band({md + 1})={arb.band(md + 1)} band({THRSH})={arb.band(THRSH)}")
    ok &= check("band_low", arb.band(THRSH + 1) == BAND_LOW)
    arb0 = D2FQArbiter(thrsh=0)
    ok &= check("band_disabled_thrsh0", arb0.band(10**12) == BAND_HIGH,
                "thrsh=0 disables banding (pure WFQ)")
    return ok


def test_deceptive_idleness():
    """Insert after both lanes empty for > di_period resets the lane vtimes
    to the cached gvt (no vtime banking); a short idle keeps the credit."""
    arb = D2FQArbiter()
    a = Tenant(100, weight=100)
    arb.register(a, 0)
    t0 = 1_000_000
    arb.note_inserted(100, R4K[0], t0)
    arb.pick(t0)                       # caches gvt (vtime 0 at pick time)
    arb.note_dispatched(100, *R4K, now=t0)
    arb.note_completed(100)            # both lanes empty now
    ok = check("di_prelim_gvt_cache", arb.gvt == 0,
               f"gvt={arb.gvt}")

    # long idle: both lanes empty, last_active old -> reset to cached gvt
    banked = a.lane[LANE_READ].vtime
    a.lane[LANE_READ].vtime = banked + 123 * THRSH     # simulate stale banking
    arb.note_inserted(100, R4K[0], t0 + DI_PERIOD_NS + 1)
    ok &= check("di_long_idle_resets_to_gvt",
                a.lane[LANE_READ].vtime == 0 and a.di_resets == 1,
                f"vtime={a.lane[LANE_READ].vtime} expected cached gvt=0 "
                f"resets={a.di_resets}")

    # short idle: credit kept (the reference only resets after di_period)
    arb.pick(t0 + DI_PERIOD_NS + 1)
    arb.note_dispatched(100, *R4K, now=t0 + DI_PERIOD_NS + 1)
    arb.note_completed(100)
    kept = a.lane[LANE_READ].vtime
    ok = check("di_short_idle_keeps_credit", kept > 0, f"kept vtime={kept}")
    arb.note_inserted(100, R4K[0], t0 + DI_PERIOD_NS + 1 + DI_PERIOD_NS // 2)
    ok &= check("di_short_idle_no_reset",
                a.lane[LANE_READ].vtime == kept and a.di_resets == 1,
                f"vtime={a.lane[LANE_READ].vtime} expected kept={kept} "
                f"resets={a.di_resets}")

    # inflight>0 guard: a tenant whose last dispatched request is still on
    # the device is NOT idle (the reference di_timer needs an EMPTY device).
    # Without this guard a throttled tenant whose queue was momentarily
    # fully dispatched re-levelled to gvt on every insert past di_period,
    # erasing the vtime deficit WFQ throttling runs on (2026-11-14 smoke:
    # 0.72 share with ~1 reset/s on the starved tenant).
    arb.pick(t0 + 2 * DI_PERIOD_NS)
    arb.note_dispatched(100, *R4K, now=t0 + 2 * DI_PERIOD_NS)  # pending 0, inflight 1
    stuck_vtime = a.lane[LANE_READ].vtime
    arb.note_inserted(100, R4K[0], t0 + 12 * DI_PERIOD_NS)     # idle >> period
    ok &= check("di_inflight_guard_no_reset",
                a.lane[LANE_READ].vtime == stuck_vtime and a.di_resets == 1,
                f"vtime={a.lane[LANE_READ].vtime} expected {stuck_vtime} "
                f"resets={a.di_resets}")
    return ok


def test_no_progress_liveness():
    """A stuck (holding-policy) tenant must not absorb every pick: after
    PICK_NO_PROGRESS_MAX empty-pop wins it is skipped for the retry window
    so the healthy tenant is served; the periodic retry lets the stuck
    tenant dispatch once its hold releases (q_linnos pairing)."""
    arb = D2FQArbiter()
    stuck, healthy = Tenant(100, weight=100), Tenant(200, weight=100)
    arb.register(stuck, 0)
    arb.register(healthy, 1)
    now = 1_000_000
    arb.note_inserted(100, R4K[0], now)
    arb.note_inserted(200, R4K[0], now)

    # stuck tenant wins picks but its policy pops nothing (empty pops);
    # the healthy tenant must still get served inside the retry window.
    healthy_served = 0
    stuck_retried = False
    for _ in range(200):
        cg = arb.pick(now)
        if cg == 0:
            now += NOPROGRESS_RETRY_NS      # blk-mq completion kick
            continue
        if cg == 100:
            # empty pop: no dispatch, nothing changes; on the 5th win the
            # guard skips it within the retry window
            if stuck.pick_no_progress > PICK_NO_PROGRESS_MAX:
                stuck_retried = True
            now += 1000
            continue
        arb.note_dispatched(200, *R4K, now=now)
        arb.note_completed(200)
        arb.note_inserted(200, R4K[0], now)
        healthy_served += 1
        now += 1000

    ok = check("noprog_healthy_served",
               healthy_served >= 10,
               f"healthy dispatches={healthy_served} while stuck tenant "
               "held picks (liveness)")
    ok &= check("noprog_stuck_guard_armed", stuck.pick_no_progress > 0,
                f"pick_no_progress={stuck.pick_no_progress}")
    ok &= check("noprog_stuck_retried_after_window", stuck_retried,
                "the stuck tenant must keep being retried after the "
                "retry window (holding policy release path)")

    # recovery: the stuck tenant's hold releases; it dispatches and the
    # guard disarms
    arb.note_dispatched(100, *R4K, now=now)
    ok &= check("noprog_recovery_disarms",
                stuck.pick_no_progress == 0 and stuck.dispatch_fails == 0,
                f"pick_no_progress={stuck.pick_no_progress}")
    return ok


def test_requeue_refund_no_leak():
    """Requeue refunds the exact charged advance + inflight--, never
    pending++ (the framework re-inserts and re-fires inserted_request)."""
    arb = D2FQArbiter()
    a = Tenant(100, weight=100)
    arb.register(a, 0)
    now = 1_000_000
    arb.note_inserted(100, R4K[0], now)
    before = a.lane[LANE_READ].vtime
    arb.note_dispatched(100, *R4K, now=now)
    charged = a.lane[LANE_READ].vtime - before
    arb.note_requeue(100, *R4K)
    pending_after_requeue = a.lane[LANE_READ].pending
    arb.note_inserted(100, R4K[0], now)      # framework re-insert
    arb.note_dispatched(100, *R4K, now=now)
    arb.note_completed(100)
    ok = check("requeue_no_pending_increment",
               pending_after_requeue == 0,
               f"pending after requeue={pending_after_requeue}")
    ok &= check("requeue_exact_refund",
                a.lane[LANE_READ].vtime - before == charged and
                a.refunded_cost == d2fq_cost(*R4K) and a.requeues == 1,
                f"net vtime={a.lane[LANE_READ].vtime - before} "
                f"charged={charged}")
    ok &= check("requeue_conservation",
                a.inflight == 0 and a.lane[LANE_READ].pending == 0)
    return ok


def test_dispatch_failed_pending():
    """Invalid-rq path: retire one pending on the last-picked lane; four in
    a row hard-reset the tenant (crystallized ghost)."""
    arb = D2FQArbiter()
    a = Tenant(100, weight=100)
    arb.register(a, 0)
    now = 1_000_000
    arb.note_inserted(100, R4K[0], now)
    arb.note_inserted(100, W4K[0], now)
    arb.pick(now)
    lane = arb.last_lane
    arb.note_dispatch_failed(100)
    ok = check("dispatch_failed_retires_pending",
               a.lane[lane].pending == 0 and a.dispatch_fails == 1,
               f"pending={a.lane[lane].pending} fails={a.dispatch_fails}")
    for _ in range(3):
        arb.note_dispatch_failed(100)
    ok &= check("dispatch_failed_ghost_reset",
                a.lane[0].pending == 0 and a.lane[1].pending == 0 and
                a.dispatch_fails == 0 and a.pick_no_progress == 0,
                "4 consecutive failures hard-reset the tenant")
    return ok


def test_work_conservation():
    """Single active tenant: every pick selects it (work conservation),
    idle tenants are never picked; with nothing queued pick returns 0."""
    arb = D2FQArbiter()
    a, b = Tenant(100, weight=100), Tenant(200, weight=100)
    arb.register(a, 0)
    arb.register(b, 1)
    served = drive_backlogged(arb, {100: R4K}, 100)
    ok = check("work_conservation",
               served[100] == 100 and b.inserts == 0 and
               b.lane[LANE_READ].dispatches == 0,
               f"served={served[100]} B dispatches={b.lane[LANE_READ].dispatches}")
    # the drive leaves exactly one re-inserted pending: drain it, then the
    # empty arbiter must return 0 (strict-mode ordinary no-work NULL)
    now = 1_000_000 + 200_000
    arb.pick(now)
    arb.note_dispatched(100, *R4K, now=now)
    arb.note_completed(100)
    ok &= check("idle_not_picked", arb.pick(now) == 0,
                "empty arbiter must return 0 (strict NULL)")
    return ok


def test_vtime_wraparound_bound():
    """Overflow bound: at the max charge (256K write, cost 16384) and
    weight 1 the lane vtime stays far below u64 for any realistic drive
    (the reference printk's on the same hazard instead of wrapping)."""
    adv = d2fq_advance(16384, LANE_WRITE, False, 1)
    dispatches_to_wrap = (2**64) // adv
    ok = check("vtime_wraparound_bound",
               dispatches_to_wrap > 100 * 1000 * 1000,
               f"dispatches to wrap={dispatches_to_wrap} adv={adv}")
    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ok = True
    ok &= test_cost_and_advance()
    ok &= test_two_type_cost_fairness()
    ok &= test_weight_ratio()
    ok &= test_type_weights_shift()
    ok &= test_lc_fast_share()
    ok &= test_band_telemetry()
    ok &= test_deceptive_idleness()
    ok &= test_no_progress_liveness()
    ok &= test_requeue_refund_no_leak()
    ok &= test_dispatch_failed_pending()
    ok &= test_work_conservation()
    ok &= test_vtime_wraparound_bound()
    print("MODEL " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
