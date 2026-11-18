#!/bin/bash
# tests/io/full_suite/full_suite.sh
# VM 内主编排: setup -> deterministic -> lifecycle -> merge -> coverage -> report
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
FS_DIR="$BASE_DIR/tests/io/full_suite"
MODE="${FULL_SUITE_MODE:-quick}"

source "$FS_DIR/lib_test.sh"

fs_log "=== io_ext full_suite START (mode=$MODE) ==="
fs_log "BASE_DIR=$BASE_DIR DEVICE=$FS_DEVICE"

source "$FS_DIR/00_setup.sh"

if [ "$MODE" != "lifecycle-only" ] && [ "$MODE" != "merge-only" ]; then
    fs_log "=== Phase: Deterministic Tests ==="
    source "$FS_DIR/deterministic/run_deterministic.sh" || fs_warn "deterministic phase had failures"
fi

if [ "$MODE" != "deterministic-only" ] && [ "$MODE" != "merge-only" ]; then
    fs_log "=== Phase: Lifecycle Tests ==="
    source "$FS_DIR/lifecycle/run_lifecycle.sh" || fs_warn "lifecycle phase had failures"
fi

if [ "$MODE" != "deterministic-only" ] && [ "$MODE" != "lifecycle-only" ]; then
    fs_log "=== Phase: Merge Tests ==="
    if [ -x "$BASE_DIR/tests/io/merge/run_merge_suite.sh" ]; then
        FIO_RUNTIME="${FIO_RUNTIME:-15}" \
        bash "$BASE_DIR/tests/io/merge/run_merge_suite.sh" || fs_warn "merge phase had failures"
    else
        fs_warn "merge suite not found, skipping"
    fi
fi

if [ "${ENABLE_COVERAGE:-0}" = "1" ]; then
    fs_log "=== Phase: Coverage Collection ==="
    source "$FS_DIR/coverage/collect_coverage.sh" || fs_warn "coverage collection had issues"
fi

fs_log "=== Phase: Report Generation ==="
source "$FS_DIR/report/generate_report.sh"

fs_cleanup_ioext

fs_log "=== full_suite END ==="
fs_log "PASS=$FS_CASES_PASS FAIL=$FS_CASES_FAIL BLOCKED=$FS_CASES_BLOCKED"
fs_log "results: $FS_RESULTS_DIR"