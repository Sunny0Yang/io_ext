#ifndef IOSCHED_EXT_LOADER_COMMON_H
#define IOSCHED_EXT_LOADER_COMMON_H

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/bpf.h>
#include <signal.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/btf.h>
#include <bpf/libbpf.h>

#include "ioext_policy_shared.h"

#ifndef IOEXT_DEBUG
#define IOEXT_DEBUG 0
#endif

#if IOEXT_DEBUG
#define IOSCHED_LOADER_DBG(fmt, ...) fprintf(stderr, "ioext: " fmt, ##__VA_ARGS__)
#else
#define IOSCHED_LOADER_DBG(fmt, ...) do { } while (0)
#endif

#define IOSCHED_DEFAULT_ARB_PRIO 7U
#define IOSCHED_DEFAULT_ARB_WEIGHT 100U
#define IOSCHED_LOADER_GRACEFUL_DRAIN_MS 1000U
#define IOSCHED_BYPASS_MODE_NONE   0ULL
#define IOSCHED_BYPASS_MODE_DIRECT 1ULL

#define ARBITER_META_PIN_PATH "/sys/fs/bpf/ioext_policy_meta"
#define ARBITER_ARRAY_PIN_PATH "/sys/fs/bpf/ioext_active_cgroups"
/*
 * Attach-complete marker for the scene-side arbiter-ready gate (Wave-B
 * patch 0005, STALL_FORENSICS.md §4.1.1 seed-1 / leak B1).  The pinned
 * maps above are created BEFORE the BPF load (ensure_pinned_map in
 * register_policy / setup_arbiter_maps), so their existence does NOT
 * prove the arbiter is attached; the kernel publishes current_arbiter
 * only at link-create time.  Requests accepted by the q policy before
 * that publish never reach the arbiter ledger (the insert hook is
 * skipped while current_arbiter is NULL) and stay invisible to a strict
 * pick forever.  This marker is written ONLY after the link exists and
 * is unlinked on every loader exit; a SIGKILL leaves a stale marker,
 * which the scene side pre-cleans via sudo before starting the chain.
 * Fixed path: the loader runs under sudo, whose env_reset drops any
 * environment-based configuration.
 */
#define ARBITER_ATTACH_READY_PATH "/tmp/ioext_arbiter_ready"
/* D1 v4: registered-tenant counter, maintained by the policy loaders
 * under the registration lock; the arbiter's pick fast path fires only
 * when exactly ONE tenant is registered (solo-run gate).  Bumped +1 on
 * register_policy and -1 on unregister_policy_if_owner. */

/*
 * Tenant capacity, shared by: the pinned active-cgroup ARRAY creation size,
 * the pinned policy_meta HASH creation size, the register/unregister slot
 * loops, and (as ARB_MAX_TENANTS in ioext_arb_common.h) the arbiter pick
 * scan space and cursor modulus. All five MUST stay equal; see
 * userspace/io/ARB_LIMITS.md. bpffs rejects regular files, so the
 * registration lock lives on tmpfs.
 */
#define IOSCHED_TENANT_LIMIT   64
#define IOSCHED_REG_LOCK_PATH  "/tmp/.ioext_reg.lock"
#define BFQ_CGROUP_STATE_PIN_PATH "/sys/fs/bpf/ioext_bfq_tenant_state"
#define HEIMDALL_POLICY_PIN_PATH "/sys/fs/bpf/ioext_heimdall_policy"
#define HEIMDALL_STATS_PIN_PATH "/sys/fs/bpf/ioext_heimdall_stats"
#define HEIMDALL_CONFIG_PIN_PATH "/sys/fs/bpf/ioext_heimdall_config"

struct iosched_ext_ops {
    int (*init_queue)(void *ctx);
    int (*exit_queue)(void *ctx);
    int (*insert_request)(void *ctx);
    __u64 (*cgroup_dispatch)(void *ctx);
    __u64 (*evacuate)(void *ctx);
    int (*init_hctx)(void *ctx);
    void (*exit_hctx)(void *ctx);
    bool (*allow_merge)(void *ctx);
    bool (*bio_merge)(void *ctx);
    __u64 (*request_merge)(void *ctx);
    void (*request_merged)(void *ctx);
    void (*requests_merged)(void *ctx);
    __u64 (*limit_depth)(void *ctx);
    void (*depth_updated)(void *ctx);
    void (*prepare_request)(void *ctx);
    void (*finish_request)(void *ctx);
    void (*completed_request)(void *ctx);
    void (*requeue_request)(void *ctx);
    __u64 (*former_request)(void *ctx);
    __u64 (*next_request)(void *ctx);
    void (*init_icq)(void *ctx);
    void (*exit_icq)(void *ctx);
    __u64 bypass_mode;
    char name[16];
};

struct bpf_struct_ops_common {
    __u32 state;
    __u32 refcnt;
};

struct bpf_struct_ops_value_stub {
    struct bpf_struct_ops_common common;
    struct iosched_ext_ops data __attribute__((aligned(64)));
};

struct iosched_loader_policy_progs {
    struct bpf_program *init_queue;
    struct bpf_program *exit_queue;
    struct bpf_program *insert_request;
    struct bpf_program *cgroup_dispatch;
    struct bpf_program *evacuate;
    struct bpf_program *init_hctx;
    struct bpf_program *exit_hctx;
    struct bpf_program *init_icq;
    struct bpf_program *exit_icq;
    struct bpf_program *allow_merge;
    struct bpf_program *bio_merge;
    struct bpf_program *request_merge;
    struct bpf_program *request_merged;
    struct bpf_program *requests_merged;
    struct bpf_program *limit_depth;
    struct bpf_program *depth_updated;
    struct bpf_program *prepare_request;
    struct bpf_program *finish_request;
    struct bpf_program *completed_request;
    struct bpf_program *requeue_request;
    struct bpf_program *former_request;
    struct bpf_program *next_request;
};

struct iosched_arbiter_ops {
    __u64 (*pick_cgroup)(void *ctx);
    bool (*hold_dispatch)(void *ctx);
    void (*completed_request)(void *ctx);
    void (*inserted_request)(void *ctx);
    void (*dispatched_request)(void *ctx);
    void (*dispatch_failed)(void *ctx);
    void (*requeue_refund)(void *ctx);
    __u64 arb_mode;
    char name[16];
};

struct bpf_struct_ops_arbiter_value_stub {
    struct bpf_struct_ops_common common;
    struct iosched_arbiter_ops data __attribute__((aligned(64)));
};

struct iosched_loader_arbiter_progs {
    struct bpf_program *pick_cgroup;
    struct bpf_program *hold_dispatch;
    struct bpf_program *completed_request;
    struct bpf_program *inserted_request;
    struct bpf_program *dispatched_request;
    struct bpf_program *dispatch_failed;
    struct bpf_program *requeue_refund;
};

static int iosched_loader_unregister_policy_if_owner(const char *cgroup_path,
                                                     __u64 owner_token);

static volatile sig_atomic_t iosched_loader_stop;

static void iosched_loader_sig_handler(int signo)
{
    (void)signo;
    iosched_loader_stop = 1;
}

static void iosched_loader_reset_stop(void)
{
    iosched_loader_stop = 0;
}

static void iosched_loader_install_signals(void)
{
    iosched_loader_reset_stop();
    signal(SIGINT, iosched_loader_sig_handler);
    signal(SIGTERM, iosched_loader_sig_handler);
}

static int iosched_loader_link_create(int map_fd, int target_fd)
{
    union bpf_attr attr = {};

    attr.link_create.prog_fd = 0;
    attr.link_create.target_fd = target_fd;
    attr.link_create.map_fd = map_fd;
    attr.link_create.attach_type = BPF_STRUCT_OPS;
    attr.link_create.flags = 0;

    return syscall(__NR_bpf, BPF_LINK_CREATE, &attr, sizeof(attr));
}

static int iosched_loader_open_target(const char *path)
{
    const char *target = path ? path : "/sys/fs/cgroup";
    int fd = open(target, O_RDONLY | O_DIRECTORY | O_CLOEXEC);

    if (fd < 0)
        perror("Failed to open cgroup target");

    return fd;
}

static __u64 iosched_loader_get_cgroup_id(const char *path)
{
    struct {
        struct file_handle handle;
        unsigned char buf[128];
    } fh = { .handle.handle_bytes = sizeof(fh.buf) };
    int mount_id;
    int dirfd;
    __u64 cgid;

    dirfd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (dirfd < 0) {
        perror("open cgroup dir");
        return 0;
    }

    if (name_to_handle_at(dirfd, "", &fh.handle, &mount_id, AT_EMPTY_PATH)) {
        perror("name_to_handle_at");
        close(dirfd);
        return 0;
    }

    close(dirfd);

    if (fh.handle.handle_bytes < sizeof(__u64))
        return 0;

    memcpy(&cgid, fh.handle.f_handle, sizeof(cgid));
	return cgid;
}

static __u64 iosched_loader_owner_token(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return ((__u64)getpid() << 32) ^ (__u64)getppid();

    return ((__u64)getpid() << 32) ^
           ((__u64)ts.tv_sec << 20) ^
           (__u64)ts.tv_nsec;
}

static __u32 iosched_loader_parse_u32(const char *arg, __u32 fallback)
{
    unsigned long val;
    char *end = NULL;

    if (!arg || !*arg)
        return fallback;

    errno = 0;
    val = strtoul(arg, &end, 10);
    if (errno || !end || *end != '\0' || val > UINT_MAX)
        return fallback;

    return (__u32)val;
}

static __u64 iosched_loader_parse_u64(const char *arg, __u64 fallback)
{
    unsigned long long val;
    char *end = NULL;

    if (!arg || !*arg)
        return fallback;

    errno = 0;
    val = strtoull(arg, &end, 10);
    if (errno || !end || *end != '\0')
        return fallback;

    return (__u64)val;
}

static bool iosched_loader_map_matches(int fd,
                                       enum bpf_map_type map_type,
                                       __u32 key_size,
                                       __u32 value_size,
                                       __u32 max_entries)
{
    struct bpf_map_info info = {};
    __u32 info_len = sizeof(info);

    if (bpf_obj_get_info_by_fd(fd, &info, &info_len))
        return false;

    return info.type == map_type &&
           info.key_size == key_size &&
           info.value_size == value_size &&
           info.max_entries == max_entries &&
           info.map_flags == 0;
}

static int iosched_loader_find_single_struct_ops_map(struct bpf_object *obj,
                                                     struct bpf_map **map_out)
{
    struct bpf_map *map;
    struct bpf_map *found = NULL;

    bpf_object__for_each_map(map, obj) {
        if (bpf_map__type(map) != BPF_MAP_TYPE_STRUCT_OPS)
            continue;
        if (found)
            return -EINVAL;
        found = map;
    }

    if (!found)
        return -ENOENT;

    *map_out = found;
    return 0;
}

static struct bpf_program *iosched_loader_find_program_by_section(struct bpf_object *obj,
                                                                  const char *section)
{
    struct bpf_program *prog;

    bpf_object__for_each_program(prog, obj) {
        const char *prog_sec = bpf_program__section_name(prog);

        if (prog_sec && strcmp(prog_sec, section) == 0)
            return prog;
    }

    return NULL;
}

static int iosched_loader_find_policy_programs(struct bpf_object *obj,
                                               struct iosched_loader_policy_progs *progs)
{
    memset(progs, 0, sizeof(*progs));

    progs->init_queue = iosched_loader_find_program_by_section(obj, "struct_ops/init_queue");
    progs->exit_queue = iosched_loader_find_program_by_section(obj, "struct_ops/exit_queue");
    progs->insert_request = iosched_loader_find_program_by_section(obj, "struct_ops/insert_request");
    progs->cgroup_dispatch = iosched_loader_find_program_by_section(obj, "struct_ops/cgroup_dispatch");
    progs->evacuate = iosched_loader_find_program_by_section(obj, "struct_ops/evacuate");
    progs->init_hctx = iosched_loader_find_program_by_section(obj, "struct_ops/init_hctx");
    progs->exit_hctx = iosched_loader_find_program_by_section(obj, "struct_ops/exit_hctx");
    progs->init_icq = iosched_loader_find_program_by_section(obj, "struct_ops/init_icq");
    progs->exit_icq = iosched_loader_find_program_by_section(obj, "struct_ops/exit_icq");
    progs->allow_merge = iosched_loader_find_program_by_section(obj, "struct_ops/allow_merge");
    progs->bio_merge = iosched_loader_find_program_by_section(obj, "struct_ops/bio_merge");
    progs->request_merge = iosched_loader_find_program_by_section(obj, "struct_ops/request_merge");
    progs->request_merged = iosched_loader_find_program_by_section(obj, "struct_ops/request_merged");
    progs->requests_merged = iosched_loader_find_program_by_section(obj, "struct_ops/requests_merged");
    progs->limit_depth = iosched_loader_find_program_by_section(obj, "struct_ops/limit_depth");
    progs->depth_updated = iosched_loader_find_program_by_section(obj, "struct_ops/depth_updated");
    progs->prepare_request = iosched_loader_find_program_by_section(obj, "struct_ops/prepare_request");
    progs->finish_request = iosched_loader_find_program_by_section(obj, "struct_ops/finish_request");
    progs->completed_request = iosched_loader_find_program_by_section(obj, "struct_ops/completed_request");
    progs->requeue_request = iosched_loader_find_program_by_section(obj, "struct_ops/requeue_request");
    progs->former_request = iosched_loader_find_program_by_section(obj, "struct_ops/former_request");
    progs->next_request = iosched_loader_find_program_by_section(obj, "struct_ops/next_request");

    if (!progs->init_queue || !progs->exit_queue || !progs->insert_request ||
        !progs->cgroup_dispatch || !progs->evacuate)
        return -ENOENT;

    return 0;
}

static __u64 iosched_loader_policy_bypass_mode(const char *policy_name)
{
    if (policy_name && strcmp(policy_name, "q_none") == 0)
        return IOSCHED_BYPASS_MODE_DIRECT;

    return IOSCHED_BYPASS_MODE_NONE;
}

static int iosched_loader_find_arbiter_programs(struct bpf_object *obj,
                                                struct iosched_loader_arbiter_progs *progs)
{
    memset(progs, 0, sizeof(*progs));
    progs->pick_cgroup = iosched_loader_find_program_by_section(obj, "struct_ops/pick_cgroup");
    progs->hold_dispatch = iosched_loader_find_program_by_section(obj, "struct_ops/hold_dispatch");
    progs->completed_request = iosched_loader_find_program_by_section(obj, "struct_ops/completed_request");
    progs->inserted_request = iosched_loader_find_program_by_section(obj, "struct_ops/inserted_request");
    progs->dispatched_request = iosched_loader_find_program_by_section(obj, "struct_ops/dispatched_request");
    progs->dispatch_failed = iosched_loader_find_program_by_section(obj, "struct_ops/dispatch_failed");
    progs->requeue_refund = iosched_loader_find_program_by_section(obj, "struct_ops/requeue_refund");
    return progs->pick_cgroup ? 0 : -ENOENT;
}

static int iosched_loader_reuse_pinned_map(struct bpf_object *obj,
                                           const char *map_name,
                                           const char *pin_path)
{
    struct bpf_map *map;
    int fd;

    map = bpf_object__find_map_by_name(obj, map_name);
    if (!map) {
        IOSCHED_LOADER_DBG("reuse map '%s': not found in object\n", map_name);
        return -ENOENT;
    }

    fd = bpf_obj_get(pin_path);
    if (fd < 0) {
        IOSCHED_LOADER_DBG("reuse map '%s': bpf_obj_get('%s') failed: %s\n",
                           map_name, pin_path, strerror(errno));
        return -errno;
    }

    if (bpf_map__reuse_fd(map, fd)) {
        IOSCHED_LOADER_DBG("reuse map '%s': bpf_map__reuse_fd failed for '%s': %s\n",
                           map_name, pin_path, strerror(errno));
        close(fd);
        return -errno;
    }

    return 0;
}

static int iosched_loader_create_pinned_map(enum bpf_map_type map_type,
                                            __u32 key_size,
                                            __u32 value_size,
                                            __u32 max_entries,
                                            const char *pin_path)
{
    union bpf_attr attr = {};
    int fd;

    attr.map_type = map_type;
    attr.key_size = key_size;
    attr.value_size = value_size;
    attr.max_entries = max_entries;

    fd = syscall(__NR_bpf, BPF_MAP_CREATE, &attr, sizeof(attr));
    if (fd < 0) {
        IOSCHED_LOADER_DBG("create pinned map '%s' failed: %s\n",
                           pin_path, strerror(errno));
        return -errno;
    }

    if (bpf_obj_pin(fd, pin_path)) {
        int pin_errno = errno;
        int err = -pin_errno;
        int pinned_fd;

        if (pin_errno == EEXIST) {
            pinned_fd = bpf_obj_get(pin_path);
            if (pinned_fd >= 0) {
                if (!iosched_loader_map_matches(pinned_fd,
                                                map_type,
                                                key_size,
                                                value_size,
                                                max_entries)) {
                    IOSCHED_LOADER_DBG("pin map '%s' exists with incompatible attributes\n",
                                       pin_path);
                    close(pinned_fd);
                    close(fd);
                    return -EINVAL;
                }
                close(pinned_fd);
                close(fd);
                return 0;
            }
        }

        IOSCHED_LOADER_DBG("pin map '%s' failed: %s\n",
                           pin_path, strerror(pin_errno));
        close(fd);
        return err;
    }

    close(fd);
    return 0;
}

static int iosched_loader_ensure_pinned_map(const char *pin_path,
                                            enum bpf_map_type map_type,
                                            __u32 key_size,
                                            __u32 value_size,
                                            __u32 max_entries)
{
    int fd;

    fd = bpf_obj_get(pin_path);
    if (fd >= 0) {
        if (!iosched_loader_map_matches(fd,
                                        map_type,
                                        key_size,
                                        value_size,
                                        max_entries)) {
            IOSCHED_LOADER_DBG("ensure pinned map '%s' found incompatible attributes\n",
                               pin_path);
            close(fd);
            return -EINVAL;
        }
        close(fd);
        return 0;
    }

    if (errno != ENOENT) {
        IOSCHED_LOADER_DBG("ensure pinned map '%s' failed to open existing pin: %s\n",
                           pin_path, strerror(errno));
        return -errno;
    }

    return iosched_loader_create_pinned_map(map_type,
                                            key_size,
                                            value_size,
                                            max_entries,
                                            pin_path);
}

static int iosched_loader_setup_policy_maps(struct bpf_object *obj)
{
    int err;

    err = iosched_loader_ensure_pinned_map(ARBITER_META_PIN_PATH,
                                           BPF_MAP_TYPE_HASH,
                                           sizeof(__u64),
                                           sizeof(struct iosched_policy_meta),
                                           IOSCHED_TENANT_LIMIT);
    if (err) {
        IOSCHED_LOADER_DBG("setup policy maps: ensure pinned meta failed: %d\n", err);
        return err;
    }

    return iosched_loader_reuse_pinned_map(obj, "policy_meta", ARBITER_META_PIN_PATH);
}

static int iosched_loader_setup_bfq_state_map(struct bpf_object *obj)
{
    int err;

    err = iosched_loader_ensure_pinned_map(BFQ_CGROUP_STATE_PIN_PATH,
                                           BPF_MAP_TYPE_HASH,
                                           sizeof(struct iosched_bfq_tenant_key),
                                           sizeof(struct iosched_bfq_cgroup_state),
                                           IOSCHED_BFQ_ACTIVE_FLOW_LIMIT);
    if (err)
        return err;

    return iosched_loader_reuse_pinned_map(obj,
                                           "bfq_cgroup_state",
                                           BFQ_CGROUP_STATE_PIN_PATH);
}

static int iosched_loader_pin_map(struct bpf_object *obj,
                                  const char *map_name,
                                  const char *pin_path)
{
    struct bpf_map *map;

    map = bpf_object__find_map_by_name(obj, map_name);
    if (!map)
        return -ENOENT;

    unlink(pin_path);
    if (bpf_map__pin(map, pin_path))
        return -errno;

    return 0;
}

static int iosched_loader_setup_arbiter_maps(struct bpf_object *obj)
{
    int err;

    err = iosched_loader_ensure_pinned_map(ARBITER_ARRAY_PIN_PATH,
                                           BPF_MAP_TYPE_ARRAY,
                                           sizeof(__u32),
                                           sizeof(__u64),
                                           IOSCHED_TENANT_LIMIT);
    if (err)
        return err;

    err = iosched_loader_reuse_pinned_map(obj, "active_cgroup_ids", ARBITER_ARRAY_PIN_PATH);
    if (err)
        return err;

    err = iosched_loader_ensure_pinned_map(ARBITER_META_PIN_PATH,
                                           BPF_MAP_TYPE_HASH,
                                           sizeof(__u64),
                                           sizeof(struct iosched_policy_meta),
                                           IOSCHED_TENANT_LIMIT);
    if (err)
        return err;

    err = iosched_loader_reuse_pinned_map(obj, "policy_meta", ARBITER_META_PIN_PATH);
    if (err)
        return err;
    return 0;
}

static void iosched_loader_teardown_arbiter_maps(void)
{
}

static void iosched_loader_teardown_heimdall_maps(void)
{
    unlink(HEIMDALL_POLICY_PIN_PATH);
    unlink(HEIMDALL_STATS_PIN_PATH);
    unlink(HEIMDALL_CONFIG_PIN_PATH);
}

static int iosched_loader_upsert_policy_meta(const char *cgroup_path,
                                             __u32 arb_prio,
                                             __u32 arb_weight,
                                             __u64 owner_token)
{
    struct iosched_policy_meta meta = {
        .owner_token = owner_token,
        .arb_prio = arb_prio,
        .arb_weight = arb_weight,
    };
    const char *cg_path = cgroup_path ? cgroup_path : "/sys/fs/cgroup";
    __u64 cgid;
    int meta_fd;

    cgid = iosched_loader_get_cgroup_id(cg_path);
    if (!cgid)
        return -ENOENT;

    meta_fd = bpf_obj_get(ARBITER_META_PIN_PATH);
    if (meta_fd < 0) {
        if (errno == ENOENT)
            return 0;
        return -errno;
    }

    if (bpf_map_update_elem(meta_fd, &cgid, &meta, BPF_ANY)) {
        int err = -errno;
        close(meta_fd);
        return err;
    }

    close(meta_fd);
    return 0;
}

static int iosched_loader_lookup_policy_meta(__u64 cgid,
                                             struct iosched_policy_meta *meta)
{
    int meta_fd;
    int err;

    meta_fd = bpf_obj_get(ARBITER_META_PIN_PATH);
    if (meta_fd < 0)
        return -errno;

    err = bpf_map_lookup_elem(meta_fd, &cgid, meta) ? -errno : 0;
    close(meta_fd);
    return err;
}

static int iosched_loader_update_policy_flags(const char *cgroup_path,
                                              __u64 owner_token,
                                              __u32 set_flags,
                                              __u32 clear_flags)
{
    const char *cg_path = cgroup_path ? cgroup_path : "/sys/fs/cgroup";
    struct iosched_policy_meta meta = {};
    __u64 cgid;
    int meta_fd;

    cgid = iosched_loader_get_cgroup_id(cg_path);
    if (!cgid)
        return -ENOENT;

    meta_fd = bpf_obj_get(ARBITER_META_PIN_PATH);
    if (meta_fd < 0)
        return -errno;

    if (bpf_map_lookup_elem(meta_fd, &cgid, &meta)) {
        int err = -errno;
        close(meta_fd);
        return err;
    }

    if (owner_token && meta.owner_token != owner_token) {
        close(meta_fd);
        return 0;
    }

    meta.flags |= set_flags;
    meta.flags &= ~clear_flags;

    if (bpf_map_update_elem(meta_fd, &cgid, &meta, BPF_ANY)) {
        int err = -errno;
        close(meta_fd);
        return err;
    }

    close(meta_fd);
    return 0;
}

static int iosched_loader_mark_policy_draining(const char *cgroup_path)
{
    return iosched_loader_update_policy_flags(cgroup_path,
                                              0,
                                              IOSCHED_POLICY_META_F_DRAINING,
                                              0);
}

static int iosched_loader_mark_policy_draining_if_owner(const char *cgroup_path,
                                                        __u64 owner_token)
{
    return iosched_loader_update_policy_flags(cgroup_path,
                                              owner_token,
                                              IOSCHED_POLICY_META_F_DRAINING,
                                              0);
}

static int iosched_loader_wait_policy_quiesced(const char *cgroup_path,
                                               unsigned int timeout_ms,
                                               __u64 owner_token)
{
    const char *cg_path = cgroup_path ? cgroup_path : "/sys/fs/cgroup";
    __u64 cgid;
    unsigned int waited_ms = 0;

    cgid = iosched_loader_get_cgroup_id(cg_path);
    if (!cgid)
        return -ENOENT;

    while (!timeout_ms || waited_ms < timeout_ms) {
        struct iosched_policy_meta meta = {};
        int err;

        err = iosched_loader_lookup_policy_meta(cgid, &meta);
        if (err == -ENOENT)
            return 0;
        if (!err && owner_token && meta.owner_token != owner_token)
            return 0;
        if (!err && meta.queue_depth == 0 && meta.active_inserts == 0)
            return 0;

        usleep(20000);
        waited_ms += 20;
    }

    return -ETIMEDOUT;
}

static int iosched_loader_wait_all_policies_quiesced(unsigned int timeout_ms)
{
    unsigned int waited_ms = 0;

    while (waited_ms < timeout_ms) {
        int meta_fd;
        __u64 key;
        __u64 next_key;
        bool found_work = false;

        meta_fd = bpf_obj_get(ARBITER_META_PIN_PATH);
        if (meta_fd < 0) {
            if (errno == ENOENT)
                return 0;
            return -errno;
        }

        if (bpf_map_get_next_key(meta_fd, NULL, &next_key) == 0) {
            do {
                struct iosched_policy_meta meta = {};

                key = next_key;
                if (!bpf_map_lookup_elem(meta_fd, &key, &meta) && meta.queue_depth > 0) {
                    found_work = true;
                    break;
                }
            } while (bpf_map_get_next_key(meta_fd, &key, &next_key) == 0);
        }

        close(meta_fd);

        if (!found_work)
            return 0;

        usleep(20000);
        waited_ms += 20;
    }

    return -ETIMEDOUT;
}

static int iosched_loader_registration_lock(int *lock_fd)
{
    int fd;

    *lock_fd = -1;
    fd = open(IOSCHED_REG_LOCK_PATH, O_CREAT | O_RDWR, 0600);
    if (fd < 0)
        return -errno;
    if (flock(fd, LOCK_EX)) {
        int err = -errno;

        close(fd);
        return err;
    }
    *lock_fd = fd;
    return 0;
}

static void iosched_loader_registration_unlock(int lock_fd)
{
    if (lock_fd < 0)
        return;
    flock(lock_fd, LOCK_UN);
    close(lock_fd);
}

static int iosched_loader_register_active_cgroup(const char *cgroup_path)
{
    const char *cg_path = cgroup_path ? cgroup_path : "/sys/fs/cgroup";
    __u64 cgid;
    int array_fd;
    int lock_fd;
    int err;
    __u32 i;

    cgid = iosched_loader_get_cgroup_id(cg_path);
    if (!cgid)
        return -ENOENT;

    array_fd = bpf_obj_get(ARBITER_ARRAY_PIN_PATH);
    if (array_fd < 0) {
        if (errno == ENOENT)
            return 0;
        return -errno;
    }

    /*
     * Claiming a slot is read-then-write on the shared ARRAY: without
     * serialization two loaders that read the same empty slot overwrite
     * each other and one cgroup silently vanishes from the arbiter's
     * pick scan, starving all of its queued requests under strict mode.
     */
    err = iosched_loader_registration_lock(&lock_fd);
    if (err) {
        close(array_fd);
        return err;
    }

    for (i = 0; i < IOSCHED_TENANT_LIMIT; i++) {
        __u64 val = 0;

        if (bpf_map_lookup_elem(array_fd, &i, &val))
            continue;
        if (val == cgid) {
            close(array_fd);
            iosched_loader_registration_unlock(lock_fd);
            return 0;
        }
        if (val == 0) {
            if (bpf_map_update_elem(array_fd, &i, &cgid, BPF_ANY)) {
                err = -errno;
                close(array_fd);
                iosched_loader_registration_unlock(lock_fd);
                return err;
            }
            close(array_fd);
            iosched_loader_registration_unlock(lock_fd);
            return 0;
        }
    }

    close(array_fd);
    iosched_loader_registration_unlock(lock_fd);
    return -ENOSPC;
}

static int iosched_loader_unregister_policy(const char *cgroup_path)
{
    return iosched_loader_unregister_policy_if_owner(cgroup_path, 0);
}

static int iosched_loader_unregister_policy_if_owner(const char *cgroup_path,
                                                     __u64 owner_token)
{
    const char *cg_path = cgroup_path ? cgroup_path : "/sys/fs/cgroup";
    __u64 cgid;
    int meta_fd = -1;
    int array_fd = -1;
    int lock_fd;
    int err;
    __u32 i;

    cgid = iosched_loader_get_cgroup_id(cg_path);
    if (!cgid)
        return -ENOENT;

    err = iosched_loader_registration_lock(&lock_fd);
    if (err)
        return err;

    meta_fd = bpf_obj_get(ARBITER_META_PIN_PATH);
    if (meta_fd >= 0) {
        struct iosched_policy_meta meta = {};

        if (!bpf_map_lookup_elem(meta_fd, &cgid, &meta) &&
            (!owner_token || meta.owner_token == owner_token))
            bpf_map_delete_elem(meta_fd, &cgid);
        close(meta_fd);
    }

	meta_fd = bpf_obj_get(BFQ_CGROUP_STATE_PIN_PATH);
	if (meta_fd >= 0) {
		struct iosched_bfq_tenant_key key;
		struct iosched_bfq_tenant_key next_key;

		if (bpf_map_get_next_key(meta_fd, NULL, &next_key) == 0) {
			do {
				key = next_key;
				if (key.cgroup_id == cgid)
					bpf_map_delete_elem(meta_fd, &key);
			} while (bpf_map_get_next_key(meta_fd, &key, &next_key) == 0);
		}
		close(meta_fd);
		meta_fd = -1;
	}

    array_fd = bpf_obj_get(ARBITER_ARRAY_PIN_PATH);
    if (array_fd >= 0) {
        for (i = 0; i < IOSCHED_TENANT_LIMIT; i++) {
            __u64 val = 0;

            if (bpf_map_lookup_elem(array_fd, &i, &val))
                continue;
            if (val != cgid)
                continue;
            if (owner_token) {
                struct iosched_policy_meta meta = {};

                meta_fd = bpf_obj_get(ARBITER_META_PIN_PATH);
                if (meta_fd >= 0) {
                    if (!bpf_map_lookup_elem(meta_fd, &cgid, &meta) &&
                        meta.owner_token != owner_token) {
                        close(meta_fd);
                        break;
                    }
                    close(meta_fd);
                    meta_fd = -1;
                }
            }
            val = 0;
            bpf_map_update_elem(array_fd, &i, &val, BPF_ANY);
            break;
        }
        close(array_fd);
    }

    iosched_loader_registration_unlock(lock_fd);
    return 0;
}

/* D1 v4: adjust the pinned registered-tenant counter by delta (+1/-1).
 * Must be called under the registration lock (or from a path that
 * already holds it).  A missing pin is tolerated as count-unknown: the
 * arbiter treats a failed read as gate-closed (no fast path). */

static int iosched_loader_register_policy(const char *cgroup_path,
                                          __u32 arb_prio,
                                          __u32 arb_weight,
                                          __u64 owner_token)
{
    int err;

    err = iosched_loader_ensure_pinned_map(ARBITER_META_PIN_PATH,
                                           BPF_MAP_TYPE_HASH,
                                           sizeof(__u64),
                                           sizeof(struct iosched_policy_meta),
                                           IOSCHED_TENANT_LIMIT);
    if (err)
        return err;

    err = iosched_loader_ensure_pinned_map(ARBITER_ARRAY_PIN_PATH,
                                           BPF_MAP_TYPE_ARRAY,
                                           sizeof(__u32),
                                           sizeof(__u64),
                                           IOSCHED_TENANT_LIMIT);
    if (err)
        return err;

    err = iosched_loader_upsert_policy_meta(cgroup_path,
                                            arb_prio,
                                            arb_weight,
                                            owner_token);
    if (err)
        return err;

    return iosched_loader_register_active_cgroup(cgroup_path);
}

static int iosched_loader_attach_and_run(int map_fd,
                                         const struct iosched_loader_policy_progs *progs,
                                         const char *policy_name,
                                         const char *cgroup_path,
                                         __u64 owner_token)
{
    struct bpf_map_info info = {};
    struct bpf_struct_ops_value_stub *stub;
    __u32 info_len = sizeof(info);
    int cgroup_fd = -1;
    int link_fd = -1;
    void *buffer;
    int ret = 1;

    if (bpf_obj_get_info_by_fd(map_fd, &info, &info_len)) {
        IOSCHED_LOADER_DBG("policy '%s': bpf_obj_get_info_by_fd failed: %s\n",
                           policy_name, strerror(errno));
        goto out_unregister;
    }

    buffer = calloc(1, info.value_size);
    if (!buffer) {
        IOSCHED_LOADER_DBG("policy '%s': calloc(%u) failed\n",
                           policy_name, info.value_size);
        goto out_unregister;
    }

    stub = buffer;
    if (progs->init_queue)
        stub->data.init_queue = (void *)((long)bpf_program__fd(progs->init_queue));
    if (progs->exit_queue)
        stub->data.exit_queue = (void *)((long)bpf_program__fd(progs->exit_queue));
    if (progs->insert_request)
        stub->data.insert_request = (void *)((long)bpf_program__fd(progs->insert_request));
    if (progs->cgroup_dispatch)
        stub->data.cgroup_dispatch = (void *)((long)bpf_program__fd(progs->cgroup_dispatch));
    if (progs->evacuate)
        stub->data.evacuate = (void *)((long)bpf_program__fd(progs->evacuate));
    if (progs->init_hctx)
        stub->data.init_hctx = (void *)((long)bpf_program__fd(progs->init_hctx));
    if (progs->exit_hctx)
        stub->data.exit_hctx = (void *)((long)bpf_program__fd(progs->exit_hctx));
    if (progs->init_icq)
        stub->data.init_icq = (void *)((long)bpf_program__fd(progs->init_icq));
    if (progs->exit_icq)
        stub->data.exit_icq = (void *)((long)bpf_program__fd(progs->exit_icq));
    if (progs->allow_merge)
        stub->data.allow_merge = (void *)((long)bpf_program__fd(progs->allow_merge));
    if (progs->bio_merge)
        stub->data.bio_merge = (void *)((long)bpf_program__fd(progs->bio_merge));
    if (progs->request_merge)
        stub->data.request_merge = (void *)((long)bpf_program__fd(progs->request_merge));
    if (progs->request_merged)
        stub->data.request_merged = (void *)((long)bpf_program__fd(progs->request_merged));
    if (progs->requests_merged)
        stub->data.requests_merged = (void *)((long)bpf_program__fd(progs->requests_merged));
    if (progs->limit_depth)
        stub->data.limit_depth = (void *)((long)bpf_program__fd(progs->limit_depth));
    if (progs->depth_updated)
        stub->data.depth_updated = (void *)((long)bpf_program__fd(progs->depth_updated));
    if (progs->prepare_request)
        stub->data.prepare_request = (void *)((long)bpf_program__fd(progs->prepare_request));
    if (progs->finish_request)
        stub->data.finish_request = (void *)((long)bpf_program__fd(progs->finish_request));
    if (progs->completed_request)
        stub->data.completed_request = (void *)((long)bpf_program__fd(progs->completed_request));
    if (progs->requeue_request)
        stub->data.requeue_request = (void *)((long)bpf_program__fd(progs->requeue_request));
    if (progs->former_request)
        stub->data.former_request = (void *)((long)bpf_program__fd(progs->former_request));
    if (progs->next_request)
        stub->data.next_request = (void *)((long)bpf_program__fd(progs->next_request));

    stub->data.bypass_mode = iosched_loader_policy_bypass_mode(policy_name);

    strncpy(stub->data.name, policy_name, sizeof(stub->data.name) - 1);

    if (bpf_map_update_elem(map_fd, &(int){0}, buffer, 0)) {
        IOSCHED_LOADER_DBG("policy '%s': struct_ops map update failed: %s\n",
                           policy_name, strerror(errno));
        free(buffer);
        goto out_unregister;
    }
    free(buffer);

    cgroup_fd = iosched_loader_open_target(cgroup_path);
    if (cgroup_fd < 0) {
        IOSCHED_LOADER_DBG("policy '%s': open target '%s' failed: %d\n",
                           policy_name, cgroup_path, cgroup_fd);
        goto out_unregister;
    }

    link_fd = iosched_loader_link_create(map_fd, cgroup_fd);
    if (link_fd < 0) {
        IOSCHED_LOADER_DBG("policy '%s': link create failed: %s\n",
                           policy_name, strerror(errno));
        goto out_close;
    }

    iosched_loader_install_signals();
    while (!iosched_loader_stop)
        sleep(1);

    ret = 0;
out_close:
    iosched_loader_mark_policy_draining_if_owner(cgroup_path, owner_token);
    if (link_fd >= 0)
        bpf_link_detach(link_fd);
    if (iosched_loader_wait_policy_quiesced(cgroup_path,
                                            IOSCHED_LOADER_GRACEFUL_DRAIN_MS,
                                            owner_token) == -ETIMEDOUT) {
        IOSCHED_LOADER_DBG("policy '%s' timed out waiting for graceful drain, forcing detach\n",
                           policy_name);
    }
    if (link_fd >= 0)
        close(link_fd);
    if (cgroup_fd >= 0)
        close(cgroup_fd);
    iosched_loader_unregister_policy_if_owner(cgroup_path, owner_token);
    return ret;
out_unregister:
    if (ret)
        iosched_loader_unregister_policy_if_owner(cgroup_path, owner_token);
    return ret;
}

static int iosched_loader_policy_attach_and_run(struct bpf_object *obj,
                                                struct bpf_map *struct_ops_map,
                                                const char *policy_name,
                                                const char *cgroup_path,
                                                __u64 owner_token)
{
    struct iosched_loader_policy_progs progs;

    if (iosched_loader_find_policy_programs(obj, &progs)) {
        IOSCHED_LOADER_DBG("policy '%s': failed to find required struct_ops programs\n",
                           policy_name);
        return 1;
    }

    return iosched_loader_attach_and_run(bpf_map__fd(struct_ops_map),
                                         &progs,
                                         policy_name,
                                         cgroup_path,
                                         owner_token);
}

/*
 * ---------------------------------------------------------------------------
 * Arbiter ledger diagnostics (arb_recovery tune3; --diag-interval-ms).
 *
 * The dump below is the direct observation instrument for the count-truth
 * ledger drift STALL_FORENSICS.md §3.4 pinned as the residual blind spot
 * of the S13 criterion: cgroup_drr.pending_total (selection truth) vs
 * policy_meta.queue_depth (the q policy's own map-entry count) vs
 * cgroup_outstanding.count (the S11.1 demand signal) must agree in
 * steady state, and every divergence is a leak signature (B1 attach-race
 * stranded entries: qdepth > pend; B2 bypass-requeue: out sagging below
 * pend+inflight truth; B3 evacuate-invalid / BUG-005 crystallized
 * pending: pend > qdepth with voids climbing until the B3 heal fires).
 *
 * The mirrors below MUST stay field-for-field identical to the BPF-side
 * structs in ioext_arb_common.h (same order, same types).  Each dump
 * verifies the live map value_size against the mirror and suppresses
 * the field decode (printing -1) on mismatch, so an ABI drift is LOUD
 * instead of silently misread.
 * ---------------------------------------------------------------------------
 */

/* Mirror of struct arb_cgroup_drr (ioext_arb_common.h). */
struct iosched_arb_cgroup_drr_mirror {
    __s64 deficit;
    __u64 refilled_round;
    __u64 pending_total;
    __u64 own_max_charge;
    __u64 dispatches;
    __u64 last_serve_dispatches;
    __u64 serve_selects;
    __u64 refill_arms;
    __u64 skip_pending_zero;
    __u64 fastpath_selects;
    __u32 serve_no_progress;
    __u32 dispatch_fails;
    __u32 void_turns;
    __u32 reserved;
    __u64 last_active_ns;
};

/* Mirror of struct arb_cgroup_outstanding (ioext_arb_common.h). */
struct iosched_arb_cgroup_outstanding_mirror {
    __u64 count;
    __u64 last_activity_ns;
};

/* Mirror of struct arb_global_state (ioext_arb_common.h). */
struct iosched_arb_global_state_mirror {
    __u64 current_round;
    __u64 ew_mult;
    /* S14 fuse gauge: maintained mirror of sum(pending_total); the diag
     * prints it (tot_pend) next to the scan-derived pend_sum so the
     * counter invariant is assertable live. */
    __u64 total_pending;
};

/* Mirror of struct arb_hctx_state (ioext_arb_common.h). */
struct iosched_arb_hctx_state_mirror {
    __u32 cursor;
    __u32 last_selected_idx;
    __u64 round_dispatches;
    __u64 skip_no_meta;
    __u64 skip_no_ts;
    __u64 round_holds;
    /* S14 scratch (see struct arb_hctx_state): size parity only, never
     * decoded. */
    __u64 stranded_cgid;
};

/*
 * Maps an arbiter loader wants dumped.  Every member except
 * active_cgroup_ids/policy_meta is optional: pass NULL when the arbiter
 * carries no such map (arb_noop has no ledger), and the dump prints the
 * q-policy depth only.
 */
struct iosched_arbiter_diag_maps {
    struct bpf_map *active_cgroup_ids;
    struct bpf_map *policy_meta;
    struct bpf_map *cgroup_drr;
    struct bpf_map *cgroup_outstanding;
    struct bpf_map *global_state;
    struct bpf_map *hctx_state;
    struct bpf_map *requeue_replay;
    __u32 interval_ms;
};

static void iosched_loader_monotonic_ns(__u64 *ns_out)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        *ns_out = 0;
        return;
    }
    *ns_out = ((__u64)ts.tv_sec * 1000000000ULL) + (__u64)ts.tv_nsec;
}

/* bpf_ktime_get_ns() is ktime_get_ns() == CLOCK_MONOTONIC, so ages
 * computed against the loader's monotonic clock are directly
 * comparable with the BPF-side stamps. */
static __u64 iosched_loader_diag_value_size(struct bpf_map *map, int *fd_out)
{
    struct bpf_map_info info = {};
    __u32 info_len = sizeof(info);
    int fd = bpf_map__fd(map);

    *fd_out = fd;
    if (fd < 0)
        return 0;
    if (bpf_obj_get_info_by_fd(fd, &info, &info_len))
        return 0;
    return info.value_size;
}

static int iosched_loader_diag_check_size(struct bpf_map *map,
                                          size_t mirror_size,
                                          int *fd_out,
                                          int *first_mismatch,
                                          const char *map_name)
{
    __u32 vsize = (__u32)iosched_loader_diag_value_size(map, fd_out);

    if (vsize == mirror_size)
        return 1;
    if (*first_mismatch) {
        fprintf(stderr,
                "arb_diag MIRROR-SIZE-MISMATCH map=%s value_size=%u expected=%zu"
                " -- ioext_loader_common.h mirror drifted from ioext_arb_common.h;"
                " field decode suppressed\n",
                map_name, vsize, mirror_size);
        *first_mismatch = 0;
    }
    return 0;
}

static void iosched_loader_arb_diag_dump(struct iosched_arbiter_diag_maps *diag,
                                         const char *tag)
{
    __u64 now_ns = 0;
    __u64 t_s;
    __u64 t_ms;
    long long round = -1;
    long long ew_mult = -1;
    long long tot_pend = -1;
    unsigned long long pend_sum = 0;
    unsigned long long out_sum = 0;
    long long qdepth_sum = 0;
    unsigned long long holds_sum = 0;
    unsigned nr_cg = 0;
    unsigned long long replay_nr = 0;
    __u32 idx;
    int mismatch_note = 1;
    int drr_fd = -1, out_fd = -1, glob_fd = -1, hctx_fd = -1;
    int drr_ok = 0, out_ok = 0, glob_ok = 0, hctx_ok = 0;
    int act_fd = -1, meta_fd = -1, replay_fd = -1;

    if (!diag)
        return;

    iosched_loader_monotonic_ns(&now_ns);
    t_s = now_ns / 1000000000ULL;
    t_ms = (now_ns % 1000000000ULL) / 1000000ULL;

    if (diag->cgroup_drr)
        drr_ok = iosched_loader_diag_check_size(diag->cgroup_drr,
                                                sizeof(struct iosched_arb_cgroup_drr_mirror),
                                                &drr_fd, &mismatch_note,
                                                "cgroup_drr");
    if (diag->cgroup_outstanding)
        out_ok = iosched_loader_diag_check_size(diag->cgroup_outstanding,
                                                sizeof(struct iosched_arb_cgroup_outstanding_mirror),
                                                &out_fd, &mismatch_note,
                                                "cgroup_outstanding");
    if (diag->global_state)
        glob_ok = iosched_loader_diag_check_size(diag->global_state,
                                                 sizeof(struct iosched_arb_global_state_mirror),
                                                 &glob_fd, &mismatch_note,
                                                 "global_state");
    if (diag->hctx_state)
        hctx_ok = iosched_loader_diag_check_size(diag->hctx_state,
                                                 sizeof(struct iosched_arb_hctx_state_mirror),
                                                 &hctx_fd, &mismatch_note,
                                                 "hctx_state");
    act_fd = diag->active_cgroup_ids ? bpf_map__fd(diag->active_cgroup_ids) : -1;
    meta_fd = diag->policy_meta ? bpf_map__fd(diag->policy_meta) : -1;
    replay_fd = diag->requeue_replay ? bpf_map__fd(diag->requeue_replay) : -1;

    if (glob_ok) {
        struct iosched_arb_global_state_mirror gs = {};
        __u32 gkey = 0;

        if (!bpf_map_lookup_elem(glob_fd, &gkey, &gs)) {
            round = (long long)gs.current_round;
            ew_mult = (long long)gs.ew_mult;
            tot_pend = (long long)(__s64)gs.total_pending;
        }
    }

    if (hctx_ok) {
        for (idx = 0; idx < 64; idx++) {
            struct iosched_arb_hctx_state_mirror hs = {};

            if (bpf_map_lookup_elem(hctx_fd, &idx, &hs))
                break; /* array maps are dense; first miss = past the end */
            holds_sum += hs.round_holds;
            if (hs.round_holds) {
                fprintf(stderr,
                        "arb_diag hctx=%u cursor=%u last_sel=%u holds=%llu rdisp=%llu\n",
                        idx, hs.cursor, hs.last_selected_idx,
                        hs.round_holds, hs.round_dispatches);
            }
        }
    }

    if (replay_fd >= 0) {
        __u64 key = 0;
        __u64 next_key = 0;
        __u64 val_buf[16] = {};

        if (bpf_map_get_next_key(replay_fd, NULL, &next_key) == 0) {
            do {
                key = next_key;
                replay_nr++;
                (void)bpf_map_lookup_elem(replay_fd, &key, val_buf);
            } while (bpf_map_get_next_key(replay_fd, &key, &next_key) == 0);
        }
    }

    if (act_fd >= 0) {
        for (idx = 0; idx < 64; idx++) {
            __u64 cgid = 0;
            struct iosched_policy_meta meta = {};
            struct iosched_arb_cgroup_drr_mirror cd = {};
            struct iosched_arb_cgroup_outstanding_mirror out = {};
            int have_cd = 0;
            long long pend = -1;
            long long out_cnt = -1;
            long long qdepth = -1;
            long long out_age_ms = -1;

            if (bpf_map_lookup_elem(act_fd, &idx, &cgid) || !cgid)
                continue;
            nr_cg++;

            if (meta_fd >= 0 &&
                !bpf_map_lookup_elem(meta_fd, &cgid, &meta)) {
                qdepth = (long long)meta.queue_depth;
                qdepth_sum += qdepth;
            }
            if (drr_ok && !bpf_map_lookup_elem(drr_fd, &cgid, &cd)) {
                have_cd = 1;
                pend = (long long)cd.pending_total;
                pend_sum += cd.pending_total;
            }
            if (out_ok && !bpf_map_lookup_elem(out_fd, &cgid, &out)) {
                out_cnt = (long long)out.count;
                out_sum += out.count;
                if (out.last_activity_ns && now_ns >= out.last_activity_ns)
                    out_age_ms = (long long)((now_ns - out.last_activity_ns) /
                                             1000000ULL);
            }

            if (have_cd) {
                fprintf(stderr,
                        "arb_diag cg=%llu pend=%lld out=%lld qdepth=%lld deficit=%lld"
                        " rsel=%llu rarm=%llu skips=%llu fast=%llu voids=%u fails=%u"
                        " out_age_ms=%lld\n",
                        cgid, pend, out_cnt, qdepth,
                        (long long)cd.deficit,
                        cd.serve_selects, cd.refill_arms,
                        cd.skip_pending_zero, cd.fastpath_selects,
                        cd.void_turns, cd.dispatch_fails,
                        out_age_ms);
            } else {
                fprintf(stderr,
                        "arb_diag cg=%llu pend=%lld out=%lld qdepth=%lld out_age_ms=%lld\n",
                        cgid, pend, out_cnt, qdepth, out_age_ms);
            }
        }
    }

    if (tot_pend >= 0) {
        __s64 delta = (__s64)tot_pend - (__s64)pend_sum;

        if (delta < -1 || delta > 1) {
            fprintf(stderr,
                    "arb_diag WARNING tot_pend=%lld vs pend_sum=%llu (delta=%lld):"
                    " the S14 gauge diverged from sum(pending_total) -- a"
                    " pending_total mutation site missed its mirror; take a map"
                    " dump now\n",
                    tot_pend, pend_sum, delta);
        }
    }
    fprintf(stderr,
            "arb_diag tag=%s t=%llu.%03llus round=%lld ew_mult=%lld cgroups=%u"
            " pend_sum=%llu out_sum=%lld qdepth_sum=%lld replay=%llu holds_sum=%llu"
            " tot_pend=%lld\n",
            tag, t_s, t_ms, round, ew_mult, nr_cg,
            pend_sum, (long long)out_sum, qdepth_sum,
            replay_nr, holds_sum, tot_pend);
}

/*
 * B1 attach-time stranded-entry check (STALL_FORENSICS.md §4.1.1 seed-1).
 * Requests ACCEPTed by a q policy before the kernel published
 * current_arbiter never reach the arbiter ledger (the insert hook is
 * skipped while current_arbiter is NULL), so a cgroup with q-policy
 * backlog at attach time carries invisible entries: pending_total == 0
 * while real entries sit in the q map, permanently skipped by strict
 * pick (skip_pending_zero) and holding their sched tags.  The marker
 * gate (ARBITER_ATTACH_READY_PATH + the scene-side ready path) prevents
 * the window from ever being populated; this check makes any survivor
 * LOUD at attach instead of silent, because policy_meta.queue_depth is
 * maintained by the q policy itself and counts exactly those entries.
 */
static void iosched_loader_arb_check_stranded(void)
{
    int meta_fd;
    __u64 key;
    __u64 next_key;
    unsigned nr_cg = 0;
    unsigned long long entries = 0;

    meta_fd = bpf_obj_get(ARBITER_META_PIN_PATH);
    if (meta_fd < 0) {
        if (errno == ENOENT)
            return;
        fprintf(stderr,
                "arbiter: attach-time stranded-entry check: policy_meta pin"
                " unavailable: %s\n",
                strerror(errno));
        return;
    }

    if (bpf_map_get_next_key(meta_fd, NULL, &next_key) == 0) {
        do {
            struct iosched_policy_meta meta = {};

            key = next_key;
            if (bpf_map_lookup_elem(meta_fd, &key, &meta))
                continue;
            if (meta.queue_depth) {
                nr_cg++;
                entries += meta.queue_depth;
                fprintf(stderr,
                        "arbiter: WARNING stranded backlog cg=%llu qdepth=%llu"
                        " at attach -- these entries predate current_arbiter and"
                        " are invisible to the arbiter ledger (leak B1); check the"
                        " scene-side attach-ready gate\n",
                        key, meta.queue_depth);
            }
        } while (bpf_map_get_next_key(meta_fd, &key, &next_key) == 0);
    }
    close(meta_fd);

    if (!nr_cg)
        fprintf(stderr,
                "arbiter: attach-time stranded-entry check: clean (0 entries)\n");
    else
        fprintf(stderr,
                "arbiter: attach-time stranded-entry check: %u cgroup(s), %llu"
                " stranded entries total\n",
                nr_cg, entries);
}

static int iosched_loader_arbiter_attach_and_run_ex(struct bpf_object *obj,
                                                    struct bpf_map *struct_ops_map,
                                                    struct iosched_arbiter_diag_maps *diag)
{
    struct iosched_loader_arbiter_progs progs;
    struct bpf_map_info info = {};
    struct bpf_struct_ops_arbiter_value_stub *stub;
    __u32 info_len = sizeof(info);
    int link_fd = -1;
    void *buffer;
    int ret = 1;

    if (iosched_loader_find_arbiter_programs(obj, &progs)) {
        fprintf(stderr, "arbiter: find_arbiter_programs failed\n");
        goto out_teardown;
    }

    if (bpf_obj_get_info_by_fd(bpf_map__fd(struct_ops_map), &info, &info_len)) {
        fprintf(stderr, "arbiter: bpf_obj_get_info_by_fd failed: %s\n", strerror(errno));
        goto out_teardown;
    }

    fprintf(stderr, "arbiter: value_size=%u\n", info.value_size);

    buffer = calloc(1, info.value_size);
    if (!buffer)
        goto out_teardown;

    stub = buffer;
    stub->data.pick_cgroup = (void *)((long)bpf_program__fd(progs.pick_cgroup));
    if (progs.hold_dispatch)
        stub->data.hold_dispatch = (void *)((long)bpf_program__fd(progs.hold_dispatch));
    if (progs.completed_request)
        stub->data.completed_request = (void *)((long)bpf_program__fd(progs.completed_request));
    if (progs.inserted_request)
        stub->data.inserted_request = (void *)((long)bpf_program__fd(progs.inserted_request));
    if (progs.dispatched_request)
        stub->data.dispatched_request = (void *)((long)bpf_program__fd(progs.dispatched_request));
    if (progs.dispatch_failed)
        stub->data.dispatch_failed = (void *)((long)bpf_program__fd(progs.dispatch_failed));
    if (progs.requeue_refund)
        stub->data.requeue_refund = (void *)((long)bpf_program__fd(progs.requeue_refund));
    /*
     * arb_mode comes from the BPF object's .struct_ops.link declaration
     * (e.g. .arb_mode = IOSCHED_ARB_STRICT in arb_wdrr.bpf.c).
     * bpf_map__initial_value() returns the object's struct_ops value
     * image in plain struct layout: function members hold program
     * pointers, data members keep their declared values.  Arbiters
     * compiled against an older ops struct (no arb_mode member) fail
     * the size guard and keep the legacy ALLOW_FALLBACK default.
     */
    {
        struct iosched_arbiter_ops *init_ops;
        size_t init_sz = 0;
        size_t need;

        init_ops = bpf_map__initial_value(struct_ops_map, &init_sz);
        need = offsetof(struct iosched_arbiter_ops, arb_mode) +
               sizeof(init_ops->arb_mode);
        if (init_ops && init_sz >= need)
            stub->data.arb_mode = init_ops->arb_mode;
    }
    strncpy(stub->data.name, bpf_map__name(struct_ops_map), sizeof(stub->data.name) - 1);

    if (bpf_map_update_elem(bpf_map__fd(struct_ops_map), &(int){0}, buffer, 0)) {
        fprintf(stderr, "arbiter: bpf_map_update_elem failed: %s (value_size=%u)\n",
                strerror(errno), info.value_size);
        free(buffer);
        goto out_teardown;
    }
    free(buffer);

    link_fd = iosched_loader_link_create(bpf_map__fd(struct_ops_map), 0);
    if (link_fd < 0) {
        fprintf(stderr, "arbiter: link_create failed: %s\n", strerror(errno));
        goto out_teardown;
    }
    /*
     * Publish the attach-complete marker (Wave-B patch 0005): the kernel
     * has published current_arbiter at this point, so every request
     * inserted from now on is charged to the arbiter ledger.  Gate
     * consumers must treat this file as the ONLY proof the arbiter is
     * live; the pinned maps above existed before the load.  Write
     * failure is logged but not fatal: the scene-side gate times out
     * with the loader's stderr in the error payload either way.
     */
    {
        int rfd = open(ARBITER_ATTACH_READY_PATH,
                       O_WRONLY | O_CREAT | O_TRUNC, 0644);

        if (rfd >= 0) {
            dprintf(rfd, "pid=%ld link_fd=%d\n", (long)getpid(), link_fd);
            close(rfd);
        } else {
            fprintf(stderr, "arbiter: ready marker %s failed: %s\n",
                    ARBITER_ATTACH_READY_PATH, strerror(errno));
        }
    }

    /* B1 forensic check: any q-policy backlog alive at this instant
     * predates the publish and is stranded outside the arbiter ledger. */
    iosched_loader_arb_check_stranded();

    iosched_loader_install_signals();
    if (diag && diag->interval_ms) {
        /*
         * Ledger-drift dump loop (arb_recovery tune3): every
         * diag->interval_ms, plus one right after attach (proves the
         * machinery) and one final at detach (captures teardown residue
         * -- B3 evacuate-invalid shows up as pend > qdepth here).  The
         * stop-signal latency is bounded by the 200ms slice (only when
         * the diag is enabled; the default path keeps sleep(1)).
         */
        __u64 now_ns = 0;
        __u64 next_dump_ns = 0;
        __u32 slice_ms = diag->interval_ms < 200 ? diag->interval_ms : 200;

        iosched_loader_monotonic_ns(&now_ns);
        next_dump_ns = now_ns + ((__u64)diag->interval_ms * 1000000ULL);
        iosched_loader_arb_diag_dump(diag, "attach");
        while (!iosched_loader_stop) {
            iosched_loader_monotonic_ns(&now_ns);
            if (now_ns >= next_dump_ns) {
                iosched_loader_arb_diag_dump(diag, "periodic");
                iosched_loader_monotonic_ns(&now_ns);
                next_dump_ns = now_ns + ((__u64)diag->interval_ms * 1000000ULL);
                continue;
            }
            usleep(slice_ms * 1000);
        }
        iosched_loader_arb_diag_dump(diag, "detach");
    } else {
        while (!iosched_loader_stop)
            sleep(1);
    }

    iosched_loader_wait_all_policies_quiesced(3000);
    ret = 0;
    iosched_loader_teardown_arbiter_maps();
    unlink(ARBITER_ATTACH_READY_PATH);
    close(link_fd);
    return ret;

out_teardown:
    unlink(ARBITER_ATTACH_READY_PATH);
    iosched_loader_teardown_arbiter_maps();
    return ret;
}

static int iosched_loader_arbiter_attach_and_run(struct bpf_object *obj,
                                                 struct bpf_map *struct_ops_map)
{
    return iosched_loader_arbiter_attach_and_run_ex(obj, struct_ops_map, NULL);
}

#endif
