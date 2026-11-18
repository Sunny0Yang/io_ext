# M2 动机图：筛选、时间与证据口径（2026-11-06）

最终选定：`figs/m2_motivation_bmr2644_1x8.{png,pdf}`，174 × 40 mm，1×8，正方形面板，首末样本均为空心圆，无活动条、caption 或参数长注释。
备选：`figs/m2_motivation_bmr2644_1x6.{png,pdf}`，174 × 42 mm，1×6，末样本为空心圆。
活动生成器：仓库根目录 `scripts/plot_m2_fig2.py`，两版均由该脚本生成。
其余历史图件、两个脚本备份及当前生成器/文档快照归档于 `archives/m2_plot_versions_20261106_110935/`，原路径及 SHA256 见其中 `archive_manifest.json`。原始实验数据未移动。

最终 1×8 顺序：`none`、`mq-deadline`、`bfq`、`io.bfq.weight`、`io.max`、`io.latency`、`io.cost`、`kyber`，仅排除 `io.cost + io.weight`。包含 BFQ 加权未观察到预期份额分化的结果；不据此宣称权重原语普遍有效。

## 备选六面板的选择依据（沿用早期简称；当前图标题为具体原语名称）

| 主图标题 | 原臂 | 机制/配置 | 展示目的 |
|---|---|---|---|
| None | a_none | native none，无控制 | 对照与竞争/启动暂态 |
| Prio | b_mqdl_prio | MQ-deadline + io.prio.class，A promote-to-rt，B idle，C restrict-to-be | 优先保护及低优先级代价 |
| Cap | e_io_max | io.max，A 2582、B/C 861 MiB/s | 上限不等于预留，其他租户退出后仍受静态帽约束 |
| Latency | f_io_latency | io.latency，A/B/C 20/1000/100 μs | 反馈暂态及退出后恢复不足 |
| Cost | g_io_cost | io.cost，相等权重 | 本轮同质读流中的分段份额 |
| Cost+W | h_io_cost_weight | io.cost + io.weight，A/B/C 1000/1/100 | 相对 Cost 的权重偏向及 B 受压 |

备选六面板用于紧凑覆盖不同控制机制并保留无控制参考。最终八面板进一步纳入 BFQ、BFQ 加权和 Kyber，并按用户选择排除成本加权；完整九臂图留在归档。BFQ 加权未出现预期份额分化是负结果；Kyber 的稳定平台也不能用于宣称所有 native 机制均不稳定。最终图不代表遍历所有原生策略。

三个租户均为 fio 64 KiB randread、QD8、单 job、io_uring、direct=1，rate=2644 MiB/s，读取同一预分配文件。它们是错峰启停的同质租户，不是三个不同真实应用。这里只使用最终 rate2644 轮次，不混入 rate1536 数据。

## 时间与图形编码

每个面板以三个租户中最早的 fio `jobs[0].job_start` 为零点。曲线样本时间 = `(job_start_ms - panel_origin_ms + log_time_ms) / 1000`，不重复添加 startdelay。

- 最终 1×8：曲线首个/末个记录到的带宽样本均为空心圆，纵坐标来自该样本。
- 备选 1×6：末个样本为空心圆；两版均不绘制活动时间条，仅保留普通时间刻度。历史二维/九臂版本的活动条已随图归档。
- 这两类时间不同：常规首样本在启动后约 1 秒；Prio-B 首样本在作业开始后 10 秒。不能把首样本解释成 job_start，也不能在没有样本的时段伪造吞吐值。
- 超过 1.5 秒的日志间隔断线；稀疏租户额外显示采样点，不补零、不平滑。
- 颜色和线型分别区分 A/B/C，横纵尺度在主图和九臂图之间一致。曲线纵轴从 MiB/s 转为 GiB/s，避免冗长刻度。
- 同时生成 `_timing.json`，包含各租户实际起止、首末样本、采样个数、空档和单位除数。
- `_summary.json` 为已有日志样本的算术均值，用于追溯；不能用不同存活窗口的该均值推断共同竞争期公平性。Prio-B 的稀疏样本均值尤其不能当成全运行吞吐。

## 对旧报告的纠正与论证边界

历史 REPORT.md / LEDGER.md 原记录保留，但其部分描述不能直接用于论文：

1. BFQ+weight 的实际 A/B/C 权重是 1000/1/100，非 B=1000；B 全程均值较高混入了其独占阶段，不能声称竞争期 B 高权重反超 A。
2. io.latency 图仅显示带宽反馈与恢复行为；没有证明延迟目标收敛，不能把它归为具有显式带宽上界的静态限流器。
3. None-A 起始存在明显 ramp，没有稳定的 2650 MiB/s 独占平台，不能沿用“下降 58%”而不定义测量窗口。
4. Cost 的分段平台只支持本次同质读负载、当前模型参数下的观察，不能推广为长期公平性或保证。Cost+W 需要同时呈现 B 近乎饥饿的代价。
5. calibration.json 的 rate_mib_s 已是 2644，但部分参数说明仍留旧 1536 字段；实际参数应核对最终 YAML、fio job options 和运行回读。

论文中图2相关句子建议：

> 图2展示了三个错峰启停的同质 I/O 租户在不同原生控制原语下的带宽轨迹：优先级与权重设置产生偏向性的资源分配；静态带宽上限在竞争者退出后仍约束租户吞吐；延迟反馈控制呈现不同的干预暂态与恢复行为；成本模型在本次实验中形成较稳定的分段份额。不同机制因而体现出不同的保护、共享和利用率权衡。

更强的“必须动态仲裁”结论仍需结合系统设计/服务目标论证；图2本身没有直接比较动态仲裁方案。

## 复现

在仓库根目录运行：

```bash
python3 scripts/plot_m2_fig2.py \
  --results experiments/m1m2_bm/runs/m2fig2/m2fig2_bmr2644_20261105 \
  --layout eight-row --out experiments/m1m2_bm/figs/m2_motivation_bmr2644_1x8
python3 scripts/plot_m2_fig2.py \
  --results experiments/m1m2_bm/runs/m2fig2/m2fig2_bmr2644_20261105 \
  --layout motivation-row --out experiments/m1m2_bm/figs/m2_motivation_bmr2644_1x6
```

程序会检查所有可见文字是否超出画布。已核对 27 条租户时间线均有实际 fio 起止且样本在运行范围内；27 条日志的单位除数均为 1024（KiB/s→MiB/s）。Prio-B 有 23 个样本、最大空档 11 秒；其余租户约 1 秒一个样本。视觉验收代理连接不可用，当前模型也无法接收图像；文字边界和 PDF 文本核查完成，但不能替代完整视觉验收。
