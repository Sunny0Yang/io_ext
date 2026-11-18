# dynamic_tenant — design-ref 出图脚本

本目录保留 dynamic-tenant 主实验的**出图脚本**。成品图与逐 run 数据不随仓库发布；
由 `experiments/dyn_tenant_bm/`（runbook + frozen contract + run 脚本）重跑生成。

| 脚本 | 说明 |
|------|------|
| `scripts/plot_kvmhost_all_compact.py` | 九臂 compact 总览（下排水平线 = 各相设计 σ(B+C+D)） |
| `scripts/plot_kvmhost_timeseries_combo.py` | 时序组合图（`collect_arm`） |

依赖：`scripts/fio_hist_p99_windows.py`。跑数入口：`experiments/dyn_tenant_bm/scripts/run_dyn_tenant_bm.sh`（L=90×6）。
