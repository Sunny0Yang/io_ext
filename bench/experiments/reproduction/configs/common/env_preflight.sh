# =========================================================================
# experiments/reproduction / common / env_preflight.sh
# 共享环境断言（guest VM 内执行，root）。
# 设计产物：只读引用，不自动执行（见 EXPERIMENT_MATRIX.md）。
#
# 环境口径（主会话补定 [MSC]）：
#   bm 上 VM（6 vCPU / 6 GiB，virtio-blk cache=none）
#   guest 内核 7.3.1-ioext3；数据盘 /dev/vdb ext4 挂 /data
#   /data/lc.bin /data/be.bin 两个预分配 32G 文件；fio 一律 direct=1
# =========================================================================

# ---- 常量（每个 arm 脚本 source 后即可用） ------------------------------
export IOEXT_DEV="${IOEXT_DEV:-vdb}"
export IOEXT_SCHED="/sys/block/${IOEXT_DEV}/queue/scheduler"
export IOEXT_NR_REQ="/sys/block/${IOEXT_DEV}/queue/nr_requests"
export IOEXT_LC_FILE="${IOEXT_LC_FILE:-/data/lc.bin}"
export IOEXT_BE_FILE="${IOEXT_BE_FILE:-/data/be.bin}"
export IOEXT_CG_ROOT="${IOEXT_CG_ROOT:-/sys/fs/cgroup}"
export IOEXT_LC_CG="${IOEXT_CG_ROOT}/iox_lc"
export IOEXT_BE_CG="${IOEXT_CG_ROOT}/iox_be"
export IOEXT_MAIN_CG="${IOEXT_CG_ROOT}/iox_main"
export IOEXT_LOADER_DIR="${IOEXT_LOADER_DIR:-/opt/io-ext/userspace}"
export IOEXT_RUN_ROOT="${IOEXT_RUN_ROOT:-/dev/shm/ioext_repro}"
export IOEXT_STATS_INTERVAL_MS="${IOEXT_STATS_INTERVAL_MS:-1000}"   # [PLAN] 主会话补定：1s
# --stats-interval-ms 落地状态（[MSC] 2026-11-14 已落地）：loaders_stats1 bundle
# 仅 5 个 loader 支持该 flag（q_kyber/q_linnos/arb_iocost/arb_iolatency/arb_d2fq，
# 证据：loaders_stats1/BUILD_PROVENANCE.txt + loader_stats_interval.patch；其余
# q_fifo/q_none/q_mq_deadline/q_iomax/arb_wdrr 为冻结副本、二进制内无该选项）。
# 各臂脚本必须用 loader_supports_stats 逐 loader 探测，不得按实验笼统传参。
export IOEXT_STATS_LANDED="${IOEXT_STATS_LANDED:-1}"
export IOEXT_LOCK="/tmp/ioext_repro.lock"

# workload 冻结随机种子（[GAP-C] C 补定：phase 级固定，保证 workload 恒等）
export IOEXT_SEED_LC_P1=2026111401
export IOEXT_SEED_LC_P2=2026111402
export IOEXT_SEED_LC_P3=2026111403
export IOEXT_SEED_LC=2026111400
export IOEXT_SEED_BE=2026111499
export IOEXT_SEED_BE_S1=2026111408
export IOEXT_SEED_BE_S2=2026111416
export IOEXT_SEED_BE_S3=2026111424
export IOEXT_SEED_BE_S4=2026111432

# io.max 限速（[PLAN] 计划固定：32 MiB/s）
export IOEXT_WBPS_CAP=33554432

env_log()  { printf '[env] %s\n' "$*" >&2; }
env_die()  { printf '[env][FAIL] %s\n' "$*" >&2; exit 2; }
assert_f() { # assert_f <描述> <命令...>
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
        env_log "PASS: $desc"
    else
        env_die "$desc"
    fi
}

# ---- 环境总断言（每个臂 preflight 第一步调用） --------------------------
env_preflight() {
    # 1) 实验互斥锁（无残留锁：上一个臂未释放锁则 fail-stop）
    exec 9>>"$IOEXT_LOCK"
    flock -n 9 || env_die "experiment lock held: $IOEXT_LOCK (stale arm still running?)"

    # 2) 内核
    local kr; kr="$(uname -r)"
    case "$kr" in
        7.3.1-ioext*) env_log "PASS: kernel=$kr" ;;
        *) env_die "kernel=$kr 不是 7.3.1-ioext*（[MSC] 口径要求 guest 内核 7.3.1-ioext3）" ;;
    esac
    grep -q scx_io /proc/kallsyms \
        || env_die "guest kernel 无 scx_io（/proc/kallsyms）—— ioext 框架未编入"  # 同 guest_hotswitch.sh:58

    # 3) 数据盘与数据文件
    assert_f "数据盘 /dev/$IOEXT_DEV 存在" test -b "/dev/$IOEXT_DEV"
    assert_f "scheduler 文件存在" test -f "$IOEXT_SCHED"
    assert_f "LC 数据文件 $IOEXT_LC_FILE 存在" test -f "$IOEXT_LC_FILE"
    assert_f "BE 数据文件 $IOEXT_BE_FILE 存在" test -f "$IOEXT_BE_FILE"
    local lc_mb be_mb
    lc_mb=$(($(stat -c %s "$IOEXT_LC_FILE") / 1024 / 1024))
    be_mb=$(($(stat -c %s "$IOEXT_BE_FILE") / 1024 / 1024))
    [ "$lc_mb" -ge 30720 ] || env_die "$IOEXT_LC_FILE 尺寸 ${lc_mb}MiB < 30GiB"
    [ "$be_mb" -ge 30720 ] || env_die "$IOEXT_BE_FILE 尺寸 ${be_mb}MiB < 30GiB"

    # 4) cgroup v2 + io controller
    assert_f "cgroup v2 挂载" mountpoint -q "$IOEXT_CG_ROOT"
    printf '%s\n' '+io' > "$IOEXT_CG_ROOT/cgroup.subtree_control" 2>/dev/null || true
    grep -qw io "$IOEXT_CG_ROOT/cgroup.subtree_control" \
        || env_die "cgroup.subtree_control 未启用 +io"

    # 5) 工具
    assert_f "fio 在 PATH" command -v fio
    command -v bpftool >/dev/null 2>&1 \
        || env_log "WARN: bpftool 不在 PATH（本套配置的快照/断言不依赖它，仅热替换类诊断需要）"

    # 6) loader bundle
    local l
    for l in loader_q_none loader_q_fifo loader_q_mq_deadline loader_q_kyber \
             loader_q_iomax loader_q_linnos loader_arb_wdrr loader_arb_iocost \
             loader_arb_iolatency loader_arb_d2fq; do
        assert_f "loader $l 存在且可执行" test -x "$IOEXT_LOADER_DIR/$l"
    done

    # 7) 无残留进程（fio/loader）
    local stale
    stale="$(pgrep -af 'loader_q_|loader_arb_|(^|/)fio( |$)' || true)"
    [ -z "$stale" ] || env_die "残留进程: $stale"

    # 8) bpffs 与残留 pin
    assert_f "bpffs 挂载" mountpoint -q /sys/fs/bpf
    rm -f /sys/fs/bpf/ioext_policy_meta /sys/fs/bpf/ioext_active_cgroups 2>/dev/null || true

    # 9) 根 cgroup 无激活的 io.cost（上次 io_cost 实验残留会让 weight/model 写行为漂移）
    # 注意 [FIX-C]：iocost 从未在该盘初始化时 io.cost.qos 为空文件（CFTYPE_ONLY_ON_ROOT
    # 的 cftypes 在策略注册即存在，但 ioc_qos_show 无 ioc 时不出行）——空文件=无残留。
    if [ -f "$IOEXT_CG_ROOT/io.cost.qos" ] && [ -s "$IOEXT_CG_ROOT/io.cost.qos" ]; then
        grep -q "enable=0" "$IOEXT_CG_ROOT/io.cost.qos" \
            || env_die "根 io.cost.qos 处于 enable=1（残留）——先跑 io_cost 臂的 postflight 复位"
    fi

    # 10) 数据盘 scheduler 必须从 none 起步（由各臂自行切到目标态）
    local cur; cur="$(sched_current)"
    [ "$cur" = "none" ] || env_die "数据盘 scheduler=$cur（期望 none 起步）"

    env_log "env_preflight OK (dev=$IOEXT_DEV MAJ:MIN=$(ioext_maj_min))"
}

# ---- 工具函数 ------------------------------------------------------------
ioext_maj_min() { lsblk -n -o MAJ:MIN "/dev/$IOEXT_DEV" | tr -d ' '; }

# 逐 loader 探测 --stats-interval-ms 支持（二进制内是否含选项帮助串）。
# 支持者（loaders_stats1）：loader_q_kyber / loader_q_linnos / loader_arb_iocost /
# loader_arb_iolatency / loader_arb_d2fq；不支持者：loader_q_fifo / loader_q_none /
# loader_q_mq_deadline / loader_q_iomax / loader_arb_wdrr（冻结副本，传参即退出）。
loader_supports_stats() { # loader_supports_stats <binary_path>
    [ "${IOEXT_STATS_LANDED:-1}" = "1" ] || return 1
    strings "$1" 2>/dev/null | grep -q "stats-interval-ms"
}

sched_current() {
    sed -n 's/.*\[\([^]]*\)\].*/\1/p' "$IOEXT_SCHED"   # 同 guest_hotswitch.sh:119
}

# scheduler 切换 + 读回断言（native/none/iox 基座共用）
# 依据：guest_hotswitch.sh:115-120（echo scx_io 后读回 [scx_io]）
sched_set() {
    local want="$1"
    grep -qw "$want" "$IOEXT_SCHED" || env_die "scheduler $want 不在可用列表: $(cat "$IOEXT_SCHED")"
    echo "$want" > "$IOEXT_SCHED" || env_die "echo $want > scheduler 失败"
    local cur; cur="$(sched_current)"
    [ "$cur" = "$want" ] || env_die "scheduler 读回=$cur 期望 $want"
    echo 1023 > "$IOEXT_NR_REQ" 2>/dev/null || true   # best-effort，同 guest_hotswitch.sh:121
    env_log "scheduler -> $want (nr_requests=$(cat "$IOEXT_NR_REQ" 2>/dev/null || echo '?'))"
}

# cgroup 准备（mkdir + 该目录拿到 io.* 文件）
cg_prep() {
    local p="$1"
    mkdir -p "$p"
    grep -qw io "$IOEXT_CG_ROOT/cgroup.subtree_control" || env_die "+io 未启用"
    test -f "$p/cgroup.procs" || env_die "cgroup $p 缺 cgroup.procs（io controller 未到达该层）"
    env_log "cgroup ready: $p"
}

cg_rm() {
    local p="$1"
    if [ -d "$p" ]; then
        # 确认无进程残留后删除（进程迁回根 = 限速/权重清除）
        local n; n="$(cat "$p/cgroup.procs" 2>/dev/null | wc -l || echo 0)"
        [ "$n" = "0" ] || env_die "cgroup $p 仍有 $n 个进程，拒绝删除"
        rmdir "$p"
    fi
}

# 在 cgroup 内启动 fio（后台），echo pid
# 依据：guest_hotswitch.sh:283-284（echo $BASHPID > cgroup.procs; exec fio）
fio_in_cg() { # fio_in_cg <cgroup> <jobfile> <rundir> <outbase>
    local cg="$1" job="$2" rd="$3" base="$4"
    (
        echo "$BASHPID" > "$cg/cgroup.procs"
        exec fio "$job" --output-format=json --output="$rd/${base}.fio.json"
    ) >/dev/null 2>&1 &
    FIO_PID=$!
    env_log "fio started: job=$job cg=$cg pid=$FIO_PID out=$rd/${base}.fio.json"
}

# loader 启动（stdout+stderr 全量追加到 <run_id>.stats.log；主会话口径）
# stats 开关：--stats-interval-ms（主会话计划新增；当前 ioext_cli_args.h 尚无此
# 选项 —— 落地前从调用方的 STATS_ARGS 中删除该 flag，见各 arms_*.sh）。
loader_start() { # loader_start <binary> <stats_log> <args...>
    local bin="$1" slog="$2"; shift 2
    touch "$slog"
    "$bin" "$@" >>"$slog" 2>&1 &
    LOADER_PID=$!
    env_log "loader started: $(basename "$bin") pid=$LOADER_PID log=$slog"
}

# loader 存活确认（2s 内不退出 + dmesg 出现注册行 best-effort，同 hotsplit:102-113）
loader_wait_alive() {
    local pid="$1" name="$2" i
    sleep 2
    kill -0 "$pid" 2>/dev/null || env_die "loader $name 启动后退出（见 stats.log）"
    for i in $(seq 1 10); do
        dmesg 2>/dev/null | grep -qiE "policy |arbiter " && break
        sleep 1
    done
    env_log "loader alive: $name pid=$pid"
}

loader_stop() { # loader_stop <pid>...
    local pid
    for pid in "$@"; do
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && kill -TERM "$pid" 2>/dev/null || true
    done
    for pid in "$@"; do
        [ -n "$pid" ] && wait "$pid" 2>/dev/null || true
    done
    sleep 5   # drain（hotsplit 惯例 guest_hotswitch.sh:392-396 的缩短版）
}

# 状态快照（每臂 preflight/postflight 各一份；主会话口径）
snapshot_state() { # snapshot_state <outfile> <tag>
    local out="$1" tag="$2"
    {
        echo "### snapshot tag=$tag ts=$(date -Iseconds)"
        echo "-- uname"; uname -r
        echo "-- scheduler"; cat "$IOEXT_SCHED"
        echo "-- nr_requests"; cat "$IOEXT_NR_REQ" 2>/dev/null || true
        echo "-- loaders/fio procs"; pgrep -af 'loader_q_|loader_arb_|(^|/)fio( |$)' || echo '(none)'
        echo "-- root io.cost.qos"; cat "$IOEXT_CG_ROOT/io.cost.qos" 2>/dev/null || echo '(absent)'
        echo "-- root io.cost.model"; cat "$IOEXT_CG_ROOT/io.cost.model" 2>/dev/null || echo '(absent)'
        echo "-- root io.stat(vrate)"; grep -o 'cost\.vrate=[0-9.]*' "$IOEXT_CG_ROOT/io.stat" 2>/dev/null || echo '(absent)'
        local cg
        for cg in "$IOEXT_LC_CG" "$IOEXT_BE_CG" "$IOEXT_MAIN_CG"; do
            [ -d "$cg" ] || continue
            echo "-- cgroup $cg io.max";    cat "$cg/io.max"    2>/dev/null || true
            echo "-- cgroup $cg io.weight"; cat "$cg/io.weight" 2>/dev/null || true
            echo "-- cgroup $cg io.bfq.weight"; cat "$cg/io.bfq.weight" 2>/dev/null || true
            echo "-- cgroup $cg io.latency"; cat "$cg/io.latency" 2>/dev/null || true
            echo "-- cgroup $cg io.stat";   cat "$cg/io.stat"   2>/dev/null || true
        done
        echo "-- bpffs pins"; ls /sys/fs/bpf/ 2>/dev/null | grep -i ioext || echo '(none)'
        echo "-- dmesg tail"; dmesg 2>/dev/null | tail -20 || true
    } > "$out" 2>&1
    env_log "snapshot -> $out"
}

# 哨兵（arm 收尾后 3 连发；主会话口径：两两臂之间 3x10s QD1）
# 输出：stdout = 本次哨兵 read iops；文件：$RUN_DIR/sentinel_<tag>.fio.json
sentinel_run() { # sentinel_run <rundir> <tag>
    local rd="$1" tag="$2" fio_json iops
    HERE="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
    fio "$HERE/../common/sentinel.fio" --output-format=json \
        --output="$rd/sentinel_${tag}.fio.json" >/dev/null 2>&1
    iops=$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print(round(d["jobs"][0]["read"]["iops"],1))' "$rd/sentinel_${tag}.fio.json" 2>/dev/null || echo -1)
    env_log "sentinel[$tag] read_iops=$iops"
    echo "$iops"
}

# 哨兵基准生成（smoke 步调用一次；产出 sentinel_drift 的比较基准）
sentinel_make_ref() { # sentinel_make_ref <out_json>
    local out="$1" rd iops
    rd="$(mktemp -d)"
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    fio "$HERE/sentinel.fio" --output-format=json \
        --output="$rd/sentinel_ref_raw.json" >/dev/null 2>&1
    iops=$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print(round(d["jobs"][0]["read"]["iops"],1))' "$rd/sentinel_ref_raw.json" 2>/dev/null || echo -1)
    rm -rf "$rd"
    python3 -c "
import json; json.dump({'read_iops': $iops, 'source': 'sentinel.fio smoke'}, open('$out','w'))"
    IOEXT_SENTINEL_REF="$out"
    env_log "sentinel reference: $out (read_iops=$iops)"
}

# 哨兵漂移（与 campaign 基准比；>20% 打 WARN 不淘汰 —— 主会话口径）
sentinel_drift() { # sentinel_drift <iops>
    local iops="$1" ref="${IOEXT_SENTINEL_REF:-$IOEXT_RUN_ROOT/_sentinel_ref.json}"
    [ -f "$ref" ] || { env_log "sentinel ref 缺失，drift=NA"; echo "NA"; return; }
    local ref_iops
    ref_iops=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["read_iops"])' "$ref" 2>/dev/null || echo 0)
    [ "$ref_iops" -gt 0 ] 2>/dev/null || { echo "NA"; return; }
    python3 -c "
print(round(($iops-$ref_iops)/$ref_iops*100,1))"
}

# 外部状态 1s 采样（native 臂的内部状态替代观测；none 臂不用）
# io_cost native：io.stat cost.vrate（blk-iocost.c:3110）；weight native：io.stat
# bfq.* 字节；io_max/io_latency native：cgroup 行读回。
external_sampler() { # external_sampler <outfile> <duration_sec>
    local out="$1" dur="$2" end=$(( $(date +%s) + dur ))
    (
        while [ "$(date +%s)" -lt "$end" ]; do
            printf '{"t":"%s","sched":"%s","vrate":"%s","lc_stat":"%s","be_stat":"%s"}\n' \
                "$(date -Iseconds)" "$(sched_current)" \
                "$(grep -o 'cost\.vrate=[0-9.]*' "$IOEXT_CG_ROOT/io.stat" 2>/dev/null | head -1)" \
                "$(cat "$IOEXT_LC_CG/io.stat" 2>/dev/null | head -2 | tr '\n' ' ')" \
                "$(cat "$IOEXT_BE_CG/io.stat" 2>/dev/null | head -2 | tr '\n' ' ')"
            sleep 1
        done
    ) > "$out" 2>&1 &
    SAMPLER_PID=$!
    env_log "external sampler started pid=$SAMPLER_PID -> $out"
}

sampler_stop() {
    [ -n "${SAMPLER_PID:-}" ] && kill "$SAMPLER_PID" 2>/dev/null || true
    wait "${SAMPLER_PID:-0}" 2>/dev/null || true
}

# 臂收尾：停 loader / 哨兵 3 连发 / 复位 scheduler / 摘 cgroup / 清 pin / 快照
teardown_common() { # teardown_common <rundir> <run_id> <有loader:1/0> <stats_log或->
    local rd="$1" run_id="$2" has_loader="$3" slog="$4"
    if [ "$has_loader" = "1" ]; then
        loader_stop "${LOADER_PIDS[@]:-}"
        rm -f /sys/fs/bpf/ioext_policy_meta /sys/fs/bpf/ioext_active_cgroups 2>/dev/null || true
    fi
    # 哨兵 3 连发（臂间插桩；sentinel_after 取第 1 发，下一臂 sentinel_before 取第 3 发）
    local s1 s2 s3 d1 d2 d3
    s1=$(sentinel_run "$rd" "${run_id}_post1"); s2=$(sentinel_run "$rd" "${run_id}_post2"); s3=$(sentinel_run "$rd" "${run_id}_post3")
    d1=$(sentinel_drift "$s1"); d2=$(sentinel_drift "$s2"); d3=$(sentinel_drift "$s3")
    sched_set none
    cg_rm "$IOEXT_LC_CG" || true; cg_rm "$IOEXT_BE_CG" || true; cg_rm "$IOEXT_MAIN_CG" || true
    {
        echo "sentinel_after_iops=$s1 drift=${d1}%"
        echo "sentinel_post2_iops=$s2 drift=${d2}%"
        echo "sentinel_post3_iops=$s3 drift=${d3}%  (下一臂的 sentinel_before 口径)"
        [ "$(python3 -c "print(1 if abs(${d1:-100})>20 else 0)" 2>/dev/null || echo 0)" = "1" ] \
            && echo "PHASE_ANOMALY: sentinel drift ${d1}% > 20%（记录不淘汰，主会话口径）" || true
    } > "$rd/${run_id}.sentinel.txt" 2>&1
    snapshot_state "$rd/${run_id}.postflight.txt" "postflight"
    env_log "teardown done: $run_id"
}
