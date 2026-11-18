# 取证记录 · 2026-11-14 pilot 执行期间的平台异常

## 时间线（+08:00）
- 15:18 同步+守卫 OK；15:20 标定（健康）：solo 572 MB/s，contention ref cg2/4/8/16 = 965/931/912/906
- 15:23 首启 runner 三处 bug（canary 浮点/timeout 包函数/flock env），15:29 杀掉修复
- 15:31 宿主侧 IO 压力（PSI io avg10=20%，本地无 IO）→ 判断为快照合并；15:32 canary 955 恢复
- 15:36–16:14 pilot 主 pass：12 序列仅 5 PASS（健康窗 ~15–30% 占空比），塌缩 run 全部 GATE-LOW 打标
- 16:20 发现孤儿 bpftrace（block_io_latency_cost.bt，TaskStop 杀 executor 所致）→ pkill 后 canary 仍 41
- 16:35 排查矩阵：
  - 1M seq read = **2741 MB/s（全速）**
  - 4k randread：QD1=5120 IOPS（p50<50µs，p99 3.4ms）；QD16/64/256 全部封顶 **~10.2k IOPS**（p99 随深度 18/66/148ms 线性涨）
  - 根 cgroup：io.cost.qos enable=0、io.max 空、io.latency 空、无 ib_d2_* 残留、kprobes=0、trace 事件全关、无 bpftrace/perf 残留
- 结论：**~10k IOPS 硬限速在客户机之外**（宿主机对该 VM 磁盘的 drive throttle，或宿主存储路径变更）。今早同一盘实测 955 MB/s ≈ 237k IOPS，证明限速是当天新出现。

## 追加（17:00–18:00）：触发源隔离 + 回滚验证

- hypervisor claim(hv-node, <alloc-id>) → rollback `ioext3-homo-v2-base` → 开机 → **canary 971 MB/s（236k IOPS）、seq 4153 MB/s**：旧 VM 配置的限速随回滚消失，但…
- 重新标定（全绿：solo 927，refs 902–969）后 8 秒即再塌缩 → 塌缩与旧配置无关，是平台固有机制
- **触发源隔离实验**（等恢复后依次加压）：
  - numjobs=8×12s（~11GB）→ canary 979 健康
  - numjobs=16×12s（再 11GB）→ canary 988 健康
  - solo QD256×20s（再 18GB）→ **塌缩 41**
  - ⇒ 由**累计高带宽读流量**触发（估计阈值 ~60–80GB/周期，含此前 canary 消耗），与作业数/executor/快照无关
- 恢复极慢：16:58 塌缩 → 静置约 1h 后才回到 714（恢复时间观测器挂在后台）
- 机制猜测：宿主侧共享 NVMe 的容量型 QoS/预算（读流量预算 + 慢速回补）；seq 1M 在塌缩窗内 2.7–4.1GB/s（1M IO 只占 2.7–4.1k IOPS < 10.2k IOPS 配额）与"IOPS 预算"模型自洽
- 运营含义：一条 80s 满速序列（~50–70GB 读）≈ 一个完整预算周期 → 每序列之间需要长冷却，或宿主侧解除预算/调大额度

## 对数据的影响
- 塌缩窗内所有臂（含 canary、root cgroup 无任何策略的裸读）被压到同一地板 → 策略差异不可分辨；健康窗内的 run 有效（none 737 / io_cost 489 J=1.000 精确 1:2:3:4 / arb_wdrr 610&565）。
- 已采数据：`results/d2_rerun_kvmhost/`（5 PASS + 7 GATE-LOW 打标），pilot 日志 `/tmp/d2_pilot_kvmhost.log`、`/tmp/d2_pilot_round2.log`。

## hypervisor 操作失误（需管理员处理）
- 16:46 我以 `--agent zcode-d2-agent` 创建了分配 `<allocation-uuid>`（TTL 12h，至 2026-11-14T20:46Z ≈ 明地 04:46），但本机 token 归属 `hv-node`，对该分配无操作权（get/rollback/release 均 FORBIDDEN），VM 被 reserve 占用。
- 需要管理员：release `d9b4df34-…`（或删除该分配/为其签发 token）。之后我用 hv-node 重新 claim → rollback `ioext3-homo-v2-base`（该快照配好 ioext3 内核 + 96G bare sdb + loaders/framework）→ canary 验证 → 重跑 pilot。
- 若管理员同时能查 kvmhost 宿主上 VM 110 的磁盘 throttle（iops_rd）配置，可确认限速根因；rollback 到 11-06 快照（当时代码/配置跑过全速）预期会一并还原。
