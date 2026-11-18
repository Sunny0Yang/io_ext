#!/usr/bin/env bash
# scripts/run_d2_rerun_kvmhost.sh
# kvmhost D2 (saturated weighted fairness) rerun — anti-wedge campaign runner.
#
# Design (2026-11-14, "D2 rerun" decision):
#   - contention-only sequences from the current single-step d2_fairness.yml
#     (the old ncg isolation steps + their solo-data parsing bug are gone);
#   - fresh on-machine calibration before anything runs (the old 320 MiB/s
#     RECAL anchor is stale by >2x vs observed device behaviour);
#   - per-sequence canary gate (6 s QD256 probe) to skip phase-collapsed
#     windows, post-gate on aggregate BW to catch in-run collapse;
#   - hard per-sequence wall clock; on timeout collect forensics FIRST
#     (dmesg, PSI, cgroup io.stat, process states, executor log tail), then
#     recover (pkill, reboot only if fio is stuck in D-state), then requeue
#     the sequence once at the end of the batch;
#   - device letter is re-discovered every sequence (sdX drifts on reboot);
#     if it drifts the yaml is regenerated in place.
#
# Usage:
#   flock -w 600 /tmp/d2_rerun_kvmhost.lock scripts/run_d2_rerun_kvmhost.sh [--sync] [--seqs-only LIST]
# Env: SLEEP_BETWEEN (default 15), SEQ_TIMEOUT_SEC (default 900), CANARY_MIN_FRAC (0.6),
#      GATE_MIN_FRAC (0.5), D2_SKIP_CALIB=1 (reuse existing calib json).
set -uo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE_DIR="${ARCHIVE_DIR:-/home/anon/ioext-ssh}"
SSH_ENV="${SSH_ENV:-$ARCHIVE_DIR/ssh_env.sh}"
GUEST_ROOT="/opt/ioext-bench"
REMOTE_CFG_DIR="src/vm/configs/task/isol_bench/paper_sdb"
REMOTE_RESULTS_REL="results/d2_rerun_kvmhost"
LOCAL_RESULTS="$BASE_DIR/results/d2_rerun_kvmhost"
LOCAL_CALIB="$BASE_DIR/experiments/d2_kvmhost_rerun/calibration_bm_kvmhost_sdb.json"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-15}"
SEQ_TIMEOUT_SEC="${SEQ_TIMEOUT_SEC:-420}"   # per-seq hard wall clock; cg16 gets 540s (see run_sequence)
CANARY_MIN_FRAC="${CANARY_MIN_FRAC:-0.6}"
GATE_MIN_FRAC="${GATE_MIN_FRAC:-0.5}"
SUDO_PASS="REDACTED"
DO_SYNC=0
SEQS_ARG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --sync) DO_SYNC=1; shift ;;
        --seqs-only) SEQS_ARG="$2"; shift 2 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

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

sync_all() {
    log "=== rsync loaders + repo ==="
    rsync -az -e "ssh -p $SSH_PORT" "$BASE_DIR"/userspace/build/loader_* \
        "$SSH_USER@$SSH_HOST:/opt/io-ext/userspace/" 2>/dev/null \
        || log "warn: loader rsync skipped/failed (using deployed loaders)"
    rsync -az -e "ssh -p $SSH_PORT" --exclude .venv --exclude results \
        --exclude '__pycache__' --exclude '*.pyc' --exclude .git \
        --exclude 'src/vm/assets' \
        "$BASE_DIR/" "$SSH_USER@$SSH_HOST:$GUEST_ROOT/" || log "warn: repo rsync non-zero"
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

# ---- calibration -------------------------------------------------------------
# Probes (all direct=1 on the raw experiment device, as root):
#   solo  : 4k randread QD256 20s x2        -> canary anchor + model_iops
#   seq   : 1M read QD8 10s x1              -> io.cost model bps
#   refN  : 4k randread QD256 numjobs=N 12s -> per-cg contention reference
calibrate() {
    if [ "${D2_SKIP_CALIB:-0}" = "1" ] && [ -f "$LOCAL_CALIB" ]; then
        log "calib: reusing $LOCAL_CALIB"
        return 0
    fi
    log "=== calibration on /dev/$DISCOVERED_DEV (est ~2.5 min) ==="
    local out
    out=$(remote_sudo_script 'set -euo pipefail
d='"$DISCOVERED_DEV"'
cd /tmp && rm -f d2cal_*.json
fio --name=solo --filename=/dev/$d --ioengine=libaio --direct=1 --bs=4k \
    --rw=randread --iodepth=256 --runtime=20 --time_based \
    --output=d2cal_solo1.json --output-format=json >/dev/null 2>&1
fio --name=solo --filename=/dev/$d --ioengine=libaio --direct=1 --bs=4k \
    --rw=randread --iodepth=256 --runtime=20 --time_based \
    --output=d2cal_solo2.json --output-format=json >/dev/null 2>&1
fio --name=seq --filename=/dev/$d --ioengine=libaio --direct=1 --bs=1M \
    --rw=read --iodepth=8 --runtime=10 --time_based \
    --output=d2cal_seq.json --output-format=json >/dev/null 2>&1
for n in 2 4 8 16; do
  fio --name=ref --filename=/dev/$d --ioengine=libaio --direct=1 --bs=4k \
      --rw=randread --iodepth=256 --numjobs=$n --group_reporting --runtime=12 \
      --time_based --output=d2cal_ref$n.json --output-format=json >/dev/null 2>&1
done
python3 - <<PYEOF
import json
def bw(p):
    j=json.load(open(p)); return j["jobs"][0]["read"]["bw_bytes"]/1e6
def iops(p):
    j=json.load(open(p)); return j["jobs"][0]["read"]["iops"]
s=[bw(f"/tmp/d2cal_solo1.json"), bw(f"/tmp/d2cal_solo2.json")]
s.sort()
ref={n: round(bw(f"/tmp/d2cal_ref{n}.json"),1) for n in (2,4,8,16)}
cal={"solo_mb_s": round(s[0],1),
     "model_bps": int(bw("/tmp/d2cal_seq.json")*1e6),
     "model_iops": int(iops("/tmp/d2cal_solo1.json")),
     "contention_ref_mb_s": {str(k): v for k,v in ref.items()}}
print("CALIB_JSON "+json.dumps(cal))
PYEOF')
    echo "$out" | grep -q CALIB_JSON || { log "calibration FAILED"; echo "$out" | tail -5; exit 1; }
    mkdir -p "$(dirname "$LOCAL_CALIB")"
    echo "$out" | grep CALIB_JSON | sed 's/^CALIB_JSON //' > "$LOCAL_CALIB"
    log "calib: $(cat "$LOCAL_CALIB")"
}

push_yaml() {
    mkdir -p /tmp/d2_rerun
    python3 "$BASE_DIR/scripts/gen_d2_kvmhost_rerun_yaml.py" \
        --dev "$DISCOVERED_DEV" --calib "$LOCAL_CALIB" --out /tmp/d2_rerun/d2_kvmhost_rerun.yml \
        --runtime-sec "${D2_RUNTIME_SEC:-20}" --ramp-sec "${D2_RAMP_SEC:-10}" \
        || { log "yaml generation failed"; exit 1; }
    scp -P "$SSH_PORT" -q /tmp/d2_rerun/d2_kvmhost_rerun.yml \
        "$SSH_USER@$SSH_HOST:$GUEST_ROOT/$REMOTE_CFG_DIR/" || { log "yaml push failed"; exit 1; }
}

canary() {
    # 6 s QD256 probe; returns 0 when >= CANARY_MIN_FRAC * solo anchor.
    local out frac_ok
    out=$(remote_sudo_script "set -euo pipefail
fio --name=canary --filename=/dev/$DISCOVERED_DEV --ioengine=libaio --direct=1 \
    --bs=4k --rw=randread --iodepth=256 --runtime=6 --time_based \
    --output=/tmp/d2canary.json --output-format=json >/dev/null 2>&1
python3 -c \"import json;print(int(json.load(open('/tmp/d2canary.json'))['jobs'][0]['read']['bw_bytes']/1e6))\"")
    case "$out" in ''|*[!0-9]*) echo 0; return 1 ;; esac
    echo "$out"
}

dump_forensics() {
    local SEQ="$1" DEST="$BASE_DIR/experiments/d2_kvmhost_rerun/forensics"
    mkdir -p "$DEST"
    local f="$DEST/${SEQ}_$(date +%H%M%S).txt"
    remote_sudo_script 'set -uo pipefail
d='"$DISCOVERED_DEV"'
echo "=== dmesg tail ===";       dmesg | tail -n 60
echo "=== pressure ===";        cat /proc/pressure/io /proc/pressure/cpu 2>/dev/null
echo "=== blocked tasks ===";   ps -eo pid,stat,wchan:32,etime,cmd | grep -E "fio|loader_|vm.executor" | grep -v grep
echo "=== ib_d2 io.stat ==="
for c in /sys/fs/cgroup/ib_d2_*; do
  [ -d "$c" ] || continue
  echo "--- $c"; cat "$c/io.stat" 2>/dev/null | head -5
done
echo "=== disk stats ===";      cat /sys/block/$d/stat
echo "=== scheduler ===";       cat /sys/block/$d/queue/scheduler
echo "=== executor log tail ==="
tail -n 40 '"$GUEST_ROOT"'/results/d2_rerun_kvmhost/'"$SEQ"'/executor_console.log 2>/dev/null || true
ls '"$GUEST_ROOT"'/results/d2_rerun_kvmhost/'"$SEQ"'/ 2>/dev/null || true
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

run_sequence() {
    local SEQ="$1"
    local RUN_LOG="/tmp/d2_rerun_${SEQ}.log"
    local t0=$(date +%s)
    ssh_q "echo $SUDO_PASS | sudo -S bash -c 'rm -rf $GUEST_ROOT/$REMOTE_RESULTS_REL/$SEQ $GUEST_ROOT/$REMOTE_RESULTS_REL/$SEQ.tar.gz $GUEST_ROOT/test_results/isol_bench/paper_sdb/d2'" 2>/dev/null | tail -1
    local cmd_b64
    cmd_b64=$(printf '%s' "cd $GUEST_ROOT && export PERF_ARENA_WORKSPACE=$GUEST_ROOT PYTHONPATH=$GUEST_ROOT/src SUDO_PASSWORD=$SUDO_PASS IOEXT_DESTRUCTIVE_TEST=1 && $GUEST_ROOT/.venv/bin/python -m vm.executor --config $REMOTE_CFG_DIR/d2_kvmhost_rerun.yml --sequence $SEQ --output $REMOTE_RESULTS_REL/$SEQ --log-level INFO; rc=\$?; chmod -R a+rX $REMOTE_RESULTS_REL/$SEQ 2>/dev/null; exit \$rc" | base64 -w0)
    local rc=0
    local TO="$SEQ_TIMEOUT_SEC"
    case "$SEQ" in *_cg16_*) TO=$((SEQ_TIMEOUT_SEC + 120)) ;; esac
    if timeout "$TO" ssh -p "$SSH_PORT" -o ConnectTimeout=10 "$SSH_USER@$SSH_HOST" \
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
        return 1
    fi
    if [ "$rc" -ne 0 ]; then
        log "FAIL $SEQ rc=$rc (${dt}s) log=$RUN_LOG"; tail -3 "$RUN_LOG" | sed 's/^/    /'
        recover || { sleep 90; }
        return 1
    fi
    # post-gate: aggregate BW across the single contention step
    local agg ref_cg cg floor
    cg=$(echo "$SEQ" | sed -E 's/.*_cg([0-9]+)_iter.*/\1/')
    agg=$(ssh_q "echo $SUDO_PASS | sudo -S python3 -c \"
import json,glob
tot=0
for p in glob.glob('$GUEST_ROOT/$REMOTE_RESULTS_REL/$SEQ/step_*/*/*fio.json'):
    try:
        d=json.load(open(p)); tot+=sum((j.get('read') or {}).get('bw_bytes',0)/1e6 for j in d.get('jobs') or [])
    except Exception: pass
print(int(tot))\" 2>/dev/null") || agg=""
    case "$agg" in ''|*[!0-9]*) log "GATE-UNPARSEABLE $SEQ"; return 0 ;; esac
    ref_cg=$(python3 -c "import json;print(json.load(open('$LOCAL_CALIB'))['contention_ref_mb_s']['$cg'])")
    floor=$(python3 -c "print(int($ref_cg*$GATE_MIN_FRAC))")
    if [ "$agg" -lt "$floor" ]; then
        log "GATE-LOW $SEQ agg=${agg}MB/s < floor=${floor} (ref cg=$cg: $ref_cg)"
        return 2   # suspect: requeue
    fi
    log "PASS $SEQ (${dt}s) agg=${agg}MB/s >= floor=${floor}"
    echo "$SEQ" >> "$LOCAL_RESULTS/_pass.log"
    return 0
}

pull_results() {
    rsync -az -e "ssh -p $SSH_PORT" \
        "$SSH_USER@$SSH_HOST:$GUEST_ROOT/$REMOTE_RESULTS_REL/" "$LOCAL_RESULTS/" \
        && log "results pulled to $LOCAL_RESULTS" || log "warn: pull failed"
}

# ---- main --------------------------------------------------------------------
mkdir -p "$LOCAL_RESULTS" "$BASE_DIR/experiments/d2_kvmhost_rerun"
discover_device || exit 1
[ "$DO_SYNC" = "1" ] && sync_all
remote_guards
calibrate
push_yaml

# sequence list from the generated yaml (single source of truth)
mapfile -t SEQS < <(python3 - <<'PYEOF'
import yaml
cfg=yaml.safe_load(open('/tmp/d2_rerun/d2_kvmhost_rerun.yml'))
for s in cfg['test_sequences']:
    print(s['sequence_id'])
PYEOF
)
if [ -n "$SEQS_ARG" ]; then
    mapfile -t SEQS < <(echo "$SEQS_ARG" | tr ' ' '\n')
fi
TOTAL=${#SEQS[@]}
log "batch: $TOTAL sequences; per-seq budget ${SEQ_TIMEOUT_SEC}s; ETA (nominal, no wedges): $(( TOTAL * 240 / 60 )) min"
declare -A NOMINAL=( [2]=150 [4]=170 [8]=210 [16]=270 )
ETA=0
for SEQ in "${SEQS[@]}"; do
    cg=$(echo "$SEQ" | sed -E 's/.*_cg([0-9]+)_iter.*/\1/')
    ETA=$((ETA + ${NOMINAL[$cg]:-210} + 25))
done
log "ETA nominal total: $((ETA/60)) min"

PASS=0; FAIL=0
declare -a REQUEUE=()
declare -a SUSPECT=()
i=0
for SEQ in "${SEQS[@]}"; do
    i=$((i+1))
    # device letter may drift after a mid-campaign reboot
    discover_device || { log "device discovery failed before $SEQ — retry in 60s"; sleep 60; discover_device || { log "FATAL: device gone"; break; }; }
    cg=$(echo "$SEQ" | sed -E 's/.*_cg([0-9]+)_iter.*/\1/')
    est=${NOMINAL[$cg]:-210}
    log "PROGRESS $i/$TOTAL $SEQ est=${est}s (nominal)"
    # canary gate
    local_canary=0
    for attempt in $(seq 1 "${CANARY_MAX_ATTEMPTS:-12}"); do
        c=$(canary) || c=0
        floor_c=$(python3 -c "import json;print(int(int(json.load(open('$LOCAL_CALIB'))['solo_mb_s'])*$CANARY_MIN_FRAC))")
        if [ "$c" -ge "$floor_c" ]; then local_canary=$c; break; fi
        log "canary LOW (${c}MB/s < ${floor_c}) attempt $attempt/${CANARY_MAX_ATTEMPTS:-12} — cooldown ${CANARY_COOLDOWN:-60}s"
        sleep "${CANARY_COOLDOWN:-60}"
    done
    [ "$local_canary" -ge "$floor_c" ] || log "canary still LOW after ${CANARY_MAX_ATTEMPTS:-12} tries — running flagged (PHASE-LOW)"
    run_sequence "$SEQ"; rc=$?
    case $rc in
        0) PASS=$((PASS+1)) ;;
        2) SUSPECT+=("$SEQ"); REQUEUE+=("$SEQ") ;;
        *) FAIL=$((FAIL+1)); REQUEUE+=("$SEQ") ;;
    esac
    # pull incrementally every 10 sequences so a disaster costs little
    if [ $((i % 10)) -eq 0 ]; then pull_results; fi
    sleep "$SLEEP_BETWEEN"
done

# requeue pass (one retry each)
if [ ${#REQUEUE[@]} -gt 0 ]; then
    log "=== requeue pass: ${#REQUEUE[@]} sequences ==="
    for SEQ in "${REQUEUE[@]}"; do
        discover_device || { log "FATAL: device gone in requeue"; break; }
        log "REQUEUE $SEQ"
        run_sequence "$SEQ"; rc=$?
        case $rc in
            0) PASS=$((PASS+1)); FAIL=$((FAIL>0?FAIL-1:0)) ;;
            2) log "STILL-LOW $SEQ (kept, flagged)" ;;
            *) log "STILL-FAIL $SEQ" ;;
        esac
        sleep "$SLEEP_BETWEEN"
    done
fi

pull_results
log "SUMMARY: total=$TOTAL PASS=$PASS FAIL=$FAIL SUSPECT_FIRSTPASS=${#SUSPECT[@]}"
exit 0
