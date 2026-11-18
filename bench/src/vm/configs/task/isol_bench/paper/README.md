# isol_bench paper — CloudLab 裸机 D1–D4 全量实验定义

paper/ 是 CloudLab NVMe 裸机（论文轨）上 D1–D4 全量、可扩展实验的**定义文件**。
固定定义与已收束的 rig 版（`gen_rig_native.py` / `gen_rig_iox.py`）逐字段一致；
只有三类刻意不同（对齐契约，2026-10-21 复核后冻结）：

> **双轨说明**：`gen_isol_bench_paper_yamls.py --track nvme` → 本目录 `paper/`
> （CloudLab NVMe 定义，硬件常数保持 NVMe 值，禁止混入 sdb 标定）；
> `--track sdb` → `paper_sdb/`（kvmhost 彩排轨：同一实验族 + 设备 sdb +
> rig 收敛的 sdb 标定常数，用于真机申请前的全量彩排）。两轨固定定义逐字段
> 一致，仅硬件绑定常数不同。

1. **扩展轴**：n（Q1 1..256 / Q2 1..17）、ncg（D2 2/4/8/16，rig 点=8）、
   D3 Pareto 点数（rig 点是每个网格的一个顶点）、D2 迭代（1..3，rig 跑 iter1）。
2. **硬件绑定常数**：设备 `nvme0n1`、io.cost 980PRO 线性模型、D2 饱和带宽
   2300 MiB/s（io.max 份额分母）、io.latency 微秒级 target。换设备必须重新标定，
   **禁止把 rig sdb 的标定（320 MiB/s 饱和、毫秒级 latency target）抄进来**。
3. **paper 附加监控**：D1-Q1 n≥64 的步骤额外挂 `ib_perf_stat`。

其余一切（fio 形状、拓扑、权重通道、BFQ、io.cost 清擦、none 中性、旋钮/策略集）
与 rig 生成器 verbatim 对齐。主路径 5 个 YAML 由
`scripts/gen_isol_bench_paper_yamls.py` 生成；Q3–Q6 手工维护。

## 文件与序列数

| 文件 | 问题 | 序列数 |
|------|------|--------|
| `d1_q1_latency.yml` | D1-Q1 延迟开销 × n | 173（19 臂 × 9 n + 开/闭 bookend 锚点） |
| `d1_q2_bandwidth.yml` | D1-Q2 带宽可扩展 × n | 173（19 臂 × 9 n + 开/闭饱和锚点） |
| `d2_fairness.yml` | D2 饱和争用 + 权重公平 | 204（17 臂 × 4 cg × 3 iter） |
| `d3_prio_util_lc.yml` | D3 LC 优先级/利用率 Pareto | 198（native 网格 + 16 iox） |
| `d4_burst_lc.yml` | D4 LC 突发响应 | 17（9 native + 8 iox） |
| `d1_q3_decomposition.yml` | D1-Q3 框架分层税（手工） | 12 |
| `d1_q4_homomorphism_kyber.yml` / `_mq_deadline.yml` | D1-Q4 同构移植（手工） | 2 + 2 |
| `d1_q5_tenant_scaling.yml` | D1-Q5 固定负载租户数扩展（手工） | 27 |
| `d1_q6_hierarchy_depth.yml` | D1-Q6 层级深度（手工） | 15 |

2026-10-21 裁撤（不再属于 paper 轨）：`d1_q7_real_ycsb.yml`、
`d1_q7_real_resnet.yml`（D1-Q7 删除）、`d3_prio_util_batch.yml`、
`d4_burst_batch.yml`（D3/D4 只保留 LC 前台变种）。

## 实验设计表（通俗版）

方法全集 = **native 9 knob**（none / kyber / bfq / mq_deadline 四个纯调度器 +
io_max / io_latency / io_cost / io_prio_class / io_bfq_weight 五个 cgroup v2 控制器）
+ **iox 策略**（10 个 queue 策略 loader_q_* + 6 个 arbiter loader_arb_*）。
每 cgroup 恒为 1 个 fio 进程（fio_subjob）；BFQ 恒 `low_latency=0, slice_idle=1`。

| 实验 | 回答什么 | 前台负载 | 后台负载 | 权重怎么打 | 扩展轴 | rig 对应点 |
|------|----------|----------|----------|-----------|--------|-----------|
| D1-Q1 | 租户越多，单请求延迟税多大 | LC：4K 随机读 QD=1 io_uring，60s（knob 全惰性） | 无 | 无（inactive 对照） | n=1..256 | n=32 |
| D1-Q2 | 租户越多，批量带宽怎么分 | batch：4K 随机读 QD=256 libaio，60s，钉 10 核 | 无 | 无（inactive 对照） | n=1,3..17 | 无（paper 扩展轨） |
| D2 | 盘打满时 1:2:…:ncg 权重分带是否公平 | ncg 个租户同时打满：4K 随机读 QD=256 libaio | — | 各方法自有通道：io_cost→io.weight 100·w；io_bfq_weight→io.bfq.weight 100·w；io_max→按 2300MiB/s 份额；io_latency→10+25(w−1)us；io_prio_class→idle/restrict/rt 三段；iox arbiter→loader −W=100·w（q_wedf/q_fifo 默认权重对照） | cg=2/4/8/16，iter=1..3 | cg=8 iter1 |
| D3-LC | BE 打满时 LC 的保护/利用率 Pareto | LC：4K 随机读 QD=1 libaio 60s | 4 BE：QD=256 | 每 knob 沿自己的轴扫：io_max BE 上限 128..2304MiB/s；io_latency LC 10..1175us；io_cost（min×rlat×权重比）；io_bfq_weight 1/64..1024 比例；io_prio_class 9 组合；调度器臂单点 | Pareto 网格 | 每轴含 rig 顶点（1024MiB/s、10us、(50,15,10000)、rt/idle、1000:1） |
| D4-LC | BE 跑着时 LC 突发，响应多快 | LC：延迟 60s 后 10s 突发，QD=1，逐 ms 记延迟 | 1 BE：80s QD=256 | io_cost 10000/1；bfq 1000/1；prio rt/idle；io_latency LC 10us；io_max BE 1GiB/s | 无（单点） | 完全一致 |
| D1-Q3 | 框架每一层的税 | LC QD=1 + batch QD=256 两条轨 | — | none→iox_bare→q_none→minimal→q_fifo→arb_noop 阶梯 | — | 无 |
| D1-Q4 | 同一算法 BPF 移植贵多少 | 读+写 QD=128 争用 | — | native kyber / mq-deadline vs iox 移植 | — | 无 |
| D1-Q5 | 负载不变、租户数变，控制面多贵 | batch 固定总 QD=256 | — | iox_bare / iox_fifo / iox_arb_noop / iox_arb_slo | n=1..128 | 无 |
| D1-Q6 | cgroup 树越深越贵多少 | batch QD=256 | — | policy 只挂 root，深度 1/2/4/8/16 | depth | 无（cf. rig T1 链形） |

卫生合同（全部序列生效）：
- **io.cost 清擦**：每个非 io_cost 序列写 `io.cost.qos.enable=false`，防止 io_cost
  序列在根 cgroup 上粘住的状态毒化后续序列。
- **none 臂中性**：none/kyber/bfq/mq_deadline 臂 io.weight 恒 100、无 per-tenant
  覆盖（绝不把 10000 写上 none）。
- **iox 权重通道**：iox 租户 cgroup 的 io.weight/io.max 显式 null（cgroup 只作
  BPF attach 点），权重只走 loader `-W`；arbiter 只吃 `-h/-v`，队列策略带 `-W`。
- **profile 无 `mode: native_*`**：每个序列都生成全局 scene（调度器逐序列钉死 +
  stale loader/BPFFS pin 清理），与 rig 收束做法一致。

## 未实现策略（会失败的序列）

2026-10-21 处置：
- `iox_arb_wfq` / `iox_arb_hwfq` / `local_ddl` **已从两轨网格删除**——ioext
  无任何源码/构建目标（纯 ref/gpt-matrix 提案），且生态位已有可用等价物
  （加权公平=arb_wdrr，class-aware deadline=q_ddl/q_sp_edf，层级加权=T2/T3 轨）。
- `iox_arb_slo` **已转正**：`loader_arb_slo` 存在（POLICIES_INVENTORY B 级），之前错标 UNIMPLEMENTED。
- `q_bfq` / `arb_bfq`：构建存在但 verifier BLOCKED（C 级），不进实验网格。

## Run

```bash
export PYTHONPATH=$PERF_ARENA_WORKSPACE/src
python3 -m vm.executor \
  --config src/vm/configs/task/isol_bench/paper/d1_q1_latency.yml \
  --sequence D1Q1_none_n1 \
  --output results/isol_bench/paper/d1_q1/D1Q1_none_n1
```

BFQ 仅在 paper 轨专用内核启用（CONFIG_BFQ_GROUP_IOSCHED）；iox 臂需先
`echo scx_io > /sys/block/nvme0n1/queue/scheduler` 并部署 `/opt/io-ext/userspace/loader_*`。