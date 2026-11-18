#!/usr/bin/env bash
# High-pressure M2 none curve on authorized kvmhost /dev/sdb via offline runner.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE_DIR="${ARCHIVE_DIR:-/home/anon/ioext-ssh}"
SSH_ENV="${SSH_ENV:-$ARCHIVE_DIR/ssh_env.sh}"
RUN_ID="${RUN_ID:-m2_curve_high_20261030}"
BUNDLE="$BASE_DIR/results/motivation_io_behavior_20261029_1793274117/motivation_m2_pilot_20261030_bundle.tar.gz"
RUNNER="$BASE_DIR/scripts/run_motivation_m2_campaign.py"
RATES="${RATES:-0,32,64,128,max}"
REPEATS="${REPEATS:-3}"
FIO_PROFILES="${FIO_PROFILES:-}"
FIO_IODEPTH="${FIO_IODEPTH:-16}"
FIO_NUMJOBS="${FIO_NUMJOBS:-1}"
GUEST_BUNDLE_ROOT="/tmp/motivation_m2_pilot_20261030_bundle"
GUEST_BUNDLE="$GUEST_BUNDLE_ROOT/motivation_m2_pilot_20261030"
REMOTE_OUT="/tmp/$RUN_ID"
REMOTE_TAR="/opt/ioext-bench/psdb_tars/${RUN_ID}.tar.gz"
LOCAL_OUT="$BASE_DIR/results/motivation_io_behavior_20261029_1793274117/remote_runs/$RUN_ID"

die() { printf 'BLOCKED: %s\n' "$*" >&2; exit 2; }
[ -f "$SSH_ENV" ] || die "missing SSH env: $SSH_ENV"
[ -f "$BUNDLE" ] || die "missing bundle: $BUNDLE"
[ -f "$RUNNER" ] || die "missing runner: $RUNNER"
source "$SSH_ENV"
SSH_OPTS+=( -o ServerAliveInterval=15 -o ServerAliveCountMax=8 )
mkdir -p "$LOCAL_OUT"

echo "=== read-only guest preflight ==="
REMOTE_CHECK='set -e;
test "$(hostname)" = target-host;
test -b /dev/sdb;
test "$(lsblk -dnbo SIZE /dev/sdb | tr -d " ")" = 103079215104;
test -z "$(findmnt -rn -S /dev/sdb -o TARGET || true)";
test -z "$(find /sys/class/block/sdb/holders -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)";
test "$(findmnt -rn -T / -o SOURCE)" != /dev/sdb;
test -z "$(pgrep -x fio || true)";
test -z "$(pgrep -x java || true)";
flock -n /tmp/paper_sdb_executor.lock -c true;
flock -n /tmp/rig_iox_executor.lock -c true;
flock -n /tmp/motivation_m2_executor.lock -c true;
printf "HOST=%s KERNEL=%s\n" "$(hostname)" "$(uname -r)";
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS /dev/sdb'
remote_sudo "$REMOTE_CHECK" | tee "$LOCAL_OUT/preflight.txt"

echo "=== ensure portable bundle on guest ==="
if ! remote_sudo "test -x '$GUEST_BUNDLE/runtime/java-21-openjdk-amd64/bin/java' && test -x '$GUEST_BUNDLE/bin/fio'"; then
  GUEST_BUNDLE_TMP="/tmp/${RUN_ID}_bundle.tar.gz"
  rsync_cmd -av "$BUNDLE" "$SSH_USER@$SSH_HOST:$GUEST_BUNDLE_TMP"
  LOCAL_BUNDLE_SHA="$(sha256sum "$BUNDLE" | awk '{print $1}')"
  remote "echo '$LOCAL_BUNDLE_SHA  $GUEST_BUNDLE_TMP' | sha256sum -c -"
  remote_sudo "rm -rf '$GUEST_BUNDLE_ROOT'; mkdir -p '$GUEST_BUNDLE_ROOT'; tar xzf '$GUEST_BUNDLE_TMP' -C '$GUEST_BUNDLE_ROOT'; rm -f '$GUEST_BUNDLE_TMP'"
fi

echo "=== upload campaign runner ==="
GUEST_RUNNER="/tmp/m2_run_motivation_m2_campaign.py"
rsync_cmd -av "$RUNNER" "$SSH_USER@$SSH_HOST:/tmp/m2_run_motivation_m2_campaign.upload.py"
remote_sudo "install -m 0755 /tmp/m2_run_motivation_m2_campaign.upload.py '$GUEST_RUNNER'; rm -f /tmp/m2_run_motivation_m2_campaign.upload.py"

FIO_EXTRA=(--fio-iodepth "$FIO_IODEPTH" --fio-numjobs "$FIO_NUMJOBS")
if [ -n "$FIO_PROFILES" ]; then
  FIO_EXTRA+=(--fio-profiles "$FIO_PROFILES")
fi
FIO_EXTRA_QUOTED=""
for arg in "${FIO_EXTRA[@]}"; do
  FIO_EXTRA_QUOTED+="$(printf '%q ' "$arg")"
done

echo "=== execute high-pressure campaign (rates=$RATES repeats=$REPEATS fio_profiles=${FIO_PROFILES:-default}) ==="
remote_sudo "for mp in \$(findmnt -rn -S /dev/sdb -o TARGET || true); do umount \"\$mp\"; done; rm -rf '$REMOTE_OUT'; python3 '$GUEST_RUNNER' \
  --bundle '$GUEST_BUNDLE' \
  --output '$REMOTE_OUT' \
  --allow-format \
  --schedulers none \
  --rates '$RATES' \
  --repeats '$REPEATS' \
  $FIO_EXTRA_QUOTED" | tee "$LOCAL_OUT/console.log"

echo "=== archive and pull ==="
remote_sudo "test \"\$(find '$REMOTE_OUT' -mindepth 1 -name result.json | wc -l)\" -gt 0;
  tar czf '$REMOTE_TAR' -C /tmp '$(basename "$REMOTE_OUT")';
  ls -lh '$REMOTE_TAR'"
scp_cmd "$SSH_USER@$SSH_HOST:$REMOTE_TAR" "$LOCAL_OUT/"
remote_sudo "tar xzf '$REMOTE_TAR' -C /tmp"

echo "=== local extract + analyze ==="
CAMPAIGN_DIR="$LOCAL_OUT/campaign"
mkdir -p "$CAMPAIGN_DIR"
tar xzf "$LOCAL_OUT/${RUN_ID}.tar.gz" -C "$CAMPAIGN_DIR"
python3 "$BASE_DIR/scripts/analyze_m2_campaign.py" \
  --input "$CAMPAIGN_DIR/$RUN_ID" \
  --output "$LOCAL_OUT/analysis"
printf 'high-pressure campaign complete: %s\n' "$LOCAL_OUT"
