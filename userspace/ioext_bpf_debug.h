#ifndef IOEXT_BPF_DEBUG_H
#define IOEXT_BPF_DEBUG_H

#ifndef IOEXT_DEBUG
#define IOEXT_DEBUG 0
#endif

#if IOEXT_DEBUG
#define IOEXT_BPF_DBG(fmt, ...) bpf_printk("ioext: " fmt, ##__VA_ARGS__)
#define IOEXT_BPF_DBG_ONCE(flag, fmt, ...)                         \
    do {                                                          \
        if (!(flag)) {                                            \
            (flag) = 1;                                           \
            bpf_printk("ioext: " fmt, ##__VA_ARGS__);            \
        }                                                         \
    } while (0)
/* Sampled printk for trace observation (avoid XADD return value: clang BPF rejects it) */
#define IOEXT_BPF_DBG_SAMPLED(period, fmt, ...)                \
    do {                                                          \
        static __u32 _sample_cnt = 0;                             \
        __u32 cnt;                                                \
        __sync_fetch_and_add(&_sample_cnt, 1);                    \
        cnt = _sample_cnt;                                        \
        if ((cnt % (period)) == 0) {                              \
            bpf_printk(fmt, ##__VA_ARGS__);                       \
        }                                                         \
    } while (0)
#else
#define IOEXT_BPF_DBG(fmt, ...) do { } while (0)
#define IOEXT_BPF_DBG_ONCE(flag, fmt, ...) do { } while (0)
#define IOEXT_BPF_DBG_SAMPLED(period, fmt, ...) do { } while (0)
#endif

#endif
