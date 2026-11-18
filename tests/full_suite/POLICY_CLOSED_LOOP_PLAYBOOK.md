# POLICY_CLOSED_LOOP_PLAYBOOK — iosched_ext 策略闭环验证共享技术文档

> 用途：作为新会话 / subagent 完成 q_* 策略闭环验证的**背景上下文**（当前覆盖 q_mq_deadline / q_kyber / q_sp_edf / q_wedf / q_ddl；bfq / heimdall 待启用）。
> 沉淀自 Phase-1（3 并行 subagent 建闭环）→ Phase-2（共享层修复）→ Phase-3（审计 + 保真度对齐 + 策略 bug 修复），所有结论均经 VM 实测。
> 基准日期：2026-09-01 · Phase-3 全量 DT 回归基线：`results/full_suite/full_suite_20260901_1800` 系列（见 §7）

---

## 1. 全景：架构与闭环门面

```
kernel scx_io (kernel/linux-7.3.1/block/iosched_ext.c, 内建 elevator "scx_io")
   └── struct_ops BPF policy (userspace/q_*.bpf.c, loader_q_* 静态二进制)
        插入路径（唯一）: blk-mq plug flush → e->ops.insert_requests(hctx, list, flags)
                        = scx_io_insert_requests() → 每请求构建 iosched_insert_ctx
                          → policy->ops->insert_request(&ctx) → accept/bypass
                        （Phase-2 起：循环内逐请求 emit trace_block_rq_insert）

三层 DT (deterministic):
  Tier-0 smoke（IO 可达） / Tier-1 invariants（计数/覆盖不变量）
  Tier-2 micro oracle（golden: exact/partial 断言 dispatch ISSUE 顺序）

闭环门面（每策略一套，q_ddl 为 Phase-3 新增）:
  tests/q_<policy>_model.py   ← 算法 spec（host exit 0），golden 的 algorithm_derivation 来源
  tests/q_<policy>_accept.sh   ← VM 内验收（gate0=model 自测 + CASES 数组 + G1–G4 + compare）
  tests/q_<policy>_ci.sh       ← host 一键入口（rig env + scripts/run.sh --exec）
  （sp_edf/kyber/wedf/ddl 另有 q_<policy>_gates.py：G2/G3/G4 自算）
  共享断言引擎: full_suite/deterministic/{compare_expect.py, check_inflight.py, trace_driver, bpftrace/trace_dispatch.bt}
  全套件入口:   full_suite/run_on_virtme.sh --mode deterministic-only
```

**事件流**（bpftrace trace_dispatch.bt）：
- `INSERT ts= sector= nr= rwbs= ioprio= bytes=` — 请求进入块层（scx_io 路径，Phase-2 修复）
- `ISSUE ts= sector= nr= rwbs= ioprio=` — 派发到设备。**一切顺序断言只看 ISSUE，不看 COMPLETE**
- `COMPLETE ts= ...` — 完成
- 窗口隔离：trace 扇区过滤 + `TRACE_PLAY_START/END` + `cluster_events`（100ms gap 首簇）+ **rwbs 'A' 过滤**（Phase-3：REQ_RAHEAD 编码为尾字符 'A'，blktrace.c:2208；trace_driver 只发 O_DIRECT sync IO，故 'A' 事件恒为背景，compare/check_inflight 均已排除）

## 2. Rig 契约（不可协商）

| 项 | 值 | 理由 |
|---|---|---|
| `OSEXT_VM_DISK=virtio` | guest vda | nvme 1023 tags 结构性无法积压 |
| `OSEXT_VM_BPS_THROTTLE=200000000` | 200MB/s | 调度器必须有 backlog。**注意**：QEMU throttle 带 ~200MB 突发桶，小 case 实际不减速（mqdd agent 实测 ~2GB/s）——需要饱和的 case 要靠大请求而非 throttle |
| `echo 1023 > /sys/block/$DEV/queue/nr_requests` | accept.sh 在 scx_io 切换后设置 | pipeline 必须 > 设备 tags；**scheduler 切换会重置 nr_requests**（blk-mq-sched init），重切后必须重设 |
| trace v3 | `defaults:{delay_us:0, submit_mode:"async_batch", batch_size:8, queue_target:8}`, `expect_inflight_min≥4` | G1 物质基础；batch_size 可按 case 调大（M06=64、kyber depth trace=24） |
| 并发 VM | 独立 `OSEXT_VM_DISK_IMG=/tmp/opencode/<who>_disk.img` | QEMU 镜像写锁 |
| 破坏性写 | `IOEXT_DESTRUCTIVE_TEST=1` | fs_require_destructive_guard |

## 3. 修复清单（Phase-2 + Phase-3，全部已落地并验证）

**Phase-2（共享层）**：
1. INSERT 可观测性：kernel `scx_io_insert_requests` 循环内补 `trace_block_rq_insert(rq)`（iosched_ext.c）。
2. G2 真实计算：`dispatch_with_choice` = ≥2 INSERT-pending 的 ISSUE 次数；删 `g2=g1` 掩护。
3. bpftrace attach-wait：共享 runner 等 `Attaching` 行再放 trace。
4. mqdd M01/M02 trace 重排（原连续扇区 plug 合并塌缩 → 历史 INVALID_PASS）。
5. kyber I01/I02 expect 重写（死 invariant → 鲁棒 min/coverage）。
6. DT_CASES 注册 sp_edf M04 / kyber K01-K03+T01 / wedf M01/M02/I01。

**Phase-3（审计驱动：可信度加固 + 保真度对齐 + 策略 bug 修复）**：
7. kernel `iosched_allow_merge_ctx` 追加 `rq_opf`（填充于 scx_io_allow_merge + scalar_access 白名单 + vmlinux.h 再生成）。
8. rwbs 'A'(readahead) 过滤进 compare_expect.py + check_inflight.py（背景风暴不再进窗）。
9. trace 重排（诅咒扇区→2560+ 非幂）：mqdd M03/M04、wedf W1/W2；T1 expect 强化（insert_count_eq 现在可断言）；attach-wait 移植进 sp_edf/wedf accept。
10. **q_kyber 动态 token 对齐原生 Kyber**（见 §6.1）——审计确认原实现是另一套控制律（方向倒置/EWMA/±1 步长/增长冻结）。
11. **q_mq_deadline allow_merge bug 修复**：原 `is_write_op(rq_sector|0)` 把扇区当 opcode（写 bio 调度器级合并恒拒绝）→ 改用 `ctx->rq_opf`。D1≡D2 无区别已文档化。
12. **q_ddl 全旁路 bug 修复**：state_map 原 HASH(cgroup,hctx) 无任何创建路径 → lookup 恒 NULL → insert 恒 -1 → 策略被完全旁路（probe: dispatches=0；修复后 dispatches=54）。镜像兄弟模式改 ARRAY(hctx_idx)。
13. 新 case：mqdd M06 写饥饿翻转（800×128KB、降序提交）；kyber KYBER_DEPTH_SHRINK/HOLD（Tier-1 动态深度，原生律方向断言 + state_map dump）；q_ddl triad（T1+M01-M03）。
14. M05 几何修复：单合并组塌缩成 1 请求 → G2 结构性不可满足 → 改双组（K03 模式）。
15. 注册：mqdd M06、q_ddl SMOKE/I01/M01-M03；`FS_NON_TRIVIAL_POLICIES += q_ddl`。

## 4. G1–G4 精确语义（post-fix）

| 门 | 定义 | 陷阱 |
|---|---|---|
| G1 | max_inflight ≥ 2（ISSUE-but-not-COMPLETE 峰值） | 诅咒扇区背景可虚假抬高（现已被 'A' 过滤+重排根治） |
| G2 | ≥1 ISSUE 时存在 ≥2 INSERT-but-not-COMPLETED | **合并塌缩几何**：单合并组 → 调度器只见 1 请求 → G2 诚实失败。合并 case 必须用 ≥2 组（K03/M05 模式） |
| G3 | 窗口纯度（扇区过滤+簇内外 sector/nr 匹配） | 100ms gap 会吞突发后 1.9-8ms 的背景 |
| G4 | 窗口 insert_count == trace 请求数（±有意合并） | INSERT 已可观测（Phase-2 起）；干净扇区 trace 用 insert_count_eq 强化 |

**INVALID_PASS 条款**：Tier-2 PASS 但 G1–G4 未满足 → 不算 Done。禁止 `match:"set"`。

## 5. Trace / Golden 设计铁律（Phase-1~3 血泪汇总）

1. **合并免疫**：同 op 扇区间隔 ≥8（建议 512+）；有意合并 case 用 **≥2 个合并组**（G2 几何）+ `invariants_only`（issue_count_lt + merge_count_min + merge_coverage_min），永不 exact 顺序。
2. **避开诅咒扇区**：分区扫描/背景命中 {0,32,64,...,2048} 及 2 的幂（4096/8192 实测也在内）；新 trace 一律 ≥2560 且跳过 2 的幂。
3. **断言 ISSUE 顺序**；`expected_merge:false`（除非有意合并）。
4. **每 case 5 次重试** + 尝试间 `fs_cleanup_ioext`；bpftrace attach-wait 必备。
5. golden 必须由 model.py 推导并交叉校验；反证（改错 golden 必 FAIL exit 1）要真跑（五策略现已齐备）。
6. **升序到达陷阱（mqdd 方向状态机）**：升序提交时 tree-dispatch victim == FIFO front → stale-front scavenge 消耗 continuation 调用 → 内核走 evacuate fallback（绕过 dir/batching/starved）。要驱动方向选择状态机（如 M06 饥饿翻转）必须**降序提交**。
7. **饱和 case**：需要数百请求积压时（M06），用大请求（128KB）让提交速率 ≫ 完成速率（throttle 突发桶不可靠），batch_size 调大到覆盖整批。
8. **kyber 深度 case 窗口控制**：评估窗口需每 domain ≥500 样本或 >1s；用 ≥500 请求/domain 或 delay_us 拉长。CLI target 只有在 loader attach 后**重切 scheduler**才生效（见 §8.1）。
9. **trace_driver 64KB 缓冲上限**：trace JSON 文件超过会截断解析（kyber 1608 请求 trace 压缩到 47.7KB 解决）。

## 6. 策略语义速查（golden 推导以此为准）

### 6.1 q_kyber（Phase-3 已对齐原生 Kyber 核心律）
- dispatch 核心（不变）：4 域 FIFO + cur_domain RR + batch 16/8/1/1 + inflight<depth_limit 门控。
- **动态 token（原生律，ioext_policy_shared.h kyber 节）**：初始 cap {READ 256, WRITE 128, DISCARD 64, OTHER 16}（不再均分 async_depth/4）；每完成采样双直方图（total+io，8 桶宽 target>>2，OTHER 除外）；窗口 ≥500 样本或 1s；拥塞 flag = 任一域 p90-io ≥ 桶4；resize 条件 bad||p99≥4，新深度 = 当前×(p99+1)>>2 clamp [1,cap]（p99 用 total 延迟；**方向：延迟差→增长至 ×2，延迟好+拥塞→收缩至 ×0.25，否则保持**）；跨窗口失步用 remembered-p99（域 p99 记忆）；增长冻结机制已删除；OTHER 不参与反馈。
- 保留偏差（已文档化）：limit_depth→shallow_depth 馈给 clamp ≤ queue_async_depth；per-hctx 状态（原生 per-queue）；completed_request 内联评估替代 100ms timer；urgent-FIFO requeue（原生为 domain-list 头插）。
- 验证：KYBER_DEPTH_SHRINK（写 tiny target→拥塞→读域收缩 256→64）+ HOLD（全 huge→不动）+ state_map dump 断言；K1/K2/K3 顺序不变。

### 6.2 q_mq_deadline（参考标准，大体忠实）
- C-LOOK + 3prio×2dir lane + fifo_batch 16 + writes_starved 2 + read expire 50ms/write 500ms + prio aging 10s（10× 短于原生，同 10:1 比）。
- **调度器级 allow_merge 在 scx_io 下结构性不可达**（plug merge 传 sched_allow_merge=false；唯一调度器侧调用方 blk_mq_sched_try_merge 只被 C 调度器使用）——stats map 只能看到 bio_merge 路径（D3 on vs D1 off: bio_merge_true 50 vs 0）。
- M06 饥饿翻转语义：selection（batching==16 或当前 lane 空）时双 lane 非空才 `starved++`（读空→写 selection 会复位 starved=0）→ 翻转需 3 次连续混合 selection；实测 `sel_flip=6`、96-write 连续 epoch（sel_stats_map 遥测可作 branch-fired oracle）。
- merge 模式 D0(默认,全拒)/D1≡D2(allow_merge 兼容检查,无区别)/D3(+bio_merge)。

### 6.3 q_sp_edf
- 3 lane（small read +1ms / small write +2ms / large(nr>8) +20ms），lane 内 (deadline,sector)=到达 FIFO；large 抢占条件（small 空 ∨ burst 耗尽(8) ∨ 20ms 到期 ∨ 8ms max-wait）；burst 于 insert 归零时重蓄、large 派发后重置。M04 burst_18 验证 S×8,L,S×8,L 交错。

### 6.4 q_wedf
- 单队列，dl=arrival+(write?500ms:50ms)；|Δdl|>5ms 严格 EDF，窗口内扇区升序（tie-immune）。跨类：全读先于全写（450ms≫窗口，W2 验证）。
- **weight 未实现且无输入通路**（BLOCKED-weight，证据包 results/q_wedf_accept_*/weight_gap/）。

### 6.5 q_ddl（Phase-3 新入列；**非 legacy deadline 移植**——原生文件已从上游删除，这是 POL-05 血统原创策略）
- 双 lane LAT/BULK：RT ioprio→LAT、IDLE→BULK、其余 read&&cost≤8扇区→LAT 否则 BULK（大读是 BULK！）。
- 派发 5 规则：(1)LAT 头超期(1ms)&&emergency<3→LAT (2)BULK 头饿≥10ms→BULK (3)LAT 紧急(≤200µs)&&lat_burst<8→LAT (4)BULK pending→BULK (5)LAT pending→LAT。burst 计数仅 BULK 派发时复位。
- **冷启动突发序：BULK 先于 LAT**（全规则冷时规则 4 胜）——与 mq-deadline 读优先相反，M01 判别器。
- ioprio 不可经 trace_driver 传递（RT/IDLE lane 不可测）；超期/守卫/饿规则（1ms/200µs/10ms）在 DT rig 结构性不可测 → model 注入时间自测覆盖。
- 状态：per-hctx ARRAY（Phase-3 修复前 HASH 全旁路）；allow_merge 恒 false；无 tunables。

## 7. CI 现状与回归基线（2026-09-01 Phase-3 后）

| CI | 结果 | 备注 |
|---|---|---|
| q_sp_edf_ci.sh | PASS=6/6 exit 0 | M01-M04+T1+gate0；T1 强化（insert_count_eq=5）已过 |
| q_kyber_ci.sh | PASS=7/7 exit 0 | K1/K2/K3/T01+**DEPTH_SHRINK/HOLD**；K2 target 现真生效（重切后 [1e14,1e14,5e9,5e6] 验证）；I01/I02 flake 不复现 |
| q_wedf_ci.sh | PASS=4 exit 0 | W1/W2/T1 全 attempt=1（attach-wait 根治冷启动）；BLOCKED-weight 设计使然 |
| q_ddl_ci.sh | PASS=6/6 exit 0 | **策略修复后 dispatches=54**（修复前 0）；M02 旁路探测器转 PASS |
| mqdd（共享 runner） | M01-M04 PASS attempt=1 | 重排后 live 验证（mqdd agent scratch run）；M05 双组几何 + M06 见全量矩阵 |

**全量 DT 矩阵**（run_on_virtme --mode deterministic-only）：Phase-3 基线 `results/full_suite/full_suite_20260901_174251/`：**PASS=38 FAIL=0 BLOCKED=7**（45 case 含当时注册的 M06）。7 个 BLOCKED = 历史 bfq/heimdall 家族 6 个 + M06（rig 失配：nvme 无 throttle 下翻转点漂移、写 epoch 约束破——案例在其设计的 virtio 饱和 rig 上 PASS，证据 results/mqdd_phase3/ + full_suite_20260901_172250；该行已从共享矩阵移除，归属未来 mqdd triad）。**未来矩阵 44 case，预期 PASS=38 FAIL=0 BLOCKED=6**。
- q_ddl 五个新注册（SMOKE/I01/M01-M03）全 PASS attempt=1；M05 双组几何 PASS attempt=1；kyber 新律下 K01-K03/T01 顺序不变全 PASS；重排后的 M03/M04/W1/W2 全 PASS。

### 7.1 稳定性账本（多 boot 证据）
- sp_edf M01-M03：6 boot 无翻转（最稳）；kyber K01/K02/T01：6-7 boot；wedf W1/W2：3+ boot（重排后 attempt=1）；mqdd M01-M04：重排后 1 boot 首过 + 历史无效 PASS 已作废（`ISSUE nr=40` 合并塌缩实锤）。
- **历史勘误**：full_suite_20260805_170146/230111 的 mqdd M01/M02 PASS 为 INVALID（合并塌缩+g2=g1 掩护+背景抬 G1）；EVOLUTION_PLAN §0"5/5 PASS"以本文件为准。

## 8. 已知土地雷（未修/已定位）

1. **CLI rodata 惰性（rig 顺序）**：`--*-target-ns` 只在 BPF `init_hctx`（scheduler 切换路径）拷入 per-hctx state，attach 不触发。q_kyber accept 已修（attach 后重切 scheduler + state dump 验 target_lat_ns）。**任何带 rodata 调参的新策略都要走同模式**。注意 scheduler 重切会重置 nr_requests。
2. **vda 多 hctx**（64 vCPU VM，hctx 0-5+）：state_map dump 解析须选样本最多条目（kyber agent 实测 trace 落在 hctx 0/2/4/5 不定）。
3. VM 内 bpftool 是坏 stub；用 kernel-tree `kernel/linux-7.3.1/tools/bpf/bpftool/bpftool`；pinned map 只能 `dump pinned`；`-j` 单匹配时输出裸对象（kyber agent 踩过）。
4. `vng --exec` 不回传 guest exit code → CI 判定读 results artifact。
5. cluster_events 100ms gap 吞后续背景 → count 型 invariant 勿用 eq/lt 于低扇区。
6. q_prio/q_none 注册 case 仍 v2 sync trace（靠背景噪声过 G1）——Phase-4 TODO。
7. fs_wait_attached 的 dmesg grep 是死 fast-path（CONFIG 未开）；强证据用 policy_meta pinned dump。
8. **trace_driver 64KB trace 解析缓冲**；**QEMU throttle ~200MB 突发桶**（小 case 不减速）。
9. **升序到达 → mqdd 方向状态机不推进**（stale-front scavenge + evacuate fallback）→ 状态机 case 必须降序提交。
10. **M06 型翻转依赖提交速率≫完成速率**（host-device-speed dependent，10×+ 余量实测）；负载无关的 branch-fired oracle 是 sel_stats_map（已埋点）。

## 9. 待办（优先级序；bfq/heimdall 就绪注记）

1. **wedf weight 输入通路**（最高优先策略特性）：loader `--req-weight` → BPF map → desc.weight → deadline/weight → trace_driver weight 字段 → weight_probe case 转正（模型双模式分歧即回归信号）。设计已备（results/q_wedf_accept_*/weight_gap/registration_snippets.txt）。
2. **bfq / heimdall 验证启用**（用户既定下一步）：loader 已构建；历史 BLOCKED 6 case（bfq SMOKE=verifier -EACCES、heimdall 家族=cgroup 问题）是入口问题清单——triad 模式 + 本 playbook 即背景；bfq 注意其 BPF verifier 拒载需先修 loader。
3. kyber requeue/urgent-FIFO 路径未测（需 requeue 注入）；kyber OTHER 域 target 保留 CLI 兼容。
4. mqdd D0/D2 未测（D1≡D2 已文档化）；front merge 结构性不可用（ports 无 request_merge hook——未来特性）。
5. q_prio/q_none trace 重做（v2 sync → async v3）。
6. K2（kyber_rw_domains_8）两轮稳定后 partial→exact 收紧。
7. sp_edf 20ms/8ms large 守卫 + 多周期 burst re-arm 未测（时间依赖，model 覆盖即可）。

## 10. 文件所有权与续接

| 层 | 文件 | 归属 |
|---|---|---|
| 五策略 triad | tests/q_{mq_deadline 无 triad——用共享 runner；sp_edf,kyber,wedf,ddl}_* | 各策略；改动需 model↔BPF↔golden 三方同步 |
| trace/golden | traces/micro/{mqdd,spedf,kyber,wedf,ddl}_*.json + expect/{golden,partial}/... | 注册于 run_deterministic.sh DT_CASES（45 case） |
| 共享 harness | run_deterministic.sh / compare_expect.py / check_inflight.py / trace_driver.c / trace_dispatch.bt / lib_test.sh | 改前读 §4-§5 |
| kernel | iosched_ext.c（INSERT tracepoint + allow_merge rq_opf，均未提交） | 重建：`make -C kernel/linux-7.3.1 KCFLAGS="-DIOEXT_DEBUG=0" CC="ccache gcc" -j$(nproc) bzImage modules`；改 ctx 结构后必须**再生成 vmlinux.h**（rm 后 make build/vmlinux.h——规则不跟踪内核时间戳） |
| loader 构建 | `make -C userspace KERNEL_SRC=... q_<policy>`（目标名是 q_*，不是 loader_q_*） | 并行 agent 勿跑 make all |
| 证据 | results/（gitignored）+ 各 results/q_*_accept_*/、results/mqdd_phase3/ | counter_evidence/、weight_gap/、bypass_gap/、stats_map/、selstats_* 为根因证据包 |

## 11. 快速命令

```bash
# 五策略 CI（各含 gate0/attach-wait/G1-G4/反证）
bash tests/q_sp_edf_ci.sh && bash tests/q_kyber_ci.sh && bash tests/q_ddl_ci.sh && bash tests/q_wedf_ci.sh
# mqdd 走共享 runner（或全量矩阵）

# 全量 DT 矩阵（45 case）
./tests/full_suite/run_on_virtme.sh --skip-build --mode deterministic-only

# host 自测模型
python3 tests/q_{sp_edf,kyber,wedf,ddl}_model.py

# kyber 深度状态 dump（多 hctx 选样本最多条目；offsets 见 results/q_kyber_accept_*/KYBER_DEPTH_*/state_map_*.json）
kernel/linux-7.3.1/tools/bpf/bpftool/bpftool -j map show name state_map
```

## 12. 判定纪律

- FAIL 可接受，必须 root-cause 落盘（root_cause.txt / compare.json / bypass_gap/ 等证据链）。
- 禁止放水：无 `match:"set"`；G1–G4 未过不 PASS；改错 golden 必须能 FAIL（反证真跑）。
- 策略行为验证三支柱：**golden 顺序 oracle**（确定性机制）+ **state/telemetry map dump 断言**（动态机制，如 kyber depth）+ **stats map branch-fired 计数**（如 mqdd sel_flip）。
- 每轮实验后更新 §7 基线；历史 INVALID_PASS 一经发现立即勘误（§7.1）。