#!/usr/bin/env bash
# D3 kvmhost triad batch: none / qnone / bfqf via vm.executor
set -uo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE_DIR="${ARCHIVE_DIR:-/home/anon/ioext-ssh}"
SSH_ENV="${SSH_ENV:-$ARCHIVE_DIR/ssh_env.sh}"
GUEST_ROOT="/opt/ioext-bench"
REMOTE_CFG_DIR="src/vm/configs/task/isol_bench/d3_kvmhost"
REMOTE_RESULTS_REL="${REMOTE_RESULTS_REL:-results/d3_kvmhost/triad_20261118}"
LOCAL_RESULTS="$BASE_DIR/$REMOTE_RESULTS_REL"
LOCAL_ANCHOR="$BASE_DIR/results/d3_kvmhost/calibration/analysis/SLO_and_B_none.json"
LOCAL_CANARY="$BASE_DIR/experiments/d2_kvmhost_rerun/calibration_bm_kvmhost_sdb.json"
LOCAL_YML="/tmp/d3_kvmhost/d3_kvmhost_triad.yml"
YML_NAME="d3_kvmhost_triad.yml"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-10}"
SEQ_TIMEOUT_SEC="${SEQ_TIMEOUT_SEC:-420}"
CANARY_MIN_FRAC="${CANARY_MIN_FRAC:-0.6}"
SUDO_PASS="REDACTED"
SEQS_ARG=""
RUNS="${TRIAD_RUNS:-1}"

while [ $# -gt 0 ]; do
    case "$1" in
        --seqs-only) SEQS_ARG="$2"; shift 2 ;;
        --runs) RUNS="$2"; shift 2 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

[ -f "$LOCAL_ANCHOR" ] || { echo "FATAL: anchor missing: $LOCAL_ANCHOR"; exit 1; }
# shellcheck disable=SC1090
source "$SSH_ENV"

ssh_q() { ssh -p "$SSH_PORT" -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "$SSH_USER@$SSH_HOST" "$@"; }

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
    case "$DISCOVERED_DEV" in sd*) : ;; *) log "device discovery failed"; return 1 ;; esac
}

remote_guards() {
    remote_sudo_script 'set -euo pipefail
mountpoint -q /data 2>/dev/null && umount /data || true
d='"$DISCOVERED_DEV"'
[ -b /dev/$d ] || { echo "GUARD FAIL: /dev/$d missing"; exit 1; }
mounts=$(findmnt -rn -S /dev/$d -o TARGET || true)
[ -z "$mounts" ] || { echo "GUARD FAIL: $d mounted at $mounts"; exit 1; }
sch=$(cat /sys/block/$d/queue/scheduler)
case "$sch" in *scx_io*) : ;; *) echo "GUARD FAIL: scx_io not available: $sch"; exit 1;; esac
test -d /opt/ioext-bench/.venv || { echo "GUARD FAIL: remote .venv missing"; exit 1; }
pgrep -f "[v]m.executor" >/dev/null 2>&1 && { echo "GUARD FAIL: executor running"; exit 1; }
pgrep -x fio >/dev/null 2>&1 && { echo "GUARD FAIL: fio running"; exit 1; }
test -x /opt/io-ext/userspace/loader_q_none || { echo "GUARD FAIL: loader_q_none"; exit 1; }
echo "GUARDS OK: /dev/$d"' || { log "PRE-FLIGHT GUARDS FAILED"; exit 1; }
}

snapshot_env() {
    remote_sudo_script 'set -euo pipefail
python3 - <<PYEOF
import json, subprocess
def out(c): return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
dev = "'"$DISCOVERED_DEV"'"
print("ENVJSON " + json.dumps({
    "hostname": out("hostname"), "uname_r": out("uname -r"),
    "fio_version": out("fio --version 2>/dev/null || echo unknown"),
    "git_commit": out("cd /opt/ioext-bench && git rev-parse HEAD 2>/dev/null || echo unknown"),
    "device": dev, "device_size_bytes": out("blockdev --getsize64 /dev/%s" % dev),
    "device_model": out("cat /sys/block/%s/device/model 2>/dev/null || echo virtio" % dev),
    "scheduler_available": out("cat /sys/block/%s/queue/scheduler" % dev),
    "captured_at": out("date -Is"),
}))
PYEOF' | grep '^ENVJSON ' | sed 's/^ENVJSON //' > "$LOCAL_RESULTS/batch_env.json"
}

gen_and_push_yaml() {
    mkdir -p /tmp/d3_kvmhost
    python3 "$BASE_DIR/scripts/gen_d3_kvmhost_triad_yaml.py" \
        --dev "$DISCOVERED_DEV" --anchor "$LOCAL_ANCHOR" --out "$LOCAL_YML" \
        --runs "$RUNS" --runtime-sec "${D3_RUNTIME_SEC:-60}" --ramp-sec "${D3_RAMP_SEC:-20}" \
        || exit 1
    ssh_q "mkdir -p $GUEST_ROOT/$REMOTE_CFG_DIR"
    scp -P "$SSH_PORT" -q "$LOCAL_YML" \
        "$SSH_USER@$SSH_HOST:$GUEST_ROOT/$REMOTE_CFG_DIR/$YML_NAME"
}

canary() {
    local out
    out=$(remote_sudo_script "set -euo pipefail
fio --name=canary --filename=/dev/$DISCOVERED_DEV --ioengine=libaio --direct=1 \
    --bs=4k --rw=randread --iodepth=256 --runtime=6 --time_based \
    --output=/tmp/d3triad_canary.json --output-format=json >/dev/null 2>&1
python3 -c \"import json;print(int(json.load(open('/tmp/d3triad_canary.json'))['jobs'][0]['read']['bw_bytes']/1e6))\"") || return 1
    echo "$out"
}

recover() {
    remote_sudo_script 'pkill -9 -f "vm.executor" 2>/dev/null || true
pkill -9 fio 2>/dev/null || true
pkill -9 -f "/opt/io-ext/userspace/loader_" 2>/dev/null || true
sleep 2; echo RECOVER'
}

write_manifest() {
    local SEQ="$1" CANARY="$2" GATE_AGG="$3" RC="$4"
    local env_json
    env_json=$(cat "$LOCAL_RESULTS/batch_env.json")
    remote_sudo_script "set -euo pipefail
cat > /tmp/d3man_${SEQ}.py <<'PYEOF'
import json, hashlib, glob, os, subprocess
seq = '${SEQ}'
root = '/opt/ioext-bench/${REMOTE_RESULTS_REL}/' + seq
env = json.loads('''${env_json}''')
def sha(p):
    h = hashlib.sha256()
    with open(p,'rb') as f:
        for b in iter(lambda: f.read(1<<20), b''): h.update(b)
    return h.hexdigest()
yml = '/opt/ioext-bench/${REMOTE_CFG_DIR}/${YML_NAME}'
body = seq[len('D3PV_'):]
arm = body.rsplit('_run', 1)[0]
if arm in ('none', 'bfqf'):
    lc, bep = 'ib_d3x_nat_lc', 'ib_d3x_nat_be_t'
    if arm == 'bfqf':
        lc, bep = 'ib_d3x_bfq_lc', 'ib_d3x_bfq_be_t'
elif arm == 'qnone':
    lc, bep = 'ib_d3x_qnone_lc', 'ib_d3x_qnone_be_t'
else:
    lc, bep = 'ib_d3x_iox_lc', 'ib_d3x_iox_be_t'
cgs = [f'/sys/fs/cgroup/{lc}'] + [f'/sys/fs/cgroup/{bep}%d' % i for i in (1,2,3,4)]
fio_jsons = sorted(glob.glob(root + '/step_*/**/*fio.json', recursive=True))
m = {
  'run_id': seq, 'batch_id': 'd3_kvmhost_triad_20261118', 'arm': arm,
  'iteration': int(body.rsplit('_run', 1)[-1]),
  'hostname': env['hostname'], 'uname_r': env['uname_r'],
  'fio_version': env['fio_version'], 'device': env['device'],
  'workload_config': '${YML_NAME}', 'workload_sha256': sha(yml),
  'strategy': arm, 'cgroup_paths': cgs,
  'remote_git_commit': env['git_commit'],
  'fio_json_paths': [p.replace(root + '/', '') for p in fio_jsons],
  'canary_mb_s': ${CANARY:-0}, 'post_gate_agg_mb_s': ${GATE_AGG:-null},
  'runner_rc': ${RC:-0}, 'clean_gate': 'pending-analysis',
  'start_ts': env['captured_at'],
  'end_ts': subprocess.run(['date','-Is'],capture_output=True,text=True).stdout.strip(),
}
json.dump(m, open(root + '/manifest.json', 'w'), indent=2)
print('MANIFEST-OK ' + seq)
PYEOF
python3 /tmp/d3man_${SEQ}.py"
}

run_sequence() {
    local SEQ="$1" RUN_LOG="/tmp/d3_triad_${SEQ}.log" t0 rc agg canary_val="${2:-0}"
    t0=$(date +%s)
    ssh_q "echo $SUDO_PASS | sudo -S bash -c 'rm -rf $GUEST_ROOT/$REMOTE_RESULTS_REL/$SEQ'" 2>/dev/null | tail -1
    local cmd_b64
    cmd_b64=$(printf '%s' "cd $GUEST_ROOT && export PERF_ARENA_WORKSPACE=$GUEST_ROOT PYTHONPATH=$GUEST_ROOT/src SUDO_PASSWORD=$SUDO_PASS IOEXT_DESTRUCTIVE_TEST=1 && $GUEST_ROOT/.venv/bin/python -m vm.executor --config $REMOTE_CFG_DIR/$YML_NAME --sequence $SEQ --output $REMOTE_RESULTS_REL/$SEQ --log-level INFO; rc=\$?; chmod -R a+rX $REMOTE_RESULTS_REL/$SEQ 2>/dev/null; exit \$rc" | base64 -w0)
    if timeout "$SEQ_TIMEOUT_SEC" ssh -p "$SSH_PORT" -o ConnectTimeout=10 "$SSH_USER@$SSH_HOST" \
        "echo $SUDO_PASS | sudo -S bash -c \"echo $cmd_b64 | base64 -d | bash\"" >"$RUN_LOG" 2>&1; then
        rc=0
    else rc=$?; fi
    local dt=$(( $(date +%s) - t0 ))
    if [ "$rc" -ne 0 ]; then
        log "FAIL $SEQ rc=$rc (${dt}s)"; tail -5 "$RUN_LOG" | sed 's/^/    /'
        recover; write_manifest "$SEQ" "$canary_val" null "$rc"; return 1
    fi
    agg=$(ssh_q "echo $SUDO_PASS | sudo -S python3 -c \"
import json,glob
tot=0
for p in glob.glob('$GUEST_ROOT/$REMOTE_RESULTS_REL/$SEQ/step_*/*/*fio.json'):
    try:
        d=json.load(open(p)); tot+=sum((j.get('read') or {}).get('bw_bytes',0)/1e6 for j in d.get('jobs') or [])
    except Exception: pass
print(int(tot))\" 2>/dev/null") || agg=0
    write_manifest "$SEQ" "$canary_val" "$agg" 0
    log "PASS $SEQ (${dt}s) agg_read=${agg}MB/s"
    return 0
}

mkdir -p "$LOCAL_RESULTS"
discover_device || exit 1
remote_guards
snapshot_env
gen_and_push_yaml

mapfile -t SEQS < <(python3 -c "
import yaml
for s in yaml.safe_load(open('$LOCAL_YML'))['test_sequences']:
    print(s['sequence_id'])
")
if [ -n "$SEQS_ARG" ]; then mapfile -t SEQS < <(echo "$SEQS_ARG" | tr ' ' '\n'); fi

CANARY_FLOOR=$(python3 -c "import json;print(int(json.load(open('$LOCAL_CANARY'))['solo_mb_s']*$CANARY_MIN_FRAC))")
PASS=0 FAIL=0
for SEQ in "${SEQS[@]}"; do
    discover_device || { sleep 30; discover_device || break; }
    c=0
    for _ in $(seq 1 8); do c=$(canary) || c=0; [ "$c" -ge "$CANARY_FLOOR" ] && break; sleep 10; done
  if run_sequence "$SEQ" "$c"; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi
    sleep "$SLEEP_BETWEEN"
done

rsync -az -e "ssh -p $SSH_PORT" "$SSH_USER@$SSH_HOST:$GUEST_ROOT/$REMOTE_RESULTS_REL/" "$LOCAL_RESULTS/" \
    && log "pulled $LOCAL_RESULTS"

python3 - <<PY
import json, glob, os
base = "$LOCAL_RESULTS"
b_none = json.load(open("$LOCAL_ANCHOR"))["b_none_mb_s"]
rows = []
for mpath in sorted(glob.glob(base + "/D3PV_*/manifest.json")):
    m = json.load(open(mpath))
    arm = m["arm"]
    agg = m.get("post_gate_agg_mb_s") or 0
    rows.append((arm, m["run_id"], agg, 100.0 * agg / b_none if agg else 0))
print("arm | run_id | BE_agg_MB/s | retention%")
for r in rows:
    print(f"{r[0]:6} | {r[1]} | {r[2]:8.1f} | {r[3]:5.1f}")
PY

log "DONE pass=$PASS fail=$FAIL"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)