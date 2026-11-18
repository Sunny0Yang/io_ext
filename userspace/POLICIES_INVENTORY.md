# iosched_ext 可用策略清单

> **基准日期**：2026-09-01  
> **内核入口**：`kernel/linux-7.3.1/block/iosched_ext.c`（elevator 名 `scx_io`）  
> **构建产物目录**：`userspace/io/build/loader_*`（静态 ELF）  
> **共享算法/结构**：`ioext_bpf_common.h`、`ioext_policy_shared.h`、`ioext_arb_common.h`

---

## 1. 架构与加载方式

```
echo scx_io > /sys/block/<dev>/queue/scheduler

```

| 步骤 | 命令示例 |
|------|----------|
| 构建单个策略 | `make -C userspace/io KERNEL_SRC=../../kernel/linux-7.3.1 q_sp_edf` |
| 构建全部 | `make -C userspace/io` 或 `./scripts/make.sh` |
| 单租户 | `./userspace/io/build/loader_q_fifo -c /sys/fs/cgroup` |
| 多租户 | 先起 arbiter，再为各 cgroup 起 queue loader（带 `--arb-prio` / `--arb-weight`） |

---

## 2. 成熟度分级

| 等级 | 含义 |
|------|------|
| **A · 实机可用** | VM DT 闭环已绿；算法语义与 model/golden 对齐；可直接上实机压测 |
| **B · Smoke 可用** | BPF 可加载、policy_smoke 或基础 fio 可跑；无完整 oracle 或未做行为验收 |
| **C · 实验/阻塞** | 构建存在但 verifier/DT 阻塞，或仅作探针/对照 |

---

## 3. Queue 策略（数据面 `q_*`）

### A · 实机可用

#### `q_sp_edf` — 单队列 SP-EDF

| 项 | 路径 |
|----|------|
| BPF | `userspace/io/q_sp_edf.bpf.c` |
| Loader | `userspace/io/build/loader_q_sp_edf` |
| 参考模型 | `tests/io/q_sp_edf_model.py` |
| 验收 | `tests/io/q_sp_edf_{accept,ci}.sh` |

**算法**：3 lane（小读 +1ms / 小写 +2ms / 大请求 +20ms deadline）；lane 内 `(deadline, sector)` 到达 FIFO；大请求抢占需满足 small 空 / burst 耗尽(8) / 20ms 到期 / 8ms max-wait 之一。

**状态**：accept **PASS=6/6**（M01–M04 + T1）；full_suite 已注册。

---

#### `q_kyber` — 四域 Kyber

| 项 | 路径 |
|----|------|
| BPF | `userspace/io/q_kyber.bpf.c` |
| Loader | `userspace/io/build/loader_q_kyber` |
| 共享深度逻辑 | `userspace/io/ioext_policy_shared.h`（kyber 节） |
| 参考模型 | `tests/io/q_kyber_model.py` |
| 验收 | `tests/io/q_kyber_{accept,ci}.sh` |

**算法**：READ/WRITE/DISCARD/OTHER 四域 FIFO + 域间 RR；batch 16/8/1/1；`inflight < depth_limit` 门控；EWMA 延迟反馈动态调整各域 depth（Phase-3 已对齐原生方向）。

**Loader 额外参数**：`--read-target-ns`、`--write-target-ns`、`--discard-target-ns`、`--other-target-ns`、`--merge-enable`（默认 1；**bio_merge 未实现，merge 非核心能力**）。

**状态**：accept **PASS=7/7**（K1/K2/K3/T1 + DEPTH_SHRINK/HOLD）；I01/I02 merge 诊断为 flake。

---

#### `q_wedf` — 窗口 EDF + 局部性

| 项 | 路径 |
|----|------|
| BPF | `userspace/io/q_wedf.bpf.c` |
| Loader | `userspace/io/build/loader_q_wedf` |
| 参考模型 | `tests/io/q_wedf_model.py` |
| 验收 | `tests/io/q_wedf_{accept,ci}.sh` |

**算法**：单队列；deadline = 到达 + (写 500ms : 读 50ms)；\|Δdl\| > 5ms 严格 EDF，窗口内扇区升序；跨类读先于写。

**已知限制**：**weight 未实现**（无 loader/BPF 输入通路）；`wedf_weight_probe` 为 BLOCKED-by-design。

**状态**：accept **PASS=4**（W1/W2/T1 + gate0）。

---

#### `q_ddl` — 双 lane 本地 deadline FIFO

| 项 | 路径 |
|----|------|
| BPF | `userspace/io/q_ddl.bpf.c` |
| Loader | `userspace/io/build/loader_q_ddl` |
| 参考模型 | `tests/io/q_ddl_model.py` |
| 验收 | `tests/io/q_ddl_{accept,ci}.sh` |

**算法**：LAT / BULK 两级 FIFO；ioprio RT→LAT、IDLE→BULK、BE 大读→BULK；派发优先级：LAT 超期(1ms) → BULK 饿(10ms) → LAT 紧急(200µs) → BULK → LAT。**冷启动 BULK 先于 LAT**（与 mq-deadline 读优先相反）。

**状态**：Phase-3 修复 state_map 全旁路 bug 后 **PASS=6/6**；含 counter-evidence。

---

#### `q_mq_deadline` — mq-deadline 风格

| 项 | 路径 |
|----|------|
| BPF | `userspace/io/q_mq_deadline.bpf.c` |
| Loader | `userspace/io/build/loader_q_mq_deadline` |
| DT 入口 | `tests/io/full_suite/deterministic/run_deterministic.sh`（共享 runner，无独立 `q_mq_deadline_ci.sh`） |

**算法**：C-LOOK + 3×2 lane（RT/BE/IDLE × R/W）；fifo_batch=16；读 expire 50ms / 写 500ms；写饥饿 `writes_starved=2`；方向状态机 + sector tree。

**Loader 额外参数**：`--merge-mode U32`（D0–D3；调度器级 merge 在 scx_io 下结构性受限，bio_merge 路径见 MERGE.md）。

**状态**：M01–M04/M05/M06 等在 full_suite 矩阵中；M06 在 virtio 非饱和 rig 上 BLOCKED（翻转依赖提交速率）。

---

### B · Smoke / 基线可用

#### `q_fifo` — 单队列 FIFO

| BPF | `userspace/io/q_fifo.bpf.c` |
| Loader | `userspace/io/build/loader_q_fifo` |

**算法**：`BPF_MAP_TYPE_QUEUE` 入队；dispatch/evacuate 队头弹出。多租户场景常用作 BE 租户队列。

---

#### `q_prio` — 读写/大小分 lane

| BPF | `userspace/io/q_prio.bpf.c` |
| Loader | `userspace/io/build/loader_q_prio` |

**算法**：读或小请求(≤8 扇区)→高优先级队列；其余→写队列；dispatch 先高后低。

---

#### `q_none` — 旁路

| BPF | `userspace/io/q_none.bpf.c` |
| Loader | `userspace/io/build/loader_q_none` |

**算法**：insert 直接 `IOSCHED_INSERT_BYPASS`，不排队。对照/穿透测试用。

---

#### `q_noop` / `q_accept_noop` — 最小 accept 路径

| BPF | `q_noop.bpf.c` / `q_accept_noop.bpf.c` |
| Loader | `loader_q_noop` / `loader_q_accept_noop` |

**算法**：accept 但不调度（框架/loader 生命周期测试）。

---

### C · 实验 / 阻塞 / 探针

#### `q_bfq` — BFQ 风格（未完成）

| BPF | `userspace/io/q_bfq.bpf.c` |
| Loader | `loader_q_bfq` |

**算法**：flow 预算 + 动态 slice；完整 rbtree 容器化 staged。

**状态**：full_suite SMOKE **BLOCKED**（BPF verifier -EACCES）。

---

#### `q_heimdall` / `q_heimdall_v2` — 延迟感知 hold

| BPF | `q_heimdall.bpf.c` / `q_heimdall_v2.bpf.c` |
| Loader | `loader_q_heimdall` / `loader_q_heimdall_v2` |
| 用户态守护 | `userspace/io/build/heimdall_daemon`（v1 配模型配置） |

**算法**：pending 队列 + EWMA 延迟；超 hold 阈值时 defer dispatch（v2 简化为 dec 测试路径）。

**状态**：full_suite **BLOCKED**（cgroup/verifier 问题）；见 `ref/heimdall/`。

---

#### `q_merge_probe` — merge 路径探针

| BPF | `userspace/io/q_merge_probe.bpf.c` |
| Loader | `loader_q_merge_probe` |

**算法**：可配置 allow_merge 模式 + `merge_stats` 计数；**测试专用**，非生产调度。

**Loader 参数**：`--mode`（0=DENY_ALL … 3=COUNT_ONLY）。

---

#### `minimal` — 最小 skeleton

| BPF | `minimal.bpf.c` |
| Loader | `loader_minimal` |

框架占位，无完整调度语义。

---

## 4. Arbiter 策略（控制面 `arb_*`）

Arbiter 通过 `pick_cgroup` 在多个已注册 queue policy 的 cgroup 间选择下一个派发对象；**须先于 queue loader 加载**。

### B · Smoke / 多租户可用

#### `arb_wdrr` — 加权 Deficit Round Robin

| BPF | `userspace/io/arb_wdrr.bpf.c` |
| Loader | `userspace/io/build/loader_arb_wdrr` |
| 参考模型 | `tests/io/arb_wdrr_model.py` |
| 验收 | `tests/io/arb_wdrr_{accept,ci}.sh` |

**算法**：dispatch-time 按字节 charge deficit；deficit>0 且 pending>0 时连续服务同一租户；**IOSCHED_ARB_STRICT**（禁止 generic fallback）。

---

#### `arb_slo` — SLO 感知 WDRR

| BPF | `userspace/io/arb_slo.bpf.c` |
| Loader | `loader_arb_slo` |

**算法**：WDRR 基底 + 按 `arb_prio` 分 LC/BW/BE；完成反馈动态 boost；LC 高 inflight 时 `hold_dispatch` 二级保护。

---

#### `arb_cost` — 请求代价加权 WDRR

| BPF | `userspace/io/arb_cost.bpf.c` |
| Loader | `loader_arb_cost` |

**算法**：charge = f(op, size_bucket) 静态代价表；等权重下按代价而非字节公平。

---

#### `arb_iocost` — io.cost 风格固定代价 DRR（cost-aware 近似）

| BPF | `userspace/io/arb_iocost.bpf.c` |
| Loader | `loader_arb_iocost` |
| 参考模型 | `tests/io/arb_iocost_model.py` |

**算法**：**cost-aware，非忠实复现，固定代价近似**——借 io.cost 的代价意识（per-request cost = f(read/write, size 档)），不实现其线性 size 项、在线模型校准与 vrate/qos 反馈。代价表由 loader 下发（`--cost-table` 10 值 / `--cost-quantum` / `--cost-min-budget`），零值/缺表回落编译内默认（= arb_cost 表）。

---

#### `arb_iolatency` — io.latency 语义 LC/BE 延迟反压

| BPF | `userspace/io/arb_iolatency.bpf.c` |
| Loader | `loader_arb_iolatency` |
| 参考模型 | `tests/io/arb_iolatency_model.py` |

**算法**：prio 0-2 = LC（有 `--lat-target-ns` 延迟目标），3-7 = BE。completed_request 反馈 LC 实测延迟（100ms 窗口、>1% 超标 → PRESSURE），LC 连续超标时 boost 阶梯（1→2→4，cap = `--boost-max`）压缩 BE 份额（DRR 下与缩 BE 比例等价）；BE 永不 boost（BE 间恒按 weight 比保底）。`--protect-inflight` 为 LC hold 硬保护门。

---

#### `arb_mix` — LC/BE 混合选 cgroup

| BPF | `userspace/io/arb_mix.bpf.c` |
| Loader | `loader_arb_mix` |

**算法**：`arb_prio < 3` 为 latency class；latency burst≥8 或 bulk 等待≥10ms 时强制切 BE；否则优先 LC。

---

#### `arb_sp` — 静态优先级

| BPF | `userspace/io/arb_sp.bpf.c` |
| Loader | `loader_arb_sp` |

**算法**：按 `arb_prio` 数值选最高优先级且 queue_depth>0 的 cgroup。

---

#### `arb_bfq` — BFQ 风格 vtime arbiter

| BPF | `userspace/io/arb_bfq.bpf.c` |
| Loader | `loader_arb_bfq` |

**算法**：start/finish tag 虚拟时间；需与 `q_bfq` 配对。

**状态**：依赖 q_bfq 就绪；当前 BLOCKED。

---

#### `arb_noop` — 空 arbiter

| BPF | `arb_noop.bpf.c` |
| Loader | `loader_arb_noop` |

对照/框架测试。

---

#### `arbiter` — 通用占位 loader

| Loader | `loader_arbiter.c` |

历史/占位入口；生产用具体 `loader_arb_*`。

---

## 5. 快速对照表

| 名称 | Loader | 等级 | DT/oracle | 一句话 |
|------|--------|------|-----------|--------|
| q_sp_edf | `loader_q_sp_edf` | A | ✅ triad | 3-lane deadline + burst 大请求 |
| q_kyber | `loader_q_kyber` | A | ✅ triad | 4 域 FIFO + 动态 depth |
| q_wedf | `loader_q_wedf` | A | ✅ triad | 窗口 EDF + 扇区局部性（无 weight） |
| q_ddl | `loader_q_ddl` | A | ✅ triad | LAT/BULK 双 FIFO + 5 规则派发 |
| q_mq_deadline | `loader_q_mq_deadline` | A | ✅ 共享 runner | C-LOOK mq-deadline |
| q_fifo | `loader_q_fifo` | B | smoke | 纯 FIFO |
| q_prio | `loader_q_prio` | B | 部分 DT | 读/写分队列 |
| q_none | `loader_q_none` | B | smoke | bypass |
| q_bfq | `loader_q_bfq` | C | BLOCKED | BFQ 未完成 |
| q_heimdall(_v2) | `loader_q_heimdall*` | C | BLOCKED | 延迟 hold |
| q_merge_probe | `loader_q_merge_probe` | C | 探针 | merge 统计 |
| arb_wdrr | `loader_arb_wdrr` | B | ✅ accept | 字节 WDRR strict（io.weight 臂，见 ARB_IO_WEIGHT_MAP.md） |
| arb_slo | `loader_arb_slo` | B | smoke | SLO boost WDRR |
| arb_cost | `loader_arb_cost` | B | smoke | 代价 WDRR |
| arb_iocost | `loader_arb_iocost` | B | ✅ model | io.cost 风格固定代价 DRR（cost-aware 近似，表可下发） |
| arb_iolatency | `loader_arb_iolatency` | B | ✅ model | io.latency 语义 LC/BE 延迟反压 + hold 门 |
| arb_mix | `loader_arb_mix` | B | smoke | LC/BE 混合 |
| arb_sp | `loader_arb_sp` | B | smoke | 静态 prio |
| arb_bfq | `loader_arb_bfq` | C | BLOCKED | 配 q_bfq |

---

## 6. 实机上线路径（最小）

```bash
echo scx_io > /sys/block/nvme0n1/queue/scheduler
echo '+io' > /sys/fs/cgroup/cgroup.subtree_control

./userspace/io/build/loader_q_sp_edf -c /sys/fs/cgroup &
fio --name=smoke --rw=randread --bs=4k --iodepth=8 --numjobs=1 --size=256M --direct=1

./userspace/io/build/loader_arb_mix &
./userspace/io/build/loader_q_sp_edf -c /sys/fs/cgroup/lc --arb-prio 1 --arb-weight 100 &
./userspace/io/build/loader_q_fifo   -c /sys/fs/cgroup/be --arb-prio 6 --arb-weight 100 &
```

---

## 7. 相关文档

| 文档 | 内容 |
|------|------|
| [`README.md`](README.md) | CLI、生命周期、map pin、故障排查 |
| [`ARB_POLICY_DEV_GUIDE.md`](ARB_POLICY_DEV_GUIDE.md) | Arbiter 开发契约 |
| [`MERGE.md`](MERGE.md) | merge hook 与 scx_io 限制 |
| [`../../tests/io/full_suite/POLICY_CLOSED_LOOP_PLAYBOOK.md`](../../tests/io/full_suite/POLICY_CLOSED_LOOP_PLAYBOOK.md) | DT 闭环 rig、G1–G4、回归基线 |
