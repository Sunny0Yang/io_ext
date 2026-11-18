/* ioext_arb_feedback_common.h - Feedback-pressure extension for arb_feedback
 *
 * PURELY ADDITIVE on top of ioext_arb_common.h: nothing in the shared
 * WDRR/SLO/cost core (structs, macros, arb_drr_pick, arb_note_*) is
 * redefined or re-semanticized here.  arb_feedback reuses the arb_drr_pick
 * skeleton and the arb_tenant_state identity counters verbatim; the
 * feedback state below lives in SEPARATE maps so the wdrr/cost/slo
 * acceptance scripts that decode arb_tenant_state/arb_hctx_state layouts
 * stay valid.
 *
 * Design (arb_feedback, dynamic multi-tenant main experiment):
 *
 *   1. Feedback pressure (AIMD x EWMA), per (cgroup, hctx):
 *      - completions feed an EWMA of total_latency_ns into the EXISTING
 *        arb_tenant_state.ewma_latency_ns field (unused by this arbiter's
 *        control path otherwise; per-object maps, no cross-arbiter alias).
 *      - a 100ms window counts over-target completions; a violating
 *        window HALVES the tenant's in-flight budget (aimd_budget), a
 *        clean window adds FB_AIMD_ADD.  The budget is clamped to
 *        [FB_BUDGET_MIN, FB_BUDGET_MAX] (or fb_cfg override, clamped) on
 *        every write, so the multiplier math can never overflow or
 *        degenerate.
 *      - hold_dispatch(): if a latency-critical tenant (arb_prio <=
 *        FB_HIGH_PRIO_MAX) is in FB_PRESSURE on this hctx AND some
 *        competing low-prio tenant's inflight (identity counter) is at or
 *        above its aimd_budget, the dispatch attempt is held.  This is
 *        the arb_slo hold pattern with a per-tenant AIMD budget instead
 *        of a fixed threshold.  Liveness: hold only delays (blk-mq
 *        re-kicks on the next completion); pick_cgroup itself never
 *        returns 0 while anyone is pending (arb_drr_pick contract).
 *
 *   2. Service preference + starvation guard:
 *      - preference is expressed via the EXISTING loader CLI
 *        (--arb-prio / --arb-weight) exactly as with arb_slo/arb_cost.
 *      - low-prio tenants (arb_prio > FB_HIGH_PRIO_MAX) accumulate a
 *        bounded starvation credit per inserted request (backlog-driven,
 *        capped at FB_CREDIT_MAX); each dispatch decays it by
 *        FB_CREDIT_DECAY.  credit >= FB_CREDIT_BOOST_AT lifts the
 *        tenant's effective_weight by FB_CREDIT_BOOST_MULT (bounded), so
 *        low-prio tenants keep making progress every round without
 *        taking rounds away from the preferred class at the same weight.
 *
 *   3. Load-aware cost (arb_cost pattern, 8 buckets):
 *      charge = f(op, size_bucket), bucket = fb_cost_size_bucket() with
 *      ARB <= 8 buckets.  The fb_cost_tbl map overrides the built-in
 *      table per entry; a 0 (or missing) entry falls back to the
 *      compile-time defaults below, which are PLACEHOLDERS pending local
 *      calibration ("待标定"): read/write 1:4, doubling per size bucket,
 *      scaled x64 so a 4K read costs 64 (same unit convention as
 *      arb_cost, FB_COST_QUANTUM=8 -> budget = w/8 4K reads per round).
 *
 * Verifier-first constraints (first version must load):
 *   - every loop bound is a compile-time constant;
 *   - no runtime division except by constants (8, shifts);
 *   - every map access is lookup + NULL-check; array indices are clamped
 *     to their bounds before indexing the static cost table;
 *   - no spinlocks, no dynamic allocation, no cross-map RMW of
 *     iosched_policy_meta (userspace upsert overwrites the whole entry,
 *     ioext_loader_common.h) -- all runtime counters live in the maps
 *     below.
 */
#ifndef _IOEXT_ARB_FEEDBACK_COMMON_H
#define _IOEXT_ARB_FEEDBACK_COMMON_H

#include "ioext_arb_common.h"

/* ------------------------------------------------------------------ */
/* Compile-time parameter table (fb_cfg map can override the marked *) */
/* ------------------------------------------------------------------ */

#define FB_WINDOW_NS            (100ULL * 1000000ULL) /* eval window      */
#define FB_MIN_SAMPLES          8      /* min completions before a window verdict */

#define FB_LAT_TARGET_NS        (200ULL * 1000ULL)  /* * LC latency target     */
#define FB_LAT_TARGET_MAX_NS    (10ULL * 1000ULL * 1000ULL) /* clamp           */
#define FB_BE_TARGET_MULT       4      /* * low-prio target = LC target * 4 */
#define FB_TARGET_OVER_PCT      50     /* * window violates if over*100 > total*PCT */

#define FB_AIMD_ADD             1      /* * clean-window budget increment */
#define FB_AIMD_DEC_SHIFT       1      /* * violating-window halve        */
#define FB_BUDGET_MIN           4      /* * in-flight budget floor        */
#define FB_BUDGET_MAX           256    /* * in-flight budget cap          */
#define FB_BUDGET_INIT          64     /* starting budget                 */
#define FB_RECOVERY_WINDOWS     3      /* * clean windows to exit PRESSURE */

#define FB_HIGH_PRIO_MAX        2      /* arb_prio <= this is latency-critical */

#define FB_CREDIT_MAX           1024   /* * starvation credit cap          */
#define FB_CREDIT_BOOST_AT      256    /* * credit that triggers boost     */
#define FB_CREDIT_BOOST_MULT    2      /* * boost multiplier (bounded)     */
#define FB_CREDIT_DECAY         4      /* * per-dispatch decay quantum     */

/* Cost: same unit convention as arb_cost (4K read = 64). */
#define FB_COST_QUANTUM         8U
#define FB_COST_MIN_BUDGET      64ULL
#define FB_COST_BUCKETS         8

enum fb_state_kind {
    FB_NORMAL   = 0,
    FB_PRESSURE = 1,
};

/* fb_cfg: single-entry ARRAY map; every 0 field means "use the
 * compile-time default".  Values are re-clamped on every read, so a bad
 * controller write cannot break the invariants. */
struct fb_config {
    __u64 latency_target_ns;   /* LC target; BE = target * FB_BE_TARGET_MULT */
    __u32 budget_min;
    __u32 budget_max;
    __u32 credit_boost_at;
    __u32 credit_max;
    __u32 aimd_add;
    __u32 recovery_windows;
    __u32 reserved;
};

/* Per (cgroup, hctx) feedback state -- SEPARATE map, keyed exactly like
 * arb_tenant_state (identity key: the hctx inserted_request fired with).
 * This map doubles as the acceptance snapshot: `bpftool map dump name
 * fb_state` exposes budget/state/credit without any debug build. */
struct fb_tenant_fb {
    __u32 aimd_budget;      /* AIMD in-flight budget (clamped)          */
    __u32 fb_state;         /* enum fb_state_kind                       */
    __u64 starve_credit;    /* bounded starvation credit                */
    __u64 window_start_ns;
    __u32 window_total;
    __u32 window_over;
    __u32 good_windows;
    __u32 reserved;
    __u64 over_target_cnt;  /* observability: completions over target   */
    __u64 violations;       /* observability: violating windows         */
    __u64 clean_windows;    /* observability: clean windows             */
};

/* ------------------------------------------------------------------ */
/* fb_cfg accessors: fallback-to-default + clamp in one place          */
/* ------------------------------------------------------------------ */

static __always_inline struct fb_config *fb_cfg_get(void *cfg_map)
{
    __u32 zero = 0;
    return bpf_map_lookup_elem(cfg_map, &zero);
}

static __always_inline __u64 fb_latency_target(void *cfg_map, __u32 arb_prio)
{
    struct fb_config *cfg = fb_cfg_get(cfg_map);
    __u64 t = (cfg && cfg->latency_target_ns) ? cfg->latency_target_ns
                                              : FB_LAT_TARGET_NS;
    if (t > FB_LAT_TARGET_MAX_NS)
        t = FB_LAT_TARGET_MAX_NS;
    if (t == 0)
        return 0;
    if (arb_prio > FB_HIGH_PRIO_MAX)
        t *= FB_BE_TARGET_MULT;
    return t;
}

static __always_inline __u32 fb_budget_min(void *cfg_map)
{
    struct fb_config *cfg = fb_cfg_get(cfg_map);
    __u32 v = (cfg && cfg->budget_min) ? cfg->budget_min : FB_BUDGET_MIN;
    if (v < 1)
        v = 1;
    if (v > FB_BUDGET_MAX)
        v = FB_BUDGET_MAX;
    return v;
}

static __always_inline __u32 fb_budget_max(void *cfg_map)
{
    struct fb_config *cfg = fb_cfg_get(cfg_map);
    __u32 v = (cfg && cfg->budget_max) ? cfg->budget_max : FB_BUDGET_MAX;
    __u32 mn = fb_budget_min(cfg_map);
    if (v < mn)
        v = mn;
    return v;
}

static __always_inline __u32 fb_credit_boost_at(void *cfg_map)
{
    struct fb_config *cfg = fb_cfg_get(cfg_map);
    __u32 v = (cfg && cfg->credit_boost_at) ? cfg->credit_boost_at
                                            : FB_CREDIT_BOOST_AT;
    __u32 mx = (cfg && cfg->credit_max) ? cfg->credit_max : FB_CREDIT_MAX;
    if (v > mx)
        v = mx;
    return v ? v : FB_CREDIT_BOOST_AT;
}

static __always_inline __u32 fb_aimd_add(void *cfg_map)
{
    struct fb_config *cfg = fb_cfg_get(cfg_map);
    return (cfg && cfg->aimd_add) ? cfg->aimd_add : FB_AIMD_ADD;
}

static __always_inline __u32 fb_recovery_windows(void *cfg_map)
{
    struct fb_config *cfg = fb_cfg_get(cfg_map);
    return (cfg && cfg->recovery_windows) ? cfg->recovery_windows
                                          : FB_RECOVERY_WINDOWS;
}

/* ------------------------------------------------------------------ */
/* fb_state map lifecycle + EWMA                                       */
/* ------------------------------------------------------------------ */

static __always_inline void fb_ensure_tenant(void *fb_map,
                                             __u64 cgroup_id,
                                             __u32 hctx_idx)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = hctx_idx,
        .reserved = 0,
    };
    struct fb_tenant_fb zero = {};

    zero.aimd_budget = FB_BUDGET_INIT;
    if (!bpf_map_lookup_elem(fb_map, &key))
        bpf_map_update_elem(fb_map, &key, &zero, BPF_ANY);
}

/* EWMA of completion total latency into the EXISTING arb_tenant_state
 * field (same integer smoothing as arb_slo_update_window: 7/8 old + 1/8
 * new, constant division only). */
static __always_inline void fb_update_ewma(struct arb_tenant_state *ts,
                                           __u64 total_latency_ns)
{
    if (!ts)
        return;
    if (ts->ewma_latency_ns == 0)
        ts->ewma_latency_ns = total_latency_ns;
    else
        ts->ewma_latency_ns = (ts->ewma_latency_ns * 7 + total_latency_ns) / 8;
    ts->last_latency_ns = total_latency_ns;
}

/* ------------------------------------------------------------------ */
/* AIMD window FSM (called from completed_request, softirq ctx)        */
/* ------------------------------------------------------------------ */

static __always_inline void fb_budget_clamp(void *cfg_map, __u32 *budget)
{
    __u32 mn = fb_budget_min(cfg_map);
    __u32 mx = fb_budget_max(cfg_map);

    if (*budget < mn)
        *budget = mn;
    if (*budget > mx)
        *budget = mx;
}

static __always_inline void fb_aimd_update(void *cfg_map,
                                           struct fb_tenant_fb *fb,
                                           struct arb_tenant_state *ts,
                                           __u32 arb_prio,
                                           __u64 now_ns,
                                           __u64 total_latency_ns)
{
    __u64 target;
    bool violation;

    if (!fb)
        return;

    fb_update_ewma(ts, total_latency_ns);

    target = fb_latency_target(cfg_map, arb_prio);
    if (target == 0)
        return;

    if (fb->window_start_ns == 0) {
        fb->window_start_ns = now_ns;
        fb->window_total = 0;
        fb->window_over = 0;
    }

    if (now_ns - fb->window_start_ns >= FB_WINDOW_NS) {
        violation = fb->window_total >= FB_MIN_SAMPLES &&
                    (__u64)fb->window_over * 100 >
                        (__u64)fb->window_total * FB_TARGET_OVER_PCT;

        if (violation) {
            /* Multiplicative decrease: halve the budget, clamp to the
             * floor.  Constant shift, no runtime division. */
            __u32 b = fb->aimd_budget >> FB_AIMD_DEC_SHIFT;
            fb->fb_state = FB_PRESSURE;
            fb->good_windows = 0;
            fb->violations++;
            fb->aimd_budget = b ? b : 1;
            fb_budget_clamp(cfg_map, &fb->aimd_budget);
        } else {
            /* Additive increase on every clean window; the state label
             * recovers only after FB_RECOVERY_WINDOWS clean windows
             * (hysteresis so hold cannot flap). */
            if (fb->fb_state != FB_NORMAL) {
                fb->good_windows++;
                if (fb->good_windows >= fb_recovery_windows(cfg_map))
                    fb->fb_state = FB_NORMAL;
            }
            fb->clean_windows++;
            if (fb->aimd_budget < FB_BUDGET_MAX) {
                __u32 b = fb->aimd_budget + fb_aimd_add(cfg_map);
                fb->aimd_budget = b;
                fb_budget_clamp(cfg_map, &fb->aimd_budget);
            }
        }

        fb->window_start_ns = now_ns;
        fb->window_total = 0;
        fb->window_over = 0;
    }

    fb->window_total++;
    if (total_latency_ns > target) {
        fb->window_over++;
        fb->over_target_cnt++;
    }
}

/* ------------------------------------------------------------------ */
/* Starvation credit (bounded, quantum decay)                          */
/* ------------------------------------------------------------------ */

static __always_inline void fb_note_insert_credit(void *cfg_map,
                                                  void *fb_map,
                                                  __u64 cgroup_id,
                                                  __u32 hctx_idx,
                                                  __u32 arb_prio)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = hctx_idx,
        .reserved = 0,
    };
    struct fb_tenant_fb *fb;
    __u64 credit;
    __u64 cap;

    /* Preferred class never needs the starvation lift. */
    if (arb_prio <= FB_HIGH_PRIO_MAX)
        return;

    fb = bpf_map_lookup_elem(fb_map, &key);
    if (!fb)
        return;

    {
        struct fb_config *cfg = fb_cfg_get(cfg_map);
        cap = (cfg && cfg->credit_max) ? cfg->credit_max : FB_CREDIT_MAX;
    }

    credit = fb->starve_credit;
    if (credit >= cap)
        return;
    fb->starve_credit = credit + 1;
}

static __always_inline void fb_note_serve_credit(void *fb_map,
                                                 __u64 cgroup_id,
                                                 __u32 hctx_idx)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgroup_id,
        .hctx_idx = hctx_idx,
        .reserved = 0,
    };
    struct fb_tenant_fb *fb;

    fb = bpf_map_lookup_elem(fb_map, &key);
    if (!fb)
        return;
    if (fb->starve_credit > FB_CREDIT_DECAY)
        fb->starve_credit -= FB_CREDIT_DECAY;
    else
        fb->starve_credit = 0;
}

/* boost callback for arb_drr_pick: bounded multiplier, 1 by default. */
static __always_inline __u32 fb_get_boost(void *cfg_map, void *fb_map,
                                          __u64 cgid, __u32 hctx_idx)
{
    struct arb_tenant_key key = {
        .cgroup_id = cgid,
        .hctx_idx = hctx_idx,
        .reserved = 0,
    };
    struct fb_tenant_fb *fb;

    fb = bpf_map_lookup_elem(fb_map, &key);
    if (!fb)
        return 1;
    if (fb->starve_credit >= fb_credit_boost_at(cfg_map))
        return FB_CREDIT_BOOST_MULT;
    return 1;
}

/* ------------------------------------------------------------------ */
/* Load-aware cost (8 buckets), config-map overridable                 */
/* ------------------------------------------------------------------ */

static __always_inline __u32 fb_cost_size_bucket(__u64 nr_sectors)
{
    __u64 bytes = nr_sectors * 512;

    if (bytes <= 4096)      return 0;
    if (bytes <= 8192)      return 1;
    if (bytes <= 16384)     return 2;
    if (bytes <= 32768)     return 3;
    if (bytes <= 65536)     return 4;
    if (bytes <= 131072)    return 5;
    if (bytes <= 262144)    return 6;
    return 7;
}

static __always_inline __u32 fb_cost_op_type(__u64 opf)
{
    /* REQ_OP_WRITE == 1 in the request opf low byte (same convention as
     * arb_cost_op_type in ioext_arb_common.h). */
    return ((opf & 0xff) == 1) ? 1 : 0;
}

/* Default cost table -- PLACEHOLDER, 待标定: read/write 1:4, doubling
 * per size bucket, x64 scale (4K read = 64).  Calibrate against the
 * local device and publish through fb_cost_tbl (index op*8+bucket); a 0
 * entry means "keep using this default". */
static const __u64 fb_base_cost[2][FB_COST_BUCKETS] = {
    /* read  4K   8K   16K  32K  64K  128K 256K >256K */
            { 64, 128, 256, 512, 1024, 2048, 4096, 8192 },
    /* write */
            { 256, 512, 1024, 2048, 4096, 8192, 16384, 32768 },
};

static __always_inline __u64 fb_cost_estimate(void *tbl_map, __u64 opf,
                                              __u64 nr_sectors)
{
    __u32 op = fb_cost_op_type(opf);
    __u32 bucket = fb_cost_size_bucket(nr_sectors);
    __u64 *e;

    /* Defensive clamps keep the verifier's static bounds proof trivial. */
    if (op >= 2)
        op = 0;
    if (bucket >= FB_COST_BUCKETS)
        bucket = FB_COST_BUCKETS - 1;

    {
        __u32 idx = op * FB_COST_BUCKETS + bucket;
        if (idx >= 2 * FB_COST_BUCKETS)
            idx = 0;
        e = bpf_map_lookup_elem(tbl_map, &idx);
    }
    if (e && *e)
        return *e;
    return fb_base_cost[op][bucket];
}

#endif /* _IOEXT_ARB_FEEDBACK_COMMON_H */
