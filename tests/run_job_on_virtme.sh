#!/bin/bash
# 宿主机：单 job 启动独立 virtme-ng VM（独立磁盘 + 结果目录）
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
RUN_ON_VIRTME="$BASE_DIR/tests/io/run_on_virtme.sh"

RUN_ID=""
JOB_ID=""
DISK_IMG=""
RESULT_DIR=""
SKIP_BUILD=1
TEST_SCRIPT=""
TEST_ARGS=()

usage() {
	cat <<'EOF'
Usage: run_job_on_virtme.sh --run-id ID --job-id ID --disk PATH --result DIR --test SCRIPT [args...]
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
	--run-id) RUN_ID="${2:-}"; shift 2 ;;
	--job-id) JOB_ID="${2:-}"; shift 2 ;;
	--disk) DISK_IMG="${2:-}"; shift 2 ;;
	--result) RESULT_DIR="${2:-}"; shift 2 ;;
	--test) TEST_SCRIPT="${2:-}"; shift 2 ;;
	--build) SKIP_BUILD=0; shift ;;
	-h | --help) usage; exit 0 ;;
	*) TEST_ARGS+=("$1"); shift ;;
	esac
done

[ -n "$RUN_ID" ] && [ -n "$JOB_ID" ] && [ -n "$DISK_IMG" ] && [ -n "$RESULT_DIR" ] && [ -n "$TEST_SCRIPT" ] \
	|| { usage >&2; exit 1; }

mkdir -p "$(dirname "$DISK_IMG")" "$RESULT_DIR"
LOG_FILE="$RESULT_DIR/job.log"

if [ ! -f "$DISK_IMG" ]; then
	qemu-img create -f raw "$DISK_IMG" "${OSEXT_VM_DISK_GB:-10}G" >/dev/null
fi

guest_cmd="cd $(printf '%q' "$BASE_DIR") && RESULT_ROOT=$(printf '%q' "$RESULT_DIR") TARGET_DEVICE=vda YCSB_DIR=$(printf '%q' "$BASE_DIR/tests/io/ycsb_runtime") ./tests/io/$(printf '%q' "$TEST_SCRIPT")"
for arg in "${TEST_ARGS[@]}"; do
	guest_cmd+=" $(printf '%q' "$arg")"
done

{
	printf '=== job %s (%s) ===\n' "$JOB_ID" "$RUN_ID"
	printf 'disk=%s result=%s\n' "$DISK_IMG" "$RESULT_DIR"
	printf 'cmd=%s\n' "$guest_cmd"
} | tee "$LOG_FILE"

build_flag=(--skip-build)
[ "$SKIP_BUILD" -eq 0 ] && build_flag=()

set +e
OSEXT_VM_DISK_IMG="$DISK_IMG" QEMU_DISK=virtio \
	"$RUN_ON_VIRTME" "${build_flag[@]}" --exec "$guest_cmd" 2>&1 | tee -a "$LOG_FILE"
rc=${PIPESTATUS[0]}
set -e

if [ -f "$RESULT_DIR/metrics.json" ]; then
	printf 'job %s: metrics ok\n' "$JOB_ID" | tee -a "$LOG_FILE"
else
	printf 'job %s: WARN no metrics.json (rc=%s)\n' "$JOB_ID" "$rc" | tee -a "$LOG_FILE"
fi

exit "$rc"
