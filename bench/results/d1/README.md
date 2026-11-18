# D1 论文图 · cost + scalability（`results/d1`）

本目录保留 D1 图的**出图脚本**与说明。按 artifact 口径，聚合汇总表（`data/*.csv`）
与成品图（`figures/`）不随仓库发布——由 `experiments/dyn_tenant_bm/scripts/` 的
D1 ladder campaign 重跑生成数据后，用下方脚本重绘。

| 脚本 | 说明 |
|------|------|
| `scripts/render_d1_paper_figures.py` | 渲染 (a) runtime cost + I/O path timing、(b) fixed-load IOPS + idle-cgroup p99 |
| `scripts/refresh_d1_figures.sh` | 一键重绘（读 `data/ladder.csv`、`fixedload16.csv`、`idle.csv`、`e6_segments.csv`、`e6_pricing.csv`） |

上游完整 ladder 表与 manifest 由 campaign 产物 `D1_ladder_v3_figures/` 提供。
口径与叙事见 `experiments/dyn_tenant_bm/D1_OVERHEAD_bm.md`。
