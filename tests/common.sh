#!/bin/bash
# io-ext YCSB 测试公共函数（VM 内使用）
set -euo pipefail

IO_TEST_POLICIES=(
	q_fifo
	q_prio
	q_none
	q_mq_deadline
	q_kyber
	q_bfq
	q_wedf
	q_sp_edf
)

BASELINE_SCHEDULERS=(
	none
	mq-deadline
	kyber
)

if [ -n "${IO_TEST_POLICIES_OVERRIDE:-}" ]; then
	read -r -a IO_TEST_POLICIES <<<"$IO_TEST_POLICIES_OVERRIDE"
fi

TARGET_DEVICE="${TARGET_DEVICE:-nvme0n1}"
BASE_DIR="${BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
LOADER_DIR="${LOADER_DIR:-$BASE_DIR/userspace/build}"
RESULT_ROOT="${RESULT_ROOT:-$BASE_DIR/results/io_ycsb_test}"
MOUNT_POINT="${MOUNT_POINT:-/tmp/nvme_data}"
DATA_DIR="${DATA_DIR:-$MOUNT_POINT/ioext_ycsb}"
CGROUP_PATH="${CGROUP_PATH:-/sys/fs/cgroup}"
SCHEDULER="${SCHEDULER:-scx_io}"

RECORDCOUNT="${RECORDCOUNT:-100000}"
OPERATIONCOUNT="${OPERATIONCOUNT:-100000}"
THREADS="${THREADS:-4}"
JVM_ARGS="${JVM_ARGS:--Xms1024m -Xmx1024m}"
FIELD_COUNT="${FIELD_COUNT:-10}"
FIELD_LENGTH="${FIELD_LENGTH:-1024}"
WORKLOAD_FILE="${WORKLOAD_FILE:-workloads/workloada}"

STABLE_MIN_SEC="${STABLE_MIN_SEC:-120}"
STABLE_MAX_SEC="${STABLE_MAX_SEC:-900}"
HOT_WARMUP_SEC="${HOT_WARMUP_SEC:-30}"
HOT_PER_POLICY_SEC="${HOT_PER_POLICY_SEC:-25}"
HOT_OPERATIONCOUNT="${HOT_OPERATIONCOUNT:-800000}"
LOADER_START_TIMEOUT_SEC="${LOADER_START_TIMEOUT_SEC:-30}"
LOADER_STOP_TIMEOUT_SEC="${LOADER_STOP_TIMEOUT_SEC:-15}"
HEALTH_POLL_SEC="${HEALTH_POLL_SEC:-10}"
STALL_TIMEOUT_SEC="${STALL_TIMEOUT_SEC:-45}"

LAST_BG_PID=""

log() { printf '[io-ycsb] %s\n' "$*" >&2; }
warn() { printf '[io-ycsb] WARN: %s\n' "$*" >&2; }
fail() { printf '[io-ycsb] FAIL: %s\n' "$*" >&2; exit 1; }

require_root() {
	[ "$(id -u)" -eq 0 ] || fail "must run as root inside virtme-ng VM"
}

resolve_ycsb_dir() {
	local cand
	for cand in \
		"${YCSB_DIR:-}" \
		"${PERF_ARENA_YCSB_RUNTIME:-}" \
		"$BASE_DIR/tests/ycsb_runtime" \
		"/opt/ioext/src/vm/assets/ycsb/ycsb_runtime"; do
		[ -n "$cand" ] || continue
		if [ -x "$cand/bin/ycsb" ]; then
			printf '%s\n' "$cand"
			return 0
		fi
	done
	return 1
}

require_java() {
	if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then
		return 0
	fi
	command -v java >/dev/null 2>&1 || fail "java not found (set JAVA_HOME)"
}

require_loaders() {
	local pol loader
	for pol in "${IO_TEST_POLICIES[@]}"; do
		loader="$LOADER_DIR/loader_${pol}"
		[ -x "$loader" ] || fail "missing loader: $loader (run: make -C userspace)"
	done
}

ensure_bpffs() {
	mountpoint -q /sys/fs/bpf 2>/dev/null \
		|| mount -t bpf bpf /sys/fs/bpf 2>/dev/null \
		|| fail "cannot mount bpffs"
}

cleanup_ioext() {
	log "cleanup io-ext loaders and pinned maps"
	pkill -TERM -f 'loader_q_' 2>/dev/null || true
	sleep 1
	pkill -9 -f 'loader_q_' 2>/dev/null || true
	pkill -9 -f 'loader_arb_' 2>/dev/null || true

	for map in ioext_cgroup_queues ioext_active_cgroups ioext_policy_meta \
		ioext_bfq_tenant_state ioext_prio_config; do
		rm -rf "/sys/fs/bpf/${map}" 2>/dev/null || true
	done

	if [ -f "/sys/block/${TARGET_DEVICE}/queue/scheduler" ]; then
		echo none > "/sys/block/${TARGET_DEVICE}/queue/scheduler" 2>/dev/null || true
	fi
}

setup_cgroup_io() {
	if [ -f /sys/fs/cgroup/cgroup.subtree_control ]; then
		echo '+io' > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true
	fi
}

setup_test_disk() {
	local dev="/dev/${TARGET_DEVICE}"
	local force="${FORMAT_DEVICE:-1}"

	detect_block_device || {
		lsblk >&2 || true
		fail "no usable block device (want ${TARGET_DEVICE})"
	}
	dev="/dev/${TARGET_DEVICE}"

	mkdir -p "$MOUNT_POINT"

	if [ "$force" = "1" ]; then
		log "format $dev -> ext4"
		umount "$MOUNT_POINT" 2>/dev/null || true
		mkfs.ext4 -F "$dev" >/dev/null
	fi

	if ! mountpoint -q "$MOUNT_POINT"; then
		mount -o noatime,nodiratime "$dev" "$MOUNT_POINT"
		chmod 1777 "$MOUNT_POINT"
	fi
}

detect_block_device() {
	local cand

	if [ -b "/dev/${TARGET_DEVICE}" ]; then
		return 0
	fi

	for cand in nvme0n1 vda sda; do
		if [ -b "/dev/$cand" ]; then
			warn "TARGET_DEVICE ${TARGET_DEVICE} missing, using /dev/$cand"
			TARGET_DEVICE="$cand"
			return 0
		fi
	done
	return 1
}

rocksdb_options_file() {
	local path="$RESULT_ROOT/rocksdb_direct_io.ini"
	mkdir -p "$RESULT_ROOT"
	cat >"$path" <<'EOF'
[Version]
  rocksdb_version=6.2.2
  options_file_version=1.1

[DBOptions]
  create_if_missing=true
  create_missing_column_families=true
  use_direct_reads=true
  use_direct_io_for_flush_and_compaction=true
  allow_mmap_reads=false
  allow_mmap_writes=false

[CFOptions "default"]
  write_buffer_size=4194304

[CFOptions "usertable"]
  write_buffer_size=4194304
EOF
	printf '%s\n' "$path"
}

active_block_scheduler() {
	local sched_path="/sys/block/${TARGET_DEVICE}/queue/scheduler"
	[ -f "$sched_path" ] || return 1
	sed -n 's/.*\[\([^]]*\)\].*/\1/p' "$sched_path"
}

assert_scx_scheduler_active() {
	local active avail
	local sched_path="/sys/block/${TARGET_DEVICE}/queue/scheduler"

	[ -f "$sched_path" ] || fail "scheduler sysfs missing: $sched_path"
	active="$(active_block_scheduler)"
	avail="$(tr -d '\n' <"$sched_path")"
	if [ "$active" != "$SCHEDULER" ]; then
		fail "block scheduler is '${active:-unknown}', expected [${SCHEDULER}] (available: ${avail})"
	fi
}

set_scx_scheduler() {
	local sched_path="/sys/block/${TARGET_DEVICE}/queue/scheduler"
	local avail

	[ -f "$sched_path" ] || fail "scheduler sysfs missing: $sched_path"
	avail="$(tr -d '\n' <"$sched_path")"
	grep -qw "$SCHEDULER" <<<"$avail" \
		|| fail "${SCHEDULER} not in available schedulers: ${avail}"

	echo "$SCHEDULER" >"$sched_path" \
		|| fail "failed to switch block scheduler to ${SCHEDULER}"

	assert_scx_scheduler_active
	log "scheduler=$(cat "$sched_path")"
}

set_native_scheduler() {
	local name="$1"
	local sched_path="/sys/block/${TARGET_DEVICE}/queue/scheduler"
	local avail active

	[ -f "$sched_path" ] || fail "scheduler sysfs missing: $sched_path"
	avail="$(tr -d '\n' <"$sched_path")"
	grep -qw "$name" <<<"$avail" \
		|| fail "${name} not in available schedulers: ${avail}"

	cleanup_ioext
	echo "$name" >"$sched_path" \
		|| fail "failed to switch block scheduler to ${name}"

	active="$(active_block_scheduler)"
	if [ "$active" != "$name" ]; then
		fail "block scheduler is '${active:-unknown}', expected [${name}]"
	fi
	log "native scheduler=$(cat "$sched_path")"
}

loader_log_path() {
	local policy="$1"
	printf '%s/loader_%s.log\n' "$RESULT_ROOT" "$policy"
}

start_policy_loader() {
	local policy="$1"
	local loader="$LOADER_DIR/loader_${policy}"
	local logfile
	logfile="$(loader_log_path "$policy")"

	mkdir -p "$RESULT_ROOT"
	: >"$logfile"

	"$loader" --cgroup "$CGROUP_PATH" >"$logfile" 2>&1 &
	LAST_BG_PID=$!
}

loader_process_alive() {
	local policy="$1"
	local pid="${2:-}"

	if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
		return 0
	fi
	pgrep -f "loader_${policy}" >/dev/null 2>&1
}

wait_policy_attached() {
	local policy="$1"
	local loader_pid="${2:-}"
	local timeout_sec="${3:-$LOADER_START_TIMEOUT_SEC}"
	local i attached=0 logfile

	logfile="$(loader_log_path "$policy")"
	assert_scx_scheduler_active

	for ((i = 1; i <= timeout_sec; i++)); do
		if ! loader_process_alive "$policy" "$loader_pid"; then
			cat "$logfile" >&2 || true
			return 1
		fi

		if dmesg | grep -qE "policy '${policy}' attached to|policy '${policy}' replaced on"; then
			attached=1
			break
		fi

		# attach 成功才会进入 sleep 保活；存活 >=2s 且 scx_io 仍激活即可认定
		if [ "$i" -ge 2 ]; then
			attached=1
			break
		fi

		sleep 1
	done

	if [ "$attached" -ne 1 ]; then
		if ! loader_process_alive "$policy" "$loader_pid"; then
			cat "$logfile" >&2 || true
			return 1
		fi
		assert_scx_scheduler_active
		warn "loader_${policy} attach not confirmed in dmesg; continuing (loader alive, scx_io active)"
	fi

	log "policy ${policy} ready (loader pid=${loader_pid:-$(pgrep -f "loader_${policy}" | head -1 || echo unknown)})"
	return 0
}

stop_policy_loader() {
	local policy="$1"
	local pid="${2:-}"
	local i

	if [ -n "$pid" ]; then
		kill -TERM "$pid" 2>/dev/null || true
	else
		pkill -TERM -f "loader_${policy}" 2>/dev/null || true
	fi

	for ((i = 1; i <= LOADER_STOP_TIMEOUT_SEC; i++)); do
		pgrep -f "loader_${policy}" >/dev/null || return 0
		sleep 1
	done

	pkill -9 -f "loader_${policy}" 2>/dev/null || true
}

count_d_state() {
	ps -eo stat= 2>/dev/null | grep -c '^D' || true
}

# 仅在 YCSB 输出停滞且 D-state 持续时才判定挂死（避免 direct-IO 短暂 D 误报）
d_state_hang_suspected() {
	local stalled_for="${1:-0}"
	[ "$stalled_for" -ge "$STALL_TIMEOUT_SEC" ] || return 1
	[ "$(count_d_state)" -gt 0 ]
}

read_inflight() {
	local path="/sys/block/${TARGET_DEVICE}/inflight"
	if [ ! -f "$path" ]; then
		printf '0\n'
		return 0
	fi
	tr -d ' \n' <"$path"
}

start_ycsb() {
	local ycsb_dir="$1"
	local phase="$2"
	local outfile="$3"
	local opcount="${4:-$OPERATIONCOUNT}"
	local workload_path="$ycsb_dir/$WORKLOAD_FILE"
	local opts
	opts="$(rocksdb_options_file)"

	mkdir -p "$(dirname "$outfile")"
	log "YCSB $phase (op=$opcount) -> $outfile"

	(
		cd "$ycsb_dir"
		if [ -n "${JVM_ARGS:-}" ]; then
			./bin/ycsb -jvm-args "$JVM_ARGS" "$phase" rocksdb -s \
				-P "$workload_path" \
				-p "rocksdb.dir=$DATA_DIR" \
				-p "rocksdb.optionsfile=$opts" \
				-p "recordcount=$RECORDCOUNT" \
				-p "operationcount=$opcount" \
				-threads "$THREADS" \
				-p "fieldcount=$FIELD_COUNT" \
				-p "fieldlength=$FIELD_LENGTH"
		else
			./bin/ycsb "$phase" rocksdb -s \
				-P "$workload_path" \
				-p "rocksdb.dir=$DATA_DIR" \
				-p "rocksdb.optionsfile=$opts" \
				-p "recordcount=$RECORDCOUNT" \
				-p "operationcount=$opcount" \
				-threads "$THREADS" \
				-p "fieldcount=$FIELD_COUNT" \
				-p "fieldlength=$FIELD_LENGTH"
		fi
	) >"$outfile" 2>&1 &
	LAST_BG_PID=$!
}

run_ycsb_phase() {
	local ycsb_dir="$1"
	local phase="$2"
	local outfile="$3"

	start_ycsb "$ycsb_dir" "$phase" "$outfile" "$OPERATIONCOUNT"
	wait "$LAST_BG_PID"
}

check_kernel_errors() {
	dmesg | tail -n 200 | grep -qiE 'hung_task|BUG:|Oops:|panic' && return 1
	return 0
}

parse_ycsb_throughput() {
	local file="$1"
	grep -E '^\[OVERALL\],\s*Throughput\(ops/sec\)' "$file" 2>/dev/null \
		| tail -1 | awk -F, '{print $3}' | tr -d ' ' || true
}

ycsb_has_failure() {
	local file="$1"
	grep -qE 'site\.ycsb\.DBException|RocksDBException|Exception in thread|ERROR site\.ycsb' "$file" 2>/dev/null
}

monitor_ycsb_progress() {
	local pid="$1"
	local outfile="$2"
	local min_sec="$3"
	local max_sec="$4"
	local start now last_size cur_size stalled_for

	start=$(date +%s)
	last_size=$(stat -c%s "$outfile" 2>/dev/null || echo 0)
	stalled_for=0

	while kill -0 "$pid" 2>/dev/null; do
		now=$(date +%s)
		elapsed=$((now - start))

		if d_state_hang_suspected "$stalled_for"; then
			fail "D-state with stalled YCSB output (${elapsed}s, d-state=$(count_d_state))"
		fi
		if ! check_kernel_errors; then
			fail "kernel error while YCSB running (${elapsed}s)"
		fi

		cur_size=$(stat -c%s "$outfile" 2>/dev/null || echo 0)
		if [ "$cur_size" -gt "$last_size" ]; then
			stalled_for=0
			last_size=$cur_size
		else
			stalled_for=$((stalled_for + HEALTH_POLL_SEC))
		fi

		if [ "$elapsed" -ge "$min_sec" ] && [ "$stalled_for" -ge "$STALL_TIMEOUT_SEC" ]; then
			fail "YCSB output stalled for ${stalled_for}s (pid=$pid)"
		fi
		if [ "$elapsed" -ge "$max_sec" ]; then
			kill -TERM "$pid" 2>/dev/null || true
			sleep 2
			kill -9 "$pid" 2>/dev/null || true
			fail "YCSB exceeded max runtime ${max_sec}s"
		fi

		sleep "$HEALTH_POLL_SEC"
	done

	wait "$pid"
}

prepare_ycsb_dataset() {
	local ycsb_dir="$1"
	local tag="$2"
	local load_out="$RESULT_ROOT/${tag}_load.txt"

	rm -rf "$DATA_DIR"
	mkdir -p "$DATA_DIR"
	run_ycsb_phase "$ycsb_dir" load "$load_out"
	if ycsb_has_failure "$load_out"; then
		cat "$load_out" >&2
		fail "YCSB load failed ($tag)"
	fi
}
