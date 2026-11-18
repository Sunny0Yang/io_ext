# M1_2 调度器偏好冲突图（2026-11-06）

成图：`figs/m1_2_policy_conflict.{png,pdf}`，固定 68 × 48 mm，适合与 104.4 × 48 mm 的 M1 时序图并排。生成器为 `scripts/render_m1_2_policy_conflict_bm.py`（相对本实验目录）。

## 数据与筛选

使用 `runs/m1_campaign/m1matrix_bm_20261105/native_matrix_{cases,summary}.csv`，四负载 × 四调度器 × 三次重复，共 48 个通过 gate 的 case，含实测 YCSB-C。不使用 mock 数据，不重算或改写输入 CSV。

保留五行：YCSB-A READ P99、YCSB-A UPDATE P99、YCSB-C READ P99、db_bench fg READ P99、Shore P99。删去 db_bench 后台写带宽、Shore P50 和 deadline-miss，使所有展示指标统一为 P99 延迟。原始指标仍留在 CSV 中。

负载名称与 M1 其他图一致，负载和指标分为两列；YCSB-A 名称在两行之间居中。单元格仅显示两位小数，无倍号、近似符号、方向箭头和物理单位；不绘制 caption。标题仍保留 `mq-deadline`（分两行）。

## 归一化与颜色

每格数值 = 该调度器三次重复的 P99 均值 / 本行最低均值；先聚合各次运行的 P99，再进行归一化。1.00 为本行最低观察均值。粗体与边框仅标记该均值，不意味着统计显著优胜。三次重复的 SD、范围与范围重叠诊断保留在输入和脚本终端输出中，不显示在图内。

颜色控制点：1.00 `#FFFFFF`，1.10 `#FAD4A6`，1.15 `#DE741C`，1.25 `#A50F15`。分段插值形成非线性视觉映射，所有 >=1.25 的颜色饱和为深红，数值不截断。色条横置于底部，最高刻度标注 `≥1.25`。文本颜色根据背景亮度选择黑/白。

| 指标 | none | kyber | mq-deadline | bfq |
|---|---:|---:|---:|---:|
| YCSB-A READ P99 | 1.00 | 1.11 | 1.21 | 1.31 |
| YCSB-A UPDATE P99 | 1.36 | 1.29 | 1.43 | 1.00 |
| YCSB-C READ P99 | 1.06 | 1.05 | 1.00 | 1.03 |
| db_bench fg READ P99 | 1.00 | 1.01 | 1.01 | 1.01 |
| Shore P99 | 1.00 | 1.03 | 1.15 | 1.16 |

## 论证边界与后续 caption

同负载操作冲突最直接体现为 YCSB-A READ 的最低均值在 none，UPDATE 的最低均值在 bfq。跨负载可观察到最低均值位置变化及惩罚幅度差异，但 YCSB-C 的四臂重复范围均与最佳臂重叠，不能宣称其 mq-deadline 优势显著。db_bench 最大均值差异约 1.3%，仅支持该场景对这些调度器不敏感、none 均值最低；没有独立测量调度软件开销，不能据此确认“规避软件开销”的因果机制，也不能推广到所有高并发压测。保留这两行使弱差异结果也可见。

后续 caption 可统一说明：Normalized P99 latency relative to the lowest scheduler mean in each row (three runs); 1.00 is best. Bold outlined cells mark the lowest observed mean; colors saturate at 1.25 while annotations retain actual values. 当前未修改论文源文件或加入图内 caption。

## 复现与验收

在仓库根目录执行（系统 Python 有 numpy/pandas/matplotlib；项目 `.venv` 缺 numpy）：

```bash
python3 -I experiments/m1m2_bm/scripts/render_m1_2_policy_conflict_bm.py \
  --data-dir experiments/m1m2_bm/runs/m1_campaign/m1matrix_bm_20261105 \
  --fig-dir experiments/m1m2_bm/figs
```

已核对 48 个 case 与汇总表的 208 项统计一致，五行 20 格归一化数值正确，四格颜色饱和，所有可见文字不越界且两两不重叠。PNG 为 803 × 566 px（300 dpi）；导出不使用 tight cropping，保留固定物理画布。

视觉验收代理因账号连接不可用未能启动，当前模型也无法接收图像；程序检查不能替代完整视觉验收。

修改前脚本与 PNG/PDF 已复制并以 SHA256 验证，存于 `archives/m1_2_before_compact_20261106_112115_460159/`，清单为其中的 `manifest.json`。未改动其他 M1/M2 图件与原始实验数据。

## 设备带宽补充版（2026-11-06）

独立成图：`figs/m1_2_policy_conflict_bandwidth.{png,pdf}`；case 窗口审计：`figs/m1_2_policy_conflict_bandwidth_timing.json`；生成器：`scripts/render_m1_2_bandwidth_bm.py`。现有 P99 图及生成器保持原状。带宽图为 68 × 44 mm；热图区保持原 0.50∶0.52 长宽比（`0.44×0.458` fig 归一化）；中间 `6.2%` 宽列为 Read/Write，右侧色条等高、刻度在条外。

这张图不是把应用吞吐、物理带宽和逻辑读写操作混为一谈：所有数值均来自目标整盘 `nvme0n1` 的 `/proc/diskstats` sector 增量，按 512 B/sector 与 `2^20` B/MiB 换算。每个 case 先按该 workload 的实测窗口计算 MiB/s，再对每个 scheduler 的三次重复取算术均值。热图每一行除以该行最高 scheduler 均值，因此 1.00 表示该 workload/方向下观察到的最高物理带宽，较低值表示其相对保留比例；不是延迟图的“1.00 最佳、越大越差”语义。底色在 0.45 以下饱和深红，1.00 为白色，数字保留实际比值。

窗口定义：YCSB-A/C 使用原始 `io_stat_snapshots.before_run` 到 `after_run` 时间戳，并与 `overall_RunTime(ms)` 交叉核对（容差 3 s）；db_bench 使用 `readwhilewriting.io_delta.before_ts` 到 `after_ts`；Shore 从 scene `start_time + 20 s`（1000 个 warmup 请求 / 配置 50 QPS）到 scene `end_time`。对每个 1 Hz sector 样本按采样区间与 ROI 的重叠比例加权，确保窗口两端不因整数秒边界而多算/少算整秒。

保留五行：YCSB-A physical Read/Write，db_bench RWW physical Write，Shore physical Read/Write。YCSB-C 读写带宽在四调度器间最大均值差约 3%，故不纳入冲突证据图；db_bench 物理 Read 约 0.02–0.04 MiB/s，重复波动较大，故省略。Shore 是固定 50 QPS offered-load；其 physical bandwidth 仅表征设备活动，不代表应用可完成更多请求。YCSB-A 也是 buffered RocksDB，physical Read/Write 包含 block cache、WAL、flush/compaction 等影响，不能对应解释为逻辑 READ/UPDATE 带宽；db_bench readwhilewriting 只归因到整个 workload 阶段，不能把设备字节归属到 foreground/background 单独参与者。因此，这版用来观察调度器与工作负载相关的物理设备流量变化，不替代 P99 的应用服务质量解释。

正式 M1_1 指纹主图展示 sampled request size（bytes）、read/write 方向与 flush activity；它本身不是带宽图。`m1_timeseries_bm_1x4` 才是补充的 raw block-device read/write MiB/s 时序。因此，M1_2 P99 与 M1_1 正式主图之间并非同一指标自相矛盾；新带宽版用于补充调度器对设备流量的影响。

复现：

```bash
python3 -I experiments/m1m2_bm/scripts/render_m1_2_bandwidth_bm.py \\
  --data-dir experiments/m1m2_bm/runs/m1_campaign/m1matrix_bm_20261105 \\
  --fig-dir experiments/m1m2_bm/figs
```

带宽图建议 caption：*Normalized device read/write bandwidth from `/proc/diskstats` on nvme0n1, averaged over three runs; within each workload and direction, 1.00 is the highest observed mean. Values are physical whole-device traffic, not application operation throughput.* 图内不嵌 caption。
