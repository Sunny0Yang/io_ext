#!/bin/bash
# drive.sh — workstation-side driver for the io_ext reproduction campaign.
# Pushes guest scripts + loader bundle to the VM (via bm), runs the campaign
# inside the guest, and pulls the results tree back.
#
# Usage:
#   drive.sh push            # upload bundle+scripts to guest
#   drive.sh smoke           # 1-run pipeline check (mq_deadline none rep0)
#   drive.sh diag-linnos     # linnos band-coverage diagnostic
#   drive.sh campaign        # full campaign (reps 5, fixed order)
#   drive.sh pull            # pull results tree to workstation
#   drive.sh status          # VM + guest reachability check
#
# Environment: ssh alias `bm` (see experiments/m1m2_bm/scripts/ssh_env_bm.sh).
# Guest is reached THROUGH bm: ssh -p 2222 <guest_user>@127.0.0.1 (port fwd
# by qemu, see ENVIRONMENT_REPORT.md).

set -u
HOST_BM="bm"
GUEST_PORT="${GUEST_PORT:-2222}"
GUEST_USER="${GUEST_USER:-user}"
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"          # experiments/reproduction/
BUNDLE="$LOCAL_ROOT/loaders_stats1"
GUEST_SCRIPTS="$LOCAL_ROOT/guest"
GUEST_DEPLOY="/opt/ioext_repro"
LOCAL_RESULTS="${LOCAL_ROOT}/results"

SSH_BM_OPTS=(-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null
            -o ConnectTimeout=10 -o BatchMode=yes)

# Guest is reached directly from the workstation via ProxyJump through bm
# (qemu hostfwd 127.0.0.1:2222 -> guest 22; dedicated key per ENVIRONMENT_REPORT).
GUEST_SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null
                -o ConnectTimeout=10 -o BatchMode=yes
                -o IdentitiesOnly=yes -i "$IOEXT_SSH_KEY"
                -J "$HOST_BM" -p "$GUEST_PORT" "$GUEST_USER@127.0.0.1")

g_ssh() {  # run command in guest (direct via ProxyJump)
    ssh "${GUEST_SSH_OPTS[@]}" "$*"
}

g_sudo() {
    ssh "${GUEST_SSH_OPTS[@]}" "sudo -n sh -c '$*'"
}

case "${1:-}" in
status)
    echo "== bm reachable:"; ssh "${SSH_BM_OPTS[@]}" "$HOST_BM" 'hostname; uname -r'
    echo "== qemu running:"; ssh "${SSH_BM_OPTS[@]}" "$HOST_BM" 'pgrep -af qemu-system | head -1 || echo NONE'
    echo "== guest reachable:"; g_ssh "'hostname; uname -r; cat /sys/block/vdb/queue/scheduler 2>/dev/null'"
    ;;
push)
    echo "== push loader bundle + scripts"
    ssh "${SSH_BM_OPTS[@]}" "$HOST_BM" "mkdir -p /tmp/ioext_repro_push"
    scp "${SSH_BM_OPTS[@]}" -r "$BUNDLE" "$HOST_BM:/tmp/ioext_repro_push/loaders_stats1"
    scp "${SSH_BM_OPTS[@]}" -r "$GUEST_SCRIPTS" "$HOST_BM:/tmp/ioext_repro_push/scripts"
    g_sudo "mkdir -p $GUEST_DEPLOY"
    ssh "${SSH_BM_OPTS[@]}" "$HOST_BM" "scp -q -P $GUEST_PORT -r /tmp/ioext_repro_push/loaders_stats1 /tmp/ioext_repro_push/scripts/* $GUEST_USER@127.0.0.1:/tmp/ioext_repro_stage/"
    g_sudo "cp -r /tmp/ioext_repro_stage/loaders_stats1 $GUEST_DEPLOY/ && cp /tmp/ioext_repro_stage/*.sh $GUEST_DEPLOY/ && chmod +x $GUEST_DEPLOY/*.sh $GUEST_DEPLOY/loaders_stats1/loader_* && sha256sum $GUEST_DEPLOY/loaders_stats1/loader_* > $GUEST_DEPLOY/loaders_stats1/MANIFEST.sha256 && sha256sum -c $GUEST_DEPLOY/loaders_stats1/MANIFEST.sha256"
    echo "== push done"
    ;;
smoke)
    echo "== pipeline smoke: mq_deadline/none rep0 (60 s)"
    g_sudo "RESULTS_ROOT=/data/results $GUEST_DEPLOY/run_experiment.sh --algo mq_deadline --arm none --rep 0 --out /data/results/smoke_mq_none"
    ;;
diag-linnos)
    g_sudo "$GUEST_DEPLOY/run_campaign.sh --diag --only linnos"
    ;;
campaign)
    shift 2>/dev/null || true
    echo "== full campaign"
    g_sudo "$GUEST_DEPLOY/run_campaign.sh --reps ${REPS:-5}"
    ;;
pull)
    CAM="${2:-}"
    [ -n "$CAM" ] || { echo "usage: drive.sh pull <campaign_id>"; exit 2; }
    mkdir -p "$LOCAL_RESULTS"
    echo "== pull $CAM"
    g_ssh "'cd /data/results && tar czf /tmp/$CAM.tar.gz $CAM'"
    ssh "${SSH_BM_OPTS[@]}" "$HOST_BM" \
        "scp -q -P $GUEST_PORT $GUEST_USER@127.0.0.1:/tmp/$CAM.tar.gz /tmp/ && scp -q /tmp/$CAM.tar.gz user@localhost:/tmp/$CAM.tar.gz 2>/dev/null || true; cp /tmp/$CAM.tar.gz /opt/ioext/ioext_repro_vm/$CAM.tar.gz"
    scp "${SSH_BM_OPTS[@]}" "$HOST_BM:/opt/ioext/ioext_repro_vm/$CAM.tar.gz" "$LOCAL_RESULTS/"
    tar xzf "$LOCAL_RESULTS/$CAM.tar.gz" -C "$LOCAL_RESULTS/"
    echo "== pulled to $LOCAL_RESULTS/$CAM"
    ;;
*)
    echo "usage: drive.sh {status|push|smoke|diag-linnos|campaign|pull <id>}" >&2
    exit 2
    ;;
esac
