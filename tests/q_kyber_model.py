#!/usr/bin/env python3
"""q_kyber reference model — executable specification of the kyber core.

Mirrors userspace/ioext_policy_shared.h (the iosched_kyber_* algorithm
core) plus the hook wiring in userspace/q_kyber.bpf.c.  Any change to the
BPF kyber core must be mirrored here and vice versa; the acceptance script
tests/q_kyber_accept.sh runs this self-test as its Gate 0, and the
expected_dispatch_order entries of the kyber_* golden/partial expect files
are derived from this model (algorithm_derivation oracle).

Model contract (identical to ioext_policy_shared.h):
  - 4 domains READ/WRITE/DISCARD/OTHER (ioext_policy_shared.h:70-76)
  - per-domain FIFO, capacity IOSCHED_MAX_PENDING=32 (h:15, push_tail_32 h:413-423)
  - requeue inserts (insert_flags & 0x1) go to the per-domain URGENT FIFO
    (iosched_kyber_insert h:1574-1588) — a deviation from stock kyber-iosched
    which requeues at the domain FIFO head (kernel_src kyber-iosched.c)
  - dispatch (iosched_kyber_dispatch h:1590-1638):
      cur_domain round-robin READ->WRITE->DISCARD->OTHER, starts INVALID
      (init_hctx q_kyber.bpf.c:427) so the first dispatch scans from READ
      (h:1613-1616).  Urgent queue first within a domain; else pop domain
      FIFO head while batching < batch_limit (READ 16 / WRITE 8 / DISCARD 1 /
      OTHER 1, h:33-36).  A domain is skipped when it has no pending request
      or inflight[domain] >= depth_limit[domain] (h:1538-1549).  Returns 0
      when nothing is dispatchable.
  - inflight++ on dispatch (iosched_kyber_note_dispatch h:1551-1557),
    inflight-- in finish_request (q_kyber.bpf.c:647 -> iosched_kyber_finish
    h:1475-1479) and requeue_request (bpf.c:687 -> h:1469-1473)
  - depth machinery = NATIVE kyber law (kyber-iosched.c, mirrored in
    ioext_policy_shared.h since the EWMA port law was retired):
      initial depth_limit = kyber_depth[] caps {READ 256, WRITE 128,
      DISCARD 64, OTHER 16} (init_hctx q_kyber.bpf.c:414-450; lazy-init in
      depth_updated h:1410-1428 for entries whose init_hctx never ran)
      completed_request (bpf.c:603) samples BOTH total and I/O latency of
      every READ/WRITE/DISCARD completion into an 8-bucket histogram of
      width target>>2 (add_latency_sample h:1516-1533); OTHER is never
      sampled (h:1585-1588).  Evaluation (update_depths h:1595-1646, the
      kyber_timer_fn port; BPF has no timers so it runs inline per
      completion, window-gated): per-domain window fires at >=500 samples
      or >1s after the window's first evaluation attempt
      (calculate_percentile h:1544-1583); congestion `bad` = p90 of I/O
      latency of ANY domain in bucket >=4; per-domain resize input = p99
      of TOTAL latency with a remembered-p99 cross-window mechanism
      (domain_p99 h:1611-1624); resize fires iff bad || p99 >= 4, new
      depth = current * (p99+1) >> 2 clamped to [1, cap] (resize h:1573-
      1590).  DIRECTION: bad latency -> GROW up to x2; good latency under
      congestion -> SHRINK down to x0.25; otherwise HOLD.  The old growth
      freeze (update_async_depth collapsing async_depth to
      max(depth_limit)) is REMOVED: depth_limit bounds are only
      [1, kyber_depth[domain]].
  - merge compatibility: same domain only (h:1379-1383), gated by the
    merge_cfg_map enable flag (q_kyber.bpf.c:498-513, loader --merge-enable)
  - targets default read 2ms / write 10ms / discard 5s / other 5ms (h:37-40);
    a CLI target of 0 falls back to the built-in default (h:1359-1366), so
    time dependence is neutralized by passing HUGE targets instead.
    CLI targets reach per-hctx state only via init_hctx (elevator init
    path); the accept rig re-switches the scheduler after loader attach
    to fire it (q_kyber_accept.sh P0 fix).

Determinism: with depth_limit = infinity (determinism mode) and no urgent
(requeue) inserts, dispatch order is fully determined by arrival order per
domain + the cur_domain/batching state — arrival/latency wall-clock times
cannot influence the order.  The Tier-2 goldens rely on this.
"""
import json
import os
import sys

# ── constants (userspace/ioext_policy_shared.h) ────────────────────────
DOMAIN_READ, DOMAIN_WRITE, DOMAIN_DISCARD, DOMAIN_OTHER = 0, 1, 2, 3
DOMAIN_NR = 4                      # enum iosched_kyber_domain (h:70-76)
MAX_PENDING = 32                   # IOSCHED_MAX_PENDING (h:15)
INVALID_INDEX = 0xFFFFFFFF         # IOSCHED_INVALID_INDEX (h:16)
BATCH_LIMIT = {                    # iosched_kyber_batch_limit (h:33-36,1322-1334)
    DOMAIN_READ: 16,               # IOSCHED_KYBER_READ_BATCH   (h:33)
    DOMAIN_WRITE: 8,               # IOSCHED_KYBER_WRITE_BATCH  (h:34)
    DOMAIN_DISCARD: 1,             # IOSCHED_KYBER_DISCARD_BATCH(h:35)
    DOMAIN_OTHER: 1,               # IOSCHED_KYBER_OTHER_BATCH  (h:36)
}
DEFAULT_TARGET_NS = {              # IOSCHED_KYBER_*_TARGET_NS (h:37-40)
    DOMAIN_READ: 2000000,
    DOMAIN_WRITE: 10000000,
    DOMAIN_DISCARD: 5000000000,
    DOMAIN_OTHER: 5000000,
}
MIN_DEPTH = 1                      # IOSCHED_KYBER_MIN_DEPTH (h:41)

# native law constants (kyber-iosched.c; ioext_policy_shared.h h:44-71)
NATIVE_DEPTH_CAP = {               # kyber_depth[] (kyber-iosched.c:59-64)
    DOMAIN_READ: 256,              # IOSCHED_KYBER_DEPTH_CAP_READ
    DOMAIN_WRITE: 128,             # IOSCHED_KYBER_DEPTH_CAP_WRITE
    DOMAIN_DISCARD: 64,            # IOSCHED_KYBER_DEPTH_CAP_DISCARD
    DOMAIN_OTHER: 16,              # IOSCHED_KYBER_DEPTH_CAP_OTHER
}
LATENCY_SHIFT = 2                  # IOSCHED_KYBER_LATENCY_SHIFT (KYBER_LATENCY_SHIFT)
GOOD_BUCKETS = 1 << LATENCY_SHIFT  # IOSCHED_KYBER_GOOD_BUCKETS = 4
LATENCY_BUCKETS = 2 << LATENCY_SHIFT  # IOSCHED_KYBER_LATENCY_BUCKETS = 8
TOTAL_LATENCY, IO_LATENCY = 0, 1   # IOSCHED_KYBER_{TOTAL,IO}_LATENCY
LATENCY_TYPES = 2                 # IOSCHED_KYBER_LATENCY_TYPES
SAMPLED_DOMAINS = 3               # READ+WRITE+DISCARD (OTHER never sampled)
P99_INVALID = -1                   # IOSCHED_KYBER_P99_INVALID
MIN_PCT_SAMPLES = 500              # IOSCHED_KYBER_MIN_PCT_SAMPLES
WINDOW_TIMEOUT_NS = 1000000000     # IOSCHED_KYBER_WINDOW_TIMEOUT_NS
PCT_P90, PCT_P99 = 90, 99          # IOSCHED_KYBER_P90 / P99

REQF_WRITE, REQF_SYNC, REQF_DISCARD, REQF_OTHER = 1, 2, 4, 8  # (h:8-11)


class RequestDesc:
    """struct iosched_request_desc (ioext_policy_shared.h:78-95) subset."""

    def __init__(self, rq_ptr, sector, nr_sectors, op='read', insert_flags=0):
        self.rq_ptr = rq_ptr
        self.sector = sector
        self.cost = nr_sectors if nr_sectors else 1  # desc.cost (ioext_bpf_common.h:209)
        self.op = op                # 'read' | 'write' | 'discard' | 'other'
        self.insert_flags = insert_flags
        self.domain = domain_from_op(op)


def domain_from_op(op):
    """iosched_kyber_domain_from_opf (ioext_bpf_common.h:174-191)."""
    if op == 'read':
        return DOMAIN_READ
    if op in ('write', 'write_zeroes', 'zone_append'):
        return DOMAIN_WRITE
    if op in ('discard', 'secure_erase'):
        return DOMAIN_DISCARD
    return DOMAIN_OTHER


def merge_compatible(rq_domain, bio_domain):
    """iosched_kyber_merge_compatible (ioext_policy_shared.h:1379-1383):
    same domain only.  Gated upstream by merge_cfg enable (q_kyber.bpf.c:507-512)."""
    return rq_domain == bio_domain


class KyberState:
    """struct iosched_kyber_state (ioext_policy_shared.h:318-341) + per-hctx
    init from q_kyber.bpf.c init_hctx (BPF_PROG init_hctx)."""

    def __init__(self, targets_ns=None, unlimited_depth=False):
        self.urgent = {d: [] for d in range(DOMAIN_NR)}   # urgent[4][32]
        self.domains = {d: [] for d in range(DOMAIN_NR)}  # domains[4][32]
        self.cur_domain = INVALID_INDEX   # init_hctx (bpf.c)
        self.batching = 0                 # init_hctx (bpf.c)
        self.inflight = {d: 0 for d in range(DOMAIN_NR)}
        self.depth_limit = {d: 0 for d in range(DOMAIN_NR)}
        self.avg_total_lat_ns = {d: 0 for d in range(DOMAIN_NR)}  # dead (ABI)
        self.avg_io_lat_ns = {d: 0 for d in range(DOMAIN_NR)}     # dead (ABI)
        self.async_depth = 0              # init_hctx (bpf.c)
        self.queue_async_depth = 0        # init_hctx (bpf.c)
        self.last_depth_update_ns = 0     # init_hctx (bpf.c); dead (ABI)
        self.target_lat_ns = dict(DEFAULT_TARGET_NS)
        # native law fields (h:336-341)
        self.lat_buckets = {d: {t: [0] * LATENCY_BUCKETS
                                  for t in range(LATENCY_TYPES)}
                            for d in range(SAMPLED_DOMAINS)}
        self.window_timeout_ns = {d: 0 for d in range(SAMPLED_DOMAINS)}
        self.domain_p99 = {d: P99_INVALID for d in range(SAMPLED_DOMAINS)}
        if targets_ns:                    # kyber_configure_entry (bpf.c:185-192)
            self.set_targets(**targets_ns)
        # determinism mode: depth_limit = infinity for every domain; the
        # inflight>=depth_limit gate (h:1547-1548) can never block.
        self.unlimited_depth = unlimited_depth

    # ── depth machinery (ioext_policy_shared.h) ───────────────────────────
    def set_targets(self, read=0, write=0, discard=0, other=0):
        """iosched_kyber_set_targets (h:1350-1367): 0 -> built-in default."""
        self.target_lat_ns[DOMAIN_READ] = read or DEFAULT_TARGET_NS[DOMAIN_READ]
        self.target_lat_ns[DOMAIN_WRITE] = write or DEFAULT_TARGET_NS[DOMAIN_WRITE]
        self.target_lat_ns[DOMAIN_DISCARD] = discard or DEFAULT_TARGET_NS[DOMAIN_DISCARD]
        self.target_lat_ns[DOMAIN_OTHER] = other or DEFAULT_TARGET_NS[DOMAIN_OTHER]

    def target_ns(self, domain):
        """iosched_kyber_target_ns (h:1369-1377)."""
        if domain >= DOMAIN_NR:
            return DEFAULT_TARGET_NS[DOMAIN_OTHER]
        return self.target_lat_ns[domain] or DEFAULT_TARGET_NS[domain]

    @staticmethod
    def depth_cap(async_depth):
        """iosched_kyber_depth_cap (h:1397-1400)."""
        return async_depth if async_depth else MIN_DEPTH

    @staticmethod
    def domain_depth_cap(domain):
        """iosched_kyber_domain_depth_cap (h:1402-1415): kyber_depth[]."""
        return NATIVE_DEPTH_CAP[domain]

    def init_hctx(self):
        """BPF init_hctx (q_kyber.bpf.c:414-450): fresh scheduler instance
        -> full state reset with native caps."""
        self.cur_domain = INVALID_INDEX
        self.batching = 0
        self.async_depth = 0
        self.queue_async_depth = 0
        self.last_depth_update_ns = 0
        self.avg_total_lat_ns = {d: 0 for d in range(DOMAIN_NR)}
        self.avg_io_lat_ns = {d: 0 for d in range(DOMAIN_NR)}
        for d in range(DOMAIN_NR):
            self.urgent[d] = []
            self.domains[d] = []
            self.inflight[d] = 0
            self.depth_limit[d] = self.domain_depth_cap(d)
        for d in range(SAMPLED_DOMAINS):
            for t in range(LATENCY_TYPES):
                self.lat_buckets[d][t] = [0] * LATENCY_BUCKETS
            self.window_timeout_ns[d] = 0
            self.domain_p99[d] = P99_INVALID

    def depth_updated(self, async_depth):
        """iosched_kyber_depth_updated (h:1417-1428)."""
        self.queue_async_depth = self.depth_cap(async_depth)
        if not self.async_depth or self.async_depth > self.queue_async_depth:
            self.async_depth = self.queue_async_depth
        for d in range(DOMAIN_NR):
            if not self.depth_limit[d]:
                self.depth_limit[d] = self.domain_depth_cap(d)

    def limit_depth(self, domain, async_depth):
        """iosched_kyber_limit_depth (h:1430-1455) — limit_depth hook body
        (q_kyber.bpf.c refreshes state first).  Port deviation: the feed is
        clamped to queue_async_depth; the stored depth_limit is not."""
        self.depth_updated(async_depth)
        if domain >= DOMAIN_NR:
            return self.queue_async_depth if self.queue_async_depth else MIN_DEPTH
        limit = self.depth_limit[domain] or self.domain_depth_cap(domain)
        if limit > self.queue_async_depth:
            limit = self.queue_async_depth
        return limit

    # ── native law: sampling + window-gated evaluation ──────────────────
    @staticmethod
    def bucket_for(target_ns, latency_ns):
        """iosched_kyber_bucket_for (h:1516-1531) = add_latency_sample
        (kyber-iosched.c:607-623)."""
        if latency_ns > 0:
            divisor = max(target_ns >> LATENCY_SHIFT, 1)
            return min((latency_ns - 1) // divisor, LATENCY_BUCKETS - 1)
        return 0

    def add_latency_sample(self, domain, total_lat_ns, io_lat_ns):
        """iosched_kyber_add_latency_sample (h:1533-1542)."""
        if domain >= SAMPLED_DOMAINS:
            return
        target = self.target_ns(domain)
        self.lat_buckets[domain][TOTAL_LATENCY][self.bucket_for(target, total_lat_ns)] += 1
        self.lat_buckets[domain][IO_LATENCY][self.bucket_for(target, io_lat_ns)] += 1

    def calculate_percentile(self, domain, type_, percentile, now_ns):
        """iosched_kyber_calculate_percentile (h:1544-1583) =
        calculate_percentile (kyber-iosched.c:219-257)."""
        if domain >= SAMPLED_DOMAINS or type_ >= LATENCY_TYPES:
            return P99_INVALID
        buckets = self.lat_buckets[domain][type_]
        samples = sum(buckets)
        if not samples:
            return P99_INVALID
        if not self.window_timeout_ns[domain]:
            self.window_timeout_ns[domain] = now_ns + WINDOW_TIMEOUT_NS
        if samples < MIN_PCT_SAMPLES and now_ns < self.window_timeout_ns[domain]:
            return P99_INVALID
        self.window_timeout_ns[domain] = 0
        pct_samples = (samples * percentile + 99) // 100
        bucket = LATENCY_BUCKETS - 1
        for b in range(LATENCY_BUCKETS - 1):
            if buckets[b] >= pct_samples:
                bucket = b
                break
            pct_samples -= buckets[b]
        self.lat_buckets[domain][type_] = [0] * LATENCY_BUCKETS
        return bucket

    def resize_domain(self, domain, depth):
        """iosched_kyber_resize_domain (h:1573-1590) =
        kyber_resize_domain (kyber-iosched.c:259-268)."""
        if domain >= DOMAIN_NR:
            return
        self.depth_limit[domain] = min(max(depth, MIN_DEPTH),
                                       self.domain_depth_cap(domain))

    def update_depths(self, now_ns):
        """iosched_kyber_update_depths (h:1595-1646) = kyber_timer_fn
        (kyber-iosched.c:270-348)."""
        bad = False
        for d in range(SAMPLED_DOMAINS):
            p90 = self.calculate_percentile(d, IO_LATENCY, PCT_P90, now_ns)
            if p90 >= GOOD_BUCKETS:
                bad = True
        for d in range(SAMPLED_DOMAINS):
            p99 = self.calculate_percentile(d, TOTAL_LATENCY, PCT_P99, now_ns)
            if bad:
                if p99 < 0:
                    p99 = self.domain_p99[d]
                self.domain_p99[d] = P99_INVALID
            elif p99 >= 0:
                self.domain_p99[d] = p99
            if p99 < 0:
                continue
            if bad or p99 >= GOOD_BUCKETS:
                orig = self.depth_limit[d] or self.domain_depth_cap(d)
                self.resize_domain(d, (orig * (p99 + 1)) >> LATENCY_SHIFT)

    # ── insert / release / feedback hooks (ioext_policy_shared.h) ────────
    def insert(self, req):
        """iosched_kyber_insert (h:1652-1666) via insert_request hook
        (q_kyber.bpf.c).  Returns True on accept; False when the
        per-domain FIFO is full (push_tail_32 h:417-418 -> hook returns -1)."""
        q = self.urgent[req.domain] if (req.insert_flags & 0x1) else self.domains[req.domain]
        if len(q) >= MAX_PENDING:
            return False
        q.append(req)
        return True

    def release(self, domain):
        """iosched_kyber_release (h:1462-1467): inflight--, floor 0."""
        if domain < DOMAIN_NR and self.inflight[domain]:
            self.inflight[domain] -= 1

    def finish_request(self, req):
        """finish_request hook (q_kyber.bpf.c -> h:1475-1479)."""
        self.release(req.domain)

    def requeue_request(self, req):
        """requeue_request hook (q_kyber.bpf.c -> h:1469-1473):
        refund inflight only; the framework re-inserts with insert_flags&0x1
        which routes the request to the URGENT fifo (h:1581-1584)."""
        self.release(req.domain)

    def complete_feedback(self, domain, async_depth, now_ns, total_lat_ns, io_lat_ns):
        """iosched_kyber_complete_feedback (h:1648-1668) — completed_request
        hook body (q_kyber.bpf.c).  OTHER is never sampled; every other
        completion adds one total + one I/O sample and re-evaluates."""
        self.depth_updated(async_depth)
        if domain >= SAMPLED_DOMAINS:
            return
        self.add_latency_sample(domain, total_lat_ns, io_lat_ns)
        self.update_depths(now_ns)

    # ── dispatch core (ioext_policy_shared.h) ────────────────────────────
    def domain_can_dispatch(self, domain):
        """iosched_kyber_domain_can_dispatch (h:1538-1549)."""
        if domain >= DOMAIN_NR:
            return False
        if not self.domains[domain] and not self.urgent[domain]:
            return False
        if self.unlimited_depth:
            return True
        limit = self.depth_limit[domain] if self.depth_limit[domain] else MIN_DEPTH
        return self.inflight[domain] < limit

    def note_dispatch(self, domain, rq_ptr):
        """iosched_kyber_note_dispatch (h:1551-1557): inflight++."""
        if rq_ptr and domain < DOMAIN_NR:
            self.inflight[domain] += 1

    @staticmethod
    def pop_head(q):
        """iosched_pop_head_32 (h:441-459) — returns None when empty."""
        return q.pop(0) if q else None

    def find_next_domain(self, start_domain):
        """iosched_kyber_find_next_domain (h:1559-1572)."""
        for step in range(DOMAIN_NR):
            d = (start_domain + step) % DOMAIN_NR
            if self.domain_can_dispatch(d):
                return d
        return INVALID_INDEX

    def dispatch(self):
        """iosched_kyber_dispatch (h:1590-1638) — cgroup_dispatch hook
        (q_kyber.bpf.c:726-733).  Returns the dispatched RequestDesc or None."""
        domain = self.cur_domain
        if self.domain_can_dispatch(domain):
            if self.urgent[domain]:
                self.batching = 1
                rq = self.pop_head(self.urgent[domain])
                self.note_dispatch(domain, rq.rq_ptr if rq else 0)
                return rq
            if self.batching < BATCH_LIMIT[domain]:
                self.batching += 1
                rq = self.pop_head(self.domains[domain])
                self.note_dispatch(domain, rq.rq_ptr if rq else 0)
                return rq
            domain = (domain + 1) % DOMAIN_NR
        else:
            if domain < DOMAIN_NR:
                domain = (domain + 1) % DOMAIN_NR
            else:
                domain = DOMAIN_READ
        domain = self.find_next_domain(domain)
        if domain == INVALID_INDEX:
            self.cur_domain = INVALID_INDEX
            self.batching = 0
            return None
        self.cur_domain = domain
        self.batching = 1
        if self.urgent[domain]:
            rq = self.pop_head(self.urgent[domain])
            self.note_dispatch(domain, rq.rq_ptr if rq else 0)
            return rq
        rq = self.pop_head(self.domains[domain])
        self.note_dispatch(domain, rq.rq_ptr if rq else 0)
        return rq

    def evacuate(self):
        """iosched_kyber_evacuate (h:1640-1671) — evacuate hook
        (q_kyber.bpf.c:735-742): urgent-first scan ignoring depth limits."""
        start = self.cur_domain if self.cur_domain < DOMAIN_NR else DOMAIN_READ
        for step in range(DOMAIN_NR):
            d = (start + step) % DOMAIN_NR
            if self.urgent[d]:
                self.cur_domain = d
                self.batching = 0
                return self.pop_head(self.urgent[d])
        for step in range(DOMAIN_NR):
            d = (start + step) % DOMAIN_NR
            if self.domains[d]:
                self.cur_domain = d
                self.batching = 0
                return self.pop_head(self.domains[d])
        self.cur_domain = INVALID_INDEX
        self.batching = 0
        return None


# ── trace-driven drivers ─────────────────────────────────────────────────
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DT_DIR = os.path.join(REPO, 'tests', 'io', 'full_suite', 'deterministic')


def load_trace(name):
    return json.load(open(os.path.join(DT_DIR, 'traces', 'micro', name)))


def load_expect(relpath):
    return json.load(open(os.path.join(DT_DIR, relpath)))


def requests_from_trace(trace):
    """Build model RequestDesc list in arrival order (trace_driver submits
    the requests array in order inside one async batch)."""
    out = []
    for i, r in enumerate(trace['requests']):
        out.append(RequestDesc(rq_ptr=0x1000 + i, sector=r['sector'],
                                nr_sectors=r.get('nr_sectors', 8), op=r['op']))
    return out


def derive_issue_order(reqs, async_depth=None):
    """Insert every request in arrival order (one async batch -> one plug
    flush: all inserts happen before the dispatch run), then dispatch to
    exhaustion, finishing each request immediately after it is issued.
    async_depth=None selects determinism mode (depth_limit=inf)."""
    st = KyberState(unlimited_depth=True) if async_depth is None else KyberState()
    if async_depth is not None:
        st.depth_updated(async_depth)   # first limit_depth refresh (bpf.c:539)
    for r in reqs:
        assert st.insert(r), 'model insert rejected (FIFO full)'
    issued = []
    while True:
        rq = st.dispatch()
        if rq is None:
            break
        issued.append(rq)
        st.finish_request(rq)
    return [r.sector for r in issued], st


def plug_merge(reqs):
    """Emulate the block-layer per-task plug merge (kernel/linux-7.3.1/block/
    blk-merge.c blk_attempt_plug_merge L1086-1115): a new bio is merged ONLY
    against the plug TAIL request (single-queue case).  Plug merges bypass
    both the allow_merge and bio_merge elevator hooks — discovered
    empirically 2026-09-01: mqdd M02 merged under D0 deny-all, and q_kyber
    with --merge-enable=0/--merge-enable=1 shows identical merge behavior
    (none when submission interleaves ops, tail-merges when same-op runs
    are contiguous).  Returns post-merge RequestDesc list."""
    tail = None
    merged = []
    for r in reqs:
        placed = False
        if tail is not None and tail.op == r.op:
            if tail.sector + tail.cost == r.sector:      # back merge
                tail.cost += r.cost
                placed = True
            elif r.sector + r.cost == tail.sector:       # front merge
                tail.sector = r.sector
                tail.cost += r.cost
                placed = True
        if not placed:
            tail = RequestDesc(r.rq_ptr, r.sector, r.cost, r.op)
            merged.append(tail)
    return merged


# ── self-tests ───────────────────────────────────────────────────────────
def check(name, cond, detail=""):
    if cond:
        print(f"PASS: {name}")
        return True
    print(f"FAIL: {name} {detail}")
    return False


def test_k1_fifo_matches_golden():
    """K1: 8 descending single-domain reads -> ISSUE == arrival FIFO order;
    must equal expect/golden/kyber_read_fifo_8.json (the golden's oracle)."""
    trace = load_trace('kyber_read_fifo_8.json')
    expect = load_expect('expect/golden/kyber_read_fifo_8.json')
    golden = [e['sector'] for e in expect['expected_dispatch_order']]
    arrival = [r['sector'] for r in trace['requests']]
    ok = check("k1_arrival_descending", arrival == sorted(arrival, reverse=True),
               f"arrival={arrival}")
    for mode, depth in (('determinism', None), ('finite_depth_1023', 1023)):
        order, _ = derive_issue_order(requests_from_trace(trace), depth)
        ok &= check(f"k1_fifo_issue_order_{mode}", order == golden,
                    f"derived={order} golden={golden}")
    ok &= check("k1_not_clook_ascending", golden != sorted(golden),
                f"golden={golden} sorted={sorted(golden)}")
    return ok


def test_k2_domain_rr_matches_partial():
    """K2: interleaved 5R+3W -> all reads (arrival FIFO) then all writes
    (arrival FIFO); must satisfy expect/partial/kyber_rw_domains_8.json."""
    trace = load_trace('kyber_rw_domains_8.json')
    expect = load_expect('expect/partial/kyber_rw_domains_8.json')
    golden = [e['sector'] for e in expect['expected_dispatch_order']]
    order, _ = derive_issue_order(requests_from_trace(trace), 1023)
    reads = [s for s in order if s < 8600]
    writes = [s for s in order if s >= 8600]
    ok = check("k2_derived_order", order == golden,
               f"derived={order} golden={golden}")
    ok &= check("k2_reads_before_writes", order == reads + writes,
                f"order={order}")
    ok &= check("k2_read_arrival_fifo",
                reads == [r['sector'] for r in trace['requests'] if r['op'] == 'read'])
    ok &= check("k2_write_arrival_fifo",
                writes == [r['sector'] for r in trace['requests'] if r['op'] == 'write'])
    return ok


def test_k3_merge_coverage_model():
    """K3: contiguous same-op adjacent bios plug-merge (tail-only) before
    kyber sees them; the model predicts 2 issues covering 64 sectors
    (merge_cov invariants intent)."""
    trace = load_trace('kyber_adjacent_merge_8.json')
    reqs = requests_from_trace(trace)
    post = plug_merge(reqs)
    order, _ = derive_issue_order(post, 1023)
    coverage = sum(r.cost for r in post)
    total = sum(r['nr_sectors'] for r in trace['requests'])
    ok = check("k3_plug_merge_count", len(post) == 2,
               f"post-merge requests={len(post)} (expected 2: 1 read + 1 write)")
    ok &= check("k3_issue_coverage", coverage == total == 64,
                f"coverage={coverage} total={total}")
    ok &= check("k3_read_domain_dispatches_first", order[:1] == [12288],
                f"order={order} (READ domain round-robin starts before WRITE)")
    return ok


def test_plug_merge_tail_only_interleave():
    """Kernel plug merges are TAIL-ONLY (blk-merge.c:1095-1099): interleaving
    a different op between adjacent bios defeats merging entirely — the
    behavior discovered live in the first KYBER_K3 run (2026-09-01, 8
    unmerged 4K issues) and fixed by contiguous same-op runs."""
    reqs = [RequestDesc(0x1000, 12288, 8, 'read'), RequestDesc(0x1001, 12544, 8, 'write'),
            RequestDesc(0x1002, 12296, 8, 'read'), RequestDesc(0x1003, 12552, 8, 'write'),
            RequestDesc(0x1004, 12304, 8, 'read'), RequestDesc(0x1005, 12560, 8, 'write'),
            RequestDesc(0x1006, 12312, 8, 'read'), RequestDesc(0x1007, 12568, 8, 'write')]
    post = plug_merge(reqs)
    ok = check("plug_tail_only_interleaved_no_merge", len(post) == 8,
               f"interleaved adjacent bios merged to {len(post)} requests (expected 8, no merge)")
    desc = [RequestDesc(0x2000 + i, 32 - 8 * i, 8, 'read') for i in range(5)]
    post2 = plug_merge(desc)
    ok &= check("plug_tail_front_merge_descending", len(post2) == 1,
                f"descending adjacent reads merged to {len(post2)} request (mqdd M02 nr=40 evidence)")
    ok &= check("plug_tail_front_merge_range",
                post2[0].sector == 0 and post2[0].cost == 40,
                f"merged range [{post2[0].sector},{post2[0].sector + post2[0].cost})")
    return ok


def test_batch_limit_reset():
    """READ batch limit 16: after 16 READ dispatches the domain rotates; with
    WRITE/DISCARD/OTHER empty the scan wraps back to READ and batching resets
    (h:1603-1611,1619-1627) -> a 20-read backlog is still pure FIFO."""
    st = KyberState(unlimited_depth=True)
    for i in range(20):
        st.insert(RequestDesc(i + 1, 1000 + 64 * i, 8, "read"))
    order = []
    while True:
        rq = st.dispatch()
        if rq is None:
            break
        order.append(rq.sector)
        st.finish_request(rq)
    return check("batch_limit_reset_pure_fifo",
                 order == [1000 + 64 * i for i in range(20)],
                 f"order={order}")


def test_cur_domain_rr_rotation():
    """Round-robin across domains: with READ, WRITE, OTHER backlogged, the
    cursor drains READ then WRITE then OTHER (each domain empty triggers the
    advance h:1613-1616)."""
    st = KyberState(unlimited_depth=True)
    st.insert(RequestDesc(1, 100, 8, 'read'))
    st.insert(RequestDesc(2, 200, 8, 'read'))
    st.insert(RequestDesc(3, 5000, 8, 'write'))
    st.insert(RequestDesc(4, 5100, 8, 'write'))
    st.insert(RequestDesc(5, 90000, 8, 'other'))
    st.insert(RequestDesc(6, 90064, 8, 'other'))
    order = []
    while True:
        rq = st.dispatch()
        if rq is None:
            break
        order.append((rq.op, rq.sector))
        st.finish_request(rq)
    expect = [('read', 100), ('read', 200), ('write', 5000), ('write', 5100),
              ('other', 90000), ('other', 90064)]
    return check("cur_domain_rr_rotation", order == expect, f"order={order}")


def test_urgent_fifo_first():
    """insert_flags&0x1 (requeue path) routes to the urgent FIFO which is
    drained before the domain FIFO (h:1596-1602,1628-1633)."""
    st = KyberState(unlimited_depth=True)
    st.insert(RequestDesc(1, 100, 8, 'read'))
    st.insert(RequestDesc(2, 200, 8, 'read'))
    st.insert(RequestDesc(3, 300, 8, 'read', insert_flags=1))  # urgent
    st.insert(RequestDesc(4, 400, 8, 'read', insert_flags=1))  # urgent
    order = []
    while True:
        rq = st.dispatch()
        if rq is None:
            break
        order.append(rq.sector)
        st.finish_request(rq)
    return check("urgent_fifo_first", order == [300, 400, 100, 200],
                 f"order={order}")


def test_native_initial_caps_and_gate():
    """init_hctx seeds depth_limit with the fixed kyber_depth[] caps
    {256,128,64,16} — NOT a function of queue depth; the dispatch gate
    blocks at inflight == depth_limit."""
    st = KyberState()
    st.init_hctx()
    ok = check("native_initial_caps",
               [st.depth_limit[d] for d in range(DOMAIN_NR)] == [256, 128, 64, 16],
               f"depth_limit={[st.depth_limit[d] for d in range(DOMAIN_NR)]}")
    st.depth_updated(1023)      # queue async depth must not clamp stored caps
    ok &= check("native_caps_ignore_queue_depth",
                st.depth_limit[DOMAIN_READ] == 256,
                f"depth_limit[R]={st.depth_limit[DOMAIN_READ]}")
    ok &= check("native_limit_depth_feed_clamped",
                st.limit_depth(DOMAIN_READ, 1023) == 256,
                f"limit_depth(R)={st.limit_depth(DOMAIN_READ, 1023)}")
    st.limit_depth(DOMAIN_READ, 64)          # tiny queue: feed clamped, state not
    ok &= check("native_feed_clamp_only",
                st.limit_depth(DOMAIN_READ, 64) == 64 and st.depth_limit[DOMAIN_READ] == 256,
                f"feed={st.limit_depth(DOMAIN_READ, 64)} stored={st.depth_limit[DOMAIN_READ]}")
    st.depth_limit[DOMAIN_READ] = 2          # shrink the gate for a blocking check
    for i in range(3):
        st.insert(RequestDesc(i + 1, 1000 + 64 * i, 8, "read"))
    issued = []
    while True:
        rq = st.dispatch()
        if rq is None:
            break
        issued.append(rq)
    ok &= check("native_gate_blocks_at_limit", len(issued) == 2,
                f"dispatched={len(issued)} (blocked at inflight==depth_limit==2)")
    return ok


def test_native_percentile_math():
    """bucket_for edges (kyber-iosched.c:607-623) and the percentile scan
    with DIV_ROUND_UP(samples*pct,100) (kyber-iosched.c:244-249)."""
    ok = check("bucket_zero_latency", KyberState.bucket_for(1000, 0) == 0)
    ok &= check("bucket_quarter_target", KyberState.bucket_for(1000, 250) == 0)
    ok &= check("bucket_above_quarter", KyberState.bucket_for(1000, 251) == 1)
    ok &= check("bucket_at_target", KyberState.bucket_for(1000, 1000) == 3)
    ok &= check("bucket_clamped", KyberState.bucket_for(1000, 10**9) == 7)
    ok &= check("bucket_zero_divisor_target", KyberState.bucket_for(0, 5) == 4,
                f"target 0 -> divisor 1 -> (5-1)/1=4")
    st = KyberState()
    st.init_hctx()
    st.lat_buckets[DOMAIN_READ][TOTAL_LATENCY] = [500, 0, 0, 0, 0, 0, 0, 0]
    p = st.calculate_percentile(DOMAIN_READ, TOTAL_LATENCY, 99, 10**15)
    ok &= check("pct_ceil_all_low", p == 0, f"p99 of 500x bucket0 = {p}")
    st.lat_buckets[DOMAIN_READ][TOTAL_LATENCY] = [0, 0, 0, 0, 0, 0, 0, 500]
    p = st.calculate_percentile(DOMAIN_READ, TOTAL_LATENCY, 90, 10**15)
    ok &= check("pct_all_high", p == 7, f"p90 of 500x bucket7 = {p}")
    st.lat_buckets[DOMAIN_READ][TOTAL_LATENCY] = [50, 0, 0, 0, 0, 0, 0, 450]
    p = st.calculate_percentile(DOMAIN_READ, TOTAL_LATENCY, 90, 10**15)
    ok &= check("pct_scan_crosses_bucket", p == 7,
                f"p90 of 50xb0+450xb7 = {p} (ceil(500*90/100)=450 > 50 in b0)")
    st.lat_buckets[DOMAIN_READ][TOTAL_LATENCY] = [451, 0, 0, 0, 0, 0, 0, 49]
    p = st.calculate_percentile(DOMAIN_READ, TOTAL_LATENCY, 90, 10**15)
    ok &= check("pct_scan_stays_low", p == 0,
                f"p90 of 451xb0+49xb7 = {p} (450 <= 451)")
    ok &= check("pct_fire_clears_buckets",
                st.lat_buckets[DOMAIN_READ][TOTAL_LATENCY] == [0] * 8)
    return ok


def test_native_grow_toward_cap():
    """Tiny target -> every sample lands bucket 7 -> p90/p99 = 7 -> resize
    x2 per window until the kyber_depth[] cap, then clamps (kyber-iosched.c
    :342-345: orig = CURRENT token depth)."""
    st = KyberState()
    st.init_hctx()
    st.set_targets(read=1000)               # 250ns buckets; all real IO ~us
    st.depth_limit[DOMAIN_READ] = 16        # start shrunken
    now = 10**6
    for window in range(4):                 # 16 -> 32 -> 64 -> 128 -> 256
        for i in range(MIN_PCT_SAMPLES):
            now += 100
            st.complete_feedback(DOMAIN_READ, 1023, now, 100000, 100000)
        expect = min(16 * (2 ** (window + 1)), NATIVE_DEPTH_CAP[DOMAIN_READ])
        if not check(f"grow_window_{window}", st.depth_limit[DOMAIN_READ] == expect,
                     f"depth_limit[R]={st.depth_limit[DOMAIN_READ]} expected={expect}"):
            return False
    for i in range(MIN_PCT_SAMPLES):       # one more window: clamped at cap
        now += 100
        st.complete_feedback(DOMAIN_READ, 1023, now, 100000, 100000)
    ok = check("grow_clamped_at_cap",
               st.depth_limit[DOMAIN_READ] == NATIVE_DEPTH_CAP[DOMAIN_READ],
               f"depth_limit[R]={st.depth_limit[DOMAIN_READ]}")
    ok &= check("grow_async_depth_not_frozen",
                st.async_depth == st.depth_cap(1023),
                f"async_depth={st.async_depth} (update_async_depth freeze removed)")
    return ok


def test_native_shrink_under_congestion():
    """Good-latency READ (huge target, bucket 0) under congestion raised by
    bad-latency WRITE (tiny target, bucket 7): read resizes
    256*(0+1)>>2 = 64 per window down to the floor; write grows x2 and
    clamps at its cap."""
    st = KyberState()
    st.init_hctx()
    st.set_targets(read=10**14, write=1000)
    now = 10**6
    for i in range(MIN_PCT_SAMPLES):       # both windows fire same-pass
        now += 100
        st.complete_feedback(DOMAIN_READ, 1023, now, 1000, 1000)
        st.complete_feedback(DOMAIN_WRITE, 1023, now, 100000, 100000)
    ok = check("shrink_read_one_window", st.depth_limit[DOMAIN_READ] == 64,
               f"depth_limit[R]={st.depth_limit[DOMAIN_READ]}")
    ok &= check("shrink_write_clamped", st.depth_limit[DOMAIN_WRITE] == 128,
                f"depth_limit[W]={st.depth_limit[DOMAIN_WRITE]} (128x2 clamped at cap)")
    ok &= check("shrink_discard_untouched", st.depth_limit[DOMAIN_DISCARD] == 64,
                f"depth_limit[D]={st.depth_limit[DOMAIN_DISCARD]} (no samples)")
    for i in range(MIN_PCT_SAMPLES):       # second window: 64 -> 16
        now += 100
        st.complete_feedback(DOMAIN_READ, 1023, now, 1000, 1000)
        st.complete_feedback(DOMAIN_WRITE, 1023, now, 100000, 100000)
    ok &= check("shrink_read_two_windows", st.depth_limit[DOMAIN_READ] == 16,
                f"depth_limit[R]={st.depth_limit[DOMAIN_READ]}")
    for _ in range(3):                     # decay to the floor
        for i in range(MIN_PCT_SAMPLES):
            now += 100
            st.complete_feedback(DOMAIN_READ, 1023, now, 1000, 1000)
            st.complete_feedback(DOMAIN_WRITE, 1023, now, 100000, 100000)
    ok &= check("shrink_read_floor", st.depth_limit[DOMAIN_READ] == MIN_DEPTH,
                f"depth_limit[R]={st.depth_limit[DOMAIN_READ]}")
    return ok


def test_native_hold_no_congestion():
    """All-huge targets, no congestion: windows fire but bad=false and
    p99<4 -> HOLD, depth_limit stays at the initial caps forever."""
    st = KyberState()
    st.init_hctx()
    st.set_targets(read=10**14, write=10**14)
    now = 10**6
    for _ in range(3):
        for i in range(MIN_PCT_SAMPLES):
            now += 100
            st.complete_feedback(DOMAIN_READ, 1023, now, 1000, 1000)
            st.complete_feedback(DOMAIN_WRITE, 1023, now, 1000, 1000)
    ok = check("hold_caps_untouched",
               [st.depth_limit[d] for d in range(DOMAIN_NR)] == [256, 128, 64, 16],
               f"depth_limit={[st.depth_limit[d] for d in range(DOMAIN_NR)]}")
    ok &= check("hold_p99_remembered", st.domain_p99[DOMAIN_READ] == 0,
                f"domain_p99[R]={st.domain_p99[DOMAIN_READ]} (fired clean, remembered)")
    return ok


def test_native_window_gate():
    """Window rule (kyber-iosched.c:232-242): no evaluation below 500
    samples before the 1s anchor; fires at 500 samples; fires on the 1s
    timeout even with fewer samples."""
    st = KyberState()
    st.init_hctx()
    st.set_targets(read=1000)
    now = 10**6
    for i in range(MIN_PCT_SAMPLES - 1):   # 499 samples, well under 1s
        now += 100
        st.complete_feedback(DOMAIN_READ, 1023, now, 100000, 100000)
    ok = check("window_immature_holds",
               st.depth_limit[DOMAIN_READ] == 256 and st.window_timeout_ns[DOMAIN_READ] > 0,
               f"depth_limit[R]={st.depth_limit[DOMAIN_READ]} "
               f"anchor={st.window_timeout_ns[DOMAIN_READ]}")
    now += 100                              # sample #500 -> window fires
    st.complete_feedback(DOMAIN_READ, 1023, now, 100000, 100000)
    ok &= check("window_500_fires", st.depth_limit[DOMAIN_READ] == 512 // 2,
                f"depth_limit[R]={st.depth_limit[DOMAIN_READ]} (256x2 clamped at 256 cap)"
                if st.depth_limit[DOMAIN_READ] == 256 else
                f"depth_limit[R]={st.depth_limit[DOMAIN_READ]}")
    ok &= check("window_500_expected", st.depth_limit[DOMAIN_READ] == 256,
                f"depth_limit[R]={st.depth_limit[DOMAIN_READ]} (p99=7 -> 256x2 -> clamp 256)")
    st2 = KyberState()
    st2.init_hctx()
    st2.set_targets(read=1000)
    now = 10**6
    for i in range(100):                    # 100 samples, then cross the anchor
        now += 1000
        st2.complete_feedback(DOMAIN_READ, 1023, now, 100000, 100000)
    ok &= check("window_timeout_immature",
                st2.depth_limit[DOMAIN_READ] == 256,
                f"depth_limit[R]={st2.depth_limit[DOMAIN_READ]} (100 samples < 1s)")
    now = st2.window_timeout_ns[DOMAIN_READ] + 1   # first eval attempt past anchor
    st2.complete_feedback(DOMAIN_READ, 1023, now, 100000, 100000)
    ok &= check("window_timeout_fires", st2.depth_limit[DOMAIN_READ] == 256,
                f"depth_limit[R]={st2.depth_limit[DOMAIN_READ]} (p99=7 -> 256x2 -> clamp 256)")
    return ok


def test_native_other_domain_excluded():
    """OTHER completions are never sampled (kyber-iosched.c:632-634) and
    never trigger evaluation: its depth stays at the cap regardless of
    latency."""
    st = KyberState()
    st.init_hctx()
    st.set_targets(other=1000)
    now = 10**6
    for i in range(3 * MIN_PCT_SAMPLES):
        now += 100
        st.complete_feedback(DOMAIN_OTHER, 1023, now, 100000, 100000)
    ok = check("other_never_sampled",
               all(sum(st.lat_buckets[d][t]) == 0
                   for d in range(SAMPLED_DOMAINS) for t in range(LATENCY_TYPES)),
               "OTHER completions leaked into sampled histograms")
    ok &= check("other_depth_untouched", st.depth_limit[DOMAIN_OTHER] == 16,
                f"depth_limit[O]={st.depth_limit[DOMAIN_OTHER]}")
    return ok


def test_native_p99_memory_cross_window():
    """The remembered-p99 mechanism (kyber-iosched.c:315-331): the READ
    window fires clean (bad=false) and remembers p99=0; the WRITE window
    fires later with bad=true and shrinks READ using the remembered p99 —
    the exact cross-window desync the KYBER_DEPTH_SHRINK rig case drives."""
    st = KyberState()
    st.init_hctx()
    st.set_targets(read=10**14, write=1000)
    now = 10**6
    for i in range(MIN_PCT_SAMPLES):       # read window fires, write immature
        now += 100
        st.complete_feedback(DOMAIN_READ, 1023, now, 1000, 1000)
    ok = check("p99_remembered_clean",
               st.domain_p99[DOMAIN_READ] == 0 and st.depth_limit[DOMAIN_READ] == 256,
               f"p99[R]={st.domain_p99[DOMAIN_READ]} depth_limit[R]={st.depth_limit[DOMAIN_READ]}")
    for i in range(MIN_PCT_SAMPLES):       # write window fires with bad=true
        now += 100
        st.complete_feedback(DOMAIN_WRITE, 1023, now, 100000, 100000)
    ok &= check("p99_memory_shrinks_read", st.depth_limit[DOMAIN_READ] == 64,
                f"depth_limit[R]={st.depth_limit[DOMAIN_READ]} (256x(0+1)>>2)")
    ok &= check("p99_memory_consumed", st.domain_p99[DOMAIN_READ] == P99_INVALID,
                f"domain_p99[R]={st.domain_p99[DOMAIN_READ]} (reset to -1 on use)")
    return ok


def test_depth_unlimited_mode():
    """Determinism mode: depth_limit=inf — a 32-read backlog dispatches in
    one round regardless of inflight accounting."""
    st = KyberState(unlimited_depth=True)
    for i in range(32):
        st.insert(RequestDesc(i + 1, 1000 + 64 * i, 8, "read"))
    n = 0
    while st.dispatch() is not None:
        n += 1
    return check("unlimited_depth_full_drain", n == 32, f"dispatched={n}")


def test_evacuate_ignores_depth():
    """evacuate (h:1640-1671) drains urgent-then-domain queues ignoring
    inflight/depth_limit gates (drain path at loader exit)."""
    st = KyberState()
    st.init_hctx()
    st.depth_limit[DOMAIN_READ] = 3         # shrink the gate below the backlog
    for i in range(8):
        st.insert(RequestDesc(i + 1, 1000 + 64 * i, 8, "read"))
    dispatched = []
    while True:
        rq = st.dispatch()
        if rq is None:
            break
        dispatched.append(rq.sector)
    ok = check("evacuate_depth_gate_blocks_dispatch",
               len(dispatched) == 3,
               f"dispatched={len(dispatched)} (blocked at inflight==depth_limit==3)")
    ev = []
    while True:
        rq = st.evacuate()
        if rq is None:
            break
        ev.append(rq.sector)
    ok &= check("evacuate_drains_despite_depth_gate",
                ev == [1000 + 64 * i for i in range(3, 8)],
                f"dispatched={len(dispatched)} evacuated={ev}")
    return ok


def test_merge_same_domain_only():
    ok = check("merge_same_domain", merge_compatible(DOMAIN_READ, DOMAIN_READ))
    ok &= check("merge_cross_domain_denied",
                not merge_compatible(DOMAIN_READ, DOMAIN_WRITE) and
                not merge_compatible(DOMAIN_WRITE, DOMAIN_READ) and
                not merge_compatible(DOMAIN_READ, DOMAIN_OTHER))
    return ok


def test_target_zero_fallback():
    st = KyberState()
    st.set_targets(read=0, write=0, discard=0, other=0)
    ok = check("target_zero_fallback",
                st.target_ns(DOMAIN_READ) == DEFAULT_TARGET_NS[DOMAIN_READ] and
                st.target_ns(DOMAIN_WRITE) == DEFAULT_TARGET_NS[DOMAIN_WRITE] and
                st.target_ns(DOMAIN_DISCARD) == DEFAULT_TARGET_NS[DOMAIN_DISCARD] and
                st.target_ns(DOMAIN_OTHER) == DEFAULT_TARGET_NS[DOMAIN_OTHER])
    huge = 100000000000000
    st.set_targets(read=huge, write=huge)
    ok &= check("target_huge_neutralizes_shrink",
                st.target_ns(DOMAIN_READ) == huge and
                st.target_ns(DOMAIN_WRITE) == huge,
                "huge targets make avg>target unreachable -> depth only grows")
    return ok


def test_fifo_capacity_32():
    st = KyberState(unlimited_depth=True)
    ok = True
    for i in range(32):
        ok &= st.insert(RequestDesc(i + 1, 1000 + 64 * i, 8, "read"))
    ok = check("fifo_capacity_32_accepts", ok)
    rejected = not st.insert(RequestDesc(99, 100000, 8, 'read'))
    return check("fifo_capacity_33_rejects", rejected) and ok


def test_requeue_refund_and_urgent_reroute():
    st = KyberState(unlimited_depth=True)
    st.insert(RequestDesc(1, 100, 8, 'read'))
    rq = st.dispatch()
    if rq is None:
        return check("requeue_dispatch", False, "dispatch returned None")
    ok = check("requeue_inflight_charged", st.inflight[DOMAIN_READ] == 1,
               f"inflight={st.inflight[DOMAIN_READ]}")
    st.requeue_request(rq)                       # inflight refund (h:1469-1473)
    ok &= check("requeue_inflight_refunded", st.inflight[DOMAIN_READ] == 0,
                f"inflight={st.inflight[DOMAIN_READ]}")
    st.insert(RequestDesc(rq.rq_ptr, rq.sector, rq.cost, 'read', insert_flags=1))
    again = st.dispatch()
    ok &= check("requeue_reroutes_urgent", again is not None and again.sector == 100,
                "requeued request must dispatch from the urgent fifo")
    return ok


def main():
    ok = True
    ok &= test_k1_fifo_matches_golden()
    ok &= test_k2_domain_rr_matches_partial()
    ok &= test_k3_merge_coverage_model()
    ok &= test_plug_merge_tail_only_interleave()
    ok &= test_batch_limit_reset()
    ok &= test_cur_domain_rr_rotation()
    ok &= test_urgent_fifo_first()
    ok &= test_native_initial_caps_and_gate()
    ok &= test_native_percentile_math()
    ok &= test_native_grow_toward_cap()
    ok &= test_native_shrink_under_congestion()
    ok &= test_native_hold_no_congestion()
    ok &= test_native_window_gate()
    ok &= test_native_other_domain_excluded()
    ok &= test_native_p99_memory_cross_window()
    ok &= test_depth_unlimited_mode()
    ok &= test_evacuate_ignores_depth()
    ok &= test_merge_same_domain_only()
    ok &= test_target_zero_fallback()
    ok &= test_fifo_capacity_32()
    ok &= test_requeue_refund_and_urgent_reroute()
    print("MODEL " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()