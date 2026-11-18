#!/bin/bash
# 宿主机：根据磁盘/内存自动并发，多 VM 并行跑 io-ext YCSB 测试套件
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
JOB_RUNNER="$BASE_DIR/tests/io/run_job_on_virtme.sh"
INGEST_SCRIPT="$BASE_DIR/tests/io/ingest_legacy_results.sh"
SUMMARIZE="$BASE_DIR/tests/io/summarize_results.py"

RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
SUITE_ROOT="${SUITE_ROOT:-$BASE_DIR/results/io_ycsb_suite/$RUN_ID}"
DISK_GB="${OSEXT_VM_DISK_GB:-10}"
VM_MEMORY_GB="${OSEXT_VM_MEMORY_GB:-4}"
RESERVE_DISK_GB="${RESERVE_DISK_GB:-15}"
MAX_PARALLEL_CAP="${MAX_PARALLEL_CAP:-4}"
SKIP_BUILD="${SKIP_BUILD:-1}"
IMPORT_LEGACY="${IMPORT_LEGACY:-1}"
JOB_FILTER="${JOB_FILTER:-baseline,hot_into,hot_from}"

IO_POLICIES=(q_fifo q_prio q_none q_mq_deadline q_kyber q_bfq q_wedf q_sp_edf)
OTHER_POLICIES=(q_fifo q_prio q_mq_deadline q_kyber q_bfq q_wedf q_sp_edf)
BASELINE_SCHEDS=(none mq-deadline kyber)

usage() {
	cat <<'EOF'
Usage: run_parallel_suite.sh [options]

Options:
  --run-id ID           结果目录名（默认时间戳）
  --max-parallel N      覆盖自动并发上限
  --filter LIST         baseline,hot_into,hot_from,stable_ioext,hot_full
  --no-import-legacy    不导入已有 io_ycsb_test 结果
  --build               构建 userspace
  -h, --help
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
	--run-id) RUN_ID="${2:-}"; SUITE_ROOT="$BASE_DIR/results/io_ycsb_suite/$RUN_ID"; shift 2 ;;
	--max-parallel) MAX_PARALLEL_CAP="${2:-}"; shift 2 ;;
	--filter) JOB_FILTER="${2:-}"; shift 2 ;;
	--no-import-legacy) IMPORT_LEGACY=0; shift ;;
	--build) SKIP_BUILD=0; shift ;;
	-h | --help) usage; exit 0 ;;
	*) echo "unknown: $1" >&2; exit 1 ;;
	esac
done

filter_has() { [[ ",$JOB_FILTER," == *",$1,"* ]]; }

calc_max_parallel() {
	local avail_gb mem_gb by_disk by_mem nproc_half n

	avail_gb=$(df -BG --output=avail "$BASE_DIR" | tail -1 | tr -d ' G')
	mem_gb=$(awk '/MemAvailable:/ {printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 10)
	by_disk=$(( (avail_gb - RESERVE_DISK_GB) / DISK_GB ))
	by_mem=$(( mem_gb / VM_MEMORY_GB ))
	nproc_half=$(( $(nproc) / 4 ))
	n="$by_disk"
	[ "$by_mem" -lt "$n" ] && n="$by_mem"
	[ "$nproc_half" -lt "$n" ] && n="$nproc_half"
	[ "$MAX_PARALLEL_CAP" -lt "$n" ] && n="$MAX_PARALLEL_CAP"
	[ "$n" -lt 1 ] && n=1
	printf '%s\n' "$n"
}

JOBS_FILE="$SUITE_ROOT/jobs.tsv"
mkdir -p "$SUITE_ROOT/disks" "$SUITE_ROOT/jobs"
: >"$JOBS_FILE"

add_job() {
	printf '%s\t%s\t%s\n' "$1" "$2" "$3" >>"$JOBS_FILE"
}

build_job_list() {
	local pol sched
	if filter_has stable_ioext; then
		for pol in "${IO_POLICIES[@]}"; do
			add_job "stable_ioext_${pol}" run_all_policies_ycsb.sh "--policy $pol --stable-only"
		done
	fi
	if filter_has baseline; then
		for sched in "${BASELINE_SCHEDS[@]}"; do
			add_job "stable_baseline_${sched}" run_baseline_ycsb.sh "--scheduler $sched"
		done
	fi
	if filter_has hot_into; then
		for pol in "${OTHER_POLICIES[@]}"; do
			add_job "hot_into_q_none_${pol}" run_all_policies_ycsb.sh "--hot-into-q-none $pol"
		done
	fi
	if filter_has hot_from; then
		for pol in "${OTHER_POLICIES[@]}"; do
			add_job "hot_from_q_none_${pol}" run_all_policies_ycsb.sh "--hot-from-q-none $pol"
		done
	fi
	if filter_has hot_full; then
		add_job hot_switch_full run_all_policies_ycsb.sh "--hot-switch-only"
	fi
}

run_one_job() {
	local job_id="$1" script="$2" args="$3"
	local disk result

	disk="$SUITE_ROOT/disks/${job_id}.img"
	result="$SUITE_ROOT/jobs/${job_id}"

	# shellcheck disable=SC2086
	"$JOB_RUNNER" --run-id "$RUN_ID" --job-id "$job_id" \
		--disk "$disk" --result "$result" \
		--test "$script" $args
}

main() {
	local max_parallel failed=0 line

	chmod +x "$JOB_RUNNER" "$BASE_DIR/tests/io/"*.sh 2>/dev/null || true

	if [ "$IMPORT_LEGACY" -eq 1 ] && [ -d "$BASE_DIR/results/io_ycsb_test" ]; then
		"$INGEST_SCRIPT" "$BASE_DIR/results/io_ycsb_test" "$SUITE_ROOT/legacy_import"
	fi

	build_job_list
	max_parallel="$(calc_max_parallel)"

	{
		printf 'run_id=%s\n' "$RUN_ID"
		printf 'suite_root=%s\n' "$SUITE_ROOT"
		printf 'jobs=%s\n' "$(wc -l <"$JOBS_FILE")"
		printf 'max_parallel=%s\n' "$max_parallel"
	} | tee "$SUITE_ROOT/suite_meta.txt"

	: >"$SUITE_ROOT/failed_jobs.txt"
	exec 9<"$JOBS_FILE"
	while IFS=$'\t' read -r job_id script args <&9; do
		while [ "$(jobs -rp | wc -l)" -ge "$max_parallel" ]; do
			if ! wait -n 2>/dev/null; then wait || true; fi
		done
		(
			set +e
			run_one_job "$job_id" "$script" "$args"
			rc=$?
			if [ "$rc" -ne 0 ]; then
				printf '%s\n' "$job_id" >>"$SUITE_ROOT/failed_jobs.txt"
			fi
			exit 0
		) &
	done
	wait || true

	if [ -s "$SUITE_ROOT/failed_jobs.txt" ]; then
		failed=1
		printf 'failed jobs:\n' >&2
		cat "$SUITE_ROOT/failed_jobs.txt" >&2
	fi

	python3 "$SUMMARIZE" --suite-root "$SUITE_ROOT" \
		--include-legacy "$SUITE_ROOT/legacy_import" \
		>"$SUITE_ROOT/summary.md"
	printf 'summary: %s/summary.md\n' "$SUITE_ROOT"

	[ "$failed" -eq 0 ]
}

main "$@"
