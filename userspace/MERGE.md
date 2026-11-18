# io-ext Merge 设计与测试指南

> **策略百科**：[`../IO_SCHED_POLICIES.md`](../IO_SCHED_POLICIES.md)（各 `q_*` 算法与已知问题）  
> **测试入口**：[`../../tests/io/merge/README.md`](../../tests/io/merge/README.md)  
> **最新测试快照**：[`../../results/io_merge_suite/suite_20260710_151201/IMPLEMENTATION_REPORT.md`](../../results/io_merge_suite/suite_20260710_151201/IMPLEMENTATION_REPORT.md)

最后更新：2026-07-10

---

## 1. Merge 在 io-ext 中的位置

Merge **不是**独立调度器或 loader，而是内嵌于每个 **Queueing Policy (`q_*`)** 的可选 `struct_ops` hook：

```
Bio → bio_merge (plug) → request_merge → allow_merge → 执行 merge
                                              ↓
                                    request_merged / requests_merged
                                              ↓
                                    更新 policy 内部队列状态
```

| Hook | 角色 | 无 BPF hook 时默认 |
|------|------|-------------------|
| `allow_merge` | 是否允许 bio 与 rq merge | 允许 |
| `bio_merge` | plug 阶段是否尝试 merge | 拒绝 |
| `request_merge` | 返回 merge 目标 rq | NO_MERGE |
| `former_request` | 查找相邻候选 rq | NULL |
| `request_merged` | merge 后更新 policy 状态 | 空 |

加载 `loader_q_kyber` / `loader_q_mq_deadline` 即附带其 merge 逻辑；**无** `loader_merge`。

### 与 Arbiter 的关系

- **正交**：merge 在 insert / bio 阶段完成；Arbiter 只管多 cgroup 间的 dispatch 选择。
- **merge 测试默认不加载 Arbiter**，依赖内核框架兜底 dispatch；唯一例外是 `BFQ_ARB`（`arb_bfq` + `q_bfq` 硬配对）。

### 内核可观测性

`CONFIG_IOSCHED_EXT_DEBUG=y` 时，merge hook 以 `IOEXT_MERGE_HOOK noinline` 导出，bpftrace 可探测：

- `scx_io_allow_merge`
- `scx_io_bio_merge`
- `scx_io_request_merge`
- `scx_io_former_request`
- `scx_io_request_merged`

内核配置：`kernel/config/config-7.0-osext-merge-test`

---

## 2. 实现策略一览

### 2.1 `q_merge_probe`（测试专用探针）

**源文件**：`q_merge_probe.bpf.c` + `loader_q_merge_probe.c`

极简 FIFO dispatch + 可配置 merge 行为，用于验证 hook 链路可达性。

| 模式 ID | 名称 | `allow_merge` | `bio_merge` | `former_request` | 用途 |
|---------|------|---------------|-------------|------------------|------|
| 0 | DENY_ALL | 恒 false | 恒 false | — | 负对照 |
| 1 | ALLOW_ALL | 恒 true | 恒 true | — | hook 可达性 |
| 2 | ADJACENT_SAME_OP | 扇区相邻 + 同 opf | true | FIFO peek | 最小正确 merge |
| 3 | COUNT_ONLY | 记录后 deny | 记录后 deny | 记录 | 纯探针 |

**Loader 参数**（`loader_q_merge_probe`）：

```
loader_q_merge_probe [cgroup] [arb_prio] [arb_weight] [mode]
# mode: 0–3，默认 0
```

**Maps**：

| Map | Pin 路径 | 用途 |
|-----|----------|------|
| `merge_stats` | `/sys/fs/bpf/merge_stats` | L1 主观测：`allow_merge_*`、`bio_merge_*` 等 |
| `probe_cfg` | `/sys/fs/bpf/probe_cfg` | 模式配置 |

### 2.2 `q_mq_deadline` merge 变体

**源文件**：`q_mq_deadline.bpf.c` + `loader_q_mq_deadline.c`

| 模式 | 名称 | `allow_merge` | `bio_merge` | `request_merged` |
|------|------|---------------|-------------|------------------|
| D0 | OFF | 恒 false | 恒 false | 无 |
| D1 | COMPAT | `mqdd_merge_compatible`（同 prio + 同 R/W） | false | 无 |
| D2 | FULL | 同 D1 | false | 更新 lane sector/cost |
| D3 | BIO | 同 D1 | **true** | 同 D2 |

`former_request` 复用已有 `lane_former`（sector 树 / FIFO 查找）。

**Loader 参数**：

```
loader_q_mq_deadline [cgroup] [arb_prio] [arb_weight] [merge_mode]
# merge_mode: 0–3，默认 0（D0 OFF）
```

**Maps**：

| Map | Pin 路径 |
|-----|----------|
| `merge_stats_map` | `/sys/fs/bpf/merge_stats_map` |
| `merge_cfg_map` | （loader 启动时写入，无 pin） |

### 2.3 `q_kyber` merge 开关

**源文件**：`q_kyber.bpf.c` + `loader_q_kyber.c`

- 新增 `merge_cfg_map`（`enable` 字段）
- `allow_merge`：先检查 `enable`，再调用 `iosched_kyber_merge_compatible`（同 domain）

**Loader 第 9 参数**（`argv[8]`）：

| 值 | 含义 |
|----|------|
| `1` | merge on（默认，K0） |
| `0` | merge off（K1） |

Pin：`/sys/fs/bpf/kyber_merge_cfg`

### 2.4 尚未实现 merge 的策略

`q_fifo`、`q_prio`、`q_none`、`q_bfq`、`q_wedf`、`q_sp_edf` 当前无 merge hook。详见 [`IO_SCHED_POLICIES.md`](../IO_SCHED_POLICIES.md) 各节「已知问题」。

---

## 3. 观测优先级

| 层级 | 手段 | 说明 |
|------|------|------|
| **L1 主** | BPF `merge_stats` / `merge_stats_map` | 判断 merge 是否生效的首选 |
| **L2 主** | fio `total_ios` / IOPS | 吞吐参考，**不宜单独**作为 merge 判据 |
| **L3 辅** | bpftrace `kprobe:scx_io_*` | 符号可达性 / hook 调用确认 |
| **L4 排障** | dmesg、`trace_snapshot` | hung_task / Oops |

### 读取 merge 统计

```bash
# q_merge_probe
bpftool map dump pinned /sys/fs/bpf/merge_stats

# q_mq_deadline
bpftool map dump pinned /sys/fs/bpf/merge_stats_map
```

### 关键判据（来自 VM 验证）

- 高压力 fio 下 merge **主要走 `bio_merge` plug 路径**；`allow_merge_calls` 常为 0。
- 以 `bio_merge_true` / `allow_merge_true` 为主，而非 `total_ios` 变化。
- D2 `request_merged` 计数本轮为 0，状态同步路径待后续验证。

---

## 4. 构建

```bash
cd ioext
KERNEL_CONFIG=kernel/config/config-7.0-osext-merge-test IOEXT_DEBUG=1 ./scripts/make.sh

# 仅构建 merge 相关策略
./scripts/make.sh q_merge_probe q_mq_deadline q_kyber
```

| 组件 | 路径 |
|------|------|
| 用户态策略 | `userspace/io/` |
| Loader 产物 | `userspace/io/build/loader_*` |
| 内核 merge hook | `kernel/iosched_ext.c` |

---

## 5. 运行测试

详见 [`tests/io/merge/README.md`](../../tests/io/merge/README.md)。

快速入口（宿主机）：

```bash
./tests/io/merge/run_on_virtme.sh --skip-build --fio-runtime 30
```

正式压测建议 `FIO_RUNTIME=120–300`。

结果写入 `results/io_merge_suite/suite_<timestamp>/`，含每 case 的 `merge_stats.json`、`verdict.json`、`MERGE_REPORT.md`（自动摘要）。

---

## 6. 测试 Case 概览

| Phase | 目标 | Case 示例 |
|-------|------|-----------|
| 1 | hook 可行性 | P0_PROBE_DENY、P1_PROBE_ALLOW、P2_PROBE_ADJACENT |
| 2 | 经典策略 | K0/K1 kyber、D0–D3 mq_deadline、F0 fifo、BFQ_ARB、B0 native |
| 3 | 无 Arbiter 兜底 | FALLBACK_NO_ARB_FIFO、FALLBACK_NO_ARB_KYBER |

完整矩阵与数字见各 suite 目录下的 `IMPLEMENTATION_REPORT.md`。

---

## 7. 后续工作

- 正式压测（`FIO_RUNTIME=120–300s`）
- 裸机 smoke（T0：P0–P2 + FALLBACK）
- 修复 / 验证 `request_merged` 状态同步（D2）
- 为其余 `q_*` 按需补齐 merge hook
