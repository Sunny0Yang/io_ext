# D1_OVERHEAD_bm — 框架开销与可扩展性探针（bm，kernel 7.3.1-ioext2）

- 日期: 2026-11-07；探针: `scripts/d1_overhead_probe_bm.sh`（矩阵 A）+ `scripts/d1_phasebc_bm.sh`（B/C），分析: `scripts/d1_analyze_bm.py`
- bm 原始数据（fio json + 全量 per-IO slat/clat/lat 日志 + map 快照）: `bm:/opt/ioext/dyn_tenant_regions/iocost_probe/d1_probe_111427/`；蒸馏副本（json/map/gzip 样本/汇总 CSV）: `results/D1_overhead_bm_data/`
- 口径: fio `--write_lat_log` 逐 IO 三列（本 fio 3.41 的 lat log 数值单位为 **ns**；fio json `clat_ns` 亦为 ns）。slat=提交路径（syscall/BPF insert 前），clat=提交后到完成（含 dispatch 决策与设备），lat=slat+clat。
- 工作负载: 4K randread QD1 io_uring direct，testfile 2G（**dd 全量写零**；首轮探针 d1_probe_105631 因 fallocate 未写导致 unwritten-extent 全部变成零页缓存命中、无设备 IO，已作废并重跑）。
- bm 已还原: scheduler `[none] mq-deadline kyber bfq scx_io`，root io.cost.qos `enable=0`，无残留 loader/cgroup（已核验）。

## 1. 分解矩阵（A 相，单租户独占，15s，1 个 4K QD1 租户）

| 配置 | IOPS | clat p50 | clat p99 | clat p99.9 | clat max | slat p99 | >500µs IO 占比 |
|---|---|---|---|---|---|---|---|
| A1 native none | 55,259 | 10.9µs | 48.3µs | 51.9µs | 0.11ms | 1.70µs | 0.000% |
| A2 scx_io 裸挂（无 loader，bypass 路径） | 55,265 | 10.9µs | 48.3µs | 51.7µs | 0.17ms | 1.68µs | 0.000% |
| A3 scx_io + q_none（空载 policy） | 55,158 | 10.9µs | 48.3µs | 52.8µs | 0.19ms | 1.68µs | 0.000% |
| A4 scx_io + q_fifo (p0/W400)，无 arbiter | 51,137 | 10.7µs | 48.2µs | 53.6µs | 4.19ms* | 3.27µs | 0.000% |
| A5 A4 + arb_feedback（f1 等价栈，单租户） | **10,419** | 10.8µs | **3,944µs** | 4,991µs | 5.10ms | 5.31µs | **1.968%** |
| A6 A5 + 空闲 tb/tc/td loader（f1 T1 精确复刻） | 10,475 | 10.7µs | **3,941µs** | 4,985µs | 5.14ms | 5.37µs | **1.971%** |
| A7 native none 复测（漂移检查） | 55,251 | 10.9µs | 48.3µs | 52.3µs | 0.21ms | 1.69µs | 0.000% |

*A4 的 4.19ms max 是单发（<0.0001%），p99.9 仍 53µs。逐层增量（clat p99）：bare−native ≈ 0；q_none ≈ 0；q_fifo ≈ 0（IOPS −7.5%，slat p99 +1.6µs = BPF insert 队列开销）；**q→arb+q：+3.90ms clat p99，IOPS −81%**。

**结论 1：3.9ms 不是框架裸开销。** scx_io 挂载、q_none、q_fifo 三层与 native none 在 clat p99 上不可区分（±1µs）；框架+最简策略的真实开销 ≤ µs 级（slat p99 1.7→3.3µs）。3.9ms 完全由 **arbiter（arb_feedback 的 DRR 决策面）** 引入，且在**全新 15s 单租户运行**即复现（无需 6 分钟状态累积）。

## 2. slat vs clat 归因（用户口径要求）

- A5/A6：slat p99 = 5.3µs（native 1.7µs，仅 +3.6µs），clat p99 = 3,944µs，lat≈slat+clat。**ms 级滞留 100% 在 clat（提交之后、完成之前）**，即请求已被 fio 提交进内核、卡在 scx_io 的 dispatch 决策/排队，而不是 syscall/BPF 提交路径。
- 滞留分布：1.97% 的 IO >500µs（≈1/51），其中 0.78% 落在 3.8–4.1ms、0.34-0.38% >4.4ms，最长 5.1ms —— 4ms 模式值 = HZ=250 下的 1 个 jiffy（T6 独占期 12ms ≈ 3 jiffies，同一机制的加长版）。
- 遥测佐证（A5 运行中每 2s `bpftool map dump`，12 快照）：`fb_hold_cnt` 全 0、`fb_state`=NORMAL → **hold_dispatch 从未触发**（其 gate 需要低优租户 inflight≥budget，T1 无 BE，排除 DIAG 嫌疑点 1）；`tenant_state` 键（cg 30438, hctx 0）：`deficit=0, pending=1, dispatches=4728, refill_arms=96` → **4728/96 = 49.3 ≈ 预算 50（=512B×W400/4KB）**，每次 round 拨款恰好放行 50 个 IO，第 51 个以 `deficit=0, pending>0` 不具资格（exhaust 规则）滞留 BPF 队列；QD1 下 inflight=0、无完成事件 → **dispatch 静默，直到 ~1 个 jiffy 粒度的迟到 re-kick**。serve_no_progress=0、requeues=0（排除嫌疑点 2/3）。
- 机制链：DRR budget 耗尽 → 租户失格（`deficit<=0` exhaust）→ 队列有 pending 但 pick 无产出 → has_work 为真但 dispatch 返回 NULL → blk-mq 停轮 → QD1 无完成事件可再 kick → 等待 jiffy 级外部事件 → re-arm → 50 个快 IO → 再耗尽。**是调度策略行为（DRR arm/eligibility 语义 + 无保障 re-kick），不是框架固有开销。**

## 3. cgroup 横向扩展（B 相，4K QD1 × n，10s；每租户独立 cgroup+fio）

| n | native 聚合 IOPS | q_fifo 聚合（Δ） | q_fifo slat p99 最差 | clat p99 最差（两栈） |
|---|---|---|---|---|
| 2 | 103,375 | 90,820 (−12.1%) | 5.4µs | 50-58µs |
| 4 | 204,504 | 186,927 (−8.6%) | 5.7µs | 46-47µs |
| 8 | 383,162 | 342,617 (−10.6%) | 8.0µs | 51µs（两栈） |
| 16 | 634,969 | 466,624 (−26.5%) | **25.3µs** | 63µs vs **148µs** |
| 4（arbfb, f1 优先级 p0/400,5/100,2/200,5/100） | 204,504（native 对照） | **60,669 (−70.3%)** | 7.4µs | 55µs |

- 横向结论：**框架路径本身可扩展**——clat p99 在 native/scx_io/q_fifo 三栈下 1→16 cgroup 基本平坦（46-63µs）；q_fifo 的 slat p99 随 n 增长（1.8→25.3µs，policy_meta map 记账 + 每 insert BPF 开销），16 cgroup 时聚合带宽 −26.5% 主要来自提交路径 CPU 开销，clat 不受影响。**多租户活跃时（B_4cg_arbfb）3.9ms 尾消失**（其他租户的完成事件持续 re-kick，静默窗口变短），但 DRR 预算把聚合 IOPS 压到 native 的 30%——总预算/轮 = Σ(w)×512B，这是策略行为不是 bug（但表明预算粒度对小 IO 流过粗）。

## 4. 纵向（C 相，native none 单租户）

- io.latency（target=1000µs）：本轮设置语法未生效（`echo target=1000 > io.latency` 仍 FAILED），C1 实为 native 复测（46.5k IOPS, clat p99 52.7µs）——**纵向 io.latency 轴待补测**。
- io.cost.qos（root，enable=1，rlat/wlat=100ms 不可达 target）：IOPS 12,377，clat p99 55µs，**slat p99 = 3,526µs** —— 即使"不可达 target"，启用 io.cost.qos 也把 ms 级尾搬到 **slat（提交路径）**，与 arbfb 的 clat 尾形成干净的机制对照（一个是提交侧 meter，一个是派发侧 gate）。注：ctrl=user 且未配 cost model，可能包含真实限速成分，解释为"纯开销"需谨慎（已标注为待对照项）。结束已还原 enable=0。

## 5. 定位 f1_arbfb T1=3.88ms / T6=12ms（与主表数据对齐）

- 主表 `figures/dyn_tenant_main_timeseries.csv` f1_arbfb A 列：T1 每 1s 窗口 p99 稳定 3.5-4.0ms（非瞬态）、p50 54µs、IOPS ~6.4k；heldout s201：T1 p99=3,850µs、T6 p99=11,993µs；n1 native：T1/T6 p99=163µs、IOPS 12.4k。与 A5/A6 探针（3,941-3,944µs、IOPS 10.4k）**同机制同量级**。
- f1 的 T1 IOPS 只有探针的 60%（6.4k vs 10.4k）：f1 场景下 4 个租户的 q_fifo loader 全部挂载（虽然 b/c/d 无流量），arbiter 扫描/记账面更大，静默窗口更长的概率更高（12ms=3 jiffies 出现率更高）。DIAG 的 boundary hang 是同一"pending>0 + 无 re-kick"形态的极端化（hold gate 或 DRR 键滞留使其永不 re-arm，直到 teardown evacuate）。
- 层归属：**调度策略层（userspace BPF arb_feedback 的 DRR arm/exhaust 语义）× 内核派发层（scx_io 对"有债无产出"无保障 re-kick）**。不是框架裸开销（A2/A3 证明），不是 q_fifo（A4 证明），不是 hold_dispatch（fb_hold_cnt=0）。

## 6. 优化方案（草案，未实施）

`results/D1_overhead_bm_data/D1_patch_draft_solo_fastpath.md`：
1. **P0（userspace BPF，~20 行）**：`ioext_arb_common.h:arb_drr_pick` 加 sole-demand fast path——扫描中已可统计 `pending_total>0` 的租户数，仅 1 个时无条件服务并按轮拨款补足 deficit（旁路 exhaust 规则；多租户路径不变）。
2. **P1（内核，~5 行）**：`iosched_ext.c:scx_io_dispatch_request` 在 `bpf_in_flight>0` 但本次无产出时 `blk_mq_delay_run_hw_queue(hctx, 1)`，把"有债无产出"的静默上限钉死在 1 jiffy，同时独立封堵 DIAG 的 teardown-until-hang 形态。
3. **P2（可选）**：`inserted_request` 里对唯一需求租户提前补款，消除一次空 pick。
预期：f1 T1/T6 独占期 clat p99 3.88ms/12ms → ~0.16ms（native none 水平）；单租户 IOPS 10.4k → ~50k。

## 7. 限制与后续

- lat log 数值单位为 ns（fio 3.41），分析脚本已按 ns 解读；B 相 16cg 的 p99 尾 148µs 需在更多 seed 下复核。
- C1 io.latency 语法待修（`io.latency` 接受的键值格式需查证）；C2 的 cost-model 对照需补。
- 精确的 re-kick 来源（jiffy 粒度事件）建议 R6 用 IOEXT_DEBUG 构建 + tracepoint 一锤定音。
- 横向结论基于 10s 单 seed/臂；arbfb 的多租户吞吐损失（−70%）是 DRR 预算语义，需 D2/D3 框架下评估是否属预期。
