# io_ext 复现 campaign 正式数据摘要（DATA_SUMMARY）

- 数据源：`results/campaign/`（campaign id `repro_20261113_183153`，guest `ioext-repro-vm`，kernel 7.3.1-ioext3，fio-3.28，blkdev vdb）。
- 覆盖 8 实验 × 5 rep × 2-3 臂，共 **110 个正式 run + mq_deadline_v2 重跑 15 run（70/30 口径），`RESULT.json` 全部 OK**；linnos 另有 `diag/rep0` 两个诊断 run，**不计入正式统计**（任务约定）。
- 本文由 `RESULTS/scripts/generate_summary.py` 生成；各图底层点数据在 `RESULTS/<algo>/data.<algo>.csv`，哨兵逐 run 数据在 `RESULTS/sentinel_drift.csv`。

## 0. 数据完整性与已知缺口（先读）

1. **fio 逐窗/per-IO 日志未入 run 目录（EXPERIMENT_MATRIX 附录 D1，最终表述）**：`write_bw_log/write_lat_log` 落在 fio 进程 CWD（campaign 启动目录），同名逐 run 覆盖 → 每 run 的时序日志不可恢复；每实验**最后一轮（rep5 iox）**的日志幸存（37 个文件，已拉回 `results/stray_logs/`）。缺陷根因已定位（runner gzip 只覆盖 RUN_DIR；fio 日志路径与 CWD 绑定），后续 campaign 修复（fio 工作目录设为 run 目录）。影响与处置：①时序面板改用 arbiter/loader 1s 统计账本（5 个 instrumented loader 的 stdout 在 run 目录内）与 fio JSON 分相（3×30s）百分位；②幸存单 run 序列作**示例序列**画入 fig.io_max / fig.io_weight / fig.d2fq（图上标注 example series, rep5 iox）；③各图完整 3臂×5rep 1s 序列仍缺失（图内红字注明）。
2. **幸存日志归属方法**：37 个文件的 mtime 全部相同（打包时刻，guest 侧 cp 丢失原 mtime），**mtime 归属不可用**；改用**内容签名归属**——行数（60/90/30 行 ↔ run 时长）、方向列（dir=0/1）、逐窗均值与对应 rep5 iox fio JSON 全 run 均值精确对照：`lat_iomax_*`（32.01 vs 32.00 MiB/s）、`lat_w_*`（份额均值 0.774 vs 字比 0.792，一致）、`lat_t100/t400_*`（205.1 vs 204.9 MiB/s）、`mq_*`（130.6 vs 130.6 MiB/s）、`lat_p4_*`（仅 linnos 有 p4 相位；3300.5 vs 3309.8 MiB/s）→ 归属成立。`lat_p1/p2/p3_{lc,be}_*` **不可归属**（4k LC 写 dir=1、~7-12k IOPS，与正式 campaign 任一 rep5 iox run 均不符；系 campaign 结束后、stray 打包前的后续 run（标定/诊断/伴随采样期）覆盖）→ 未用于任何图。
3. **'per-IO' 日志实为 1s 窗均值（重要口径）**：job 模板 global 段带 `log_avg_msec=1000`，fio 对 `write_lat_log` 输出同样按窗平均 → 幸存的 `*_lat.1.log` 每行是该 1s 窗口的均值延迟（非逐 IO 样本）。因此**现有日志无法计算 1s 分桶 p99**（fig.kyber / fig.io_latency 上面板的 per-IO p99 时序按原计划不可恢复），各图已如实标注。
4. **LC 读侧洞读效应（如实说明）**：每 run 开始 `reset_data_files` truncate+fallocate（全洞）；kyber/linnos 的 LC 为 4k **randread**，实测 ~85 万 IOPS、mean clat ~8 µs（linnos rep5 iox p4 LC）——洞读几乎不产生真实设备 IO（与哨兵 before 同机制）。kyber/linnos 实验的 LC 读延迟与深度反应发生在近零真实读流量之上，解读需带此背景。
5. **native 内部状态伴随补采已送达（EXPERIMENT_MATRIX 附录 D2）**：`results/kyber_depth_native/rep{1..5}/native/`（python 采样器 1s，debugfs `sched/{read,write}_tokens` 的 depth，~155 行/run，尾部 READ_ERR 行已过滤）与 `results/io_cost_vrate_native/rep{1..5}/native/`（io.stat cost.vrate + usage，1s）。两目录独立于正式 campaign 数据（不覆盖）。例外：**io_cost_vrate_native rep5 不完整**（无 RESULT.json、p3 fio JSON 为空、轨迹止于 t≈72s）→ 已排除，native vrate 用 rep1–4。
6. **日志是否 gzip**：runner 自身的 `runner*.log/state_snapshot.log` 已 gzip，伴随轨迹为 .gz；分析代码（`common.py:read_text_maybe_gz`）对 gzip 在内存解包读取，未落盘解压。
7. **时间对齐口径**：fio JSON 的 `timestamp` = **作业结束时刻**（已用 `sentinel_before_3 结束 ≈ p1 开始 = timestamp-runtime` 交叉验证）；故 p1 起点 = `ts(p1_lc) − runtime`，相位边界 t=30/60s 由此换算，误差 ≤1 s。loader 统计的 `t_ms` 是单调时钟（uptime ms），用 `state_snapshot` 的 `armed` 时刻（loader attach ≈ armed−1 s）映射到墙钟；伴随轨迹用 epoch_ts 直接减 p1 起点。经 io_cost 验证：IOX FSM 活动起点与 p1 起点差 ~0.5 s。整体对齐误差 **±2 s 内**。

## 1. mq_deadline

负载（**正式口径 = mq_deadline_v2**）：单 fio `randrw 4k QD32` 60 s，**rwmixread=70**（计划冻结的 70/30 混合比）。三臂：none（调度器 none）、native mq-deadline、IOX q_mq_deadline。数据源 `results/mq_deadline_v2/rep{1..5}/{none,native,iox}/`（15 run 全 OK；15/15 run 的 read/write IOPS 比 2.330–2.338，70/30 已生效）。
- **U1 配置缺陷说明**：正式 campaign 内的原 mq_deadline 集合（`results/campaign/mq_deadline/`）job 行丢失 `rwmixread=70`，实际 50/50（read/write IOPS 比 ≈1.0）——为配置缺陷（U1，见矩阵审查采纳记录）产生的作废集合，**已由 v2 取代**；v1 原始数据保留在原处、不再用于图与统计。
| arm | rep | read p99 µs | write p99 µs | read IOPS | write IOPS | IOPS r/w |
|---|---|---|---|---|---|---|
| none | 1 | 782.3 | 905.2 | 117363 | 50346 | 2.331 |
| none | 2 | 749.6 | 839.7 | 119868 | 51335 | 2.335 |
| none | 3 | 741.4 | 782.3 | 120761 | 51819 | 2.330 |
| none | 4 | 1105.9 | 4292.6 | 81677 | 34969 | 2.336 |
| none | 5 | 1089.5 | 5210.1 | 73303 | 31429 | 2.332 |
| native | 1 | 675.8 | 766.0 | 113615 | 48604 | 2.338 |
| native | 2 | 733.2 | 806.9 | 113428 | 48616 | 2.333 |
| native | 3 | 815.1 | 4079.6 | 90134 | 38631 | 2.333 |
| native | 4 | 929.8 | 11468.8 | 62078 | 26638 | 2.330 |
| native | 5 | 888.8 | 7241.7 | 67399 | 28886 | 2.333 |
| iox | 1 | 634.9 | 643.1 | 102474 | 43881 | 2.335 |
| iox | 2 | 659.5 | 643.1 | 102926 | 44092 | 2.334 |
| iox | 3 | 766.0 | 13959.2 | 60353 | 25896 | 2.331 |
| iox | 4 | 757.8 | 4292.6 | 72556 | 31104 | 2.333 |
| iox | 5 | 831.5 | 14876.7 | 51713 | 22151 | 2.335 |

- **none**：read p99 median 782 µs，write p99 median 905 µs
- **native**：read p99 median 815 µs，write p99 median 4080 µs
- **iox**：read p99 median 758 µs，write p99 median 4293 µs

**图 fig.mq_deadline**：Read/Write P99 两组分组柱（各臂 median）+ 每 rep 黑色散点，对数纵轴，数据 = v2。系列含义：柱=5 rep 中位数，点=单 rep。
- v2 vs v1 一句话：70/30 让整体读延迟从 v1 的 ~990–1420 µs 降到 ~640–1110 µs（读占比高、4k 读更便宜），write p99 的臂间 median 差异被 rep 间大离散（同臂 643 µs–14.9 ms）主导，v1 中 IOX 读侧最低的排序在 v2 仍保持（iox 758 < none 782 < native 815 µs，差距收窄）。
- 哨兵：本实验 after/before 比 ~0.04（详见第 9 节哨兵机制说明），属文件状态效应，非设备漂移；三臂一致可比。

## 2. kyber

负载：3 相位 × 30 s；LC = 4k randread QD 32→64→32，BE = 128k seqwrite QD16 同步。native 臂 = kyber 调度器（sysfs read_lat_nsec=80µs/write_lat_nsec=1ms，E4 口径）；IOX 臂 = scx_io + q_kyber 同目标。
| arm | rep | LC read p99 p1 µs | p2 µs | p3 µs | BE write p99 p1 µs | p2 µs | p3 µs |
|---|---|---|---|---|---|---|---|
| none | 1 | 53.0 | 95.7 | 52.5 | 99090.4 | 102236.2 | 79167.5 |
| none | 2 | 52.0 | 103.9 | 57.1 | 20578.3 | 20578.3 | 20578.3 |
| none | 3 | 52.0 | 101.9 | 52.0 | 20578.3 | 20578.3 | 20578.3 |
| none | 4 | 57.1 | 99.8 | 53.0 | 20578.3 | 20578.3 | 20578.3 |
| none | 5 | 57.1 | 118.3 | 58.6 | 20578.3 | 20578.3 | 20578.3 |
| native | 1 | 58.6 | 97.8 | 56.1 | 48496.6 | 38535.2 | 20578.3 |
| native | 2 | 52.5 | 107.0 | 51.5 | 20578.3 | 20578.3 | 20578.3 |
| native | 3 | 59.6 | 123.4 | 60.7 | 20578.3 | 20578.3 | 20578.3 |
| native | 4 | 54.0 | 97.8 | 61.7 | 20578.3 | 20578.3 | 20578.3 |
| native | 5 | 57.1 | 117.2 | 57.1 | 20578.3 | 20578.3 | 20578.3 |
| iox | 1 | 50.4 | 103.9 | 60.2 | 26345.5 | 20578.3 | 20578.3 |
| iox | 2 | 51.5 | 121.3 | 57.1 | 20578.3 | 20578.3 | 20578.3 |
| iox | 3 | 59.6 | 112.1 | 60.7 | 20578.3 | 20578.3 | 20578.3 |
| iox | 4 | 52.0 | 119.3 | 60.7 | 20578.3 | 20578.3 | 20578.3 |
| iox | 5 | 57.6 | 119.3 | 50.9 | 20578.3 | 20578.3 | 20578.3 |

**图 fig.kyber**：上面板 = LC 读 p99 的 1 s 分桶时序——**不可恢复**（幸存 fio lat 日志是 1s 窗均值且无一可归属到 kyber，见第 0 节第 2/3 条），面板留空注明；下面板 = 读域深度：IOX = loader_q_kyber 统计 `hctx[*].dl[0]`（6 个 hctx 的 median 线 + min-max 带）；**native = kyber_depth_native 补采 depth_trace.log.gz 的 read 列**（debugfs read_tokens depth，5 rep 中位线，epoch_ts 对齐各 rep p1 起点）。
- **native 读域深度全程 256 不收缩**（5 rep×~154 行逐行核实 min=max=256）——与 IOX dl[0] 256↔64 动态收缩形成对照；native 采样中唯一变动在**写域**（write_tokens 大多 128，rep1/4/5 在相位边界附近短暂落到 1–8；图未画出、如实记录）。
- IOX dl[0] 实测范围 **64↔256**（5 rep 合计），256→64 的塌落与 LC 流量启动同步。
- 口径注：BE write p99 多行出现同一常数 20578.3 µs，是 fio clat 百分位桶的桶边界量化（多个 rep 的 p99 落在同一延迟桶），属原始数据形态而非数据缺失。
- kyber iox 5 rep loader 统计齐全（每 rep 153 行，1 s 粒度）。

## 3. io_max

负载：BE cgroup 单 fio `4k randwrite QD32` 60 s。native 臂 io.max `wbps=33554432`（32 MiB/s）；IOX q_iomax 同 cap；none 不受限参照。
| arm | rep | mean bw MiB/s | write p99 µs |
|---|---|---|---|
| none | 1 | 149.1 | 8716.3 |
| none | 2 | 174.1 | 4554.8 |
| none | 3 | 130.3 | 13303.8 |
| none | 4 | 147.3 | 12386.3 |
| none | 5 | 112.7 | 12779.5 |
| native | 1 | 32.0 | 94896.1 |
| native | 2 | 32.0 | 95944.7 |
| native | 3 | 32.0 | 94896.1 |
| native | 4 | 32.0 | 94896.1 |
| native | 5 | 32.0 | 95944.7 |
| iox | 1 | 32.0 | 4554.8 |
| iox | 2 | 32.0 | 4620.3 |
| iox | 3 | 32.0 | 4489.2 |
| iox | 4 | 32.0 | 4423.7 |
| iox | 5 | 32.0 | 4554.8 |

- **none**：mean bw median 147.30 MiB/s（5 rep）
- **native**：mean bw median 32.00 MiB/s（5 rep）
- **iox**：mean bw median 32.00 MiB/s（5 rep）
- whole-run 口径：native 与 IOX 的 mean bw 均精确压在 **32 MiB/s**（两个机制的限速精度一致）；none ≈ 150 MiB/s。
**图 fig.io_max**：上面板 = BE 写带宽 1 s 时序——**示例序列**（幸存 stray_logs/lat_iomax_bw.1.log = rep5 iox 单 run，虚线红；60 行逐秒贴 32 MiB/s ±0.1）+ 32 MiB/s 参考线；下面板 = 每 rep 的 60 s 平均带宽（fio JSON，柱=median、点=rep）。完整 3臂×5rep 1s 序列缺失（D1）。

## 4. io_weight

负载：两个 cgroup 各一条 128k seqwrite QD16 饱和写（90 s）。权重 LC:BE = 100:400 （IOX arb_wdrr；native = BFQ io.bfq.weight；none 无权重）。BE 目标份额 = 0.8。
| arm | rep | lc bw MiB/s | be bw MiB/s | BE whole-run share |
|---|---|---|---|---|
| none | 1 | 218.8 | 193.0 | 0.469 |
| none | 2 | 274.0 | 89.9 | 0.247 |
| none | 3 | 310.8 | 81.2 | 0.207 |
| none | 4 | 412.4 | 151.3 | 0.268 |
| none | 5 | 272.8 | 120.6 | 0.307 |
| native | 1 | 222.8 | 157.9 | 0.415 |
| native | 2 | 124.1 | 384.7 | 0.756 |
| native | 3 | 222.6 | 189.5 | 0.460 |
| native | 4 | 213.0 | 227.0 | 0.516 |
| native | 5 | 326.0 | 373.3 | 0.534 |
| iox | 1 | 72.1 | 229.7 | 0.761 |
| iox | 2 | 88.7 | 277.7 | 0.758 |
| iox | 3 | 159.1 | 621.2 | 0.796 |
| iox | 4 | 99.4 | 342.6 | 0.775 |
| iox | 5 | 137.3 | 523.1 | 0.792 |

- **none**：BE 份额 median 0.268（min 0.207 / max 0.469）
- **native**：BE 份额 median 0.516（min 0.415 / max 0.756）
- **iox**：BE 份额 median 0.775（min 0.758 / max 0.796）
**图 fig.io_weight**：BE 字节份额 1 s 时序——**示例序列**（幸存 stray_logs/lat_w_{lc,be}_bw.1.log = rep5 iox 单 run，虚线红；份额全程贴 0.8，t≈37-40s 与 74-86s 短暂跌至 0.6-0.75）+ 0.8 目标线；右下角文本框 = 各臂 whole-run 份额 median（fio JSON 字节口径，非第三指标）。完整 3臂×5rep 1s 序列缺失（D1）。

## 5. io_cost

负载：3 相位 × 30 s；LC = **4k randwrite** QD 32→64→32（E2 口径），BE = 128k seqwrite QD16。native 臂 = io.cost qos（root，12ms 目标等效 rpct 配置）；IOX = arb_iocost（vrate FSM）。
| arm | rep | LC write p99 p1 ms | p2 ms | p3 ms |
|---|---|---|---|---|
| none | 1 | 11075.6 | 20316.2 | 11337.7 |
| none | 2 | 60031.0 | 67633.2 | 70778.9 |
| none | 3 | 64749.6 | 65273.9 | 61603.8 |
| none | 4 | 84410.4 | 89653.2 | 92799.0 |
| none | 5 | 56361.0 | 28704.8 | 65798.1 |
| native | 1 | 57409.5 | 43253.8 | 60031.0 |
| native | 2 | 51118.1 | 59506.7 | 67633.2 |
| native | 3 | 85458.9 | 58982.4 | 89653.2 |
| native | 4 | 81264.6 | 73924.6 | 88604.7 |
| native | 5 | 56361.0 | 74973.2 | 80216.1 |
| iox | 1 | 4948.0 | 25821.2 | 63701.0 |
| iox | 2 | 6520.8 | 17432.6 | 9764.9 |
| iox | 3 | 6258.7 | 77070.3 | 17170.4 |
| iox | 4 | 4948.0 | 17694.7 | 11993.1 |
| iox | 5 | 4358.1 | 11468.8 | 13434.9 |

IOX vrate FSM 状态（5 rep）：
- rep1: 活动样本 121 行，vrate_fp 范围 64–1024（0.25×–4.00×），down/up 步进 155/200
- rep2: 活动样本 121 行，vrate_fp 范围 64–923（0.25×–3.61×），down/up 步进 153/217
- rep3: 活动样本 121 行，vrate_fp 范围 64–342（0.25×–1.34×），down/up 步进 235/114
- rep4: 活动样本 122 行，vrate_fp 范围 64–1024（0.25×–4.00×），down/up 步进 112/274
- rep5: 活动样本 121 行，vrate_fp 范围 64–1024（0.25×–4.00×），down/up 步进 106/267

**图 fig.io_cost**：唯一指标 vrate 时序。IOX = loader 统计 `vrate = vrate_fp/256`（乘子，0.25–4.0）；**native = results/io_cost_vrate_native/rep{1..4}/native/vrate.log.gz**（io.stat cost.vrate 绝对速率；rep5 补采 run 不完整已排除，见第 0 节第 5 条）。两者各自归一化到首个非 NA 值后同坐标绘制（细线=单 rep，粗线=rep 间 median；NA 段断线——本次 4 rep 无 NA）；**注意两臂 vrate 语义不同**，只比较响应方向与 t=30/60s 相位阶跃。
- **native vrate 全程恒定 150.00**（rep1–4 各 ~155 行、0 个 NA、唯一值 150.00；usage 持续增长说明 io.stat cost 记账活着）——LC QD 阶跃期间 native blk-iocost 未动 vrate（IOX 同期 0.25×–4.0× 全程摆动）。
- iox 5 rep loader 统计齐全（每 rep ~153 行）；FSM 活动起点与 p1 起点一致（±0.5 s）。

## 6. io_latency（最高优先级）

负载：3 相位 × 30 s；LC = 4k randwrite QD 32→64→32，BE = 128k seqwrite QD16。目标 12 ms（E3 口径）：native io.latency（LC cgroup target=12000），IOX arb_iolatency （--lat-target-ns 12000000）；none 无控制。
| arm | rep | LC p99 p1 ms | p2 ms | p3 ms | BE bw p1 MiB/s | p2 | p3 | boost max | hold 末端累计 | 样本数 |
|---|---|---|---|---|---|---|---|---|---|---|
| none | 1 | 90701.8 | 101187.6 | 89653.2 | 184.0 | 167.6 | 308.5 |  |  |  |
| none | 2 | 86507.5 | 90701.8 | 57409.5 | 149.0 | 190.9 | 379.6 |  |  |  |
| none | 3 | 94896.1 | 87556.1 | 105381.9 | 180.3 | 244.6 | 196.4 |  |  |  |
| none | 4 | 45350.9 | 160432.1 | 166723.6 | 279.0 | 157.3 | 36.6 |  |  |  |
| none | 5 | 162529.3 | 158335.0 | 111673.3 | 131.1 | 63.4 | 127.5 |  |  |  |
| native | 1 | 27131.9 | 83361.8 | 98041.9 | 423.4 | 312.0 | 224.9 |  |  |  |
| native | 2 | 95944.7 | 83361.8 | 93847.6 | 194.9 | 208.1 | 272.2 |  |  |  |
| native | 3 | 131596.3 | 121110.5 | 156237.8 | 138.3 | 210.4 | 140.3 |  |  |  |
| native | 4 | 96993.3 | 29753.3 | 94896.1 | 265.7 | 358.1 | 314.1 |  |  |  |
| native | 5 | 102236.2 | 91750.4 | 81264.6 | 195.8 | 227.3 | 324.9 |  |  |  |
| iox | 1 | 7503.9 | 26345.5 | 36438.0 | 187.8 | 125.9 | 68.9 | 4 | 119628 | 153 |
| iox | 2 | 23986.2 | 81264.6 | 74973.2 | 117.1 | 87.3 | 54.8 | 4 | 131556 | 152 |
| iox | 3 | 21889.0 | 16318.5 | 15663.1 | 115.0 | 167.6 | 130.2 | 4 | 72186 | 152 |
| iox | 4 | 4423.7 | 7241.7 | 4423.7 | 219.5 | 192.2 | 168.6 | 4 | 55602 | 152 |
| iox | 5 | 4816.9 | 56885.2 | 27394.0 | 224.4 | 89.9 | 94.5 | 4 | 84160 | 152 |

**图 fig.io_latency**：上面板 LC 写 p99（1 s 分桶，per-IO 日志口径；当前快照缺日志，fallback 为每 rep 每相位 p99 散点，fio JSON）+ 12 ms 目标线；下面板 BE 写带宽 （fallback 为每 rep 每相位平均带宽散点）+ **IOX 仲裁账本 1 s 带宽**（arb_iolatency 统计的 tenants[].bytes 为累计 completed_bytes，按 cgroup 求和后差分 → 1 s 系列；cg↔LC/BE 映射由账本终值与 fio io_bytes 精确匹配确定，误差 <5%，仅 IOX 臂有账本）。竖线 = t=30/60s 相位边界。
- IOX 臂 arb_iolatency 统计齐全：boost 到满档 4、hold 门在 5 rep 内累计触发 5.6万–13.2万 次、slo 状态含 0/1/2（含 PRESSURE）——反馈链路在 LC=write 下活跌（与冒烟 F5 一致）。
- LC write p99 离散度大（几 ms 到 160 ms，跨 rep/相位），读图时以 rep 散点为准。

## 7. d2fq

负载：两 tenant（t100 权重 100 / t400 权重 400）各一条 128k seqwrite QD16 饱和写 90 s；IOX = arb_d2fq；**native 臂 VM 内 N/A**（virtio-blk 无 NVMe WRRU，E1 口径）。
| arm | rep | t100 bw MiB/s | t400 bw MiB/s | t400 whole-run share |
|---|---|---|---|---|
| none | 1 | 192.2 | 192.3 | 0.500 |
| none | 2 | 215.2 | 215.1 | 0.500 |
| none | 3 | 337.1 | 337.1 | 0.500 |
| none | 4 | 180.2 | 180.2 | 0.500 |
| none | 5 | 159.2 | 159.0 | 0.500 |
| iox | 1 | 188.3 | 188.2 | 0.500 |
| iox | 2 | 203.2 | 203.3 | 0.500 |
| iox | 3 | 184.8 | 184.8 | 0.500 |
| iox | 4 | 195.6 | 196.0 | 0.500 |
| iox | 5 | 204.9 | 204.9 | 0.500 |

- **none**：t400 份额 median 0.500（min 0.500 / max 0.500）
- **iox**：t400 份额 median 0.500（min 0.500 / max 0.500）
- **如实记录**：两臂 whole-run 份额均为 **50/50**（arb_d2fq 权重不分化，与冒烟发现 F2 一致）；none 臂 50/50 属预期（等需求流无权重机制）。
- 任务预期 'iox 4 rep'：实际数据 5 rep 全部 OK，故图中画 5 条。
**图 fig.d2fq**：t400 累计字节份额曲线——**示例序列**（幸存 stray_logs/lat_t100/t400_bw.1.log = rep5 iox 单 run，虚线红；1s bw 累计积分，全程贴 0.50）+ 端点散点（各 rep whole-run 份额，全部 0.500）+ 0.2/0.8 参考线；Native N/A 以图注说明。完整 per-rep 1s 序列缺失（D1）。

## 8. linnos

负载：4 相位 × 30 s；LC = 4k randread QD8 恒定，BE = 4k randwrite QD 8/16/32/64。IOX = q_linnos（BE 侧分带拦截目标 band0/1/2 = 0/40/60%）；none 无拦截机制；native N/A。
数据 = `loader_q_be.stdout`（pol=q_linnos）末行累计计数：`band[i]`=各带累计插入数，`ir{i}[1]`=各带 BE 被拦截数（intercepted[band][class=BE]）。
| rep | ins total | band0 ins | band1 ins | band2 ins | 实际率 b0 % | b1 % | b2 % | 目标 0/40/60 % |
|---|---|---|---|---|---|---|---|---|
| 1 | 4380783 | 12 | 8 | 4380763 | 0.00 | 100.00 | 60.000 | 0/40/60 |
| 2 | 3743110 | 12 | 8 | 3743090 | 0.00 | 100.00 | 60.000 | 0/40/60 |
| 3 | 4769610 | 12 | 8 | 4769590 | 0.00 | 100.00 | 60.000 | 0/40/60 |
| 4 | 3605997 | 12 | 8 | 3605977 | 0.00 | 100.00 | 60.000 | 0/40/60 |
| 5 | 4126362 | 12 | 8 | 4126342 | 0.00 | 100.00 | 60.000 | 0/40/60 |

- **band 覆盖退化（如实呈现）**：5 rep 中 band2 占插入数 **>99.999%**（band0≈12、band1≈8 次），与冒烟发现 F4 一致：q_linnos inflight 计数泄漏（completions=0，policy 级完成钩子不触发）→ busy 代理运行中单调增长 → 插入几乎全部落 band2。因此 band0/band1 的实际率仅有极小样本意义（band1 的 100% 来自确定性 1/100 序号抽签在最初 8 次插入上的体现），band2 实际率 **≈60.00%** 与目标 60% 精确吻合（1/100 确定性抽签）。
- none 臂：无拦截机制（图中文字标注）；linnos diag/rep0 两个诊断 run 未入正式统计。
**图 fig.linnos**：三带分组柱（目标 vs 实际 median）+ 每 rep 散点；柱下标注各带插入量。

## 9. 哨兵（3×10 s QD1 randread）漂移统计

逐 run 数据见 `RESULTS/sentinel_drift.csv`。**机制性说明（重要）**：哨兵读 `/data/lc.bin`；每 run 开头 `reset_data_files` 将文件 truncate+fallocate（全洞），**before 哨兵的读大多命中文件洞、几乎不产生真实设备 IO**（QD1 ~93 万 IOPS）；若本 run 工作负载写过 lc.bin，**after 哨兵读的是真实数据块**（QD1 ~3–9 万 IOPS）。因此：
- LC/工作负载写过 lc.bin 的实验（mq_deadline、io_weight、io_cost、io_latency、d2fq）：after/before 比 0.01–0.15，全部 >20% 阈值——**属文件状态效应而非设备漂移**；跨臂之间（同实验内同文件状态）仍然可比。
- 未写过 lc.bin 的实验（kyber、io_max、linnos）：ratio median = 0.996–1.000，范围 0.9769–1.0125，**无 >20% 漂移**，可作为设备稳定性的直接证据（上界 1.0125 = linnos rep2 iox；U7 更正：此前误写为 1.011）。
- 按约定（[MSC]）漂移 >20% 记录不淘汰；125 run（含 mq_deadline_v2 15 run）中被标记 85 个，全部属于上述机制性类别。

## 10. 结果文件索引

| 文件 | 内容 |
|---|---|
| RESULTS/mq_deadline/fig.mq_deadline.{pdf,png} + data.mq_deadline.csv | F1 图与点数据 |
| RESULTS/kyber/fig.kyber.{pdf,png} + data.kyber.csv | F2 图与点数据（含 native 深度线；p99 面板按 D1 不可恢复） |
| RESULTS/io_max/fig.io_max.{pdf,png} + data.io_max.csv | F3 图与点数据（含 rep5 iox 示例 1s 序列） |
| RESULTS/io_weight/fig.io_weight.{pdf,png} + data.io_weight.csv | F4 图与点数据（含 rep5 iox 示例份额序列） |
| RESULTS/io_cost/fig.io_cost.{pdf,png} + data.io_cost.csv | F5 图与点数据（IOX vrate + native vrate rep1–4） |
| RESULTS/io_latency/fig.io_latency.{pdf,png} + data.io_latency.csv | F6 图与点数据（fallback + IOX 账本 1s bw） |
| RESULTS/d2fq/fig.d2fq.{pdf,png} + data.d2fq.csv | F7 图与点数据（含 rep5 iox 示例累计份额序列） |
| RESULTS/linnos/fig.linnos.{pdf,png} + data.linnos.csv | F8 图与点数据 |
| RESULTS/sentinel_drift.csv | 全部 110 run 哨兵逐次 IOPS 与比率 |
| RESULTS/scripts/*.py | 分析脚本（只读 campaign/，只写 RESULTS/） |
