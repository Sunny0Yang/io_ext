# io_ext 8 算法复现实现审计（逐条对照 Linux native 源码）

> 审计日期 2026-11-14。目的：为 bm virtio-VM guest（内核 7.3.1-ioext3，vdb）的
> 8 算法 × {none / Native / IOX} 三臂单图实验，核对每个算法"Native 关键机制 →
> IOX 复现实现"的对应关系，列出**已声明偏差**与**未声明差异（审查发现）**，
> 并给出两侧内部状态的可观测途径（供主会话实验改造直接引用）。
>
> **源码基准与行号口径**
> - IOX 复现实现：`/opt/ioext-io_ext/userspace/io/`，以**冻结 commit
>   以审计时的仓库冻结 HEAD 为准**。工作树是脏的（D1 时代未提交改动：
>   `arb_wdrr/arb_iocost/arb_iolatency/q_iomax/q_linnos/ioext_arb_common.h/
>   ioext_loader_common.h` 等有 diff；`q_mq_deadline.bpf.c`、`q_kyber.bpf.c`、
>   `arb_d2fq.bpf.c`、`q_none.bpf.c`、`q_fifo.bpf.c` 与冻结版**逐字节一致**）。
>   本审计对这 5 个脏文件全部用 `git show <frozen-commit>:<path>` 提取冻结行号。
> - Native 内核：`/opt/ioext-io_ext/kernel/linux-7.3.1/block/`
>   （下文 `mq-deadline.c:30` 即 `kernel/linux-7.3.1/block/mq-deadline.c:30`）。
> - D2FQ 参考实现：`skkucsl/d2fq reference tree/block/d2fq.c`（FAST'21
>   skkucsl/d2fq，Linux 5.3 补丁）；native port 文档
>   `experiments/homo_v2/D2FQ_NATIVE_PORT_20261110.md`。
> - 行为镜像模型：`/opt/ioext-io_ext/tests/io/*_model.py`（每臂均有）。
> - stats-interval 补丁（主会话已加，冻结树中**没有**）：
>   `experiments/reproduction/loaders_stats1/loader_stats_interval.patch` +
>   `ioext_stats_json.h`；IOX 侧内部状态观测一律引用该补丁的
>   `--stats-interval-ms N`（每 N ms 打一行 JSON 到 stdout）。

## 0. 总览

| # | 算法（本轮单图主指标） | IOX 文件（冻结行号） | Native 对位 | 忠实度判定（本轮复核） | 最大实验风险 |
|---|---|---|---|---|---|
| 1 | mq-deadline：读写 P99 对比 | `q_mq_deadline.bpf.c`（1777 行） | `mq-deadline.c` | 完整移植；调度法则一致，合并默认 D0 + c-look wrap 分支两处差异 | 合并禁用是臂间系统差，不是移植误差 |
| 2 | kyber：LC 读 P99 + depth-limit 时序 | `q_kyber.bpf.c` + `ioext_policy_shared.h:1380-1666` | `kyber-iosched.c` | 法则级完整移植；timer→inline 一条声明偏差 + 1 个未声明上限 | 慢相位 1.56–1.60× gap 未解释（正需时序定位） |
| 3 | io.max：写带宽时序 vs 32 MiB/s cap | `q_iomax.bpf.c`（冻结 467 行） | `blk-throttle.c` | 机制级（4 通道 token bucket ↔ slice/trim 模型），限速语义对齐已由 T1 数据支持 | token/slice 两种 burst 形态在低速率通道可分辨 |
| 4 | io.weight：两 cgroup 字节份额 | `arb_wdrr.bpf.c`（冻结 205 行） | BFQ（io.weight/bfq.weight） | 语义级近似（声明）；**native 侧传导路径存疑（审查发现 #4-1）** | native io.weight 臂的"载体"与源码矛盾，须先运行时裁决 |
| 5 | io.cost：vrate 时序 | `arb_iocost.bpf.c`（冻结 750 行） | `blk-iocost.c` | 机制级（冻结线性模型 + vrate FSM，偏差 (a)–(e) 声明） | 偏差 (c)（轮预算 vs 周期预算）的实证形态差异 |
| 6 | io.latency：LC P99 + BE 带宽时序 | `arb_iolatency.bpf.c`（冻结 455 行） | `blk-iolatency.c` | 机制不同、LC 侧可观测近似（声明）；BE 方向分歧从未检验；**violation 门限差异（审查发现 #6-1）** | BE 再分配方向（自节流 vs boost）是本轮必答问题 |
| 7 | D2FQ：累计字节份额 | `arb_d2fq.bpf.c`（823 行） | FAST'21 参考 `d2fq.c` / ioextd2fq port | 机制级近似（静态部分）；动态再标定未复现（声明）；VM 内 native 臂 N/A | thrsh 量纲与参考码不同源，band 分布不可直接对读 |
| 8 | LinnOS：BE 目标拦截率 vs 实际 | `q_linnos.bpf.c`（冻结 504 行） | 无可信参考（论文 OSDI'20） | 能力级（离线查表控制律，声明）；VM 内 native 臂 N/A | 拦截率证据只有 loader 计数（外部只有 BE 吞吐代理） |

---

## 1. mq-deadline（读写 P99 对比图）

| 项 | 内容 |
|---|---|
| Native 关键机制 | 常量：`read_expire=HZ/2(50ms)`、`write_expire=5*HZ(500ms)`、`prio_aging_expire=10*HZ`、`writes_starved=2`、`fifo_batch=16`（mq-deadline.c:30-39）。per-prio（RT/BE/IDLE）× per-dir（R/W）的 sort_list(rbtree)+fifo_list+latest_pos（mq-deadline.c:73-79）。派发：`__dd_dispatch_request`（mq-deadline.c:322-411）——batching<16 时沿 last_dir 的 sector 顺序续发（334-339）；否则读优先 + `starved++ >= writes_starved` 翻写（346-356）；`dispatch_find_request`：若 FIFO 头到期**或**无 sector-后继则取 FIFO 头，否则取 `latest_pos` 后继（375-393）；优先级老化 `dd_dispatch_prio_aged_requests`（417-439，`started_after` 门 402-403）；总入口 `dd_dispatch_request`（449-483，RT→BE→IDLE、`rq || dd_queued()` 断 475-477）。**整队列单实例**（`QUEUE_FLAG_SQ_SCHED`，mq-deadline.c:556，注释 446-448）。`dd_limit_depth`：仅非同步读压 `shallow_depth=q->async_depth`（485-489）。sysfs `read_expire/write_expire/writes_starved/front_merges/fifo_batch/prio_aging_expire`（784-792）；debugfs `batching/starved/queued/owned_by_driver/dispatch`（963-982）。`front_merges=1` 默认开（549）|
| IOX 实现要点 | 6 lane（RT/BE/IDLE × R/W）per-hctx：lane_map/dispatch_map/hctx_map（q_mq_deadline.bpf.c:70-89）；常量同 native 默认（ioext_policy_shared.h:18-22）；`deadline_ns = arrival + expire`（q_mq_deadline.bpf.c:1329）；c-look 选块 `lane_dispatch_c_look`（728-753）：FIFO 头到期→FIFO 头，否则 `first_ge(latest_pos)`，否则 `first_sector`；starved/batching/last_dir 状态机 `local_dispatch_prio`（915-987）与 aged 版（989-1048，多级老化 1111-1126）；AT_HEAD 走 dispatch 队列（1331-1386 ↔ native 644-647）；`limit_depth` 只对非同步读返回 `cfg->async_depth`、同步读返回 0（1282-1295，语义 ↔ native 485-489）；方向选择遥测 `sel_stats_map`（267-294，含 sel_flip/starved_value，为 writes_starved 分支的覆盖证据）；merge 模式 D0–D3（213-230），D0=全拒 |
| 已声明偏差 | (1) merge：IOX 默认 D0（allow_merge=false、bio_merge=false，q_mq_deadline.bpf.c:1468-1507）；native `front_merges=1` + 后向合并（mq-deadline.c:549、600-615）。实验口径即 merge 0（gen_homo_v2_yamls.py:113-114、RUNBOOK_kvmhost §8.4"merge 0"），**对 native 臂这是行为差不是移植误差**（见风险点）。(2) 前 merge 结构性不可批：`request_merged` 只计数、无树重定位路径，前合并会孤儿化 sector 键（q_mq_deadline.bpf.c:367-394 注释，mqdd_merge_compatible 只放行后向邻接）。(3) ~20us/req 结构性提交路径开销（HANDOFF §5.3，kvmhost 成对 p50 1.04×/p99 1.28×）。 |
| 未声明差异（审查发现） | **审查发现 #1-1（c-look wrap 分支）**：native 在"无 sector 后继且 FIFO 未到期"时取 **FIFO 头**（mq-deadline.c:379-386：`!next_rq → deadline_fifo_request`）；IOX 同场景取 **sector 最小者**（q_mq_deadline.bpf.c:747-752 `lane_first_sector`）。回绕时派发对象可能不同（FIFO 头=最早到期 vs 最低 sector）；4k randread 且 FIFO 未到期时触发概率低，但 70/30 混合流下可产生偶发顺序差。**审查发现 #1-2（作用域）**：native 调度器状态是**整队列单份**（QUEUE_FLAG_SQ_SCHED，mq-deadline.c:556），IOX 状态是 **per-hctx**（Q_MQDD_MAX_HCTX=256 组 lane，14-89）。VM 的 virtio-blk 若配置多 hctx，两臂的 FIFO/批处理作用域不同（单 hctx 时无差异）。 |
| 内部状态观测 | IOX 侧：本轮**无内部状态需求**（主指标为外部读写 P99 时序，由 fio `*_lat.1.log` 1s 分桶即得）。如需派发方向/饥饿证据：`sel_stats_map`（非 pin，loader 存续期间 `bpftool map dump name sel_stats_map`）或 merge 证据 pin 路径 `/sys/fs/bpf/merge_stats_map`（loader_q_mq_deadline.c:86，冻结版）；stats 补丁未覆盖 q_mq_deadline——不需要加。Native 侧（源码确证）：debugfs `/sys/kernel/debug/block/vdb/` 下 `batching`、`starved`、`queued`（"rt be idle" 三列）、`owned_by_driver`、`dispatch`（mq-deadline.c:963-982；CONFIG_BLK_DEBUG_FS=y 见 §10）；sysfs 参数 `/sys/block/vdb/queue/iosched/{read_expire,write_expire,writes_starved,fifo_batch,prio_aging_expire}`（784-792）。 |
| 实验风险点 | ① merge 差异：IOX D0 拒所有 scheduler 级合并 → 请求尺寸分布与 native 不同 → IOPS/延迟分布天然有差（bm elev_mq 读 p99 中位差 0%、快窗 +12.9% 的既有口径不受影响，但单图解读时勿把 merge 差归因为调度法则差）。② 多 hctx guest 下 per-hctx 与 queue-wide 作用域差（#1-2）。③ 写饥饿保护（writes_starved=2）的可观测性在 IOX 侧已有 sel_flip 计数，native 侧只有 debugfs starved 读数——两侧证据不对称，判据只应落在外部读写 P99 双稳上。 |

## 2. kyber（LC 读 P99 + depth-limit 时序图）

| 项 | 内容 |
|---|---|
| Native 关键机制 | 4 域（READ/WRITE/DISCARD/OTHER，kyber-iosched.c:29-35）；深度上限 `kyber_depth[]={256,128,64,16}`（59-64）；延迟目标 `{2ms,10ms,5s}`（69-73，OTHER 不采样）；batch `{16,8,1,1}`（79-84）；**token 深度控制**：per-域 sbitmap_queue `domain_tokens`（157），`kyber_get_domain_token`（692-737）拿不到 token 即阻塞派发并挂 wake（681-690）。延迟反馈：8 桶直方图（宽 target>>2，99-112），total+IO 双型（118-121），per-cpu 累积 `kyber_cpu_latency`（132-134）；`calculate_percentile`（219-257）：≥500 样本或 1s 窗先到即结算（236-242），`DIV_ROUND_UP(samples*pct,100)`（244），结算清桶（250）；`kyber_timer_fn`（270-348）：任一域 IO-延迟 p90 ≥ GOOD_BUCKETS → 拥塞 `bad`（295-302）；逐域 total-延迟 p99，bad 时用记忆 p99 且复位（309-331）；`depth = orig*(p99+1)>>2`（342-345），`kyber_resize_domain` clamp [1, kyber_depth]（259-268）。timer 由完成路径 `timer_reduce(jiffies+HZ/10)` 重臂（644）→ **≤100ms 去抖定时评估**。`async_depth=nr_requests*75%`（410），`kyber_limit_depth` 仅非同步读（547-551）。观测面：debugfs `{read,write,discard,other}_tokens`（969-975，sbitmap depth 即当前深度）、hctx `{...}_rqs/{...}_waiting/cur_domain/batching`（981-989）；sysfs `read_lat_nsec/write_lat_nsec`（877-882）；tracepoint `kyber_adjust/kyber_latency/kyber_throttled`（include/trace/events/kyber.h:48/14/71，emit 于 252-254/265/767-781） |
| IOX 实现要点 | 状态机逐函数移植进 `ioext_policy_shared.h`：常量（33-65，SHIFT=2/GOOD=4/BUCKETS=8/MIN_PCT=500/WINDOW=1s/DEPTH_CAP 同 native）；`iosched_kyber_add_latency_sample`（1505-1538 ↔ add_latency_sample 607-623）；`calculate_percentile`（1545-1582，含 500 样本/1s 窗、ceil 除法、清桶 ↔ 219-257）；`resize_domain`（1586-1597 ↔ 259-268）；`update_depths`（1606-1645 ↔ kyber_timer_fn 270-348，含 domain_p99 记忆逻辑 1627-1633 ↔ 323-329）；per-hctx `state_map`（q_kyber.bpf.c:26-31，字段 `urgent_nr/domain_nr/inflight/depth_limit/lat_buckets/window_timeout_ns/domain_p99/cur_domain/batching/target_lat_ns`，policy_shared.h:341-363）；`init_hctx` 全量复位+CLI targets（q_kyber.bpf.c:419-463）；`completed_request` 内联喂样本并**当场**重估深度（631-671 → complete_feedback 1650-1666）；`limit_depth` 把 `depth_limit[domain]` 喂给 shallow_depth（543-585 → iosched_kyber_limit_depth 1453-1476）；旧 EWMA 字段保留 ABI 不用（356-359 注释） |
| 已声明偏差 | (1) **timer→inline**：native 是 ≤100ms 去抖定时评估（kyber-iosched.c:644 timer_reduce），BPF 无 timer → 每次完成内联评估、仅由窗口规则（500 样本/1s）时间门控（policy_shared.h:1599-1605 注释）。评估时机相对负载的相位不同——这正是慢相位 gap 的候选机理。(2) **limit 喂值 clamp**：native token 深度不受 queue depth 钳制；IOX 喂给 shallow_depth 的值 clamp 到 `queue_async_depth`，**存储的 depth_limit 仍保持 native [1,kyber_depth] 界**（policy_shared.h:1468-1475 注释"Port deviation (the only one)"——该注释指的是法则内部，配合 (1) 共两条声明）。 |
| 未声明差异（审查发现） | **审查发现 #2-1（urgent 容量 32 上限）**：IOX urgent/domain 队列是固定 32 槽数组（`IOSCHED_MAX_PENDING=32`，policy_shared.h:15、push_tail_32 满则 -1 → insert 拒绝，443-447）；native 用链表无上限（kyber-iosched.c:583-598）。AT_HEAD（urgent）积压 >32 时 IOX 会拒绝插入（核心层重试），native 不会。QD64 深队列读突发下同一域待插请求可瞬时超 32。**审查发现 #2-2（urgent 语义近似）**：native 无独立 urgent 队列——AT_HEAD 请求只是插到同域 kcq/rqs 链表头（590-593），与同域 FIFO 的相对顺序近似；IOX 是独立 urgent FIFO 且恒先于 domain 队列（policy_shared.h:1720-1739）。语义近似、非逐行同构。**审查发现 #2-3（token 记账点）**：native token 在 `kyber_finish_request`（含 requeue 同函数，600-605/1006）释放；IOX `inflight[domain]--` 在 `finish_request`/`requeue_request`（q_kyber.bpf.c:673-750 → release 1485-1502），等价；但 native token 由 **dispatch 时拿号**（kyber_get_domain_token，拿到才派发），IOX 是 dispatch 时比较 `inflight < depth_limit`（1668-1679）——并发多 hctx 下 IOX 的判定在 `state_dispatch` 自旋锁内做，等价性成立，但 native 的 token 等待唤醒（`*_waiting`）在 IOX 无对应物（NULL 返回后靠 blk-mq 重试）。 |
| 内部状态观测 | IOX 侧：`--stats-interval-ms 1000`（stats 补丁，`pol":"q_kyber"` 行）字段 `hctx[{idx, dl[4]=depth_limit[域], inf[4]=inflight, pend[4]=domain_nr, cur=cur_domain, bat=batching, p99[3]=domain_p99}]`（patch 内 q_kyber emit，fields 见 ioext_stats_json.h）——直接给出 depth-limit 时序与 resize 台阶。Native 侧：①debugfs `/sys/kernel/debug/block/vdb/{read,write,discard,other}_tokens`（0400；sbitmap 输出含 `depth=`、busy、wait）——**当前深度的直接读数**（kyber-iosched.c:969-975，CONFIG_BLK_DEBUG_FS=y）【源码确证】；②**resize 事件序列**：`bpftrace -e 'tracepoint:kyber:kyber_adjust { printf("%s %s %d\n", ...dev..., args->domain, args->depth) }'`（TP_PROTO=`(dev_t dev, const char *domain, unsigned int depth)`，include/trace/events/kyber.h:48-50，emit 于 kyber_resize_domain:265）【源码确证，推荐】；③sysfs 目标值 `/sys/block/vdb/queue/iosched/{read_lat_nsec,write_lat_nsec}`（877-882）【源码确证】；④kprobe 兜底：`kprobe:kyber_resize_domain`（静态函数，arg1=kqd、arg2=sched_domain 0-3、arg3=新深度；是否内联需 guest `grep kyber_resize_domain /proc/kallsyms` 确认）【推断】。 |
| 实验风险点 | ① 慢相位 1.56–1.60× gap：本轮 depth-limit 时序（IOX `dl[]` 台阶 vs native kyber_adjust 事件）正是定位"窗口动态差 vs 深度差"所需证据；两侧采样频率应一致（IOX 1s 一行、native tracepoint 事件流需 1s 聚合）。② IOX inline 评估会在低完成率的慢相位把 1s 窗拖满（native 定时器去抖不受完成率影响）——深度调整节奏的臂间差可能是**真语义差**（声明偏差 (1) 的实证面），判据不要写成"resize 时刻逐一对齐"。③ limit 喂值 clamp（声明 (2)）：guest `q->async_depth` 小于 256 时 IOX 实际生效深度与 native token 深度不同，读 p99 图注应记 async_depth。 |

## 3. io.max / q_iomax（写带宽时序 vs 32 MiB/s cap 图）

| 项 | 内容 |
|---|---|
| Native 关键机制 | `blk-throttle.c`（rq-qos bio 层）：per-(cgroup,device) `throtl_grp` 四通道 `bps[READ/WRITE]`、`iops[READ/WRITE]`（tg_bps_limit/tg_iops_limit 84-112）；**slice/trim 记账**而非 token bucket：`DFL_THROTL_SLICE=HZ/10`（100ms，:24），`tg_within_bps_limit`（791-826）按 `roundup(jiffy_elapsed, SLICE)` 算 `bytes_allowed`、超了算 `jiffy_wait=extra*HZ/bps`；`tg_dispatch_time`（900-925）= **bps 先过并记账（throtl_charge_bps_bio 828-838）→ 再过 iops**（AND 门）；per-dir 双队列 `queued[READ]/queued[WRITE]`（tg_dispatch_time BUG_ON 钉首 bio 911-912）；`throtl_trim_slice`（658-710）按 2+ slice 时间回收 `bytes_disp/io_disp`；`__tg_update_carryover`（712-753）配置变更时结转；层级 parent/child 递归（tg_update_disptime 968-993、throtl_select_dispatch 1080-1127）；`THROTL_GRP_QUANTUM=8/THROTL_QUANTUM=32`（18-21）。配置面：cgroup v2 `io.max` 文件（throtl_files 1622-1629，tg_set_limit 1540-1620，`rbps/wbps/riops/wiops`，0=U64_MAX=不限）。日志通道：`throtl_log` → blktrace 消息（113-124；trim slice 日志 705-709、carryover 764） |
| IOX 实现要点 | `q_iomax.bpf.c`（冻结）：per-cgroup 四通道 token bucket `q_iomax_bucket`（99-119/128-133，`tokens+carry` per channel、`last_ns`）；方向判定与 native `bio_data_dir` 同构：opcode LSB（172-175，注释 167-171）；`q_iomax_refill`（185-198）：`carry += dt*rate`，整秒入 token，分数 carry 进位（↔ carryover 712-753 的 analogue，注释 177-184）；cap=rate×0.5s（loader 写 `*_cap`，83-92；声明边界 42-47）；AND 门 + bps 先记账（peek 头 bytes 判 bps、iops 判 1，357-362；记账在 pop 后 390-405 ↔ tg_dispatch_time 914-924）；per-dir FIFO 双队列（137-147 ↔ queued[READ/WRITE]），`rr_turn` 方向轮转 work-conserving（372-377）；`allow_merge=false`（449-455，防记账字节漂移）；evacuate DRAINING 门（413-447，防 fallback 绕限速）；时钟倒挂防护 dt=0（317-332 注释：锁序倒转曾造成 2.8× 超发）；守恒计数 `enqueued/dispatches/evac_pops/lost/null_returns/push_fails/read_dispatches/write_dispatches`（99-119）。T1 修复（per-cgroup dispatch 串行化 + busy_bounces）在冻结内核/ loader 侧（HANDOFF §5.5/T1 ledger；本轮未逐行复核 kernel 面，如实标注） |
| 已声明偏差 | (1) **burst 约定**：cap=rate×0.5s（q_iomax.bpf.c:42-47 注释）——rate×0.5s < 单请求字节时会饿死该尺寸；native 是 slice 边界 roundup 预算（一个 slice 即可放行 1 个 IO）。文档要求 rbps/wbps ≥ 2× 最大请求。(2) **无层级**：native io.max 有 parent/child throttle 域递归（968-993、1080-1127），IOX 是扁平 per-cgroup。(3) **记账层**：native 在 bio 层（`throtl_bio_data_size`，128-134）、IOX 在 request 层（`cost*512`，243）——合并在 native 于 throttle 前发生、IOX 则禁合并。(4) evacuate 释放顺序读先写后、非语义（422-428 注释）。 |
| 未声明差异（审查发现） | **审查发现 #3-1（burst 形态可分辨）**：native slice 模型在稳定超速供给下表现为"每 100ms 一个 batch 型台阶"（slice 边界）；IOX token 模型在 carry 进位下表现为连续可微流量 + 0.5s 上限突发。32 MiB/s 与 4k 请求的组合两者都能贴 cap（track_err 数据已证），但**低速率通道（8–16 MiB/s）或大请求尺寸边界附近两臂的秒级 bw 时序形态不同**——单图解读时按"贴 cap 误差带"判，不按时序形态判。**审查发现 #3-2（锁序倒转防护的代价）**：IOX `now<last_ns → dt=0`（317-332）在多 CPU 并发 dispatch 下会丢弃至多一个 tick 的 refill（native spin_lock 保护下无此概念）——幅度 ≤ 一次派发间隔×rate，32 MiB/s 下不可测。**审查发现 #3-3（peek/pop 竞态）**：peek 判定与 pop 实际可能跨并发 popper（378-383 注释），扣费按**实际 popped** 的 bytes（386-404，floor 0）——native 有 BUG_ON 钉死首 bio（911-912），IOX 是 best-effort 一致。 |
| 内部状态观测 | IOX 侧：主指标为外部 bw 时序（fio `*_bw.1.log`），**无内部状态需求**；现有 loader 已有 graceful-stop 终值 dump `q_iomax_dump_stats`（loader_q_iomax.c:155，冻结版，stderr，含 `cgid/cfg=[riops..wbps]/tokens/carry/enqueued/dispatches/evac_pops/null_returns/push_fails/lost/read/write_dispatches`）——诊断用。stats 补丁未加 q_iomax 周期 dump；若要 tokens 时序（可选证据）需另行扩展，本轮不建议。Native 侧：①配置状态即 `io.max` 文件本身（throtl.c:1622-1629；sidecar 已采"行字面"）【源码确证】；②运行时 slice/trim 状态**无 debugfs/sysfs 导出**，可经 blktrace 消息通道（`throtl_log`→`blk_add_cgroup_trace_msg`，113-124；trim 日志 705-709 带 `bytes_trim/io_trim/slice_start/end`）：guest 开 blktrace（`blktrace /dev/vdb`）或 bpftrace kprobe `blk_add_cgroup_trace_msg`【机制源码确证、采集点为推断】；③不采也够：主判据是外部贴 cap 误差带。 |
| 实验风险点 | ① 两臂 burst 形态不同（#3-1）——32 MiB/s 图按秒级误差带判。② IOX evacuate/teardown 与 native blkdiscard 清盘交互（既有 T1 口径）保持不变即可。③ native 臂 io.max 是 rq-qos（bio 层）：LC 并发读流的记账粒度与 IOX request 层不同；LC 是 4k randread、BE 64k seqwrite，误差主要落在 BE 大请求上——贴 cap 判据已覆盖。 |

## 4. io.weight / arb_wdrr（两 cgroup 字节份额图）

| 项 | 内容 |
|---|---|
| Native 关键机制 | cgroup v2 `io.weight` 的**本树实现是 blk-iocost 的 cftype**：`ioc_files[].name="weight"`（blk-iocost.c:3574-3579），写路径 `ioc_weight_write`（3145-3210）：`default V` 写 `iocc->dfl_weight`、`MAJ:MIN V` 写 `iocg->cfg_weight=v*WEIGHT_ONE`（3199-3205），值域 1..10000（cgroup.h:39-41）——**只更新 iocg，无任何 BFQ 传导**（blk-iocost.c 全文无 bfq 引用；blk-cgroup.c 无通用 weight）。且该文件写入要求 iocost rq-qos 在该盘启用（`blkg_conf_prep` → `blkcg_policy_enabled` → 否则 -EOPNOTSUPP，blk-cgroup.c:834-848；`blkcg_activate_policy` 仅发生于 `blk_iocost_init` 2915-2967，即 cost.qos/cost.model enable 路径）。**BFQ 的权重面**：cgroup v2 文件是 `io.bfq.weight`（bfq-cgroup.c:1405-1411 `bfq.weight`；legacy `bfq.weight_device` 1288-1299），值域 `BFQ_MIN/MAX_WEIGHT=1..1000`（bfq-iosched.h:17-19），`bfq_group_set_weight`（1000-1031）写 `entity.new_weight + prio_changed`；BFQ 内部 weight→服务：`BFQ_WEIGHT_CONVERSION_COEFF=10`（bfq-iosched.h:19），vtime/WF2Q 分配（bfq-iosched.c:654 `limit = DIV_ROUND_CLOSEST(limit*entity->weight, wsum)`）。**cgroup v2 下 BFQ 无 per-cgroup 统计文件**（`bfq_blkg_files` 仅 bfq.weight，1405-1411；`bfq.io_service_bytes` 等仅 legacy/v1 1286-1400，且 `CONFIG_BFQ_CGROUP_DEBUG=n`），也无 debugfs（grep 无 queue_debugfs_attrs） |
| IOX 实现要点 | `arb_wdrr.bpf.c`（冻结 205 行）：dispatch 时按**字节**记账的 cgroup 级 DRR——`pick_cgroup` → `arb_drr_pick`（90-118，量子 `ARB_WDRR_QUANTUM_BYTES=512`、下限 `ARB_WDRR_MIN_BUDGET_BYTES=4096`，ioext_arb_common.h:135/268 冻结行）；charge=`nr_sectors*512` 于 `dispatched_request`（139-154，冻结 149-153），requeue 精确退款（181-194）；S12 cgroup 级决策状态 `cgroup_drr` + 全局轮 `global_state`（69-83）；S11.1 需求信号 `cgroup_outstanding`（55-66）；boost 恒 1（`wdrr_get_boost` 85-88）；`IOSCHED_ARB_STRICT`（204）。映射规则与两个 caveat 全部书面化并有模型断言：`ARB_IO_WEIGHT_MAP.md`（weight→round budget=512×w 线性；w<8 floor 压缩；×8 缩放配方；高端 pending-bound 软退化），`tests/io/arb_wdrr_model.py` + `arb_io_weight_model.py` |
| 已声明偏差 | (1) **不复现 BFQ 行为**：只复现"io.weight → 长期字节份额"语义（arb_wdrr.bpf.c:8-13 头注释；HANDOFF §5.6）；BFQ 的同步流 idling、budget、per-process 公平不在范围。(2) w<8 floor 压缩 + ×8 配方（ARB_IO_WEIGHT_MAP.md §3.1）。(3) ≳1000:1 权重比的 pending-bound 软退化（§3.2）。(4) 实验口径：native 臂钉 scheduler=bfq（gen_homo_v2_yamls.py:347-355 注释："native io.weight 的载体是 BFQ…该臂 pins scheduler=bfq 保留 100:200 io.weight knob"）。 |
| 未声明差异（审查发现） | **审查发现 #4-1（native 臂传导路径存疑——本轮最重要的新发现）**：在本内核树里 `io.weight` 是 iocost 的文件且**不触达 BFQ**（见上）；实验框架写的也正是 `io.weight`（src/vm/scene/io_ext/common.py:594、706-734），gen 注释声称"2026-11-07 验证 bfq 下有效"（347-352）与源码矛盾。可能解释：a) 该验证轮实际测到的是 BFQ 默认权重行为或其他效应；b) guest deb 内核与树有差异（未发现对应 patch：kernel/linux-7.3.1 git log 无 weight 相关提交）；c) 写入实际因 iocost 未启用而失败但场景未报错（与 `set -e` 矛盾）。**处置：native io.weight 臂跑主实验前先做载体验证**（写 io.weight=100:200 与 200:100 两档对比 share 是否响应；不响应则改写 `io.bfq.weight` 并同步 expected_state 行字面与 sidecar 门）。**审查发现 #4-2（值域不对称）**：BFQ 权重值域 1..1000，io.weight 1..10000——若改走 io.bfq.weight，200 合法，但 >1000 的探测点不可用。**审查发现 #4-3（BFQ 无 v2 份额统计）**：native 臂的份额证据**只有 fio bw**（无 bfq.io_service_bytes on v2、无 debugfs）——"native bfq weight→份额映射随设备状态漂移 10.7%↔53%"只能外部口径复算。 |
| 内部状态观测 | IOX 侧：主指标为外部字节份额时序（fio `*_bw.1.log`），无内部状态需求；如需 DRR 轨迹可 `bpftool map dump name cgroup_drr` / `global_state`（非 pin）——本轮不需要，stats 补丁未覆盖 arb_wdrr（合理）。Native 侧：①权重配置状态：`io.weight`（或 io.bfq.weight）文件内容【源码确证】；②BFQ 调度内部状态在 v2 **无文件面**（§审查发现 #4-3）【源码确证】；③share 证据只能外部（bw.1.log），并按快/慢相位分桶（既有纪律）。 |
| 实验风险点 | ① #4-1 是本轮 native 臂有效性前提，先验证后跑。② native bfq 份额漂移必须相位分桶（既有纪律），否则会把设备相位误读为权重响应。③ IOX 份额稳定（0.667±0.005/share_dev<4%）与 native 漂移并存时，"一致性"判据应写为份额收敛带 + native 漂移如实呈现，不追求点对点贴合。 |

## 5. io.cost / arb_iocost（vrate 时序图）

| 项 | 内容 |
|---|---|
| Native 关键机制 | `blk-iocost.c`：在线 cost 模型（per-device 标定系数 `i_lcoefs` 379、autocal 默认表 587-634、`io.cost.model` 写 3430+）；qos 参数 `QOS_{RPPM,RLAT,WPPM,WLAT,MIN,MAX}`（332-337，`io.cost.qos` 写 3207-3256 显示面 ioc_qos_prfill 3219-3246）；**周期反馈**：`ioc_timer_fn`（2234-2464）每 period 结算 `ioc_lat_stat`（missed_ppm/rq_wait_pct，2249），busy_level 阶梯（2391-2436：`missed>thr → busy++`；`<25% margin 且有 shortage → busy--`），`ioc_adjust_base_vrate`（2438）调 vrate，clamp `vrate_min/max`（QOS_MIN/MAX→VRATE_MIN/MAX_PPM，943-1018 `ioc_refresh_vrate`）；**vtime 记账**：`current_hweight`（1153）、inuse/surplus 调整（`transfer_surpluses` 1847、`ioc_forgive_debts` 2073）、margin 门限 `MARGIN_{MIN,LOW,TARGET}_PCT=10/20/50`（224-226，`ioc_refresh_margins` 733-741）；wbt 联动、delay 诱导。观测面：tracepoint `iocost_ioc_ioc_vrate_adj`（`TP_PROTO(ioc,new_vrate,missed_ppm,rq_wait_pct,nr_lagging,nr_shortages)`，include/trace/events/iocost.h:142-175，emit 于 blk-iocost.c:982/1014）；`io.stat` 行内 `cost.vrate=%u.%02u`（level 0，3106-3111，ioc enabled 时）+ `blkcg_debug_stats` 追加 `cost.wait/indebt/indelay`（3115-3119；模块参数 blk-cgroup.c:2264）；drgn 监控脚本 tools/cgroup/iocost_monitor.py（头注释 155-170） |
| IOX 实现要点 | `arb_iocost.bpf.c`（冻结 750 行）：冻结 byte-linear cost 表 `cost=bytes/64`（默认表 112-118，`arb_iocost_estimate` 304-337，loader `--cost-table` 可覆盖）；vrate FSM 单实例 `iocost_qos` map（207-234，字段 `window_start_ns/window_sum_ns/window_total/window_over/late_windows/good_windows/vrate_fp/down_steps/up_steps/last_violation/last_comfortable/last_sample_ns`）；DOWN 门=窗口 LC 样本超 rlat 占比 > (100-rpct)%（**精确百分位条件** 515-518）×205/256（173）；UP 门=窗口均值 < rlat/2 ×269/256（519-520、175-176）；2 窗迟滞（177）；clamp fp[64,1024]（171-172）；vrate 应用双通道：全局 `runtime_eff_quantum=ceil(quantum*vrate)` 预计算缓存（351-366，pick 单分支读 628-641）+ LC margin `clamp(ceil(256²/vrate²),1,16)` 写 per-tenant `ts->boost`（588-604，阶梯注释 148-165）；vrate==1.0 位级回退 byte-fair（359-362）；`IOSCHED_ARB_STRICT`（冻结 748） |
| 已声明偏差 | 源内 (a)–(e) 逐条转录（arb_iocost.bpf.c:23-43）：(a) cost 表冻结非在线标定；(b) UP 门用均值（DOWN 用精确百分位）；(c) vrate 作用于 DRR 轮预算发放而非时间周期预算——全局 vrate 分量本身保份额，落点靠 margin；(d) 窗口 100ms/≥20 样本/2 窗迟滞（io.cost 是每 period 自适应 + busy_level 阶梯）；(e) S11 统一 floor 使 BE 只在份额上被压、不绝对饿死（vrate_min 类似物）。另有遥测字段声明（202-206 注释：`bpftool map dump name iocost_qos`）。 |
| 未声明差异（审查发现） | **审查发现 #5-1（窗口竞态欠计数）**：窗口滚动 CAS 与计数器复位之间 XADD 的样本丢失一次（453 注释自述"bounded structural undercount"——源内有注释，属半声明；列为差异是因为它让 `win_total` 序列在 1s 采样下偶发跳变，判读 vrate 台阶时忽略）。**审查发现 #5-2（margin 语义载体）**：native 的"达标者保护"是 vtime debt/inuse 机制（hweight_inuse，2073-2233），IOX 是 boost=margin 乘进 DRR 预算（588-604）——比 (c) 更具体：native 保护对象是**vtime 落后者**（含 BE 落后），IOX 只保护 **LC 角色**（prio≤2）；BE 若在 native 下也进 debt，其 inuse 同样被保护——即 native 的压降落点可能不只压 BE。单图判读 vrate 时序不受影响，判 BE 份额轨迹时需注意。**审查发现 #5-3（冻结基线 arb_mode）**：冻结版 `arb_iocost` 为 `IOSCHED_ARB_STRICT`（748）；脏工作树已改为 `ALLOW_FALLBACK`——**实验必须用冻结版编译物**（stats 补丁基线=冻结树），避免 fallback 语义混入。 |
| 内部状态观测 | IOX 侧：`--stats-interval-ms 1000`（`pol":"arb_iocost"` 行）字段 `vrate_fp/vrate（fp/256 浮点）/eff_quantum/quantum/win_total/win_over/win_sum_ms/late_w/good_w/down_steps/up_steps/last_violation/last_comfortable`——vrate 台阶 + 两个门的状态直接成时序。Native 侧：①**vrate 序列首选 tracepoint**：`bpftrace -e 'tracepoint:iocost:iocost_ioc_vrate_adj { printf("%s %llu->%llu busy=%d missed=%u:%u rqwait=%u\n", str(args->devname), args->old_vrate, args->new_vrate, args->busy_level, args->read_missed_ppm, args->write_missed_ppm, args->rq_wait_pct) }'`（字段见 include/trace/events/iocost.h:142-175）【源码确证，CONFIG_TRACEPOINTS=y】；②**周期采样**：`cat /sys/fs/cgroup/<root路径>/io.stat`（level 0 行含 `cost.vrate=NN.NN`，blk-iocost.c:3106-3111，需 ioc enabled）【源码确证】；③辅助：`/sys/module/blkcg/parameters/blkcg_debug_stats=1`（blk-cgroup.c:2264，built-in 代码的模块参数；不可写时内核 cmdline `blkcg.blkcg_debug_stats=1`）开启 `cost.wait/indebt/indelay`【源码确证（3115-3119）、路径为推断（built-in 参数名空间）】；④qos/model 配置面：`io.cost.qos`/`io.cost.model` 文件内容（sidecar 已采）。 |
| 实验风险点 | ① (c) 偏差的实证面：native vrate 是"时间速率"（每 period 预算），IOX 是"轮预算"——vrate 数值**不可比**（native vrate=时间基准 vs IOX fp/256 相对值），时序图应并排画各自归一化轨迹而非同轴叠画。② native vrate 事件只在调整时 emit（tracepoint），静态期需 io.stat 采样补齐；IOX 每 1s 一行恒发。③ native busy_level 会在"无人被 throttle 且欠饱和"时 coast 到 0（2422-2430），IOX 无此状态——负载阶跃段的行为差异判据按门条件写，不按 busy_level。 |

## 6. io.latency / arb_iolatency（LC P99 + BE 带宽时序图）

| 项 | 内容 |
|---|---|
| Native 关键机制 | `blk-iolatency.c`（rq-qos bio 层，submit 路径阻塞）：per-cgroup `iolat_grp`，`io.latency target=<µs>`（iolatency_set_limit 827-899，显示 `target=%llu` 900-911）；**LC 自节流**：per-cgroup `rq_wait` + `max_depth`（iolat_acquire_inflight 280-284，`rq_qos_wait` 309 阻塞提交者）；窗口评估 `iolatency_check_latencies`（515-581）：窗口 `cur_win_nsec`（min 100ms max 1s，161-162）、门 `latency_sum_ok`（232-241）：**ssd 判 missed < total/10（≥10% 越限才算违例）**，非 ssd 判均值 ≤ target；调整节奏 500ms 最小间隔（512 `BLKIOLATENCY_MIN_ADJUST_TIME`）+ ≥5 好样本（513）；**scale 状态机**：`scale_change`（373-396）down = max_depth 减半（393-394，SCALE_DOWN_FACTOR=2），up = max_depth += qd/16（SCALE_UP_FACTOR=4，315-318）；父级 `scale_cookie` 广播（329-364），子按 cookie 差调整（check_scale_change 399-461，5% 样本豁免 440-443，max_depth==1 时 blkcg_use_delay 447-450）。观测面：`io.stat` 行 ` missed=%llu total=%llu depth=%u`（ssd，921-944）或 ` depth=%u avg_lat=%llu win=%llu`（非 ssd，958-965）——**gated by `blkcg_debug_stats`**（952） |
| IOX 实现要点 | `arb_iolatency.bpf.c`（冻结 455 行）：角色判定 `arb_prio<=2=LC`（76/174-177）；**单一全局 target**（loader `--lat-target-ns`，默认 100us，77/83-95；实验冻结 2ms）；窗口 FSM（共享 `arb_slo_update_window`，ioext_arb_common.h:1885-1978）：100ms 窗 / ≥20 样本 / 越限占比 **>1%** 即 violation（1904-1906）；NORMAL→PRESSURE(boost 4)→持续 >10% 越限 SEVERE(boost 8)→RECOVERY(boost 2)→3 连好窗回 NORMAL（1909-1938，阶梯常量 410-418）；boost 乘进 DRR 有效权重（`iolat_get_boost` 186-199 ↔ pick 201-235），等价于把 BE 压 1/k（头注释 24-39 的 ratio-equivalence 论证）；**hold 硬闸**：LC PRESSURE 且 per-hctx inflight≥`--protect-inflight`（默认 32）时 `hold_dispatch` 返回 true 停派发（236-310，计数 `iolat_hold_cnt`）；BE floor：boost_max clamp（79，默认 4）。`IOSCHED_ARB_STRICT`（冻结 453） |
| 已声明偏差 | (1) 单一全局 target：框架无 per-cgroup 配置通道（头注释 11-15）。(2) **机制分歧（核心）**：native=LC 自节流（收缩自身提交深度，余量自然落 BE）；IOX=LC boost 压 BE 份额（头注释 20-39；HANDOFF §5.8 标注"BE/吞吐侧再分配方向在机制上相反、从未被专门检验"）。 |
| 未声明差异（审查发现） | **审查发现 #6-1（violation 门限不同源）**：IOX 门是 **>1%** 越限（arb_slo_update_window 1905），native ssd 门是 **≥10%**（missed ≥ total/10 才算不 ok，blk-iolatency.c:235-239）——IOX 的 PRESSURE 触发灵敏度高一个数量级（同窗同负载下 IOX 更早进 PRESSURE）。源注释与 HANDOFF 均未记录此差。处置：单图判读 FSM 触发时刻时按各自门限解释；若要门限对齐可加 loader 门限参数（本轮不必）。**审查发现 #6-2（窗口宽度）**：native 窗自适应 100ms→1s（161-162 + `iolatency_set_min_lat_nsec` 内 win 缩放 787-808），IOX 固定 100ms。**审查发现 #6-3（保护动作形态）**：native 在 max_depth 减到 1 后转为 `blkcg_use_delay`（任务级 delay 诱导，447-450），IOX 的对应物是 hold_dispatch 停派发——形态完全不同，但都只在深度路径耗尽后出现。**审查发现 #6-4（作用域）**：native 是 rq-qos（bio submit 层、每 cgroup 独立 rq_wait），作用于**提交者阻塞**；IOX 是 dispatch 仲裁（LC 加权 + hctx 停派发）——LC 提交者不阻塞，LC 吞吐保持（与 HANDOFF 的方向分歧描述一致，此为其机理细化）。 |
| 内部状态观测 | IOX 侧：`--stats-interval-ms 1000`（`pol":"arb_iolatency"` 行）字段 `tenants[{cg,h,boost,slo(0/1/2=NORMAL/PRESSURE/RECOVERY),wt(window_total),wo(over_target),gw(good_windows),dsp,bytes}],hold[](per-hctx hold 计数),inflight[](per-hctx inflight)`——boost/window FSM 轨迹 + hold 事件直接成时序。Native 侧：①**scale 状态（当前 max_depth）**：`blkcg_debug_stats=1` 后 cgroup `io.stat` 行 ` depth=%u`（ssd）/` depth=%u avg_lat= win=`（921-965，952 gate）——侧车 2s 采样即可【源码确证（字段）+ 路径写法推断：/sys/module/blkcg/parameters/blkcg_debug_stats 或 cmdline blkcg.blkcg_debug_stats=1】；②**scale 事件**：`bpftrace -e 'kprobe:scale_cookie_change { @dir[arg2] = count(); }'`（arg1=blkiolat、arg2=lat_info、arg3=up 方向，blk-iolatency.c:329）或 `kprobe:scale_change`（arg2=up）——静态符号需 guest kallsyms 确认存活【推断】；③配置面：`io.latency` 文件 `target=2000`（µs，900-911；sidecar 已采"行字面"）；④missed/total 累计也在同一 io.stat 行内（ssd 路径），可差分出每窗违例率。 |
| 实验风险点 | ① **BE 方向是本轮核心检验**：native 预期 LC iops 下降、BE 吸收余量；IOX 预期 LC 保持、BE 份额下降——若实测 BE 方向一致则机制分歧在可观测层无害，反之必须写进论文口径（HANDOFF §5.8 已列为与 kyber 并列优先）。② #6-1 门限差让两侧 FSM 触发时刻天然错位——LC P99 时序判据按"贴 target"与"状态分布"判，不按触发时刻判。③ native depth 时序依赖 blkcg_debug_stats=1，且 depth 是**提交侧** max_depth；VM virtio 无"ssd 判定"差异——确认 guest 走 ssd 路径（`iolat->ssd` 由设备 rotational 决定，virtio SSD 通常 ssd=1）。 |

## 7. D2FQ / arb_d2fq（累计字节份额图）

| 项 | 内容 |
|---|---|
| Native 关键机制（参考码 + ioext port） | 参考 `skkucsl/d2fq reference tree/block/d2fq.c`：per-flow（per-task，O_D2FQ 选举）`d2fq_data{vt_unit=840/weight(:368), lvt}`；**完成时记账** `__d2fq_update_vt`（421-457）：`tmp=(vt_unit*dlen)>>10`（425，dlen=请求字节），lvt+=tmp 后 `gh_update_min` 更新全局 gvt（411-417 + gholder）；类判定 `__d2fq_start_request`（483-518）：`gap=lvt-gvt`；`md_range=thrsh*(100-md_ratio)/100`；单活动流→MEDIUM；gap>thrsh→LOW；gap>md_range→MEDIUM；否则 HIGH；类 → **NVMe WRRU 硬件 WRR 队列**（`set_weight(q->queue_hw_ctx[1], cls_base)` 102-127，HIGH:LOW=cls_base:1）；deceptive idleness：无 inflight 时 `di_timer` re-arm（447-448，`D2FQ_DI_SLICE*di_period`，di_period=50→0.2s），重激活 lvt:=gvt（assign_dd 400）；**动态 WRR 权重再标定** `dw_en`（142-232，d2fq_timer_fn 扫描 min/max vt 比调 cls_base）。sysctl 9 项（27-39：read/write_weight、thrsh=8192"8MB"、cls_base、md_ratio=70、di_period=50、dw_en/dw_period、…）。ioext port（`kernel/linux-7.3.1/block/d2fq.c` + blk-mq）：9 sysctl 表（598-660）、`/sys/block/<dev>/queue/d2fq_en`（116-150，非 NVMe 拒写）、完成路径记账 blk-mq.c:1365 `__d2fq_update_vt(dd, rq->d2fq_prio_cls, blk_rq_bytes(rq))`、类重映射 rq->d2fq_prio_cls（blk-mq.c:459）、7 条声明偏差在 `include/linux/d2fq.h:11-62`（含参考码 rq->d2fq 未赋值 bug 修复） |
| IOX 实现要点 | `arb_d2fq.bpf.c`（823 行，冻结=工作树）：per-(cgroup,hctx) × read/write 双 lane vtime WFQ：`D2FQ_VT_NUM=840<<10` 精确除法 charge `cost*860160*100/(type_w*weight[*fast_share])`（106、306-326 ↔ 参考截断 vt_unit，声明为折叠改进 100-105）；cost 表（148-155/260-289，默认 arb_cost x64 表，loader --cost-table/--type-weights）；gvt=每 pick 两遍扫描 min eligible vtime（605-689，pass1 631-648）；band 判定 `d2fq_band`（495-504 ↔ __d2fq_start_request，gap<=md_range→HIGH、<=thrsh→MEDIUM、否则 LOW；band 进 pick 排序键与 `band_picks` 遥测，声明 483-494）；LC fast-share=charge/fast_share（默认 6=cls_base enabled 值，44-59/113-117）；DI 复位：insert 时两 lane 均 pending==0 且静默 >di_period(200ms) → 两 lane vtime:=gvt（344-391 ↔ di_timer 重激活复位）；F2 幽灵守卫（no-progress skip 121-125/660-671；dispatch_fail×4 硬复位 751-787）；**动态再标定未实现**（62-64） |
| 已声明偏差 | 源头注释 44-66 逐条：(1) 记账点在派发侧（参考在完成侧——completion 的 cgroup 归属在本内核不可靠，ARB guide pitfall 10）；(2) WRR 类队列 → pick 排序（band 进 sort key，硬件 WRR 不可用）；(3) 动态 WRR 权重再标定未实现（离线策略）；(4) 单活动流 MEDIUM 特例跳过；(5) 精确除法替代截断 vt_unit（100-105）；(6) F2 幽灵守卫/退避窗口为框架适配（80-86）。 |
| 未声明差异（审查发现） | **审查发现 #7-1（thrsh 量纲不同源）**：参考 `sysctl_d2fq_thrsh=8192`（注释"8MB"，与 dlen=字节同量纲的 vtime 累计）；IOX `D2FQ_DEFAULT_THRSH=64*860160≈5.5e7`（cost 单位×VT_NUM，107-110）——两套默认**不可互换**，band 分布（HIGH/MEDIUM/LOW 命中率）在两侧默认下不可直接对读；实验如需 band 证据必须显式 `--thrsh` 校准（参考换算：gap_参考 ≈ (840/w)*bytes/1024）。**审查发现 #7-2（per-lane vtime vs per-flow）**：参考是 per-flow（per-task/per-group_leader）lvt；IOX 是 per-(cgroup,hctx)×方向 lane——fio 单进程多 job 的聚合粒度不同，"公平类"的语义对象不同（声明 (2) 的细化，影响份额-权重映射标定）。**审查发现 #7-3（DI 触发面）**：参考 DI 由 timer 在"无 inflight"后 0.2s 触发、复位发生在**重激活**；IOX 在 **insert 时**判静默超期即刻复位（363-383）——对"空闲→重激活份额恢复时序"的观测面等价，但复位时刻的抖动不同。 |
| 内部状态观测 | IOX 侧：`--stats-interval-ms 1000`（`pol":"arb_d2fq"` 行）字段 `hctx[{i,gvt,picks,bp[3]=band_picks}],tenants[{cg,h,vt[2](lane vtime),pd[2](pending),ch[2](charges),ds[2](dispatches),ins,di(di_resets)}]`——vtime/gvt 轨迹 + band 命中率 + DI 复位事件直接成时序（vtime/band 为可选内部证据，主指标是外部累计字节份额）。Native 侧（**VM 内 N/A**：ioextd2fq 未部署到 guest，且 virtio 无 NVMe WRRU；以下供 bm 裸机补项）：①9 个 `/proc/sys/kernel/d2fq_{read,write}_weight,thrsh,cls_base,md_ratio,di_period,dw_en,dw_period,force_all` + `/sys/block/nvme0n1/queue/d2fq_en`——配置状态【源码确证（ioext d2fq.c:598-660/116-150）+ ioextd2fq 烟测确认】；②lvt/gvt 无 debugfs → bpftrace：`kprobe:__d2fq_update_vt`（EXPORT_SYMBOL，args=(dd, cls, dlen)，ioext blk-mq.c:1365 调用点）逐完成拿 cls/dlen，`kretprobe:__d2fq_start_request` 拿类判定返回值；lvt 数值需按 `struct d2fq_data` 偏移 deref `dd->lvt`（atomic64_t）——**推断**（需先在 guest 里编译/算偏移，风险中等）；③参考码 CONFIG_IOSCHED_D2FQ_VERBOSE 计数器（d2fq.c:427-431 cnt_h/m/l）若 port 未启用则无 printk 面d——port 编译态需查（未找到该 config 在 deb config 中的断言，见 §10）。 |
| 实验风险点 | ① **VM 内 native D2FQ 臂 N/A 是既定决策**（矩阵口径）：ioextd2fq 未部署、virtio 无 WRRU——三臂中 native 柱缺位时，该图只能做 none/IOX 两臂 + 机制叙事。② #7-1 量纲差：份额-权重映射标定（§9.3 待办）必须用 IOX 自己的 cost/权重口径完成，不能借参考码默认。③ 记账点差异（派发 vs 完成）让 IOX 的份额是"授权份额"、参考是"完成份额"——长时间窗下相等、短窗抖动不同。 |

## 8. LinnOS / q_linnos（BE 目标拦截率 vs 实际拦截率图）

| 项 | 内容 |
|---|---|
| Native/论文关键机制 | OSDI'20 论文：NVMe 提交/完成路径内**在线 NN** 预测每请求延迟 + 每设备离线训练；预测超 SLO 的请求按策略比例拦截。**无可信参考实现**（唯一同名 GitHub 仓无法证实出处；论文 target ~4.x 内核）→ native 臂 N/A（前人显式决策维持 front_n_none 口径，D2FQ_NATIVE_PORT_20261110.md"§ LinnOS：native 不可行的原因"） |
| IOX 实现要点 | `q_linnos.bpf.c`（冻结 504 行）：离线查表拦截器——busy 代理=全局 inflight（dispatched−completed，121/288），band 0/1/2 = <50%/50–80%/>80% of `--busy-ref`（76-77/198-210，实验显式 busy-ref=24）；class：arb_prio<`--lc-prio-max`(3)→LC 否则 BE（214-222）；拦截率表 `ir[band][class]`（默认 {0,0}/{0,20}/{5,60}，91-93；实验迭代#2 显式 LC 恒 0、BE 0/40/60）；**精确比例抽取**：per-(band,class) 序号计数器 `(seq%100)<ir`（287-292，声明"exactly ratio-faithful"）；held FIFO（max-hold `--hold` 实验值 500us，默认 2ms，80/103-106/301-317）+ 四步 dispatch（超时 held 先出 371-377 → fast 380-383 → fast 空时 held 软释放 386-389 → NULL 自踢）；DRAINING-gated evacuate（冻结 424-437，仅 DRAINING 后放行；脏工作树另加了 rescue_active——**非冻结**）；遥测 `q_linnos_stats`（108-127） |
| 已声明偏差 | (1) **离线查表近似，非在线 NN**（头注释 4-13 大写声明；RUNBOOK §9.1）——无在线学习、无延迟预测、无特征向量。(2) busy 代理选择：全局 inflight 而非 per-cgroup（16-23 注释：completion cgroup 归属不可靠）；band 边界固定 50/80。(3) LC 恒 0 拦截、BE 分档压缩是实验显式表（迭代#2 口径，RUNBOOK 表 bm）。(4) allow_merge=false（485-491，防记账漂移）。 |
| 未声明差异（审查发现） | **审查发现 #8-1（inflight 起点）**：inflight 从 **dispatch** 起算（399）而非 insert——band 判定的"忙碌"是"已下发的在途数"，比论文的设备级 busy（提交队列深度/NN 特征）滞后且偏小；QD 深、派发被 arbiter 限速时 band 可能长期偏低（RUNBOOK 迭代#2 的 busy-ref 64→24 修正是对这个现象的补偿，属已量化但未机制化）。**审查发现 #8-2（held 释放顺序）**：超时 held 优先于 fast（371-377）——被拦截请求在 hold 到期后**插队**到所有未拦截请求之前；这会让"拦截"对被拦截请求近似无代价、把代价转嫁给 fast 队列（LC）——与论文"拦截=延迟不合规请求"的语义方向一致但代价分配不同；hold=500us 时影响很小。 |
| 内部状态观测 | IOX 侧：`--stats-interval-ms 1000`（`pol":"q_linnos"` 行）字段 `ins/band[3](inserts_by_band)/ir0/ir1/ir2[2](per band per class intercepted)/disp_fast/disp_held_to/disp_held_soft/inflight/completions/lost_fast/lost_held`——**实际拦截率（ir 分子）×band 分布**直接成时序；BE 目标拦截率=loader 显式表（ir 常量）。graceful-stop 终值 dump 已有（loader_q_linnos.c:132 `q_linnos_dump_stats`，冻结版）。Native 侧：N/A（无可信 native；论文对照不属于本轮三臂口径）。 |
| 实验风险点 | ① "BE 目标拦截率 vs 实际拦截率"图的分母：实际率 = intercepted[band][BE]/inserts_by_band[band]（stats 字段齐备）；但 band 由 inflight/busy-ref 决定——BE 目标档位（0/40/60）是否被激活取决于 inflight 是否跨过 50%/80% 线，**压力阶梯场景必须能稳定跨档**（busy-ref=24 已按 QD32+BE 校准）。② LC 恒 0 拦截是表配置而非机制保证——判据写"LC 分子恒 0"而非"LC 永不被拦截"。③ 拦截的"收益"（LC p99 改善）只有外部时序可证（fio lat.log）；loader 计数只证"行为与表一致"（模型断言 q_linnos_model.py 已有）。 |

## 9. q_none / q_fifo（基线与工具臂）

| 臂 | 审查结论 |
|---|---|
| q_none（none 基线臂） | `IOSCHED_BYPASS_MODE_DIRECT` 直通（q_none.bpf.c:56）；insert 在非 DRAINING 时一律返回 -1（拒绝入队 = defer to parent，23-33）——等价 native `none`（无电梯）语义；cgroup_dispatch/evacuate 恒 0（35-49）。三臂中 none 臂是两图（front_n_none、ctrl_n_none）与 LinnOS/D2FQ 的 native 替身；无内部状态。 |
| q_fifo（工具臂） | 单 `BPF_MAP_TYPE_QUEUE`（4096）顺序 push/pop（q_fifo.bpf.c:9-13/29-61），dispatch/evacuate 为通用宏（63-64）。**不是任何 native 调度器的复现**——它是 arbiter 臂中租户的中性队列（把调度决策集中到 arb_* 层）；份额/代价记账由 arbiter 的 dispatched_request 完成，队列本身无内部状态。 |

## 10. 内核配置验证（guest deb：linux-image-7.3.1-ioext3_7.3.1-16_amd64.deb）

`dpkg -c`（2026-11-07 17:04 构建，共 8257 条目）：含 `/boot/vmlinuz-7.3.1-ioext3`、
`/boot/config-7.3.1-ioext3`、完整 `/lib/modules/7.3.1-ioext3/` 树（drivers/kvm/fs 等数千 .ko）。
**block 调度器与 IO 控制器全部内建（=y），deb 中不存在 mq-deadline.ko/kyber.ko/bfq.ko**
（dpkg -c 对 block/ 目录 grep 零命中，与 config 一致）。

| 断言 | config 值（解包 `/boot/config-7.3.1-ioext3`） | 实验意义 |
|---|---|---|
| CONFIG_MQ_IOSCHED_DEADLINE | =y | native mq-deadline 臂可用（guest scheduler 列表已实证含 none/mq-deadline/kyber/bfq/scx_io） |
| CONFIG_MQ_IOSCHED_KYBER | =y | native kyber 臂可用；tracepoint kyber_* 依赖 CONFIG_TRACEPOINTS=y ✓ |
| CONFIG_IOSCHED_BFQ / CONFIG_BFQ_GROUP_IOSCHED | =y / =y | native io.weight/bfq 载体可用；`CONFIG_BFQ_CGROUP_DEBUG` **not set** → v1 bfq.* 统计文件不存在（§4 #4-3 成立） |
| CONFIG_BLK_CGROUP / CGROUPS | =y / =y | cgroup v2 io.* 控制面可用 |
| CONFIG_BLK_DEV_THROTTLING | =y | io.max（blk-throttle）可用 |
| CONFIG_BLK_CGROUP_IOCOST | =y | io.cost（blk-iocost）可用；tracepoint iocost_ioc_vrate_adj 依赖 CONFIG_TRACEPOINTS=y ✓ |
| CONFIG_BLK_CGROUP_IOLATENCY | =y | io.latency（blk-iolatency）可用；blkcg_debug_stats 的 io.stat depth 行可用 |
| CONFIG_BLK_DEBUG_FS | =y | kyber `*_tokens`、mq-deadline `batching/starved/queued` 等 debugfs 观测面可用（需 guest mount debugfs） |
| CONFIG_TRACEPOINTS | =y | kyber_adjust / iocost_vrate_adj tracepoint 面可用 |
| CONFIG_DEBUG_INFO_BTF / _MODULES | =y / =y | /sys/kernel/btf/vmlinux 存在 → guest 内 bpftrace CO-RE 可用 |
| CONFIG_CGROUP_BPF | =y | scx_io/struct_ops 基座（既证） |
| KYBER_IOSCHED 命名注意 | 该树 config 项名是 `CONFIG_MQ_IOSCHED_KYBER`（非 `CONFIG_KYBER_IOSCHED`） | 查 config 时勿用错键名 |
| D2FQ（ioextd2fq 内核） | 本 deb（ioext3）**不含** d2fq（block/d2fq.c 属 ioextd2fq 分支 tag v7.3.1-ioextd2fq，deb linux-image-7.3.1-ioextd2fq_7.3.1-27）；VM native D2FQ 臂 N/A 口径与此一致 | D2FQ native 属 bm 部署后事项 |

## 11. 主会话待办建议（Native 侧观测落地清单）

IOX 侧已由 stats 补丁覆盖（q_kyber/arb_iocost/arb_iolatency/arb_d2fq/q_linnos 五个
loader 的 `--stats-interval-ms`；建议 1000ms，q_linnos 可 500ms 以对齐 band 跳变）。
下面是 **Native 臂侧**需要主会话落到 guest 侧车/run 脚本的清单：

1. **kyber（native）**：a) run 脚本确保 guest 挂载 debugfs，case 期间每 1s
   `cat /sys/kernel/debug/block/vdb/read_tokens` 与 `write_tokens`（只需 `depth=` 行）；
   b) 更优：`bpftrace -e 'tracepoint:kyber:kyber_adjust { printf("%d %s %d\n", args->dev>>20, args->domain, args->depth) }'` 输出到 case 日志（1s 聚合成深度轨迹，事件流含 resize 时刻）。可信度：源码确证。kprobe 兜底先 `grep kyber_resize_domain /proc/kallsyms`。
2. **io.cost（native）**：侧车每 2s 采 root cgroup `io.stat` 中的 `cost.vrate=`（blk-iocost.c:3106-3111）；或常驻 `tracepoint:iocost:iocost_ioc_vrate_adj`（事件含 old/new vrate + missed_ppm）。两条二选一即可，tracepoint 更完整。
3. **io.latency（native）**：a) guest 设 `blkcg_debug_stats=1`（先试
   `echo 1 > /sys/module/blkcg/parameters/blkcg_debug_stats`；built-in 参数不行则内核
   cmdline `blkcg.blkcg_debug_stats=1`——需重启验证一次）；b) 侧车每 2s 采 LC/BE
   cgroup `io.stat` 的 ` missed= total= depth=`（差分出违例率，depth 即 scale 状态）。
4. **io.weight（native 臂有效性验证，优先级最高）**：正式 campaign 前做一次性
   载体验证：scheduler=bfq 下写 `io.weight 100:200` 与 `200:100` 两档，确认 share
   响应且写入不报 -EOPNOTSUPP；若无响应，改写 `io.bfq.weight`（bfq-cgroup.c:1405-1411）
   并同步 expected_state 行字面/sidecar 门（审查发现 #4-1）。
5. **io.max（native）**：无内部状态需求（io.max 文件即状态 + fio bw 时序）；若要
   trim 证据再考虑 blktrace（blk-throttle.c:705），本轮不加。
6. **mq-deadline（native）**：无内部状态需求；如需饥饿保护证据读 debugfs
   `starved`/`queued`（mq-deadline.c:976-981）。
7. **D2FQ/LinnOS（native）**：VM 内 N/A 维持；bm 裸机补项留 §7 的 kprobe 方案
   （`__d2fq_update_vt`/`__d2fq_start_request` + dd->lvt 偏移 deref，先验 kallsyms）。
8. **采样对称性纪律**：native 侧新增的 bpftrace/io.stat 采样必须两侧同开或
   x-only 分离（HANDOFF §8"监控对称"），新采集点先以 tune 轮入账，不混入既有数据口径。

### 附：IOX 侧 stats 字段速查（stats 补丁 JSON，`--stats-interval-ms N`）

| 策略 | 每行字段 |
|---|---|
| q_kyber | `hctx[{idx, dl[4], inf[4], pend[4], cur, bat, p99[3]}]` |
| arb_iocost | `vrate_fp, vrate, eff_quantum, quantum, win_total, win_over, win_sum_ms, late_w, good_w, down_steps, up_steps, last_violation, last_comfortable` |
| arb_iolatency | `tenants[{cg,h,boost,slo,wt,wo,gw,dsp,bytes}], hold[], inflight[]` |
| arb_d2fq | `hctx[{i,gvt,picks,bp[3]}], tenants[{cg,h,vt[2],pd[2],ch[2],ds[2],ins,di}]` |
| q_linnos | `ins, band[3], ir0/ir1/ir2[2], disp_fast, disp_held_to, disp_held_soft, inflight, completions, lost_fast, lost_held` |
| q_mq_deadline / q_iomax / arb_wdrr / q_fifo / q_none | 未加周期 dump（本轮无需求；q_iomax 与 q_linnos 另有 graceful-stop 终值 dump：loader_q_iomax.c:155 / loader_q_linnos.c:132，冻结版） |
