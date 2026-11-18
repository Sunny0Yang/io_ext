#!/usr/bin/env python3
# generate_summary.py — build RESULTS/DATA_SUMMARY.md (Chinese) from the data.
import csv
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (ALGOS, ARMS_ALL, CAMPAIGN, REPS, RESULTS,
                    fio_bw_kib, fio_bytes, fio_iops, fio_p99_ns, load_json,
                    load_jsonl, run_dir, sentinel_iops)

OUT = os.path.join(RESULTS, "DATA_SUMMARY.md")

sent = {}
for r in csv.DictReader(open(os.path.join(RESULTS, "sentinel_drift.csv"))):
    sent[(r["algo"], int(r["rep"]), r["arm"])] = r


def fmt(v, nd=1):
    return "" if v is None else ("%.*f" % (nd, v))


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def j99(path, direction):
    return fio_p99_ns(load_json(path), direction)


def phase_rows(algo, metric_fn, arms, extra=None):
    rows = []
    for arm in arms:
        for rep in REPS:
            run = run_dir(algo, rep, arm)
            r = [arm, rep]
            r += metric_fn(run)
            if extra:
                r += extra(run)
            rows.append(r)
    return rows


def kyber_metrics(run):
    out = []
    for ph in ("p1", "p2", "p3"):
        v = j99(os.path.join(run, "fio_%s_lc.json" % ph), "read")
        out.append(fmt(v / 1000.0) if v else "")
    for ph in ("p1", "p2", "p3"):
        v = j99(os.path.join(run, "fio_%s_be.json" % ph), "write")
        out.append(fmt(v / 1000.0) if v else "")
    return out


def iocost_metrics(run):
    out = []
    for ph in ("p1", "p2", "p3"):
        v = j99(os.path.join(run, "fio_%s_lc.json" % ph), "write")
        out.append(fmt(v / 1000.0) if v else "")
    return out


def iolat_metrics(run):
    out = []
    for ph in ("p1", "p2", "p3"):
        v = j99(os.path.join(run, "fio_%s_lc.json" % ph), "write")
        out.append(fmt(v / 1000.0) if v else "")
    for ph in ("p1", "p2", "p3"):
        jb = load_json(os.path.join(run, "fio_%s_be.json" % ph))
        bw = fio_bw_kib(jb, "write")
        out.append(fmt(bw / 1024.0) if bw else "")
    return out


def iolat_extra(run):
    recs = load_jsonl(os.path.join(run, "loader_arb_iolatency.stdout")) or []
    ts = [r for r in recs if r.get("pol") == "arb_iolatency" and "tenants" in r]
    if not ts:
        return ["", "", ""]
    boosts = [t["boost"] for r in ts for t in r["tenants"]]
    hold = max(sum(r.get("hold", []) or []) for r in ts)
    return [str(max(boosts)), str(hold), str(len(ts))]


def iomax_metrics(run):
    j = load_json(os.path.join(run, "fio_iomax.json"))
    bw = fio_bw_kib(j, "write")
    p99 = j99(os.path.join(run, "fio_iomax.json"), "write")
    return [fmt(bw / 1024.0) if bw else "", fmt(p99 / 1000.0) if p99 else ""]


def ioweight_metrics(run):
    lc = fio_bytes(load_json(os.path.join(run, "fio_w_lc.json")), "write")
    be = fio_bytes(load_json(os.path.join(run, "fio_w_be.json")), "write")
    if lc and be:
        lcb = fio_bw_kib(load_json(os.path.join(run, "fio_w_lc.json")), "write")
        beb = fio_bw_kib(load_json(os.path.join(run, "fio_w_be.json")), "write")
        return [fmt(lcb / 1024.0), fmt(beb / 1024.0), fmt(be / (lc + be), 3)]
    return ["", "", ""]


def d2fq_metrics(run):
    lc = fio_bytes(load_json(os.path.join(run, "fio_d2fq_lc.json")), "write")
    be = fio_bytes(load_json(os.path.join(run, "fio_d2fq_be.json")), "write")
    if lc and be:
        lcb = fio_bw_kib(load_json(os.path.join(run, "fio_d2fq_lc.json")), "write")
        beb = fio_bw_kib(load_json(os.path.join(run, "fio_d2fq_be.json")), "write")
        return [fmt(lcb / 1024.0), fmt(beb / 1024.0), fmt(be / (lc + be), 3)]
    return ["", "", ""]


def mq_metrics(run):
    j = load_json(os.path.join(run, "fio_mq.json"))
    r99 = fio_p99_ns(j, "read")
    w99 = fio_p99_ns(j, "write")
    ri = fio_iops(j, "read")
    wi = fio_iops(j, "write")
    return [fmt(r99 / 1000.0), fmt(w99 / 1000.0), fmt(ri, 0), fmt(wi, 0)]


def linnos_row(run):
    recs = load_jsonl(os.path.join(run, "loader_q_be.stdout")) or []
    q = [r for r in recs if r.get("pol") == "q_linnos"]
    if not q:
        return None
    last = q[-1]
    ins = last.get("ins", 0)
    band = last.get("band", [0, 0, 0])
    irbe = [last.get("ir0", [0, 0])[1], last.get("ir1", [0, 0])[1],
            last.get("ir2", [0, 0])[1]]
    return ins, band, irbe


L = []
A = L.append

A("# io_ext 复现 campaign 正式数据摘要（DATA_SUMMARY）")
A("")
A("- 数据源：`results/campaign/`（campaign id `repro_20261113_183153`，guest `ioext-repro-vm`，"
  "kernel 7.3.1-ioext3，fio-3.28，blkdev vdb）。")
A("- 覆盖 8 实验 × 5 rep × 2-3 臂，共 **110 个正式 run + mq_deadline_v2 重跑 15 run（70/30 口径），"
  "`RESULT.json` 全部 OK**；"
  "linnos 另有 `diag/rep0` 两个诊断 run，**不计入正式统计**（任务约定）。")
A("- 本文由 `RESULTS/scripts/generate_summary.py` 生成；各图底层点数据在 "
  "`RESULTS/<algo>/data.<algo>.csv`，哨兵逐 run 数据在 `RESULTS/sentinel_drift.csv`。")
A("")
A("## 0. 数据完整性与已知缺口（先读）")
A("")
A("1. **fio 逐窗/per-IO 日志未入 run 目录（EXPERIMENT_MATRIX 附录 D1，最终表述）**："
  "`write_bw_log/write_lat_log` 落在 fio 进程 CWD（campaign 启动目录），同名逐 run 覆盖 → "
  "每 run 的时序日志不可恢复；每实验**最后一轮（rep5 iox）**的日志幸存（37 个文件，已拉回 "
  "`results/stray_logs/`）。缺陷根因已定位（runner gzip 只覆盖 RUN_DIR；fio 日志路径与 CWD 绑定），"
  "后续 campaign 修复（fio 工作目录设为 run 目录）。影响与处置："
  "①时序面板改用 arbiter/loader 1s 统计账本（5 个 instrumented loader 的 stdout 在 run 目录内）"
  "与 fio JSON 分相（3×30s）百分位；②幸存单 run 序列作**示例序列**画入 fig.io_max / fig.io_weight / "
  "fig.d2fq（图上标注 example series, rep5 iox）；③各图完整 3臂×5rep 1s 序列仍缺失（图内红字注明）。")
A("2. **幸存日志归属方法**：37 个文件的 mtime 全部相同（打包时刻，guest 侧 cp 丢失原 mtime），"
  "**mtime 归属不可用**；改用**内容签名归属**——行数（60/90/30 行 ↔ run 时长）、方向列（dir=0/1）、"
  "逐窗均值与对应 rep5 iox fio JSON 全 run 均值精确对照：`lat_iomax_*`（32.01 vs 32.00 MiB/s）、"
  "`lat_w_*`（份额均值 0.774 vs 字比 0.792，一致）、`lat_t100/t400_*`（205.1 vs 204.9 MiB/s）、"
  "`mq_*`（130.6 vs 130.6 MiB/s）、`lat_p4_*`（仅 linnos 有 p4 相位；3300.5 vs 3309.8 MiB/s）"
  "→ 归属成立。`lat_p1/p2/p3_{lc,be}_*` **不可归属**（4k LC 写 dir=1、~7-12k IOPS，与正式 campaign "
  "任一 rep5 iox run 均不符；系 campaign 结束后、stray 打包前的后续 run（标定/诊断/伴随采样期）覆盖）"
  "→ 未用于任何图。")
A("3. **'per-IO' 日志实为 1s 窗均值（重要口径）**：job 模板 global 段带 `log_avg_msec=1000`，"
  "fio 对 `write_lat_log` 输出同样按窗平均 → 幸存的 `*_lat.1.log` 每行是该 1s 窗口的均值延迟"
  "（非逐 IO 样本）。因此**现有日志无法计算 1s 分桶 p99**（fig.kyber / fig.io_latency 上面板的 "
  "per-IO p99 时序按原计划不可恢复），各图已如实标注。")
A("4. **LC 读侧洞读效应（如实说明）**：每 run 开始 `reset_data_files` truncate+fallocate（全洞）；"
  "kyber/linnos 的 LC 为 4k **randread**，实测 ~85 万 IOPS、mean clat ~8 µs（linnos rep5 iox p4 LC）"
  "——洞读几乎不产生真实设备 IO（与哨兵 before 同机制）。kyber/linnos 实验的 LC 读延迟与深度反应"
  "发生在近零真实读流量之上，解读需带此背景。")
A("5. **native 内部状态伴随补采已送达（EXPERIMENT_MATRIX 附录 D2）**："
  "`results/kyber_depth_native/rep{1..5}/native/`（python 采样器 1s，debugfs "
  "`sched/{read,write}_tokens` 的 depth，~155 行/run，尾部 READ_ERR 行已过滤）与 "
  "`results/io_cost_vrate_native/rep{1..5}/native/`（io.stat cost.vrate + usage，1s）。"
  "两目录独立于正式 campaign 数据（不覆盖）。例外：**io_cost_vrate_native rep5 不完整**"
  "（无 RESULT.json、p3 fio JSON 为空、轨迹止于 t≈72s）→ 已排除，native vrate 用 rep1–4。")
A("6. **日志是否 gzip**：runner 自身的 `runner*.log/state_snapshot.log` 已 gzip，伴随轨迹为 .gz；"
  "分析代码（`common.py:read_text_maybe_gz`）对 gzip 在内存解包读取，未落盘解压。")
A("7. **时间对齐口径**：fio JSON 的 `timestamp` = **作业结束时刻**（已用 "
  "`sentinel_before_3 结束 ≈ p1 开始 = timestamp-runtime` 交叉验证）；"
  "故 p1 起点 = `ts(p1_lc) − runtime`，相位边界 t=30/60s 由此换算，误差 ≤1 s。"
  "loader 统计的 `t_ms` 是单调时钟（uptime ms），用 `state_snapshot` 的 `armed` 时刻"
  "（loader attach ≈ armed−1 s）映射到墙钟；伴随轨迹用 epoch_ts 直接减 p1 起点。"
  "经 io_cost 验证：IOX FSM 活动起点与 p1 起点差 ~0.5 s。整体对齐误差 **±2 s 内**。")
A("")
A("## 1. mq_deadline")
A("")
A("负载（**正式口径 = mq_deadline_v2**）：单 fio `randrw 4k QD32` 60 s，**rwmixread=70**"
  "（计划冻结的 70/30 混合比）。三臂：none（调度器 none）、native mq-deadline、IOX q_mq_deadline。"
  "数据源 `results/mq_deadline_v2/rep{1..5}/{none,native,iox}/`（15 run 全 OK；15/15 run 的 "
  "read/write IOPS 比 2.330–2.338，70/30 已生效）。")
A("- **U1 配置缺陷说明**：正式 campaign 内的原 mq_deadline 集合（`results/campaign/mq_deadline/`）"
  "job 行丢失 `rwmixread=70`，实际 50/50（read/write IOPS 比 ≈1.0）——为配置缺陷（U1，见矩阵审查"
  "采纳记录）产生的作废集合，**已由 v2 取代**；v1 原始数据保留在原处、不再用于图与统计。")
hdr = ["arm", "rep", "read p99 µs", "write p99 µs", "read IOPS", "write IOPS", "IOPS r/w"]
rows = []
meds = {a: ([], []) for a in ARMS_ALL}
MQ_V2 = os.path.join(os.path.dirname(CAMPAIGN), "mq_deadline_v2")
for arm in ARMS_ALL:
    for rep in REPS:
        run = os.path.join(MQ_V2, "rep%d" % rep, arm)
        j = load_json(os.path.join(run, "fio_mq.json"))
        r99, w99 = fio_p99_ns(j, "read"), fio_p99_ns(j, "write")
        ri, wi = fio_iops(j, "read"), fio_iops(j, "write")
        rows.append([arm, rep, fmt(r99 / 1000), fmt(w99 / 1000), fmt(ri, 0), fmt(wi, 0),
                     fmt(ri / wi, 3)])
        meds[arm][0].append(r99 / 1000)
        meds[arm][1].append(w99 / 1000)
A(md_table(hdr, rows))
A("")
for arm in ARMS_ALL:
    A("- **%s**：read p99 median %.0f µs，write p99 median %.0f µs" %
      (arm, float(np.median(meds[arm][0])), float(np.median(meds[arm][1]))))
A("")
A("**图 fig.mq_deadline**：Read/Write P99 两组分组柱（各臂 median）+ 每 rep 黑色散点，对数纵轴，"
  "数据 = v2。系列含义：柱=5 rep 中位数，点=单 rep。")
A("- v2 vs v1 一句话：70/30 让整体读延迟从 v1 的 ~990–1420 µs 降到 ~640–1110 µs（读占比高、"
  "4k 读更便宜），write p99 的臂间 median 差异被 rep 间大离散（同臂 643 µs–14.9 ms）主导，"
  "v1 中 IOX 读侧最低的排序在 v2 仍保持（iox 758 < none 782 < native 815 µs，差距收窄）。")
s = sent[("mq_deadline_v2", 1, "none")]
A("- 哨兵：本实验 after/before 比 ~0.04（详见第 9 节哨兵机制说明），属文件状态效应，"
  "非设备漂移；三臂一致可比。")
A("")
A("## 2. kyber")
A("")
A("负载：3 相位 × 30 s；LC = 4k randread QD 32→64→32，BE = 128k seqwrite QD16 同步。"
  "native 臂 = kyber 调度器（sysfs read_lat_nsec=80µs/write_lat_nsec=1ms，E4 口径）；"
  "IOX 臂 = scx_io + q_kyber 同目标。")
hdr = ["arm", "rep", "LC read p99 p1 µs", "p2 µs", "p3 µs",
       "BE write p99 p1 µs", "p2 µs", "p3 µs"]
A(md_table(hdr, phase_rows("kyber", kyber_metrics, ARMS_ALL)))
A("")
A("**图 fig.kyber**：上面板 = LC 读 p99 的 1 s 分桶时序——**不可恢复**（幸存 fio lat 日志是 1s 窗均值"
  "且无一可归属到 kyber，见第 0 节第 2/3 条），面板留空注明；下面板 = 读域深度："
  "IOX = loader_q_kyber 统计 `hctx[*].dl[0]`（6 个 hctx 的 median 线 + min-max 带）；"
  "**native = kyber_depth_native 补采 depth_trace.log.gz 的 read 列**（debugfs read_tokens depth，"
  "5 rep 中位线，epoch_ts 对齐各 rep p1 起点）。")
A("- **native 读域深度全程 256 不收缩**（5 rep×~154 行逐行核实 min=max=256）——与 IOX dl[0] "
  "256↔64 动态收缩形成对照；native 采样中唯一变动在**写域**（write_tokens 大多 128，rep1/4/5 在"
  "相位边界附近短暂落到 1–8；图未画出、如实记录）。")
A("- IOX dl[0] 实测范围 **64↔256**（5 rep 合计），256→64 的塌落与 LC 流量启动同步。")
A("- 口径注：BE write p99 多行出现同一常数 20578.3 µs，是 fio clat 百分位桶的桶边界量化"
  "（多个 rep 的 p99 落在同一延迟桶），属原始数据形态而非数据缺失。")
A("- kyber iox 5 rep loader 统计齐全（每 rep 153 行，1 s 粒度）。")
A("")
A("## 3. io_max")
A("")
A("负载：BE cgroup 单 fio `4k randwrite QD32` 60 s。native 臂 io.max `wbps=33554432`（32 MiB/s）；"
  "IOX q_iomax 同 cap；none 不受限参照。")
hdr = ["arm", "rep", "mean bw MiB/s", "write p99 µs"]
rows = []
meds = {a: [] for a in ARMS_ALL}
for arm in ARMS_ALL:
    for rep in REPS:
        run = run_dir("io_max", rep, arm)
        m = iomax_metrics(run)
        rows.append([arm, rep] + m)
        if m[0]:
            meds[arm].append(float(m[0]))
A(md_table(hdr, rows))
A("")
for arm in ARMS_ALL:
    if meds[arm]:
        A("- **%s**：mean bw median %.2f MiB/s（5 rep）" % (arm, float(np.median(meds[arm]))))
A("- whole-run 口径：native 与 IOX 的 mean bw 均精确压在 **32 MiB/s**（两个机制的限速精度一致）；"
  "none ≈ 150 MiB/s。")
A("**图 fig.io_max**：上面板 = BE 写带宽 1 s 时序——**示例序列**（幸存 stray_logs/lat_iomax_bw.1.log"
  " = rep5 iox 单 run，虚线红；60 行逐秒贴 32 MiB/s ±0.1）+ 32 MiB/s 参考线；"
  "下面板 = 每 rep 的 60 s 平均带宽（fio JSON，柱=median、点=rep）。完整 3臂×5rep 1s 序列缺失（D1）。")
A("")
A("## 4. io_weight")
A("")
A("负载：两个 cgroup 各一条 128k seqwrite QD16 饱和写（90 s）。权重 LC:BE = 100:400 "
  "（IOX arb_wdrr；native = BFQ io.bfq.weight；none 无权重）。BE 目标份额 = 0.8。")
hdr = ["arm", "rep", "lc bw MiB/s", "be bw MiB/s", "BE whole-run share"]
rows = []
shares = {a: [] for a in ARMS_ALL}
for arm in ARMS_ALL:
    for rep in REPS:
        run = run_dir("io_weight", rep, arm)
        m = ioweight_metrics(run)
        rows.append([arm, rep] + m)
        if m[2]:
            shares[arm].append(float(m[2]))
A(md_table(hdr, rows))
A("")
for arm in ARMS_ALL:
    if shares[arm]:
        A("- **%s**：BE 份额 median %.3f（min %.3f / max %.3f）" %
          (arm, float(np.median(shares[arm])), min(shares[arm]), max(shares[arm])))
A("**图 fig.io_weight**：BE 字节份额 1 s 时序——**示例序列**（幸存 stray_logs/lat_w_{lc,be}_bw.1.log"
  " = rep5 iox 单 run，虚线红；份额全程贴 0.8，t≈37-40s 与 74-86s 短暂跌至 0.6-0.75）+ 0.8 目标线；"
  "右下角文本框 = 各臂 whole-run 份额 median（fio JSON 字节口径，非第三指标）。"
  "完整 3臂×5rep 1s 序列缺失（D1）。")
A("")
A("## 5. io_cost")
A("")
A("负载：3 相位 × 30 s；LC = **4k randwrite** QD 32→64→32（E2 口径），BE = 128k seqwrite QD16。"
  "native 臂 = io.cost qos（root，12ms 目标等效 rpct 配置）；IOX = arb_iocost（vrate FSM）。")
hdr = ["arm", "rep", "LC write p99 p1 ms", "p2 ms", "p3 ms"]
A(md_table(hdr, phase_rows("io_cost", iocost_metrics, ARMS_ALL)))
A("")
A("IOX vrate FSM 状态（5 rep）：")
for rep in REPS:
    recs = load_jsonl(run_dir("io_cost", rep, "iox") + "/loader_arb_iocost.stdout") or []
    act = [r for r in recs if r.get("pol") == "arb_iocost" and (r.get("vrate_fp") or 0) > 0]
    if act:
        fps = [r["vrate_fp"] for r in act]
        A("- rep%d: 活动样本 %d 行，vrate_fp 范围 %d–%d（0.25×–%.2f×），down/up 步进 %d/%d" %
          (rep, len(act), min(fps), max(fps), max(fps) / 256.0,
           act[-1]["down_steps"], act[-1]["up_steps"]))
A("")
A("**图 fig.io_cost**：唯一指标 vrate 时序。IOX = loader 统计 `vrate = vrate_fp/256`（乘子，"
  "0.25–4.0）；**native = results/io_cost_vrate_native/rep{1..4}/native/vrate.log.gz**"
  "（io.stat cost.vrate 绝对速率；rep5 补采 run 不完整已排除，见第 0 节第 5 条）。"
  "两者各自归一化到首个非 NA 值后同坐标绘制（细线=单 rep，粗线=rep 间 median；NA 段断线——"
  "本次 4 rep 无 NA）；**注意两臂 vrate 语义不同**，只比较响应方向与 t=30/60s 相位阶跃。")
A("- **native vrate 全程恒定 150.00**（rep1–4 各 ~155 行、0 个 NA、唯一值 150.00；usage 持续增长"
  "说明 io.stat cost 记账活着）——LC QD 阶跃期间 native blk-iocost 未动 vrate（IOX 同期 0.25×–4.0× "
  "全程摆动）。")
A("- iox 5 rep loader 统计齐全（每 rep ~153 行）；FSM 活动起点与 p1 起点一致（±0.5 s）。")
A("")
A("## 6. io_latency（最高优先级）")
A("")
A("负载：3 相位 × 30 s；LC = 4k randwrite QD 32→64→32，BE = 128k seqwrite QD16。"
  "目标 12 ms（E3 口径）：native io.latency（LC cgroup target=12000），IOX arb_iolatency "
  "（--lat-target-ns 12000000）；none 无控制。")
hdr = ["arm", "rep", "LC p99 p1 ms", "p2 ms", "p3 ms",
       "BE bw p1 MiB/s", "p2", "p3", "boost max", "hold 末端累计", "样本数"]
rows = []
for arm in ARMS_ALL:
    for rep in REPS:
        run = run_dir("io_latency", rep, arm)
        m = iolat_metrics(run)
        ex = iolat_extra(run) if arm == "iox" else ["", "", ""]
        rows.append([arm, rep] + m + ex)
A(md_table(hdr, rows))
A("")
A("**图 fig.io_latency**：上面板 LC 写 p99（1 s 分桶，per-IO 日志口径；当前快照缺日志，"
  "fallback 为每 rep 每相位 p99 散点，fio JSON）+ 12 ms 目标线；下面板 BE 写带宽 "
  "（fallback 为每 rep 每相位平均带宽散点）+ **IOX 仲裁账本 1 s 带宽**（arb_iolatency 统计的 "
  "tenants[].bytes 为累计 completed_bytes，按 cgroup 求和后差分 → 1 s 系列；cg↔LC/BE 映射由"
  "账本终值与 fio io_bytes 精确匹配确定，误差 <5%，仅 IOX 臂有账本）。竖线 = t=30/60s 相位边界。")
A("- IOX 臂 arb_iolatency 统计齐全：boost 到满档 4、hold 门在 5 rep 内累计触发 "
  "5.6万–13.2万 次、slo 状态含 0/1/2（含 PRESSURE）——反馈链路在 LC=write 下活跌（与冒烟 F5 一致）。")
A("- LC write p99 离散度大（几 ms 到 160 ms，跨 rep/相位），读图时以 rep 散点为准。")
A("")
A("## 7. d2fq")
A("")
A("负载：两 tenant（t100 权重 100 / t400 权重 400）各一条 128k seqwrite QD16 饱和写 90 s；"
  "IOX = arb_d2fq；**native 臂 VM 内 N/A**（virtio-blk 无 NVMe WRRU，E1 口径）。")
hdr = ["arm", "rep", "t100 bw MiB/s", "t400 bw MiB/s", "t400 whole-run share"]
rows = []
shares = {a: [] for a in ("none", "iox")}
for arm in ("none", "iox"):
    for rep in REPS:
        run = run_dir("d2fq", rep, arm)
        m = d2fq_metrics(run)
        rows.append([arm, rep] + m)
        if m[2]:
            shares[arm].append(float(m[2]))
A(md_table(hdr, rows))
A("")
for arm in ("none", "iox"):
    A("- **%s**：t400 份额 median %.3f（min %.3f / max %.3f）" %
      (arm, float(np.median(shares[arm])), min(shares[arm]), max(shares[arm])))
A("- **如实记录**：两臂 whole-run 份额均为 **50/50**（arb_d2fq 权重不分化，与冒烟发现 F2 一致）；"
  "none 臂 50/50 属预期（等需求流无权重机制）。")
A("- 任务预期 'iox 4 rep'：实际数据 5 rep 全部 OK，故图中画 5 条。")
A("**图 fig.d2fq**：t400 累计字节份额曲线——**示例序列**（幸存 stray_logs/lat_t100/t400_bw.1.log"
  " = rep5 iox 单 run，虚线红；1s bw 累计积分，全程贴 0.50）+ 端点散点（各 rep whole-run 份额，"
  "全部 0.500）+ 0.2/0.8 参考线；Native N/A 以图注说明。完整 per-rep 1s 序列缺失（D1）。")
A("")
A("## 8. linnos")
A("")
A("负载：4 相位 × 30 s；LC = 4k randread QD8 恒定，BE = 4k randwrite QD 8/16/32/64。"
  "IOX = q_linnos（BE 侧分带拦截目标 band0/1/2 = 0/40/60%）；none 无拦截机制；native N/A。")
A("数据 = `loader_q_be.stdout`（pol=q_linnos）末行累计计数：`band[i]`=各带累计插入数，"
  "`ir{i}[1]`=各带 BE 被拦截数（intercepted[band][class=BE]）。")
hdr = ["rep", "ins total", "band0 ins", "band1 ins", "band2 ins",
       "实际率 b0 %", "b1 %", "b2 %", "目标 0/40/60 %"]
rows = []
for rep in REPS:
    r = linnos_row(run_dir("linnos", rep, "iox"))
    if r:
        ins, band, irbe = r
        rate = [(100.0 * irbe[b] / band[b]) if band[b] else None for b in range(3)]
        rows.append([rep, ins, band[0], band[1], band[2],
                     fmt(rate[0], 2), fmt(rate[1], 2), fmt(rate[2], 3), "0/40/60"])
A(md_table(hdr, rows))
A("")
A("- **band 覆盖退化（如实呈现）**：5 rep 中 band2 占插入数 **>99.999%**（band0≈12、band1≈8 次），"
  "与冒烟发现 F4 一致：q_linnos inflight 计数泄漏（completions=0，policy 级完成钩子不触发）"
  "→ busy 代理运行中单调增长 → 插入几乎全部落 band2。因此 band0/band1 的实际率仅有极小样本意义"
  "（band1 的 100% 来自确定性 1/100 序号抽签在最初 8 次插入上的体现），band2 实际率 "
  "**≈60.00%** 与目标 60% 精确吻合（1/100 确定性抽签）。")
A("- none 臂：无拦截机制（图中文字标注）；linnos diag/rep0 两个诊断 run 未入正式统计。")
A("**图 fig.linnos**：三带分组柱（目标 vs 实际 median）+ 每 rep 散点；柱下标注各带插入量。")
A("")
A("## 9. 哨兵（3×10 s QD1 randread）漂移统计")
A("")
rs_nw = [float(sent[(a, r, arm)]["ratio_med_after_over_before"])
         for a in ("kyber", "io_max", "linnos") for r in REPS
         for arm in (["none", "iox"] if a == "linnos" else ARMS_ALL)
         if sent[(a, r, arm)]["ratio_med_after_over_before"]]
A("逐 run 数据见 `RESULTS/sentinel_drift.csv`。**机制性说明（重要）**：哨兵读 `/data/lc.bin`；"
  "每 run 开头 `reset_data_files` 将文件 truncate+fallocate（全洞），**before 哨兵的读大多命中"
  "文件洞、几乎不产生真实设备 IO**（QD1 ~93 万 IOPS）；若本 run 工作负载写过 lc.bin，"
  "**after 哨兵读的是真实数据块**（QD1 ~3–9 万 IOPS）。因此：")
A("- LC/工作负载写过 lc.bin 的实验（mq_deadline、io_weight、io_cost、io_latency、d2fq）："
  "after/before 比 0.01–0.15，全部 >20% 阈值——**属文件状态效应而非设备漂移**；"
  "跨臂之间（同实验内同文件状态）仍然可比。")
A("- 未写过 lc.bin 的实验（kyber、io_max、linnos）：ratio median = 0.996–1.000，"
  "范围 %.4f–%.4f，**无 >20%% 漂移**，可作为设备稳定性的直接证据（上界 1.0125 = linnos rep2 iox；"
  "U7 更正：此前误写为 1.011）。"
  % (min(rs_nw), max(rs_nw)))
A("- 按约定（[MSC]）漂移 >20% 记录不淘汰；125 run（含 mq_deadline_v2 15 run）中被标记 85 个，"
  "全部属于上述机制性类别。")
A("")
A("## 10. 结果文件索引")
A("")
A("| 文件 | 内容 |")
A("|---|---|")
A("| RESULTS/mq_deadline/fig.mq_deadline.{pdf,png} + data.mq_deadline.csv | F1 图与点数据 |")
A("| RESULTS/kyber/fig.kyber.{pdf,png} + data.kyber.csv | F2 图与点数据（含 native 深度线；p99 面板按 D1 不可恢复） |")
A("| RESULTS/io_max/fig.io_max.{pdf,png} + data.io_max.csv | F3 图与点数据（含 rep5 iox 示例 1s 序列） |")
A("| RESULTS/io_weight/fig.io_weight.{pdf,png} + data.io_weight.csv | F4 图与点数据（含 rep5 iox 示例份额序列） |")
A("| RESULTS/io_cost/fig.io_cost.{pdf,png} + data.io_cost.csv | F5 图与点数据（IOX vrate + native vrate rep1–4） |")
A("| RESULTS/io_latency/fig.io_latency.{pdf,png} + data.io_latency.csv | F6 图与点数据（fallback + IOX 账本 1s bw） |")
A("| RESULTS/d2fq/fig.d2fq.{pdf,png} + data.d2fq.csv | F7 图与点数据（含 rep5 iox 示例累计份额序列） |")
A("| RESULTS/linnos/fig.linnos.{pdf,png} + data.linnos.csv | F8 图与点数据 |")
A("| RESULTS/sentinel_drift.csv | 全部 110 run 哨兵逐次 IOPS 与比率 |")
A("| RESULTS/scripts/*.py | 分析脚本（只读 campaign/，只写 RESULTS/） |")

with open(OUT, "w") as f:
    f.write("\n".join(L) + "\n")
print("wrote", OUT, "(%d lines)" % len(L))
