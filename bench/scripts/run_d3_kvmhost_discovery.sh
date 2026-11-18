#!/usr/bin/env bash
# scripts/run_d3_kvmhost_discovery.sh
# kvmhost D3 discovery batch runner (campaign Phase 2) — same anti-wedge
# machinery as the calibration runner; anchor comes from the Phase-1
# SLO_and_B_none.json (must be verdict=PASS; caps are derived by the yaml
# generator from that file, never hand-copied).
#
# Usage:
#   flock -w 600 /tmp/d3_kvmhost_disc.lock scripts/run_d3_kvmhost_discovery.sh [--seqs-only LIST]
# Env: SLEEP_BETWEEN (15), SEQ_TIMEOUT_SEC (420), CANARY_MIN_FRAC (0.6)
set -uo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE_DIR="${ARCHIVE_DIR:-/home/anon/ioext-ssh}"
SSH_ENV="${SSH_ENV:-$ARCHIVE_DIR/ssh_env.sh}"
GUEST_ROOT="/opt/ioext-bench"
REMOTE_CFG_DIR="src/vm/configs/task/isol_bench/d3_kvmhost"
REMOTE_RESULTS_REL="results/d3_kvmhost/discovery"
LOCAL_RESULTS="$BASE_DIR/results/d3_kvmhost/discovery"
LOCAL_ANCHOR="$BASE_DIR/results/d3_kvmhost/calibration/analysis/SLO_and_B_none.json"
LOCAL_CANARY="$BASE_DIR/experiments/d2_kvmhost_rerun/calibration_bm_kvmhost_sdb.json"
LOCAL_YML="/tmp/d3_kvmhost/d3_kvmhost_discovery.yml"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-15}"
SEQ_TIMEOUT_SEC="${SEQ_TIMEOUT_SEC:-420}"
CANARY_MIN_FRAC="${CANARY_MIN_FRAC:-0.6}"
SUDO_PASS="REDACTED"
SEQS_ARG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --seqs-only) SEQS_ARG="$2"; shift 2 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

[ -f "$LOCAL_ANCHOR" ] || { echo "FATAL: Phase-1 anchor missing: $LOCAL_ANCHOR"; exit 1; }
python3 -c "import json,sys;a=json.load(open('$LOCAL_ANCHOR'));sys.exit(0 if a.get('verdict')=='PASS' and a.get('b_none_mb_s') else 1)" \
    || { echo "FATAL: anchor is not PASS-validated"; exit 1; }
[ -f "$LOCAL_CANARY" ] || { echo "FATAL: canary anchor missing: $LOCAL_CANARY"; exit 1; }

# shellcheck disable=SC1090
source "$SSH_ENV"

ssh_q() { ssh -p "$SSH_PORT" -o ConnectTimeout=10 "$SSH_USER@$SSH_HOST" "$@"; }

remote_sudo_script() {
    local script="$1" pw_b64 cmd_b64
    pw_b64=$(printf '%s\n' "$SUDO_PASS" | base64 -w0)
    cmd_b64=$(printf '%s' "$script" | base64 -w0)
    ssh_q "echo ${pw_b64} | base64 -d | sudo -S bash -c \"echo ${cmd_b64} | base64 -d | bash\""
}

log() { echo "[$(date -Is)] $*"; }

SDB_SECTORS=201326592
discover_device() {
    DISCOVERED_DEV="$(remote_sudo_script 'set -euo pipefail
matches=()
for syseq in /sys/class/block/sd*; do
    d=${syseq#/sys/class/block/}
    [ -e "$syseq/size" ] || continue
    [ "$(cat "$syseq/size")" = "'"$SDB_SECTORS"'" ] || continue
    [ "$(ls "$syseq/holders" 2>/dev/null | wc -l)" = "0" ] || continue
    [ "$(ls /sys/class/block/ | grep -c "^${d}[0-9]" || true)" = "0" ] || continue
    matches+=("$d")
done
if [ ${#matches[@]} -ne 1 ]; then echo "GUARDFAIL:${matches[*]:-none}" >&2; exit 1; fi
echo "${matches[0]}"')" || DISCOVERED_DEV=""
    case "$DISCOVERED_DEV" in
        sd*) : ;;
        *) log "device discovery failed"; return 1 ;;
    esac
}

remote_guards() {
    remote_sudo_script 'set -euo pipefail
d='"$DISCOVERED_DEV"'
[ -b /dev/$d ] || { echo "GUARD FAIL: /dev/$d missing"; exit 1; }
size=$(blockdev --getsize64 /dev/$d)
[ "$size" = "103079215104" ] || { echo "GUARD FAIL: $d size $size"; exit 1; }
mounts=$(findmnt -rn -S /dev/$d -o TARGET || true)
[ -z "$mounts" ] || { echo "GUARD FAIL: $d mounted at $mounts"; exit 1; }
rootsrc=$(findmnt -rn -T / -o SOURCE)
case "$rootsrc" in /dev/$d*) echo "GUARD FAIL: root on $d"; exit 1;; esac
sch=$(cat /sys/block/$d/queue/scheduler)
case "$sch" in *scx_io*) : ;; *) echo "GUARD FAIL: scx_io not available: $sch"; exit 1;; esac
test -d /opt/ioext-bench/.venv || { echo "GUARD FAIL: remote .venv missing"; exit 1; }
pgrep -f "[v]m.executor" >/dev/null 2>&1 && { echo "GUARD FAIL: an executor is running"; exit 1; }
pgrep -x fio >/dev/null 2>&1 && { echo "GUARD FAIL: fio running"; exit 1; }
echo "GUARDS OK: /dev/$d"' || { log "PRE-FLIGHT GUARDS FAILED"; exit 1; }
}

snapshot_env() {
    remote_sudo_script 'set -euo pipefail
python3 - <<PYEOF
import json, subprocess
def out(c): return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
dev = "'"$DISCOVERED_DEV"'"
print("ENVJSON " + json.dumps({
    "hostname": out("hostname"),
    "uname_r": out("uname -r"),
    "fio_version": out("fio --version 2>/dev/null || echo unknown"),
    "git_commit": out("cd /opt/ioext-bench && git rev-parse HEAD 2>/dev/null || echo unknown"),
    "device": dev,
    "device_size_bytes": out("blockdev --getsize64 /dev/%s" % dev),
    "device_model": out("cat /sys/block/%s/device/model 2>/dev/null || echo virtio" % dev),
    "scheduler_available": out("cat /sys/block/%s/queue/scheduler" % dev),
    "captured_at": out("date -Is"),
}))
PYEOF' | grep '^ENVJSON ' | sed 's/^ENVJSON //' > "$LOCAL_RESULTS/batch_env.json" \
      || { log "env snapshot failed"; exit 1; }
    log "env: $(cat "$LOCAL_RESULTS/batch_env.json")"
}

gen_and_push_yaml() {
    mkdir -p /tmp/d3_kvmhost
    python3 "$BASE_DIR/scripts/gen_d3_kvmhost_discovery_yaml.py" \
        --dev "$DISCOVERED_DEV" --anchor "$LOCAL_ANCHOR" --out "$LOCAL_YML" \
        --runtime-sec "${D3_RUNTIME_SEC:-60}" --ramp-sec "${D3_RAMP_SEC:-20}" \
        || { log "yaml generation failed"; exit 1; }
    ssh_q "mkdir -p $GUEST_ROOT/$REMOTE_CFG_DIR" || { log "remote mkdir failed"; exit 1; }
    scp -P "$SSH_PORT" -q "$LOCAL_YML" \
        "$SSH_USER@$SSH_HOST:$GUEST_ROOT/$REMOTE_CFG_DIR/d3_kvmhost_discovery.yml" \
        || { log "yaml push failed"; exit 1; }
}

canary() {
    local out
    out=$(remote_sudo_script "set -euo pipefail
fio --name=canary --filename=/dev/$DISCOVERED_DEV --ioengine=libaio --direct=1 \
    --bs=4k --rw=randread --iodepth=256 --runtime=6 --time_based \
    --output=/tmp/d3disc_canary.json --output-format=json >/dev/null 2>&1
python3 -c \"import json;print(int(json.load(open('/tmp/d3disc_canary.json'))['jobs'][0]['read']['bw_bytes']/1e6))\"")
    case "$out" in ''|*[!0-9]*) echo 0; return 1 ;; esac
    echo "$out"
}

dump_forensics() {
    local SEQ="$1" DEST="$BASE_DIR/experiments/d3_kvmhost/forensics"
    mkdir -p "$DEST"
    local f="$DEST/${SEQ}_$(date +%H%M%S).txt"
    remote_sudo_script 'set -uo pipefail
d='"$DISCOVERED_DEV"'
echo "=== dmesg tail ===";       dmesg | tail -n 60
echo "=== pressure ===";        cat /proc/pressure/io /proc/pressure/cpu 2>/dev/null
echo "=== blocked tasks ===";   ps -eo pid,stat,wchan:32,etime,cmd | grep -E "fio|loader_|vm.executor" | grep -v grep
echo "=== ib_d3x io.stat ==="
for c in /sys/fs/cgroup/ib_d3x*; do
  [ -d "$c" ] || continue
  echo "--- $c"; cat "$c/io.stat" 2>/dev/null | head -5
done
echo "=== scheduler state ==="; cat /sys/block/$d/queue/scheduler; cat /sys/kernel/sched_ext/state 2>/dev/null || true
echo "=== executor log tail ==="
tail -n 40 '"$GUEST_ROOT"'/results/d3_kvmhost/discovery/'"$SEQ"'/executor_console.log 2>/dev/null || true
ls '"$GUEST_ROOT"'/results/d3_kvmhost/discovery/'"$SEQ"'/ 2>/dev/null || true
' > "$f" 2>&1
    log "forensics saved: $f"
}

recover() {
    remote_sudo_script 'set -euo pipefail
pkill -9 -f "vm.executor" 2>/dev/null || true
pkill -9 fio 2>/dev/null || true
pkill -9 -f "/opt/io-ext/userspace/loader_" 2>/dev/null || true
sleep 2
if pgrep -x fio >/dev/null 2>&1; then
  echo "RECOVER: fio stuck (D-state?) — rebooting"
  sync; nohup bash -c "sleep 2; reboot" >/dev/null 2>&1 &
  exit 2
fi
echo "RECOVER: cleared"' || return 2
    return 0
}

write_manifest() {
    local SEQ="$1" CANARY="$2" GATE_AGG="$3" RC="$4"
    local env_json
    env_json=$(cat "$LOCAL_RESULTS/batch_env.json")
    remote_sudo_script "set -euo pipefail
cat > /tmp/d3man_${SEQ}.py <<PYEOF
import json, hashlib, glob, os, subprocess
seq = '${SEQ}'
root = '/opt/ioext-bench/results/d3_kvmhost/discovery/' + seq
env = json.loads('''${env_json}''')
def sha(p):
    h = hashlib.sha256()
    with open(p,'rb') as f:
        for b in iter(lambda: f.read(1<<20), b''): h.update(b)
    return h.hexdigest()
yml = '/opt/ioext-bench/src/vm/configs/task/isol_bench/d3_kvmhost/d3_kvmhost_discovery.yml'
fio_jsons = sorted(glob.glob(root + '/step_*/**/*fio.json', recursive=True))
body = seq[len('D3PV_'):]
arm = body.rsplit('_run', 1)[0]
m = {
  'run_id': seq,
  'batch_id': 'd3_kvmhost_discovery',
  'arm': arm,
  'iteration': int(body.rsplit('_run', 1)[-1]),
  'hostname': env['hostname'],
  'uname_r': env['uname_r'],
  'kernel_build': env['uname_r'],
  'fio_version': env['fio_version'],
  'device': env['device'],
  'device_size_bytes': env['device_size_bytes'],
  'device_model': env['device_model'],
  'workload_config': 'd3_kvmhost_discovery.yml',
  'workload_sha256': sha(yml),
  'strategy': arm,
  'strategy_params': {},
  'cap_units': 'MiB/s per BE job (--rate-mib-s)' if 'qrl' in arm else None,
  'cgroup_paths': ['/sys/fs/cgroup/ib_d3x_nat_lc' if 'nat' in body or arm in ('none','bfq') else '/sys/fs/cgroup/ib_d3x_iox_lc']
                   + ['/sys/fs/cgroup/ib_d3x_nat_be_t%d' % i if arm in ('none','bfq') else '/sys/fs/cgroup/ib_d3x_iox_be_t%d' % i for i in (1,2,3,4)],
  'remote_git_commit': env['git_commit'],
  'fio_json_paths': [p.replace(root + '/', '') for p in fio_jsons],
  'raw_log_paths': sorted(os.path.basename(p) for p in glob.glob(root + '/step_*/**/*.log', recursive=True)),
  'canary_mb_s': ${CANARY:-0},
  'post_gate_agg_mb_s': ${GATE_AGG:-null},
  'runner_rc': ${RC:-0},
  'clean_gate': 'pending-analysis',
  'start_ts': env['captured_at'],
  'end_ts': subprocess.run(['date','-Is'],capture_output=True,text=True).stdout.strip(),
}
json.dump(m, open(root + '/manifest.json', 'w'), indent=2)
print('MANIFEST-OK ' + seq)
PYEOF
python3 /tmp/d3man_${SEQ}.py" | grep MANIFEST-OK || log "warn: manifest write failed for $SEQ"
}

run_sequence() {
    local SEQ="$1" RUN_LOG="/tmp/d3_kvmhost_${SEQ}.log" t0 rc agg canary_val
    canary_val="$2"
    t0=$(date +%s)
    ssh_q "echo $SUDO_PASS | sudo -S bash -c 'rm -rf $GUEST_ROOT/$REMOTE_RESULTS_REL/$SEQ $GUEST_ROOT/$REMOTE_RESULTS_REL/$SEQ.tar.gz'" 2>/dev/null | tail -1
    local cmd_b64
    cmd_b64=$(printf '%s' "cd $GUEST_ROOT && export PERF_ARENA_WORKSPACE=$GUEST_ROOT PYTHONPATH=$GUEST_ROOT/src SUDO_PASSWORD=$SUDO_PASS IOEXT_DESTRUCTIVE_TEST=1 && $GUEST_ROOT/.venv/bin/python -m vm.executor --config $REMOTE_CFG_DIR/d3_kvmhost_discovery.yml --sequence $SEQ --output $REMOTE_RESULTS_REL/$SEQ --log-level INFO; rc=\$?; chmod -R a+rX $REMOTE_RESULTS_REL/$SEQ 2>/dev/null; exit \$rc" | base64 -w0)
    if timeout "$SEQ_TIMEOUT_SEC" ssh -p "$SSH_PORT" -o ConnectTimeout=10 "$SSH_USER@$SSH_HOST" \
        "echo $SUDO_PASS | sudo -S bash -c \"echo $cmd_b64 | base64 -d | bash\"" \
        > "$RUN_LOG" 2>&1; then
        rc=0
    else
        rc=$?
    fi
    local dt=$(( $(date +%s) - t0 ))
    if [ "$rc" -eq 124 ]; then
        log "TIMEOUT $SEQ after ${dt}s — collecting forensics then recovering"
        dump_forensics "$SEQ"
        recover || { log "reboot scheduled for $SEQ wedge; waiting 90s"; sleep 90; }
        write_manifest "$SEQ" "$canary_val" null "$rc"
        return 1
    fi
    if [ "$rc" -ne 0 ]; then
        log "FAIL $SEQ rc=$rc (${dt}s) log=$RUN_LOG"; tail -3 "$RUN_LOG" | sed 's/^/    /'
        recover || { sleep 90; }
        write_manifest "$SEQ" "$canary_val" null "$rc"
        return 1
    fi
    agg=$(ssh_q "echo $SUDO_PASS | sudo -S python3 -c \"
import json,glob
tot=0
for p in glob.glob('$GUEST_ROOT/$REMOTE_RESULTS_REL/$SEQ/step_*/*/*fio.json'):
    try:
        d=json.load(open(p)); tot+=sum((j.get('read') or {}).get('bw_bytes',0)/1e6 for j in d.get('jobs') or [])
    except Exception: pass
print(int(tot))\" 2>/dev/null") || agg=""
    case "$agg" in ''|*[!0-9]*) agg="0" ;; esac
    write_manifest "$SEQ" "$canary_val" "$agg" 0
    echo "$SEQ" >> "$LOCAL_RESULTS/_pass.log"
    log "PASS $SEQ (${dt}s) canary=${canary_val} agg=${agg}MB/s"
    return 0
}

pull_results() {
    rsync -az -e "ssh -p $SSH_PORT" \
        "$SSH_USER@$SSH_HOST:$GUEST_ROOT/$REMOTE_RESULTS_REL/" "$LOCAL_RESULTS/" \
        && log "results pulled to $LOCAL_RESULTS" || log "warn: pull failed"
}

# ---- main --------------------------------------------------------------------
mkdir -p "$LOCAL_RESULTS" "$BASE_DIR/experiments/d3_kvmhost"
rm -f "$LOCAL_RESULTS/_pass.log"
discover_device || exit 1
remote_guards
snapshot_env
gen_and_push_yaml

mapfile -t SEQS < <(python3 - <<'PYEOF'
import yaml
cfg = yaml.safe_load(open('/tmp/d3_kvmhost/d3_kvmhost_discovery.yml'))
for s in cfg['test_sequences']:
    print(s['sequence_id'])
PYEOF
)
if [ -n "$SEQS_ARG" ]; then
    mapfile -t SEQS < <(echo "$SEQS_ARG" | tr ' ' '\n')
fi
TOTAL=${#SEQS[@]}
log "batch: $TOTAL sequences; per-seq budget ${SEQ_TIMEOUT_SEC}s"

ANCHOR_SOLO=$(python3 -c "import json;print(json.load(open('$LOCAL_CANARY'))['solo_mb_s'])")
CANARY_FLOOR=$(python3 -c "print(int($ANCHOR_SOLO*$CANARY_MIN_FRAC))")
log "canary floor: ${CANARY_FLOOR} MB/s (0.6 x D2 solo anchor ${ANCHOR_SOLO})"

PASS=0; FAIL=0
declare -a REQUEUE=()
i=0
for SEQ in "${SEQS[@]}"; do
    i=$((i+1))
    discover_device || { log "device discovery failed before $SEQ — retry in 60s"; sleep 60; discover_device || { log "FATAL: device gone"; break; }; }
    log "PROGRESS $i/$TOTAL $SEQ"
    canary_val=0
    for attempt in $(seq 1 "${CANARY_MAX_ATTEMPTS:-12}"); do
        c=$(canary) || c=0
        if [ "$c" -ge "$CANARY_FLOOR" ]; then canary_val=$c; break; fi
        log "canary LOW (${c}MB/s < ${CANARY_FLOOR}) attempt $attempt/${CANARY_MAX_ATTEMPTS:-12} — cooldown ${CANARY_COOLDOWN:-60}s"
        sleep "${CANARY_COOLDOWN:-60}"
    done
    [ "$canary_val" -ge "$CANARY_FLOOR" ] || log "canary still LOW after ${CANARY_MAX_ATTEMPTS:-12} tries — running flagged (PHASE-LOW)"
    if run_sequence "$SEQ" "$canary_val"; then
        PASS=$((PASS+1))
    else
        FAIL=$((FAIL+1)); REQUEUE+=("$SEQ")
    fi
    if [ $((i % 8)) -eq 0 ]; then pull_results; fi
    sleep "$SLEEP_BETWEEN"
done

if [ ${#REQUEUE[@]} -gt 0 ]; then
    log "=== requeue pass: ${#REQUEUE[@]} sequences ==="
    for SEQ in "${REQUEUE[@]}"; do
        discover_device || { log "FATAL: device gone in requeue"; break; }
        log "REQUEUE $SEQ"
        canary_val=0
        for attempt in $(seq 1 "${CANARY_MAX_ATTEMPTS:-12}"); do
            c=$(canary) || c=0
            if [ "$c" -ge "$CANARY_FLOOR" ]; then canary_val=$c; break; fi
            sleep "${CANARY_COOLDOWN:-60}"
        done
        if run_sequence "$SEQ" "$canary_val"; then
            PASS=$((PASS+1)); FAIL=$((FAIL>0?FAIL-1:0))
        else
            log "STILL-FAIL $SEQ"
        fi
        sleep "$SLEEP_BETWEEN"
    done
fi

pull_results
log "SUMMARY: total=$TOTAL PASS=$PASS FAIL=$FAIL"
exit 0
