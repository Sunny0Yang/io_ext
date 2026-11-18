#!/bin/bash
# YCSB/io-ext 测试结果指标读写（VM 内或宿主机均可）
set -euo pipefail

ycsb_has_failure() {
	local file="$1"
	grep -qE 'site\.ycsb\.DBException|RocksDBException|Exception in thread|ERROR site\.ycsb' "$file" 2>/dev/null
}

parse_ycsb_runtime_ms() {
	local file="$1"
	grep -E '^\[OVERALL\],\s*RunTime\(ms\)' "$file" 2>/dev/null \
		| tail -1 | awk -F, '{print $3}' | tr -d ' ' || true
}

parse_ycsb_throughput() {
	local file="$1"
	grep -E '^\[OVERALL\],\s*Throughput\(ops/sec\)' "$file" 2>/dev/null \
		| tail -1 | awk -F, '{print $3}' | tr -d ' ' || true
}

metrics_json_path() {
	printf '%s/metrics.json\n' "${1:-${RESULT_ROOT:-.}}"
}

write_metrics_json() {
	local outdir="$1"
	local status="$2"
	shift 2
	local out
	out="$(metrics_json_path "$outdir")"
	mkdir -p "$outdir"

	# 其余参数为 key=value
	{
		printf '{\n'
		printf '  "status": "%s",\n' "$status"
		printf '  "timestamp": "%s",\n' "$(date -Iseconds)"
		local first=1 kv key val
		for kv in "$@"; do
			key="${kv%%=*}"
			val="${kv#*=}"
			[ "$first" -eq 1 ] || printf ',\n'
			first=0
			if [[ "$val" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
				printf '  "%s": %s' "$key" "$val"
			elif [ "$val" = "null" ]; then
				printf '  "%s": null' "$key"
			else
				val="${val//\\/\\\\}"
				val="${val//\"/\\\"}"
				printf '  "%s": "%s"' "$key" "$val"
			fi
		done
		printf '\n}\n'
	} >"$out"
}

record_stable_metrics() {
	local outdir="$1" kind="$2" scheduler="$3" policy="${4:-}"
	local load_log="$5" run_log="$6"
	local load_tp run_tp run_ms status="pass"

	load_tp="$(parse_ycsb_throughput "$load_log")"
	run_tp="$(parse_ycsb_throughput "$run_log")"
	run_ms="$(parse_ycsb_runtime_ms "$run_log")"

	if ycsb_has_failure "$run_log" 2>/dev/null || [ -z "$run_tp" ]; then
		status="fail"
	fi

	write_metrics_json "$outdir" "$status" \
		"suite=stable" \
		"scheduler_kind=$kind" \
		"scheduler=$scheduler" \
		"policy=${policy:-null}" \
		"load_throughput_ops_sec=${load_tp:-0}" \
		"throughput_ops_sec=${run_tp:-0}" \
		"run_time_ms=${run_ms:-0}" \
		"recordcount=${RECORDCOUNT:-0}" \
		"operationcount=${OPERATIONCOUNT:-0}" \
		"threads=${THREADS:-0}" \
		"target_device=${TARGET_DEVICE:-unknown}" \
		"kernel=$(uname -r 2>/dev/null || echo unknown)" \
		"load_log=$load_log" \
		"run_log=$run_log"
}

record_hot_switch_metrics() {
	local outdir="$1" tag="$2" run_log="$3" extra="${4:-}"
	local run_tp run_ms status="pass"

	run_tp="$(parse_ycsb_throughput "$run_log")"
	run_ms="$(parse_ycsb_runtime_ms "$run_log")"

	if ycsb_has_failure "$run_log" 2>/dev/null || [ -z "$run_tp" ]; then
		status="fail"
	fi

	local args=(
		"suite=hot_switch"
		"tag=$tag"
		"throughput_ops_sec=${run_tp:-0}"
		"run_time_ms=${run_ms:-0}"
		"operationcount=${HOT_OPERATIONCOUNT:-0}"
		"threads=${THREADS:-0}"
		"target_device=${TARGET_DEVICE:-unknown}"
		"kernel=$(uname -r 2>/dev/null || echo unknown)"
		"run_log=$run_log"
	)
	if [ -n "$extra" ]; then
		IFS='|' read -r -a extra_kv <<<"$extra"
		args+=("${extra_kv[@]}")
	fi
	write_metrics_json "$outdir" "$status" "${args[@]}"
}
