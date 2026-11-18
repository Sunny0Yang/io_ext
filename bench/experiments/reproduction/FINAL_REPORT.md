# io_ext 算法复现与验证 · 最终总实验报告

> 2026-11-14 · 主智能体汇总 · 依据计划《io_ext 算法复现与验证实验计划》执行
> 环境原则：全部正式数据来自 **bm 宿主机上 NVMe-backed virtio VM**（guest 内核
> 7.3.1-ioext3），与此前裸机数据不合并。判据：同负载下证明关键调度行为与
> 资源分配语义一致，吞吐相近不充分。

## 1. 执行摘要

- **8 个算法 × none/Native/IOX 三臂 × 5 rep**（D2FQ/LinnOS 无 native 臂，已声明
  N/A），**112/112 正式 run 全部 OK、0 失败**（RUN_INDEX.csv）；另 mq_deadline
  因实现缺陷（U1：丢失 70/30 混合比）按计划口径重跑 15 run（mq_deadline_v2）；
  kyber/io_cost 的 native 内部状态伴随补采 10 run（kyber_depth_native、
  io_cost_vrate_native，155 行/rep）。
- 每实验一张核心图（RESULTS/<algo>/fig.*.pdf+png + data.csv）、每 run 完整
  manifest/sentinel/loader-stats；统计口径 median（5 rep）。
- 子智能体分工按计划执行：A 算法审查、B 环境部署、C 固定配置、D 分析出图、
  E 独立 Claim-Evidence 审查；正式运行由主智能体统一排队。

## 2. 分级结论总表（E 独立判定，详见 PAPER_EVIDENCE.md 分级汇总表）

| 算法 | 机制正确性 | 行为一致性 | 与 native 等价性 | 关键数字 |
|---|---|---|---|---|
| io.max / q_iomax | ◐ | **✓** | **✓** | native 31.997 vs IOX 31.998 MiB/s（5/5 rep，cap 偏差 <0.02%），none 147 |
| io.weight / arb_wdrr | ◐ | **✓** | ◐（份额语义级，非 BFQ 完整复现——计划已声明） | IOX BE 份额 0.775 [0.758–0.796]（目标 0.8）；native(BFQ) 0.516 离散大 |
| kyber / q_kyber | ◐ | ◐ | **✘（本轮）** | IOX 读域深度 64↔256 动态反馈；**native 读域 5 rep 恒 256 不收缩**——决策分歧归因未定（归因实验见 §5） |
| io.cost / arb_iocost | ◐ | ◐ | ✘（本轮） | IOX vrate 0.25–4.0× 全程摆动（100+ 步进）响应阶跃；**native vrate 恒 150.00（贴 max 钳位）零调节** |
| io.latency / arb_iolatency | ◐ | ◐ | ◐ | IOX 反馈链路实测全活（boost=4、PRESSURE、hold 5.6–13.2 万次）；LC=write 口径下 IOX LC p99 优于两臂 3–13×；方向分歧实测成立但 12ms 目标仅 p1 段达标 |
| mq-deadline / q_mq_deadline | —（机制观测面未采） | ◐ | ◐（读侧） | v2（70/30）：读 p99 三臂 782/815/758 µs（极差 7.5%，不可区分）；write 双峰=设备相位伪影，**禁止跨臂比较** |
| D2FQ / arb_d2fq | — | ✘ | N/A | t400 份额 **10/10 run 恒 0.500**（权重 100:400 不分化；对照 arb_wdrr 1:3.8 正确）——归因数据已在盘（见 E 清单） |
| LinnOS / q_linnos | ◐ | ◐ | N/A | band2 拦截率 **60.000–60.005% vs 目标 60%**（5 rep 零偏差，确定性抽签精确）；band 覆盖退化如实标注（inflight 泄漏） |

图例：✓ 支撑 / ◐ 部分支撑 / ✘ 不支撑 / — 无观测面无法判定（详见 VALIDITY_REPORT.md）。

## 3. 本轮最重要的一般性发现（超出单算法，建议优先关注）

1. **读侧 blkcg 归账在 ioext 内核系统性损坏**（guest ioext3 与 bm 裸机 ioext4v3
   同现；探针证据链完整）：读 IO 不入任何 cgroup 的 io.stat，blk-throttle/
   blk-iocost/blk-iolatency 的读侧路径因此失效，scx_io 完成钩子的策略解析对
   读请求也失败。由此：
   - native io.latency / io.cost 的读侧反馈在该内核家族上**从未真正 engage**
     （含本轮之前的 homo_v2 数据——相关旧结论需重审）；
   - IOX 完成侧反馈同理 → 本轮 io_cost/io_latency 的 LC 改为写流量（E2），
     写侧两个实现的反馈机制均实测激活。
2. **q_none 租户的请求不进完成钩子**（BYPASS_MODE_DIRECT）——homo_v2 的
   x_latency_arb 用 q_none 做 LC，意味着该配置下 boost/hold 当年也从未生效。
3. **scx_io 完成钩子的 cgroup 归账对写流量精确**（arbiter 完成账本与 fio 三相
   字节 0.0% 偏差）——与 q_linnos completions=0 泄漏形成对照，定位了问题在
   "policy 级完成钩子"而非记账基础设施。
4. **设备快/慢相位在 VM 中同样存在**（mq_v2 write 双峰与执行顺序完美相关，
   哨兵 IOPS 随序单调漂移 25.3k→45.6k）——跨臂比较必须做相位条件化。

## 4. 已声明偏差与数据质量（全部留痕于 EXPERIMENT_MATRIX.md）

- E1–E11：D2FQ Native N/A（VM 内）、FSM 目标重锚（12ms/80µs，诊断证据）、
  io_cost/io_latency LC=write、io_weight/d2fq 同型饱和流、io.bfq.weight、
  kyber sysfs 路径、数据文件 30G、日志模式、MAJ:MIN 前缀、io.cost 模型现场标定。
- D1–D3：fio 时序日志 CWD 缺陷（每实验末轮幸存日志已回收并归属）、native
  内部状态伴随补采、哨兵文件状态效应。
- U1–U7（E 审查新增，全部采纳处置）：U1 mq 50/50→v2 重跑；U2 io_max QD32；
  U3 分相调用协议；U4 快照 grep 键已修；U5 读回断言缺口（后续补）；U6 linnos
  BE -W 200；U7 哨兵上界更正（1.0125）。

## 5. 后续工作（按 E 的归因实验清单 + 主会话补充）

1. **arb_d2fq 权重不分化归因**（数据已在盘上）：policy_meta 权重传递链 vs
   pick/charge 路径，对照 arb_wdrr 修复后重跑 d2fq 实验。
2. **native kyber 深度不收缩归因**：核对 80µs 目标写入是否生效（U5 读回断言）
   、native 拥塞检测（p90 io_lat）在 VM 负载下是否从不触发、IOX 与 native 的
   窗口数据源差异（逐 IO 采样 vs timer 窗口）。
3. **native io_cost vrate 贴顶（150=max）语义**：确认是否"LC 未越限→无需调
   节"（若然，12ms rlat 未 binding 于 native 的 cost 单位——需换算验证）。
4. **读侧 blkcg 归账损坏的内核级定位与修复**（影响面最大：三个 native 控制
   器 + scx_io 完成侧），修复后以读 LC 恢复 io_latency/io_cost 的读侧口径。
5. **mq-deadline 机制观测面补齐**（sel_flip、debugfs starved 计数、写饥饿触发
   率）+ 相位条件化协议（执行顺序分层或按哨兵分桶）。
6. **bm 裸机 front_n_d2fq 三臂**（ioextd2fq 已构建待部署）补 D2FQ native 对照。
7. 时序日志 CWD 缺陷修复后（fio CWD=run 目录）重采集 kyber/io_latency 的
   per-IO 1s p99 时序（本轮该两图的 LC p99 面板以分相 JSON 兜底）。

## 6. 交付物索引（experiments/reproduction/）

| 文件/目录 | 内容 |
|---|---|
| ENVIRONMENT_REPORT.md | bm + virtio VM 部署与逐项验收（B） |
| ALGORITHM_AUDIT.md | 8 算法 native/IOX 机制差异审查（A，file:line 证据） |
| EXPERIMENT_MATRIX.md | 固定配置矩阵 + 附录 A（CLI 核对）/E（偏差 E1-E11）/F（发现 F1-F9）/U（U1-U7 采纳）/D（D1-D3） |
| configs/ | 8 实验设计层配置（62 文件） |
| guest/ + host/ | 执行管线（run_experiment/run_campaign/drive，含冻结负载定义） |
| loaders_stats1/ | --stats-interval-ms 观测改造 bundle（patch + provenance，BPF 零改动） |
| results/campaign/ | 112 正式 run + mq_v2 15 run + 两组 native 伴随补采（全部 RESULT=OK） |
| results/stray_logs/ | 37 个幸存 fio 时序日志（归属方法见 DATA_SUMMARY） |
| RESULTS/ | 8 张核心图（PDF+PNG）+ data.*.csv + DATA_SUMMARY.md + sentinel_drift.csv（125 run）+ 11 个可重跑脚本 |
| RUN_INDEX.csv | 127 run（112 正式 + 15 mq_v2）索引 |
| VALIDITY_REPORT.md | 逐实验有效性判定（E，含独立复算数字） |
| PAPER_EVIDENCE.md | 论文证据面：可安全声明/禁止措辞/归因实验清单/8×3 分级表（E） |

## 7. 结论

本轮以"每算法一个针对性实验、一张核心图"的纪律，在受控 VM 环境中完成了 8
个算法复现的行为级检验：**io.max 限速精度与 io.weight 份额语义达成与 native
等价的行为级证据**；kyber/io.cost/io.latency 的 IOX 反馈机制实测激活并暴露出
与 native 的真实行为分歧（深度收缩、vrate 调节、保护方向）；D2FQ 权重语义
不成立（已定位为待归因缺陷而非论文声明）；LinnOS 查表控制律精确执行。
所有"不支持/无法判定"的判定、机制分歧与环境缺陷均如实入档，并给出了可执行
的归因实验清单。上述结论以 VALIDITY_REPORT.md/PAPER_EVIDENCE.md 为准。
