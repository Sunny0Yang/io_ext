/* ioext_arb_common.h - Shared infrastructure for WDRR/SLO/cost arbiters
 *
 * Dispatch-time charged DRR core. The arbiter maintains its own pending
 * and inflight state via framework hooks (inserted_request / dispatched_request
 * / completed_request / requeue_refund), independent of local policy
 * policy_meta.queue_depth.
 *
 * Charge model:
 *   WDRR:  charge = bytes (dispatch-time)
 *   COST:  charge = estimated_cost(op, size) (dispatch-time)
 *   SLO:   no charge; uses boost as effective_weight multiplier
 *
 * SLO boost is per-tenant: effective_weight = base_weight * tenant_boost.
 * The global max_boost pattern is forbidden.
 *
 * ---------------------------------------------------------------------------
 * Key contract (identity vs decision keys):
 *
 *   pending and inflight are REQUEST-IDENTITY counters. They must be keyed
 *   by the hctx the request was allocated on (rq->mq_hctx->queue_num), the
 *   same key inserted_request fires with, so that insert (pending++) pairs
 *   exactly with dispatch (pending--/inflight++) and completion/requeue
 *   (inflight--) regardless of which hctx's dispatch loop pops the request
 *   out of a per-cgroup shared local-policy queue. The kernel passes this
 *   key in iosched_dispatched_ctx.home_hctx_idx.
 *
 *   deficit is a DECISION counter: it is charged on the key whose pick
 *   granted the turn (the dispatching hctx, iosched_dispatched_ctx.hctx_idx)
 *   and refunded by requeue_refund on the same key (kernel-stashed in
 *   elv.priv[1]). Charging deficit on the identity key instead lets a
 *   granting key's deficit never exhaust and starves that hctx's other
 *   tenants; charging pending on the decision key instead leaks phantom
 *   pending whenever a shared queue is popped cross-hctx and dead-locks
 *   strict dispatch (cursor pinned on a tenant whose queue is empty).
 * ---------------------------------------------------------------------------
 * DRR contract (mirrored by the reference model in tests/io/arb_wdrr_model.py;
 * S12: the decision state is CGROUP-LEVEL — one deficit / refilled_round
 * per cgroup, ONE global round counter shared by every hctx — because on
 * multi-hctx devices the per-(cg,hctx) round barriers are disjoint when
 * each tenant's requests home to a single hardware queue):
 *
 *   eligibility : the cgroup's shared-queue entry count (cgroup_drr
 *                 .pending_total, fed by the inserted_request /
 *                 dispatched_request hooks) > 0, ONLY for SELECTION.
 *                 The per-cgroup local-policy queue is shared across
 *                 every hctx, so any hctx's pick may serve any cgroup's
 *                 queued request. Never consult policy_meta.queue_depth
 *                 (that is the local policy's view and diverges from the
 *                 arbiter's accounting).
 *   serve       : while deficit > 0 and pending_total > 0 the cursor stays
 *                 on the same cgroup (burst continues across picks).
 *   arm         : a cgroup receives its round budget (deficit += quantum
 *                 * eff_weight * ew_mult, capped) at most once per
 *                 GLOBAL round, via the refilled_round CAS latch.  The
 *                 grant fires for ANY cgroup with outstanding work:
 *                 pending_total > 0 (selected immediately) or an empty
 *                 queue with a live outstanding set (S11.1 arm-on-dry:
 *                 accrued as banked credit, never selected, never counted
 *                 as service).
 *   exhaust     : when a visited cgroup has deficit <= 0, it stays
 *                 skipped until the next round's arm.
 *   new round   : the GLOBAL round advances ONLY when every cgroup that
 *                 received a grant this round consumed it (deficit <= 0).
 *                 A cgroup that is mid-turn but transiently drained
 *                 HOLDS the round while it SERVED recently; a banked
 *                 buried cgroup does NOT hold (rounds pass, its bank
 *                 carries) and its bank survives the forfeit while its
 *                 outstanding set stays live (S11.1); a genuinely
 *                 stopped cgroup forfeits at ARB_ROUND_IDLE_RELEASE_NS
 *                 (S10; the pre-S10 unconditional advance spun the round
 *                 counter on every all-drain scan and collapsed weighted
 *                 fairness at cg16 -- grants went ~1/w, cancelling the
 *                 ~w budget).
 *   void turn   : a turn consumed by the serve-no-progress rotation (F2)
 *                 forfeits its leftover credit (deficit = 0) so a stuck
 *                 cgroup can neither poison the next rounds nor hold one.
 *                 S11.1 bounds the damage: the arm-on-dry re-arm
 *                 restores the banked credit within one sibling round
 *                 grant.
 *   liveness    : (S14, strict-NULL breaker forensics 20261111) a NULL
 *                 pick taken while the BPF q maps hold more than
 *                 ARB_STRICT_FUSE_BACKLOG entries is a fuse tick for
 *                 ioext4v2-rev2's strict breaker (iosched_ext.c
 *                 SCX_IO_STRICT_NULL_MIN_INFLIGHT): the second such
 *                 NULL >= 1s after the first degrades the hctx to the
 *                 active-pd fallback, permanently (one-way ratchet).
 *                 The fallback pops the q map WITHOUT dispatched_request
 *                 (pending_total leaks +1 per pop) and its completions
 *                 fire completed_request with cgroup_id = 0
 *                 (blk_update_request unlinks rq->bio BEFORE the
 *                 elevator completed hook, and elv.priv[0] is stashed
 *                 only on the pick path), so the outstanding decrement
 *                 misses the map too -- the ledger drifts at the
 *                 fallback dispatch rate on BOTH counters (bare-metal
 *                 S-decisive v3: out/pend +15-22k/s, pend sawtoothing
 *                 on the B3 void heal, out frozen at 445519 post-run).
 *                 When the aggregate queued backlog exceeds the
 *                 threshold, the S10 hold exit and the round-advance
 *                 exit therefore overflow-serve the stranded cgroup
 *                 instead of returning 0 (deficit clamped at 0
 *                 pre-serve -- pay-as-you-go, one-overshoot bound; the
 *                 S10 pin itself is preserved: the round only advances
 *                 when the holder releases, so grants cannot spin).
 *                 Below the threshold the NULL exits are byte-identical
 *                 to the pre-S14 design (one NULL per round boundary,
 *                 blk-mq re-kicks; the fuse cannot trip there), keeping
 *                 steady-state weighted fairness untouched.
 *
 * Per round a cgroup receives quantum * effective_weight bytes (cost for
 * arb_cost) of service, hence the long-run service ratio between
 * backlogged cgroups equals their weight ratio. deficit may go
 * transiently negative; the overshoot is bounded by one max request size.
 *
 * Requeue contract for THIS framework (verified against
 * kernel/linux-7.3.1/block/iosched_ext.c):
 *   blk_mq_requeue_request() calls the elevator requeue hook
 *   (arb->requeue_refund) and THEN re-inserts the request through
 *   blk_mq_insert_request() -> scx_io_insert_requests(), which re-fires
 *   policy->insert_request and arb->inserted_request.  Therefore
 *   requeue_refund must refund the charge and drop inflight, but must NOT
 *   increment pending: the re-insert's inserted_request does that.
 *   Incrementing pending here as well double counts and permanently leaks
 *   an entry, which in strict mode dead-locks dispatch (pick keeps
 *   selecting a tenant whose local queue is empty).
 */
#ifndef _IOEXT_ARB_COMMON_H
#define _IOEXT_ARB_COMMON_H

#include "ioext_policy_shared.h"
#include "ioext_bpf_debug.h"

#ifndef IOSCHED_ARB_ALLOW_FALLBACK
#define IOSCHED_ARB_ALLOW_FALLBACK  0ULL
#endif
#ifndef IOSCHED_ARB_STRICT
#define IOSCHED_ARB_STRICT         1ULL
#endif

/* Capacity invariants: ARB_MAX_TENANTS must equal IOSCHED_TENANT_LIMIT in
 * ioext_loader_common.h (pinned-map creation size and register/unregister
 * slot loops). ARB_MAX_HCTX must be >= the device's hardware-queue count
 * (virtio: nr virtqueues; NVMe: num_queues). See userspace/io/ARB_LIMITS.md. */
#define ARB_MAX_TENANTS   64
#define ARB_MAX_HCTX      64
#define ARB_SCAN_LIMIT    ARB_MAX_TENANTS

/*
 * quantum is per unit of effective weight: round budget = quantum * ew.
 * F1 sweep 20261021 (post-F2, D2 flat cg8 -W 100..800, kvmhost):
 *   q=64   -> weighted Jain 0.9742 (max/min 4.97; charge-overshoot
 *             asymmetry: w100 budget 6.4KB = 1.56 IOs, overshoot +28%)
 *   q=512  -> weighted Jain 0.9823 (max/min 5.29)  <-- SELECTED
 *   q=2048 -> weighted Jain 0.9740 (max/min 4.89; top weights pending-
 *             limited inside visits, share granularity resurfaces)
 * 512B * weight(100..800) = 51.2..409.6KB per round = 12.5..100 max-size
 * requests: overshoot <=8% at the low end and the turn (deficit > 0)
 * persists across pending refills so budget still binds per round.  The
 * 20260919 hardware run (session6) proved the old 32KB quantum let
 * visits end by pending exhaustion and service self-cancelled to equal
 * share (weighted Jain 0.63); the 20261021 F2 liveness fix (serve
 * no-progress rotation) alone moved Jain 0.81 -> 0.974.
 */
#define ARB_WDRR_QUANTUM_BYTES  512ULL
#define ARB_WDRR_IDLE_CAP_MULT  2

/*
 * S11 cap on the uniform floor lift (see arb_hctx_state.ew_mult): beyond
 * this the per-visit budget binding collapses anyway (F1 caveat 2), so a
 * wider lift cannot buy drift protection -- a pathological mix (tiny
 * weight + huge requests) keeps its structural one-request overshoot and
 * the documented soft degradation instead of megabyte-scale visits.
 */
#define ARB_WDRR_MAX_EW_MULT    32ULL

/*
 * F2 liveness hardening (20261021, treesub 2/2 P20 FAIL evidence):
 *
 * Under ARB strict the kernel gives the arbiter NO feedback when the pop for
 * the picked (cg,hctx) returns nothing (empty pop ends the dispatch attempt
 * silently).  A key that keeps pending > 0 with deficit > 0 while its pop
 * stays empty then absorbs EVERY pick on that hctx (serve branch returns
 * early, the scan never reaches the other tenants, the round never
 * advances) -- the 20260919 treesub wedge froze the low-weight subtree
 * completely (44k serves, 0 dispatches, sibling backlog 852 deep).
 *
 * Two self-healing rules, both bounded and both inactive in the healthy
 * path (every serve dispatches, so both counters stay 0):
 *   serve_no_progress : a serve that dispatched nothing since the previous
 *     serve of the same key increments the counter; after
 *     ARB_SERVE_NO_PROGRESS_MAX such serves the turn is CONSUMED
 *     (refilled_round = round) and the cursor rotates so the scan reaches
 *     the other tenants; the stuck key retries next round (1 wasted pick
 *     per round per stuck key at worst).
 *   dispatch_fails    : consecutive dispatch_failed events with no
 *     intervening successful dispatch; the guarded pending decrement
 *     cannot clear a crystallized ghost (drain-phase accounting races),
 *     so after ARB_DISPATCH_FAIL_GHOST_RESET consecutive failures the key
 *     is hard-reset (pending = 0, deficit = 0) and self-heals on the next
 *     insert (backlogged tenants re-insert immediately; idle tenants are
 *     skipped anyway).
 */
#define ARB_SERVE_NO_PROGRESS_MAX    3
#define ARB_DISPATCH_FAIL_GHOST_RESET 4

/*
 * STALL_FORENSICS.md leak B2 / B3 fixes (20261111, arb_recovery tune3):
 *
 * B2 -- DONTPREP/FLUSH bypass requeue double-decrements the outstanding
 * set.  blk_mq_requeue_request() ALWAYS fires the elevator requeue hook
 * (blk_mq_sched_requeue_request), but blk_mq_requeue_work() re-inserts
 * through the ELEVATOR only when RQF_DONTPREP is clear; a started
 * request (RQF_DONTPREP set) or a flush is handed to
 * blk_mq_request_bypass_insert() (hctx->dispatch) and NEVER re-fires
 * inserted_request.  The old ledger contract ("requeue -1, the
 * re-insert adds it back", see the requeue contract at the top of this
 * file) is therefore only half-true: for the bypass variant the -1 is
 * never repaid, the request still completes later through
 * completed_request (-1 again), and one request path costs the
 * cgroup's outstanding count TWO decrements against ONE insert.  The
 * count sags by one per bypass requeue until it crosses 0 with real
 * queued/inflight work still present -- arb_outstanding_nonzero (the
 * S13 corpse test) then hard-zeroes pending_total of a LIVE tenant
 * (skip_pending_zero forever, strict NULL streak; the ioext4v2-rev2
 * S-decisive v1/v2 fingerprint).  The requeue hook CANNOT tell the two
 * variants apart at event time (the kernel decides the route
 * asynchronously in requeue_work, and iosched_dispatched_ctx carries
 * only cmd_flags, not rq_flags), so the fix PAIRS events by request
 * identity (rq_ptr) in a small LRU map:
 *
 *   requeue_refund   : outstanding -1 as before (elevator contract
 *                      unchanged), plus stash {rq_ptr -> ts, refund,
 *                      requeues} (accumulate on re-stash).
 *   inserted_request : stash hit => this insert is the elevator
 *                      re-insert that repays the requeue -- pop the
 *                      stash, account NORMALLY (+1 outstanding).  A
 *                      miss behaves exactly as before.
 *   completed_request: stash hit => NO re-insert ever repaid this
 *                      requeue (bypass variant): undo the premature
 *                      outstanding -1 (add back `requeues`) and claw
 *                      back the unearned deficit refund (the bypass
 *                      re-dispatch from hctx->dispatch never fires
 *                      dispatched_request, so the +refund given at
 *                      requeue was never matched by a new charge),
 *                      then pop the stash and account the completion
 *                      NORMALLY (-1 outstanding).  Net lifetime for
 *                      one request: insert +1, requeue -1, completion
 *                      (undo +1) -1 == 0, charge booked exactly once.
 *
 * Residual hazard (accepted, benign direction): a request whose stash
 * entry lingers (it ended with neither a re-insert nor a completion --
 * only teardown edge paths can do that) can have its rq_ptr recycled by
 * a fresh request; that fresh request's completion then consumes the
 * stale entry and over-counts by one for the rest of the run.
 * Over-count is the SAFE direction: the S13 corpse test keeps erring
 * toward "alive" (it can never blind a live tenant, the observed
 * failure), and a bypass-requeued request whose completion pops its own
 * entry is unaffected.  LRU bounds the map; entries whose request
 * completes or re-inserts are always popped eagerly.
 *
 * B3 -- evacuate-invalid leaves a crystallized pending_total behind.
 * The kernel's teardown workfn pairs a VALID evacuate pop with
 * arb->dispatched_request + arb->completed_request (the ledger closes
 * exactly), but an INVALID evacuate pop (ghost entry whose request is
 * already gone: BUG-005 cancel residue) fires NO arbiter hook, so the
 * entry's insert-time pending_total charge is never retired while the
 * cgroup stays live.  The kernel-side bpf_in_flight half of that leak
 * needs patches/0003 (out of userspace scope); the arbiter-side
 * counterpart is the void-turn heal below: a pending_total that keeps
 * selecting the cgroup while every pop returns empty across FOUR
 * consecutive voided turns (>= 12 empty-pop serves with zero successful
 * dispatches in between -- a real pop->dispatched_request race resolves
 * within one dispatch call, microseconds, and any real dispatch resets
 * the counter) is crystallized by construction and is hard-zeroed,
 * mirroring the existing ARB_DISPATCH_FAIL_GHOST_RESET rule for the
 * dispatch-fail path.
 */
#define ARB_VOID_GHOST_RESET 4

/*
 * S14 strict-liveness fuse gauge threshold (20261111, ioext4v2-rev2
 * breaker forensics; see the liveness bullet in the DRR contract at the
 * top of this file).  The kernel's strict-NULL breaker arms when a pick
 * NULLs while data->bpf_in_flight (the BPF q-map backlog, insert++/pop--)
 * exceeds SCX_IO_STRICT_NULL_MIN_INFLIGHT, and degrades the SECOND such
 * NULL >= 1s later to the active-pd fallback -- permanently (one-way
 * ratchet).  The fallback path fires NO dispatched_request and its
 * completions carry an unresolvable cgroup id, so both ledger counters
 * drift for the rest of the run.  arb_drr_pick therefore serves the
 * round-stranded queued cgroup instead of returning NULL whenever the
 * maintained global_state.total_pending gauge (the mirror of
 * sum(pending_total), maintained +1/-1 by every pending_total mutation)
 * exceeds this mirror of the kernel threshold; below it the pre-S14
 * round/hold NULL exits are kept byte-identical, so steady-state
 * weighted fairness is untouched.  The gauge is a maintained counter
 * (NOT a scan-time sum) so the pick's scan loop keeps its pre-S14
 * live-register set: loop-carried accumulators doubled the verifier's
 * state exploration and pushed pick_cgroup to -E2BIG (bare-metal v4,
 * 1000001/1000000 processed insns).  MUST track block/iosched_ext.c
 * SCX_IO_STRICT_NULL_MIN_INFLIGHT.
 */
#define ARB_STRICT_FUSE_BACKLOG 32ULL

/* Capacity of the per-arbiter requeue_replay map (B2 pairing).  Must
 * comfortably exceed the number of concurrently requeue-transit
 * requests; LRU evicts under pathological pressure. */
#define ARB_REQUEUE_REPLAY_MAX 4096

/* B2 pairing state, keyed by rq_ptr. */
struct arb_requeue_replay {
    __u64 ts_ns;      /* requeue time, diagnostics only */
    __u64 refund;     /* accumulated unearned deficit refund (arbiter
                       * charge unit: bytes for WDRR/SLO, cost for cost) */
    __u64 requeues;   /* outstanding-sub decrements taken for this
                       * request since its last insert */
};

/*
 * S11 round-boundary demand accrual (20261107, homo-v2 burst-regime skew).
 *
 * Hardware evidence (homo v2 ctrl coloc, kvmhost sdb, LC 4k randread QD32
 * w100 / BE 64k seqwrite QD16 w200, per-500ms fio traces + tenant_state
 * dumps): the pre-S11 round structure treated pending == 0 as "no demand".
 * A tenant whose whole QD is inflight on the device (device-buried: LC
 * clat ~1-2ms behind BE write floods, per-hctx pending ~0 for hundreds of
 * round boundaries) received NO turn grant at those boundaries, so:
 *   - it banked no deficit credit, so the S10.1 round hold had nothing to
 *     pin, so
 *   - the round spun at completion-kick cadence and the OTHER tenant
 *     re-armed every spin -- BE dispatched at the full host-absorption
 *     rate (~1000 MiB/s bursts, 4x its weight share) while LC sat at its
 *     completion-trickle work-conserving floor (~117 MiB/s).
 * Measured be_share 0.765 (weight arm) / 0.78 (cost arm) vs the 0.667
 * io.weight byte-share contract; native bfq+io.weight held 0.66.
 *
 * S11.1 (second hardware iteration, same day): the demand signal CANNOT
 * be the per-(cg,hctx) key's inflight — on multi-hctx devices (kvmhost
 * sdb: 6 hw queues) the key the PICK runs on is the DECISION hctx while
 * the outstanding requests live on the HOME keys (first acceptance run:
 * LC inflight 32 all on h3..h5 while the dispatch picks ran on h0..h2 —
 * the dry-arm never fired where it mattered, BE re-armed at 2.4x LC's
 * cadence, absorption-phase be_share 0.82).  The demand signal is the
 * CGROUP-LEVEL outstanding map (pending + inflight summed over home
 * keys, with a last-activity timestamp); each arbiter carries one and
 * the framework hooks maintain it (insert +1, complete/fail/requeue -1).
 *
 * Three rules close the hole:
 *   arm-on-dry : a key whose queue is dry while its CGROUP still has
 *     outstanding requests with recent activity (a completion arrived
 *     within the round idle-release window) still receives its
 *     once-per-round turn grant — accrued as credit, never selected (its
 *     local queue is empty; selecting it would waste the pick on a
 *     silent empty pop).  The accrual deliberately does NOT refresh
 *     last_active_ns: accrual is not service.
 *   hold (S10.1, unchanged) : the round is pinned only by keys with
 *     unconsumed credit that SERVED recently — a mid-turn transiently
 *     drained key.  A banked buried key (credit, no recent serve) does
 *     NOT pin: rounds pass, its bank carries, the sibling keeps its full
 *     work-conserving pace, and the buried key drains its bank whenever
 *     its completions return (long-run share == arms x budget holds by
 *     conservation).
 *   forfeit (S10.3, extended) : a stale credit-holder loses its bank
 *     only when its CGROUP is also stale (no outstanding activity for
 *     the release window) — a genuinely stopped tenant.  A buried
 *     tenant whose completions still flow keeps its banked credit
 *     across round boundaries.
 * A voided F2 turn forfeits its leftover credit but the arm-on-dry
 * re-arm bounds the leak to one sibling round grant, then re-accrues.
 */

/*
 * S10 strict round semantics (20261026, D2 cg16 weighted-fairness collapse).
 *
 * Hardware evidence (tenant_state/hctx_state dumps, D2 cg16 iter1/iter2):
 *   - budgets are granted weight-proportional and fully consumed
 *     (delta dispatches / delta refill_arms == w/8 exactly for every
 *     weight: 12.5, 25.0, ..., 199.9),
 *   - but refill_arms (turn grants) scale ~1/w (11377 @w100 vs 1556 @w1600
 *     per 47s), so service = grants x budget self-cancels to equal share
 *     (JainW 0.64-0.67 at cg16),
 *   - because current_round spins ahead ~246/s (vs ~8/s full-consumption
 *     rounds): every all-drain scan (all 16 keys transiently pending==0)
 *     advanced the round, refilled_round went permanently stale, and the
 *     once-per-round turn grant degenerated into "refill whenever
 *     deficit <= 0" -- a cadence set by each key's own drain time, which
 *     is inversely proportional to its budget.
 *
 * Fix: the round may only advance when every key that received this
 * round's grant has consumed it (deficit <= 0).  A mid-turn key that is
 * transiently drained (pending == 0 with leftover credit) HOLDS the
 * round until its inserts return (blk-mq re-kicks on every completion,
 * so holding costs no work conservation) or until it has been dry for
 * ARB_ROUND_IDLE_RELEASE_NS, after which a stopped tenant releases the
 * hold and its credit carries over (classic DRR idle semantics).
 * A no-progress turn void also forfeits its leftover credit so a
 * crystallized key cannot hold the round forever (F2 wedge immunity).
 */
#define ARB_ROUND_IDLE_RELEASE_NS   (20ULL * 1000000ULL)

/*
 * Absolute round-budget floor: one 4K page.  The workload-adaptive part
 * of the floor (the uniform ew_mult lift computed at round advance from
 * every active key's own_max_charge) scales ALL budgets together until
 * each key's budget affords one of its own max-size requests, so a
 * large-request stream keeps byte fairness without compressing the
 * weight ratio (S11; the pre-S11 global last_round_max_charge floor
 * lifted only the keys below it).
 */
#define ARB_WDRR_MIN_BUDGET_BYTES 4096ULL

#define ARB_SLO_WINDOW_NS         (100ULL * 1000000ULL)
#define ARB_SLO_RECOVERY_WINDOWS  3
#define ARB_SLO_MIN_SAMPLES       20

#define ARB_SLO_BOOST_NORMAL    1
#define ARB_SLO_BOOST_MILD      2
#define ARB_SLO_BOOST_HIGH      4
#define ARB_SLO_BOOST_SEVERE    8
#define ARB_SLO_BOOST_MAX       16

#define ARB_SLO_DEFAULT_LATENCY_TARGET_NS  (100ULL * 1000ULL)
#define ARB_SLO_DEFAULT_PROTECT_INFLIGHT   32

enum arb_slo_state {
    SLO_NORMAL     = 0,
    SLO_PRESSURE   = 1,
    SLO_RECOVERY   = 2,
};

struct arb_tenant_key {
    __u64 cgroup_id;
    __u32 hctx_idx;
    __u32 reserved;
};

/*
 * S11.1 cgroup-level demand signal: outstanding = pending + inflight
 * summed over ALL of the cgroup's home keys, plus the last time the set
 * changed (insert / completion / dispatch-fail / requeue — the four
 * hooks that move a request in or out of the outstanding set; dispatch
 * only moves it pending -> inflight and does not touch this map).
 * Maintained by arb_outstanding_add/sub from the framework hooks; read
 * by arb_drr_pick's dry-arm and forfeit paths.  The per-(cg,hctx)
 * tenant keys cannot serve as the demand signal: the pick runs on the
 * DECISION hctx while the requests live on the HOME keys (see the S11.1
 * comment above).
 */
struct arb_cgroup_outstanding {
    __u64 count;
    __u64 last_activity_ns;
};

struct arb_tenant_state {
    __s64 deficit;
    __u64 pending;
    __u64 inflight;
    __u64 completed_bytes;
    __u64 completed_cost;
    __u64 dispatches;
    __u64 requeues;
    __u64 requeued_bytes;

    /* pick telemetry: counts of each arb_drr_pick scan outcome for this
     * tenant. dump with `bpftool map dump name tenant_state` to diagnose
     * fairness anomalies without any debug build. */
    __u64 serve_selects;
    __u64 refill_arms;
    __u64 wrap_selects;
    __u64 skip_pending_zero;

    __u64 refilled_round;

    __u32 boost;
    __u32 slo_state;
    __u64 window_start_ns;
    __u32 window_total;
    __u32 window_over_target;
    __u32 good_windows;
    __u64 last_latency_ns;
    __u64 ewma_latency_ns;

    __u64 last_charge_ns;
    __u64 last_active_ns;

    /* F2 (20261021): liveness counters, see ARB_SERVE_NO_PROGRESS_MAX.
     * last_serve_dispatches snapshots ts->dispatches at the previous serve
     * select so the next serve can tell whether the granted turn actually
     * dispatched anything. */
    __u64 last_serve_dispatches;
    __u32 serve_no_progress;
    __u32 dispatch_fails;

    /* S11 ratio-preserving floor lift: largest charge booked against this
     * key's deficit since the last round advance (window = one round).
     * The advance path derives hctx_state->ew_mult from every active
     * key's ceil(own_max_charge / (quantum * weight)) so the NEXT round's
     * budgets scale uniformly until each key's budget affords at least
     * one of its own max-size requests.  Uniform scaling keeps the
     * service ratio == weight ratio (the pre-S11 global max-charge floor
     * lifted only the keys below it and compressed the ratio, e.g.
     * 100:200 with 64K requests -> 1:1.5625 instead of 1:2). */
    __u64 own_max_charge;
};

/*
 * S12 cgroup-level DRR state (see the S12 comment in the S11.1 block and
 * arb_drr_pick): on multi-hctx devices the per-(cg,hctx) round barriers
 * are DISJOINT -- each fio tenant's requests home to one hctx, so the
 * tenant's own hctx spins its rounds unopposed (homo-v2 hardware:
 * BE absorbed at the full host rate on its home hctx while LC's home
 * hctx held a separate, unenforced round).  The decision state --
 * deficit, refilled_round, serve accounting, own-max-charge -- is
 * therefore keyed by CGROUP only, the round counter and the uniform
 * lift live in ONE global slot, and selection eligibility is the
 * cgroup's shared-queue entry count (pending_total), which makes the
 * per-cgroup local-policy FIFO genuinely cross-hctx: any hctx's pick
 * can serve any cgroup's queued request and the charge lands on the
 * cgroup's single deficit.  pending/inflight stay per-(cg,hctx)
 * REQUEST-IDENTITY counters (the insert/dispatch/complete pairing
 * contract is unchanged); only the DECISION state is cgroup-level.
 */
struct arb_cgroup_drr {
    __s64 deficit;               /* charge unit: bytes or cost */
    __u64 refilled_round;        /* the once-per-round grant latch */
    __u64 pending_total;         /* shared-queue entries across home keys */
    __u64 own_max_charge;        /* largest charge since last round advance */
    __u64 dispatches;            /* cgroup-global (serve_no_progress baseline) */
    __u64 last_serve_dispatches;
    __u64 serve_selects;
    __u64 refill_arms;
    __u64 skip_pending_zero;
    __u64 fastpath_selects;      /* D1 sole-demand override serves (see the
                                  * D1 block in arb_drr_pick); nonzero only
                                  * in single-demand regimes -- CI-assertable
                                  * that it stays 0 in multi-tenant runs. */
    __u32 serve_no_progress;      /* F2 liveness (see ARB_SERVE_NO_PROGRESS_MAX) */
    __u32 dispatch_fails;        /* F2 ghost reset (see ARB_DISPATCH_FAIL_GHOST_RESET) */
    __u32 void_turns;           /* B3 heal (see ARB_VOID_GHOST_RESET): consecutive
                                 * serve-no-progress turns with zero intervening
                                 * dispatches; >= ARB_VOID_GHOST_RESET crystallizes
                                 * pending_total (evacuate-invalid / BUG-005
                                 * residue) and hard-zeroes it. */
    __u64 last_active_ns;        /* last SERVE/select-arm (not accrual) */
};

/* One global slot per arbiter: the shared round counter + the S11
 * uniform floor lift.  All hctxes' picks gate against the SAME round,
 * which is what makes the round barrier bind across hardware queues. */
struct arb_global_state {
    __u64 current_round;
    __u64 ew_mult;               /* uniform budget lift, ARB_WDRR_MAX_EW_MULT cap */
    /* S14 fuse gauge (20261111, verifier-fit rework of the tune4 S14):
     * the mirror of sum(pending_total) over every cgroup -- the userspace
     * view of the SAME quantity the kernel breaker's bpf_in_flight counts
     * (BPF q-map backlog).  Maintained +1/-1 by every pending_total
     * mutation (insert +1; guarded dispatch dec -1; dispatch-fail
     * ghost reset, B3 void heal and S13 corpse reset subtract the whole
     * pre-zero value).  The gauge exists so arb_drr_pick can test
     * "queued backlog > ARB_STRICT_FUSE_BACKLOG" with ONE map lookup
     * instead of accumulating a sum across the 64-slot scan loop: three
     * loop-carried registers doubled the verifier's state exploration
     * and pushed pick_cgroup to -E2BIG (bare-metal S-decisive v4: 1000001
     * processed insns vs the 1000000 limit).  Race profile matches the
     * per-cgroup counter it mirrors (XADD everywhere; the zero sites
     * subtract the pre-read value, so a concurrent insert can leave
     * the gauge +1 over -- the SAFE direction, it errs toward serving,
     * never toward the NULL the fuse counts).  The loader diag prints
     * it (tot_pend) next to pend_sum so the invariant is assertable
     * live on the target. */
    __u64 total_pending;
};

struct arb_hctx_state {
    __u32 cursor;
    __u32 last_selected_idx;
    __u64 round_dispatches;
    /* Visibility diagnostics: occupied-slot-but-unregistered (meta missing)
     * and registered-but-no-tenant-state scan outcomes. Nonzero after steady
     * state indicates a registration/accounting inconsistency that strict
     * mode turns into starvation, so they must be assertable from CI. */
    __u64 skip_no_meta;
    __u64 skip_no_ts;
    /* S10/S11.1 round-hold telemetry: scan-completes that pinned the
     * round on a recently-served credit holder. */
    __u64 round_holds;
    /* S14 scratch (verifier-fit rework of the tune4 S14): the first
     * cgroup with pending work seen by THIS pick call, stored here
     * instead of a loop-carried register so the scan loop's live-
     * register set is byte-identical to the pre-S14 verifier footprint
     * (loop-carried scalars doubled the state exploration -> -E2BIG).
     * Cleared at pick entry, consumed by the overflow serves at the
     * hold/round-advance exits.  Pure scratch: never read by userspace
     * tools (the loader mirror keeps it for size parity only). */
    __u64 stranded_cgid;
};

#define ARB_COST_OP_READ   0
#define ARB_COST_OP_WRITE  1
#define ARB_COST_OPS       2
#define ARB_COST_BUCKETS   5

static __always_inline __u32 arb_cost_size_bucket(__u64 nr_sectors)
{
    __u64 bytes = nr_sectors * 512;
    if (bytes <= 4096)      return 0;
    if (bytes <= 16384)     return 1;
    if (bytes <= 65536)     return 2;
    if (bytes <= 131072)    return 3;
    return 4;
}

static __always_inline __u32 arb_cost_op_type(__u64 opf)
{
    __u32 op = (__u32)(opf & 0xff);
    return (op == 1) ? ARB_COST_OP_WRITE : ARB_COST_OP_READ;
}

static __always_inline __u64 arb_cost_estimate(__u64 opf, __u64 nr_sectors)
{
    __u32 op = arb_cost_op_type(opf);
    __u32 bucket = arb_cost_size_bucket(nr_sectors);
    /* S6: table scaled x64 (4K read = 64) so ARB_COST_QUANTUM=1 gives
     * budget(w) = w/64 dispatches of a 4K read (1.56..12.5 for
     * w=100..800) -- inside the hardware per-visit queue depth, see
     * arb_cost.bpf.c and ARB_WDRR_QUANTUM_BYTES.  Read/write ratio 4:1
     * and size-bucket ratios are unchanged. */
    static const __u64 base_cost[ARB_COST_OPS][ARB_COST_BUCKETS] = {
        {64, 256, 1024, 2048, 4096},
        {256, 1024, 4096, 8192, 16384},
    };
    if (op >= ARB_COST_OPS || bucket >= ARB_COST_BUCKETS)
        return nr_sectors;
    return base_cost[op][bucket];
}


/* S11.1 cgroup-outstanding map maintenance (see struct
 * arb_cgroup_outstanding).  +1 on insert; -1 (guarded) on completion /
 * dispatch-fail / requeue — the re-insert that follows a requeue adds it
 * back.  Both paths stamp last_activity_ns: activity is "the outstanding
 * set is changing", i.e. completions are flowing. */
static __always_inline void arb_outstanding_add(
    void *out_map, __u64 cgroup_id, __u64 now_ns)
{
    struct arb_cgroup_outstanding *o;
    struct arb_cgroup_outstanding init = {};

    o = bpf_map_lookup_elem(out_map, &cgroup_id);
    if (!o) {
        bpf_map_update_elem(out_map, &cgroup_id, &init, BPF_NOEXIST);
        o = bpf_map_lookup_elem(out_map, &cgroup_id);
        if (!o)
            return;
    }
    __sync_fetch_and_add(&o->count, 1);
    o->last_activity_ns = now_ns;
}

static __always_inline void arb_outstanding_sub(
    void *out_map, __u64 cgroup_id, __u64 now_ns)
{
    struct arb_cgroup_outstanding *o;

    o = bpf_map_lookup_elem(out_map, &cgroup_id);
    if (!o)
        return;
    if (__sync_fetch_and_add(&o->count, -1) == 0)
        __sync_fetch_and_add(&o->count, 1);   /* guarded: never below 0 */
    o->last_activity_ns = now_ns;
}

/* Undo N premature outstanding decrements in one atomic add (B2
 * completion-side compensation; N = stash->requeues).  The entry is
 * guaranteed to exist (insert created it before any requeue of the
 * same request could fire), so unlike arb_outstanding_add this does
 * not need the create-on-miss path. */
static __always_inline void arb_outstanding_add_n(
    void *out_map, __u64 cgroup_id, __u64 n, __u64 now_ns)
{
    struct arb_cgroup_outstanding *o;

    if (!n)
        return;
    o = bpf_map_lookup_elem(out_map, &cgroup_id);
    if (!o)
        return;
    __sync_fetch_and_add(&o->count, n);
    o->last_activity_ns = now_ns;
}

/*
 * B2 requeue-replay pairing (see the leak-B2 block at the constants
 * above).  Three event-side helpers, one per hook that can observe a
 * requeued request's continuation.  Call them from the arbiter's hooks
 * with the SAME refund value the requeue hook passes to
 * arb_note_requeue; the outstanding/deficit compensation runs ONLY on
 * the completion that finds the stash still live (the bypass variant).
 */
static __always_inline void arb_requeue_replay_stash(
    void *replay_map, __u64 rq_ptr, __u64 refund, __u64 now_ns)
{
    struct arb_requeue_replay *rp;
    struct arb_requeue_replay init = {};

    if (!replay_map || !rq_ptr)
        return;
    rp = bpf_map_lookup_elem(replay_map, &rq_ptr);
    if (rp) {
        /* Chained bypass requeue (re-dispatched from hctx->dispatch
         * straight back to the driver, failed again): each requeue
         * event took another outstanding -1 and another deficit
         * refund with no matching charge, so accumulate both; the
         * single completion undoes them all at once. */
        __sync_fetch_and_add(&rp->requeues, 1);
        __sync_fetch_and_add(&rp->refund, refund);
        rp->ts_ns = now_ns;
        return;
    }
    init.ts_ns = now_ns;
    init.refund = refund;
    init.requeues = 1;
    bpf_map_update_elem(replay_map, &rq_ptr, &init, BPF_ANY);
}

/* inserted_request side: an insert whose rq_ptr has a live stash entry
 * is the elevator re-insert that repays the requeue's -1.  Pop the
 * entry so the request's eventual completion is accounted normally;
 * accounting itself proceeds exactly as for a fresh insert (+1). */
static __always_inline void arb_requeue_replay_pair_insert(
    void *replay_map, __u64 rq_ptr)
{
    if (!replay_map || !rq_ptr)
        return;
    if (bpf_map_lookup_elem(replay_map, &rq_ptr))
        bpf_map_delete_elem(replay_map, &rq_ptr);
}

/* completed_request side: a completion whose rq_ptr still has a live
 * stash entry is the bypass variant's single retirement -- no re-insert
 * ever repaid the requeue.  Undo the outstanding decrements, claw back
 * the unearned deficit refund, pop the entry, and let the caller's
 * normal completion accounting (-1 outstanding) proceed. */
static __always_inline void arb_requeue_replay_pair_complete(
    void *replay_map, void *outstanding_map, void *drr_map,
    __u64 rq_ptr, __u64 cgroup_id, __u64 now_ns)
{
    struct arb_requeue_replay *rp;
    struct arb_cgroup_drr *cd;
    __u64 requeues;
    __u64 refund;

    if (!replay_map || !rq_ptr)
        return;
    rp = bpf_map_lookup_elem(replay_map, &rq_ptr);
    if (!rp)
        return;
    /* Copy BEFORE the delete: the lookup pointer is dead the moment the
     * entry is removed (the slot may be handed to another CPU's
     * update), and the verifier requires no map-value access after the
     * deleting call. */
    requeues = rp->requeues;
    refund = rp->refund;
    bpf_map_delete_elem(replay_map, &rq_ptr);
    arb_outstanding_add_n(outstanding_map, cgroup_id, requeues, now_ns);
    if (!refund || !drr_map)
        return;
    cd = bpf_map_lookup_elem(drr_map, &cgroup_id);
    if (!cd)
        return;
    __sync_fetch_and_add(&cd->deficit, -(__s64)refund);
}

/* Demand test for the S11.1 dry-arm / forfeit paths: the cgroup has
 * outstanding requests AND its outstanding set is still changing (a
 * completion/insert arrived within the round idle-release window).  A
 * stale count (leaked entry on a stopped cgroup) goes inert here: no
 * accrual, no bank protection — it can neither wedge nor steal. */
static __always_inline int arb_outstanding_active(
    void *out_map, __u64 cgroup_id, __u64 now_ns)
{
    struct arb_cgroup_outstanding *o;

    o = bpf_map_lookup_elem(out_map, &cgroup_id);
    if (!o || o->count == 0)
        return 0;
    return (now_ns - o->last_activity_ns) < ARB_ROUND_IDLE_RELEASE_NS;
}

/* S13 corpse test (criterion fix 20261110, bm fio backfill livelock):
 * LEVEL check only -- is the cgroup's outstanding set empty?  The corpse
 * reset in arb_drr_pick must NOT use the ACTIVITY test above: a live
 * tenant on a fast device (600k+ IOPS on the fast DRAM-less NVMe, fio backfill ~200k) sits in
 * backfill gaps longer than ARB_ROUND_IDLE_RELEASE_NS with its queue
 * still loaded, and the activity test misreads that inert-but-live set
 * as death, hard-zeroing pending_total and blinding the tenant forever
 * (pick -> skip_pending_zero, fio livelock).  The count is the lifetime
 * truth instead: this map is +1'd by inserted_request and -1'd by
 * completed_request / dispatch-fail / requeue, and an io_uring exit
 * CANCEL retires still-queued requests through completed_request (no
 * dispatch-fail, no pop -- that is exactly how pending_total leaks,
 * BUG-005), so a genuinely exited tenant drains its count to 0 while
 * any queued-or-inflight request of a live tenant keeps it > 0 --
 * staleness cannot fake death.  No signal (map missing / no entry)
 * returns 0 so the corpse reset keeps its original no-signal behavior.
 * now_ns is kept in the signature for call-site symmetry with
 * arb_outstanding_active; a pure count test needs no clock. */
static __always_inline int arb_outstanding_nonzero(
    void *out_map, __u64 cgroup_id, __u64 now_ns)
{
    struct arb_cgroup_outstanding *o;

    if (!out_map)
        return 0;
    o = bpf_map_lookup_elem(out_map, &cgroup_id);
    if (!o)
        return 0;
    return o->count > 0;
}

/*
 * S14 fuse-gauge maintenance (see struct arb_global_state.total_pending):
 * mirror ONE pending_total mutation onto the global counter.  XADD keeps
 * the gauge exact against concurrent inserts/dispatches from other CPUs.
 */
static __always_inline void arb_total_pending_add(
    void *global_map, __s64 delta)
{
    struct arb_global_state *gs;
    __u32 gkey = 0;

    if (!global_map || !delta)
        return;
    gs = bpf_map_lookup_elem(global_map, &gkey);
    if (!gs)
        return;
    __sync_fetch_and_add(&gs->total_pending, delta);
}

/* S14: the guarded pending_total decrement, gauge-mirrored -- the
 * decrement only counts (and only mirrors) when the counter was
 * actually nonzero (arb_atomic_dec_guarded semantics preserved). */
static __always_inline void arb_pending_dec_gauged(
    void *global_map, __u64 *pend)
{
    if (__sync_fetch_and_add(pend, -1) == 0)
        __sync_fetch_and_add(pend, 1);   /* guarded: never below 0 */
    else
        arb_total_pending_add(global_map, -1);
}

/*
 * S14 v5 forensics (20261111): exact zeroing for the pending_total
 * hard-reset sites (B3 void heal / S13 corpse reset / dispatch-fail
 * ghost reset).  The pre-v6 pattern (read -> gauge-subtract -> store 0)
 * is racy against concurrent insert/dispatch on other CPUs: a guarded
 * dispatch dec between the read and the store is counted BOTH in the
 * swapped-out value AND by its own mirror -> the gauge loses one extra
 * per race -> tot_pend wraps (v5 on bm: 18446744073709551614 == -2);
 * an insert in the same window survives in the gauge while the store
 * wipes the per-cgroup side -> the gauge over-counts with NO real
 * pending -> the overflow-serve gate fires with an empty scratch ->
 * NULL -> breaker fuse tick (v5: +38 trips -> degrade -> the fallback's
 * uncharged round-robin collapsed the share to 1:1 and halved IOPS).
 * The atomic exchange takes the exact pre-zero value: every concurrent
 * mutation lands before (counted in the swapped value) or after
 * (counted normally) -- the gauge stays exact under all interleavings.
 *
 * __noinline (bare-metal v6 oracle forensics): the exchange's returned scalar,
 * INLINED inside the serve branch's scan loop and the corpse pass, is a
 * fresh unknown register born in the caller's per-iteration state
 * lattice -- it pushed pick_cgroup from 808k to past the 1M processed
 * insn limit (identical static size, oracle-measured twice).  As a
 * BPF-to-BPF subprogram the scalar is born and consumed inside the
 * callee's frame; the two call sites shrink to one BPF_CALL each.
 */
static __attribute__((noinline)) void arb_pending_zero_gauged(
    void *global_map, __u64 *pend)
{
    __u64 old = __sync_lock_test_and_set(pend, 0UL);

    arb_total_pending_add(global_map, -(__s64)old);
}

/*
 * S13 round-hold feasibility (20261107, bm f1 T3/T4 round-trickle): can one
 * turnover of the cgroup's outstanding set plausibly consume the leftover
 * credit?  A holder pins the global round only while
 *     count * own_max_charge >= deficit
 * (count = cgroup-level pending+inflight from the framework hooks,
 * own_max_charge = largest single charge booked this round).  A QD1 tenant
 * granted 50..100 of its own request sizes (W400 x 512B quantum = 200..400KB
 * vs one 4K, doubled by the S11 ew_mult lift) consumes ONE request per
 * completion, so letting it pin holds every round for its whole QD1 drain
 * (bare-metal f1 T3: rounds ~90/s, B W100 -> ~7 MiB/s, C W200 -> ~13) while its
 * siblings' once-per-round grants trickle weight-proportionally.  An
 * infeasible holder KEEPS its credit (serve-branch priority on its next
 * insert; S10.3 forfeit still applies when stale) but stops pinning, which
 * restores the ioext2-era advance cadence (rounds advance on all-drain
 * picks) without reintroducing the pre-S10 refill-on-spin fairness collapse
 * (re-arm stays gated on deficit <= 0).  Buried-but-deep tenants (homo-v2
 * LC: QD32 x 4K >= its 12.8-request budget) still pin.  No signal (no map /
 * no entry) conservatively preserves the old always-hold behavior; a count
 * of zero can never consume, so it never pins.
 */
static __always_inline int arb_hold_feasible(
    void *out_map, __u64 cgroup_id, __s64 deficit, __u64 own_max_charge)
{
    struct arb_cgroup_outstanding *o;

    if (!out_map)
        return 1;
    o = bpf_map_lookup_elem(out_map, &cgroup_id);
    if (!o)
        return 1;
    if (o->count == 0)
        return 0;
    if (own_max_charge == 0)
        return 1;
    return ((__u64)o->count * own_max_charge) >= (__u64)deficit;
}

/*
 * Round-budget effective weight, the single definition shared by the
 * arm grant and the D1 sole-demand top-up: weight clamped to 1, scaled
 * by the S11 uniform lift, then raised to the absolute min_budget floor
 * (see arb_drr_arm_grant for the rationale of each step).
 */
static __always_inline __u64 arb_round_eff_weight(
    struct iosched_policy_meta *meta,
    struct arb_global_state *gs,
    __u64 quantum,
    __u64 min_budget,
    __u32 boost)
{
    __u64 ew = meta->arb_weight * boost;

    /* Branchless clamps (D1 verifier-fit, kept from the D1 slimming:
     * the branchy forms cost MORE verifier states here than the
     * arithmetic ones -- ew += (ew == 0) / the ew_mult fold keep the
     * arm-grant's range analysis compact at both inlined sites):
     *   ew == 0        -> ew + 1 == 1, else ew + 0
     *   ew_mult == 0/1 -> *1 (original: no multiply), else *ew_mult */
    ew += (ew == 0);
    ew *= gs->ew_mult + (gs->ew_mult == 0);
    if (quantum * ew < min_budget)
        ew = (min_budget + quantum - 1) / quantum;
    return ew;
}

/*
 * Shared turn-grant path (the arm): latch the once-per-round grant via
 * the refilled_round CAS on the CGROUP-LEVEL state (blk-mq does not
 * serialize dispatch loops, so exactly one concurrent pick must win the
 * turn), then add the round budget quantum * eff_weight * ew_mult to the
 * cgroup's deficit, capped at 2x.
 *
 * eff_weight = max(arb_weight * boost, min_budget/quantum): the absolute
 * floor keeps tiny weights able to afford one page per round.  The
 * workload-adaptive part of the floor is the UNIFORM global ew_mult
 * (S11): every cgroup's budget scales together until each active
 * cgroup's budget affords one of its own max-size requests, so
 * large-request streams keep byte fairness WITHOUT compressing the
 * weight ratio (the pre-S11 global max-charge floor lifted only the keys
 * below it, e.g. 4K-read w100 vs 64K-write w200 -> 65536:102400 =
 * 1:1.5625 instead of the contracted 1:2).
 *
 * accrual=1 is the S11.1 dry path: the grant is banked for a cgroup
 * whose shared queue is empty but whose outstanding set is live.  The
 * accrual deliberately does NOT refresh last_active_ns (accrual is not
 * service), so a banked cgroup never pins the round by itself.
 *
 * Returns 1 when THIS caller won the grant latch, 0 when a concurrent
 * pick already granted this round's turn (caller must not select).
 */
static __always_inline int arb_drr_arm_grant(
    void *states_map,
    struct iosched_policy_meta *meta,
    struct arb_cgroup_drr *cd,
    struct arb_global_state *gs,
    __u64 *cgid,
    __u32 hctx_idx,
    __u64 now_ns,
    __u64 quantum,
    __u64 min_budget,
    __u32 (*get_boost_fn)(void *states_map, __u64 cgid, __u32 hctx_idx),
    int accrual)
{
    __u64 cur_round;
    __u64 prev_round;
    __u64 ew;
    __s64 deficit_val;
    __s64 cap;
    __u32 boost;

    /* The round is re-read HERE (the entry snapshot can be stale by the
     * time the scan reaches this cgroup): another dispatch loop on any
     * hctx may have advanced the global round mid-scan, and arming
     * against the stale snapshot would CAS refilled_round BACKWARD past
     * a concurrent arm -- granting a second budget in the same round.
     * The refilled_round CAS is the grant latch: exactly one concurrent
     * pick wins the turn, once per round, per cgroup. */
    cur_round = gs->current_round;
    if (cd->refilled_round == cur_round)
        return 0;
    prev_round = cd->refilled_round;
    if (__sync_val_compare_and_swap(&cd->refilled_round,
                                    prev_round, cur_round) != prev_round)
        return 0;

    boost = get_boost_fn ? get_boost_fn(states_map, *cgid, hctx_idx) : 1;
    ew = arb_round_eff_weight(meta, gs, quantum, min_budget, boost);

    /* The arm branch is only reached with deficit <= 0 (the serve branch
     * catches positive deficit), so the add cannot cross the cap; keep
     * the clamp as a defensive no-op.  XADD keeps the add exact against
     * concurrent dispatch-time charges. */
    __sync_fetch_and_add(&cd->deficit, (__s64)(quantum * ew));
    deficit_val = cd->deficit;
    cap = (__s64)(quantum * ew * ARB_WDRR_IDLE_CAP_MULT);
    if (deficit_val > cap)
        cd->deficit = cap;
    if (!accrual)
        cd->last_active_ns = now_ns;
    cd->refill_arms++;
    return 1;
}

/* Lazy per-cgroup decision-state creation (mirrors arb_ensure_tenant for
 * the identity keys).  Concurrent creators collapse onto one entry. */
static __always_inline struct arb_cgroup_drr *arb_ensure_cgroup_drr(
    void *drr_map, __u64 cgroup_id)
{
    struct arb_cgroup_drr *cd;
    struct arb_cgroup_drr zero = {};

    cd = bpf_map_lookup_elem(drr_map, &cgroup_id);
    if (cd)
        return cd;
    bpf_map_update_elem(drr_map, &cgroup_id, &zero, BPF_NOEXIST);
    return bpf_map_lookup_elem(drr_map, &cgroup_id);
}

/*
 * S14 overflow serve (see the liveness bullet in the DRR contract): the
 * pop that follows is expected to succeed (the stranded cgroup's queue
 * is deep -- the serve fired on the fuse gauge), so dispatched_request
 * charges the cgroup's deficit and retires pending_total exactly like a
 * turn serve.  The deficit is CLAMPED at 0 pre-serve (the D1 fastpath
 * v2 precedent: pay-as-you-go, no standing debt): an un-clamped charge
 * accumulates one unbudgeted request per round boundary and the debt
 * outpaces the arm grant (measured solo: deficit to -27MB in 500
 * rounds, turn service dead); the clamp bounds the post-serve deficit
 * to -bytes >= -max_request -- the DRR contract's own overshoot bound.
 * A crystallized-ghost stranding (B3 residue: pending_total > 0 on an
 * empty queue) fails the pop, advances nothing, and the existing
 * serve-no-progress / void-turn machinery bounds it -- same as a turn
 * serve on the same state.  Cursor and last_selected_idx are
 * deliberately untouched (the D1 v1 lesson: re-pinning the cursor on
 * the served key starves the scan); the next pick re-evaluates from
 * the scan position.  The cd may have vanished (unregister raced
 * between the scan and here): return 0 in that case -- the entry set
 * is gone with it.
 */
static __always_inline __u64 arb_drr_overflow_serve(
    void *drr_map, __u64 cgroup_id, __u64 now_ns)
{
    struct arb_cgroup_drr *cd = bpf_map_lookup_elem(drr_map, &cgroup_id);

    if (!cd)
        return 0;
    if (cd->deficit < 0)
        cd->deficit = 0;
    cd->serve_selects++;
    cd->last_active_ns = now_ns;
    return cgroup_id;
}
/*
 * Dispatch-time charged DRR pick (classic round structure; see the
 * contract at the top of this file and tests/io/arb_wdrr_model.py).
 *
 * S12 cgroup-level round structure: the round counter and the uniform
 * lift live in ONE global slot shared by every hctx, and each cgroup
 * carries ONE deficit/refilled_round pair.  A cgroup may be refilled
 * (deficit += quantum * eff_weight * ew_mult, capped) at most once per
 * global round, and that refill IS its turn grant: it is served (from
 * ANY hctx -- the local-policy queue is per-cgroup and shared across
 * hardware queues) until its deficit exhausts, then skipped for the
 * rest of the round.  When every active cgroup has been served this
 * round, the round advances and the first eligible cgroup is granted
 * the new round's budget.  Per-hctx state is scan machinery only
 * (cursor + diagnostics).
 *
 * S11.1 demand accrual: a cgroup whose shared queue is empty while its
 * outstanding set is live (pending_total == 0, outstanding > 0, activity
 * within the release window) still receives its once-per-round grant --
 * banked as credit, never selected (the pop would be an empty one), and
 * never counted as service for the hold.  Its bank survives the S10.3
 * forfeit while completions keep flowing, and is served when its
 * inserts return.
 *
 * min_budget is the ABSOLUTE floor for the round budget, in the SAME unit
 * as quantum's charge (bytes for WDRR/SLO, cost units for arb_cost) -- a
 * byte-denominated constant passed by a cost-charged arbiter inflates the
 * floor and degenerates the round structure (verified by tenant_state map
 * dumps 20260917).  The workload-adaptive floor part is the uniform
 * global ew_mult (see arb_drr_arm_grant).
 *
 * get_boost_fn: per-tenant multiplier (SLO); WDRR/cost pass a constant 1.
 */
static __always_inline __u64 arb_drr_pick(
    void *states_map,
    void *outstanding_map,
    void *drr_map,
    void *global_map,
    struct arb_hctx_state *hctx_state,
    void *active_cgroup_ids,
    void *policy_meta_map,
    __u32 hctx_idx,
    __u64 now_ns,
    __u64 quantum,
    __u64 min_budget,
    __u32 (*get_boost_fn)(void *states_map, __u64 cgid, __u32 hctx_idx),
    __u32 solo_gate)
{
    __u32 base;
    __u32 step;
    __u32 idx;
    __u32 gkey = 0;
    __u64 *cgid;
    __u64 round;
    struct iosched_policy_meta *meta;
    struct arb_cgroup_drr *cd;
    struct arb_global_state *gs;
    /* D1: sole-demand census -- counted during the scan below, consumed
     * by the D1 fast path after the scan (see the D1 block there). */
    __u32 demand_count = 0;
    __u64 sole_demand_cgid = 0;
    __u32 sole_demand_idx = 0;
    /* S14 liveness (strict-NULL breaker forensics 20261111; see the
     * liveness bullet in the DRR contract and the gauge comment in
     * struct arb_global_state): a NULL pick taken while the queued
     * backlog exceeds ARB_STRICT_FUSE_BACKLOG is a fuse tick for the
     * ioext4v2-rev2 breaker (iosched_ext.c
     * SCX_IO_STRICT_NULL_MIN_INFLIGHT; one-way ratchet, second such
     * NULL >= 1s after the first degrades the hctx to the active-pd
     * fallback, whose pops skip dispatched_request and whose
     * completions carry an unresolvable cgroup id -- BOTH ledger
     * counters drift, bm S-decisive v3: out/pend +15-22k/s, out frozen
     * at 445519 post-run).  Verifier-fit rework of the tune4 S14 (bare-metal
     * v4: -E2BIG, 1000001/1000000 processed insns -- three loop-
     * carried scalars doubled the state exploration): NO loop-carried
     * gauge here anymore.  The backlog total is the maintained
     * global_state.total_pending (one gs-> lookup, already live at both
     * exits); the serve candidate is the hctx_state->stranded_cgid
     * scratch (first pending cgroup seen, stored mid-scan, cleared at
     * pick entry) -- the scan loop's live-register set is byte-
     * identical to the pre-S14 verifier footprint. */

    if (!hctx_state || !active_cgroup_ids || !drr_map || !global_map)
        return 0;
    if (quantum == 0)
        quantum = 1;

    gs = bpf_map_lookup_elem(global_map, &gkey);
    if (!gs)
        return 0;
    round = gs->current_round;
    base = hctx_state->cursor;
    /* S14 scratch: first pending cgroup this pick call (cleared every
     * call -- a stale value from an aborted early exit can never leak
     * into the overflow serves). */
    hctx_state->stranded_cgid = 0;

    for (step = 0; step < ARB_SCAN_LIMIT; step++) {
        idx = (base + step) % ARB_MAX_TENANTS;

        cgid = bpf_map_lookup_elem(active_cgroup_ids, &idx);
        if (!cgid || *cgid == 0)
            continue;
        meta = bpf_map_lookup_elem(policy_meta_map, cgid);
        if (!meta) {
            hctx_state->skip_no_meta++;
            continue;
        }

        cd = bpf_map_lookup_elem(drr_map, cgid);
        if (!cd) {
            hctx_state->skip_no_ts++;
            continue;
        }

        if (cd->pending_total == 0) {
            /* S11.1 arm-on-dry: an empty shared queue with a live
             * outstanding set (requests on the device, completions
             * arriving within the release window) is NOT idle.  Bank
             * this round's grant; never select (the pop would be an
             * empty one) and never count accrual as service. */
            if (cd->deficit <= 0 && cd->refilled_round != round &&
                outstanding_map &&
                arb_outstanding_active(outstanding_map, *cgid, now_ns))
                arb_drr_arm_grant(states_map, meta, cd, gs, cgid,
                                  hctx_idx, now_ns, quantum, min_budget,
                                  get_boost_fn, 1);
            cd->skip_pending_zero++;
            continue;
        }

        /* D1 demand census (rides the existing scan, no extra pass):
         * every visited key that passed the meta/cd lookups and has a
         * queued entry is demand; the fast path after the scan acts
         * only while exactly one such key exists.
         * D1 verifier-fit: capture only the FIRST demanded key and
         * saturate at 2 -- once two demands are seen the fast path is
         * dead anyway, and freezing the census (no further writes)
         * keeps the scan loop's verifier states prunable.  With
         * demand_count == 1 the first demanded key IS the sole one, so
         * the fast path sees the same cgid as a last-write census.
         * The freshness test keeps crystallized/stale keys (exited
         * tenants with leaked pending_total) out of the census so the
         * T6 solo window still fast-paths. */
        /* S14: remember the first cgroup with pending work in the
         * hctx scratch (NOT a loop-carried register -- see the S14
         * block above); the overflow serves at the hold/round-advance
         * exits consume it instead of a NULL whenever the maintained
         * global gauge exceeds ARB_STRICT_FUSE_BACKLOG.  Past this
         * point pending_total > 0 is guaranteed (the pend==0 branch
         * continued above), so no second test is needed.  Capturing
         * at EVERY pending sighting (not just the round-barrier skip
         * path) is harmless: a serve-branch cgroup never reaches the
         * epilogue -- the pick returns first whenever the scan serves
         * someone. */
        if (!hctx_state->stranded_cgid)
            hctx_state->stranded_cgid = *cgid;
        if (demand_count < 2 &&
            now_ns - cd->last_active_ns < ARB_ROUND_IDLE_RELEASE_NS) {
            if (demand_count == 0) {
                sole_demand_cgid = *cgid;
                sole_demand_idx = idx;
            }
            demand_count++;
        }

        if (cd->deficit > 0) {
            /* Turn in progress: keep serving this cgroup -- unless the
             * turn grants keep landing on empty pops (F2: strict mode
             * gives no empty-pop feedback, so detect it ourselves). */
            if (cd->dispatches == cd->last_serve_dispatches) {
                cd->serve_no_progress++;
            } else {
                cd->serve_no_progress = 0;
            }
            cd->last_serve_dispatches = cd->dispatches;
            if (cd->serve_no_progress > ARB_SERVE_NO_PROGRESS_MAX) {
                /* Consume the turn and rotate: the scan must reach the
                 * other cgroups or the round never advances (treesub
                 * wedge).  The voided turn forfeits its leftover credit
                 * (S10); the round stamp uses the CURRENT round so a
                 * concurrent arm cannot move the latch BACKWARD.  S11.1
                 * bounds the damage: the dry re-arm restores the accrued
                 * credit within one sibling round grant. */
                cd->serve_no_progress = 0;
                cd->refilled_round = gs->current_round;
                cd->deficit = 0;
                /* B3 heal (see ARB_VOID_GHOST_RESET): this turn was
                 * consumed by serve-no-progress AGAIN with no
                 * intervening dispatch.  A real pending entry cannot
                 * survive here -- a pop with entries present succeeds
                 * and the dispatched_request hook resets the streak
                 * within the same dispatch call; only the microseconds
                 * pop->hook cross-hctx race can transiently show
                 * pending>0 on an empty pop, and it cannot span
                 * ARB_VOID_GHOST_RESET turns of 4 no-progress serves
                 * apiece.  Four voided turns therefore mean the map is
                 * REALLY empty while pending_total is stuck > 0 --
                 * the evacuate-invalid / BUG-005 crystallized residue
                 * (no hook ever retires it) -- so hard-zero it, the
                 * same surgery as the dispatch-fail ghost reset.  A
                 * live tenant's next real insert re-adds pending and
                 * dispatches immediately (counters reset).  S14 gauge:
                 * XCHG-exact zeroing (arb_pending_zero_gauged; the v5
                 * tot_pend wrap and the +38 fuse trips both lived in
                 * the racy read->sub->store form).  A ghost stranded
                 * at the overflow-serve exits heals through THIS path:
                 * the round advance re-latches refilled_round, the
                 * next pick arms it, and the serve branch's no-progress
                 * cycle runs the 4-turn heal -- the overflow serve
                 * itself needs no duplicate machinery (adding it
                 * pushed pick_cgroup past the verifier's 1M processed-
                 * insn limit even as __noinline subprograms; bm v6
                 * oracle measurement). */
                if (++cd->void_turns >= ARB_VOID_GHOST_RESET) {
                    cd->void_turns = 0;
                    arb_pending_zero_gauged(global_map,
                                            &cd->pending_total);
                }
                hctx_state->cursor = (idx + 1) % ARB_MAX_TENANTS;
                continue;
            }
            hctx_state->cursor = idx;
            hctx_state->last_selected_idx = idx;
            cd->last_active_ns = now_ns;
            cd->serve_selects++;
            return *cgid;
        }

        if (cd->refilled_round != round) {
            /* New turn: grant this round's budget and serve it. */
            if (!arb_drr_arm_grant(states_map, meta, cd, gs, cgid,
                                    hctx_idx, now_ns, quantum, min_budget,
                                    get_boost_fn, 0)) {
                /* Concurrent pick won this round's grant latch. */
                hctx_state->cursor = (idx + 1) % ARB_MAX_TENANTS;
                continue;
            }
            hctx_state->cursor = idx;
            hctx_state->last_selected_idx = idx;
            return *cgid;
        }

        /* Already had its turn this round: move the cursor past it. */
        hctx_state->cursor = (idx + 1) % ARB_MAX_TENANTS;
    }

    /*
     * D1 sole-demand fast path v2 (20261107, D1_overhead_bm.md A5): a
     * solo QD1 stream exhausts its round budget (quantum * ew = 50 x 4K
     * at W400) and the exhaust rule leaves the next request pending with
     * nothing in flight -- no completion exists to re-kick dispatch, so
     * the hctx sits silent for ~1 jiffy (clat p99 3.9 ms, IOPS -81%).
     * With exactly ONE cgroup holding fresh queued demand, DRR gating
     * cannot buy fairness (there is nobody queued to be fair against):
     * serve the sole demander unconditionally.
     *
     * v2 serves with a ZEROED ledger and touches NOTHING else -- the
     * v1 top-up (deficit += quantum*ew, refilled_round = current,
     * cursor = sole idx) broke multi-tenant DRR three ways (bare-metal run
     * DYN_arm_f1_arbfb 2026-11-07: B/C collapsed to ~10-20 MiB/s with
     * the device mostly idle):
     *   1. the top-up re-granted budget MANY times per round, bypassing
     *      the once-per-round refilled_round CAS latch, so the served
     *      tenant kept a standing positive deficit and its serve branch
     *      early-returned every subsequent pick;
     *   2. cursor = sole_demand_idx re-pinned the scan ON the served
     *      tenant, so later picks hit that serve branch BEFORE the
     *      census ever saw the re-queued competitors -- the census only
     *      stays correct on scans that run to completion;
     *   3. demand was "pending_total > 0", so the census==1 moments
     *      (competitors momentarily fully dispatched, QD8/rate-paced)
     *      fired constantly in MULTI-tenant windows.
     * v2: the fast path fires only for the sole FRESH queued tenant
     * (see the census comment), serves it with deficit clamped at 0 --
     * pay-as-you-go, no standing credit, no debt lockout once real
     * competition returns -- and leaves cursor/refilled_round/deficit
     * grants entirely to the unchanged S10/S11 machinery.  The theft
     * window is exactly "nobody else has queued work": competitors that
     * are in flight cannot consume more anyway, and their next insert
     * makes demand_count 2, resuming the untouched rotation.
     *
     * v3 GATE (bare-metal f1 rerun 2026-11-07 15:4x, same collapse with v1 and
     * v2): in multi-tenant windows the per-pick census==1 moments are
     * constant (QD/rate-paced competitors are in flight, not queued),
     * and the sole-queued tenant then serves WITHOUT ever consuming a
     * round -- A (4K QD1) racked up 1.2M fastpath serves while B (64K
     * QD8, W100, grant ~1.5 of its own 64K reads per round) got re-armed
     * only at the round cadence, which the healthy fast-pathed QD1
     * stream slows to ~24 rounds/s: B collapsed to ~7 MiB/s (ioext2
     * baseline: 1285).  ioext2 never had this because its QD1 tenant
     * sat exhaust-locked (the very disease D1 fixes), leaving the round
     * machine to spin freely.  So the fast path now requires the whole
     * active list to hold EXACTLY ONE tenant: T1/T6 solo windows get
     * the full work-conserving bypass (51k IOPS, p99 48us), and every
     * multi-tenant window runs the unchanged S10/S11 DRR machinery --
     * byte-identical to ioext2 semantics, which measured
     * B/C/D = 1510/1263/434 MiB/s on the same arm. */
    if (demand_count == 1 && solo_gate) {
        /* sole_demand_cgid is nonzero by construction here: the census
         * only counts keys that passed the *cgid == 0 skip. */
        meta = bpf_map_lookup_elem(policy_meta_map, &sole_demand_cgid);
        cd = bpf_map_lookup_elem(drr_map, &sole_demand_cgid);
        if (meta && cd) {
            if (cd->deficit <= 0)
                cd->deficit = 0;
            cd->last_active_ns = now_ns;
            cd->serve_selects++;
            cd->fastpath_selects++;
            return sole_demand_cgid;
        }
    }

    /*
     * Every selectable cgroup already had its pick this scan.  S10 strict
     * round advance: hold the round while any cgroup holds unconsumed
     * credit and SERVED recently (a mid-turn transiently drained one --
     * its inserts will re-kick the hctx).  Pre-S10 the round advanced
     * here unconditionally and spun ~246/s on all-drain scans at cg16,
     * collapsing weighted fairness.  S11.1: a buried cgroup's banked
     * credit does NOT pin the round (accrual never refreshed recency) --
     * rounds pass, the bank carries, the sibling keeps its
     * work-conserving pace; the bank survives the forfeit while the
     * cgroup's outstanding set stays live.  The same pass accumulates
     * the next round's uniform lift k = max over cgroups of
     * ceil(own_max_charge / (quantum * base_ew)).
     */
    {
        struct arb_cgroup_drr *cd2;
        __u64 k = 1;
        __u64 base_ew;
        __u64 need;

        for (step = 0; step < ARB_SCAN_LIMIT; step++) {
            idx = (base + step) % ARB_MAX_TENANTS;

            cgid = bpf_map_lookup_elem(active_cgroup_ids, &idx);
            if (!cgid || *cgid == 0)
                continue;

            cd2 = bpf_map_lookup_elem(drr_map, cgid);
            if (!cd2)
                continue;

            /* S13 corpse reset: fio/io_uring exit cancels still-queued
             * requests WITHOUT a dispatch-fail and WITHOUT a pop, so
             * cgroup_drr.pending_total leaks on the exited tenant's key
             * (bare-metal f1 T6: stale B key, pending_total=64, forever; see
             * D1_FASTPATH_V2.md §3).  The ghost is re-armed every round,
             * its "serves" are empty pops that quiesce the hctx, and the
             * solo survivor pays with its tail latency.
             *
             * Criterion fix 20261110 (bare-metal fio backfill livelock): a corpse
             * is a key whose CGROUP-LEVEL OUTSTANDING SET IS EMPTY, not
             * one whose set is merely quiet (the pre-fix activity test
             * via arb_outstanding_active).  Cancel exits retire their
             * requests through completed_request -> arb_outstanding_sub,
             * so the exited tenant's count reaches 0 and the leak is
             * still caught (BUG-005 unchanged); a live tenant's queued
             * or inflight requests keep its count > 0 across any backfill
             * gap, so it can never be misjudged no matter how stale the
             * activity timestamp (the activity test hard-zeroed
             * pending_total on fast devices -> permanent
             * skip_pending_zero).  Map missing / no entry keeps the
             * original reset behavior (no signal = assume corpse). */
            if (cd2->pending_total &&
                !arb_outstanding_nonzero(outstanding_map, *cgid, now_ns)) {
                /* S14 gauge: XCHG-exact zeroing (arb_pending_zero_gauged). */
                arb_pending_zero_gauged(global_map,
                                        &cd2->pending_total);
                cd2->deficit = 0;
            }

            if (cd2->deficit > 0) {
                if (now_ns - cd2->last_active_ns < ARB_ROUND_IDLE_RELEASE_NS) {
                    /* S13 hold-feasibility: a mid-turn drained holder pins
                     * the round only while one turnover of its outstanding
                     * set could cover the leftover credit (see
                     * arb_hold_feasible).  An infeasible holder keeps its
                     * credit but does not pin: the round advances and the
                     * siblings get their once-per-round grants. */
                    if (arb_hold_feasible(outstanding_map, *cgid,
                                          cd2->deficit,
                                          cd2->own_max_charge)) {
                        hctx_state->round_holds++;
                        /* S14 liveness: the pin stands only while the
                         * fuse cannot trip (maintained queued-backlog
                         * gauge at/below the breaker threshold).  With
                         * a deep stranded queue, returning 0 hands
                         * those entries to the kernel's breaker
                         * fallback -- popped WITHOUT dispatched_request
                         * and completed with cgroup_id 0, drifting
                         * BOTH ledger counters (see the liveness
                         * bullet in the DRR contract).  Serve the
                         * stranded cgroup instead and return straight
                         * from here (verifier-fit: no loop-carried
                         * flag; the remaining cgroups' corpse/forfeit
                         * passes run on the next pick's epilogue --
                         * both are idempotent).  The round does NOT
                         * advance: the pin is preserved, the holder
                         * keeps its turn window and the stranded
                         * cgroup pays its debt at the next arm. */
                        if (gs->total_pending > ARB_STRICT_FUSE_BACKLOG)
                            return arb_drr_overflow_serve(
                                drr_map, hctx_state->stranded_cgid,
                                now_ns);
                        return 0;
                    }
                } else if (!(outstanding_map &&
                             arb_outstanding_active(outstanding_map, *cgid,
                                                    now_ns))) {
                    /* S10.3 self-healing release: only a genuinely stopped
                     * cgroup (no outstanding activity) forfeits its credit.
                     * A live-but-stalled one keeps the bank. */
                    cd2->deficit = 0;
                }
            }

            /* S11: accumulate the uniform lift need for this cgroup
             * (used only if the round actually advances below).
             * base_ew is the pre-lift effective weight (weight clamped
             * to 1, then raised to the absolute min_budget floor);
             * boost >= 1 only grows the real budget, so the base is
             * the conservative choice for the lift target. */
            meta = bpf_map_lookup_elem(policy_meta_map, cgid);
            if (!meta)
                continue;
            base_ew = meta->arb_weight ? meta->arb_weight : 1;
            if (quantum * base_ew < min_budget)
                base_ew = (min_budget + quantum - 1) / quantum;
            need = cd2->own_max_charge / (quantum * base_ew);
            if (cd2->own_max_charge % (quantum * base_ew))
                need += 1;
            if (need > k)
                k = need;
        }

        /*
         * Round genuinely complete: advance the GLOBAL round.  The CAS
         * keeps concurrent dispatch loops (any hctx) from
         * double-advancing; the next pick grants the new round's budget
         * to the first eligible cgroup.  (S14: the post-advance exit
         * below no longer returns 0 while a stranded cgroup has queued
         * work -- under the ioext4v2-rev2 breaker that NULL was a fuse
         * tick, not the negligible round boundary the pre-S14 design
         * assumed.)  The advance also rolls the uniform lift window:
         * the next round's budgets scale by the accumulated k (capped)
         * and every cgroup's own-max-charge window resets.  A charge that
         * races between the accumulation read and the reset is lost from
         * both windows -- bounded to one structural overshoot,
         * self-corrects next round.
         */
        if (__sync_val_compare_and_swap(&gs->current_round,
                                        round, round + 1) == round) {
            if (k > ARB_WDRR_MAX_EW_MULT)
                k = ARB_WDRR_MAX_EW_MULT;
            gs->ew_mult = k;
            for (step = 0; step < ARB_SCAN_LIMIT; step++) {
                idx = (base + step) % ARB_MAX_TENANTS;

                cgid = bpf_map_lookup_elem(active_cgroup_ids, &idx);
                if (!cgid || *cgid == 0)
                    continue;

                cd2 = bpf_map_lookup_elem(drr_map, cgid);
                if (cd2)
                    cd2->own_max_charge = 0;
            }
        }

        /*
         * S14 liveness, round-advance variant: the barrier just rolled
         * while the maintained queued-backlog gauge still exceeds the
         * breaker threshold.  The pre-S14 exit returned 0 here ("one
         * NULL dispatch per round is negligible") -- that assumption
         * died with the ioext4v2-rev2 strict breaker: every NULL taken
         * while the BPF maps hold > SCX_IO_STRICT_NULL_MIN_INFLIGHT
         * entries arms the one-way fuse, and the second such NULL >= 1s
         * later degrades this hctx to the active-pd fallback for the
         * rest of the run (uncharged pops leak pending_total; their
         * completions miss the outstanding map).  Serve the stranded
         * cgroup now, charged (debt clamped to one overshoot by
         * arb_drr_overflow_serve); its next arm -- the round just
         * advanced, so refilled_round latches the new round on the
         * next pick -- pays the debt.  The round cadence is unchanged
         * -- the advance already happened.  If a concurrent retire
         * emptied the last queue between the scan and here the gauge
         * follows it (both XADD-maintained) and this test simply does
         * not fire; a residual µs race can leave the scratch empty
         * with the gauge hot, in which case the serve's lookup misses
         * and returns 0 -- the same fuse-tick residual the insert
         * push->hook window already documents (bounded, needs two 1s
         * apart to matter).
         */
        if (gs->total_pending > ARB_STRICT_FUSE_BACKLOG)
            return arb_drr_overflow_serve(drr_map,
                                          hctx_state->stranded_cgid,
                                          now_ns);
    }
    return 0;
}

static __always_inline void arb_drr_advance_cursor(
    struct arb_hctx_state *hctx_state)
{
    if (hctx_state)
        hctx_state->cursor = (hctx_state->cursor + 1) % ARB_MAX_TENANTS;
}

static __always_inline void arb_ensure_tenant(
    void *states_map, __u64 cgroup_id, __u32 hctx_idx)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = hctx_idx,
        .reserved = 0,
    };
    struct arb_tenant_state *ts;
    /* BE tenants never run arb_slo_update_window (latency target 0), so a
     * zero-init boost would collapse ew to the pick clamp (arb_slo_model.py
     * spec: boost==1 at creation). */
    struct arb_tenant_state zero_state = {};
    zero_state.boost = ARB_SLO_BOOST_NORMAL;

    ts = bpf_map_lookup_elem(states_map, &key);
    if (ts)
        return;
    if (bpf_map_update_elem(states_map, &key, &zero_state, BPF_ANY)) {
        IOEXT_BPF_DBG_SAMPLED(1,
            "[arb] tenant_state insert failed cg=%llu hctx=%u\n",
            cgroup_id, hctx_idx);
    }
}

/* Insert (submit ctx), dispatch (plug-flush or completion-kick ctx) and
 * completion (softirq ctx) update the paired pending/inflight counters from
 * different execution contexts; plain BPF loads/stores lose updates (JIT'd
 * as load-add-store, not a single RMW). All paired counters must go through
 * atomic XADD. */
static __always_inline void arb_atomic_dec_guarded(__u64 *v)
{
    if (__sync_fetch_and_add(v, -1) == 0)
        __sync_fetch_and_add(v, 1);
}

/*
 * S11 floor-lift bookkeeping: track the largest charge booked against the
 * DECISION key's deficit since the last round advance.  The advance path
 * derives the next round's uniform ew_mult from every active key's
 * own_max_charge so each key's budget affords one of its own max-size
 * requests (see arb_hctx_state.ew_mult and arb_drr_pick's advance path).
 */
static __always_inline void arb_note_own_max_charge_cd(
    struct arb_cgroup_drr *cd, __u64 charge)
{
    if (cd && charge > cd->own_max_charge)
        cd->own_max_charge = charge;
}

static __always_inline void arb_note_inserted(
    void *states_map, void *outstanding_map, void *drr_map,
    void *global_map, __u64 cgroup_id, __u32 hctx_idx, __u64 now_ns)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = hctx_idx,
        .reserved = 0,
    };
    struct arb_tenant_state *ts;
    struct arb_cgroup_drr *cd;

    if (outstanding_map)
        arb_outstanding_add(outstanding_map, cgroup_id, now_ns);
    /* S12: selection eligibility is the cgroup's shared-queue entry
     * count (the per-cgroup local-policy queue is shared across every
     * hardware queue), not the per-(cg,hctx) identity slice. */
    if (drr_map) {
        cd = arb_ensure_cgroup_drr(drr_map, cgroup_id);
        if (cd) {
            __sync_fetch_and_add(&cd->pending_total, 1);
            arb_total_pending_add(global_map, 1);
        }
    }
    arb_ensure_tenant(states_map, cgroup_id, hctx_idx);
    ts = bpf_map_lookup_elem(states_map, &key);
    if (!ts)
        return;
    __sync_fetch_and_add(&ts->pending, 1);
}

static __always_inline void arb_note_dispatched_bytes(
    void *states_map, void *drr_map, void *global_map, __u64 cgroup_id,
    __u32 hctx_idx, __u32 home_hctx_idx, __u64 bytes)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = home_hctx_idx,
        .reserved = 0,
    };
    struct arb_tenant_state *ts;
    struct arb_cgroup_drr *cd;

    ts = bpf_map_lookup_elem(states_map, &key);
    if (ts) {
        arb_atomic_dec_guarded(&ts->pending);
        __sync_fetch_and_add(&ts->inflight, 1);
        ts->completed_bytes += bytes;
        ts->dispatch_fails = 0;
        ts->serve_no_progress = 0;
    }

    if (!drr_map)
        return;
    /* S12: one CGROUP-LEVEL deficit is charged no matter which hctx's
     * pick won the pop (the local-policy queue is shared).  XADD keeps
     * the budget exact against concurrent dispatch loops, which
     * blk-mq does not serialize. */
    cd = bpf_map_lookup_elem(drr_map, &cgroup_id);
    if (!cd)
        return;
    arb_pending_dec_gauged(global_map, &cd->pending_total);
    __sync_fetch_and_add(&cd->deficit, -(__s64)bytes);
    __sync_fetch_and_add(&cd->dispatches, 1);
    cd->dispatch_fails = 0;
    cd->serve_no_progress = 0;
    cd->void_turns = 0;
    arb_note_own_max_charge_cd(cd, bytes);
}

static __always_inline void arb_note_dispatched_cost(
    void *states_map, void *drr_map, void *global_map, __u64 cgroup_id,
    __u32 hctx_idx, __u32 home_hctx_idx, __u64 cost)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = home_hctx_idx,
        .reserved = 0,
    };
    struct arb_tenant_state *ts;
    struct arb_cgroup_drr *cd;

    ts = bpf_map_lookup_elem(states_map, &key);
    if (ts) {
        arb_atomic_dec_guarded(&ts->pending);
        __sync_fetch_and_add(&ts->inflight, 1);
        ts->completed_cost += cost;
        ts->dispatch_fails = 0;
        ts->serve_no_progress = 0;
    }

    if (!drr_map)
        return;
    /* S12: one CGROUP-LEVEL deficit, charged in cost units (see
     * arb_note_dispatched_bytes). */
    cd = bpf_map_lookup_elem(drr_map, &cgroup_id);
    if (!cd)
        return;
    arb_pending_dec_gauged(global_map, &cd->pending_total);
    __sync_fetch_and_add(&cd->deficit, -(__s64)cost);
    __sync_fetch_and_add(&cd->dispatches, 1);
    cd->dispatch_fails = 0;
    cd->serve_no_progress = 0;
    cd->void_turns = 0;
    arb_note_own_max_charge_cd(cd, cost);
}

static __always_inline void arb_note_dispatched_inflight(
    void *states_map, void *drr_map, void *global_map, __u64 cgroup_id,
    __u32 hctx_idx, __u32 home_hctx_idx)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = home_hctx_idx,
        .reserved = 0,
    };
    struct arb_tenant_state *ts;
    struct arb_cgroup_drr *cd;
    (void)hctx_idx;
    ts = bpf_map_lookup_elem(states_map, &key);
    if (ts) {
        arb_atomic_dec_guarded(&ts->pending);
        __sync_fetch_and_add(&ts->inflight, 1);
        ts->dispatch_fails = 0;
        ts->serve_no_progress = 0;
    }
    if (!drr_map)
        return;
    cd = bpf_map_lookup_elem(drr_map, &cgroup_id);
    if (!cd)
        return;
    arb_pending_dec_gauged(global_map, &cd->pending_total);
    cd->dispatch_fails = 0;
    cd->serve_no_progress = 0;
    cd->void_turns = 0;
}

/*
 * The framework pops the rq from the local policy before validating it,
 * so an invalid-rq dispatch consumes one pending entry: retire it here
 * (dispatch_failed is only called on the invalid-rq path).  Consecutive
 * failures with no intervening successful dispatch mean the key's pending
 * is a crystallized ghost the guarded decrement cannot clear — hard-reset
 * it (F2, see ARB_DISPATCH_FAIL_GHOST_RESET).
 */
static __always_inline void arb_note_dispatch_failed(
    void *states_map, void *outstanding_map, void *drr_map,
    void *global_map, __u64 cgroup_id, __u32 hctx_idx, __u64 now_ns)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = hctx_idx,
        .reserved = 0,
    };
    struct arb_tenant_state *ts;
    struct arb_cgroup_drr *cd;
    if (outstanding_map)
        arb_outstanding_sub(outstanding_map, cgroup_id, now_ns);
    if (drr_map) {
        cd = bpf_map_lookup_elem(drr_map, &cgroup_id);
        if (cd) {
            if (++cd->dispatch_fails >= ARB_DISPATCH_FAIL_GHOST_RESET) {
                /* F2 ghost reset, S12 cgroup level: a crystallized
                 * pending_total the guarded decrement cannot clear.
                 * S14 gauge: XCHG-exact zeroing (arb_pending_zero_gauged). */
                arb_pending_zero_gauged(global_map, &cd->pending_total);
                cd->deficit = 0;
                cd->dispatch_fails = 0;
                cd->serve_no_progress = 0;
                cd->void_turns = 0;
            } else {
                arb_pending_dec_gauged(global_map, &cd->pending_total);
            }
        }
    }
    ts = bpf_map_lookup_elem(states_map, &key);
    if (!ts)
        return;
    if (++ts->dispatch_fails >= ARB_DISPATCH_FAIL_GHOST_RESET) {
        ts->pending = 0;
        ts->deficit = 0;
        ts->dispatch_fails = 0;
        ts->serve_no_progress = 0;
        return;
    }
    arb_atomic_dec_guarded(&ts->pending);
}

static __always_inline void arb_note_completed(
    void *states_map, void *outstanding_map, __u64 cgroup_id, __u32 hctx_idx,
    __u64 now_ns)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = hctx_idx,
        .reserved = 0,
    };
    struct arb_tenant_state *ts;
    if (outstanding_map)
        arb_outstanding_sub(outstanding_map, cgroup_id, now_ns);
    ts = bpf_map_lookup_elem(states_map, &key);
    if (!ts)
        return;
    arb_atomic_dec_guarded(&ts->inflight);
}

/*
 * Refund only — never touch pending here: the kernel re-inserts a
 * requeued request through scx_io_insert_requests(), which re-fires
 * inserted_request and restores pending (see the requeue contract at
 * the top of this file).  `refund` is in the arbiter's own charge unit
 * (bytes for WDRR/SLO, cost for arb_cost) and must mirror what
 * dispatched_request charged for the same request.
 */
static __always_inline void arb_note_requeue(
    void *states_map, void *outstanding_map, void *drr_map,
    __u64 cgroup_id, __u32 hctx_idx, __u32 home_hctx_idx,
    __u64 refund, __u64 now_ns)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = home_hctx_idx,
        .reserved = 0,
    };
    struct arb_tenant_state *ts;
    struct arb_cgroup_drr *cd;

    if (outstanding_map)
        arb_outstanding_sub(outstanding_map, cgroup_id, now_ns);
    ts = bpf_map_lookup_elem(states_map, &key);
    if (ts)
        arb_atomic_dec_guarded(&ts->inflight);

    if (!drr_map)
        return;
    /* S12: the refund mirrors the dispatched charge on the single
     * CGROUP-LEVEL deficit; the decision-hctx key stash is no longer
     * needed for accounting. */
    cd = bpf_map_lookup_elem(drr_map, &cgroup_id);
    if (!cd)
        return;
    if (refund)
        __sync_fetch_and_add(&cd->deficit, (__s64)refund);
    ts = bpf_map_lookup_elem(states_map, &key);
    if (ts) {
        ts->requeued_bytes += refund;
        ts->requeues++;
    }
}

static __always_inline void arb_slo_update_window(
    struct arb_tenant_state *ts,
    __u64 now_ns,
    __u64 latency_target_ns,
    __u32 max_boost,
    __u64 total_latency_ns)
{
    if (!ts)
        return;

    if (ts->window_start_ns == 0) {
        ts->window_start_ns = now_ns;
        ts->window_total = 0;
        ts->window_over_target = 0;
    }

    if (now_ns - ts->window_start_ns >= ARB_SLO_WINDOW_NS) {
        bool violation = false;

        if (ts->window_total >= ARB_SLO_MIN_SAMPLES && latency_target_ns > 0) {
            if ((__u64)ts->window_over_target * 100 > (__u64)ts->window_total)
                violation = true;
        }

        if (ts->slo_state == SLO_NORMAL) {
            if (violation) {
                ts->slo_state = SLO_PRESSURE;
                ts->boost = ARB_SLO_BOOST_HIGH;
                ts->good_windows = 0;
            }
        } else if (ts->slo_state == SLO_PRESSURE) {
            if (!violation) {
                ts->slo_state = SLO_RECOVERY;
                ts->good_windows = 1;
                ts->boost = ARB_SLO_BOOST_MILD;
            } else if (ts->window_total > 0 &&
                       (__u64)ts->window_over_target * 10 > (__u64)ts->window_total) {
                ts->boost = ARB_SLO_BOOST_SEVERE;
            } else {
                ts->boost = ARB_SLO_BOOST_HIGH;
            }
        } else if (ts->slo_state == SLO_RECOVERY) {
            if (violation) {
                ts->slo_state = SLO_PRESSURE;
                ts->boost = ARB_SLO_BOOST_HIGH;
                ts->good_windows = 0;
            } else {
                ts->good_windows++;
                if (ts->good_windows >= ARB_SLO_RECOVERY_WINDOWS) {
                    ts->slo_state = SLO_NORMAL;
                    ts->boost = ARB_SLO_BOOST_NORMAL;
                }
            }
        }

        if (ts->boost > max_boost)
            ts->boost = max_boost;
        if (ts->boost < ARB_SLO_BOOST_NORMAL)
            ts->boost = ARB_SLO_BOOST_NORMAL;

        ts->window_start_ns = now_ns;
        ts->window_total = 0;
        ts->window_over_target = 0;
    }

    ts->window_total++;
    if (latency_target_ns > 0 && total_latency_ns > latency_target_ns)
        ts->window_over_target++;

    if (ts->ewma_latency_ns == 0)
        ts->ewma_latency_ns = total_latency_ns;
    else
        ts->ewma_latency_ns = (ts->ewma_latency_ns * 7 + total_latency_ns) / 8;
    ts->last_latency_ns = total_latency_ns;
}

#endif /* _IOEXT_ARB_COMMON_H */