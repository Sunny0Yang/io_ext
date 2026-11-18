# 内核 CONFIG 能力清单 —— 7.3.1-ioext1 @ bm（下次编译对照）

> 依据 2026-11-04 只读探测：`/boot/config-7.3.1-ioext1` + cgroup/queue sysfs 实测。
> 用途：下次在构建机重编内核时按此清单决定开关，避免部署后才发现控制器缺失。

## 1. 实测已可用（**不是**缺口，编译时保持现状即可）

| CONFIG | 状态 | 证据 / 实验用途 |
| --- | --- | --- |
| `CONFIG_BLK_CGROUP=y` / `BLK_DEV_THROTTLING=y` | =y | cgroup v2 `io.max` 限流（m2' 正式臂主要限流手段） |
| `CONFIG_BLK_CGROUP_IOCOST=y` | =y | 根 cgroup 有 `io.cost.model` / `io.cost.qos` / `io.pressure` → **io.cost 控制器可用**（此前探测 queue 下无 `iocost` sysfs 为误判：块级 iocost 模型 sysfs 需在设置 `io.cost.model/qos` 后由 rq_qos 注册出现；root cgroup 本就不显示 `io.max/io.cost`，是内核语义） |
| `CONFIG_BLK_CGROUP_IOPRIO=y` | =y | io.prio.class 可用 |
| `CONFIG_BLK_WBT=y` / `BLK_WBT_MQ=y` | =y | `wbt_lat_usec` 可用 |
| scheduler 家族 | 内建/模块 | `mq-deadline` builtin；**`bfq.ko`、`kyber-iosched.ko` =m**（modprobe 后即入 scheduler 列表）；`scx_io`（ioext 调度通道） |
| `CONFIG_SCHED_CLASS_EXT=y` | =y | sched_ext（iox/cax 结构体加载通道） |
| `CONFIG_LRU_GEN=y` + `LRU_GEN_ENABLED=y` | =y | cache_ext（`cax_*` 页缓存策略）依赖就绪 |
| `CONFIG_BPF_SYSCALL=y` / `BPF_LSM=y` / `BPF_EVENTS=y` | =y | iox/cax loader 所需 |
| `CONFIG_PERF_EVENTS=y`（含 uncore/rapl/cstate =m） | 基本可用 | perf 侧监控场景 |

## 2. 明确缺失（下次编译建议打开）

| CONFIG | 当前状态 | 缺它影响什么 |
| --- | --- | --- |
| **`CONFIG_BLK_CGROUP_IOLATENCY`** | **not set** | cgroup v2 `io.latency` 控制器整体缺失。影响 M2 控制器/干扰类实验的 io.latency 臂与 isol-bench 口径中 latency-based 组件。当前 M1_1/M1_2/M2' 三图**不依赖**它；若后续要复跑 M2 干扰曲线图（m2_controller_arms 里的 io.latency 臂）则必须打开 |

## 3. 建议编译时评估的可选项

| CONFIG | 当前 | 建议 |
| --- | --- | --- |
| `CONFIG_BLK_CGROUP_IOLATENCY` | not set | =y（见上） |
| `CONFIG_BFQ` / `CONFIG_KYBER`（block bfq/kyber-iosched） | =m | 如希望 m1_2 native 矩阵每次 case 免去 modprobe（且降低 root 依赖），可改 =y；=m 亦可接受（modprobe 即可） |
| `CONFIG_LRU_GEN_STATS` | not set | cache_ext/cax 调试观测可选 |
| `CONFIG_PERF_EVENTS_AMD_POWER` | not set | bm 为 Intel 平台，无影响 |

## 4. 非缺口说明（避免误判）

- `io.max` / `io.weight` 不出现在**根 cgroup** 是内核语义（root 不可自限），在子 cgroup 上创建即出现 —— 不代表内核缺功能。
- 块级 `/sys/block/<dev>/queue/iocost` 在未配置 `io.cost.model/qos` 前不出现，不代表 iocost 缺失。

---
*探测环境：bm（127.0.0.1, hostname user），内核 7.3.1-ioext1，cgroup2。*