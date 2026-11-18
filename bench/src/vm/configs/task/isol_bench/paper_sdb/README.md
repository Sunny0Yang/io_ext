# paper_sdb — kvmhost 彩排轨（CloudLab 真机申请前的全量演练）

本目录是 **paper 实验族在 sdb 上的彩排定义**，由
`python3 scripts/gen_isol_bench_paper_yamls.py --track sdb` 生成（Q3–Q6 为
paper/ 的设备替换副本）。目的：在申请 CloudLab 真机**之前**，把论文实验族
（D1-Q1/Q2、D2、D3-LC、D4-LC、Q3–Q6）的每一条序列完整跑通一遍，证明
「实验定义 → 运行 → 数据 → 分析」全链路没有坑，避免真机白申请。

- 结构、拓扑、权重通道、卫生合同与 `paper/`（NVMe 定义）**逐字段一致**；
- 仅硬件绑定常数换成 rig 已收敛的 sdb 标定（见 §4 标定表）；
- `paper/` 保持 NVMe 纯净，sdb 标定永远不进 paper/。

## 1. 方法全集（19 臂，每族一致）

| 臂 | 个数 | 说明 |
|----|------|------|
| native 纯调度器 | 4 | none / kyber / bfq / mq-deadline（无每租户写值，中性对照） |
| native 控制器 | 5 | io.max / io.latency / io.cost / io.prio.class / io.bfq.weight（各走各的权重通道） |
| iox queue 策略 | 10 | loader_q_{sp_edf, kyber, wedf, ddl, mq_deadline, fifo, prio, none, noop, accept_noop} |
| iox arbiter | 6 | loader_arb_{wdrr, slo, cost, mix, sp, noop} |

D2 用 17 臂（q_fifo/q_wedf + 6 arbiter + 9 native），D3 用 16 个 iox 序列
（8 homo + 2 异形 homo + 6 hetero），D4 用 8 个 iox 策略（noop/accept_noop 除外）。

## 2. 每个实验族的设计

| 族 | 序列数 | 回答什么 | 前台负载 | 后台负载 | 权重怎么打 | 扩展轴 |
|----|--------|----------|----------|----------|-----------|--------|
| D1-Q1 (P1) | 171 | 租户数↑，单请求延迟税多大 | LC：4K 随机读 QD=1 io_uring 60s | 无 | 无（全部 inactive：io.max 5GB/s 不可达、io.latency 1s 不可达、io.cost 惰性 qos） | n=1,2,4,…,256；n≥64 附加 perf_stat |
| D1-Q2 (P2) | 171 | 租户数↑，批量带宽怎么分 | batch：4K 随机读 QD=256 libaio 60s 钉 10 核 | 无 | 无（inactive 同上） | n=1,3,…,17 |
| D2 (PD2) | 204 | 盘打满时 1:2:…:ncg 分带公平性 | ncg 个租户同时打满：4K 随机读 QD=256 libaio，**每 cgroup 1 个 fio 进程**（fio_subjob） | — | io.cost→io.weight=100·w；io.bfq.weight=100·w；io.max→饱和带宽×w/Σw；io.latency→按权重梯度 target；io.prio→三段映射；iox arbiter→loader −W=100·w；q_wedf/q_fifo 默认权重对照 | cg=2/4/8/16，iter=1..3 |
| D3-LC (PD3) | 149 | BE 打满时 LC 保护/利用率 Pareto | LC：4K 随机读 QD=1 libaio 60s | 4 BE：QD=256 libaio | 每 knob 沿自己的轴扫（见 §4）；调度器臂单点 | io.max BE 上限 32..640MiB/s（20 点）；io.latency 10us+2..30ms（10 点）；io.cost min×rlat×权重比（45 点）；bfq 权重比 46 点；prio 9 组合 |
| D4-LC (PD4) | 17 | BE 跑着时 LC 突发，恢复多快 | LC：延迟 60s 后 10s 突发 QD=1，逐 ms 记 write_lat_log | 1 BE：80s QD=256 | io.cost 10000/1+20ms qos；bfq 1000/1；prio rt/idle；io.latency LC 12ms；io.max BE 1GiB/s | 无（单点，与 rig D4 同构） |
| D1-Q3 (PQ3) | 12 | 框架每层税 | LC QD=1 + batch QD=256 两条轨 | — | none→iox_bare→q_none→minimal→q_fifo→arb_noop 阶梯 | — |
| D1-Q4 (PQ4K/M) | 2+2 | 同一算法 BPF 移植贵多少 | 读+写 QD=128 争用 | — | native kyber / mq-deadline vs iox 移植 | — |
| D1-Q5 (PQ5) | 30 | 负载不变租户数变，控制面多贵 | batch 固定总 QD=256 | — | iox_bare / iox_fifo / iox_arb_noop / **iox_arb_slo**（已转正：loader 存在）/ arb_wfq（无二进制，标 unimplemented 会失败） | n=1,2,4,…,128 |
| D1-Q6 (PQ6) | 16 | cgroup 树深度开销 | batch QD=256 | — | policy 只挂 root，深度 1/2/4/8/16 | depth；arb_hwfq 无二进制（会失败） |

监控合同：每步挂 `ib_system_metric` + `ib_block_io_latency_cost`（iox 臂再挂
`ib_scx_io_bypass_fallback`）。

## 3. 卫生合同（所有序列生效）

1. **io.cost 清擦**：每个非 io_cost 序列写 `io.cost.qos.enable=false`
   （io.cost 序列在根 cgroup 粘住的状态不会毒化后续序列）。
2. **none 臂中性**：none/kyber/bfq/mq-deadline io.weight 恒 100、无每租户覆盖。
3. **iox 权重通道**：iox 租户 cgroup 的 io.weight/io.max 显式 null（cgroup 只作
   BPF attach 点），权重只走 loader `-W`；arbiter 只吃 `-h/-v`。
4. **profile 无 `mode:`**：每序列生成全局 scene（调度器逐序列钉死 + stale
   loader/BPFFS pin 清理）。
5. **BFQ** 恒 `low_latency=0, slice_idle=1`；**D2 每 cgroup 恰 1 个 fio 进程**。

## 4. sdb 标定表（rig 收敛值；NVMe 轨保持原值不进本表）

| 常数 | NVMe 轨（paper/） | sdb 彩排轨（本目录） | 依据 |
|------|------------------|---------------------|------|
| 设备 | nvme0n1 | **sdb** | 目标硬件 |
| D2 饱和带宽（io.max 份额分母） | 2300 MiB/s | **320 MiB/s** (335544320 B) | rig 实测 sdb D2 形状饱和 |
| D2 io.latency 加权 target | 10+25(w−1) µs | **12ms×(ncg+1−w)** | rig calib（sdb p50≈10.7ms，µs 级会永久违约钳制） |
| io.cost 线性模型 | 980PRO（rbps 2.7GB/s） | **sdb 模型**（320MiB/s / 82k IOPS） | rig IOCOST_MODEL_SDB |
| D3/D4 io.cost LC qos rlat | 15 µs | **20 ms** | rig calib（Session 6：0916 发现 15µs 永久违约） |
| D3 io.max BE 轴 | 128..2304 MiB/s | **32..640 MiB/s**（含 224≈rig 逐租户饱和） | sdb 容量尺度 |
| D3 io.latency 轴 | 10µs + 75..1175µs | **10µs（rig 顶点）+ 2..30ms** | 钳制→解除钳制的有效扫描 |
| D4 io.latency LC target | 10 µs | **12 ms** | 同上 |

> ⚠️ 复核发现：paper 原版 io.latency 微秒级 target 在**两代硬件上都低于设备
> p50**（NVMe 4K QD1 p50 ≈ 百余 µs > 10µs）→ 真机上也会进入永久违约钳制区。
> CloudLab 正式跑之前建议把 NVMe 轨的 latency target 也按目标盘重新标定
> （这是定义修正项，等确认）。

## 5. 已知风险（彩排要暴露的）

1. **D1-Q1 n=256 / Q5 n=128 的 fio_subjob 大租户数**从未在 110 上跑过
   （rig 只到 n=32）——启动/内存风险，彩排第一批就会暴露。
2. Q5 的 `iox_arb_wfq`、Q6 的 `iox_arb_hwfq` 无 loader 二进制，**注定失败**
   （POLICIES_INVENTORY 确认仍缺；保留是为了网格完整性，失败是预期结果）。
3. D2 io.cost 臂在 sdb 上用「sdb 模型 + 20ms qos」（rig calib 形态）；若想复现
   0916 的「惰性 qos 无分离」对照，那是 rig 轨已做过的事，不属彩排目标。
4. D3 bfq 臂（纯调度器）与 io_bfq_weight 臂（权重扫描）已按 rig 拆分；
   paper 原版把两者混在一个 bfq knob 里，已修正。

## 6. 运行计划（待确认后执行）

- 推送：`scripts/run_paper_sdb_remote.sh --rsync-only`（新脚本，rig 零改动）。
- 逐批运行：PQ3 PQ4K PQ4M PQ5 PQ6 P1 P2 PD2 PD3 PD4
  （先小后大，Q 族先暴露机械问题）。
- 每序列一个 tar（`/tmp/paper_sdb_<SEQ>.tar.gz`），批后自动回传
  `$ARCHIVE/paper_sdb_<d>/`。
- **总量 774 条序列 ≈ 21.3h**（按今天实测 1.65min/seq）。当前分配
  f58cc13f 到 2026-10-22T07:48Z，剩 **≈24.4h**：全量可行但余量仅 ~3h。
- 可选裁剪（任选，等确认）：
  - A. D2 iter 3→1：−136 序列，省 **3.7h**（总 638 条 ≈ 17.6h）；
  - B. D1-Q1/Q2 各砍 n=128/256（各 −2 点×19 臂）：省 **2.1h**；
  - C. 只跑 A+B：总 ≈ 15h，余量充足。
- 建议顺序：先 PQ 族（0.9h 冒烟全机械面），人工确认无系统性失败后再放行
  P1/P2/PD2/PD3/PD4 大批次。

## 7. 数据分析计划（跑完后）

- 解包 774 个序列 tar → 每序列一行 CSV（fio 主指标 + system_metric +
  block_io_latency_cost + bypass 计数 + 失败/异常旗标），**所有序列全量入表**，
  失败序列保留失败行（这正是彩排要的验收证据）。
- 按族出对比表：D1 延迟/带宽 vs n 曲线（19 臂）；D2 加权 Jain/份额 MAE/
  Spearman（17 臂 × 4 cg）；D3 Pareto 前沿（LC p99 vs 总带宽）；D4 突发恢复
  时间；Q3 分层税阶梯；Q5/Q6 扩展曲线。
- 校准锚点复用今天已采的 rig CAL 数据（solo LC/BE + np1..16 容量阶梯，
  已归档 `rig_iox_cal/`）。
- 结论回答：**每一条序列是否可运行、数据形状是否可分析、真机总时长估计**。