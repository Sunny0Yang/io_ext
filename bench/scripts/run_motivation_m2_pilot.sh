#!/usr/bin/env bash
# Run the authorized /dev/sdb M2 pilot on kvmhost.
# Only the reviewed task YAML is synchronized; the dirty host checkout is not rsynced.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE_DIR="${ARCHIVE_DIR:-/home/anon/ioext-ssh}"
SSH_ENV="${SSH_ENV:-$ARCHIVE_DIR/ssh_env.sh}"
GUEST_ROOT="/opt/ioext-bench"
CONFIG_REL="src/vm/configs/task/isol_bench/rig_motivation_io_behavior/m2_ycsb_a_fio_pilot.yml"
CONFIG="$BASE_DIR/$CONFIG_REL"
BUNDLE="$BASE_DIR/results/motivation_io_behavior_20261029_1793274117/motivation_m2_pilot_20261030_bundle.tar.gz"
GUEST_CONFIG="$GUEST_ROOT/$CONFIG_REL"
SEQ="M2_PILOT_NONE_FIO_8M"
RUN_ID="motivation_m2_pilot_20261030"
REMOTE_OUT="/tmp/$RUN_ID"
REMOTE_TAR="${REMOTE_OUT}.tar.gz"
REMOTE_LOG="/tmp/${RUN_ID}.log"
LOCAL_OUT="$BASE_DIR/results/motivation_io_behavior_20261029_1793274117/remote_runs/$RUN_ID"

die() { printf 'BLOCKED: %s\n' "$*" >&2; exit 2; }
[ -f "$SSH_ENV" ] || die "missing archived SSH environment: $SSH_ENV"
[ -f "$CONFIG" ] || die "missing pilot config: $CONFIG"
[ -f "$BUNDLE" ] || die "missing portable Java/pilot bundle: $BUNDLE"
source "$SSH_ENV"
SSH_OPTS+=( -o ServerAliveInterval=15 -o ServerAliveCountMax=4 )
mkdir -p "$LOCAL_OUT"

echo "=== read-only guest preflight ==="
REMOTE_CHECK='set -e;
test "$(hostname)" = target-host;
test -b /dev/sdb;
test "$(lsblk -dnbo SIZE /dev/sdb | tr -d " " )" = 103079215104;
test "$(lsblk -dno RO /dev/sdb | tr -d " " )" = 0;
test -z "$(findmnt -rn -S /dev/sdb -o TARGET || true)";
test -z "$(find /sys/class/block/sdb/holders -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)";
test "$(findmnt -rn -T / -o SOURCE)" != /dev/sdb;
test -z "$(pgrep -x fio || true)";
test -z "$(pgrep -x db_bench || true)";
test ! -e /tmp/vng.lock;
flock -n /tmp/rig_iox_executor.lock -c true;
flock -n /tmp/paper_sdb_executor.lock -c true;
flock -n /tmp/motivation_m2_executor.lock -c true;
test -x /opt/ioext-bench/.venv/bin/python;
test -x /opt/ioext-bench/src/vm/assets/ycsb/ycsb_runtime/bin/ycsb;
test -f /opt/ioext-bench/src/vm/executor.py;
printf "HOST=%s KERNEL=%s\n" "$(hostname)" "$(uname -r)";
lsblk -o NAME,KNAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,RO,MODEL /dev/sdb;
printf "scheduler=%s\n" "$(cat /sys/block/sdb/queue/scheduler)";
uptime'
remote_sudo "$REMOTE_CHECK" | tee "$LOCAL_OUT/preflight.txt"

echo "=== upload portable runtime bundle ==="
GUEST_BUNDLE_TMP="/tmp/${RUN_ID}_bundle.tar.gz"
rsync_cmd -av "$BUNDLE" "$SSH_USER@$SSH_HOST:$GUEST_BUNDLE_TMP"
LOCAL_BUNDLE_SHA="$(sha256sum "$BUNDLE" | awk '{print $1}')"
remote "echo '$LOCAL_BUNDLE_SHA  $GUEST_BUNDLE_TMP' | sha256sum -c -"
remote_sudo "mkdir -p /tmp/${RUN_ID}_bundle; tar xzf '$GUEST_BUNDLE_TMP' -C /tmp/${RUN_ID}_bundle; rm -f '$GUEST_BUNDLE_TMP'; test -x /tmp/${RUN_ID}_bundle/motivation_m2_pilot_20261030/runtime/java-21-openjdk-amd64/bin/java; install -m 0644 /tmp/${RUN_ID}_bundle/motivation_m2_pilot_20261030/src/ycsb_rocksdb.py '$GUEST_ROOT/src/vm/scene/io_ext/ycsb_rocksdb.py'"

 echo "=== sync reviewed pilot config only ==="
GUEST_CONFIG_TMP="/tmp/m2_ycsb_a_fio_pilot.yml"
rsync_cmd -av "$CONFIG" "$SSH_USER@$SSH_HOST:$GUEST_CONFIG_TMP"
remote_sudo "mkdir -p '$(dirname "$GUEST_CONFIG")'; install -m 0644 '$GUEST_CONFIG_TMP' '$GUEST_CONFIG'; rm -f '$GUEST_CONFIG_TMP'"

echo "=== execute authorized raw-sdb pilot ==="
JAVA_HOME="/tmp/${RUN_ID}_bundle/motivation_m2_pilot_20261030/runtime/java-21-openjdk-amd64"
remote_sudo "rm -rf '$REMOTE_OUT' '$REMOTE_TAR' '$REMOTE_LOG' /tmp/motivation_io_behavior_20261029_1793274117/pilot_scene; cd '$GUEST_ROOT' && export JAVA_HOME='$JAVA_HOME' PATH='$JAVA_HOME/bin':\$PATH && java -version && env PERF_ARENA_WORKSPACE='$GUEST_ROOT' PYTHONPATH='$GUEST_ROOT/src' '$GUEST_ROOT/.venv/bin/python' -m vm.executor --config '$GUEST_CONFIG' --sequence '$SEQ' --output '$REMOTE_OUT' --log-file '$REMOTE_LOG' --log-level INFO" | tee "$LOCAL_OUT/console.log"

echo "=== validate and archive ==="
remote_sudo "test -s '$REMOTE_TAR'; test -f '$REMOTE_OUT/metadata.json'; grep -q '\"failed_scenes\": {}' '$REMOTE_OUT/metadata.json'; mkdir -p '$GUEST_ROOT/psdb_tars'; cp -f '$REMOTE_TAR' '$GUEST_ROOT/psdb_tars/${RUN_ID}.tar.gz'; cp -f '$REMOTE_LOG' '$GUEST_ROOT/psdb_tars/${RUN_ID}.log'; ls -lh '$GUEST_ROOT/psdb_tars/${RUN_ID}.tar.gz' '$GUEST_ROOT/psdb_tars/${RUN_ID}.log'"
scp_cmd "$SSH_USER@$SSH_HOST:$GUEST_ROOT/psdb_tars/${RUN_ID}.tar.gz" "$LOCAL_OUT/"
scp_cmd "$SSH_USER@$SSH_HOST:$GUEST_ROOT/psdb_tars/${RUN_ID}.log" "$LOCAL_OUT/"

printf 'pilot complete: %s\n' "$LOCAL_OUT/${RUN_ID}.tar.gz"
