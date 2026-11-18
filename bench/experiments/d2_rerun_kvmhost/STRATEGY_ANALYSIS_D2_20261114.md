# STRATEGY ANALYSIS · D2 策略选型（高公平 + 高带宽 + 低 CPU）· 2026-11-14

目的：为 kvmhost D2 重跑选择 **1 个 iox 简单策略（单 q 或单 arb）**。证据来自两台机器的已有 D2 数据（bm NVMe 多批 n=3、kvmhost addendum n=3）与 loader CLI 核查。给另一 agent 对齐用。

## 0. 负载样式（选型约束，新策略实现也按此描述）

- ncg 个 cgroup 租户，fio 4k randread、每租户 1 进程 QD256、direct=1，全部**同时饱和**打满盘；
- 权重梯度 1:2:…:ncg（期望份额 share_i = C·w_i/Σw，C=实测饱和带宽）；
- 全程 60s 深队列争用，无空闲段 → **不需要借用/欠载机制**（bm 上 u_* 借用族已证伪：QD1 需求设计下 demand_frac 0.00–0.41）；
- 判据：JainW(bw_i/w_i) 高 + 聚合带宽 ≈ 饱和锚 + CPU 开销低。

参考曲线：等分（不认权重）的 JainW = 0.90/0.76/0.60/0.45（cg=2/4/8/16）。任何臂低于这条曲线=比不管还差；贴着这条=没执行权重；J→1.0=真正按权分配。

## 1. 证据汇总

### 1.1 bm NVMe（多独立批次，n=3，相位门控，质量最高）

| 策略 | cg2 | cg4 | cg8 | cg16 | 聚合带宽 vs none | 备注 |
|---|---|---|---|---|---|---|
| none（等分参照） | 0.90 | 0.76 | 0.60 | 0.45 | 100%（~2600–3000） | work-conserving 独占 |
| **io_cost**（native io.weight 通道） | **1.0** | **1.0** | **1.0** | **1.0** | ~65–75%（1300–2300） | **7+ 批全部 J=1.0，唯一稳定公平臂** |
| q_ratelimit（单 q，token bucket） | 0.90塌 | 0.99 | 0.99+ | 0.99+ | ~40–50%（690–1300） | cg2 公平塌；cap 由 k 档位决定 |
| iox_q_fifo（root FIFO） | 0.90 | 0.76 | 0.59 | 0.45 | 40–90% | 等分曲线，无权重通道 |
| iox_arbonly / iox_qarb（bm 旧 D2 批） | 0.90 | 0.76–0.78 | 0.59–0.61 | 0.45–0.48 | 55–90% | 等分曲线：**bm 旧批 arb 未接 -W 权重通道** |
| u_*（欠载借用） | — | 0.62–0.79 | 0.55–0.91 | flip–0.94 | — | 负结果，排除 |
| wedfW 批（bm） | 0.90 | 0.76 | 0.61 | 0.46 | — | 加权 wedf 未证明生效 |

### 1.2 kvmhost（addendum 批，per-tenant contention 数据；介质相位混杂，看相对关系）

| 策略 | cg2 | cg4 | cg8 | cg16 | agg 中位 (MB/s) |
|---|---|---|---|---|---|
| q_fifo | 0.90 | 0.76 | 0.60 | 0.45 | 390–473 |
| q_wedf（root 无权重） | 0.90 | 0.76 | 0.62 | 0.45 | 38–531 |
| **arb_wdrr**（+per-tenant q_fifo -W=100·w） | 0.90 | **0.84** | **0.85** | 0.42 | 224–373 |
| arb_cost | 0.90 | 0.84 | 0.78 | 0.41 | 130–421 |
| arb_slo | 0.90 | 0.83 | 0.78 | 0.42 | 152–603 |
| arb_sp | 0.99 | 0.87 | 0.69 | 0.43 | 111–565 |
| q_rl（d2_fairness 旧批） | 0.90塌 | 0.99 | 0.99+ | 0.99+ | 468–2063 |

要点：**arb_wdrr 是唯一在 cg4/8 上明显高于等分曲线的 iox 臂**（0.84/0.85 vs 0.76/0.60），说明 -W 通道真实生效，只是记账不完美；其低聚合值（224–373）主要受旧数据相位塌缩污染，不能作为带宽结论。

### 1.3 CPU/进程开销（结构性分析 + homo_v2 侧车经验）

| 形态 | 进程数（cg16） | 热路径 | 相对开销 |
|---|---|---|---|
| native（none/io_cost/bfq） | 0 | 内核原生 | 最低 |
| 单 q root policy（q_fifo/q_wedf root） | 1 | scx_io BPF 入队 | 低 |
| 单 q per-cgroup（q_ratelimit/q_wedf per-tg） | ncg=16 | BPF + 每 cgroup 记账 | 中 |
| 单 arb（arbiter + ncg q_fifo） | 17（1 arbiter+16 q_fifo） | BPF + 用户态 arbiter 记账 | 中 |
| 换内核（arb_d2fq native 路径） | 0（内核内） | blk-mq/NVMe WRRU | 低（但需 ioextd2fq 内核） |

## 2. 策略清单（全部现存 loader，逐个判定）

**单 q 族**（BPF 入队策略；root=1 进程，per-cgroup=ncg 进程）：

| 策略 | 权重通道 | 公平证据 | 判定 |
|---|---|---|---|
| q_fifo | 无（root FIFO） | 等分曲线 | ❌ 不认权重 |
| q_none / q_ddl / q_kyber / q_mq_deadline | 无 | 等分曲线 | ❌ 电梯/透传 |
| q_iomax | 硬帽 | 帽即份额（需精确锚） | ⚠️ 与 io.max native 重复 |
| q_ratelimit | per-cgroup rate cap（--rate-mib-s/--burst-ms） | **bm/kvmhost 双平台 J≈0.99–1.0（cg≥4）** | ✅ 公平确定性最强，⚠️ 非工作保持（份额外浪费）、cg2 需排查 |
| q_wedf | -W（per-cgroup）；旧 D2 全用 root 无权重 | 等分曲线；bm wedfW 批未证明生效 | ⚠️ 加权变体未验证 |
| q_sp_edf | 优先级非权重 | cg16 崩（11 MB/s） | ❌ |

**单 arb 族**（arbiter 用户态记账 + per-tenant q_fifo -W=100·w）：

| 策略 | 公平证据 | 判定 |
|---|---|---|
| **arb_wdrr** | kvmhost cg4/8 J=0.84/0.85（唯一超等分曲线的 iox 臂）；work-conserving | ✅ **主候选** |
| arb_cost | 0.78–0.84 | 备选（引入 cost 模型复杂度，公平无增益） |
| arb_slo | 0.78–0.83 | 备选（面向 D3 延迟目标，D2 无优势） |
| arb_sp / arb_mix / arb_noop | ≤等分或崩 | ❌ |
| arb_iocost / arb_iolatency / arb_feedback | D2 未测/借用已证伪 | ❌（feedback 无 live writer） |
| **arb_d2fq**（FAST'21 设备直 WFQ） | 未在 D2 测过；语义即为此目标设计（vtime WFQ + NVMe WRRU 硬件权重） | ⚠️ 黑马：**需把 kvmhost 内核换成 7.3.1-ioextd2fq**（现 ioext3），上机成本高 |

**native（必须进 baseline 作对照）**：none（分母）、bfq（公平调度器对照）、**io_cost（io.weight 通道——全平台唯一 J=1.0 臂，零用户态开销，"公平可行"的存在性证明）**。

## 3. 候选建议

**首选：`arb_wdrr`（单 arb，arb_wdrr + per-tenant q_fifo -W=100·w）**
- 理由：work-conserving WFQ——争用时按权分配、无空闲浪费 → 公平与带宽的理论上限同时最高；kvmhost 唯一被证明权重通道生效的 iox 臂（cg4/8 J 0.84/0.85）；结构简单（1 arbiter + ncg q_fifo，CLI 现成 `-W`）；
- 风险：记账不完美（J 未到 1.0，旧数据 cg16 掉到 0.42）；cg16 的 -W=100·w 到 1600 是否溢出需核查（q_fifo -W 上限）；
- 若 cg16 记账崩，回退口径为 cg∈{2,4,8} + 报告 cg16 为已知限制。

**公平确定性对照：`q_ratelimit`（单 q，per-tenant token bucket，rate=share_i=ref_agg(cg)·w/Σw）**
- 理由：token bucket 硬分率，公平是构造性的（不依赖记账质量）；带宽锚定实测饱和（全贪心租户下 agg→ref）；
- 风险：非工作保持（有租户需求低于份额时浪费带宽——D2 全饱和场景不触发）；cg2 公平塌需在 pilot 中确认；p99 受桶调度影响需观察。

**不建议本次引入**：arb_d2fq（需换内核，kvmhost 刚回滚，风险叠加；建议作为 bm 上的后续臂）。

## 4. 新策略实现预期（若对齐后决定要"更好的 arb_wdrr"）

若另一 agent 认为 arb_wdrr 的记账不完美不可接受，新策略 = **在 scx_io 调度器内部做 vtime WFQ**（q_wedf 加权化 / "q_wfq"）：
- 语义：per-cgroup vtime，入队时按 `-W` 权重记账服务量（4k randread → 全部按字节数记账，无需 cost 表），分发出队选最小 vtime（deficit 补偿，防饿死）；
- 负载样式适配：D2 全饱和深队列（QD256/租户），vtime WFQ 在深队列下收敛到精确按权分配（理论 J→1.0），工作保持（聚合=设备饱和）；
- 实现点：scx_io BPF 的入队/分发路径 + cgroup→flow 映射（q_wedf 已有 per-cgroup 挂载骨架，改窗口调度为 vtime 即可）；
- 预期：J ≥ 0.98 且 agg ≈ none 的 90%+，CPU 与 q_wedf per-cgroup 同级；
- 验收：D2 cg=4/8 pilot 对比 arb_wdrr，J 提升 ≥0.10 且带宽不降。

## 5. 待对齐的决策点（给另一 agent）

1. iox 主臂选 **arb_wdrr** 还是 **q_ratelimit**（或两个都进 pilot，约多 10 min）？
2. baseline 是否定为 `none + io_cost + bfq`（建议保留 bfq 仅作 native 公平对照；若省时间可去掉）？
3. pilot 范围：cg∈{4,8} × n=2 × {none, io_cost, 主选 iox}（≈12 序列，~40 min）→ 对齐后再放全量 cg{2,4,8,16}×n=3；
4. q_ratelimit 若入选：rate 锚用标定 ref_agg(cg)（各 cg 分帽，推荐）还是全局 SAT 单值（旧口径，不推荐）；
5. 新策略（q_wfq）是否排期：建议先跑 pilot 拿 arb_wdrr 的真实基线再决定。
