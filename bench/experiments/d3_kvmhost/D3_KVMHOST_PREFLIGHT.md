# D3_kvmhost_PREFLIGHT — Phase 0 工程资产审计与实验入口检查报告

日期：2026-11-15
执行方：ZCode 主会话（计划书：D3 kvmhost Campaign Plan，批判性吸收版）
判定：**Phase 0 PASS —— 授权进入 Phase 1（标定）**
关联：`HANDOFF_D3_kvmhost_数据现状与策略候选_20261115.md`（数据现状与策略候选）

---

## 0. 判定与授权

Phase 0 全部检查项通过（见 §1–§4）。Phase 1 标定批（LC solo n=5 + none
contention n=5）已获得执行授权，runner 与 YAML 已就绪并待发。

对计划书的批判性修正（已在本文与工具中落地）：

| 计划书原表述 | 修正 | 理由 |
|---|---|---|
| BE 相位地板 600 MB/s（§1.5/bm 沿用） | **比例地板**（<20%×B_none 或逐秒分布突变判定） | 600 是 bm NVMe（B_none≈2979）的 ~20% 比例值；kvmhost B_none≈950，600=63% 会把正常方差误判为翻转 |
| arb_wdrr 入批阈值 SLO≥0.6ms | **SLO≥0.5ms 即入发现批**（bm arb 中位 0.379 + 30% 余量），0.6 保守线保留 | 每臂仅 +3 run 边际成本；0.6 可能错过真实可行窗口 |
| Phase 0 只审不测 | **新增分析链路 dry-run**（self-test + bm 真实数据回归） | 标定数据产出后才发现分析代码不通是最贵的失败模式 |
| region 文件规划 | **不需要**：kvmhost 走裸设备 /dev/sdb（D2 rerun 先例），bm 的 region.bin 布局不适用 | 96G 盘为未挂载独占裸盘 |

---

## 1. 平台检查（全部通过，2026-11-15 实测）

| 检查项 | 结果 | 证据 |
|---|---|---|
| SSH 连通 | ✅ BatchMode 直连 | ssh_env.sh（SSH_HOST/PORT/USER） |
| 主机 | `target-host`，up 11:48h，load 0.04 | uptime |
| 内核 | `7.3.1-ioext3+`（硬契约满足） | uname -r |
| 96G 实验盘 | ✅ `sdb` 96G，未挂载、无分区、无 holder | lsblk；3s 两次 /sys/block/sdb/stat 完全一致（**空闲**） |
| root 盘 | /dev/sda2 46G，余 19G（结果暂存足够） | df |
| scx_io 可用 | ✅ `[none] mq-deadline kyber bfq scx_io` | /sys/block/sdb/queue/scheduler |
| fio | ✅ fio-3.41（与 bm 同版） | fio --version |
| 遗留进程 | ✅ 无 fio/loader/executor | ps |
| 遗留 cgroup | ✅ 无 ib_* 残留 | ls /sys/fs/cgroup |
| 资源 | 6 CPU / 15G RAM（12G free） | nproc; free |
| 远端仓库 | pin 冻结版执行器（与 D2 rerun 同版，利于可比） | git rev-parse |
| /tmp | tmpfs 7.8G 空；仅 D2 标定 json 残留（无害） | ls /tmp |

设备身份纪律：不依赖 `sdb` 字面名，runner 每 run 前按 **201326592 sectors +
无 holder + 无分区** 唯一匹配重发现（复用 D2 rerun 的 discover_device）。

## 2. 策略资产检查（全部就位）

远端 `/opt/io-ext/userspace/` 实测存在：
`loader_q_fifo`、`loader_q_ratelimit`、`loader_arb_wdrr`、`loader_arb_slo`、
`loader_arb_cost`（+ 各 .bpf.o/.skel.h）。

D2 rerun（2026-11-14）已在 kvmhost 实跑 9 臂含
`iox_q_rl / iox_arb_wdrr / iox_arb_slo / iox_arb_cost`——**单 q 与单 arb 路线
零部署成本**，确认与 HANDOFF 判断一致。

策略语义确认（供 Phase 2 参数推导）：
- q_ratelimit：per-cgroup `--rate-mib-s`（二进制 MiB/s）+ `--burst-ms`（默认 500）
- D3 确认批布线沿用 bm 冻结口径：LC=loader_q_fifo `-p0` 直通无帽；
  BE=loader_q_ratelimit `-p7 --rate-mib-s M(k)` 每 BE 独立
- arb 系 loader 的权重/fallback 参数在 Phase 2 前需逐项核对 D2 rerun yml
  （`gen_d2_kvmhost_rerun_yaml.py` 的 iox_arb_wdrr_cg4 profile 为基准）

## 3. Runner / Analyzer 审计

**复用决策（不重复开发）**：
- runner 骨架 = `scripts/run_d2_rerun_kvmhost.sh`（反楔死全套：每序列设备重发现、
  canary 门、硬墙钟、forensics-before-recovery、单轮 requeue、增量 pull）→
  适配为 `scripts/run_d3_kvmhost_calib.sh`
- YAML 模板 = bm 冻结的 `d3_lc_bm.yml`（LC/BE 形状、percentile_list、cgroup
  结构逐项保留）→ 生成器 `scripts/gen_d3_kvmhost_calib_yaml.py`
- 逐秒遥测 = system_metric 场景（1s diskstats）+ **新增** fio
  `write_bw_log/write_lat_log/log_avg_msec=1000`（fio_global 透传，fio.py:107
  确认为自由 dict 合入 [global]）；busy 秒统计复用
  `scripts/diagnose_d2d4_bm_phase_seconds.py`（支持 device 覆写）

**分析链路 dry-run（新增项）**：
1. `analyze_d2d4_bm.py --self-test` ✅ 全部断言通过（D2/D3/D4 指标块、门、
   相位分类）
2. bm 主批真实数据回归：`--results-root results/isol_d2d4_bm --slo-ms 0.2365`
   端到端跑通（tar 解包 → 指标提取 → 门 → retention → calib 锚复算）
   - **calib 锚复算精确命中 0.1183 ms**（calib tar 与文档锚一致 ✅）
   - ⚠️ **发现历史批溯源断链（详见 §5）**

## 4. Manifest schema（v1，写入每 run 的 manifest.json）

| 字段 | 来源 | 计划书要求覆盖 |
|---|---|---|
| run_id / batch_id / arm / iteration | runner | ✅ |
| hostname / uname_r / kernel_build / fio_version | batch_env 快照 | ✅ |
| device / device_size_bytes / device_model / scheduler_available | batch_env 快照 | ✅ |
| workload_config + workload_sha256 | yml sha256（每 run 重算） | ✅ 配置可追溯 |
| strategy / strategy_params / cap_units | runner | ✅（标定批=none） |
| cgroup_paths | runner（按 solo/contention 区分） | ✅ |
| remote_git_commit | batch_env 快照 | ✅ 执行器版本 |
| fio_json_paths / raw_log_paths（含 bw/lat 逐秒日志） | 扫描结果目录 | ✅ |
| canary_mb_s / post_gate_agg_mb_s / runner_rc | runner 实测 | ✅ |
| clean_gate | `pending-analysis` → 分析器回填 | ✅ |
| start_ts / end_ts | batch_env + 落盘时刻 | ✅ |

硬规则（计划书 §1.4）：hostname/uname/arm 参数缺失的 run 不进主分析表——
由分析器校验 manifest 完整性后回填 clean_gate。

## 5. 重大审计发现：bm 主批分析↔原始数据断链

用现行 `analyze_d2d4_bm.py` 对 `results/isol_d2d4_bm/` 归档 tar 复算 D3，
与归档 `analysis/d3_runs.csv` **不一致**（同 sequence_id，不同数值）：

| run | 归档 CSV | 复算（归档 tar） |
|---|---|---|
| io_cost run1 | p99 0.1587 / BE 1006.87 | p99 0.1183 / BE 1508.0 |
| iox_qonly run1-3 | 0.3953–0.4649（clean） | 25.3/24.0/28.2 ms（**全判 LC flip**） |
| none run1 | 1.4008 / 596.39 | 1.3189 / 2239.66 |
| none run3 | 1.3517（clean） | 1518.338 ms / BE 4.1（**flip+flip**） |

- 归档 CSV 无 device_phase 列（中间版分析器产物）；复算结果带 phase 列与
  reason，且复算值与 v21/alt2 时代的文档数值同族（如 none 1.3189 =
  v21_d3_ddl none；1518.338 = RUNBOOK 记载的 none 巨方差 rep）
- 判定：`results/isol_d2d4_bm/d3/` 目录中现存的臂 run tar 与
  `analysis/d3_runs.csv` 描述的**不是同一批执行**（目录被某次重收集覆盖，
  或 CSV 来自另一数据代）。calib tar 一致，断链仅在臂 run。
- 影响：EVAL_MASTER 中源自该 CSV 的 9 行主批数值**不可从原始数据重建**。
  不改变策略结论（结论立于 pair_n3/highcap/GF 批），但坐实计划书红线：
  - 确认批数据必须"从原始 fio JSON + manifest 独立重建"（§5.1）
  - analyzer 结果与原始 JSON 不一致 → 暂停批次（§6）
- 处置：两份 CSV 均保留；断链根因排查列为低优先级悬案（D3-PROV-001），
  不阻塞 kvmhost 战役。

## 6. 逐秒遥测定义（Phase 1 起强制）

1. **设备级**：system_metric 场景 1s 采样 diskstats（util%、ios_in_progress、
   read_sectors_delta）→ `system_metric_summary.json`；busy 秒定义 = util≥80%
   且 ios≥1（与 bm 诊断脚本口径一致）
2. **任务级**：fio `write_bw_log`/`write_lat_log`（log_avg_msec=1000）→ 每
   LC/BE job 每秒 bw/lat，BE 聚合与单 BE 分解均可重建
3. 分析侧保留逐秒原始数据；**均值/P99 只作汇总，不作门判唯一依据**——
   逐秒分布（连续低吞吐区间、busy 秒比例）必须入报告（bm 聚合门盲区的修正）

## 7. Phase 1 执行计划（已授权）

| 项 | 值 |
|---|---|
| 批次 | `results/d3_kvmhost/calibration/`，10 run = 5×`D3PV_lcsolo_none_runN` + 5×`D3PV_none_runN` |
| 负载 | LC 4k randread QD1 60s+ramp20；BE 4k randread QD256（none policy，裸 /dev/sdb） |
| canary | 6s QD256 探针 ≥556 MB/s（0.6×D2 solo 锚 926.6，仅相位门，不入标定值） |
| 单 run 预算 | 墙钟 420s（实际 ~150–180s） |
| 总预算 | ~25–35 min（无楔死） |
| 产出 | `lc_solo_runs.csv`、`none_contention_runs.csv`、`SLO_and_B_none.json`、`CALIBRATION_REPORT.md` |
| 通过条件 | solo≥3 有效（目标 5）；cont≥3 有效（目标 5）；锚无未解释双峰/漂移；manifest+遥测完整 |

标定分析器（`scripts/analyze_d3_kvmhost_calib.py`）待标定数据落盘后编写——
先看真实数据形态再定逐秒统计的具体切面，避免闭门造车。
