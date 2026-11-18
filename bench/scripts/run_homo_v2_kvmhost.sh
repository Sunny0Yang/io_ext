#!/usr/bin/env bash
# scripts/run_homo_v2_kvmhost.sh
# Control-host wrapper for the homo-v2 homomorphic-reproduction campaign on
# kvmhost (exclusive 96G experiment disk; ALL homo-v2 data is re-run on the
# 7.3.1-ioext kernel per the campaign contract).
#
# Structure mirrors scripts/run_dyn_tenant_kvmhost.sh (device discovery by
# exact 96G size, remote guards, remote_sudo_script base64 pattern, root-run
# vm.executor, per-sequence rsync back). On top of it:
#
#   - the config is generated fresh each sync by
#     scripts/gen_homo_v2_yamls.py with HOMO_V2_TARGET_DEVICE = the
#     discovered 96G device, then pushed to
#     $GUEST_ROOT/src/vm/configs/task/homo_v2/ on the guest;
#   - an internal flock on /tmp/homo_v2_kvmhost.lock serializes executions
#     (run WITHOUT extra external flock);
#   - the outer loop is reps: for rep in 1..REPS, every sequence runs in the
#     family order seq_elevator arms -> seq_controller arms -> seq_frontier
#     arms (n=10 by default => median + bootstrap CI per arm);
#   - every (rep, sequence) run is archived on the guest as
#     results/homo_v2_kvmhost/rep<NN>/<SEQ>.tar.gz with a sha256 verifier JSON
#     (manifest of every payload file + the run log), and both are rsynced
#     back immediately so an interrupted campaign keeps finished artifacts;
#   - a guest-side sidecar (same sysfs/pgrep evidence loop as the RQ1
#     sampler) snapshots scheduler/io.cost/bpffs-pins/loaders per sampling
#     tick; the log lands in the run dir as <SEQ>.sidecar.log (build_rq1_
#     results.py's per-case sidecar convention);
#   - the frontier native reference column is NOT re-run: it cross-references
#     HOMO_ctrl_n_none (identical workload/device/kernel; the config has no
#     HOMO_front_n_none sequence).
#
# SAFETY GUARDS (hard failures before anything runs):
#   - /dev/<dev> exists, size == 103079215104 bytes, NOT mounted, not root,
#     holders empty (device discovered by size, sdX letters move on reboot);
#   - remote kernel release must contain the ioext marker (homo-v2 contract);
#   - scx_io in the scheduler list, remote .venv present.
#
# Usage:
#   scripts/run_homo_v2_kvmhost.sh [--sync-only] [--skip-sync]
#       [--reps N] [--seq SEQ ...] [--resume]
#   --reps       number of repetition rounds (default 10)
#   --seq        marker: remaining positional args are sequence ids
#                (default: all 16 in family order)
#   --resume     skip (sequence, rep) pairs whose verifier JSON already
#                exists locally (previously-pulled manifest _manifest.json)
# Env overrides: ARCHIVE_DIR, HOMO_V2_REPS (default reps),
#   HOMO_V2_KERNEL_MARK (remote kernel-release substring guard, default
#   'ioext3' per the homo-v2 contract), LOCAL_RESULTS.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE_DIR="${ARCHIVE_DIR:-/home/anon/ioext-ssh}"
SSH_ENV="${SSH_ENV:-$ARCHIVE_DIR/ssh_env.sh}"
IOEXT_WT="${IOEXT_WORKSPACE:-$BASE_DIR}"
IOEXT_BUILD="${IOEXT_BUILD:-$IOEXT_WT/userspace/build}"
GUEST_ROOT="/opt/ioext-bench"
REMOTE_RESULTS_REL="results/homo_v2_kvmhost"
LOCAL_RESULTS="${LOCAL_RESULTS:-$BASE_DIR/results/homo_v2_kvmhost}"
SDB_EXPECTED_BYTES=103079215104
SYNC_ONLY=0
SKIP_SYNC=0
REPS="${HOMO_V2_REPS:-10}"
RESUME=0
CONFIG="homo_v2.yml"
LOCK_FILE="/tmp/homo_v2_kvmhost.lock"

# Family-ordered sequence list (matches gen_homo_v2_yamls.py emission order;
# the frontier family deliberately carries NO native reference sequence —
# cross-referenced from HOMO_ctrl_n_none, see the RUNBOOK).
DEFAULT_SEQS="
HOMO_elev_n_none HOMO_elev_n_mq_deadline HOMO_elev_n_kyber HOMO_elev_x_mq_deadline_q HOMO_elev_x_kyber_q
HOMO_ctrl_n_none HOMO_ctrl_n_io_max HOMO_ctrl_x_iomax_q HOMO_ctrl_n_io_weight HOMO_ctrl_x_weight_arb
HOMO_ctrl_n_io_cost HOMO_ctrl_x_cost_arb HOMO_ctrl_n_io_latency HOMO_ctrl_x_latency_arb
HOMO_front_x_d2fq_arb HOMO_front_x_linnos_q"

while [ $# -gt 0 ]; do
    case "$1" in
        --sync-only) SYNC_ONLY=1; shift ;;
        --skip-sync) SKIP_SYNC=1; shift ;;
        --reps) REPS="$2"; shift 2 ;;
        --resume) RESUME=1; shift ;;
        --seq) shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) break ;;
    esac
done
# --seq filters as positional args (single token or repeated).
SEQS="${*:-$DEFAULT_SEQS}"

# ---- single-flight lock (held for the whole campaign) -----------------------
exec 300>"$LOCK_FILE"
if ! flock -n 300; then
    echo "another homo-v2 kvmhost campaign holds $LOCK_FILE — aborting" >&2
    exit 1
fi

# shellcheck disable=SC1090
source "$SSH_ENV"

# ---- device discovery: sdX letters move across reboots; identify by exact
# 96G size + no mount/holders (run_dyn_tenant_kvmhost.sh pattern). -----
SDB_EXPECTED_SECTORS=201326592
DISCOVERED_DEV=""
remote_sudo_script() {
    local script="$1"
    local pw_b64 cmd_b64
    pw_b64=$(printf '%s\n' "$SUDO_PASS" | base64 -w0)
    cmd_b64=$(printf '%s' "$script" | base64 -w0)
    ssh -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" \
        "echo ${pw_b64} | base64 -d | sudo -S bash -c \"echo ${cmd_b64} | base64 -d | bash\""
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
    echo "=== rsync loaders (homo-v2 arm set; final CLIs, frozen loaders) ==="
    rsync -az -e "ssh -p $SSH_PORT" "$IOEXT_BUILD"/loader_* "$SSH_USER@$SSH_HOST:/opt/io-ext/userspace/"
    echo "=== guest repo hygiene (reset uncommitted drift, then synced overlay) ==="
    # The guest repo may carry stale modifications from earlier sessions
    # (reproducibility risk flagged in the audit). Reset tracked drift and
    # drop untracked stray files, EXCLUDING the venv, results/ (run outputs
    # + .homo_v2_sdb_filled marker) so they survive.
    ssh -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" \
        "cd $GUEST_ROOT && git reset --hard >/dev/null 2>&1; git clean -fd -e .venv -e results -q 2>/dev/null; true" \
        || { echo 'warn: guest repo hygiene non-zero' >&2; }
    echo "=== rsync ioext-bench repo ==="
    rsync -az --delete -e "ssh -p $SSH_PORT" --exclude .venv --exclude results \
        --exclude '__pycache__' --exclude '*.pyc' --exclude .git \
        "$BASE_DIR/" "$SSH_USER@$SSH_HOST:$GUEST_ROOT/" \
        || { echo "warn: rsync repo non-zero" >&2; }
    echo "=== generate + push device variant homo-v2 task config ==="
    HOMO_V2_TARGET_DEVICE="$DISCOVERED_DEV" \
        python3 "$BASE_DIR/scripts/gen_homo_v2_yamls.py" \
        --output-dir /tmp/homo_v2_kvmhost_yaml > /tmp/homo_v2_kvmhost_gen.log 2>&1 \
        || { echo "config generation FAILED:" >&2; tail -5 /tmp/homo_v2_kvmhost_gen.log; return 1; }
    grep -q "target_device: $DISCOVERED_DEV" /tmp/homo_v2_kvmhost_yaml/$CONFIG \
        || { echo "generated config missing target_device: $DISCOVERED_DEV — refusing to push" >&2; return 1; }
    ssh -n -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" \
        "echo REDACTED | sudo -S bash -c 'rm -f $GUEST_ROOT/src/vm/configs/task/homo_v2/*.yml' 2>/dev/null; true"
    scp -P "$SSH_PORT" -q /tmp/homo_v2_kvmhost_yaml/$CONFIG \
        "$SSH_USER@$SSH_HOST:$GUEST_ROOT/src/vm/configs/task/homo_v2/$CONFIG"
    echo "sync complete ($CONFIG under task/homo_v2/)"
}

# ---- pre-flight guards on the remote (fail loudly) --------------------------
remote_guards() {
    remote_sudo_script "set -euo pipefail
d='$DISCOVERED_DEV'
[ -b /dev/\$d ] || { echo 'GUARD FAIL: /dev/\$d missing'; exit 1; }
size=\$(blockdev --getsize64 /dev/\$d)
[ \"\$size\" = '103079215104' ] || { echo \"GUARD FAIL: \$d size \$size != 103079215104\"; exit 1; }
mounts=\$(findmnt -rn -S /dev/\$d -o TARGET || true)
[ -z \"\$mounts\" ] || { echo \"GUARD FAIL: \$d mounted at: \$mounts\"; exit 1; }
rootsrc=\$(findmnt -rn -T / -o SOURCE)
case \"\$rootsrc\" in /dev/\$d*) echo 'GUARD FAIL: root on '\$d; exit 1;; esac
holders=\$(ls /sys/class/block/\$d/holders 2>/dev/null | wc -l)
[ \"\$holders\" = '0' ] || { echo 'GUARD FAIL: \$d has holders'; exit 1; }
sch=\$(cat /sys/block/\$d/queue/scheduler)
case \"\$sch\" in *scx_io*) : ;; *) echo \"GUARD FAIL: scx_io not in scheduler list: \$sch\"; exit 1;; esac
krel=\$(uname -r)
case \"\$krel\" in *'${HOMO_V2_KERNEL_MARK:-ioext3}'*) : ;; *) echo \"GUARD FAIL: kernel \$krel lacks '${HOMO_V2_KERNEL_MARK:-ioext3}' — homo-v2 contract requires the 7.3.1-ioext3 kernel\"; exit 1;; esac
test -d /opt/ioext-bench/.venv || { echo 'GUARD FAIL: remote .venv missing'; exit 1; }
for l in loader_q_none loader_q_mq_deadline loader_q_kyber loader_q_fifo loader_arb_wdrr; do
    test -x /opt/io-ext/userspace/\$l || { echo \"GUARD FAIL: /opt/io-ext/userspace/\$l missing\"; exit 1; }
done
MISS=\$(for l in loader_q_iomax loader_arb_iocost loader_arb_iolatency loader_arb_d2fq loader_q_linnos; do test -x /opt/io-ext/userspace/\$l || echo \$l; done)
if [ -n \"\$MISS\" ]; then
    echo \"GUARD FAIL: loaders missing on guest (stale sync): \$MISS\"; exit 1
fi
echo \"GUARDS OK: /dev/\$d 96G unmounted, kernel \$krel, scx_io + loaders present\"
" || { echo "PRE-FLIGHT GUARDS FAILED" >&2; exit 1; }
}

# One-time whole-disk fill (independent marker from dyn_tenant's; same fio
# preconditioning rationale — raw randread/replay windows must see real media).
fill_once() {
    remote_sudo_script "set -euo pipefail
d='$DISCOVERED_DEV'
MARKER=/opt/ioext-bench/results/.homo_v2_sdb_filled
if [ -f \"\$MARKER\" ]; then echo \"fill: already done\"; exit 0; fi
mkdir -p /opt/ioext-bench/results
fio --name=fill --filename=/dev/\$d --ioengine=io_uring --direct=1 \
    --bs=1M --rw=write --iodepth=16 --size=100% --output=/dev/null
sync
touch \"\$MARKER\"
echo \"fill: complete\"
" || { echo "FILL FAILED" >&2; exit 1; }
}

# ---- sidecar: guest-side sysfs/pgrep evidence sampler (RQ1 sampler loop,
# rq1_sidecar_sample.py GUEST_LOOP analog; evidence-only, writes no sysfs) ----
HOMO_SAMPLER_DIR="/tmp/homo_v2_sampler_20261106"
sidecar_start() {
    remote_sudo_script "set -u
mkdir -p $HOMO_SAMPLER_DIR
: > $HOMO_SAMPLER_DIR/state.log
(
while true; do
  ts=\$(date +%s.%N)

  {
    echo \"=== T=\$ts\"
    echo \"-- sdb scheduler\"
    cat /sys/block/$DISCOVERED_DEV/queue/scheduler 2>&1
    echo \"-- io.cost.model/qos (root)\"
    grep '^8:16 ' /sys/fs/cgroup/io.cost.model /sys/fs/cgroup/io.cost.qos 2>/dev/null || echo '(no 8:16 lines)'
    echo \"-- cgroup io.* arm state (non-max lines)\"
    grep -rh ' ' /sys/fs/cgroup/homo_v2_*/io.max /sys/fs/cgroup/homo_v2_*/io.weight /sys/fs/cgroup/homo_v2_*/io.latency 2>/dev/null | grep -v ' max$' || echo '(none)'
    echo \"-- ioext bpffs pins\"
    ls /sys/fs/bpf 2>/dev/null | grep -i ioext || echo '(none)'
    echo \"-- loaders\"
    pgrep -af 'loader_' 2>/dev/null | head -20 || echo '(none)'
    echo \"-- scx procs\"
    pgrep -af 'scx' 2>/dev/null | grep -v pgrep || echo '(none)'
  } >> $HOMO_SAMPLER_DIR/state.log 2>&1
  sleep 2
done
) >/dev/null 2>&1 </dev/null &
echo \$! > $HOMO_SAMPLER_DIR/pid
echo \"sidecar started pid \$(cat $HOMO_SAMPLER_DIR/pid)\"
" | tail -1
}

sidecar_stop_and_file() {
    local dest_dir="$1" seq="$2"
    remote_sudo_script "set -u
pid=\$(cat $HOMO_SAMPLER_DIR/pid 2>/dev/null || true)
[ -n \"\$pid\" ] && kill \"\$pid\" 2>/dev/null || true
sleep 0.3
mkdir -p $dest_dir 2>/dev/null || true
cp $HOMO_SAMPLER_DIR/state.log $dest_dir/${seq}.sidecar.log 2>/dev/null && echo 'sidecar log filed'
" | tail -1
}

# ---- run one (rep, sequence) remotely ---------------------------------------
# Convention (finish_step1.sh / run_dyn_tenant_kvmhost.sh): the executor runs
# AS ROOT via sudo -S so scene-level run_sudo_shell trivially authenticates.
run_pair() {
    local SEQ="$1" REP="$2"
    local REPDIR="$REMOTE_RESULTS_REL/rep$(printf '%02d' "$REP")"
    local LOG_LOCAL="/tmp/homo_v2_kvmhost_${SEQ}_r${REP}.log"
    echo "=== [$SEQ rep$REP] remote run (as root) ==="
    # Pre-remove the (root-owned) previous result dir + archives inside the
    # sudo context (ABSOLUTE path; executor refuses to mix into a non-empty
    # output dir).
    ssh -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" \
        "echo REDACTED | sudo -S bash -c 'rm -rf $GUEST_ROOT/$REPDIR/$SEQ $GUEST_ROOT/$REPDIR/$SEQ.tar.gz $GUEST_ROOT/$REPDIR/${SEQ}_manifest.json $GUEST_ROOT/test_results/io_ext/policies/HV_* /dev/shm/ioext_bench_homo_v2' 2>/dev/null; true" \
        | tail -1
    echo "=== blkdiscard $DISCOVERED_DEV (per-run write-path reset) ==="
    # Every (seq, rep) starts from a freshly deallocated NVMe: without this,
    # the write path decays monotonically run-over-run (SLC burst -> tLC with
    # no TRIM: measured 20k->1.2k IOPS / 275->3 MiB/s BE across 10 reps of the
    # 2026-11-06 pilot), which drowns the faithful-reproduction deltas.
    # Discard returns the drive to the pristine-state baseline both native and
    # iox arms see (same-fare reset, aligned with rq1 case_script hygiene).
    trim_ok=$(remote_sudo_script "blkdiscard -f /dev/$DISCOVERED_DEV && echo TRIM_OK" | tail -1)
    # fail-stop if the reset did not land (hard guard, like the other GUARDs)
    case "$trim_ok" in
        *TRIM_OK*) : ;;
        *) echo "TRIM FAILED: $trim_ok — refusing to run with decayed write path" >&2; return 1 ;;
    esac
    sidecar_start
    local cmd_b64
    cmd_b64=$(printf '%s' "cd $GUEST_ROOT && export PERF_ARENA_WORKSPACE=$GUEST_ROOT PYTHONPATH=$GUEST_ROOT/src SUDO_PASSWORD=REDACTED IOEXT_DESTRUCTIVE_TEST=1 && $GUEST_ROOT/.venv/bin/python -m vm.executor --config src/vm/configs/task/homo_v2/$CONFIG --sequence $SEQ --output $REPDIR/$SEQ --log-level INFO; rc=\$?; chmod -R a+rX $REPDIR/$SEQ 2>/dev/null; exit \$rc" | base64 -w0)
    set +e
    ssh -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" \
        "echo REDACTED | sudo -S bash -c \"echo $cmd_b64 | base64 -d | bash\"" \
        > "$LOG_LOCAL" 2>&1
    local rc=$?
    set -e
    sidecar_stop_and_file "$GUEST_ROOT/$REPDIR/$SEQ" "$SEQ"
    if [ "$rc" != "0" ]; then
        echo "RUN FAILED rc=$rc (log: $LOG_LOCAL) — dumping tail and ABORTING" >&2
        tail -30 "$LOG_LOCAL" >&2
        return 1
    fi
    tail -2 "$LOG_LOCAL"
    # Archive + verifier manifest (sha256 of the tarball and of every payload
    # file) then pull all three artifacts home immediately; an interrupted
    # campaign keeps finished (sequence, rep) artifacts.
    remote_sudo_script "set -euo pipefail
cd '$GUEST_ROOT/$REPDIR'
tar czf ${SEQ}.tar.gz ${SEQ}
python3 - <<'PYEOF'
import hashlib, json, os, sys
seq = sys.argv[1]
files = {}
for root in (seq,):
    dp_root = root
    for dp, _dns, fns in os.walk(dp_root):
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            files[p] = hashlib.sha256(open(p, 'rb').read()).hexdigest()
h = hashlib.sha256()
with open(seq + '.tar.gz', 'rb') as fh:
    for chunk in iter(lambda: fh.read(1 << 20), b''):
        h.update(chunk)
doc = {'sequence': seq, 'tar_sha256': h.hexdigest(), 'files': files}
with open(seq + '_manifest.json', 'w') as fh:
    json.dump(doc, fh, indent=1, sort_keys=True)
PYEOF
echo '$SEQ archived'
" | tail -1
    mkdir -p "$LOCAL_RESULTS/rep$(printf '%02d' "$REP")"
    rsync -az -e "ssh -p $SSH_PORT" \
        "$SSH_USER@$SSH_HOST:$GUEST_ROOT/$REPDIR/${SEQ}.tar.gz" \
        "$SSH_USER@$SSH_HOST:$GUEST_ROOT/$REPDIR/${SEQ}_manifest.json" \
        "$LOCAL_RESULTS/rep$(printf '%02d' "$REP")/"
    echo "$SEQ rep$REP archived + pulled"
    return 0
}

skip_done() {
    local SEQ="$1" REP="$2"
    local REPDIR="rep$(printf '%02d' "$REP")"
    [ "$RESUME" = "1" ] || return 1
    [ -f "$LOCAL_RESULTS/$REPDIR/${SEQ}_manifest.json" ]
}

mkdir -p "$LOCAL_RESULTS"
# Linear main flow: discovery MUST succeed and precede config generation.
if [ "$SKIP_SYNC" = "1" ]; then
    DISCOVERED_DEV=$(discover_device)
    DISCOVERED_DEV="${DISCOVERED_DEV##*/dev/}"
else
    discover_device
    DISCOVERED_DEV="${DISCOVERED_DEV##*/dev/}"
    sync_all
fi
if [ "$SYNC_ONLY" = "1" ]; then echo "sync-only done"; exit 0; fi
remote_guards
fill_once

cp "/tmp/homo_v2_kvmhost_yaml/$CONFIG" "$LOCAL_RESULTS/" 2>/dev/null || true

PASS=0; SKIP=0
for REP in $(seq 1 "$REPS"); do
    for SEQ in $SEQS; do
        if skip_done "$SEQ" "$REP"; then
            echo "[$SEQ rep$REP] resume: manifest already present, skipping"
            SKIP=$((SKIP+1)); continue
        fi
        if run_pair "$SEQ" "$REP"; then
            PASS=$((PASS+1))
        else
            echo "=== ABORT at $SEQ rep$REP (fail-stop) ===" >&2
            exit 1
        fi
    done
done

echo "=== kvmhost homo-v2 SUMMARY: runs=$PASS skipped=$SKIP reps=$REPS seqs=$(echo $SEQS | wc -w) ==="
echo "note: per-(rep,sequence) artifacts live under $LOCAL_RESULTS/rep*/
      aggregate with a builder that parses arm = <id> minus the
      HOMO_<family>_ prefix (build_rq1_results.py ARM_FROM_SEQ semantics);
      the frontier native reference column = HOMO_ctrl_n_none (cross-referenced,
      never re-run)."
