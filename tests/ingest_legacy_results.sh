#!/bin/bash
# 将已有 io_ycsb_test 文本结果导入 metrics.json（宿主机）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=metrics.sh
source "$SCRIPT_DIR/metrics.sh"

LEGACY_ROOT="${1:-$(cd "$SCRIPT_DIR/../.." && pwd)/results/io_ycsb_test}"
OUT_SUITE="${2:-$(cd "$SCRIPT_DIR/../.." && pwd)/results/io_ycsb_suite/legacy_import}"

mkdir -p "$OUT_SUITE"

import_stable_ioext() {
	local policy="$1"
	local job_dir="$OUT_SUITE/stable_ioext_${policy}"
	local load run

	load="$LEGACY_ROOT/stable_${policy}_load.txt"
	run="$LEGACY_ROOT/stable_${policy}_run.txt"
	[ -f "$run" ] || return 0

	RECORDCOUNT=100000 OPERATIONCOUNT=100000 THREADS=4 TARGET_DEVICE=vda \
		record_stable_metrics "$job_dir" ioext scx_io "$policy" "$load" "$run"
	printf 'imported stable ioext %s\n' "$policy"
}

import_hot_full() {
	local job_dir="$OUT_SUITE/hot_switch_full"
	local load run

	load="$LEGACY_ROOT/hot_switch_load.txt"
	run="$LEGACY_ROOT/hot_switch_run.txt"
	[ -f "$run" ] || return 0

	HOT_OPERATIONCOUNT=800000 THREADS=4 TARGET_DEVICE=vda \
		record_hot_switch_metrics "$job_dir" full "$run" \
		"scheduler_kind=ioext|scheduler=scx_io|switch_path=full_chain"
	printf 'imported hot_switch full\n'
}

for pol in q_fifo q_prio q_none q_mq_deadline q_kyber q_bfq q_wedf q_sp_edf; do
	import_stable_ioext "$pol"
done
import_hot_full

printf 'legacy import done -> %s\n' "$OUT_SUITE"
