#!/usr/bin/env bash
# Auto-escalate M2 FIO pressure on kvmhost until P99/throughput thresholds are met.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE_DIR="${ARCHIVE_DIR:-/home/anon/ioext-ssh}"
SSH_ENV="${SSH_ENV:-$ARCHIVE_DIR/ssh_env.sh}"
RUN_ID="${RUN_ID:-m2_escalate_20261031}"
BUNDLE="$BASE_DIR/results/motivation_io_behavior_20261029_1793274117/motivation_m2_pilot_20261030_bundle.tar.gz"
RUNNER="$BASE_DIR/scripts/run_motivation_m2_campaign.py"
ESCALATE="$BASE_DIR/scripts/run_motivation_m2_escalate.py"
READ_FACTOR="${READ_FACTOR:-1.30}"
UPDATE_FACTOR="${UPDATE_FACTOR:-1.20}"
TP_FACTOR="${TP_FACTOR:-0.88}"
GUEST_BUNDLE_ROOT="/tmp/motivation_m2_pilot_20261030_bundle"
GUEST_BUNDLE="$GUEST_BUNDLE_ROOT/motivation_m2_pilot_20261030"
REMOTE_OUT="/tmp/$RUN_ID"
REMOTE_TAR="/opt/ioext-bench/psdb_tars/${RUN_ID}.tar.gz"
LOCAL_OUT="$BASE_DIR/results/motivation_io_behavior_20261029_1793274117/remote_runs/$RUN_ID"
GUEST_RUNNER="/tmp/m2_run_motivation_m2_campaign.py"
GUEST_ESCALATE="/tmp/m2_run_motivation_m2_escalate.py"

die() { printf 'BLOCKED: %s\n' "$*" >&2; exit 2; }
[ -f "$SSH_ENV" ] || die "missing SSH env"
[ -f "$BUNDLE" ] || die "missing bundle"
source "$SSH_ENV"
SSH_OPTS+=( -o ServerAliveInterval=15 -o ServerAliveCountMax=8 )
mkdir -p "$LOCAL_OUT"

remote_sudo 'set -e;
test "$(hostname)" = target-host;
test -b /dev/sdb;
test -z "$(findmnt -rn -S /dev/sdb -o TARGET || true)";
test -z "$(pgrep -x fio || true)";
test -z "$(pgrep -x java || true)";
flock -n /tmp/motivation_m2_executor.lock -c true' | tee "$LOCAL_OUT/preflight.txt"

if ! remote_sudo "test -x '$GUEST_BUNDLE/runtime/java-21-openjdk-amd64/bin/java'"; then
  tmp="/tmp/${RUN_ID}_bundle.tar.gz"
  rsync_cmd -av "$BUNDLE" "$SSH_USER@$SSH_HOST:$tmp"
  remote_sudo "rm -rf '$GUEST_BUNDLE_ROOT'; mkdir -p '$GUEST_BUNDLE_ROOT'; tar xzf '$tmp' -C '$GUEST_BUNDLE_ROOT'; rm -f '$tmp'"
fi

rsync_cmd -av "$RUNNER" "$SSH_USER@$SSH_HOST:/tmp/m2_run_motivation_m2_campaign.upload.py"
rsync_cmd -av "$ESCALATE" "$SSH_USER@$SSH_HOST:/tmp/m2_run_motivation_m2_escalate.upload.py"
remote_sudo "install -m 0755 /tmp/m2_run_motivation_m2_campaign.upload.py '$GUEST_RUNNER';
  install -m 0755 /tmp/m2_run_motivation_m2_escalate.upload.py '$GUEST_ESCALATE';
  rm -f /tmp/m2_run_motivation_m2_campaign.upload.py /tmp/m2_run_motivation_m2_escalate.upload.py"

remote_sudo "for mp in \$(findmnt -rn -S /dev/sdb -o TARGET || true); do umount \"\$mp\"; done;
  rm -rf '$REMOTE_OUT';
  python3 '$GUEST_ESCALATE' \
    --bundle '$GUEST_BUNDLE' \
    --runner '$GUEST_RUNNER' \
    --output '$REMOTE_OUT' \
    --read-p99-factor '$READ_FACTOR' \
    --update-p99-factor '$UPDATE_FACTOR' \
    --throughput-factor '$TP_FACTOR'" | tee "$LOCAL_OUT/console.log"

remote_sudo "tar czf '$REMOTE_TAR' -C /tmp '$(basename "$REMOTE_OUT")'; ls -lh '$REMOTE_TAR'"
scp_cmd "$SSH_USER@$SSH_HOST:$REMOTE_TAR" "$LOCAL_OUT/"

mkdir -p "$LOCAL_OUT/campaign"
tar xzf "$LOCAL_OUT/${RUN_ID}.tar.gz" -C "$LOCAL_OUT/campaign"
if [ -f "$LOCAL_OUT/campaign/$RUN_ID/escalation_report.json" ]; then
  cp "$LOCAL_OUT/campaign/$RUN_ID/escalation_report.json" "$LOCAL_OUT/"
fi
printf 'escalation complete: %s\n' "$LOCAL_OUT"
