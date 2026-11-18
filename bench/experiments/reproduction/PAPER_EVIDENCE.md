# PAPER_EVIDENCE — io_ext 复现实验的论文证据面与措辞建议（独立审查员 E）

- 日期：2026-11-14
- 依据：`VALIDITY_REPORT.md`（同目录，本审查的有效性判定）、`EXPERIMENT_MATRIX.md`（附录 E）、`ALGORITHM_AUDIT.md`、`RESULTS/`（图 + data CSV）、`results/campaign/`（原始数据，关键数字均经本审查独立复算）
- 用法说明：①"安全声明"= 当前证据可直接写进论文的话（中英各一），均须连同其 caveat 使用；②"避免措辞"= 会被本轮数据直接反驳或无证据支持的写法；③"更强声明缺口"= 要升级声明必须补的实验。图引用一律指 `RESULTS/<algo>/fig.<algo>.pdf` 及其点数据 `data.<algo>.csv`。

---

## 1. 逐算法证据面

### 1.1 mq-deadline（图 fig.mq_deadline；**正式口径 = v2 重跑 70/30**，`results/mq_deadline_v2/`；v1 50/50 集合已退出图/统计）

- **① 安全声明**
  - 中：在计划冻结负载（4k randrw 70/30、QD32、60s，`rwmixread=70` 已生效：15/15 run 读 IOPS 份额恰 0.700）下，三个臂的读 P99 不可区分——中位 none 782 / native 815 / IOX 758 µs（rep 散布 635–1106 µs 完全重叠），即 IOX 的 q_mq_deadline 移植在外部读延迟上与 native 无方向性分歧、与无调度器基线亦无分辨差异。写 P99 呈每臂 2–3 rep 在 0.64–0.91 ms、其余在 4.1–14.9 ms 的双峰，该双峰与臂无关、与执行时间完美相关（前 6 个 run 全快相、后 9 个全慢相，且同一 run 内读/写延迟与 IOPS 同步退化）——按设备快/慢相报告，不作为臂间差异解读。
  - En: "Under the frozen plan workload (4 KiB randrw 70/30, QD32, 60 s; rwmixread=70 verified effective: read share exactly 0.700 in all 15 runs), read p99 is indistinguishable across the three arms — medians of 782 (none), 815 (native mq-deadline), and 758 µs (IOX port), with per-run values 635–1106 µs fully overlapping — i.e., the IOX port shows no directional divergence from native, and neither differs detectably from the scheduler-free baseline. Write p99 is bimodal per arm (2–3 runs at 0.64–0.91 ms, the rest at 4.1–14.9 ms); the split is arm-independent and tracks execution order exactly (first six runs fast, last nine slow, with read and write latency and IOPS degrading together within each run) — we therefore report it as a device fast/slow-phase effect, not an arm difference."
  - 支撑图/表：fig.mq_deadline（v2 数据，D 更新版：读侧三臂紧簇 + 写侧双峰散点）；`RESULTS/mq_deadline/data.mq_deadline.csv`（已切换为 mq_deadline_v2 源）。
- **② 避免措辞**：
  - 不要写"验证了读写优先级/写饥饿保护（writes_starved）法则"——v1/v2 均无机制直接观测（sel_flip、debugfs starved 未采）；v2 快相内三臂 write/read 比均近 1（0.86–0.99），慢相内 none 也出现 4.3–4.8× 写放大，模式不能归因于调度器法则。
  - **不要用 v2 的 write p99 中位数做任何跨臂比较或排序**——中位数落在快相还是慢相由该臂分到的快相 run 数决定（none 3 快 2 慢→905 µs；native/iox 2 快 3 慢→4080/4293 µs），是相分配伪影。
  - **不要引用 v1 的"IOX 两方向优于 native 25–32%"**——该观察在 v2（正式口径）未复现（读侧 iox vs native 差 3.2%，噪声内）。
  - 不要写"IOX 与 native 行为一致/等价"（无条件语）——读侧可写"不可区分"；写侧现有数据无法判定。
- **③ caveat 写法**：必须注明 (a) 正式口径为 v2 70/30 重跑（v1 50/50 系生成器缺陷，已记录并替换）；(b) 写侧双峰为设备快/慢相（证据：与执行顺序完美相关、同 run 内读写与 IOPS 共退化、哨兵 IOPS 随顺序单调漂移 25.3k→45.6k），**现有数据无法分辨设备状态效应与算法差异**；(c) merge D0（IOX）vs front_merges=1（native）臂间系统差仍存在（ALGORITHM_AUDIT §1 已声明）；(d) n=5 无 CI。
- **④ 更强声明缺口**：merge D0 与法则的分离实验；IOX sel_flip + native debugfs starved/queued 采集，使"写饥饿保护"有直接读数；写侧去混淆设计（rep 间冷却/随机化 run 顺序/设备状态探针）后重跑，方可在写侧做臂间声明。

### 1.2 kyber（图 fig.kyber；DATA_SUMMARY §2）

- **① 安全声明**
  - 中：在相同延迟目标（读 80 µs/写 1 ms）与相同负载下，IOX q_kyber 的读域深度限制在 64↔256 间动态调整（5/5 rep，1s 分辨率），直接复现了 kyber 式延迟反馈→深度收缩的控制环；作为对照，native kyber 的读域深度在全部 5 次伴随采样中保持 256 不变——两种实现面对同一压力做出了不同的深度决策，这一分歧是当前移植的已测边界。
  - En: "With identical latency targets (80 µs read / 1 ms write) and identical workloads, the IOX kyber port dynamically adjusts its read-domain depth limit between 64 and 256 in all five runs (1 s resolution), demonstrating an active latency-to-depth feedback loop; the native kyber scheduler, sampled via debugfs in companion runs, never shrinks its read-domain depth below 256. This measured divergence in depth decisions — under the same target and workload — marks the current boundary of behavioral equivalence."
  - 支撑图/表：fig.kyber 下面板（native 恒线 + IOX 台阶带）；`RESULTS/kyber/data.kyber.csv`（depth_read 行）。
- **② 避免措辞**：
  - 不要写"深度反馈使 LC 延迟保持在目标内"——分相 P99 显示 p2 段三臂全部超过 80 µs 目标（102–119 µs），与 none（101.9 µs）无分辨差异。
  - 不要写"native 深度不收缩是 native 的缺陷"或"IOX 收缩是正确的"——归因未定（LC 为洞读近零真实 IO；timer vs inline 评估相位差；两解释均可能，VALIDITY_REPORT §4.2）。
  - 不要引用 LC P99 的 1s 时序曲线（不可恢复，D1；图上面板留空）。
- **③ caveat 写法**：必须写明 (a) LC 读负载落在近乎全洞的文件上（~8.2×10^5 IOPS、平均完成延迟 8.5–38.6 µs），真实设备读压力近零；(b) native 深度数据来自同协议伴随补采 run（D2），非同一次执行；(c) IOX 的深度评估为完成内联、native 为 ≤100ms 去抖定时（声明偏差），评估时机天然错位。
- **④ 更强声明缺口**：洞读归因实验（真实写满后重跑，对比两臂深度响应）；native `kyber_adjust` tracepoint 与 IOX 评估时刻对齐；记录 guest `q->async_depth`（IOX 喂值 clamp 上限）。

### 1.3 io.max（图 fig.io_max；DATA_SUMMARY §3）

- **① 安全声明**
  - 中：在 32 MiB/s 写限速、4k 随机写 QD32 负载下，native io.max（blk-throttle）与 IOX q_iomax（token bucket）达到同等的限速精度：5 rep 全程平均带宽分别为 31.997 与 31.998 MiB/s（与限速值偏差 <0.02%，none 对照 147.3 MiB/s）；幸存的逐秒序列显示 IOX 单窗带宽始终在 32±0.12 MiB/s 内。
  - En: "At a 32 MiB/s write cap under 4 KiB random writes (QD32), native io.max (blk-throttle) and the IOX token-bucket port achieve indistinguishable rate accuracy: five-run mean bandwidths of 31.997 and 31.998 MiB/s respectively (<0.02% deviation, uncapped control at 147.3 MiB/s); the surviving per-second trace holds every window within 32±0.12 MiB/s."
  - 支撑图/表：fig.io_max（上=示例 1s 序列+32MiB/s 线；下=5 rep 均值柱）；`RESULTS/io_max/data.io_max.csv`。
- **② 避免措辞**：
  - 不要写"两种限速机制等价"（无条件语）——等价只在 32 MiB/s、4k、外部带宽口径下成立；低速率通道的 burst 形态（native 100ms slice 台阶 vs IOX 0.5s token 上限）是审计指出的可分辨差异，未测。
  - 不要写"逐字节记账验证"——观测面只有外部带宽，token/slice 内部记账无直接读数。
- **③ caveat 写法**：写明 (a) 单一速率点；(b) 记账层不同（native bio 层+允许合并、IOX request 层+禁合并）与 burst 约定差异（rate×0.5s）为已声明偏差；(c) 1s 序列为单 run 示例（D1）。
- **④ 更强声明缺口**：8/16 MiB/s 与 128k 大请求边界档，区分 token vs slice 的 burst 形态；blktrace 采 native trim 证据。

### 1.4 io.weight / arb_wdrr（图 fig.io_weight；DATA_SUMMARY §4）

- **① 安全声明**
  - 中：在两条同型饱和流（128k seqwrite QD16）争用下，IOX arb_wdrr 将 100:400 权重实现为稳定的字节份额：BE 租户 5 rep 份额 0.758–0.796（中位 0.775，目标 0.8，极差 0.038），显著紧于无权重基线（0.207–0.469）与 native BFQ+io.bfq.weight（0.415–0.756，中位 0.516）。这支持"io.weight→长期字节份额"的**语义级**复现声明；同时实测表明 native BFQ 在该虚拟化写负载下并不逼近权重比——语义级与 BFQ 完整行为必须分开声明。
  - En: "Under two identical saturating write streams contending for the device, the IOX WRR arbiter maps a 100:400 weight ratio into a stable byte share of 0.758–0.796 for the weighted tenant over five runs (median 0.775 vs. 0.8 target; range 0.038) — markedly tighter than the unweighted baseline (0.207–0.469) and native BFQ with the same nominal weights (0.415–0.756, median 0.516). We therefore claim a semantics-level reproduction of io.weight (weight → long-term byte share), explicitly not a full BFQ reimplementation — a distinction our own data makes necessary, since native BFQ does not approach the weight ratio under this virtualized write-only workload."
  - 支撑图/表：fig.io_weight（示例 1s 份额序列贴 0.8 + 各臂 whole-run 份额文本框）；`RESULTS/io_weight/data.io_weight.csv`。
- **② 避免措辞**：
  - **不要写"IOX 与 native io.weight 行为一致"**——native 中位 0.516 距 0.8 很远且 rep 间漂移 0.34；这是计划预先给定的"语义级非 BFQ 复现"限定语被本轮数据证实的情形（VALIDITY_REPORT §4.4 判定：该句 caveat 被证据支撑且必要）。
  - 不要写份额"精确 0.8"（实测 0.775，-3.1%，示例序列有 0.6–0.75 的短暂下探）。
  - 不要忽略 none 基线不对称（无机制下 BE 仅 0.268）——份额归因须以 none 为参照。
- **③ caveat 写法**：写明 (a) 语义级（weight→share）而非 BFQ 完整行为（idling/budget/per-process 公平不在范围，ALGORITHM_AUDIT §4 已声明）；(b) native 载体为 io.bfq.weight（BFQ 真旋钮）+ io.weight 伴写；(c) 负载为同型写流（E5），读写混合下的权重行为未测。
- **④ 更强声明缺口**：io.bfq.weight 运行时读回 + 双向权重（400:100）载体验证；BFQ 0.516 的归因（write-only 流下 BFQ budget/idling 行为）；读写混合负载轮。

### 1.5 io.cost / arb_iocost（图 fig.io_cost；DATA_SUMMARY §5）

- **① 安全声明**
  - 中：IOX arb_iocost 的 vrate 反馈环在 90s 全程活跃且双向（vrate 乘子在 0.25×–4.0× 全程摆动，5 rep 每 rep 200–390 次上下步进）；同条件下 native blk-iocost 的 vrate 读数保持在其 QoS 上限钳位值 150.00 不变、对 LC 队列深度阶跃（32→64→32）零响应。两者的差异形态（摆动 vs 钳位恒值）是本轮实测到的复现边界，其机理（IOX 轮预算模型 + 100ms 窗 FSM vs native 周期模型在虚拟盘上的惰性）需归因实验区分。
  - En: "The IOX io.cost port's vrate feedback loop is active and bidirectional throughout each 90 s run (multiplier sweeping the full 0.25×–4.0× range, 200–390 up/down steps per run), while native blk-iocost's vrate reading stays pinned at its QoS ceiling (150.00) and does not respond to the LC queue-depth step at all. This divergence — oscillation vs. a pinned ceiling — is a measured reproduction boundary; attributing it (round-budget FSM with 100 ms windows vs. an inert period-based controller on this virtual device) requires the follow-up experiments we outline."
  - 支撑图/表：fig.io_cost（native 平线 1.0 vs IOX 摆动，双轴归一化+语义差异注记）；`RESULTS/io_cost/data.io_cost.csv`。
- **② 避免措辞**：
  - 不要写"vrate 跟随负载阶跃/方向一致"——IOX 相位均值方向跨 rep 不一致（p1→p2 三降一升一平），p3 全部触底 0.25–0.46×（饱和态）；native 无方向可言。
  - 不要写"native vrate 恒定证明 native 无反馈需求/设备未过载"——未归因（也可能是记账失效，与 E2 同族；qos 落盘无运行时读回，VALIDITY_REPORT U5-a）。
  - 不要把两条 vrate 曲线画/解读为同轴可比（native 绝对速率 vs IOX 相对乘子——矩阵已声明，图已注明）。
- **③ caveat 写法**：写明 (a) LC 为 4k 写（E2）；(b) 目标 12ms（E3）与权重 100:200（区别于 io_weight 的 100:400）；(c) native 数据为伴随补采 rep1–4（D2）；(d) 两臂 vrate 语义不同，仅比较"有无响应与活动性"。
- **④ 更强声明缺口**：native 归因（blkcg_debug_stats `cost.wait`/tracepoint `iocost_ioc_vrate_adj` 拿 missed/rq_wait，判定"无违例"还是"反馈失效"；写小 max 验证钳位）；IOX 相位锁定阶跃轮；IOX margin/boost 通道对 BE 份额的实证（#5-2 审计发现的验证）。

### 1.6 io.latency / arb_iolatency（最高优先级；图 fig.io_latency；DATA_SUMMARY §6）

- **① 安全声明**
  - 中：本轮直接实测了 native io.latency 与 IOX arb_iolatency 的控制方向分歧（机制上 native 自节流提交侧、IOX 加权保护 LC 并压 BE）：在 LC 4k 写、目标 12ms 下，IOX 将 LC 写 P99 中位压至 7.5/26.3/27.4 ms（三相位，none 为 90.7/101.2/105.4，native 97.0/83.4/94.9——native 未表现出可见保护），代价是 BE 带宽降至 136 MiB/s（none 207，-34%）；IOX 的 boost 达满档、SLO 状态机在 NORMAL/PRESSURE/RECOVERY 间循环、hold 门每 run 触发 5.6万–13.2万 次。**"即使最终 P99 接近也不代表机制等价"在本轮不适用——两臂最终 P99 相差 3–13 倍，方向分歧在可观测层清晰成立**。
  - En: "We directly measure the control-direction divergence between native io.latency (self-throttling the LC submitter) and the IOX arbiter (boosting LC and suppressing BE): with a 12 ms target on 4 KiB write traffic, IOX holds LC write p99 at 7.5/26.3/27.4 ms across the three phases (none: 90.7/101.2/105.4; native: 97.0/83.4/94.9 — no visible protection), at the cost of reducing BE bandwidth to 136 MiB/s vs. 207 uncontrolled (−34%). The IOX feedback chain is fully active (boost at its cap, SLO FSM cycling through PRESSURE/RECOVERY, 56k–132k hold-gate events per run). Native and IOX do not converge to similar observable behavior — the mechanism divergence manifests externally, not merely internally."
  - 支撑图/表：fig.io_latency（LC p99 散点 + 12ms 线；BE 带宽散点 + IOX 账本 1s 序列）；`RESULTS/io_latency/data.io_latency.csv`（be_write_bw_ledger 行）。
- **② 避免措辞**：
  - 不要写"IOX 达到 12ms SLO"——仅 p1 相位达标；p2/p3 超标（26/27 ms 中位，rep 极值 81 ms）。
  - 不要写"native 自节流按预期工作/BE 吸收了余量"——native LC p99 与 none 重叠（保护未显现），BE +20%（249 vs 207）但 rep 散布 163–320 MiB/s 且 n=5 无 CI，方向性描述只能是弱观察。
  - 不要把两臂 FSM 触发时刻对齐解读——violation 门限不同源（IOX >1% vs native ≥10%，ALGORITHM_AUDIT #6-1）。
  - 不要写"逐 IO P99 时序"——1s 时序只有 IOX 臂账本 BE 带宽；LC p99 面板是分相散点 fallback。
- **③ caveat 写法**：写明 (a) LC=write、目标 12ms（E2/E3，2ms 原目标因读侧 blkcg 归账损坏不可用）；(b) native 深度状态（max_depth）未观测，"native 机制未触发还是触发无效"未归因；(c) IOX 账本字节与 fio 全程字节 0.0% 偏差（记账可信度正面证据，可写）；(d) 门限/窗口宽度两臂不同源。
- **④ 更强声明缺口**：blkcg_debug_stats=1 采 native LC `depth` 时序（归因"未触发 vs 无效"）；门限对齐轮；BE 方向结论增 rep/加压提升功效；LC=write 换成真实读负载需先修复内核读归账（超出本轮范围，如实声明）。

### 1.7 D2FQ / arb_d2fq（图 fig.d2fq；DATA_SUMMARY §7）

- **① 安全声明（仅剩"如实报告未解决差异"一种写法）**
  - 中：在同型饱和写流（128k seqwrite QD16 ×2）与 100:400 权重下，arb_d2fq 未产生权重分化的份额——t400 租户 5 rep 份额全部为 0.500（0.4996–0.5005），与无权重机制的 none 臂不可区分（目标应为 0.8）。该结果与冒烟期发现一致、未做中途修改，是当前 D2FQ 移植（静态 vtime 近似）的已证边界；权重不分化的根因归因（vtime 记账 / gvt 选举 / DI 复位 / dispatch 归账）列为未解决问题。
  - En: "Under identical saturating write streams with a 100:400 weight ratio, the current D2FQ port produces no weight-differentiated share: the 400-weight tenant receives exactly 0.500 in all five runs (range 0.4996–0.5005), indistinguishable from the mechanism-free baseline (target: 0.8). Consistent with our smoke-test finding and recorded without post-hoc modification, this is a demonstrated boundary of the current static vtime approximation; root-cause attribution (vtime accounting, gvt election, DI resets, dispatch attribution) remains open."
  - 支撑图/表：fig.d2fq（端点散点全 0.500 + 示例累计份额曲线贴 0.50 + 0.2/0.8 参考线）；`RESULTS/d2fq/data.d2fq.csv`。
- **② 避免措辞**：
  - **绝对不要写"D2FQ 复现/实现了 virtual time 公平队列"或"份额符合权重"**——被 10/10 run 的 0.500 直接否定。
  - 不要用"静态近似"来解释 0.500——静态 vtime 近似在饱和双流下同样应产生 100:400 长期份额；矩阵 §8 的"静态近似，不得描述为完整 D2FQ 复现"限定语是**必要但不充分**的（VALIDITY_REPORT §4.7）。
  - 不要引用 band 命中率/vtime 轨迹作为"机制正确"证据——stats 已落盘但归因分析未做。
- **③ caveat 写法**：写明 (a) native 臂 VM 内 N/A（virtio 无 NVMe WRRU，E1）；(b) 两租户同型写流（E5）刻意排除读写成本不对称，因此 0.500 是干净的反例；(c) arb_wdrr 在同负载同权重下分化正确（F1/0.775），差异定位于 arb_d2fq 内部而非仲裁框架。
- **④ 更强声明缺口**（修复前无"更强声明"可谈）：归因实验清单见 §2.5；修复后重跑两臂 + bm 裸机 native 三臂（E1 后续项）。

### 1.8 LinnOS / q_linnos（图 fig.linnos；DATA_SUMMARY §8）

- **① 安全声明**
  - 中：q_linnos 的离线查表拦截律在其实际被激活的运行区间内精确执行：band2（busy>80%×busy_ref）上 BE 目标拦截率 60%，5 rep 实测全部 60.000%（确定性比例抽签，0 偏差）；LC 类在显式配置下全程 0 拦截。由于 busy 代理的完成计数缺陷（completions=0，F4），插入在启动 1–2s 后恒落 band2（占 99.9995%）——**分带维度（band0/1/2 的差异化拦截）本轮未获有效检验**，表中 band1 的 100% 仅来自 8 个样本。
  - En: "The table-driven interception law executes exactly within the operating region it actually reached: on band 2, where >99.9995% of all inserts land, the BE interception rate is 60.000% against a 60% table target in all five runs (deterministic ratio-faithful draw, zero deviation), with LC never intercepted by explicit configuration. Because a completion-counting defect pins the busy proxy to band 2 after the first seconds (completions=0, a recorded policy-level issue), the band-differentiated dimension of the table (bands 0/1) received only 20 inserts in total and remains effectively untested."
  - 支撑图/表：fig.linnos（三带目标 vs 实际柱 + 插入量标注）；`RESULTS/linnos/data.linnos.csv`。
- **② 避免措辞**：
  - 不要写"验证了 busy band × class 查表产生预期拦截"（无分带限定语）——三带中只有一带被实质检验。
  - 不要写"拦截保护了 LC/BE 吞吐受益"——本轮无任何拦截收益的因果证据（LC 恒 0 拦截是配置；BE 拦截=500µs hold 后全量软释放，`disp_held_soft`==拦截数，无请求被丢弃，"拦截"的代价面未测）。
  - 不要写"在线 NN 语义复现"——离线查表近似是头注释级声明（table-driven approximation），保持。
- **③ caveat 写法**：写明 (a) band 覆盖退化（F4：busy 代理=全局 inflight 从 dispatch 起算且完成钩子不触发→单调增长）；(b) band1/band0 样本量 8/12，无统计意义；(c) native 臂 N/A（无可信参考实现）；(d) busy_ref=24 为沿用 homo_v2 校准值（矩阵允许诊断冻结，本轮直接取 24）。
- **④ 更强声明缺口**：修复 completions 钩子或外置 inflight 代理后重跑，使 4 段 QD 阶梯真正横跨三带；busy_ref 敏感性扫描（16/24/32）；hold 代价面实验（对被拦/未拦请求的延迟转移）。

---

## 2. 机制差异的正面价值：把"分歧"写成"复现边界与语义保真度"

本轮最有论文价值的不是"都复现成功了"，而是**四个被真实测到的行为分歧**。它们共同支撑一个方法论命题：*同态复现的正确单元是"可观测语义"而非"内部机制"；机制不同源时，外部行为分歧是可测量、可报告、可归因的*。这与论文中常见的"黑盒对齐 P99 即宣称复现"形成对照。

### 2.1 可直接写进论文的分歧（数据在手，措辞见上各节）

| 分歧 | 实测形态 | 论文用途 |
|---|---|---|
| **native kyber 深度不收缩 vs IOX 收缩** | native 读域 5 rep 恒 256；IOX dl[0] {64,256} 双稳（同目标 80µs、同负载） | 论证"延迟反馈的控制环可以在 IOX 落地（机制存在性），但评估时机/负载感知差异导致决策分歧"——支撑 "semantics-level, not mechanism-level" 的主张边界 |
| **native io_cost vrate 贴顶 vs IOX 摆动** | native 恒 150.00（=QoS max 钳位）；IOX 0.25×–4.0× 全摆动 | 论证 vrate 的**载体不同**（时间周期预算 vs DRR 轮预算）导致可观测动态完全不同；归因未定部分如实写"两个候选解释" |
| **d2fq 权重不分化 vs wdrr 正确** | arb_d2fq 0.500×10 run；arb_wdrr 同负载 0.775±0.02（目标 0.8） | 最有力的"复现边界"证据：**同一仲裁框架下**，一个 arbiter 语义正确、另一个失效——说明失效是策略特定而非框架性；同时否定"静态近似≈D2FQ"的乐观假设 |
| **io.latency 方向分歧的实测形态** | native LC 无保护（p99≈none）且 BE 不减；IOX LC 3–13 倍改善且 BE -34% | 直接回答计划钦定问题："自节流 vs boost"的分歧**在可观测层成立**；反驳"只要 P99 接近就等价"的黑盒复现标准 |

### 2.2 需要先做归因实验才能写强的部分（归因实验清单）

1. **d2fq 归因（优先级最高，数据已在盘上）**：用已落盘 `arb_d2fq` stats（`tenants[].vt[2]/ch[2]/ds[2]`、`hctx[].gvt/bp[3]`）重建 vtime 轨迹、gvt 选举与 pick 决策；检查 per-(cgroup,hctx) lane 粒度下两租户是否从未同时 eligible；对照 arb_wdrr 的 `cgroup_drr` 记账；用 IOX 自己口径显式标定 `--thrsh`（#7-1 量纲差）。产出：失效层定位（记账/选举/排序键/DI）。
2. **kyber 洞读归因**：真实写满 lc.bin 后重跑两臂（native depth + IOX dl[0] 同采），区分"native 不收缩=正确控制（多数读远快于目标）"还是"inline vs timer 相位差"；补 `kyber_adjust` tracepoint 对齐评估时刻。
3. **native io_cost/io.latency 反馈惰性归因**：blkcg_debug_stats=1 + tracepoint（`iocost_ioc_vrate_adj`、native depth 行）判定 native 是"无违例"还是"记账失效（E2 同族）"；io.latency 侧确认 max_depth 是否收缩过。
4. **mq-deadline 效应分离**（70/30 mix 已由 v2 补齐）：merge D0 vs native 合并的对照轮 + sel_flip/starved 读数 + 写侧快/慢相去混淆设计，把"调度器法则效应"从"提交路径系统差"与"设备相位"中分离。
5. **linnos band 覆盖**：完成钩子修复/外置 inflight 代理后重跑 4 段 QD 阶梯 + busy_ref 扫描（16/24/32），验证三带差异化拦截。
6. **io.max burst 形态**：8/16 MiB/s 档区分 token vs slice（审计 #3-1 的实证面）。

### 2.3 写作建议

- 上述 2.1 的四行可以合成论文的一张"divergence table"（算法 × native 行为 × IOX 行为 × 归因状态）：已归因的写结论，未归因的写候选解释与实验计划——这比把分歧藏进附录更能支撑"复现边界"的可信度。
- 所有归因未定的分歧，论文措辞用 "measured, attribution open"（已测、归因未决），不要预支任何一方为"正确"。

---

## 3. 声明的分级汇总表（8 算法 × 三级评级）

> 机制正确性 = 实现自身的控制律可观测且符合其声明语义；行为一致性 = 跨 rep/臂外部行为稳定且符合声明；与 native 等价性 = IOX 与 native 外部行为可互换。评级：✔ 支撑 / ◐ 部分支撑 / ✘ 不支撑 / — 不可得（native N/A 或无观测面）。每格附一句话证据（全部数字见 VALIDITY_REPORT 附录 A）。

| 算法 | 机制正确性 | 行为一致性 | 与 native 等价性 | 一句话证据 |
|---|---|---|---|---|
| mq-deadline | — 无法判定（法则无直接观测，v1/v2 同） | ◐ v2（70/30）读侧三臂不可区分（中位 758–815 µs，rep 635–1106 重叠）；写侧中位数被快/慢相双峰混淆 | ◐ 读侧与 native 不可区分（差 3.2%，v1 的 25–32% 差未复现）；写侧无法判定（双峰与执行顺序完美相关，判为设备状态效应） | 70/30 重跑读侧无方向分歧；写双峰同 run 内读写与 IOPS 共退化、哨兵随序单调漂移，设备状态 vs 算法不可分辨（fig.mq_deadline v2） |
| kyber | ◐ IOX 反馈环存在（dl[0] 64↔256，5/5 rep）；native 读域恒 256 | ◐ 外部 P99 三臂同型，深度决策分歧 | ✘ 深度轨迹相反（恒 256 vs 双稳收缩） | 同目标同负载下两控制器决策分歧，归因未定（fig.kyber 下面板 + 伴随补采） |
| io.max | ✔ 限速语义正确（贴 cap <0.02%） | ✔ 5/5 rep 全贴 32.00（native 31.995–31.999 / iox 31.997–31.999） | ✔ 此工作点等价（median 差 0.001 MiB/s） | 两机制 mean bw 精确压线，none 147.3 对照排除负载解释（fig.io_max） |
| io.weight | ✔ weight→share 语义正确（0.775 vs 0.8，极差 0.038） | ✔ 5 rep 紧凑 + 示例序列贴 0.8 | ✘ native BFQ 0.516（0.415–0.756）远不到 0.8 | IOX 紧贴目标且聚合吞吐无损；"语义级非 BFQ 复现"限定语被数据证实为必要（fig.io_weight） |
| io.cost | ◐ FSM 活跃双向（0.25–4.0×、106–274 步）但相位不锁相、p3 触底 | ◐ 摆动形态稳定存在，方向跨 rep 不一致 | ✘ native 恒 150.00 钳位、零响应 | 摆动 vs 钳位恒值的形态分歧（fig.io_cost） |
| io.latency | ◐ IOX 保护链全活跃（boost 满档/FSM 循环/hold 5.6万–13.2万）但 12ms 仅 p1 达标 | ◐ IOX LC p99 稳定优于两臂 3–13 倍，rep 散布大 | ✘ 方向分歧实测成立：native 无保护（p99≈none）BE 不减；IOX 保 LC 压 BE（-34%） | "自节流 vs boost"分歧在可观测层清晰成立（fig.io_latency） |
| d2fq | ✘ 权重→份额失效（0.500×10 run，目标 0.8） | — 两臂均 0.5（无可检验差异） | — native N/A（E1） | 同型饱和流下 arb_d2fq 与无机制基线不可区分（fig.d2fq） |
| linnos | ◐ 查表执行面精确（band2 60.000%×5 rep 零偏差）但分带维度退化（band0/1 合计 20 样本） | ✔ 5/5 rep 与表值零偏差（在被激活区间内） | — native N/A（无可信参考） | 唯一被激活的 band 上确定性抽签精确执行；完成计数缺陷使 busy 恒 band2（fig.linnos） |

**总体**：论文可安全声明的最大集合 = "io.max 在该工作点与 native 等价；mq-deadline 在计划口径（70/30）下读侧与 native 及 none 基线均不可区分（写侧因设备快/慢相双峰不作臂间声明）；io.weight/io.latency/kyber/linnos 在 IOX 侧达到语义级/执行级复现并被内部状态证实；四个实测分歧（kyber 深度、iocost vrate、d2fq 份额、iolatency 方向）界定了当前移植的复现边界，其中 io.latency 方向分歧直接回答了计划的核心问题"。任何"完整复现 Linux 原生 IO 调度行为"或"与 native 等价"的无限定语声明都不被本轮证据支持。
