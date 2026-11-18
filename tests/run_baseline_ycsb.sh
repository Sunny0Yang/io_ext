#!/bin/bash
# VM 内：原生块调度器 baseline YCSB stable 测试（无 scx_io / loader）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"
# shellcheck source=metrics.sh
source "$SCRIPT_DIR/metrics.sh"

SCHEDULER_NAME=""

usage() {
	cat <<'EOF'
Usage: run_baseline_ycsb.sh --scheduler NAME

NAME: none | mq-deadline | kyber
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
	--scheduler)
		SCHEDULER_NAME="${2:-}"
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

[ -n "$SCHEDULER_NAME" ] || fail "--scheduler required"

main() {
	local ycsb_dir tag load_out run_out tp

	require_root
	require_java
	mkdir -p "$RESULT_ROOT"

	ycsb_dir="$(resolve_ycsb_dir)" || fail "YCSB runtime not found"
	tag="baseline_${SCHEDULER_NAME}"
	load_out="$RESULT_ROOT/${tag}_load.txt"
	run_out="$RESULT_ROOT/${tag}_run.txt"

	log "=== BASELINE STABLE: ${SCHEDULER_NAME} ==="
	log "kernel: $(uname -r)"

	setup_test_disk
	setup_cgroup_io
	set_native_scheduler "$SCHEDULER_NAME"
	dmesg -c >/dev/null 2>&1 || true

	prepare_ycsb_dataset "$ycsb_dir" "$tag"

	start_ycsb "$ycsb_dir" run "$run_out" "$OPERATIONCOUNT"
	monitor_ycsb_progress "$LAST_BG_PID" "$run_out" "$STABLE_MIN_SEC" "$STABLE_MAX_SEC"

	if ycsb_has_failure "$run_out"; then
		cat "$run_out" >&2
		record_stable_metrics "$RESULT_ROOT" baseline "$SCHEDULER_NAME" "" "$load_out" "$run_out"
		fail "baseline ${SCHEDULER_NAME}: YCSB run reported errors"
	fi

	tp="$(parse_ycsb_throughput "$run_out")"
	record_stable_metrics "$RESULT_ROOT" baseline "$SCHEDULER_NAME" "" "$load_out" "$run_out"
	log "PASS baseline ${SCHEDULER_NAME}: throughput=${tp} ops/s"
}

main "$@"
