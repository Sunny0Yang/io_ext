#!/usr/bin/env bash
# scripts/run_dyn_tenant_kvmhost.sh
# Control-host wrapper: sync loaders + ioext-bench, then run the dynamic-tenant
# timeline sequences on kvmhost (dedicated guest, /dev/sdb 96G, bare-metal-grade
# per the campaign convention) — the main-experiment pipeline ported off the
# local virtme QEMU disk (user decision 2026-11-04: local multi-layer
# virtualization makes tuning numbers untrustworthy; recalibrate on sdb).
#
# Conventions follow the provisioning archive
# (ioext-ssh/ssh_env.sh: ssh_cmd/scp_cmd/
# remote_sudo; finish_step1.sh: PERF_ARENA_WORKSPACE + PYTHONPATH +
# SUDO_PASSWORD + .venv/bin/python -m vm.executor).
#
# SAFETY GUARDS (hard failures, checked before anything runs):
#   - /dev/sdb exists, size == 103079215104 bytes, NOT mounted, not "/", not
#     the root device; holders empty.
#   - one-time whole-disk fill (marker .dyn_tenant_sdb_filled under results)
#     so raw randread sees real media (paper §III preconditioning analogue).
#
# Usage:
#   flock -w 7200 /tmp/dyn_tenant_kvmhost.lock scripts/run_dyn_tenant_kvmhost.sh \
#       [--sync-only] [--skip-sync] [--config dynamic_tenant_smoke.yml] [SEQ ...]
# Configs: dynamic_tenant_smoke.yml (DYN_smoke default), *_calibration.yml,
# *_virtme.yml, *_static_arms.yml (all generated fresh for sdb, no local-calib
# rates — sdb re-freezes its own contract).
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE_DIR="${ARCHIVE_DIR:-/home/anon/ioext-ssh}"
SSH_ENV="${SSH_ENV:-$ARCHIVE_DIR/ssh_env.sh}"
IOEXT_WT="${IOEXT_WORKSPACE:-$BASE_DIR}"
IOEXT_BUILD="${IOEXT_BUILD:-$IOEXT_WT/userspace/build}"
GUEST_ROOT="/opt/ioext-bench"
REMOTE_RESULTS_REL="results/dyn_tenant_kvmhost"
LOCAL_RESULTS="$BASE_DIR/results/dyn_tenant_kvmhost"
SDB_EXPECTED_BYTES=103079215104
SYNC_ONLY=0
SKIP_SYNC=0
RECOVER_ONLY=0
NO_FILL="${DYN_TENANT_NO_FILL:-0}"
CONFIG="dynamic_tenant_smoke.yml"

while [ $# -gt 0 ]; do
    case "$1" in
        --sync-only) SYNC_ONLY=1; shift ;;
        --skip-sync) SKIP_SYNC=1; shift ;;
        --recover-only) RECOVER_ONLY=1; shift ;;
        --no-fill) NO_FILL=1; shift ;;
        --config) CONFIG="$2"; shift 2 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) break ;;
    esac
done
SEQS="${*:-DYN_smoke}"

# shellcheck disable=SC1090
source "$SSH_ENV"

# ---- device discovery: the 96G experiment disk's sdX letter is NOT stable
# across reboots (post-reboot 20261104: sdb->sdc). Identify by exact size +
# no mount/holders; refuse on ambiguity.
SDB_EXPECTED_SECTORS=201326592  # 103079215104 / 512
DISCOVERED_DEV=""

# remote_sudo_script: run a root script on the remote. Script travels as a
# base64 ARGUMENT (the archive remote_sudo pattern); sudo -S reads ONLY the
# password pipe -- a heredoc as ssh stdin would be swallowed by sudo (bug
# observed 20261104: device discovery got an empty script).
remote_sudo_script() {
    local script="$1"
    local pw_b64 cmd_b64
    pw_b64=$(printf '%s\n' "$SUDO_PASS" | base64 -w0)
    cmd_b64=$(printf '%s' "$script" | base64 -w0)
    ssh -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" \
        "echo ${pw_b64} | base64 -d | sudo -S -p '' bash -c \"echo ${cmd_b64} | base64 -d | bash\""
}

discover_device() {
    local script
    script=$(cat <<'EOS'
set -euo pipefail
matches=()
for syseq in /sys/class/block/sd*; do
    d=${syseq#/sys/class/block/}
    [ -e "$syseq/size" ] || continue
    size=$(cat "$syseq/size")
    [ "$size" = "201326592" ] || continue
    holders=$(ls "$syseq/holders" 2>/dev/null | wc -l)
    parts=$(ls /sys/class/block/ | grep -c "^${d}[0-9]" || true)
    if [ "$holders" != "0" ] || [ "$parts" != "0" ]; then
        echo "GUARD FAIL: $d has holders/parts" >&2; exit 1
    fi
    matches+=("$d")
done
if [ ${#matches[@]} -ne 1 ]; then
    echo "GUARD FAIL: expected exactly one 96G experiment disk, found ${#matches[@]}: ${matches[*]:-none}" >&2
    exit 1
fi
echo "${matches[0]}"
EOS
)
    DISCOVERED_DEV="$(remote_sudo_script "$script")" || DISCOVERED_DEV=""
    case "$DISCOVERED_DEV" in
        sd*) echo "experiment disk discovered: /dev/$DISCOVERED_DEV" ;;
        *) echo "device discovery failed: $DISCOVERED_DEV" >&2; exit 1 ;;
    esac
}

sync_all() {
    echo "=== rsync loaders (incl. arb_feedback) ==="
    rsync -az -e "ssh -p $SSH_PORT" "$IOEXT_BUILD"/loader_* "$SSH_USER@$SSH_HOST:/opt/io-ext/userspace/"
    echo "=== rsync ioext-bench repo ==="
    rsync -az -e "ssh -p $SSH_PORT" --exclude .venv --exclude results \
        --exclude '__pycache__' --exclude '*.pyc' --exclude .git \
        "$BASE_DIR/" "$SSH_USER@$SSH_HOST:$GUEST_ROOT/" \
        || { echo "warn: rsync repo non-zero" >&2; }
    echo "=== generate + push device variant task configs ==="
    # Default: no calibration contract (remote pipeline re-freezes its own).
    # DYN_TENANT_RUNNER_CALIB_JSON=<path>: generate WITH that frozen contract
    # (platform-correct L + per-tenant rates) — used by the static-arms stage.
    local gen_env=(DYN_TENANT_TARGET_DEVICE="$DISCOVERED_DEV" DYN_TENANT_NO_CALIB=1)
    [ -z "${DYN_TENANT_RUNNER_CALIB_JSON:-}" ] || gen_env=(DYN_TENANT_TARGET_DEVICE="$DISCOVERED_DEV" DYN_TENANT_CALIB_JSON="$DYN_TENANT_RUNNER_CALIB_JSON")
    # C4 needs measured io.cost coefficients (DYN_TENANT_IOCOST_COEF_JSON):
    # pass it through when set; without it the generator aborts on C4 with a
    # clear message, which is the designed behavior (measured constants only).
    [ -z "${DYN_TENANT_IOCOST_COEF_JSON+x}" ] || gen_env+=("DYN_TENANT_IOCOST_COEF_JSON=$DYN_TENANT_IOCOST_COEF_JSON")
    env "${gen_env[@]}" \
        python3 "$BASE_DIR/scripts/gen_dynamic_tenant_yamls.py" \
        --output-dir /tmp/dyn_tenant_kvmhost_yaml > /tmp/dyn_tenant_kvmhost_gen.log 2>&1 \
        || { echo "config generation FAILED:" >&2; tail -5 /tmp/dyn_tenant_kvmhost_gen.log; return 1; }
    grep -q "target_device: $DISCOVERED_DEV" /tmp/dyn_tenant_kvmhost_yaml/dynamic_tenant_smoke.yml \
        || { echo "generated config missing target_device: $DISCOVERED_DEV — refusing to push" >&2; return 1; }
    ssh -n -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" \
        "printf '%s\\n' '$SUDO_PASS' | sudo -S -p '' bash -c 'rm -f $GUEST_ROOT/src/vm/configs/task/isol_bench/rig_dynamic_tenant/*.yml' 2>/dev/null; true"
    scp -P "$SSH_PORT" -q /tmp/dyn_tenant_kvmhost_yaml/*.yml \
        "$SSH_USER@$SSH_HOST:$GUEST_ROOT/src/vm/configs/task/isol_bench/rig_dynamic_tenant/kvmhost/"
    echo "sync complete (variants under rig_dynamic_tenant/kvmhost/)"
}

# ---- pre-flight guards on the remote (fail loudly, never touch other disks) --
# $DISCOVERED_DEV is the experiment disk (letters move across reboots).
remote_guards() {
    remote_sudo_script 'set -euo pipefail
d='"$DISCOVERED_DEV"'
[ -b /dev/$d ] || { echo "GUARD FAIL: /dev/$d missing"; exit 1; }
size=$(blockdev --getsize64 /dev/$d)
[ "$size" = "103079215104" ] || { echo "GUARD FAIL: $d size $size != 103079215104"; exit 1; }
mounts=$(findmnt -rn -S /dev/$d -o TARGET || true)
[ -z "$mounts" ] || { echo "GUARD FAIL: $d mounted at: $mounts"; exit 1; }
rootsrc=$(findmnt -rn -T / -o SOURCE)
case "$rootsrc" in /dev/$d*) echo "GUARD FAIL: root on $d"; exit 1;; esac
holders=$(ls /sys/class/block/$d/holders 2>/dev/null | wc -l)
[ "$holders" = "0" ] || { echo "GUARD FAIL: $d has holders"; exit 1; }
sch=$(cat /sys/block/$d/queue/scheduler)
case "$sch" in *scx_io*) : ;; *) echo "GUARD FAIL: scx_io not in scheduler list: $sch"; exit 1;; esac
test -d /opt/ioext-bench/.venv || { echo "GUARD FAIL: remote .venv missing"; exit 1; }
echo "GUARDS OK: /dev/$d 96G unmounted, scx_io available, .venv present"
' || { echo "PRE-FLIGHT GUARDS FAILED" >&2; exit 1; }
}

fill_once() {
    # One-time whole-disk fill so randread windows see real media everywhere.
    # Device name interpolated locally (env prefixes do not reach the remote
    # shell through the base64 remote_sudo_script path).
    remote_sudo_script "set -euo pipefail
d='$DISCOVERED_DEV'
MARKER=/opt/ioext-bench/results/.dyn_tenant_sdb_filled
if [ -f \"\$MARKER\" ]; then echo \"fill: already done\"; exit 0; fi
mkdir -p /opt/ioext-bench/results
fio --name=fill --filename=/dev/\$d --ioengine=io_uring --direct=1 \\
    --bs=1M --rw=write --iodepth=16 --size=100% --output=/dev/null
sync
touch \"\$MARKER\"
echo \"fill: complete\"
" || { echo "FILL FAILED" >&2; exit 1; }
}

# Hard wall-clock on one sequence SSH (default 18m). Hangs after executor
# timeout left fio in D-state for hours; this forces local return + recover.
DYN_TENANT_SEQ_TIMEOUT_SEC="${DYN_TENANT_SEQ_TIMEOUT_SEC:-1080}"

remote_recover() {
    local why="${1:-recover}"
    echo "=== remote_recover ($why) ==="
    remote_sudo_script "$(cat <<'EOS'
set -euo pipefail
pkill -9 -f '/opt/ioext-bench/.venv/bin/python -m vm.executor' 2>/dev/null || true
pkill -9 fio 2>/dev/null || true
sleep 2
if pgrep -x fio >/dev/null 2>&1; then
  echo "RECOVER: fio still present (likely D-state) — need reboot"
  exit 2
fi
echo "RECOVER: fio and executor cleared"
EOS
)" || {
        if [ "${DYN_TENANT_RECOVER_REBOOT:-1}" = "1" ]; then
            echo "RECOVER: scheduling reboot"
            remote_sudo_script 'sync; nohup bash -c "sleep 2; reboot" >/dev/null 2>&1 &' || true
        fi
    }
}

# ---- run one sequence remotely ------------------------------------------------
# Convention from finish_step1.sh / run_motivation_m2_campaign.py: the
# executor runs AS ROOT via `sudo -S bash -c` (scene-level run_sudo_shell
# then authenticates trivially; remote user sudo is passworded).
run_sequence() {
    local SEQ="$1"
    local RUN_LOG="/tmp/dyn_tenant_kvmhost_${SEQ}.log"
    echo "=== [$SEQ] remote run (as root) ==="
    # Pre-remove the (root-owned) previous result dir inside the sudo context;
    # a partially-written dir would poison the next attempt (executor refuses
    # to mix runs into a non-empty output dir).
    # Pre-remove the (root-owned) previous result dir inside the sudo context
    # (ABSOLUTE path: sudo -c cwd is the invoking user's home, not $GUEST_ROOT).
    ssh -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" \
        "printf '%s\\n' \"$SUDO_PASS\" | sudo -S -p '' bash -c 'rm -rf $GUEST_ROOT/$REMOTE_RESULTS_REL/$SEQ $GUEST_ROOT/$REMOTE_RESULTS_REL/$SEQ.tar.gz $GUEST_ROOT/test_results/isol_bench/rig_dynamic_tenant'" 2>&1 | tail -1
    local cmd_b64
    cmd_b64=$(printf '%s' "cd $GUEST_ROOT && export PERF_ARENA_WORKSPACE=$GUEST_ROOT PYTHONPATH=$GUEST_ROOT/src SUDO_PASSWORD=$SUDO_PASS IOEXT_DESTRUCTIVE_TEST=1 && $GUEST_ROOT/.venv/bin/python -m vm.executor --config src/vm/configs/task/isol_bench/rig_dynamic_tenant/kvmhost/$CONFIG --sequence $SEQ --output $REMOTE_RESULTS_REL/$SEQ --log-level INFO; rc=\$?; chmod -R a+rX $REMOTE_RESULTS_REL/$SEQ 2>/dev/null; exit \$rc" | base64 -w0)
    local pw_b64_run
    pw_b64_run=$(printf '%s\n' "$SUDO_PASS" | base64 -w0)
    local rc=0
    if timeout "${DYN_TENANT_SEQ_TIMEOUT_SEC}" ssh -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" \
        "echo $pw_b64_run | base64 -d | sudo -S -p '' bash -c \"echo $cmd_b64 | base64 -d | bash\"" \
        > "$RUN_LOG" 2>&1; then
        rc=0
    else
        rc=$?
        if [ "$rc" -eq 124 ]; then
            echo "RUN HANG/timeout after ${DYN_TENANT_SEQ_TIMEOUT_SEC}s — recovering" >&2
            remote_recover "seq_timeout_$SEQ"
        fi
    fi
  # Always try to clear stray fio between sequences.
    remote_recover "post_seq_$SEQ" || true
    tail -4 "$RUN_LOG"
    if [ "$rc" != "0" ]; then
        echo "RUN FAILED rc=$rc (log: $RUN_LOG)"
        return 1
    fi
    echo "$SEQ" >> "$LOCAL_RESULTS/_pass.log"
    return 0
}

pull_results() {
    echo "=== pulling results back ==="
    rsync -az -e "ssh -p $SSH_PORT" \
        "$SSH_USER@$SSH_HOST:$GUEST_ROOT/$REMOTE_RESULTS_REL/" "$LOCAL_RESULTS/"
    echo "results at $LOCAL_RESULTS"
}

mkdir -p "$LOCAL_RESULTS"

if [ "$RECOVER_ONLY" = "1" ]; then
    discover_device || true
    remote_recover "recover_only"
    pull_results
    exit 0
fi

# Linear main flow: discovery MUST succeed and precede config synchronization
# (a failed discovery must never trigger a sync with an empty device — that
# bug produced target_device: '' configs on 20261104).
if [ "$SKIP_SYNC" = "1" ]; then
    discover_device
else
    discover_device
    sync_all
fi
if [ "$SYNC_ONLY" = "1" ]; then echo "sync-only done"; exit 0; fi
remote_guards
if [ "$NO_FILL" = "1" ]; then
    echo "fill: skipped (--no-fill / DYN_TENANT_NO_FILL=1)"
else
    fill_once
fi

if [ "$CONFIG" != "dynamic_tenant_smoke.yml" ]; then
    cp "/tmp/dyn_tenant_kvmhost_yaml/$CONFIG" "$LOCAL_RESULTS/" 2>/dev/null || true
fi

PASS=0; FAIL=0
for SEQ in $SEQS; do
    if run_sequence "$SEQ"; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi
done
pull_results

echo "=== kvmhost dyn-tenant SUMMARY: PASS=$PASS FAIL=$FAIL ==="
# Local validation when the config copy is available (validator is device-agnostic:
# config structure + metadata + raw fio JSONs).
CONFIG_COPY="$LOCAL_RESULTS/$CONFIG"
if [ -f "$CONFIG_COPY" ]; then
    # The validator resolves base_scene relative to the config's directory.
    cp -r "$BASE_DIR/src/vm/configs/scene" "$LOCAL_RESULTS/scene" 2>/dev/null || true
    for SEQ in $SEQS; do
        python3 "$BASE_DIR/scripts/validate_isol_bench_result.py" \
            --config "$CONFIG_COPY" --sequence "$SEQ" \
            --result-dir "$LOCAL_RESULTS/$SEQ" 2>&1 | tail -1 | sed "s/^/[$SEQ] /"
    done
fi
[ "$FAIL" = "0" ]