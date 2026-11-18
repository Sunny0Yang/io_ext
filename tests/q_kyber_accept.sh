#!/bin/bash
# tests/q_kyber_accept.sh — q_kyber closed-loop acceptance test (VM-side).
#
# Mirrors the run_dt_mqdd_only.sh run_one flow for policy q_kyber with
# per-case G1–G4 gates and honest PASS/FAIL/BLOCKED verdicts:
#   Gate 0  reference model self-test (tests/q_kyber_model.py)
#   G1      check_inflight.py --min-inflight 2 (scheduler actually active)
#   G2      tests/q_kyber_gates.py: ISSUE with >=2 other requests
#           outstanding (strict form for order cases; literal form for the
#           merge case). Shared check_inflight.py dispatch_with_choice is a
#           dead counter (always 0) — computed here instead.
#   G3      play-window purity via q_kyber_gates.py (>=90% trace-sector
#           issues, all trace sectors covered)
#   G4      honest substitute: window ISSUE accounting == trace request
#           count (coverage for the merge case) + raw INSERT tracepoint
#           count recorded. The literal G4 (insert_count == trace count)
#           is structurally impossible: block:block_rq_insert NEVER fires
#           for scx_io-managed requests in this kernel (diagnosed in the
#           I01/I02 insert_count=0 flake — see flake_diag below).
#   D5      bpftool assertion on the pinned /sys/fs/bpf/kyber_merge_cfg map
#           (enable must match the case's --merge-enable value)
#   D8      re-runs the registered q_kyber-DT-I01/I02 case definitions
#           (shared trace/expect files used read-only) as diagnostic cases
#           and records whether the insert_count=0 failure reproduces.
#   P0      CLI --*-target-ns values are INERT under the plain accept order
#           (targets reach per-hctx state only via init_hctx, which fires
#           only on the ELEVATOR INIT path, never at policy attach): cases
#           with a want_targets spec re-switch the scheduler after loader
#           attach to re-fire init_hctx, then VERIFY target_lat_ns via the
#           state_map dump before driving traffic (kyber_refire_init_hctx +
#           kyber_assert_targets).
#   D9/D10  KYBER_DEPTH_SHRINK / KYBER_DEPTH_HOLD Tier-1 dynamic-depth
#           cases: native kyber token law (histogram -> percentile ->
#           resize) asserted from state_map dumps, gated on P0.
#
# Run inside the virtme-ng VM as root (IOEXT_DESTRUCTIVE_TEST=1 required):
#   tests/q_kyber_accept.sh
# Exit: 0 = all strict cases PASS, 1 = at least one FAIL/BLOCKED verdict.
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FS_DIR="$BASE_DIR/tests/full_suite"
DT_DIR="$FS_DIR/deterministic"
export TARGET_DEVICE="${TARGET_DEVICE:-vda}"
export IOEXT_DESTRUCTIVE_TEST="${IOEXT_DESTRUCTIVE_TEST:-1}"
export POLICY_OVERRIDE="${POLICY_OVERRIDE:-q_kyber}"

RESULTS_DIR="${RESULTS_DIR:-$BASE_DIR/results/q_kyber_accept_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$RESULTS_DIR"
R_FILE="$RESULTS_DIR/results.txt"
: > "$R_FILE"
PASS=0; FAIL=0; BLOCKED=0; DIAG=0

log()     { printf '[q_kyber_accept] %s\n' "$*" >&2; }
record()  { printf '%s\n' "$*" >> "$R_FILE"; }
pass()    { log "PASS: $1"; record "PASS: $1"; PASS=$((PASS+1)); }
fail()    { log "FAIL: $1: $2"; record "FAIL: $1: $2"; FAIL=$((FAIL+1)); }
blocked() { log "BLOCKED: $1: $2"; record "BLOCKED: $1: $2"; BLOCKED=$((BLOCK+1)); }
diag()    { log "DIAG: $1"; record "DIAG: $1"; DIAG=$((DIAG+1)); }

source "$FS_DIR/lib_test.sh"
set +e

TRACE_DRIVER="$DT_DIR/trace_driver"
if [ ! -x "$TRACE_DRIVER" ]; then
    mkdir -p /tmp/opencode
    gcc -O2 -o /tmp/opencode/q_kyber_trace_driver "$DT_DIR/trace_driver.c" \
        || { log "trace_driver build failed"; exit 1; }
    TRACE_DRIVER=/tmp/opencode/q_kyber_trace_driver
fi

# The VM's PATH bpftool is a broken distro stub ("bpftool not found for
# kernel 7.0.0-rc4"); the kernel-tree bpftool is the working one (verified
# 2026-09-01: pinned dump emits [{"key":[...],"value":["0x01",...],
# "formatted":{"key":0,"value":{"enable":1,"reserved":0}}}]).
BT="$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
if [ ! -x "$BT" ]; then
    BT="$(command -v bpftool || true)"
fi

BPFTRACE_SCRIPT="$DT_DIR/bpftrace/trace_dispatch.bt"
COMPARE_PY="$DT_DIR/compare_expect.py"
CHECK_INFLIGHT_PY="$DT_DIR/check_inflight.py"
GATES_PY="$BASE_DIR/tests/q_kyber_gates.py"
MODEL_PY="$BASE_DIR/tests/q_kyber_model.py"

log "=== q_kyber acceptance START ==="
log "device=$TARGET_DEVICE results=$RESULTS_DIR nr_requests=$(cat /sys/block/$FS_DEVICE/queue/nr_requests 2>/dev/null)"

fs_require_vm
fs_detect_device
fs_require_destructive_guard
fs_ensure_bpffs
fs_setup_cgroup_io
fs_cleanup_ioext

if [ ! -x "$FS_LOADER_DIR/loader_q_kyber" ]; then
    blocked "loader" "missing $FS_LOADER_DIR/loader_q_kyber"
    exit 1
fi
command -v bpftrace >/dev/null 2>&1 || { blocked "bpftrace" "not installed in VM"; exit 1; }
command -v python3 >/dev/null 2>&1 || { blocked "python3" "not installed in VM"; exit 1; }

log "--- gate 0: reference model self-test ---"
if timeout 120 python3 "$MODEL_PY" > "$RESULTS_DIR/model.txt" 2>&1; then
    pass "gate0_model_self_test"
else
    fail "gate0_model_self_test" "$(tail -3 "$RESULTS_DIR/model.txt" | tr '\n' ' ')"
    log "=== q_kyber acceptance END: model FAIL ==="
    echo "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED DIAG=$DIAG" >> "$R_FILE"
    exit 1
fi

kyber_trace_driver_args() {
    local trace_file="$1" tier="$2"
    if [ "$tier" -ge 2 ]; then
        local mode batch
        mode=$(python3 -c "import json;d=json.load(open('$trace_file'));print(d.get('defaults',{}).get('submit_mode','sync'))" 2>/dev/null || echo sync)
        if [ "$mode" = "async_batch" ] || [ "$mode" = "async" ]; then
            batch=$(python3 -c "import json;d=json.load(open('$trace_file'));print(d.get('defaults',{}).get('batch_size',8))" 2>/dev/null || echo 8)
            echo "--mode async --batch $batch"
            return
        fi
    fi
    echo ""
}

kyber_dump_merge_cfg() {
    local out="$1"
    : > "$out"
    if "$BT" -j map dump pinned /sys/fs/bpf/kyber_merge_cfg > "$out" 2>/dev/null && [ -s "$out" ]; then
        echo "pinned" > "${out%.json}.method.txt"
        return 0
    fi
    "$BT" -j map dump name kyber_merge_cfg > "$out" 2>/dev/null
    if [ -s "$out" ]; then
        echo "name" > "${out%.json}.method.txt"
        return 0
    fi
    echo '[]' > "$out"
    echo "failed" > "${out%.json}.method.txt"
    return 0
}

kyber_merge_cfg_enable() {
    python3 - "$1" <<'PY'
import json, sys
def le32(vals):
    v = 0
    for i, x in enumerate(vals[:4]):
        v |= int(x, 0) << (8 * i)
    return v
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(-1)
    sys.exit(0)
if isinstance(d, dict):
    d = [d]
for e in d if isinstance(d, list) else []:
    v = e.get('value', {}) if isinstance(e, dict) else {}
    if isinstance(v, dict) and 'enable' in v:
        x = v['enable']
        print(int(x, 0) if isinstance(x, str) else x)
        sys.exit(0)
    if isinstance(v, list):
        print(le32(v))
        sys.exit(0)
    if isinstance(v, str):
        print(le32([v[i:i+2] for i in range(0, min(8, len(v)), 2)]))
        sys.exit(0)
print(-1)
PY
}

# ── P0 rig-order fix: state_map access + CLI target verification ────────
# CLI --*-target-ns values reach the per-hctx state ONLY via the BPF
# init_hctx hook, which the kernel invokes only from the ELEVATOR INIT
# path (blk-mq-sched.c blk_mq_init_sched -> scx_io_init_hctx), never at
# policy attach (attach is a pointer swap, iosched_ext.c iosched_reg_ctx).
# The legacy accept order switched the scheduler BEFORE starting the
# loader, so init_hctx ran policy-less and target_lat_ns[] stayed 0 (the
# CLI targets were INERT; the runtime fallback silently used compile-time
# defaults). kyber_refire_init_hctx() re-fires init_hctx by re-switching
# the elevator with the policy attached (scx_io keeps cgroup links active
# but dormant across runtime scheduler switches, iosched_ext.c
# scx_io_exit_sched), and kyber_assert_targets() verifies via the
# state_map dump that the CLI targets actually landed BEFORE any traffic.
# state_map is an in-object BPF_MAP_TYPE_ARRAY (q_kyber.bpf.c:26-31,
# key=hctx_idx; virtio vda is multi-hctx under this rig — the trace runs on
# its submit CPU's hctx, so it must be
# dumped while the loader is alive. The kernel-tree bpftool -j output is
# either raw hex byte lists or BTF-decoded dicts; both are tolerated
# (precedent: tests/arb_slo_accept.sh).

kyber_state_map_count() {
    # bpftool -j emits a bare object on exactly one match, a list otherwise
    "$BT" -j map show name state_map 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(0); raise SystemExit
if isinstance(d, list):
    print(len(d))
elif isinstance(d, dict):
    print(1 if "id" in d else 0)
else:
    print(0)' 2>/dev/null || echo 0
}

kyber_state_map_dump() {  # out_file (gzip-compressed) -> 0 on success
    local out="$1"
    : > "$out"
    local map_id
    map_id="$("$BT" -j map show name state_map 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(-1); raise SystemExit
if isinstance(d, list) and len(d) == 1:
    print(d[0].get("id", -1))
elif isinstance(d, dict):
    print(d.get("id", -1))
else:
    print(-1)' 2>/dev/null || echo -1)"
    if [ "$map_id" -ge 0 ] 2>/dev/null; then
        "$BT" -j map dump id "$map_id" 2>/dev/null | gzip -c > "$out"
    fi
    if [ ! -s "$out" ]; then
        "$BT" -j map dump name state_map 2>/dev/null | gzip -c > "$out"
    fi
    [ -s "$out" ]
}

# kyber_state_field DUMP FILE FIELD [ENTRY] -> value(s), comma-separated for
# arrays. ENTRY: "0" (default, hctx 0) or "auto" (the entry with the most
# latency-histogram samples, i.e. the hctx the trace actually ran on:
# virtio vda is multi-hctx under this rig and the trace CPU maps to one of
# them, empirically hctx 2 in the 64-vCPU VM). Absolute entry offsets
# (host-verified vs ioext_policy_shared.h, struct q_kyber_state_entry =
# lock(4)+pad(4)+state):
#   cur_domain@28712 batching@28716 inflight@28720 depth_limit@28736
#   async_depth@28816 queue_async_depth@28820 target_lat_ns@28832
#   lat_buckets@28864 window_timeout_ns@29056 domain_p99@29080
kyber_state_field() {
    python3 - "$1" "$2" "${3:-0}" <<'PY' 2>/dev/null
import json, struct, sys, gzip

FIELDS = {
    'cur_domain':        ('I', 1, 28712),
    'batching':          ('I', 1, 28716),
    'inflight':          ('I', 4, 28720),
    'depth_limit':       ('I', 4, 28736),
    'async_depth':       ('I', 1, 28816),
    'queue_async_depth': ('I', 1, 28820),
    'target_lat_ns':     ('Q', 4, 28832),
    'lat_buckets':       ('I', 48, 28864),
    'window_timeout_ns': ('Q', 3, 29056),
    'domain_p99':        ('i', 3, 29080),
}

def key_index(k):
    if isinstance(k, list) and k:
        return int(k[0], 0)
    if isinstance(k, dict):
        return int(k.get('hctx_idx', 0))
    if isinstance(k, int):
        return k
    return None

def to_int(x):
    try:
        return int(x, 0) if isinstance(x, str) else int(x)
    except Exception:
        return None

def field_from_raw(raw, name):
    fmt, n, off = FIELDS[name]
    return list(struct.unpack_from('<' + fmt * n, raw, off))

def flatten(x):
    out = []
    if isinstance(x, list):
        for i in x:
            out.extend(flatten(i))
    else:
        v = to_int(x)
        if v is not None:
            out.append(v)
    return out

dump, name, entry = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(dump, 'rb') as f:
        magic = f.read(2)
    opener = gzip.open if magic == b'\x1f\x8b' else open
    with opener(dump, 'rt') as f:
        entries = json.load(f)
except Exception:
    print(''); raise SystemExit
if isinstance(entries, dict):
    entries = [entries]

parsed = []   # (key, samples, field value)
for e in entries:
    if not isinstance(e, dict):
        continue
    k = key_index(e.get('key', 0))
    if k is None:
        continue
    v = e.get('value')
    fmt = e.get('formatted')
    if isinstance(fmt, dict) and isinstance(fmt.get('value'), dict):
        v = fmt['value']
    if isinstance(v, dict):
        st = v.get('state', v)
        if name in st:
            lat = st.get('lat_buckets')
            samples = sum(flatten(lat)) if lat else 0
            parsed.append((k, samples, st.get(name)))
    elif isinstance(v, list):
        try:
            raw = bytes(to_int(x) for x in v)
        except Exception:
            continue
        if len(raw) >= 29096 and name in FIELDS:
            samples = sum(field_from_raw(raw, 'lat_buckets'))
            parsed.append((k, samples, field_from_raw(raw, name)))

if entry == 'auto':
    with_samples = [p for p in parsed if p[1] > 0]
    if not with_samples:
        print(''); raise SystemExit
    k, samples, fv = max(with_samples, key=lambda p: p[1])
    print(','.join(str(i) for i in [k] + flatten(fv)))
    raise SystemExit

want_key = to_int(entry) or 0
val = None
for k, samples, fv in parsed:
    if k == want_key:
        val = fv
        break
if isinstance(val, list):
    print(','.join(str(i) for i in flatten(val)))
elif val is not None:
    v = to_int(val)
    print(v if v is not None else val)
else:
    print('')
PY
}
# Re-fire the BPF init_hctx by re-switching the elevator with the policy
# attached (P0 fix). The switch resets q->nr_requests to the elevator
# default, so the rig value is restored afterwards.
kyber_refire_init_hctx() {
    local sched_path="$FS_SYS_BLOCK/queue/scheduler"
    echo none > "$sched_path" 2>/dev/null || return 1
    sleep 1
    fs_set_scx_scheduler || return 1
    echo 1023 > "$FS_SYS_BLOCK/queue/nr_requests" 2>/dev/null || true
    return 0
}

# kyber_assert_targets CASE_ID CASE_DIR SPEC ATTEMPT -> 0 iff state_map
# target_lat_ns matches the CLI values in SPEC ("read=V,write=V,...").
kyber_assert_targets() {
    local case_id="$1" case_dir="$2" spec="$3" attempt="$4"
    local count
    count="$(kyber_state_map_count)"
    if [ "$count" != "1" ]; then
        log "[$case_id attempt $attempt] state_map show hits=$count (expected exactly 1)"
        return 1
    fi
    local dump="$case_dir/state_map_post_toggle.json"
    kyber_state_map_dump "$dump" || return 1
    local targets clause want got idx
    targets="$(kyber_state_field "$dump" target_lat_ns)"
    log "[$case_id attempt $attempt] post-toggle target_lat_ns=[$targets] (spec: $spec)"
    for clause in ${spec//,/ }; do
        [ -n "$clause" ] || continue
        case "$clause" in
            read=*)    idx=1;;
            write=*)   idx=2;;
            discard=*) idx=3;;
            other=*)   idx=4;;
            *) continue;;
        esac
        want="${clause#*=}"
        got="$(echo "$targets" | cut -d, -f$idx)"
        if [ "$got" != "$want" ]; then
            log "[$case_id attempt $attempt] target verify FAIL: $clause got=$got"
            return 1
        fi
    done
    return 0
}

# run_one: full closed-loop case execution. Returns 0 on PASS verdict.
# kind: t1_strict | t2_strict | t2_merge_diag | t1_flake
# want_targets: optional "read=V,write=V,..." spec; when non-empty the
# scheduler is re-switched after attach (P0 fix) and the CLI targets are
# verified against the state_map dump before any traffic is driven.
run_one() {
    local case_id="$1" trace_file="$2" expect_file="$3" tier="$4" extra="$5"
    local merge_expect="$6" kind="$7" attempt="$8" want_targets="${9:-}"
    local case_dir="$RESULTS_DIR/${case_id}_attempt${attempt}"
    mkdir -p "$case_dir"
    local loader_log="$case_dir/loader.log"
    local bpftrace_log="$case_dir/bpftrace.log"
    local trace_out="$case_dir/trace_output.txt"
    local compare_out="$case_dir/compare.json"
    local inflight_out="$case_dir/inflight.json"
    local gates_out="$case_dir/g_gates.json"
    local mark
    mark="$(fs_dmesg_mark "q_kyber_${case_id}_${attempt}")"

    fs_set_scx_scheduler || { fail "$case_id" "cannot switch to scx_io (attempt $attempt)"; return 1; }
    echo 1023 > "/sys/block/$FS_DEVICE/queue/nr_requests" 2>/dev/null || \
        log "WARN: cannot raise nr_requests on $FS_DEVICE"

    fs_start_loader q_kyber "$loader_log" $extra
    if ! fs_wait_attached q_kyber "$FS_LOADER_PID" 15; then
        cat "$loader_log" >&2
        fs_stop_loader "$FS_LOADER_PID"
        fail "$case_id" "loader attach timeout (attempt $attempt)"
        return 1
    fi

    if [ -n "$want_targets" ]; then
        # P0 evidence: before the re-toggle the CLI targets are inert
        kyber_state_map_dump "$case_dir/state_map_pre_toggle.json" || true
        if ! kyber_refire_init_hctx; then
            fs_stop_loader "$FS_LOADER_PID"
            fail "$case_id" "scheduler re-toggle failed (attempt $attempt)"
            return 1
        fi
        if ! kyber_assert_targets "$case_id" "$case_dir" "$want_targets" "$attempt"; then
            fs_stop_loader "$FS_LOADER_PID"
            fail "$case_id" "target verification failed (attempt $attempt)"
            return 1
        fi
    fi

    if [ -n "$merge_expect" ]; then
        local cfg_dump="$case_dir/kyber_merge_cfg.json"
        ls -la /sys/fs/bpf/kyber_merge_cfg > "$case_dir/kyber_merge_cfg.pin.txt" 2>&1 || true
        kyber_dump_merge_cfg "$cfg_dump"
        local enable
        enable="$(kyber_merge_cfg_enable "$cfg_dump")"
        if [ "$enable" != "$merge_expect" ]; then
            fail "$case_id" "merge_cfg enable=$enable != expected $merge_expect (attempt $attempt)"
            cat "$cfg_dump" >&2
            fs_stop_loader "$FS_LOADER_PID"
            return 1
        fi
        diag "$case_id merge_cfg enable==$merge_expect verified via bpftool (attempt $attempt)"
    fi

    bpftrace "$BPFTRACE_SCRIPT" > "$bpftrace_log" 2>&1 &
    local bpf_pid=$!
    # Wait until bpftrace reports probes attached: on the first run of a
    # cold VM the first bpftrace invocation can take >1s to attach and the
    # trace would play before any probe was live (observed: T1 attempt 1
    # captured zero trace events, 2026-09-01).
    local wait_i
    for wait_i in $(seq 1 20); do
        grep -q 'Attaching' "$bpftrace_log" 2>/dev/null && break
        sleep 0.5
    done
    sleep 1

    local driver_args
    driver_args="$(kyber_trace_driver_args "$trace_file" "$tier")"
    log "[$case_id attempt $attempt] trace_driver args: $driver_args"
    "$TRACE_DRIVER" "$FS_DEV_PATH" "$trace_file" $driver_args > "$trace_out" 2>&1 || true

    sleep 1
    kill -INT "$bpf_pid" 2>/dev/null || true
    wait "$bpf_pid" 2>/dev/null || true
    grep -E 'TRACE_PLAY_(START|END)' "$trace_out" >> "$bpftrace_log" 2>/dev/null || true

    fs_stop_loader "$FS_LOADER_PID"

    if fs_dmesg_has_errors "$mark"; then
        fs_dmesg_snapshot "$mark" > "$case_dir/dmesg_errors.txt" 2>/dev/null
        fail "$case_id" "dmesg errors (attempt $attempt)"
        return 1
    fi

    if [ ! -s "$bpftrace_log" ]; then
        fail "$case_id" "bpftrace log empty (attempt $attempt)"
        return 1
    fi
    if ! grep -q 'TRACE_PLAY_START' "$trace_out"; then
        fail "$case_id" "no TRACE_PLAY_START marker (attempt $attempt)"
        cat "$trace_out" >&2 || true
        return 1
    fi

    local compare_status="FAIL"
    local g1_pass=false g2_ok=false

    if [ "$tier" -ge 2 ]; then
        if ! python3 "$CHECK_INFLIGHT_PY" --bpftrace-log "$bpftrace_log" \
                --trace "$trace_file" --out "$inflight_out" --min-inflight 2 \
                > "$case_dir/inflight_stdout.txt" 2>&1; then
            fail "$case_id" "G1 FAIL scheduler_inactive max_inflight<2 (attempt $attempt)"
            cat "$inflight_out" 2>/dev/null >&2 || true
            return 1
        fi
        g1_pass=true
    fi

    local merge_flag=""
    [ "$kind" = "t2_merge_diag" ] && merge_flag="--merge-case"
    if [ "$tier" -ge 2 ]; then
        if python3 "$GATES_PY" --bpftrace-log "$bpftrace_log" --trace "$trace_file" \
                --out "$gates_out" $merge_flag > "$case_dir/g_gates_stdout.txt" 2>&1; then
            g2_ok=true
        else
            g2_ok=false
        fi
    fi

    local stats_arg=""
    local stats_file="$case_dir/bpftool_stats.json"
    if [ "$tier" -ge 1 ]; then
        "$BT" -j map dump pinned /sys/fs/bpf/kyber_merge_cfg > "$stats_file" 2>/dev/null || true
        [ -s "$stats_file" ] && stats_arg="--bpftool-stats $stats_file"
    fi

    python3 "$COMPARE_PY" --bpftrace-log "$bpftrace_log" \
        --expect "$expect_file" --trace "$trace_file" \
        --policy q_kyber --tier "$tier" $stats_arg \
        --out "$compare_out" > "$case_dir/compare_stdout.txt" 2>&1 || true
    compare_status=$(python3 -c "import json;print(json.load(open('$compare_out')).get('status','FAIL'))" 2>/dev/null || echo FAIL)

    local insert_count issue_count complete_count
    insert_count=$(python3 -c "import json;print(json.load(open('$compare_out'))['details'].get('insert_count',-1))" 2>/dev/null || echo -1)
    issue_count=$(python3 -c "import json;print(json.load(open('$compare_out'))['details'].get('issue_count',-1))" 2>/dev/null || echo -1)
    complete_count=$(python3 -c "import json;print(json.load(open('$compare_out'))['details'].get('complete_count',-1))" 2>/dev/null || echo -1)
    log "[$case_id attempt $attempt] compare=$compare_status insert=$insert_count issue=$issue_count complete=$complete_count g1=$g1_pass g2=$g2_ok"

    if [ "$kind" = "t1_flake" ]; then
        if [ "$compare_status" = "FAIL" ] && grep -q "insert_count=0" "$case_dir/compare_stdout.txt" 2>/dev/null; then
            diag "$case_id attempt $attempt REPRODUCED insert_count=0 (issue=$issue_count complete=$complete_count)"
            return 2
        fi
        diag "$case_id attempt $attempt NOT-REPRODUCED (compare=$compare_status insert=$insert_count)"
        return 3
    fi

    if [ "$compare_status" != "PASS" ]; then
        fail "$case_id" "compare status=$compare_status (attempt $attempt)"
        cat "$compare_out" >&2 2>/dev/null || true
        return 1
    fi

    if [ "$kind" = "t1_strict" ]; then
        pass "$case_id (attempt $attempt, insert_tp=$insert_count)"
        return 0
    fi

    if ! $g1_pass; then
        fail "$case_id" "G1 not recorded (attempt $attempt)"
        return 1
    fi
    if ! $g2_ok; then
        fail "$case_id" "G2/G3/G4 gates failed (attempt $attempt) — see $gates_out"
        cat "$gates_out" >&2 2>/dev/null || true
        return 1
    fi
    pass "$case_id (attempt $attempt)"
    return 0
}

# run_depth_case: Tier-1 dynamic-depth case (native kyber token law).
# The final state dump is taken BEFORE fs_stop_loader: state_map is an
# in-object map and dies with the loader's BPF object.
# depth_spec: "R_LT=256;W_EQ=128;D_EQ=64;O_EQ=16" (R/W/D/O = READ/WRITE/
# DISCARD/OTHER depth_limit; _LT asserts 1 <= got < want, _EQ equality).
run_depth_case() {
    local case_id="$1" trace_file="$2" expect_file="$3" extra="$4"
    local merge_expect="$5" targets_spec="$6" depth_spec="$7" attempt="$8"
    local case_dir="$RESULTS_DIR/${case_id}_attempt${attempt}"
    mkdir -p "$case_dir"
    local loader_log="$case_dir/loader.log"
    local bpftrace_log="$case_dir/bpftrace.log"
    local trace_out="$case_dir/trace_output.txt"
    local compare_out="$case_dir/compare.json"
    local mark
    mark="$(fs_dmesg_mark "q_kyber_${case_id}_${attempt}")"

    fs_set_scx_scheduler || { fail "$case_id" "cannot switch to scx_io (attempt $attempt)"; return 1; }
    echo 1023 > "/sys/block/$FS_DEVICE/queue/nr_requests" 2>/dev/null || \
        log "WARN: cannot raise nr_requests on $FS_DEVICE"

    fs_start_loader q_kyber "$loader_log" $extra
    if ! fs_wait_attached q_kyber "$FS_LOADER_PID" 15; then
        cat "$loader_log" >&2
        fs_stop_loader "$FS_LOADER_PID"
        fail "$case_id" "loader attach timeout (attempt $attempt)"
        return 1
    fi

    if [ -n "$merge_expect" ]; then
        local cfg_dump="$case_dir/kyber_merge_cfg.json"
        kyber_dump_merge_cfg "$cfg_dump"
        local enable
        enable="$(kyber_merge_cfg_enable "$cfg_dump")"
        if [ "$enable" != "$merge_expect" ]; then
            fail "$case_id" "merge_cfg enable=$enable != expected $merge_expect (attempt $attempt)"
            fs_stop_loader "$FS_LOADER_PID"
            return 1
        fi
    fi

    kyber_state_map_dump "$case_dir/state_map_pre_toggle.json" || true
    if ! kyber_refire_init_hctx; then
        fs_stop_loader "$FS_LOADER_PID"
        fail "$case_id" "scheduler re-toggle failed (attempt $attempt)"
        return 1
    fi
    if ! kyber_assert_targets "$case_id" "$case_dir" "$targets_spec" "$attempt"; then
        fs_stop_loader "$FS_LOADER_PID"
        fail "$case_id" "target verification failed (attempt $attempt)"
        return 1
    fi

    local init_dump="$case_dir/state_map_init.json"
    kyber_state_map_dump "$init_dump" || true
    local init_dl
    init_dl="$(kyber_state_field "$init_dump" depth_limit)"
    log "[$case_id attempt $attempt] post-toggle initial depth_limit=[$init_dl]"
    if [ "$init_dl" != "256,128,64,16" ]; then
        fs_stop_loader "$FS_LOADER_PID"
        fail "$case_id" "initial depth_limit [$init_dl] != native caps 256,128,64,16 (attempt $attempt)"
        return 1
    fi

    bpftrace "$BPFTRACE_SCRIPT" > "$bpftrace_log" 2>&1 &
    local bpf_pid=$!
    local wait_i
    for wait_i in $(seq 1 20); do
        grep -q 'Attaching' "$bpftrace_log" 2>/dev/null && break
        sleep 0.5
    done
    sleep 1

    local driver_args
    driver_args="$(kyber_trace_driver_args "$trace_file" 2)"
    log "[$case_id attempt $attempt] trace_driver args: $driver_args"
    "$TRACE_DRIVER" "$FS_DEV_PATH" "$trace_file" $driver_args > "$trace_out" 2>&1 || true

    sleep 1
    kill -INT "$bpf_pid" 2>/dev/null || true
    wait "$bpf_pid" 2>/dev/null || true
    grep -E 'TRACE_PLAY_(START|END)' "$trace_out" >> "$bpftrace_log" 2>/dev/null || true

    local final_dump="$case_dir/state_map_final.json"
    if ! kyber_state_map_dump "$final_dump"; then
        fs_stop_loader "$FS_LOADER_PID"
        fail "$case_id" "final state_map dump failed (attempt $attempt)"
        return 1
    fi
    fs_stop_loader "$FS_LOADER_PID"

    if fs_dmesg_has_errors "$mark"; then
        fs_dmesg_snapshot "$mark" > "$case_dir/dmesg_errors.txt" 2>/dev/null
        fail "$case_id" "dmesg errors (attempt $attempt)"
        return 1
    fi
    if ! grep -q 'TRACE_PLAY_START' "$trace_out"; then
        fail "$case_id" "no TRACE_PLAY_START marker (attempt $attempt)"
        cat "$trace_out" >&2 || true
        return 1
    fi

    local compare_status="FAIL"
    python3 "$COMPARE_PY" --bpftrace-log "$bpftrace_log" \
        --expect "$expect_file" --trace "$trace_file" \
        --policy q_kyber --tier 1 \
        --out "$compare_out" > "$case_dir/compare_stdout.txt" 2>&1 || true
    compare_status=$(python3 -c "import json;print(json.load(open('$compare_out')).get('status','FAIL'))" 2>/dev/null || echo FAIL)
    log "[$case_id attempt $attempt] compare=$compare_status"
    if [ "$compare_status" != "PASS" ]; then
        fail "$case_id" "compare status=$compare_status (attempt $attempt)"
        cat "$compare_out" >&2 2>/dev/null || true
        return 1
    fi

    local dl il d0 d1 d2 d3 i0 i1 i2 i3 p99 clause name want got spec_ok=0
    # virtio vda is multi-hctx: the trace runs on ONE hctx (its CPU's map);
    # "auto" picks the entry with the most latency samples and prefixes the
    # picked key, recorded as evidence.
    dl="$(kyber_state_field "$final_dump" depth_limit auto)"
    il="$(kyber_state_field "$final_dump" inflight auto)"
    p99="$(kyber_state_field "$final_dump" domain_p99 auto)"
    local trace_hctx
    trace_hctx="$(echo "$dl" | cut -d, -f1)"
    log "[$case_id attempt $attempt] FINAL trace_hctx=$trace_hctx depth_limit=[$(echo "$dl" | cut -d, -f2-)] inflight=[$(echo "$il" | cut -d, -f2-)] domain_p99=[$(echo "$p99" | cut -d, -f2-)]"
    IFS=',' read -r _ d0 d1 d2 d3 <<< "$dl"
    IFS=',' read -r _ i0 i1 i2 i3 <<< "$il"
    for clause in ${depth_spec//;/ }; do
        [ -n "$clause" ] || continue
        name="${clause%%_*}"
        want="${clause#*=}"
        case "$name" in
            R) got=$d0;; W) got=$d1;; D) got=$d2;; O) got=$d3;;
            *) continue;;
        esac
        case "$clause" in
            *_LT=*)
                if [ "$got" -ge 1 ] 2>/dev/null && [ "$got" -lt "$want" ] 2>/dev/null; then
                    log "[$case_id attempt $attempt] depth assert ok: $clause (got=$got)"
                else
                    log "[$case_id attempt $attempt] depth assert FAIL: $clause (got=$got)"
                    spec_ok=1
                fi
                ;;
            *_EQ=*)
                if [ "$got" -eq "$want" ] 2>/dev/null; then
                    log "[$case_id attempt $attempt] depth assert ok: $clause (got=$got)"
                else
                    log "[$case_id attempt $attempt] depth assert FAIL: $clause (got=$got)"
                    spec_ok=1
                fi
                ;;
        esac
    done
    if [ "$i0" -le "$d0" ] 2>/dev/null && [ "$i1" -le "$d1" ] 2>/dev/null && \
       [ "$i2" -le "$d2" ] 2>/dev/null && [ "$i3" -le "$d3" ] 2>/dev/null; then
        log "[$case_id attempt $attempt] inflight<=depth_limit holds for all domains"
    else
        log "[$case_id attempt $attempt] inflight<=depth_limit VIOLATED"
        spec_ok=1
    fi

    if [ "$spec_ok" -ne 0 ]; then
        fail "$case_id" "depth assertions failed (attempt $attempt) — see $final_dump"
        return 1
    fi
    pass "$case_id (attempt $attempt)"
    return 0
}

# D8 probe: prove the insert path fires on kprobe:scx_io_insert_requests
# while tracepoint block:block_rq_insert stays silent for the same IO.
run_insert_probe() {
    local case_id="KYBER_INS_PROBE"
    local case_dir="$RESULTS_DIR/${case_id}"
    mkdir -p "$case_dir"
    local trace_file="$DT_DIR/traces/micro/kyber_read_fifo_8.json"
    local loader_log="$case_dir/loader.log"
    local probe_log="$case_dir/probe.log"
    local trace_out="$case_dir/trace_output.txt"

    fs_set_scx_scheduler || { diag "$case_id: no scx_io"; return 1; }
    echo 1023 > "/sys/block/$FS_DEVICE/queue/nr_requests" 2>/dev/null || true
    fs_start_loader q_kyber "$loader_log" --merge-enable=0
    fs_wait_attached q_kyber "$FS_LOADER_PID" 15 || { fs_stop_loader "$FS_LOADER_PID"; diag "$case_id: attach timeout"; return 1; }

    bpftrace -e 'BEGIN { printf("PROBE_START\n"); }
                 kprobe:scx_io_insert_requests { printf("SCXINS ts=%llu\n", nsecs); }
                 tracepoint:block:block_rq_insert { printf("BLKINS ts=%llu\n", nsecs); }
                 tracepoint:block:block_rq_issue { printf("ISSUE ts=%llu sector=%llu nr=%llu rwbs=%s\n", nsecs, args->sector, args->nr_sector, args->rwbs); }' \
        > "$probe_log" 2>&1 &
    local bpf_pid=$!
    local wait_i
    for wait_i in $(seq 1 20); do
        grep -q 'Attaching' "$probe_log" 2>/dev/null && break
        sleep 0.5
    done
    sleep 2
    "$TRACE_DRIVER" "$FS_DEV_PATH" "$trace_file" --mode async --batch 8 > "$trace_out" 2>&1 || true
    sleep 1
    kill -INT "$bpf_pid" 2>/dev/null || true
    wait "$bpf_pid" 2>/dev/null || true
    fs_stop_loader "$FS_LOADER_PID"

    local scxins blkins trace_issues
    scxins=$(awk '/^SCXINS/{c++} END{print c+0}' "$probe_log" 2>/dev/null)
    blkins=$(awk '/^BLKINS/{c++} END{print c+0}' "$probe_log" 2>/dev/null)
    trace_issues=$(grep '^ISSUE' "$probe_log" | awk -F'sector=' '{split($2,a," "); print a[1]}' | \
        python3 -c "
import sys, json
tr = json.load(open('$trace_file'))
sectors = set(r['sector'] for r in tr['requests'])
print(sum(1 for l in sys.stdin if l.strip() and int(l.strip()) in sectors))" 2>/dev/null || echo 0)
    local verdict
    if [ "$scxins" -gt 0 ] && [ "$blkins" = "0" ]; then
        verdict="PROVEN: scx_io insert path fired $scxins times (kprobe:scx_io_insert_requests) while block:block_rq_insert tracepoint fired 0 times for $trace_issues trace-sector ISSUEs"
    elif [ "$scxins" = "0" ]; then
        verdict="INCONCLUSIVE: kprobe scx_io_insert_requests never fired (symbol absent/inlined?); blkins=$blkins"
    else
        verdict="REFUTED: block_rq_insert fired $blkins times (hypothesis wrong)"
    fi
    diag "$case_id: $verdict"
    printf '{"scx_insert_kprobe_count":%s,"block_rq_insert_tp_count":%s,"trace_sector_issues":%s,"verdict":"%s"}\n' \
        "$scxins" "$blkins" "$trace_issues" "$verdict" > "$case_dir/probe_summary.json"
    grep -E '^(SCXINS|BLKINS|ISSUE)' "$probe_log" | head -30 > "$case_dir/probe_excerpt.txt" 2>/dev/null || true
    return 0
}

# CASES: case_id|trace|expect|tier|extra_loader_args|merge_enable|kind|want_targets
# (want_targets non-empty -> P0 re-toggle + state_map target verification)
CASES=(
    "KYBER_T1_COUNTS|traces/micro/kyber_read_fifo_8.json|expect/partial/kyber_t1_counts.json|1|--merge-enable=0|0|t1_strict|"
    "KYBER_K1_FIFO|traces/micro/kyber_read_fifo_8.json|expect/golden/kyber_read_fifo_8.json|2|--merge-enable=0|0|t2_strict|"
    "KYBER_K2_RW|traces/micro/kyber_rw_domains_8.json|expect/partial/kyber_rw_domains_8.json|2|--merge-enable=0 --read-target-ns 100000000000000 --write-target-ns 100000000000000|0|t2_strict|read=100000000000000,write=100000000000000"
    "KYBER_K3_MERGE_DIAG|traces/micro/kyber_adjacent_merge_8.json|expect/partial/kyber_merge_cov.json|2|--merge-enable=1|1|t2_merge_diag|"
    "KYBER_DT_I01_RERUN|traces/micro/adjacent_read_8.json|expect/manual/kyber_merge_on.json|1|--merge-enable=1|1|t1_flake|"
    "KYBER_DT_I02_RERUN|traces/micro/adjacent_read_8.json|expect/manual/kyber_merge_off.json|1|--merge-enable=0|0|t1_flake|"
)

# DEPTH_CASES: case_id|trace|expect|tier|extra_loader_args|merge_enable|targets_spec|depth_spec
# Tier-1 native-law dynamic-depth cases (P3), all gated on the P0 target
# verification. Depth law (kyber-iosched.c): initial caps {256,128,64,16};
# resize fires iff congestion (p90-io of ANY domain >= bucket 4) || p99 of
# the domain's TOTAL latency >= 4; new = current*(p99+1)>>2 clamped to
# [1, cap]. Window: >=500 samples per domain or >1s since the window's
# first evaluation attempt; the trace fires both windows by sample count.
DEPTH_CASES=(
    "KYBER_DEPTH_SHRINK|traces/micro/kyber_depth_mixed_1608.json|expect/partial/kyber_depth_counts.json|1|--merge-enable=0 --read-target-ns 100000000000000 --write-target-ns 1000|0|read=100000000000000,write=1000|R_LT=256;W_EQ=128;D_EQ=64;O_EQ=16"
    "KYBER_DEPTH_HOLD|traces/micro/kyber_depth_mixed_1608.json|expect/partial/kyber_depth_counts.json|1|--merge-enable=0 --read-target-ns 100000000000000 --write-target-ns 100000000000000|0|read=100000000000000,write=100000000000000|R_EQ=256;W_EQ=128;D_EQ=64;O_EQ=16"
)

declare -A CASE_VERDICT
for entry in "${CASES[@]}"; do
    IFS='|' read -r case_id trace_rel expect_rel tier extra merge_expect kind want_targets <<< "$entry"
    trace_file="$DT_DIR/$trace_rel"
    expect_file="$DT_DIR/$expect_rel"
    if [ ! -f "$trace_file" ]; then
        blocked "$case_id" "trace missing: $trace_rel"
        CASE_VERDICT[$case_id]="BLOCKED"
        continue
    fi
    if [ ! -f "$expect_file" ]; then
        blocked "$case_id" "expect missing: $expect_rel"
        CASE_VERDICT[$case_id]="BLOCKED"
        continue
    fi

    attempts=5
    [ "$kind" = "t1_flake" ] && attempts=3
    verdict="BLOCKED"
    repro=0; notrepro=0
    for attempt in $(seq 1 "$attempts"); do
        log "=== $case_id attempt $attempt/$attempts (kind=$kind) ==="
        run_one "$case_id" "$trace_file" "$expect_file" "$tier" "$extra" "$merge_expect" "$kind" "$attempt" "$want_targets"
        rc=$?
        if [ "$kind" = "t1_flake" ]; then
            [ "$rc" -eq 2 ] && repro=$((repro+1))
            [ "$rc" -eq 3 ] && notrepro=$((notrepro+1))
            continue
        fi
        if [ $rc -eq 0 ]; then
            verdict="PASS"
            break
        fi
        fs_cleanup_ioext
        sleep 1
    done
    if [ "$kind" = "t1_flake" ]; then
        if [ "$repro" -gt 0 ]; then
            CASE_VERDICT[$case_id]="FLAKE-REPRODUCED(insert_count=0) in $repro/$attempts attempts"
        else
            CASE_VERDICT[$case_id]="FLAKE-NOT-REPRODUCED in $attempts attempts"
        fi
        diag "$case_id final: ${CASE_VERDICT[$case_id]}"
    else
        if [ "$verdict" != "PASS" ]; then
            CASE_VERDICT[$case_id]="FAIL(after $attempts attempts)"
            record "FINAL $case_id: ${CASE_VERDICT[$case_id]}"
        else
            CASE_VERDICT[$case_id]="PASS"
            record "FINAL $case_id: PASS"
        fi
    fi
    fs_cleanup_ioext
    sleep 1
done

for entry in "${DEPTH_CASES[@]}"; do
    IFS='|' read -r case_id trace_rel expect_rel tier extra merge_expect targets_spec depth_spec <<< "$entry"
    trace_file="$DT_DIR/$trace_rel"
    expect_file="$DT_DIR/$expect_rel"
    if [ ! -f "$trace_file" ] || [ ! -f "$expect_file" ]; then
        blocked "$case_id" "trace/expect missing: $trace_rel / $expect_rel"
        CASE_VERDICT[$case_id]="BLOCKED"
        continue
    fi
    verdict="BLOCKED"
    for attempt in 1 2 3 4 5; do
        log "=== $case_id attempt $attempt/5 (kind=t1_depth) ==="
        run_depth_case "$case_id" "$trace_file" "$expect_file" "$extra" \
                       "$merge_expect" "$targets_spec" "$depth_spec" "$attempt"
        if [ $? -eq 0 ]; then
            verdict="PASS"
            break
        fi
        fs_cleanup_ioext
        sleep 1
    done
    if [ "$verdict" != "PASS" ]; then
        CASE_VERDICT[$case_id]="FAIL(after 5 attempts)"
        record "FINAL $case_id: ${CASE_VERDICT[$case_id]}"
    else
        CASE_VERDICT[$case_id]="PASS"
        record "FINAL $case_id: PASS"
    fi
    fs_cleanup_ioext
    sleep 1
done

run_insert_probe
fs_cleanup_ioext

echo "" >> "$R_FILE"
echo "## Summary $(date)" >> "$R_FILE"
for k in "${!CASE_VERDICT[@]}"; do
    echo "$k: ${CASE_VERDICT[$k]}" >> "$R_FILE"
done
echo "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED DIAG=$DIAG" >> "$R_FILE"
{
    printf '{\n  "results_dir": "%s",\n  "verdicts": {\n' "$RESULTS_DIR"
    first=1
    for k in "${!CASE_VERDICT[@]}"; do
        v="${CASE_VERDICT[$k]}"
        v="${v//\"/\\\"}"
        if [ $first -eq 1 ]; then first=0; else printf ',\n'; fi
        printf '    "%s": "%s"' "$k" "$v"
    done
    printf '\n  },\n  "pass": %s, "fail": %s, "blocked": %s, "diag": %s\n}\n' \
        "$PASS" "$FAIL" "$BLOCKED" "$DIAG"
} > "$RESULTS_DIR/summary.json"

log "=== q_kyber acceptance END: PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED DIAG=$DIAG ==="
log "results: $RESULTS_DIR"
cat "$R_FILE"

strict_fail=0
for k in "${!CASE_VERDICT[@]}"; do
    case "${CASE_VERDICT[$k]}" in
        PASS|FLAKE-*) ;;
        *) strict_fail=$((strict_fail+1)) ;;
    esac
done
[ "$strict_fail" -eq 0 ] && exit 0 || exit 1