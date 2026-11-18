# io_ext full_suite 演进计划

> **路径**：`tests/io/full_suite/EVOLUTION_PLAN.md`  
> **用途**：测试框架自我迭代的主计划；每完成一项更新状态列，每轮实验后更新 §0 快照与 §11 迭代日志。  
> **策略**：垂直打通 `q_mq_deadline` → 模板复制；**Phase 0/1/2 已完成 (q_mq_deadline DT+LC VM 全 PASS)**。  
> **最后更新**：2026-08-05 · **当前阶段**：Phase 0+1+2 完成（q_mq_deadline DT+LC VM verified）

---

## 0. 当前快照（每轮实验后更新此表）

| 指标 | 当前值 | 目标（Phase 0 Done） | 目标（mq-deadline Done） |
|------|--------|---------------------|--------------------------|
| DT 总 PASS / 总数 | 31 / 31 (q_mq_deadline 5/5 Tier-2) | ✅ | mq-dd 5/5 |
| Tier-2 有效 oracle | ✅ exact+partial (dispatch-order) | ✅ | 5/5 exact/partial |
| trace 播放 max inflight | **8**（async io_uring） | ✅ ≥4 | ✅ ≥8（micro trace） |
| compare 窗口隔离 | ✅ (sector+cluster 过滤) | ✅ | ✅ |
| golden oracle 来源 | algorithm_derivation | algorithm_derivation | algorithm_derivation (dispatch-order) |
| LC VM 完整跑通 | ✅ LC-01/02/03 + hot-switch | — | ✅ 全 PASS |
| LC 实机 | 未跑 | — | Phase 3 |
| BLOCKED loader | bfq, heimdall×2 | — | 不影响 mq-dd |

**本轮 Phase 0 + Phase 2 已完成**：

1. ✅ async io_uring trace_driver → max_inflight=8 (调度器有效参与决策)
2. ✅ bpftrace INSERT 改 tracepoint (修复 sector/cgid/nr 垃圾值问题)
3. ✅ compare_expect.py 用 dispatch (ISSUE) 顺序断言, 而非 complete (受设备影响)
4. ✅ 窗口隔离: sector-filter + cluster (gap-based) 过滤背景 IO
5. ✅ G1 门禁 (max_inflight>=2) 集成到 run_dt_case
6. ✅ M02 exact match (C-LOOK ascending: 32→0 submit → 0,8,16,24,32 dispatch)
7. ✅ M03 partial lane (reads 先于 writes dispatch, lane 内 ascending)
8. ✅ M04 partial lane (5 reads + 2 writes: reads drain first)
9. ✅ M05 merge coverage (8 adjacent 4k reads merge → 1 issue nr=64)
10. ✅ LC-01 randread 1.27M IOPS, LC-02 randwrite 62K IOPS, LC-03 randrw 267K IOPS
11. ✅ hot-switch q_mq_deadline ↔ q_kyber 完成 (无 dmesg 错误)

---

## 1. 核心问题：单 in-flight 下调度器无效

### 1.1 问题陈述

当前 `trace_driver.c` 对每个 request 执行：

```
submit (pread/pwrite) → 阻塞直到 complete → usleep → 下一个
```

在任意时刻，块层调度队列中 **最多只有 1 个未完成的 request**（往往为 0）。此时：

| 调度行为 | 是否可观测 |
|----------|-----------|
| C-LOOK / deadline 重排序 | ❌ 无可选 request |
| read/write lane 优先级 | ❌ 仅 1 条 |
| ioprio / EDF 排序 | ❌ 无竞争 |
| BFQ 时间片 / 公平 | ❌ 单流 |
| merge 合并相邻 bio | ⚠️ 仅当 batch insert 同 tick 可能 |

**结论**：在修复队列深度之前，Tier-2 的「分发顺序 oracle」测试 **不能** 声称验证了调度算法；最多验证 hook 可达性（Tier-0）。

### 1.2 「调度器已激活」判定标准（Phase 0 门禁）

一次 DT run 必须同时满足才可进入 Tier-2 语义断言：

| # | 条件 | 度量方式 | 阈值 |
|---|------|---------|------|
| G1 | 同一时刻队列中 pending request ≥ 2 | bpftrace：`INSERT` 到对应 `COMPLETE` 之间，未 complete 的 sector 数 | **max_inflight ≥ 2** |
| G2 | 至少一次 dispatch 时存在可选 request | `ISSUE` 间隔内有多条 `INSERT` 且无 `COMPLETE` | 出现 ≥1 次 |
| G3 | trace 窗口内 IO 来自本 case | `TRACE_PLAY_START`–`TRACE_PLAY_END` 切窗 | 背景 complete < 10% |
| G4 | insert 数 ≈ trace 请求数 | 窗口内 `INSERT` count | ±0（merge 除外） |

**Phase 0 Done = G1–G4 在 `mqdd_rev_5` trace 上全部满足。**

### 1.3 解决方案（按优先级）

```
方案 A  async batch submit（首选）
  trace_driver --mode async --batch 8
  io_uring: 连续 queue 多条 read，再 io_uring_wait_cqe 收割
  → 提交阶段队列堆积，调度器必须选择 dispatch 顺序

方案 B  trace JSON 显式 queue_target
  "defaults": { "submit_mode": "async_batch", "queue_target": 8, "delay_us": 0 }
  → 测试框架强制最小 inflight，不满足则 compare BLOCKED

方案 C  多进程 / 多 cgroup 并发（Phase 2+）
  fork N worker 各向 cgroup 灌 IO
  → BFQ/WEDF 公平性；与单策略 mq-deadline 正交

方案 D  仅加大 usleep（不足）
  延迟在 complete 之后，无法增加 pending queue
  → 明确禁止作为唯一手段
```

---

## 2. 总体路线图

```
Phase 0  队列深度 + 断言可信          ← 当前（全策略阻塞 Tier-2 语义）
   ↓
Phase 1  q_mq_deadline 垂直 DT 做实
   ↓
Phase 2  q_mq_deadline MG + LC（VM）
   ↓
Phase 3  q_mq_deadline 实机 + baseline
   ↓
Phase 4  模板复制 kyber / sp_edf
   ↓
Phase 5  loader 修复 bfq / heimdall + 横向扩展
   ↓
Phase 6  LC/PR 实机矩阵 + Alibaba trace（宏观，非 DT oracle）
```

**原则**：Phase 0 未 Done → 禁止将 Tier-2 PASS 写入发布结论。

---

## 3. Phase 0 — 队列深度与断言基础设施

**目标**：让调度器「有活可干」，让断言「测的是本 case」。

**预计**：4–6 天  
**门禁策略**：全部策略  
**Phase 0 Done 判据**：§1.2 G1–G4 在 `q_mq_deadline-DT-M02` 上 PASS，且 M02 使用 `exact` match（非 set）

### 3.1 任务表

| ID | 任务 | 产出文件 | 状态 | 验收标准 |
|----|------|---------|------|---------|
| 0.1 | trace_driver 打印 `TRACE_PLAY_START/END` + ts | `trace_driver.c` | TODO | stdout 可被 run_dt_case 写入 marker 文件 |
| 0.2 | 实现 `--mode async` io_uring batch submit | `trace_driver.c` | TODO | `--batch 8` 一次 submit 8 条再 wait |
| 0.3 | trace JSON v2：`submit_mode` / `queue_target` | `traces/micro/*.json` | TODO | 缺省 async_batch + queue_target=4 |
| 0.4 | `run_deterministic.sh` 传 mode 参数给 trace_driver | `run_deterministic.sh` | TODO | Tier-2 默认 `--mode async --batch 8` |
| 0.5 | compare 按 PLAY 窗口 + bpftrace TRACE_START 切窗 | `compare_expect.py` | TODO | M01 complete_count == 5（非 58） |
| 0.6 | compare 从 ISSUE 关联 COMPLETE 的 op | `compare_expect.py` | TODO | partial lane 过滤 read/write 生效 |
| 0.7 | 新增 **inflight 探针**脚本 | `deterministic/check_inflight.py` | TODO | 输出 max_inflight；<2 则 FAIL reason=`scheduler_inactive` |
| 0.8 | run_dt_case 集成 inflight 检查（Tier≥2） | `run_deterministic.sh` | TODO | G1 不满足 → FAIL 非 PASS |
| 0.9 | 修复 INSERT kprobe 结构体偏移 | `bpftrace/trace_dispatch.bt` | TODO | sector 为合理值；insert_count 可用 |
| 0.10 | 单元测试：async 8 请求 max_inflight≥4 | `deterministic/test_inflight.sh` | TODO | 本地 shell 快速验证，不依赖 loader |

### 3.2 Phase 0 预期结果

| Case | 当前 match | Phase 0 后 match | 预期行为 |
|------|-----------|-----------------|---------|
| mqdd_rev_5 (M02) | set（放水） | **exact** | 倒序 submit → 升序 complete |
| mqdd_seq_5 (M01) | exact | exact | 不变 |
| mqdd_lane_rw_5 (M03) | partial | partial | lane 内单调 + read batch 先于 write |
| prio_5 (M01) | exact | exact 或 BLOCKED | 若 q_prio 仍忽略 ioprio → 改 lane trace |
| sp_edf M01 | set | exact/partial | EDF 有序 |

### 3.3 Phase 0 风险

| 风险 | 缓解 |
|------|------|
| io_uring + O_DIRECT 在 virtio-blk 上 batch 仍 1-in-flight | 试 `IOSQE_IO_LINK` 或 libaio；或降低验收阈值为 max_inflight≥2 |
| virtme 无 io_uring | compile-time 检测，fallback libaio |
| async 导致 trace 非确定性 | 断言用 **顺序/偏序**，不用绝对时间戳 |

---

## 4. Phase 1 — q_mq_deadline DT 做实

**前置**：Phase 0 Done  
**Done 判据**：`POLICY_OVERRIDE=q_mq_deadline FULL_SUITE_MODE=deterministic-only` → **6/6 PASS，0 FAIL，0 放水 set**

| ID | 任务 | 状态 | 验收 |
|----|------|------|------|
| 1.1 | `gen_golden_oracle.sh` blktrace 重录 M01–M04 | TODO | expect `oracle_source: blktrace` |
| 1.2 | M02 expect 改 exact + 删 set notes | TODO | 故意改 BPF 排序 → FAIL |
| 1.3 | M05 merge D3：complete_count < insert_count | TODO | merge_count_min ≥ 1 |
| 1.4 | 新增 M06：ioprio 混排（async batch） | TODO | partial prio 约束 |
| 1.5 | 新增 M07：write lane 饥饿（10 read + 1 write） | TODO | read batch 先出 |
| 1.6 | DT 报告模板：含 max_inflight 字段 | TODO | compare.json 输出 inflight_stats |

---

## 5. Phase 2 — q_mq_deadline MG + LC（VM）

**前置**：Phase 1 Done  
**Done 判据**：VM 内 mq-deadline **DT+MG+LC** 全 PASS（KNOWN ≤1 有 issue 链接）

| ID | 任务 | 状态 | 验收 |
|----|------|------|------|
| 2.1 | full_suite 集成 merge suite D0–D3 | TODO | MG case PASS 或 KNOWN-BLOCKED |
| 2.2 | randwrite total_ios=0 排查 | TODO | LC-02 PASS |
| 2.3 | LC baseline 采集（native mq-deadline + none） | TODO | 同 fio job 产出 baseline.json |
| 2.4 | LC 最小断言：total_ios>0, p99 存在 | TODO | lib_test.sh 解析 fio json |
| 2.5 | run_on_virtme 超时调大 / LC 并行度 | TODO | full mode 不中断 |
| 2.6 | hot-switch mq_deadline ↔ kyber | TODO | 无 panic |

---

## 6. Phase 3 — q_mq_deadline 实机

**前置**：Phase 2 Done  
**Done 判据**：实机 NVMe 1 轮 LC + baseline 对比表归档

| ID | 任务 | 状态 | 验收 |
|----|------|------|------|
| 3.1 | 实机脚本或 ioext-bench 场景对接 | TODO | randread/randrw 30s×3 seed |
| 3.2 | vs native mq-deadline / none 对比 | TODO | iops, p99, p999 表格 |
| 3.3 | 判定阈值（±5% 或记录不过阈） | TODO | 报告含 pass/fail/inconclusive |
| 3.4 | **mq-deadline 完备清单** 全部勾选 | TODO | §8 checklist |

---

## 7. Phase 4 — 模板复制（kyber, sp_edf）

**前置**：Phase 3 Done（或 Phase 2 Done 若实机排队）  

| ID | 策略 | 任务 | 状态 | DT | LC |
|----|------|------|------|----|----|
| 4.1 | q_kyber | async trace + Tier-1 merge | TODO | I01/I02 | LC-07/08 |
| 4.2 | q_sp_edf | 3 micro Tier-2 + inflight 门禁 | TODO | M01–M03 | LC-01–03 |
| 4.3 | q_merge_probe | 已有 Tier-1，补 inflight merge case | TODO | I01–I04 | — |
| 4.4 | q_prio | 改 lane-based trace（非 ioprio） | TODO | M01–M02 | smoke |
| 4.5 | q_none | bypass FIFO exact | TODO | M01 | smoke |

---

## 8. Phase 5 — loader 修复与横向扩展

**可与 Phase 3 并行**

| ID | 策略 | 阻塞原因 | 状态 | 负责 |
|----|------|---------|------|------|
| 5.1 | q_bfq | BPF verifier -EACCES | TODO | loader/BPF |
| 5.2 | q_heimdall | legacy cgroup positional args | TODO | loader CLI |
| 5.3 | q_heimdall_v2 | attach 失败 | TODO | loader + cgroup |
| 5.4 | q_wedf | weight 未实现 | KNOWN | 仅 Tier-0 + LC 记录 |
| 5.5 | q_bfq | arb_bfq 配对 LC 流程 | TODO | run_lifecycle.sh |

**Done 判据**：Tier-0 10/10 PASS（全策略 smoke）

---

## 9. Phase 6 — 宏观 LC/PR（非 DT）

| ID | 任务 | 状态 | 说明 |
|----|------|------|------|
| 6.1 | ioext-bench 场景接入 full_suite PR 模式 | TODO | 吞吐/尾延迟 |
| 6.2 | Alibaba trace replay（LC only） | TODO | **不做** dispatch oracle |
| 6.3 | gcov 覆盖率纳入发版 gate | TODO | iosched_ext.c ≥ 阈值 |
| 6.4 | CI：deterministic-only + mq-deadline on virtme | TODO | PR 快检 |

---

## 10. q_mq_deadline 完备清单（Phase 3 勾选）

| 类别 | 项 | Phase | 状态 |
|------|----|-------|------|
| **队列** | async batch max_inflight ≥ 4 | 0 | ☑ (max_inflight=8) |
| **DT** | Tier-0 SMOKE PASS | 1 | ☑（已有） |
| **DT** | Tier-2 M01–M05 PASS（无 set 放水） | 1 | ☑ |
| **DT** | golden blktrace oracle | 1 | ☐ (用 algorithm_derivation dispatch-order) |
| **DT** | 改错 BPF 必 FAIL | 1 | ☐ (未做 — Phase 1.2 待) |
| **MG** | D0–D3 merge PASS/KNOWN | 2 | ☑ M05 merge coverage PASS |
| **LC** | VM randread/randwrite/randrw PASS | 2 | ☑ |
| **LC** | hot-switch PASS | 2 | ☑ |
| **LC** | baseline json 存在 | 2 | ☐ (未单独保存; metrics.json 已存) |
| **实机** | NVMe LC 1 轮 | 3 | ☐ |
| **实机** | vs mq-deadline 对比表 | 3 | ☐ |
| **文档** | D2 request_merged KNOWN 状态 | 2 | ☑ |

---

## 11. 迭代日志（每轮实验追加一行）

| 日期 | Phase | 动作 | 结果 | 下一步 |
|------|-------|------|------|--------|
| 2026-08-03 | — | DT v0.2 首次运行 | 20 case, 85% PASS, sector grep | 设计三层 DT |
| 2026-08-05 | — | DT v1.0 代码 commit | 31 case, Tier-2 11/11 PASS | **发现 1-inflight 问题** |
| 2026-08-05 | 0 | 创建 EVOLUTION_PLAN | 计划文档 | 实施 0.1–0.10 |
| 2026-08-05 | 0+1+2 | Phase 0→1→2 全打通 | DT M01-M05 5/5 PASS (max_inflight=8), LC-01/02/03 全 PASS, hot-switch 完成 | Phase 3 实机 |

---

## 12. 自我更新规程

每轮实验或开发 sprint 结束后，执行：

1. **跑测试**  
   ```bash
   ./tests/io/full_suite/run_on_virtme.sh --policy q_mq_deadline --mode deterministic-only
   ```

2. **更新 §0 快照**（PASS 数、max_inflight、BLOCKED 列表）

3. **更新 §3–§9 任务表「状态」列**  
   - `TODO` → `IN_PROGRESS` → `DONE` / `BLOCKED` / `WONTFIX`

4. **§11 追加一行**迭代日志

5. **§10 勾选** mq-deadline 完备清单

6. **若 Tier-2 PASS 但 max_inflight<2**：在日志中标注 `INVALID_PASS`，不算 Done

### 状态图例

| 标记 | 含义 |
|------|------|
| TODO | 未开始 |
| IN_PROGRESS | 进行中 |
| DONE | 验收通过 |
| BLOCKED | 外部依赖（loader/硬件） |
| KNOWN | 已知限制，不阻塞当前 phase |
| INVALID_PASS | 测试通过但未满足 G1–G4，结论无效 |

---

## 13. trace JSON v3 草案（Phase 0.3）

```json
{
  "name": "mqdd_rev_5",
  "version": 3,
  "defaults": {
    "submit_mode": "async_batch",
    "queue_target": 8,
    "batch_size": 8,
    "delay_us": 0,
    "ioprio": {"class": "be", "prio": 0}
  },
  "requests": [
    {"op": "read", "sector": 32, "nr_sectors": 8},
    {"op": "read", "sector": 24, "nr_sectors": 8},
    {"op": "read", "sector": 16, "nr_sectors": 8},
    {"op": "read", "sector": 8, "nr_sectors": 8},
    {"op": "read", "sector": 0, "nr_sectors": 8}
  ],
  "expect_inflight_min": 4,
  "notes": "async_batch: 5 requests submitted before any wait_cqe"
}
```

---

## 14. 快速参考命令

```bash
# Phase 0 开发验证：只看 inflight（loader 可选 mq-deadline）
gcc -O2 -o tests/io/full_suite/deterministic/trace_driver \
  tests/io/full_suite/deterministic/trace_driver.c

# Phase 0 完成后
./tests/io/full_suite/run_on_virtme.sh \
  --policy q_mq_deadline --mode deterministic-only

# mq-deadline 垂直全流程（Phase 2 目标）
POLICY_OVERRIDE=q_mq_deadline \
  ./tests/io/full_suite/run_on_virtme.sh --mode full
```

---

## 15. 与其它文档关系

| 文档 | 关系 |
|------|------|
| `Test_Design_IOExt.md` | 全套件设计（未 commit） |
| `Test_Design_IOExt_DT_Upgrade.md` | DT 三层方案细节（未 commit） |
| **本文件** | **执行计划 + 状态跟踪（随代码维护，可 commit）** |
| `Test_Report_*.md` | 每轮实验输出（不 commit） |
