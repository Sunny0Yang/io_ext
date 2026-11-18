#define _GNU_SOURCE

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <bpf/bpf.h>

#include "ioext_loader_common.h"

struct heimdall_active_snapshot {
    __u64 cgid;
    __u64 queue_depth;
    __u64 dispatches;
    __u64 completions;
    __u32 weight;
};

static volatile sig_atomic_t heimdall_stop;

static void heimdall_sig_handler(int signo)
{
    (void)signo;
    heimdall_stop = 1;
}

static void heimdall_install_signals(void)
{
    signal(SIGINT, heimdall_sig_handler);
    signal(SIGTERM, heimdall_sig_handler);
}

static __u64 heimdall_now_ns(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0;

    return (__u64)ts.tv_sec * 1000000000ULL + (__u64)ts.tv_nsec;
}

static unsigned long heimdall_env_ulong(const char *name, unsigned long fallback)
{
    const char *value = getenv(name);
    char *end = NULL;
    unsigned long parsed;

    if (!value || !value[0])
        return fallback;

    errno = 0;
    parsed = strtoul(value, &end, 0);
    if (errno || end == value)
        return fallback;

    return parsed;
}

static int heimdall_open_pinned_map(const char *path, const char *name)
{
    unsigned int tries;

    for (tries = 0; tries < 100 && !heimdall_stop; tries++) {
        int fd = bpf_obj_get(path);

        if (fd >= 0)
            return fd;
        if (errno != ENOENT) {
            fprintf(stderr, "failed to open %s at %s: %s\n",
                    name, path, strerror(errno));
            return -1;
        }

        usleep(100000);
    }

    fprintf(stderr, "timed out waiting for %s at %s\n", name, path);
    return -1;
}

static int heimdall_update_config(int config_fd,
                                  unsigned long stale_ms,
                                  unsigned long max_hold_us,
                                  unsigned long enable_global_hold)
{
    struct iosched_heimdall_global_config cfg = {
        .stale_timeout_ns = (__u64)stale_ms * 1000000ULL,
        .max_global_hold_ns = (__u64)max_hold_us * 1000ULL,
        .default_epoch = 1,
        .enable_global_hold = enable_global_hold ? 1U : 0U,
    };
    __u32 key = 0;

    if (bpf_map_update_elem(config_fd, &key, &cfg, BPF_ANY)) {
        fprintf(stderr, "failed to update heimdall config: %s\n", strerror(errno));
        return -1;
    }

    return 0;
}

static __u32 heimdall_read_active(int active_fd,
                                  int meta_fd,
                                  int stats_fd,
                                  struct heimdall_active_snapshot *snapshots,
                                  __u32 max_entries)
{
    __u32 count = 0;
    __u32 idx;

    for (idx = 0; idx < IOSCHED_HEIMDALL_ACTIVE_LIMIT && count < max_entries; idx++) {
        struct iosched_heimdall_stats stats = {};
        struct iosched_policy_meta meta = {};
        __u64 cgid = 0;

        if (bpf_map_lookup_elem(active_fd, &idx, &cgid) || !cgid)
            continue;
        if (bpf_map_lookup_elem(meta_fd, &cgid, &meta))
            continue;

        if (stats_fd >= 0 && bpf_map_lookup_elem(stats_fd, &cgid, &stats))
            bpf_map_update_elem(stats_fd, &cgid, &stats, BPF_NOEXIST);

        snapshots[count].cgid = cgid;
        snapshots[count].queue_depth = meta.queue_depth;
        snapshots[count].dispatches = meta.dispatches;
        snapshots[count].completions = stats.completions;
        snapshots[count].weight = meta.arb_weight ? meta.arb_weight : IOSCHED_HEIMDALL_DEFAULT_WEIGHT;
        count++;
    }

    return count;
}

static __u64 heimdall_pick_hold_cgroup(const struct heimdall_active_snapshot *snapshots,
                                       __u32 count,
                                       __u64 epoch)
{
    __u64 best_cgid = 0;
    __u64 best_depth = 0;
    __u32 i;

    if (count < 2)
        return 0;

    for (i = 0; i < count; i++) {
        if (snapshots[i].queue_depth > best_depth) {
            best_depth = snapshots[i].queue_depth;
            best_cgid = snapshots[i].cgid;
        }
    }

    if (best_depth)
        return best_cgid;

    return snapshots[epoch % count].cgid;
}

static int heimdall_publish_policies(int policy_fd,
                                     const struct heimdall_active_snapshot *snapshots,
                                     __u32 count,
                                     __u64 epoch,
                                     __u64 hold_cgid,
                                     unsigned long hold_us)
{
    __u64 now = heimdall_now_ns();
    __u32 i;

    for (i = 0; i < count; i++) {
        struct iosched_heimdall_policy policy = {
            .epoch = epoch,
            .updated_at_ns = now,
            .hold_until_ns = 0,
            .budget_bytes = 0,
            .weight = snapshots[i].weight,
            .flags = IOSCHED_HEIMDALL_POLICY_F_ENABLED,
            .max_hold_us = (__u32)hold_us,
        };
        __u64 cgid = snapshots[i].cgid;

        if (hold_us && cgid == hold_cgid) {
            policy.hold_until_ns = now + (__u64)hold_us * 1000ULL;
            policy.flags |= IOSCHED_HEIMDALL_POLICY_F_HOLD;
        } else {
            policy.flags |= IOSCHED_HEIMDALL_POLICY_F_PREFER;
        }

        if (bpf_map_update_elem(policy_fd, &cgid, &policy, BPF_ANY)) {
            fprintf(stderr, "failed to publish policy for cgid=%llu: %s\n",
                    cgid, strerror(errno));
            return -1;
        }
    }

    return 0;
}

int main(void)
{
    struct heimdall_active_snapshot snapshots[IOSCHED_HEIMDALL_ACTIVE_LIMIT];
    unsigned long interval_ms = heimdall_env_ulong("HEIMDALL_INTERVAL_MS", 200);
    unsigned long hold_us = heimdall_env_ulong("HEIMDALL_HOLD_US", 750);
    unsigned long stale_ms = heimdall_env_ulong("HEIMDALL_STALE_MS", 1000);
    unsigned long max_epochs = heimdall_env_ulong("HEIMDALL_MAX_EPOCHS", 0);
    unsigned long enable_global_hold = heimdall_env_ulong("HEIMDALL_ENABLE_GLOBAL_HOLD", 0);
    int active_fd = -1;
    int meta_fd = -1;
    int policy_fd = -1;
    int stats_fd = -1;
    int config_fd = -1;
    __u64 epoch = 0;
    int ret = 1;

    heimdall_install_signals();

    active_fd = heimdall_open_pinned_map(ARBITER_ARRAY_PIN_PATH, "active_cgroup_ids");
    meta_fd = heimdall_open_pinned_map(ARBITER_META_PIN_PATH, "policy_meta");
    policy_fd = heimdall_open_pinned_map(HEIMDALL_POLICY_PIN_PATH, "heimdall_policy");
    stats_fd = heimdall_open_pinned_map(HEIMDALL_STATS_PIN_PATH, "heimdall_stats");
    config_fd = heimdall_open_pinned_map(HEIMDALL_CONFIG_PIN_PATH, "heimdall_config");
    if (active_fd < 0 || meta_fd < 0 || policy_fd < 0 ||
        stats_fd < 0 || config_fd < 0)
        goto out;

    if (heimdall_update_config(config_fd, stale_ms, hold_us, enable_global_hold))
        goto out;

    printf("HEIMDALL_DAEMON_STARTED interval_ms=%lu hold_us=%lu stale_ms=%lu global_hold=%lu\n",
           interval_ms, hold_us, stale_ms, enable_global_hold);
    fflush(stdout);

    while (!heimdall_stop) {
        __u32 count;
        __u64 hold_cgid;

        epoch++;
        count = heimdall_read_active(active_fd,
                                     meta_fd,
                                     stats_fd,
                                     snapshots,
                                     IOSCHED_HEIMDALL_ACTIVE_LIMIT);
        hold_cgid = heimdall_pick_hold_cgroup(snapshots, count, epoch);
        if (heimdall_publish_policies(policy_fd, snapshots, count, epoch,
                                      hold_cgid, hold_us))
            goto out;

        printf("HEIMDALL_POLICY_EPOCH=%llu active=%u held_cgid=%llu\n",
               epoch, count, hold_cgid);
        fflush(stdout);

        if (max_epochs && epoch >= max_epochs)
            break;

        usleep(interval_ms * 1000UL);
    }

    ret = 0;

out:
    if (active_fd >= 0)
        close(active_fd);
    if (meta_fd >= 0)
        close(meta_fd);
    if (policy_fd >= 0)
        close(policy_fd);
    if (stats_fd >= 0)
        close(stats_fd);
    if (config_fd >= 0)
        close(config_fd);

    printf("HEIMDALL_DAEMON_EXIT status=%d\n", ret);
    fflush(stdout);
    return ret;
}
