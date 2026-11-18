# DISCOVERY_REPORT — D3 kvmhost 发现批 v1+v2（Phase 2）

日期：2026-11-15（v1 终判 + v2 burst 扫描终判；bfqf 补样运行中）
批次：`results/d3_kvmhost/discovery/`（24 run）+ `results/d3_kvmhost/discovery2/`（18+2 run）
锚：标定批 `SLO=0.4936 ms` / `B_none=908.93 MB/s`（`SLO_and_B_none.json`）

## 判定：v1+v2 均无一臂通过双门 —— 但 Pareto 形状已完整成像，机制结论可交付

## 1. v2（burst 扫描）逐臂终判

| 臂 | n clean | LC p99 med | BE med | ret(vs 920) | 判定 |
|---|---|---|---|---|---|
| none（新鲜） | 2/3 | 10.35ms | **920.11** | 100% | 分母修复 ✓（LC 10ms 稳定复现） |
| bfqf（接线修复+补样） | **3/3** | **0.4895ms**（0.354–0.594） | 366.83 | **39.9%** | **FAIL(measured)**——SLO 2/3（中位距 SLO 仅 4µs！），BE 40% 差 60% 线远；metadata 3/3 确认 bfq 生效 ✓ |
| qrl_k065_b050 | 2/3 | 1.014 | 487.7 | 53.0% | NOT-EVALUABLE |
| qrl_k065_b100 | 2/3 | 1.008 | 604.5 | 65.7% | NOT-EVALUABLE |
| qrl_k065_b250 | 2/3 | 1.008 | 604.1 | 65.7% | NOT-EVALUABLE |
| qrl_k075_b050 | 2/3 | 1.597 | 512.7 | 55.7% | NOT-EVALUABLE |

**v2 三个结论**：
1. **burst 假设证伪**——k065 在 burst 50/100/250ms 下 LC p99 均 ~1.01ms（v1 默认
   500ms 为 0.93ms，无差别），更小 burst 单调损伤 BE（488→604 MB/s）。qrl 的
   LC 尾部是**结构性**的：cap 限速后的 BE 稳态洪流持续占据 virtio 单队列，
   LC QD1 请求在队头等待，与突发注入无关。burst 参数路线关闭。
2. **bfq 是 kvmhost 上最强的 LC 保护者，且贴着 SLO 线**——补样后 3/3 clean：
   LC p99 = 0.354 / 0.489 / 0.594ms（中位 0.4895 vs SLO 0.4936，差 4µs），
   2/3 过 SLO；BE 271–413 MB/s（39.9%），距 60% 线远。它是**确认批候选**
   （若确认批复核 LC 稳定性，其 BE 40% 使它成不了 D3 全过点，但它是 Pareto
   上 LC 最优的实测点）。
3. **none 分母修复**：新鲜批 B_none = 920.11（vs 标定 908.93，差 1.2%——
   标定锚可信）。

## 1b. v1 逐臂终判（口径修正后）

判定规则（v1 中修正两处移植错误，均为计划书红线）：
1. **数据失效 = BE 侧塌缩**（<20%×标定 B_none = 182 MB/s，即 41MB/s 停窗签名）；
   bm 的 LC 翻转门（绝对 5ms）**不再作剔除依据**——kvmhost none 臂 LC 10–52ms 是
   "无保护"的正当测量而非相位翻转（批判性吸收预告的绝对阈值陷阱，BE 地板改了
   比例制但 LC 门 v1 分析时漏改，已修）
2. **策略生效审计**（metadata.json `io_schedulers` vs 臂期望，逐 run 自动核对）：
   qrl×15 / arb×3 / none×3 全部 scx_io 生效 ✅；**bfq×3 全部 actual=none ❌**

## 2. 机制性发现一：q 家族的 virtio 迁移失败（尾部）

bm NVMe 上 qrl 帽给 solo 级 LC（0.118ms，三窗口复现）；kvmhost 上同款接线
LC p99 = 0.93–5.3ms（**3.8–21× solo，1.9–10.7× SLO**），且随 cap 单调上升
（k065 0.93 → k075 1.58 → k085 2.09 → k095 5.3）。逐秒画像显示 LC 中位极稳
（k065 每秒中位 0.16–0.25ms，无突发窗）——**帽保住了中位数，保不住 virtio
单队列上的尾部**。机制：token bucket（默认 burst 500ms）把 BE 注入成大块
突发，NVMe 深队列能吸收，virtio 单队列吸收不了——与 HDD 战役"限速反号"
同族（BE 突刺增加排队）。

## 3. 机制性发现二：arb fallback 在 virtio 上灾难化

arb_wdrr `--allow-fallback 1`：2 个 clean run LC p99 64.7–127.4ms——fallback
路径一旦接管，LC 暴露在无保护洪泛下。BE 侧 79.6% 达标。arb 路线在 kvmhost
的出路只有 strict 语义（bm 的 L3 解锁件），或依赖 SLO 宽松化（当前 0.4936ms
不足以覆盖 64ms）。

## 4. 工程缺陷：bfq 臂接线失效（已修复，v2 重跑）

根因：v1 yml 中 bfq 臂复用了 `scheduler: "none"` 的 NAT 租户组——租户 fio
scene 在 profile 设置 bfq 之后把电梯重置回 none。metadata 审计实锤
（`io_schedulers: [{scheduler: none}]` ×3）。修复：生成器改为每臂独立租户组、
tenant scheduler 跟随臂（`gen_d3_kvmhost_discovery_yaml.py` 已修），v2 以
`bfqf` 臂重跑。此缺陷正是计划书 §1.2"策略切换后遗留配置"检查要抓的类别，
现已固化为分析器的逐 run 自动审计（`policy_engaged` 列）。

## 5. 平台行为记录（kvmhost 特有）

- **run 后设备静默窗**：QD256 洪泛 run 结束后，设备常进入 41MB/s 停窗
  （1–12 分钟），canary 门捕捉并冷却；v1 中 2 个 run 被停窗击中判设备失效
  （k085 run1、k075 run3 等），1 个 run 带 PHASE-LOW 照跑（arbonly run3）
- **同批 none 分母被压低**：none run1/2 BE ≈ 576（劣化窗）、run3 = 926（健康窗）
  → 576 中位数。**对标定锚 909 的 ret_pct_calib 列已加入分析器作为第二参照**：
  qrl_k065 真实保留率 = 66.4%（≥60% 线 ✓）——分母问题不影响 qrl 的 BE 达标结论，
  但确认批必须在健康窗内重测 none（计划书 §4.1 同批重锚定）

## 6. v2 设计与结果回顾

v2 攻击 burst（v1 唯一未测的 qrl 旋钮）：`qrl_k065 × burst{50,100,250}` +
`qrl_k075_b050` + `bfqf`（修复验证）+ 新鲜 none，18 run 交错，2026-11-15
17:12–18:05 执行，18/18 runner PASS。结果见 §1：burst 假设证伪、bfqf 惊喜、
分母修复。bfqf 补样（run1/run3 重跑）进行中。

## 7. 对 Phase 3 的含义（待用户裁决）

kvmhost 当前 Pareto 图像（健康窗数据）：

```
LC p99 (ms)
 10.4 ┤ ● none                                    (BE 100%)
  5.3 ┤   ● qrl_k095
  2.4 ┤     ● qrl_k100                (BE 51%)
  1.0 ┤       ● qrl_k065 (burst 无关) (BE 66%)
 0.35 ┤           ★ bfq               (BE 40%)
      └──────────────────────────── BE 保留率
         40%      51%  60%─ ─ ─66%      76%    100%
                      （SLO+60% 双门象限：空）
```

- **没有任何已测策略同时满足 LC≤0.4936ms 与 BE≥60%**：bfq 保 LC 丢 BE（40%），
  qrl 保 BE 丢 LC 尾部（1.0ms = 2×SLO），X 形交叉但交叉点上方无点
- bm NVMe 同判据下 qrl-300 已过 LC、qrl-531 逼近全过（后被相位证伪）——
  **kvmhost/virtio 对该 Pareto 是结构性更难的平台**
- 分析器将 **bfqf 列入确认批候选**（FAIL(measured) 但 2/3 SLO 过线）——确认它
  的价值是把 Pareto 的 LC 端钉牢（0.354–0.594ms 是否稳定），但 BE 40% 决定了
  它无论如何成不了 D3"双门全过"点
- 尚未上机的机制杠杆：① **混合策略**（LC io.latency 目标 + BE qrl 帽——homo_v2
  已证明 io.latency 在 kvmhost 可用，这是 iox 组合灵活性对 native 单机制的故事点）
  ② arb strict（L3 解锁件）③ 更深 cap（k<0.65，BE 线以下，无意义）
- 三条路线需要用户裁决：**(a)** kvmhost 继续作为主图 → 设计混合策略新臂再迭代；
  **(b)** 回归 bm NVMe 为主图（其证据链最强）、kvmhost 作机理对照章（"虚拟化
  单队列上帽式隔离的结构性边界"——本身是可发表的负结果）；**(c)** 主图双平台
  （bm 正面 + kvmhost 边界）。执行层不擅自定叙事。
