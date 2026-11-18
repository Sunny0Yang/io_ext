#!/bin/bash
# VM 内运行：YCSB 压测全部 io-ext q_* 策略（无 arbiter，root 全局挂载）
#
# 阶段 1 stable  — 每个策略独立 load+run，验证稳定处理请求
# 阶段 2 hot-switch — 高负载 YCSB run 期间热切换全部策略
#
# 用法:
#   ./tests/io/run_all_policies_ycsb.sh                 # 两阶段都跑
#   ./tests/io/run_all_policies_ycsb.sh --stable-only
#   ./tests/io/run_all_policies_ycsb.sh --hot-switch-only
#   ./tests/io/run_all_policies_ycsb.sh --policy q_fifo
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"
# shellcheck source=metrics.sh
source "$SCRIPT_DIR/metrics.sh"

RUN_STABLE=1
RUN_HOT=1
ONLY_POLICY=""
HOT_INTO_Q_NONE=""
HOT_FROM_Q_NONE=""

usage() {
	cat <<'EOF'
Usage: run_all_policies_ycsb.sh [options]

Options:
  --stable-only       仅跑 per-policy 稳定性阶段
  --hot-switch-only   仅跑高负载热切换阶段
  --policy NAME       只测试单个 q_* 策略（仅 stable 阶段）
  --hot-into-q-none FROM  负载下从 FROM 热切换到 q_none
  --hot-from-q-none TO    负载下从 q_none 热切换到 TO
  -h, --help          显示帮助

环境变量（压力参数，默认对齐 ioext-bench paper 单租户配置）:
  RECORDCOUNT=100000 OPERATIONCOUNT=100000 THREADS=4
  HOT_OPERATIONCOUNT=800000 HOT_WARMUP_SEC=30 HOT_PER_POLICY_SEC=25
  YCSB_DIR=/path/to/ycsb_runtime
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
	--stable-only)
		RUN_STABLE=1
		RUN_HOT=0
		shift
		;;
	--hot-switch-only)
		RUN_STABLE=0
		RUN_HOT=1
		shift
		;;
	--policy)
		ONLY_POLICY="${2:-}"
		[ -n "$ONLY_POLICY" ] || fail "--policy requires argument"
		RUN_HOT=0
		shift 2
		;;
	--hot-into-q-none)
		HOT_INTO_Q_NONE="${2:-}"
		[ -n "$HOT_INTO_Q_NONE" ] || fail "--hot-into-q-none requires argument"
		RUN_STABLE=0
		RUN_HOT=0
		shift 2
		;;
	--hot-from-q-none)
		HOT_FROM_Q_NONE="${2:-}"
		[ -n "$HOT_FROM_Q_NONE" ] || fail "--hot-from-q-none requires argument"
		RUN_STABLE=0
		RUN_HOT=0
		shift 2
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		fail "unknown argument: $1"
		;;
	esac
done

if [ -n "$ONLY_POLICY" ]; then
	IO_TEST_POLICIES=("$ONLY_POLICY")
fi

run_stable_policy() {
	local policy="$1"
	local ycsb_dir="$2"
	local tag="stable_${policy}"
	local run_out="$RESULT_ROOT/${tag}_run.txt"
	local loader_pid tp

	log "=== STABLE: ${policy} ==="
	cleanup_ioext
	setup_cgroup_io
	set_scx_scheduler

	start_policy_loader "$policy"
	loader_pid=$LAST_BG_PID
	if ! wait_policy_attached "$policy" "$loader_pid"; then
		stop_policy_loader "$policy" "$loader_pid"
		fail "${policy}: loader failed to attach"
	fi

	prepare_ycsb_dataset "$ycsb_dir" "$tag"

	start_ycsb "$ycsb_dir" run "$run_out" "$OPERATIONCOUNT"
	ycsb_pid=$LAST_BG_PID
	monitor_ycsb_progress "$ycsb_pid" "$run_out" "$STABLE_MIN_SEC" "$STABLE_MAX_SEC"

	stop_policy_loader "$policy" "$loader_pid"

	if ycsb_has_failure "$run_out"; then
		cat "$run_out" >&2
		fail "${policy}: YCSB run reported errors"
	fi

	tp="$(parse_ycsb_throughput "$run_out")"
	if [ -z "$tp" ] || awk "BEGIN{exit !($tp <= 0)}"; then
		cat "$run_out" >&2
		record_stable_metrics "$RESULT_ROOT" ioext scx_io "$policy" \
			"$RESULT_ROOT/${tag}_load.txt" "$run_out"
		fail "${policy}: invalid throughput '$tp'"
	fi

	record_stable_metrics "$RESULT_ROOT" ioext scx_io "$policy" \
		"$RESULT_ROOT/${tag}_load.txt" "$run_out"
	log "PASS stable ${policy}: throughput=${tp} ops/s"
}

run_hot_switch() {
	local ycsb_dir="$1"
	local policy loader_pid ycsb_pid run_out
	local -a switch_policies=("${IO_TEST_POLICIES[@]}")

	log "=== HOT-SWITCH: ${#switch_policies[@]} policies under YCSB load ==="
	cleanup_ioext
	setup_cgroup_io
	set_scx_scheduler

	# 初始策略
	policy="${switch_policies[0]}"
	start_policy_loader "$policy"
	loader_pid=$LAST_BG_PID
	wait_policy_attached "$policy" "$loader_pid" || fail "hot-switch: initial loader ${policy} failed"

	prepare_ycsb_dataset "$ycsb_dir" "hot_switch"
	run_out="$RESULT_ROOT/hot_switch_run.txt"

	start_ycsb "$ycsb_dir" run "$run_out" "$HOT_OPERATIONCOUNT"
	ycsb_pid=$LAST_BG_PID

	log "YCSB background run pid=$ycsb_pid, warmup ${HOT_WARMUP_SEC}s on ${policy}"
	sleep "$HOT_WARMUP_SEC"
	if ! kill -0 "$ycsb_pid" 2>/dev/null; then
		cat "$run_out" >&2
		fail "YCSB exited during warmup"
	fi

	local idx
	for ((idx = 1; idx < ${#switch_policies[@]}; idx++)); do
		local next="${switch_policies[$idx]}"
		log "hot-switch: ${policy} -> ${next}"
		stop_policy_loader "$policy" "$loader_pid"
		sleep 1
		policy="$next"
		start_policy_loader "$policy"
		loader_pid=$LAST_BG_PID
		wait_policy_attached "$policy" "$loader_pid" || fail "hot-switch: ${policy} attach failed"

		local waited=0
		while [ "$waited" -lt "$HOT_PER_POLICY_SEC" ]; do
			if ! kill -0 "$ycsb_pid" 2>/dev/null; then
				break
			fi
			if ! check_kernel_errors; then
				fail "kernel error during hot-switch on ${policy}"
			fi
			sleep "$HEALTH_POLL_SEC"
			waited=$((waited + HEALTH_POLL_SEC))
		done
	done

	# 收尾：再等一段时间确认仍存活
	local tail=0
	while [ "$tail" -lt "$HOT_PER_POLICY_SEC" ]; do
		kill -0 "$ycsb_pid" 2>/dev/null || break
		if ! check_kernel_errors; then
			fail "kernel error in hot-switch tail"
		fi
		sleep "$HEALTH_POLL_SEC"
		tail=$((tail + HEALTH_POLL_SEC))
	done

	if kill -0 "$ycsb_pid" 2>/dev/null; then
		wait "$ycsb_pid" || true
	fi

	stop_policy_loader "$policy" "$loader_pid"

	if ycsb_has_failure "$run_out"; then
		cat "$run_out" >&2
		fail "hot-switch: YCSB run reported errors"
	fi

	local tp
	tp="$(parse_ycsb_throughput "$run_out")"
	if [ -z "$tp" ] || awk "BEGIN{exit !($tp <= 0)}"; then
		cat "$run_out" >&2
		fail "hot-switch: invalid throughput '$tp'"
	fi

	log "PASS hot-switch: final throughput=${tp} ops/s"
	record_hot_switch_metrics "$RESULT_ROOT" full "$run_out" \
		"scheduler_kind=ioext|scheduler=scx_io|switch_path=full_chain"
}

run_hot_switch_pair() {
	local ycsb_dir="$1"
	local from="$2"
	local to="$3"
	local tag="$4"
	local policy="$from" loader_pid ycsb_pid run_out tp

	[ "$from" != "$to" ] || fail "hot-switch pair: from and to must differ"
	log "=== HOT-SWITCH PAIR (${tag}): ${from} -> ${to} ==="
	cleanup_ioext
	setup_cgroup_io
	set_scx_scheduler

	start_policy_loader "$from"
	loader_pid=$LAST_BG_PID
	wait_policy_attached "$from" "$loader_pid" \
		|| fail "hot-switch pair: initial ${from} failed"

	prepare_ycsb_dataset "$ycsb_dir" "$tag"
	run_out="$RESULT_ROOT/${tag}_run.txt"

	start_ycsb "$ycsb_dir" run "$run_out" "$HOT_OPERATIONCOUNT"
	ycsb_pid=$LAST_BG_PID

	log "YCSB background pid=$ycsb_pid, warmup ${HOT_WARMUP_SEC}s on ${from}"
	sleep "$HOT_WARMUP_SEC"
	kill -0 "$ycsb_pid" 2>/dev/null || {
		cat "$run_out" >&2
		fail "YCSB exited during warmup (${from})"
	}

	log "hot-switch pair: ${from} -> ${to}"
	stop_policy_loader "$from" "$loader_pid"
	sleep 2
	policy="$to"
	start_policy_loader "$to"
	loader_pid=$LAST_BG_PID
	wait_policy_attached "$to" "$loader_pid" \
		|| fail "hot-switch pair: attach ${to} failed"

	local waited=0
	while [ "$waited" -lt "$HOT_PER_POLICY_SEC" ]; do
		kill -0 "$ycsb_pid" 2>/dev/null || break
		if ! check_kernel_errors; then
			fail "kernel error during hot-switch pair on ${to}"
		fi
		sleep "$HEALTH_POLL_SEC"
		waited=$((waited + HEALTH_POLL_SEC))
	done

	local tail=0
	while [ "$tail" -lt "$HOT_PER_POLICY_SEC" ]; do
		kill -0 "$ycsb_pid" 2>/dev/null || break
		if ! check_kernel_errors; then
			fail "kernel error in hot-switch pair tail"
		fi
		sleep "$HEALTH_POLL_SEC"
		tail=$((tail + HEALTH_POLL_SEC))
	done

	if kill -0 "$ycsb_pid" 2>/dev/null; then
		wait "$ycsb_pid" || true
	fi
	stop_policy_loader "$to" "$loader_pid"

	if ycsb_has_failure "$run_out"; then
		cat "$run_out" >&2
		record_hot_switch_metrics "$RESULT_ROOT" "$tag" "$run_out" \
			"scheduler_kind=ioext|scheduler=scx_io|from_policy=${from}|to_policy=${to}"
		fail "hot-switch pair ${from}->${to}: YCSB errors"
	fi

	tp="$(parse_ycsb_throughput "$run_out")"
	record_hot_switch_metrics "$RESULT_ROOT" "$tag" "$run_out" \
		"scheduler_kind=ioext|scheduler=scx_io|from_policy=${from}|to_policy=${to}"
	log "PASS hot-switch pair ${from}->${to}: throughput=${tp} ops/s"
}

run_hot_into_q_none() {
	local ycsb_dir="$1" from="$2"
	[ "$from" != "q_none" ] || fail "hot-into-q-none: FROM cannot be q_none"
	run_hot_switch_pair "$ycsb_dir" "$from" "q_none" "hot_into_q_none_${from}"
}

run_hot_from_q_none() {
	local ycsb_dir="$1" to="$2"
	[ "$to" != "q_none" ] || fail "hot-from-q-none: TO cannot be q_none"
	run_hot_switch_pair "$ycsb_dir" "q_none" "$to" "hot_from_q_none_${to}"
}

main() {
	local ycsb_dir

	require_root
	require_java
	require_loaders
	ensure_bpffs
	mkdir -p "$RESULT_ROOT"

	ycsb_dir="$(resolve_ycsb_dir)" || fail "YCSB runtime not found; set YCSB_DIR or mount ioext-bench ycsb_runtime"
	log "kernel: $(uname -r)"
	log "ycsb_dir: $ycsb_dir"
	log "loaders: $LOADER_DIR"
	log "pressure: recordcount=$RECORDCOUNT threads=$THREADS stable_op=$OPERATIONCOUNT hot_op=$HOT_OPERATIONCOUNT"

	setup_test_disk
	dmesg -c >/dev/null 2>&1 || true

	if [ "$RUN_STABLE" -eq 1 ]; then
		local pol
		for pol in "${IO_TEST_POLICIES[@]}"; do
			run_stable_policy "$pol" "$ycsb_dir"
		done
	fi

	if [ "$RUN_HOT" -eq 1 ]; then
		run_hot_switch "$ycsb_dir"
	fi

	if [ -n "$HOT_INTO_Q_NONE" ]; then
		run_hot_into_q_none "$ycsb_dir" "$HOT_INTO_Q_NONE"
	fi

	if [ -n "$HOT_FROM_Q_NONE" ]; then
		run_hot_from_q_none "$ycsb_dir" "$HOT_FROM_Q_NONE"
	fi

	log "all requested io-ext YCSB tests passed"
	log "results: $RESULT_ROOT"
}

main "$@"
