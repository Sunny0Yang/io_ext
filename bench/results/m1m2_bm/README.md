# M1/M2' 动机图 · 出图脚本（`results/m1m2_bm`）

本目录保留 M1/M2' 动机图的**出图脚本**。按 artifact 口径，逐 run 原始数据、
汇总表与成品图均不随仓库发布；由 `experiments/m1m2_bm/` 的 campaign 重跑生成。

| 脚本 | 说明 |
|------|------|
| `scripts/render_m1_timeseries_bm.py` | M1 四负载 1×4 时序（MiB/s + in-flight + flush lane；读 sampler 日志） |
| `scripts/render_m1_2_bandwidth_bm.py` | M1.2 设备带宽热力图（48-case native matrix） |
| `scripts/plot_m2_fig2.py` | M2' 八臂动机条带 |
| `scripts/refresh_m1m2_bm_figures.sh` | 一键重绘（路径常量指向 campaign 产物目录） |
| `scripts/crop_figure_canvas.py` | 按内容 bbox 裁白边（不重绘） |

数据输入（campaign 产物）：`m1cacheon_bm_20261105`（M1 时序）、
`m1matrix_bm_20261105`（M1.2 热力图）、`m2fig2_bmr2644_20261105`（M2' 条带）。
采样日志解析器见 `results/motivation_m1/tools/m1_metrics.py`。
机器与口径见 `experiments/m1m2_bm/REPORT.md`、`M1_2_FIGURE_DESIGN.md`、`M2_FIGURE_DESIGN.md`。
