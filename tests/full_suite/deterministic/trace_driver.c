/*
 * trace_driver.c v3 — 确定性 IO trace 播放器
 *
 * Phase 0 升级项:
 *   - io_uring async batch submit (--mode async --batch N)
 *     一次 queue N 条 read/write 再 io_uring_submit, 随后逐条收割 CQE
 *     → 提交阶段调度队列堆积, 调度器必须选择 dispatch 顺序
 *   - TRACE_PLAY_START / TRACE_PLAY_END stdout marker (ts)
 *     compare_expect.py 据此切窗, 过滤背景 IO
 *   - v3 trace JSON: defaults { submit_mode, batch_size, queue_target, delay_us,
 *                              ioprio{class,prio}, force }
 *   - 每条 request 可覆盖 defaults
 *   - 失败 non-zero exit (--force 容忍)
 *   - ioprio_set() 支持 class+prio
 *   - MAX_REQS 8192
 *
 * 编译: gcc -O2 -o trace_driver trace_driver.c
 * 用法:
 *   ./trace_driver /dev/nvme0n1 trace.json [--mode sync|async] [--batch 8]
 *                  [--delay-us 500] [--force]
 *
 * 行为模式:
 *   sync  (默认, 兼容旧版): 逐条 pread/pwrite 阻塞直到 complete, 再 usleep
 *   async (推荐 Tier-2):   io_uring 一次 submit min(batch_size, queue_target) 条,
 *                          然后逐条 io_uring_wait_cqe 收割. 单批 inflight 目标 >=2.
 *
 * 注意: O_DIRECT + io_uring 在 virtio-blk 上可达成 multi-inflight; 若运行环境
 *       无 io_uring 头文件, 编译时自动 fallback 到 libaio 风格的 sync 模式.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <time.h>
#include <linux/fs.h>
#include <stdint.h>

#ifdef __linux__
#include <linux/io_uring.h>
#endif

#ifndef IOPRIO_CLASS_SHIFT
#define IOPRIO_CLASS_SHIFT 13
#endif
#ifndef IOPRIO_PRIO_SHIFT
#define IOPRIO_PRIO_SHIFT 0
#endif

static inline int ioprio_value(int class, int prio) {
    return (class << IOPRIO_CLASS_SHIFT) | (prio << IOPRIO_PRIO_SHIFT);
}
static inline int ioprio_set(int which, int who, int ioprio) {
    return syscall(SYS_ioprio_set, which, who, ioprio);
}

#define MAX_REQS 8192
struct req {
    char op[8];
    long sector;
    long nr_sectors;
    int ioprio_class;
    int ioprio_prio;
    long delay_us;
};

static struct req reqs[MAX_REQS];
static int n_reqs = 0;
static long default_delay_us = 1000;
static int default_batch_size = 8;
static int default_queue_target = 8;

static inline unsigned long long now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (unsigned long long)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

/* 运行时模式: 0=sync, 1=async (io_uring) */
static int submit_mode_async = 0;

static char *find_field_value(char *obj_start, const char *field) {
    char pattern[64];
    snprintf(pattern, sizeof(pattern), "\"%s\"", field);
    char *p = strstr(obj_start, pattern);
    if (!p) return NULL;
    p = strchr(p + strlen(pattern), ':');
    if (!p) return NULL;
    p++;
    while (*p == ' ' || *p == '\t') p++;
    return p;
}

static long parse_long_field(char *obj_start, const char *field) {
    char *p = find_field_value(obj_start, field);
    if (!p) return -1;
    return strtol(p, NULL, 10);
}

static char *parse_string_field(char *obj_start, const char *field, char *out, int outlen) {
    char *p = find_field_value(obj_start, field);
    if (!p) { out[0] = 0; return NULL; }
    while (*p == ' ' || *p == '"') p++;
    int i = 0;
    while (i < outlen - 1 && p[i] && p[i] != '"') { out[i] = p[i]; i++; }
    out[i] = 0;
    return out;
}

static int parse_ioprio_obj(char *obj_start, int *class_out, int *prio_out) {
    *class_out = 0; *prio_out = 0;
    char *ioprio_pos = strstr(obj_start, "\"ioprio\"");
    if (!ioprio_pos) return -1;
    char *brace = strchr(ioprio_pos, '{');
    if (!brace) {
        char *p = find_field_value(obj_start, "ioprio");
        if (p) {
            *prio_out = (int)strtol(p, NULL, 10);
            *class_out = 2; /* BE default */
            return 0;
        }
        return -1;
    }
    char *close = strchr(brace, '}');
    if (!close) return -1;
    char class_str[16] = "be";
    parse_string_field(brace, "class", class_str, sizeof(class_str));
    *prio_out = (int)parse_long_field(brace, "prio");
    if (strcmp(class_str, "rt") == 0) *class_out = 1;
    else if (strcmp(class_str, "be") == 0) *class_out = 2;
    else if (strcmp(class_str, "idle") == 0) *class_out = 3;
    else *class_out = 2;
    return 0;
}

/* 解析 defaults 块: submit_mode, batch_size, queue_target, delay_us */
static void parse_defaults(char *buf) {
    char *defaults_pos = strstr(buf, "\"defaults\"");
    if (!defaults_pos) return;
    char *dbrace = strchr(defaults_pos, '{');
    if (!dbrace) return;
    char *dclose = strchr(dbrace, '}');
    if (!dclose) return;
    *dclose = 0;

    long du = parse_long_field(dbrace, "delay_us");
    if (du > 0) default_delay_us = du;

    long bs = parse_long_field(dbrace, "batch_size");
    if (bs > 0) default_batch_size = (int)bs;

    long qt = parse_long_field(dbrace, "queue_target");
    if (qt > 0) default_queue_target = (int)qt;

    char mode_str[16] = "";
    parse_string_field(dbrace, "submit_mode", mode_str, sizeof(mode_str));
    if (strcmp(mode_str, "async_batch") == 0 || strcmp(mode_str, "async") == 0) {
        submit_mode_async = 1;
    } else if (strcmp(mode_str, "sync") == 0) {
        submit_mode_async = 0;
    }

    *dclose = '}';
}

static int parse_trace(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) { perror("fopen trace"); return -1; }
    char buf[65536];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    buf[n] = 0;
    fclose(f);

    parse_defaults(buf);

    char *p = strstr(buf, "\"requests\"");
    if (!p) { fprintf(stderr, "no \"requests\" in trace\n"); return -1; }
    p = strchr(p, '[');
    if (!p) return -1;
    p++;

    while (n_reqs < MAX_REQS) {
        char *open = strchr(p, '{');
        if (!open) break;
        char *close = strchr(open, '}');
        if (!close) break;
        *close = 0;

        char op[8] = "read";
        parse_string_field(open, "op", op, sizeof(op));
        long sector = parse_long_field(open, "sector");
        long nr = parse_long_field(open, "nr_sectors");
        if (nr < 0) nr = 8;
        if (sector < 0) sector = 0;

        int iclass = 2, iprio = 0;
        parse_ioprio_obj(open, &iclass, &iprio);

        long du = parse_long_field(open, "delay_us");
        if (du < 0) du = default_delay_us;

        strcpy(reqs[n_reqs].op, op);
        reqs[n_reqs].sector = sector;
        reqs[n_reqs].nr_sectors = nr;
        reqs[n_reqs].ioprio_class = iclass;
        reqs[n_reqs].ioprio_prio = iprio;
        reqs[n_reqs].delay_us = du;
        n_reqs++;
        p = close + 1;
    }
    return n_reqs > 0 ? 0 : -1;
}

/* ── io_uring async batch submit ── */
#ifdef __NR_io_uring_setup
#define HAVE_IOURING_SYSCALL 1
#endif

#ifdef HAVE_IOURING_SYSCALL
static inline int io_uring_setup(unsigned entries, struct io_uring_params *p) {
    return syscall(__NR_io_uring_setup, entries, p);
}
static inline int io_uring_enter(int fd, unsigned to_submit, unsigned min_complete,
                                 unsigned flags, void *sig) {
    return syscall(__NR_io_uring_enter, fd, to_submit, min_complete, flags, sig, 0);
}
static inline int io_uring_register_buffers(int fd, const struct iovec *iovs,
                                             unsigned nr_iovs) {
    return syscall(__NR_io_uring_register, fd,
                   1 /* IORING_REGISTER_BUFFERS */, iovs, nr_iovs);
}

/* 返回 0=ok, <0=fatal. 仅在 async 模式调用. */
static int run_async(int fd) {
    /* ring 配置 */
    struct io_uring_params params;
    memset(&params, 0, sizeof(params));
    int ring_fd = io_uring_setup(default_queue_target * 2 + 8, &params);
    if (ring_fd < 0) {
        fprintf(stderr, "io_uring_setup failed: %s (errno=%d)\n",
                strerror(errno), errno);
        return -1;
    }

    /* SQ/CQ mmap */
    size_t sq_ring_sz = params.sq_off.array + params.sq_entries * sizeof(unsigned);
    size_t cq_ring_sz = params.cq_off.cqes + params.cq_entries * sizeof(struct io_uring_cqe);
    void *sq_ptr = mmap(NULL, sq_ring_sz, PROT_READ | PROT_WRITE, MAP_SHARED,
                        ring_fd, 0ULL /* IORING_OFF_SQ_RING */);
    if (sq_ptr == MAP_FAILED) { fprintf(stderr, "mmap sq failed: %s\n", strerror(errno)); close(ring_fd); return -1; }
    void *cq_ptr = mmap(NULL, cq_ring_sz, PROT_READ | PROT_WRITE, MAP_SHARED,
                        ring_fd, 0x8000000ULL /* IORING_OFF_CQ_RING */);
    if (cq_ptr == MAP_FAILED) { fprintf(stderr, "mmap cq failed: %s\n", strerror(errno)); munmap(sq_ptr, sq_ring_sz); close(ring_fd); return -1; }
    struct io_uring_sqe *sqes = mmap(NULL, params.sq_entries * sizeof(struct io_uring_sqe),
                                     PROT_READ | PROT_WRITE, MAP_SHARED, ring_fd,
                                     0x10000000ULL /* IORING_OFF_SQES */);
    if (sqes == MAP_FAILED) { fprintf(stderr, "mmap sqes failed\n"); munmap(cq_ptr, cq_ring_sz); munmap(sq_ptr, sq_ring_sz); close(ring_fd); return -1; }

    unsigned *sq_tail = (unsigned *)((char *)sq_ptr + params.sq_off.tail);
    unsigned *sq_mask = (unsigned *)((char *)sq_ptr + params.sq_off.ring_mask);
    unsigned *sq_array = (unsigned *)((char *)sq_ptr + params.sq_off.array);
    unsigned *sq_head = (unsigned *)((char *)sq_ptr + params.sq_off.head);

    unsigned *cq_head = (unsigned *)((char *)cq_ptr + params.cq_off.head);
    unsigned *cq_tail_p = (unsigned *)((char *)cq_ptr + params.cq_off.tail);
    unsigned *cq_mask = (unsigned *)((char *)cq_ptr + params.cq_off.ring_mask);
    struct io_uring_cqe *cqes = (struct io_uring_cqe *)((char *)cq_ptr + params.cq_off.cqes);

    /* 预分配每个 request 的 buffer */
    void *bufs[MAX_REQS];
    int registered = 0; /* 暂不注册 buffers, 用 FIXED_FD 风险大 */

    printf("TRACE_PLAY_START ts=%llu\n", now_ns());

    int failures = 0;
    int idx = 0;
    while (idx < n_reqs) {
        int batch = default_batch_size;
        if (batch > n_reqs - idx) batch = n_reqs - idx;
        if (batch > default_queue_target) batch = default_queue_target;

        /* 准备并 submit 一批 SQE */
        int submitted = 0;
        for (int b = 0; b < batch; b++) {
            struct req *r = &reqs[idx + b];
            if (r->ioprio_prio > 0 || r->ioprio_class != 2) {
                ioprio_set(0, 0, ioprio_value(r->ioprio_class, r->ioprio_prio));
            }
            long bytes = r->nr_sectors * 512;
            void *buf;
            if (posix_memalign(&buf, 512, bytes) != 0) {
                fprintf(stderr, "posix_memalign failed for req %d\n", idx + b);
                failures++;
                continue;
            }
            memset(buf, 0xAA, bytes);
            bufs[idx + b] = buf;

            unsigned tail = __atomic_load_n(sq_tail, __ATOMIC_ACQUIRE);
            unsigned next = tail + 1;
            unsigned tail_idx = tail & *sq_mask;
            struct io_uring_sqe *sqe = &sqes[tail_idx];
            memset(sqe, 0, sizeof(*sqe));
            sqe->opcode = (strcmp(r->op, "write") == 0) ? IORING_OP_WRITE : IORING_OP_READ;
            sqe->fd = fd;
            sqe->addr = (unsigned long)buf;
            sqe->len = bytes;
            sqe->off = r->sector * 512;
            sqe->ioprio = ioprio_value(r->ioprio_class, r->ioprio_prio);
            sq_array[tail & *sq_mask] = tail_idx;
            __atomic_store_n(sq_tail, next, __ATOMIC_RELEASE);
            submitted++;
        }

        /* 通知内核 */
        if (submitted > 0) {
            int ret = io_uring_enter(ring_fd, submitted, 0, 0, NULL);
            if (ret < 0) {
                fprintf(stderr, "io_uring_enter submit failed: %s\n", strerror(errno));
                failures += submitted;
            }
        }

        /* 逐条收割 CQE (顺序 == 完成顺序) */
        for (int b = 0; b < batch; b++) {
            struct req *r = &reqs[idx + b];
            long bytes = r->nr_sectors * 512;
            /* 等待一个 CQE */
            unsigned head;
            for (;;) {
                head = __atomic_load_n(cq_head, __ATOMIC_ACQUIRE);
                if (head != __atomic_load_n(cq_tail_p, __ATOMIC_ACQUIRE)) break;
                /* busy wait + yield; min_complete=0 不会阻塞 */
                if (io_uring_enter(ring_fd, 0, 1, 0x1 /* IORING_ENTER_GETEVENTS */, NULL) < 0) {
                    /* 信号/错误 */
                    break;
                }
            }
            unsigned cq_idx = head & *cq_mask;
            struct io_uring_cqe *cqe = &cqes[cq_idx];
            long ret = cqe->res;
            if (ret < 0) {
                fprintf(stderr, "IO %d (%s sector=%ld nr=%ld) failed: %s\n",
                        idx + b, r->op, r->sector, r->nr_sectors, strerror(-ret));
                failures++;
            }
            printf("TRACE_IO op=%s sector=%ld nr_sectors=%ld bytes=%ld ret=%ld ioprio=%d/%d\n",
                   r->op, r->sector, r->nr_sectors, bytes, ret,
                   r->ioprio_class, r->ioprio_prio);
            __atomic_store_n(cq_head, head + 1, __ATOMIC_RELEASE);
        }

        /* 清理 buffers */
        for (int b = 0; b < batch; b++) {
            free(bufs[idx + b]);
            bufs[idx + b] = NULL;
        }

        /* 批间延迟 (仅在 batch 之间, 不在 batch 内部, 保证 inflight) */
        if (reqs[idx + batch - 1].delay_us > 0) usleep(reqs[idx + batch - 1].delay_us);
        idx += batch;
    }

    printf("TRACE_PLAY_END ts=%llu\n", now_ns());
    printf("TRACE_DONE total=%d failures=%d\n", n_reqs, failures);

    munmap(sqes, params.sq_entries * sizeof(struct io_uring_sqe));
    munmap(cq_ptr, cq_ring_sz);
    munmap(sq_ptr, sq_ring_sz);
    close(ring_fd);
    (void)registered; (void)sq_head;
    return failures > 0 ? 1 : 0;
}
#else
static int run_async(int fd) {
    fprintf(stderr, "io_uring syscall not available on this build; falling back to sync\n");
    (void)fd;
    return -1;
}
#endif

/* ── sync 模式 (兼容旧版) ── */
extern int force;
static int run_sync(int fd) {
    int failures = 0;
    printf("TRACE_PLAY_START ts=%llu\n", now_ns());
    for (int i = 0; i < n_reqs; i++) {
        if (reqs[i].ioprio_prio > 0 || reqs[i].ioprio_class != 2) {
            ioprio_set(0, 0, ioprio_value(reqs[i].ioprio_class, reqs[i].ioprio_prio));
        }
        long bytes = reqs[i].nr_sectors * 512;
        void *buf;
        if (posix_memalign(&buf, 512, bytes) != 0) {
            perror("posix_memalign");
            if (!force) { close(fd); return 1; }
            failures++;
            continue;
        }
        memset(buf, 0xAA, bytes);
        off_t offset = reqs[i].sector * 512;
        ssize_t ret;
        if (strcmp(reqs[i].op, "write") == 0) {
            ret = pwrite(fd, buf, bytes, offset);
        } else {
            ret = pread(fd, buf, bytes, offset);
        }
        if (ret < 0) {
            fprintf(stderr, "IO %d (%s sector=%ld nr=%ld) failed: %s\n",
                    i, reqs[i].op, reqs[i].sector, reqs[i].nr_sectors,
                    strerror(errno));
            failures++;
            if (!force) {
                free(buf);
                close(fd);
                return 1;
            }
        }
        printf("TRACE_IO op=%s sector=%ld nr_sectors=%ld bytes=%ld ret=%zd ioprio=%d/%d\n",
               reqs[i].op, reqs[i].sector, reqs[i].nr_sectors, bytes, ret,
               reqs[i].ioprio_class, reqs[i].ioprio_prio);
        free(buf);
        if (reqs[i].delay_us > 0) usleep(reqs[i].delay_us);
    }
    printf("TRACE_PLAY_END ts=%llu\n", now_ns());
    printf("TRACE_DONE total=%d failures=%d\n", n_reqs, failures);
    return failures > 0 ? 1 : 0;
}

int force = 0;
static int run_sync(int fd);

int main(int argc, char *argv[]) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s /dev/<dev> trace.json [--mode sync|async] "
                        "[--batch N] [--delay-us N] [--force]\n", argv[0]);
        return 1;
    }
    const char *dev = argv[1];
    const char *trace = argv[2];

    for (int i = 3; i < argc; i++) {
        if (strcmp(argv[i], "--force") == 0) force = 1;
        else if (strcmp(argv[i], "--delay-us") == 0 && i + 1 < argc) {
            default_delay_us = strtol(argv[++i], NULL, 10);
        }
        else if (strcmp(argv[i], "--batch") == 0 && i + 1 < argc) {
            default_batch_size = (int)strtol(argv[++i], NULL, 10);
            default_queue_target = default_batch_size;
        }
        else if (strcmp(argv[i], "--mode") == 0 && i + 1 < argc) {
            const char *m = argv[++i];
            if (strcmp(m, "async") == 0 || strcmp(m, "async_batch") == 0)
                submit_mode_async = 1;
            else
                submit_mode_async = 0;
        }
    }

    if (parse_trace(trace) < 0) {
        fprintf(stderr, "failed to parse trace: %s\n", trace);
        return 1;
    }

    int fd = open(dev, O_RDWR | O_DIRECT);
    if (fd < 0) { perror("open device"); return 1; }

    int rc;
    if (submit_mode_async) {
        rc = run_async(fd);
        if (rc < 0) {
            fprintf(stderr, "async mode failed, falling back to sync\n");
            rc = run_sync(fd);
        }
    } else {
        rc = run_sync(fd);
    }

    fdatasync(fd);
    close(fd);
    return rc;
}