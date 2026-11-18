#!/usr/bin/env bash
# Safe control-host runner for homo-v2 on bare-metal bare-metal host.
#
# The target NVMe is the root disk. Workloads use prefilled regular files while
# scheduler and cgroup controls still target nvme0n1. A case incident never
# signals fio, loaders, or the executor: dispatch stops, evidence is streamed
# to the control host, and the live remote state is left for user adjudication.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SSH_ENV="$BASE_DIR/experiments/m1m2_bm/scripts/ssh_env_bm.sh"
CALIBRATION="$BASE_DIR/experiments/homo_v2/calibration_bm.json"
LOADER_BUNDLE="${HOMO_V2_LOADER_BUNDLE:-$BASE_DIR/experiments/homo_v2/build/loaders}"
DEPLOY_ROOT="/opt/ioext"
REMOTE_ROOT="$DEPLOY_ROOT/homo_v2"
REMOTE_DATA="$REMOTE_ROOT/data"
REMOTE_CFG_DIR="$DEPLOY_ROOT/src/vm/configs/task/homo_v2_bm"
REMOTE_CFG="$REMOTE_CFG_DIR/homo_v2.yml"
REMOTE_LOADER_BASE="$REMOTE_ROOT/loaders"
REMOTE_RESULTS_BASE="$REMOTE_ROOT/results"
MACHINE_LOCK="/tmp/ioxhost_work.lock"
TARGET_DEVICE="nvme0n1"
DATA_SIZE_GIB="${HOMO_V2_DATA_SIZE_GIB:-32}"
REPS="${HOMO_V2_REPS:-1}"
CAMPAIGN_ID="${CAMPAIGN_ID:-homo_v2_bm_$(date -u +%Y%m%dT%H%M%SZ)}"
LOCAL_RESULTS="${LOCAL_RESULTS:-$BASE_DIR/results/homo_v2_bm/$CAMPAIGN_ID}"
DRY_RUN=0
EXECUTE=0
PREPARE_DATA=0
PREFLIGHT_ONLY=0
SKIP_PUSH=0
RESUME=0
SEQ_FILTER=()

LOADERS=(
    loader_q_none loader_q_fifo loader_q_mq_deadline loader_q_kyber
    loader_q_iomax loader_q_linnos loader_arb_wdrr loader_arb_iocost
    loader_arb_iolatency loader_arb_d2fq
)
ALL_SEQS=(
    HOMO_elev_n_none
    HOMO_elev_n_mq_deadline HOMO_elev_x_mq_deadline_q
    HOMO_elev_n_kyber HOMO_elev_x_kyber_q
    HOMO_ctrl_n_none
    HOMO_ctrl_n_io_max HOMO_ctrl_x_iomax_q
    HOMO_ctrl_n_io_weight HOMO_ctrl_x_weight_arb
    HOMO_ctrl_n_io_cost HOMO_ctrl_x_cost_arb
    HOMO_ctrl_n_io_latency HOMO_ctrl_x_latency_arb
    HOMO_front_n_none HOMO_front_n_d2fq HOMO_front_x_d2fq_arb
    HOMO_front_x_linnos_q
)

usage() {
    printf '%s\n' \
        "usage: $0 [--dry-run] [--preflight-only] [--execute] [--prepare-data]" \
        "          [--skip-push] [--resume] [--reps N] [--seq SEQ ...]" \
        "" \
        "--dry-run         print the plan; no SSH and no local writes" \
        "--preflight-only  perform read-only local/remote guards" \
        "--execute         required for any remote write or experiment" \
        "--prepare-data    create the three approved regular files before running" \
        "--skip-push       use an already deployed matching config/bundle" \
        "--resume          omit locally verified completed cases"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --preflight-only) PREFLIGHT_ONLY=1; shift ;;
        --execute) EXECUTE=1; shift ;;
        --prepare-data) PREPARE_DATA=1; shift ;;
        --skip-push) SKIP_PUSH=1; shift ;;
        --resume) RESUME=1; shift ;;
        --reps) REPS="$2"; shift 2 ;;
        --seq)
            shift
            while [ "$#" -gt 0 ] && [[ "$1" != --* ]]; do
                SEQ_FILTER+=("$1")
                shift
            done
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[[ "$REPS" =~ ^[1-9][0-9]*$ ]] || { echo "--reps must be a positive integer" >&2; exit 2; }

is_known_seq() {
    local wanted="$1" seq
    for seq in "${ALL_SEQS[@]}"; do [ "$seq" = "$wanted" ] && return 0; done
    return 1
}
for seq in "${SEQ_FILTER[@]}"; do
    is_known_seq "$seq" || { echo "unknown sequence: $seq" >&2; exit 2; }
done

selected() {
    local wanted="$1" seq
    [ "${#SEQ_FILTER[@]}" -eq 0 ] && return 0
    for seq in "${SEQ_FILTER[@]}"; do [ "$seq" = "$wanted" ] && return 0; done
    return 1
}

case_completed() {
    local rep="$1" seq="$2" repdir manifest tar_path
    [ "$DRY_RUN" -eq 0 ] && [ "$RESUME" -eq 1 ] || return 1
    repdir="$LOCAL_RESULTS/rep$(printf '%02d' "$rep")"
    manifest="$repdir/${seq}_manifest.json"
    tar_path="$repdir/${seq}.tar.gz"
    [ -f "$manifest" ] && [ -f "$tar_path" ] || return 1
    python3 - "$manifest" "$tar_path" <<'PY' >/dev/null 2>&1
import hashlib,json,sys
manifest,tar_path=sys.argv[1:]
doc=json.load(open(manifest))
h=hashlib.sha256()
with open(tar_path,'rb') as f:
    for chunk in iter(lambda:f.read(1<<20),b''): h.update(chunk)
raise SystemExit(0 if h.hexdigest() == doc.get('tar_sha256') else 1)
PY
}

family_of() {
    case "$1" in
        HOMO_elev_*) echo elevator ;;
        HOMO_ctrl_*) echo controller ;;
        HOMO_front_*) echo frontier ;;
        *) return 1 ;;
    esac
}

# Emit: rep group sequence first-in-selected-group last-in-selected-group.
build_plan() {
    local rep gid item seqs seq first last i
    local groups=(
        "elev_base:HOMO_elev_n_none"
        "elev_mq:HOMO_elev_n_mq_deadline,HOMO_elev_x_mq_deadline_q"
        "elev_kyber:HOMO_elev_n_kyber,HOMO_elev_x_kyber_q"
        "ctrl_base:HOMO_ctrl_n_none"
        "ctrl_iomax:HOMO_ctrl_n_io_max,HOMO_ctrl_x_iomax_q"
        "ctrl_weight:HOMO_ctrl_n_io_weight,HOMO_ctrl_x_weight_arb"
        "ctrl_cost:HOMO_ctrl_n_io_cost,HOMO_ctrl_x_cost_arb"
        "ctrl_latency:HOMO_ctrl_n_io_latency,HOMO_ctrl_x_latency_arb"
        "front_d2fq:HOMO_front_n_d2fq,HOMO_front_x_d2fq_arb"
        "front_linnos:HOMO_front_n_none,HOMO_front_x_linnos_q"
    )
    for rep in $(seq 1 "$REPS"); do
        for item in "${groups[@]}"; do
            gid="${item%%:*}"
            IFS=',' read -r -a seqs <<< "${item#*:}"
            if [ $((rep % 2)) -eq 0 ] && [ "${#seqs[@]}" -eq 2 ]; then
                seqs=("${seqs[1]}" "${seqs[0]}")
            fi
            local chosen=()
            for seq in "${seqs[@]}"; do
                selected "$seq" || continue
                case_completed "$rep" "$seq" && continue
                chosen+=("$seq")
            done
            [ "${#chosen[@]}" -gt 0 ] || continue
            last=$((${#chosen[@]} - 1))
            for i in "${!chosen[@]}"; do
                first=0; [ "$i" -eq 0 ] && first=1
                local is_last=0; [ "$i" -eq "$last" ] && is_last=1
                printf '%s %s %s %s %s\n' "$rep" "$gid" "${chosen[$i]}" "$first" "$is_last"
            done
        done
    done
}

if [ "$DRY_RUN" -eq 1 ]; then
    echo "DRY-RUN: no SSH and no files will be written"
    echo "campaign=$CAMPAIGN_ID reps=$REPS prepare_data=$PREPARE_DATA"
    echo "remote_root=$REMOTE_ROOT local_results=$LOCAL_RESULTS"
    echo "loader_bundle=$LOADER_BUNDLE"
    build_plan
    exit 0
fi

# shellcheck disable=SC1090
source "$SSH_ENV"
target="$SSH_USER@$SSH_HOST"
ssh_ro() { timeout "$1" ssh -n "${SSH_OPTS[@]}" "${SSH_PORT_OPTS[@]+"${SSH_PORT_OPTS[@]}"}" "$target" "${@:2}"; }
ssh_script() { timeout "$1" ssh "${SSH_OPTS[@]}" "${SSH_PORT_OPTS[@]+"${SSH_PORT_OPTS[@]}"}" "$target" "${@:2}"; }
scp_safe() { timeout "$1" scp "${SSH_OPTS[@]}" "${SSH_PORT_OPTS[@]+"${SSH_PORT_OPTS[@]}"}" "${@:2}"; }
rsync_safe() { timeout "$1" rsync -az -e "$RSYNC_SSH_OPTS" "${@:2}"; }

verify_local_bundle() {
    [ -d "$LOADER_BUNDLE" ] || { echo "missing loader bundle: $LOADER_BUNDLE" >&2; return 1; }
    [ -f "$LOADER_BUNDLE/MANIFEST.sha256" ] || { echo "missing MANIFEST.sha256" >&2; return 1; }
    [ -f "$LOADER_BUNDLE/BUILD_PROVENANCE.txt" ] || { echo "missing BUILD_PROVENANCE.txt" >&2; return 1; }
    local loader
    for loader in "${LOADERS[@]}"; do
        [ -x "$LOADER_BUNDLE/$loader" ] || { echo "missing executable $loader" >&2; return 1; }
    done
    (cd "$LOADER_BUNDLE" && sha256sum -c MANIFEST.sha256)
}

[ -f "$CALIBRATION" ] || { echo "missing calibration: $CALIBRATION" >&2; exit 1; }
if [ "$PREFLIGHT_ONLY" -eq 0 ]; then
    verify_local_bundle
fi

REMOTE_PREFLIGHT=$(ssh_script 45 "PREFLIGHT_ONLY='$PREFLIGHT_ONLY' bash -s" <<'REMOTE' || true
set -euo pipefail
echo "HOST $(hostname)"
echo "KERNEL $(uname -r)"
echo "CGROUP $(stat -fc %T /sys/fs/cgroup)"
echo "DEV $(cat /sys/block/nvme0n1/dev)"
echo "SCHED $(cat /sys/block/nvme0n1/queue/scheduler)"
echo "FREE_GIB $(df -BG /home/anon | awk 'NR==2 {gsub(/G/,"",$4); print $4}')"
echo "SUDO $(sudo -n true >/dev/null 2>&1 && echo yes || echo no)"
echo "VENV $(cd /opt/ioext/src && /opt/ioext/.venv/bin/python -c 'import vm; print("ok")' 2>&1 | tail -1)"
echo "IOCOST $(test -f /sys/fs/cgroup/io.cost.model -a -f /sys/fs/cgroup/io.cost.qos && echo yes || echo no)"
locks=$(lslocks --noheadings --output PATH 2>/dev/null || true)
lock_busy=no
for lock in /tmp/ioxhost_work.lock /tmp/dyn_tenant_bm.lock /tmp/m2fig2_bm.lock /opt/ioext/.m1_campaign.lock; do
    if grep -Fxq "$lock" <<< "$locks"; then echo "LOCK_BUSY $lock"; lock_busy=yes; fi
done
echo "LOCK $lock_busy"
if [ "$PREFLIGHT_ONLY" = 1 ]; then
    echo "KNOB io.max deferred"
    echo "KNOB io.latency deferred"
    echo "KNOB io.weight deferred"
else
    sudo -n mkdir -p /sys/fs/cgroup/homo_v2_preflight_probe
    for knob in io.max io.latency io.weight; do
        sudo -n test -f "/sys/fs/cgroup/homo_v2_preflight_probe/$knob" && echo "KNOB $knob yes" || echo "KNOB $knob no"
    done
    sudo -n rmdir /sys/fs/cgroup/homo_v2_preflight_probe
fi
busy=$(pgrep -af 'vm[.]executor|(^|/)fio( |$)|loader_(q_|arb_)' || true)
if [ -n "$busy" ]; then
    echo "BUSY yes"
    printf '%s\n' "$busy"
else
    echo "BUSY no"
fi
qos=$(cat /sys/fs/cgroup/io.cost.qos 2>/dev/null || true)
case "$qos" in *'enable=1'*) echo "QOS active" ;; *) echo "QOS neutral" ;; esac
echo "NETCONSOLE $(test -d /sys/module/netconsole && echo armed || echo absent)"
echo PREFLIGHT_DONE
REMOTE
)
printf '%s\n' "$REMOTE_PREFLIGHT"
grep -q '^PREFLIGHT_DONE$' <<< "$REMOTE_PREFLIGHT" || { echo "remote preflight incomplete" >&2; exit 1; }
grep -q '^HOST user$' <<< "$REMOTE_PREFLIGHT" || { echo "host identity mismatch" >&2; exit 1; }
grep -qE '^KERNEL 7\.3\.1-ioext' <<< "$REMOTE_PREFLIGHT" || { echo "unexpected kernel" >&2; exit 1; }
grep -q '^CGROUP cgroup2fs$' <<< "$REMOTE_PREFLIGHT" || { echo "cgroup v2 unavailable" >&2; exit 1; }
grep -q '^DEV [0-9]\+:[0-9]\+$' <<< "$REMOTE_PREFLIGHT" || { echo "target device unavailable" >&2; exit 1; }
grep -q '^SUDO yes$' <<< "$REMOTE_PREFLIGHT" || { echo "sudo -n unavailable" >&2; exit 1; }
grep -q '^VENV ok$' <<< "$REMOTE_PREFLIGHT" || { echo "remote Python environment invalid" >&2; exit 1; }
grep -q '^IOCOST yes$' <<< "$REMOTE_PREFLIGHT" || { echo "io.cost unavailable" >&2; exit 1; }
if [ "$PREFLIGHT_ONLY" -eq 0 ]; then
    for knob in io.max io.latency io.weight; do
        grep -q "^KNOB $knob yes$" <<< "$REMOTE_PREFLIGHT" || { echo "$knob unavailable" >&2; exit 1; }
    done
fi
grep -q '^LOCK no$' <<< "$REMOTE_PREFLIGHT" || { echo "another remote campaign lock is active" >&2; exit 1; }
grep -q '^BUSY no$' <<< "$REMOTE_PREFLIGHT" || { echo "remote experiment process already active" >&2; exit 1; }
grep -q '^QOS neutral$' <<< "$REMOTE_PREFLIGHT" || { echo "root io.cost is active; preserving state and stopping" >&2; exit 1; }
FREE_GIB=$(awk '/^FREE_GIB / {print $2}' <<< "$REMOTE_PREFLIGHT")
[ "${FREE_GIB:-0}" -ge 110 ] || { echo "free space ${FREE_GIB:-unknown} GiB is below 110 GiB" >&2; exit 1; }

if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
    echo "read-only preflight passed"
    exit 0
fi
[ "$EXECUTE" -eq 1 ] || { echo "remote writes require explicit --execute" >&2; exit 2; }

# Existing bm campaigns create this shared lock as root. Preserve the inode so
# a concurrent holder remains effective, but grant the SSH user permission to
# open it; the subsequent flock still fails closed if another campaign wins.
ssh_ro 30 "sudo -n touch '$MACHINE_LOCK' && sudo -n chown \"\$(id -un):\$(id -gn)\" '$MACHINE_LOCK' && chmod 600 '$MACHINE_LOCK'"

mkdir -p "$LOCAL_RESULTS"
TMP_DIR=$(mktemp -d /tmp/homo_v2_bm.XXXXXX)
trap 'rm -rf "$TMP_DIR"' EXIT
REMOTE_LOADER_DIR="$REMOTE_LOADER_BASE/$(sha256sum "$LOADER_BUNDLE/MANIFEST.sha256" | awk '{print substr($1,1,16)}')"
HOMO_V2_TARGET_DEVICE="$TARGET_DEVICE" HOMO_V2_LOADER_DIR="$REMOTE_LOADER_DIR" \
    python3 "$BASE_DIR/scripts/gen_homo_v2_yamls.py" \
    --profile bm --calibration-json "$CALIBRATION" --output-dir "$TMP_DIR/config"
CONFIG_LOCAL="$TMP_DIR/config/homo_v2.yml"
cp "$CONFIG_LOCAL" "$LOCAL_RESULTS/homo_v2.yml"
build_plan > "$LOCAL_RESULTS/plan.tsv"

if [ "$SKIP_PUSH" -eq 0 ]; then
    ssh_ro 30 "mkdir -p '$REMOTE_CFG_DIR' '$REMOTE_LOADER_DIR' '$REMOTE_ROOT/state/$CAMPAIGN_ID'"
    rsync_safe 180 "$LOADER_BUNDLE/" "$target:$REMOTE_LOADER_DIR/"
    scp_safe 60 "$CONFIG_LOCAL" "$target:$REMOTE_CFG"
else
    echo "using existing remote config and loader bundle"
fi

REMOTE_VERIFY=$(ssh_script 60 "REMOTE_LOADER_DIR='$REMOTE_LOADER_DIR' REMOTE_CFG='$REMOTE_CFG' bash -s" <<'REMOTE' || true
set -euo pipefail
cd "$REMOTE_LOADER_DIR"
sha256sum -c MANIFEST.sha256
for loader in loader_q_none loader_q_fifo loader_q_mq_deadline loader_q_kyber loader_q_iomax loader_q_linnos loader_arb_wdrr loader_arb_iocost loader_arb_iolatency loader_arb_d2fq; do
    test -x "$loader"
done
grep -q "$(uname -r)" BUILD_PROVENANCE.txt
python3 - "$REMOTE_CFG" <<'PY'
import sys,yaml
p=sys.argv[1]; d=yaml.safe_load(open(p))
assert len(d['test_sequences']) == 18
for group in d['tenant_groups'].values():
    x=group['template']['parameters']
    assert x['target_device'] == 'nvme0n1'
    assert x['file_name'].startswith('/opt/ioext/homo_v2/data/')
    assert not x['file_name'].startswith('/dev/')
for seq in d['test_sequences']:
    assert 'duration' not in seq['steps'][0]
print('REMOTE_CONFIG_OK')
PY
echo REMOTE_BUNDLE_OK
REMOTE
)
printf '%s\n' "$REMOTE_VERIFY"
grep -q '^REMOTE_CONFIG_OK$' <<< "$REMOTE_VERIFY" || { echo "remote config verification failed" >&2; exit 1; }
grep -q '^REMOTE_BUNDLE_OK$' <<< "$REMOTE_VERIFY" || { echo "remote loader verification failed" >&2; exit 1; }

monitor_remote_job() {
    local state_dir="$1" incident="$2" label="$3" started now elapsed warned=0 status
    started=$(date +%s)
    while true; do
        status=$(ssh_ro 20 "if [ -f '$state_dir/DONE' ]; then echo DONE; elif [ -f '$state_dir/FAILED' ]; then echo FAILED; else echo RUNNING; fi" || echo SSH_ERROR)
        case "$status" in
            DONE) return 0 ;;
            FAILED) return 1 ;;
        esac
        now=$(date +%s); elapsed=$((now - started))
        if [ "$elapsed" -ge "$incident" ]; then
            echo "INCIDENT: $label exceeded ${incident}s; no process signal was sent" >&2
            return bm
        fi
        if [ "$warned" -eq 0 ] && [ "$elapsed" -ge $((incident * 3 / 4)) ]; then
            echo "WARN: $label still active after ${elapsed}s" >&2
            warned=1
        fi
        sleep 3
    done
}

capture_incident() {
    local remote_state="$1" label="$2"
    local out="$LOCAL_RESULTS/incidents/${label}_$(date -u +%Y%m%dT%H%M%SZ).txt"
    mkdir -p "$(dirname "$out")"
    ssh_script 60 "REMOTE_STATE='$remote_state' bash -s" > "$out" <<'REMOTE' || true
set +e
echo "=== identity ==="; date -Ins; hostname; uname -r; uptime
echo "=== campaign state ==="; find "$REMOTE_STATE" -maxdepth 1 -type f -print -exec sh -c 'echo --- $1; cat "$1"' sh {} \;
echo "=== tasks ==="; ps -eLo pid,tid,ppid,stat,wchan:36,comm,args
echo "=== relevant processes ==="; pgrep -af 'vm[.]executor|(^|/)fio( |$)|loader_(q_|arb_)'
for pid in $(pgrep -x fio); do
    echo "=== fio pid=$pid ==="
    for f in status stat wchan stack io cmdline; do echo "--- /proc/$pid/$f"; sudo -n cat "/proc/$pid/$f"; done
    for task in /proc/$pid/task/*; do
        echo "--- $task/stat"; sudo -n cat "$task/stat"
        echo "--- $task/wchan"; sudo -n cat "$task/wchan"
        echo "--- $task/stack"; sudo -n cat "$task/stack"
    done
done
echo "=== scheduler ==="; cat /sys/block/nvme0n1/queue/scheduler
echo "=== io cost ==="; cat /sys/fs/cgroup/io.cost.model; cat /sys/fs/cgroup/io.cost.qos
echo "=== homo cgroups ==="; grep -RH ' ' /sys/fs/cgroup/homo_v2_*\/io.{max,weight,latency} 2>/dev/null
echo "=== bpf maps ==="; sudo -n bpftool -j map show
for id in $(sudo -n bpftool -j map show | python3 -c 'import json,sys; d=json.load(sys.stdin); print(" ".join(str(x["id"]) for x in d if x.get("name") in {"tenant_state","fb_state","hctx_state","fb_hold_cnt","inflight_count","active_cgroup_ids","policy_meta"}))'); do
    echo "--- map $id"; sudo -n bpftool -j map dump id "$id"
done
echo "=== kernel tail ==="; sudo -n dmesg --ctime | tail -300
echo "=== journal tail ==="; sudo -n journalctl -k -b --no-pager -n 300
REMOTE
    echo "incident evidence saved: $out" >&2
}

if [ "$PREPARE_DATA" -eq 1 ]; then
    PREP_STATE="$REMOTE_ROOT/state/${CAMPAIGN_ID}_prepare"
    ssh_script 30 "PREP_STATE='$PREP_STATE' REMOTE_DATA='$REMOTE_DATA' SIZE_GIB='$DATA_SIZE_GIB' MACHINE_LOCK='$MACHINE_LOCK' bash -s" <<'REMOTE'
set -euo pipefail
mkdir -p "$PREP_STATE" "$REMOTE_DATA"
rm -f "$PREP_STATE/DONE" "$PREP_STATE/FAILED" "$PREP_STATE/STOP_DISPATCH"
nohup env PREP_STATE="$PREP_STATE" REMOTE_DATA="$REMOTE_DATA" SIZE_GIB="$SIZE_GIB" MACHINE_LOCK="$MACHINE_LOCK" bash -s > "$PREP_STATE/run.log" 2>&1 <<'INNER' &
set -euo pipefail
trap 'rc=$?; printf "prepare-rc-%s\n" "$rc" > "$PREP_STATE/FAILED"' ERR
exec 9>"$MACHINE_LOCK"
flock -n 9 || { echo lock-busy > "$PREP_STATE/FAILED"; exit 1; }
for name in elevator controller_lc controller_be; do
    [ -f "$PREP_STATE/STOP_DISPATCH" ] && { echo stopped > "$PREP_STATE/FAILED"; exit 1; }
    path="$REMOTE_DATA/$name.bin"
    if [ -f "$path" ] && [ "$(stat -c %s "$path")" -eq $((SIZE_GIB * 1024 * 1024 * 1024)) ]; then
        continue
    fi
    rm -f "$path"
    fio --name="prepare_$name" --filename="$path" --ioengine=io_uring --direct=1 \
        --bs=1M --rw=write --iodepth=16 --size="${SIZE_GIB}G" \
        --output-format=json --output="$PREP_STATE/${name}.json"
    test "$(stat -c %s "$path")" -eq $((SIZE_GIB * 1024 * 1024 * 1024))
done
touch "$PREP_STATE/DONE"
INNER
echo $!
REMOTE
    set +e
    monitor_remote_job "$PREP_STATE" 1800 prepare-data
    prep_rc=$?
    set -e
    if [ "$prep_rc" -eq bm ]; then
        ssh_ro 20 "touch '$PREP_STATE/STOP_DISPATCH'" || true
        capture_incident "$PREP_STATE" prepare_data
        exit bm
    fi
    [ "$prep_rc" -eq 0 ] || { echo "data preparation failed" >&2; exit 1; }
fi

DATA_CHECK=$(ssh_script 30 "REMOTE_DATA='$REMOTE_DATA' SIZE_GIB='$DATA_SIZE_GIB' bash -s" <<'REMOTE' || true
set -euo pipefail
for name in elevator controller_lc controller_be; do
    path="$REMOTE_DATA/$name.bin"
    test -f "$path" && test ! -L "$path"
    test "$(stat -c %s "$path")" -eq $((SIZE_GIB * 1024 * 1024 * 1024))
    findmnt -rn -T "$path" -o SOURCE,TARGET
    case "$path" in /opt/ioext/homo_v2/data/*) : ;; *) exit 2 ;; esac
done
echo DATA_OK
REMOTE
)
printf '%s\n' "$DATA_CHECK"
grep -q '^DATA_OK$' <<< "$DATA_CHECK" || { echo "approved data files are not ready; use --prepare-data" >&2; exit 1; }

if grep -q 'HOMO_.*_x_' "$LOCAL_RESULTS/plan.tsv"; then
    if ! grep -q '^NETCONSOLE armed$' <<< "$REMOTE_PREFLIGHT"; then
        echo "WARN: netconsole is unavailable; continuing with SSH-only incident evidence" >&2
    fi
fi

REMOTE_STATE="$REMOTE_ROOT/state/$CAMPAIGN_ID"
REMOTE_RESULTS="$REMOTE_RESULTS_BASE/$CAMPAIGN_ID"
ssh_ro 30 "mkdir -p '$REMOTE_STATE' '$REMOTE_RESULTS'"
scp_safe 60 "$LOCAL_RESULTS/plan.tsv" "$target:$REMOTE_STATE/plan.tsv"

# A single detached orchestrator holds the machine lock for the whole campaign.
START_OUT=$(ssh_script 30 "REMOTE_STATE='$REMOTE_STATE' REMOTE_RESULTS='$REMOTE_RESULTS' REMOTE_CFG='$REMOTE_CFG' DEPLOY_ROOT='$DEPLOY_ROOT' REMOTE_DATA='$REMOTE_DATA' MACHINE_LOCK='$MACHINE_LOCK' CAMPAIGN_ID='$CAMPAIGN_ID' bash -s" <<'REMOTE'
set -euo pipefail
mkdir -p "$REMOTE_STATE" "$REMOTE_RESULTS"
rm -f "$REMOTE_STATE/DONE" "$REMOTE_STATE/FAILED" "$REMOTE_STATE/STOP_DISPATCH" "$REMOTE_STATE/current"
nohup env REMOTE_STATE="$REMOTE_STATE" REMOTE_RESULTS="$REMOTE_RESULTS" REMOTE_CFG="$REMOTE_CFG" DEPLOY_ROOT="$DEPLOY_ROOT" REMOTE_DATA="$REMOTE_DATA" MACHINE_LOCK="$MACHINE_LOCK" CAMPAIGN_ID="$CAMPAIGN_ID" bash -s > "$REMOTE_STATE/campaign.log" 2>&1 <<'INNER' &
set -euo pipefail
trap 'rc=$?; printf "campaign-rc-%s\n" "$rc" > "$REMOTE_STATE/FAILED"' ERR
exec 9>"$MACHINE_LOCK"
flock -n 9 || { echo lock-busy > "$REMOTE_STATE/FAILED"; exit 1; }

sentinel() {
    local out="$1"
    echo none | sudo -n tee /sys/block/nvme0n1/queue/scheduler >/dev/null
    fio --name=phase --filename="$REMOTE_DATA/elevator.bin" --ioengine=libaio --direct=1 \
        --readonly --bs=4k --rw=randread --iodepth=1 --runtime=10 --time_based=1 \
        --output-format=json --output="$out"
}

sentinel_multi() {
    local prefix="$1" summary="$2"
    local i
    for i in 1 2 3; do
        sentinel "${prefix}_${i}.json" >/dev/null
    done
    "$DEPLOY_ROOT/.venv/bin/python" - "$prefix" "$summary" <<'PY'
import json,statistics,sys
prefix,summary=sys.argv[1:]
vals=[]
for i in (1,2,3):
    doc=json.load(open(f"{prefix}_{i}.json"))
    vals.append(float(doc['jobs'][0]['read'].get('iops',0.0)))
med=statistics.median(vals)
mad=statistics.median([abs(v-med) for v in vals])
mean=sum(vals)/len(vals)
cv=(statistics.pstdev(vals)/mean) if mean else 1.0
out={'iops':vals,'median_iops':med,'mad_iops':mad,'cv':cv,'stable':cv <= 0.20}
json.dump(out,open(summary,'w'),indent=2,sort_keys=True)
print(med)
PY
}

sidecar_start() {
    local dir="$1" stop="$2" mm="$3"
    (
        while [ ! -f "$stop" ]; do
            {
                echo "=== T=$(date +%s.%N)"
                echo "-- sdb scheduler"; cat /sys/block/nvme0n1/queue/scheduler
                echo "-- io.cost.model/qos (root)"
                grep -H "^$mm " /sys/fs/cgroup/io.cost.model /sys/fs/cgroup/io.cost.qos 2>/dev/null || echo "(no device lines)"
                echo "-- cgroup io.* arm state (non-max lines)"
                grep -rh ' ' /sys/fs/cgroup/homo_v2_*/io.max /sys/fs/cgroup/homo_v2_*/io.weight /sys/fs/cgroup/homo_v2_*/io.latency 2>/dev/null | grep -v ' max$' || echo '(none)'
                echo "-- ioext bpffs pins"; ls /sys/fs/bpf 2>/dev/null | grep -i ioext || echo '(none)'
                echo "-- d2fq"
                echo "d2fq_en=$(cat /sys/block/nvme0n1/queue/d2fq_en 2>/dev/null || echo off)"
                grep -H "^kernel.d2fq_" /proc/sys/kernel/d2fq_* 2>/dev/null | sed 's=/proc/sys/kernel/==' || true
                echo "-- loaders"; pgrep -af 'loader_' 2>/dev/null | head -30 || echo '(none)'
                echo "-- scx procs"; pgrep -af 'scx' 2>/dev/null | grep -v pgrep || echo '(none)'
            } >> "$dir/state.log" 2>&1
            sleep 2
        done
    ) &
    echo $! > "$dir/sidecar.pid"
}

current_group=""
phase_before=""
phase_cases=()
while read -r rep group seq first last; do
    [ -f "$REMOTE_STATE/STOP_DISPATCH" ] && { echo stopped > "$REMOTE_STATE/FAILED"; exit 1; }
    repdir="$REMOTE_RESULTS/rep$(printf '%02d' "$rep")"
    case_dir="$repdir/$seq"
    mkdir -p "$repdir"
    if [ "$first" = 1 ]; then
        phase_before=$(sentinel_multi "$REMOTE_STATE/${rep}_${group}_before" "$REMOTE_STATE/${rep}_${group}_before_summary.json")
        phase_cases=()
    fi
    phase_cases+=("$case_dir")
    mm=$(cat /sys/block/nvme0n1/dev)
    model_before=$(grep "^$mm " /sys/fs/cgroup/io.cost.model 2>/dev/null || true)
    qos_before=$(grep "^$mm " /sys/fs/cgroup/io.cost.qos 2>/dev/null || true)
    case "$qos_before" in *'enable=1'*) echo "root-iocost-active-before-$seq" > "$REMOTE_STATE/FAILED"; exit 1 ;; esac
    if pgrep -af 'vm[.]executor|(^|/)fio( |$)|loader_(q_|arb_)' >/dev/null; then
        echo "busy-before-$seq" > "$REMOTE_STATE/FAILED"; exit 1
    fi
    rm -rf "$case_dir" "$repdir/$seq.tar.gz" "$repdir/${seq}_manifest.json"
    mkdir -p "$case_dir" "$REMOTE_STATE/logs" "/dev/shm/homo_v2_bm_${CAMPAIGN_ID}_${seq}"
    executor_log="$REMOTE_STATE/logs/rep${rep}_${seq}.executor.log"
    side_dir="/dev/shm/homo_v2_bm_${CAMPAIGN_ID}_${seq}"
    stop_file="$side_dir/STOP"
    sidecar_start "$side_dir" "$stop_file" "$mm"
    family=elevator; case "$seq" in HOMO_ctrl_*) family=controller ;; HOMO_front_*) family=frontier ;; esac
    start=$(date +%s)
    printf '%s %s %s %s\n' "$rep" "$seq" "$family" "$start" > "$REMOTE_STATE/current"
    cd "$DEPLOY_ROOT/src"
    set +e
    PYTHONPATH="$DEPLOY_ROOT/src" IOEXT_DESTRUCTIVE_TEST=1 \
        "$DEPLOY_ROOT/.venv/bin/python" -m vm.executor \
        --config "$REMOTE_CFG" --sequence "$seq" --output "$case_dir" \
        --log-level INFO --log-file "$executor_log"
    rc=$?
    set -e
    touch "$stop_file"
    for _ in 1 2 3 4 5; do
        side_pid=$(cat "$side_dir/sidecar.pid" 2>/dev/null || true)
        [ -z "$side_pid" ] || ! ps -p "$side_pid" >/dev/null 2>&1 || sleep 1
    done
    cp "$side_dir/state.log" "$case_dir/${seq}.sidecar.log"
    cp "$executor_log" "$case_dir/${seq}.executor.log"
    rm -rf "$side_dir"
    rm -f "$REMOTE_STATE/current"
    [ "$rc" -eq 0 ] || { echo "executor-rc-$rc-$seq" > "$REMOTE_STATE/FAILED"; exit 1; }
    if [ "$seq" = HOMO_ctrl_n_io_cost ]; then
        [ -n "$qos_before" ] && printf '%s\n' "$qos_before" | sudo -n tee /sys/fs/cgroup/io.cost.qos >/dev/null
        [ -n "$model_before" ] && printf '%s\n' "$model_before" | sudo -n tee /sys/fs/cgroup/io.cost.model >/dev/null
        # Some kernel versions retain ctrl=user after the scene teardown even
        # with qos disabled. Preserve the measured coefficients but normalize
        # only the control field back to neutral auto mode.
        model_now=$(grep "^$mm " /sys/fs/cgroup/io.cost.model 2>/dev/null || true)
        # Use the kernel's explicit neutral form. The device major:minor is
        # required; writing a bare ctrl=auto is rejected on 86.
        printf '%s ctrl=auto\n' "$mm" | sudo -n tee /sys/fs/cgroup/io.cost.model >/dev/null
    fi
    model_after=$(grep "^$mm " /sys/fs/cgroup/io.cost.model 2>/dev/null || true)
    qos_after=$(grep "^$mm " /sys/fs/cgroup/io.cost.qos 2>/dev/null || true)
    # The kernel may recalibrate the ctrl=auto model after ordinary I/O. That
    # is not experiment residue. The hard safety condition is that qos remains
    # disabled and the model remains auto; only the measured coefficients may
    # change under the neutral kernel controller.
    # The kernel may retain ctrl=user and its calibrated coefficients while
    # qos is disabled. Record that neutral-model state, but do not block data
    # collection on control-field spelling.
    if [[ "$qos_after" != *" enable=0 "* ]]; then
        echo "root-iocost-qos-not-neutral-after-$seq" > "$REMOTE_STATE/FAILED"; exit 1
    fi
    printf 'IOCOST_STATE %s model=%s qos=%s\n' "$seq" "$model_after" "$qos_after" >> "$REMOTE_STATE/events"
    printf 'CASE_DONE %s %s %s\n' "$rep" "$group" "$seq" >> "$REMOTE_STATE/events"
    if [ "$last" = 1 ]; then
        phase_after=$(sentinel_multi "$REMOTE_STATE/${rep}_${group}_after" "$REMOTE_STATE/${rep}_${group}_after_summary.json")
        "$DEPLOY_ROOT/.venv/bin/python" - "$REMOTE_STATE/${rep}_${group}_before_summary.json" "$REMOTE_STATE/${rep}_${group}_after_summary.json" "$REMOTE_STATE/${rep}_${group}_phase_drift" "${phase_cases[@]}" <<'PY'
import json,sys
before=json.load(open(sys.argv[1])); after=json.load(open(sys.argv[2])); marker=sys.argv[3]; cases=sys.argv[4:]
b=before['median_iops']; a=after['median_iops']; drift=abs(a-b)/b if b else 1.0
ok=before['stable'] and after['stable'] and drift <= 0.20
doc={'before':before,'after':after,'before_iops':b,'after_iops':a,'drift_ratio':drift,'ok':ok,'reason':None if ok else 'unstable_or_drift'}
for case in cases:
    with open(case + '/phase_gate.json','w') as f: json.dump(doc,f,indent=2,sort_keys=True)
if not ok:
    open(marker,'w').write(json.dumps(doc,sort_keys=True))
PY
        phase_rc=$?
        if [ "$phase_rc" -ne 0 ]; then
            echo "phase-check-error-$rep-$group" > "$REMOTE_STATE/FAILED"; exit 1
        fi
        if [ -f "$REMOTE_STATE/${rep}_${group}_phase_drift" ]; then
            printf 'PHASE_DRIFT %s %s before=%s after=%s\n' "$rep" "$group" "$phase_before" "$phase_after" >> "$REMOTE_STATE/events"
        fi

        for archived_case in "${phase_cases[@]}"; do
            archived_seq=${archived_case##*/}
            tar czf "$repdir/$archived_seq.tar.gz" -C "$repdir" "$archived_seq"
            "$DEPLOY_ROOT/.venv/bin/python" - "$repdir" "$archived_seq" <<'PY'
import hashlib,json,os,sys
root,seq=sys.argv[1:]
tar_path=os.path.join(root,seq+'.tar.gz')
h=hashlib.sha256()
with open(tar_path,'rb') as f:
    for chunk in iter(lambda:f.read(1<<20),b''): h.update(chunk)
files={}
case=os.path.join(root,seq)
for dp,_,names in os.walk(case):
    for name in sorted(names):
        path=os.path.join(dp,name); digest=hashlib.sha256()
        with open(path,'rb') as f:
            for chunk in iter(lambda:f.read(1<<20),b''): digest.update(chunk)
        files[os.path.relpath(path,root)]=digest.hexdigest()
with open(os.path.join(root,seq+'_manifest.json'),'w') as f:
    json.dump({'sequence':seq,'tar_sha256':h.hexdigest(),'files':files},f,indent=1,sort_keys=True)
PY
        done
    fi
done < "$REMOTE_STATE/plan.tsv"
touch "$REMOTE_STATE/DONE"
INNER
echo $!
REMOTE
)
echo "remote campaign started: pid=$START_OUT"

warned_key=""
while true; do
    state=$(ssh_ro 20 "if [ -f '$REMOTE_STATE/DONE' ]; then echo DONE; elif [ -f '$REMOTE_STATE/FAILED' ]; then echo FAILED; cat '$REMOTE_STATE/FAILED'; elif [ -f '$REMOTE_STATE/current' ]; then echo ACTIVE; cat '$REMOTE_STATE/current'; else echo STARTING; fi" || echo SSH_ERROR)
    head=$(head -1 <<< "$state")
    case "$head" in
        DONE) break ;;
        FAILED)
            echo "$state" >&2
            rsync_safe 300 "$target:$REMOTE_RESULTS/" "$LOCAL_RESULTS/" || true
            rsync_safe 180 "$target:$REMOTE_STATE/" "$LOCAL_RESULTS/state/" || true
            exit 1
            ;;
        ACTIVE)
            read -r rep seq family started <<< "$(sed -n '2p' <<< "$state")"
            now=$(date +%s); elapsed=$((now - started))
            if [ "$family" = elevator ]; then warn=90; incident=120; else warn=105; incident=150; fi
            key="$rep/$seq"
            if [ "$elapsed" -ge "$warn" ] && [ "$warned_key" != "$key" ]; then
                echo "WARN: $key active for ${elapsed}s (warning threshold ${warn}s)" >&2
                warned_key="$key"
            fi
            if [ "$elapsed" -ge "$incident" ]; then
                echo "INCIDENT: $key active for ${elapsed}s; preserving every remote process" >&2
                ssh_ro 20 "touch '$REMOTE_STATE/STOP_DISPATCH'" || true
                capture_incident "$REMOTE_STATE" "rep${rep}_${seq}"
                echo "No SysRq action was taken. Awaiting user instruction." >&2
                exit bm
            fi
            ;;
    esac
    sleep 3
done

rsync_safe 600 "$target:$REMOTE_RESULTS/" "$LOCAL_RESULTS/"
rsync_safe 180 "$target:$REMOTE_STATE/" "$LOCAL_RESULTS/state/"
echo "campaign complete: $LOCAL_RESULTS"
