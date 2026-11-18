# REPORT —— bm（7.3.1-ioext2 / 1TB DRAM-less consumer NVMe SSD）motivation 三图重跑验收

*2026-11-05；对照基线 = kvmhost（7.3.x guest + 96G 独立 sdb，results/motivation_*_20261031）。*

## 1. 执行汇总

| 实验 | RUN_ID | 状态 |
| --- | --- | --- |
| m1_1 cache=on（5 seq × 3 repeats） | `m1cacheon_bm_20261105` | **15/15 ok** |
| m1 cache=off 对照（用户裁定不进入证据链） | `m1cacheab_bm_20261105` | 12/12 ok（留档） |
| m1_2 native 矩阵（3 场景 × 4 scheduler × 3 repeats） | `m1matrix_bm_20261105` | **36/36 ok** |
| m2' 九臂（3 tenant × 9 旋钮臂） | `m2fig2_bmr2644_20261105`（最终）＋ `m2fig2_bm_20261105`（rate1536 存档） | **9/9 PASS**（两轮均全过、全 attempt 1） |

## 2. 逐图验收

### m1_1（block-IO fingerprints + 补充时序）
- 版式按用户指定口径复刻 `paper/dac_ioext/figs/m1_block_io_fingerprints.png`；bm 与 kvmhost 四面板
  **同构同型**：YCSB-A（12-48KB 周期缓冲读 + 128KB 主导写）、YCSB-C（早段密集 4-8KB 读 → cache
  吸收后消失）、db_bench（128KB 横线）、Shore（低率 4/8/32/64KB 线）。
- flush/s 与 kvmhost **同量级同排序**（bm: 4.98/3.16/9.95/0.17/109.0 vs kvmhost: 2.69/6.18/14.99/1.07/168.3
  —— YCSB_a/YCSB_c/dbbench/Shore）。
- 补充时序图 `m1_timeseries_bm`：**YCSB-C 逐秒块层读 900→<0.1 MiB/s 单调下滑 = 页缓存吸收的
  直接时间序列证据**（cache=on 口径最有力的动态特征）；DBB rww inflight 锯齿、Shore 满负荷
  flush/s 最高且吞吐低两个数量级——四负载在三条通道（吞吐比/队列动态/flush 频率）上完全可分。

### m1_2（native matrix）
- **rotation 模式成立（与 kvmhost 同向）**：YCSB-A READ P99 臂序 none<kyber<mq-deadline<bfq
  完全一致，bfq 惩罚 bm +31.5%（4.41→5.79ms） vs kvmhost +35%（7.08→9.58ms）；
  YCSB-A UPDATE P99 的最优臂切换到 bfq（10.79ms）而 none 13-15ms——"没有单一调度器全胜"
  （none 5/7 wins）与 kvmhost 结论一致。
- db_bench fg_read_p99 两边都是平的（bm 21.6-21.9µs 1.3% 内，kvmhost 32.5-32.7µs 0.7% 内）
  —— 该场景本就不是分化源，与旧版一致。
- 全部 36 case gate_ok=3/3；population SD ≤2% 的臂在图中以 ≈/hatch 诚实标注。

### m2'（九臂，最终口径 rate=2644）
- **两轮口径**：首轮 rate=1536（沿用 paper 截断值）9/9 PASS 但三方竞争仅压低 26%
  （3×1536=4608 需求 vs 设备并行 ~3.8-4.5G，余量 1.39×；kvmhost 为 0.8×solo=1062、
  需求 2.4× → 压低 48%）。最终口径改按 **kvmhost 方法 rate=0.8×solo=2644**（无截断，
  io.max 同比例 2582/861），九臂 9/9 PASS，数据 `runs/m2fig2/m2fig2_bmr2644_20261105`。
- **最终图 `figs/m2_fig2_bmr2644.png` 九面板与 kvmhost 参考逐一同构**：
  (a) A solo 2650 → 三方竞争 ~1100（压低 58%）→ B solo 3100；(b) A 钉帽 2644、
  C restrict-to-be starve；(c) BFQ 三方交叠震荡带；(d) B@weight1000 反超 A；
  (e) B/C 精确贴 861 io.max 帽；(f) io.latency 目标收敛（含 kvmhost 同款暂态）；
  (g/h) io.cost 模型+权重分化。
- 首轮"看似跑炸"为**绘图 bug 非数据问题**：plot 的单位探测启发式（p90>1.5×cap ⇒
  bytes/s）被 bm 快盘的 catch-up burst（3.2 GiB/s）触发，把 KiB/s 日志再除 1024²，
  含 burst 租户被画成 ~0。已修：scale 用租户 fio json 聚合 bw（KiB/s 定义单位）
  锚定（scripts/plot_m2_fig2.py `_agg_bw_mib`），对 kvmhost 旧数据行为不变。
  逐租户对账：首轮数据本身健康（A/B/C 聚合与日志一致），保留存档。
- WARN 项（不阻断）：catch-up burst 峰值超 1.15×cap（fio rate 补偿，kvmhost 同现象）、
  B idle 类时间线覆盖不足、A 起步 ramp（fio rate pacing 预热，~10s 内到位）。

## 3. 设备效应归因（bm vs kvmhost 定量差异）

| 项 | kvmhost | bm | 归因 |
| --- | --- | --- | --- |
| YCSB-A read p99 绝对值 | 7.1-9.6 ms | 4.4-5.8 ms | 在快盘（DRAM-less NVMe）上快 3-5×，块层响应整体前移 |
| db_bench fg read p99 | 32.5 µs | 21.6 µs | 同上 |
| Shore p99 延迟 | 1.28-1.52 s | 63-73 ms | 同上（20×）；SLO 50ms 超限幅度同步收敛 |
| DBB_rww inflight 均值 | 23.7 | 207 | 快盘完成更快→瞬时并发可堆得更高（排水快） |
| DBB_rr inflight/cv | 1.45 / 4.57 | 0.03 / 8.67 | 同上 |
| Shore 绝对吞吐 | MB/s 级 | MB/s 级（更低） | open-loop QPS50，盘速无关 |

分化**方向与幅度**（%)在 device 整体加速下保持一致——结论对设备更换稳健；
绝对数值不可与 kvmhost 直接混用（figure 内不跨机混算，热力图按行归一）。

## 4. 运行插曲（时间线见 LEDGER §5）

1. m1cacheon 首轮 12/15 失败：root 属主数据目录 → 复位 rm 改 `sudo -n`；无脏数据混入
2. 一次物理链路失联（无重启，partner 回 10baseT 降级 advertise）；SHORE_r1 已打包未传：
   salvage 回收 + KPI 验证后标 ok
3. m1matrix 首轮 SHORE 12 case 全灭：`chrt -r 99` EPERM（无 cgroup→无 sudo 包装）→
   补 accounting-only io_ext.cgroup 修复
4. m2' 首轮 9 臂全灭：`src/lib` 漏同步（fio scene 依赖）→ 补传；runner 两 bug
   （`[$target]` 占位符、远端 retry 不清 tar）修复后第四轮 9/9 attempt1 通过

## 5. 图目录

```
figs/m1_block_io_fingerprints_bm.{png,pdf}   # 用户指定最终口径
figs/m1_timeseries_bm.{png,pdf}              # 补充时序（四负载三通道分离）
figs/m1_2_policy_conflict.{png,pdf}          # bm matrix heatmap（rotation 成立）
figs/m2_fig2_bm.{png,pdf} (+_summary.json)   # m2' 九臂
figs/m1_1_behavior_profile.{png,pdf}         # 面板聚合版（补充材料候选，非最终口径）
```

kvmhost 原版证据链（results/motivation_*)保持只读不动；bm 证据链独立成档。

## 6. g/h 臂实测 io.cost 系数重跑（2026-11-07，决策 7 收尾）

- r2644 轮的 (g) io.cost / (h) io.cost+io.weight 原用 linear-from-solo fallback
  模型；已用 bm 实测系数（dyn_tenant_bm contract，rbps=1878M 等）重跑两臂
  （RUN_ID `m2fig2_bmr2644_ghmeasured_20261107`，双双 attempt-1 PASS）。
- 注入零代码改动：新配置目录 `configs/m2fig2_rate2644_ghmeasured/`
  （calibration 仅换 `iocost_model`，qos 预声明常量不动，fallback 原值留档），
  runner 走既有 `CFG_DIR` 开关；其余七臂数据与历史未动。
- 结果对比（fio 聚合，详见 `REPORT_GH_RERUN.md`）：实测模型容量 ~1791 MiB/s
  低于 rate 帽，io.cost 首次真实参与调控——(g) 三方 bw -23~-32%、p99 +43~+52%、
  同权成本分摊现分化；(h) 权重分层在真竞争预算上保持（A/C 1.35×，validator
  分离判据 PASS），C p99 因 QoS min 保护改善 -25%。
- 红线核查：全程仅文件 IO（32G m2f2_data.bin）；跑后已还原 root io.cost
  （qos enable=0 / model ctrl=auto——遗留 enable=1 与 2026-11-06 "RECAL 毒"
  同型，已显式清除）+ scheduler [none] + 无残留 cgroup/fio。
