# Arbiter 策略开发指导（arb_* Dev Skill）

> 本文是 `arb_wdrr` 闭环开发过程中沉淀的**通用开发参考**，适用于后续 `arb_slo`、`arb_cost`
> 及任何新的 iosched_ext arbiter 策略。它包含：框架接口契约、DRR 核心契约、
> 测试环境（rig）要求、以及一份用真实调试代价换来的**易错点清单**。
>
> 闭环门面：`tests/io/arb_wdrr_ci.sh`（host 一键跑）→ `tests/io/arb_wdrr_accept.sh`（VM 内验收）
> 参考模型（算法 spec）：`tests/io/arb_wdrr_model.py`（先改模型、后改 BPF，二者必须同步）

---

## 1. 开发工作流（CI 闭环）

```
1. 改算法      tests/io/arb_wdrr_model.py（纯 python，表达 spec）
2. 同步 BPF    userspace/io/ioext_arb_common.h + arb_*.bpf.c
3. host 构建   ./scripts/make.sh arb_wdrr q_fifo        # 无内核改动时跳过内核
4. host 自测   python3 tests/io/arb_wdrr_model.py       # 必须全 PASS
5. VM 验收     tests/io/arb_wdrr_ci.sh                  # 12 项断言，exit 0 = PASS
6. 人工抽查    VM 内: bpftool map dump / dmesg（见 §5）
```

验收不通过时**先看遥测**（§5），不要凭直觉改算法。所有断言输出、fio json、
tenant_state dump 都落在 `results/arb_wdrr_accept_<ts>/`。

## 2. 框架接口契约（kernel/linux-7.3.1/block/iosched_ext.h）

arbiter 是 `struct_ops iosched_arbiter_ops`（struct_ops 类型名 `iosched_arbiter_ops`），
全局单例（先于 policy 加载）。7 个 hook + 1 个数据字段：

| hook | 调用时机（内核源已核实） | BPF 侧职责 |
|---|---|---|
| `pick_cgroup` | 每次 dispatch，`hold_dispatch` 通过后 | 返回选中 cgid；0 = 本次不派发 |
| `hold_dispatch` | pick 之前 | true 则本次直接 NULL（SLO 备用保护） |
| `inserted_request` | local policy `INSERT_ACCEPT` 之后 | `pending++`（arbiter 自维护的队列深度） |
| `dispatched_request` | 选中 cgroup 真正派发、kernel 校验通过后 | **charge**（WDRR: bytes；COST: cost）+ `pending--` + `inflight++` |
| `dispatch_failed` | 选中 cgroup 返回 invalid rq（rq_ptr 非 0 但非法） | `pending--`（见易错点 3）+ cursor 前移 |
| `requeue_refund` | `blk_mq_requeue_request` 中（重入队） | **只退 charge + `inflight--`，绝不碰 pending**（易错点 4） |
| `completed_request` | 请求完成 | `inflight--` |
| `arb_mode` | 数据字段 | `IOSCHED_ARB_STRICT`（加权策略必须；否则 generic active-list fallback 会绕过公平性） |

**strict 语义**（`scx_io_dispatch_request`）：`bpf_in_flight > 0` 时若 arbiter 为 STRICT，
generic fallback 被完全抑制（`ci_fallback_count++`，返回 NULL）。liveness 依赖 arbiter
在有人 pending 时必须返回非 0 —— 这是 strict 模式的核心契约。

**arb_mode 如何到达内核**：loader 用手工构造的 stub 做 `bpf_map_update_elem` 再
`BPF_LINK_CREATE`（struct_ops map 不支持 lookup，内核侧返回 -EINVAL）。因此 loader
从 `bpf_map__initial_value()`（即 `.struct_ops.link` 段里 BPF 源码声明的值）读取 arb_mode，
**不要在 loader 里硬编码**（ioext_loader_common.h 已修，保持该模式）。
dmesg 验证：`iosched: arbiter 'xxx' ... arb_mode=1`（reg 时无条件 pr_info）。

## 3. DRR 核心契约（ioext_arb_common.h / arb_drr_pick）

状态：`tenant_state[(cgid,hctx)]` = {deficit, pending, inflight, 计数遥测, refilled_round}；
`hctx_state[hctx]` = {cursor, current_round}。

```
eligibility : pending > 0（只用 arbiter 自己的 pending，绝不读 policy_meta.queue_depth）
turn grant  : deficit <= 0 且本轮尚未 refill（refilled_round != current_round）
              → deficit += quantum × eff_weight（cap = 2×budget），本轮只此一次，
                并立即选中该 tenant（refill 即获得服务权，经典 DRR）
serve       : deficit > 0 → 继续选中同一 tenant（cursor 不动），每次 dispatch charge
exhaust     : deficit <= 0 且本轮已 refill → cursor 前移，本轮不再服务
round 边界  : 扫描一圈无人可服务 → current_round++，返回 0（每轮一次 NULL dispatch，
              blk-mq 会在下一次 completion kick 时重试，无 liveness 风险）
empty tenant : pending == 0 → 跳过且不 refill（不占轮次、不囤 credit）
```

- charge 单位即公平单位：WDRR=bytes，COST=`arb_cost_estimate()`。`requeue_refund` 的退款
  值必须与 dispatched 时的 charge 单位一致。
- quantum 是**每单位 weight** 的预算：round budget = quantum × weight。当前 64B×weight（S6：预算必须在单次 visit 内绑定才产生权重分离；20260919 硬件 dump 证明旧 32KB×weight 的预算远超 per-visit 深度，visit 全部 pending 结束、份额均分）。
- 预算下限 `ARB_WDRR_MIN_BUDGET_BYTES`(256KB)：小 weight + 大请求不至于永久饥饿。
- deficit 允许瞬时为负，overshoot 上界 ≈ 一个 max request。
- **修改本文件必须同步 `tests/io/arb_wdrr_model.py`**（它就是 spec，CI 第 0 关）。

## 4. 测试环境（rig）——没有这个 rig，WDRR 语义无法被测出

**一个 IO scheduler 只能仲裁"真实排队"的请求。** 三层原因（全部实测踩过）：

1. **设备必须饱和**：不饱和时 elevator 是直通的暂存区，`__blk_mq_do_dispatch_sched`
   会一直抽到 driver tag 耗尽。QEMU 原生 nvme 完成快于 fio 提交（~106K IOPS 是 fio 单核
   CPU 瓶颈），请求全部瞬间进设备队列。→ 用 QEMU 块层限速模拟饱和设备：
   `OSEXT_VM_BPS_THROTTLE=200000000`（throttling.bps-total，run.sh 已支持）。
2. **sched pipeline（nr_requests）必须 > 设备队列深度**：live request 上限由
   nr_requests 决定；`nr_requests == device tags`（nvme 1023==1023、virtio 256==256 的
   默认值都是！）时 elevator 永远为空。accept 脚本在 set_scx_io 后 `echo 1023 > nr_requests`。
3. **设备队列必须浅**：QEMU nvme 固定 1023 tags（8.2 版无 queue_size 属性），
   即使 nr_requests=1023 也全被设备吃掉 → **nvme rig 结构上无法测仲裁**。
   → `OSEXT_VM_DISK=virtio`（vda，~256 tags）+ nr_requests=1023 → elevator 稳定积压。

**一键 rig**：`tests/io/arb_wdrr_ci.sh`（封装上述 3 个条件）。
症状对照：ratio≈1 且 charge coverage PASS = 设备未饱和/pipeline 未展开（看 HINT 提示）。

fio 侧还有两条铁律（`arb_wdrr_accept.sh` 已内置）：
- **`echo $BASHPID > cgA/cgroup.procs`**：进程必须真正进入 cgroup（`cd` 进目录**不算**）。
  否则 IO 走 root bypass，tenant_state 为空、policy_meta dispatches=0（正是 8/14 版
  "ratio≈1" 的主因之一）。
- **两个 fio 必须钉同一 CPU**（`cpus_allowed=1`）：per-hctx 仲裁，不同 CPU → 不同
  hctx → 两个租户永不同场。64 CPU→64 queue 是 1:1 映射。

## 5. 遥测与排障工具（不需要 debug build）

`arb_tenant_state` 自带 pick 遥测计数，`bpftool -j map dump name tenant_state` 直接读：

| 字段 | 含义 | 异常形态 |
|---|---|---|
| `pending` | arbiter 视角的 (cgid,hctx) 队列深度 | 恒为 0 → rig 不成立（§4）|
| `dispatches`/`completed_bytes` | 已 charge 的请求数/字节数 | 与 fio ios/bytes 不等 → 有 bypass 或 fallback |
| `serve_selects` | deficit>0 的选中次数 | 两者相近且 refills 3:1 → 退化为 request-level RR |
| `refill_arms` | refill 次数（≈轮次） | n_a×budget_a == n_b×budget_b 恒等 → 轮结构未生效 |
| `skip_pending_zero` | 因空队列被跳过的扫描步数 | 巨大 → 队列没积压 |

- **charge coverage 判据**：`(completed_bytes − requeued_bytes) ≈ fio io_bytes`。
  generic fallback 派发不会经过 `dispatched_request`，因此 coverage 破口 = fallback 发生
  ——这是 strict 生效的定量证明（比 `ci_fallback_count` 更可用，后者未暴露）。
- 时间序列采样器：`tests/io/debug_wdrr_sample.sh`（每秒 dump tenant_state）。
- **bpf_printk 进 tracefs 而非 dmesg**（`/sys/kernel/debug/tracing/trace_pipe`）。
  内核侧 `ioext_dbg_pr_info` 才进 dmesg，且需 `CONFIG_IOSCHED_EXT_DEBUG`（当前未开）。

## 6. 易错点清单（每条都有真实调试代价）

1. **`cd cgroup` ≠ 进 cgroup**。fio 必须写 `cgroup.procs`；子 shell 里用 `$BASHPID`
   （`$$` 是父 shell 的 pid，会把整个测试脚本迁进去）。
2. **`echo '+io' > cgroup.subtree_control` 必须在 cgroup 已建后、IO 前生效**。漏掉它时
   子 cgroup 没有 io css，policy 会注册到 root css（`policy_cgroup_id=1`），pending 全落在
   错误 key 上，strict 模式下 IO 直接挂死。
3. **dispatch_failed 必须退 pending**：内核在 invalid-rq 路径已经把 rq 从 policy 弹出，
   不退会造成 pending 泄漏 → strict 模式 pick 永选空队列 → IO 挂死。
4. **requeue_refund 绝不能 `pending++`**：内核 requeue 后会把 rq 重新走
   `blk_mq_insert_request → insert_requests → inserted_request`（pending 已恢复）。
   再加一次 = 每次 requeue 泄漏 1，strict 下死锁。refine.md KER-09 的原设计与本内核
   实现不符，以 `arb_note_requeue` 注释为准。
5. **pinned map 是匿名 map**：`iosched_loader_create_pinned_map` 的 BPF_MAP_CREATE
   没设 name，`bpftool map dump name policy_meta` 永远空。要用
   `map dump pinned /sys/fs/bpf/ioext_policy_meta`。
6. **bpftool 原始字节输出**：BTF-less map（pinned map）的 key/value 是十六进制字符串数组，
   `int('0x2e')` 会抛异常（要 `int(b, 0)`）。解析器两种格式都要兼容（见 accept 脚本
   `decode_tenant_entries`）。另外注意 shell 里 `TS_FIELDS = "..."`（带空格）不是赋值。
7. **arb_mode 别硬编码**：读 `bpf_map__initial_value()`，旧 struct 的 map 有 size guard。
8. **轮语义是 DRR 的命门**：refill 即 turn grant、每轮至多一次。曾有两版错误实现：
   (a) refill 后立刻再选（永不让出 cursor）；(b) exhaust→refill→advance（wrap 路径给
   刚 refill 的租户额外 turn，turn 数反比于 weight，服务比恒 1:1）。修法都是围绕
   "每轮每租户至多一次 grant"。
9. **cgroup inode == cgroup id**：`stat -c %i` 可直接当 cgid 用（与 loader 的
   name_to_handle_at 一致）。
10. **completion hook 的 cgid 与 insert 不一致的已知问题**：`inflight` 在当前内核下
    不递减（completed_request 的 ctx.cgroup_id 与 insert 路径来源不同），WDRR 不受影响
    （inflight 不参与 pick），**arb_slo 开发时必须先修**（hold_dispatch 依赖 inflight）。
11. **IOEXT_DEBUG=1 构建会显著改变行为**（tracefs printk 洪泛可致 fio 退出挂死），
    只用于一次性诊断；CI 一律用默认构建。
12. **`make` 不跟踪 flag 变化**：改 IOEXT_DEBUG 后要 `rm` 掉 build/arb_*.bpf.o 再编。

## 7. 新策略上线 checklist（arb_slo / arb_cost）

1. 在 `arb_*.bpf.c` 声明 `.arb_mode = IOSCHED_ARB_STRICT`（loader 会透传）。
2. charge/refund 单位成对实现（dispatched↔requeue_refund），复用 `arb_note_*`。
3. per-tenant 状态用 `arb_tenant_state`（加字段加在**尾部**，同步改 accept 脚本
   `TS_FIELDS`/FMT 和 model）。
4. boost/charge 逻辑以 `get_boost_fn` / charge 单位注入 `arb_drr_pick`，不复制 pick 本体。
5. 新写 `tests/io/arb_<name>_model.py`（spec）+ `tests/io/arb_<name>_accept.sh`
   （复制 wdrr 版改断言）+ `tests/io/arb_<name>_ci.sh`（改 rig 环境变量即可）。
6. 验收 = §1 的 6 步全绿；断言至少覆盖：strict armed、比例窗口、charge coverage、
   状态守恒、kernel stable。