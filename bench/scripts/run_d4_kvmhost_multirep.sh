#!/usr/bin/env bash
# D4 LC burst — multi-rep reruns on kvmhost (paper_sdb PD4).
#
# Pulls each finished sequence tar as paper_sdb_<SEQ>_r<REP>.tar.gz so repeats
# do not overwrite the guest /tmp/paper_sdb_<SEQ>.tar.gz name.
#
# Usage:
#   bash scripts/run_d4_kvmhost_multirep.sh [--reps N] [--dry-run]
#   SEQUENCES="D4LC_io_latency D4LC_none" bash scripts/run_d4_kvmhost_multirep.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE_DIR="${ARCHIVE_DIR:-/home/anon/ioext-ssh}"
SSH_ENV="${SSH_ENV:-$ARCHIVE_DIR/ssh_env.sh}"
GUEST_ROOT="/opt/ioext-bench"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
OUT_DIR="${OUT_DIR:-$ROOT/results/isol_bench/paper/d4_kvmhost_rerun_$RUN_ID}"
REPS="${REPS:-3}"
DRY_RUN=0
SLEEP_BETWEEN="${SLEEP_BETWEEN:-20}"

# Figure arms (repeat) + missing native baselines + extra iox candidates
DEFAULT_SEQUENCES=(
  D4LC_none D4LC_kyber D4LC_bfq D4LC_mq_deadline D4LC_io_max
  D4LC_iox_q_wedf D4LC_iox_q_ddl
  D4LC_io_latency D4LC_io_cost
  D4LC_iox_q_sp_edf D4LC_iox_q_fifo D4LC_iox_q_prio
)

while [ $# -gt 0 ]; do
  case "$1" in
    --reps) REPS="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --run-id) RUN_ID="$2"; OUT_DIR="$ROOT/results/isol_bench/paper/d4_kvmhost_rerun_$RUN_ID"; shift 2 ;;
    -h|--help)
      sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown: $1" >&2; exit 1 ;;
  esac
done

[ -f "$SSH_ENV" ] || { echo "missing $SSH_ENV" >&2; exit 1; }
# shellcheck source=/dev/null
source "$SSH_ENV"

SSH_BASE=(ssh -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=8
  -p "${SSH_PORT:-22}" "$SSH_USER@$SSH_HOST")
RSYNC_SSH_OPTS="-p ${SSH_PORT:-22} -o BatchMode=yes"
rsync_cmd() { rsync -avz -e "ssh $RSYNC_SSH_OPTS" "$@"; }
scp_cmd() { scp -P "${SSH_PORT:-22}" -o BatchMode=yes "$@"; }

remote_sudo() {
  if [ -z "${SUDO_PASS:-}" ]; then
    echo "SUDO_PASS empty in $SSH_ENV" >&2
    exit 1
  fi
  local cmd="$*" pw_b64 cmd_b64
  pw_b64=$(printf '%s\n' "$SUDO_PASS" | base64 -w0)
  cmd_b64=$(printf '%s' "$cmd" | base64 -w0)
  "${SSH_BASE[@]}" "echo ${pw_b64} | base64 -d | sudo -S bash -c \"echo ${cmd_b64} | base64 -d | bash\""
}

run_remote_seq() {
  local seq="$1"
  remote_sudo "cd $GUEST_ROOT && RESUME=0 SLEEP_BETWEEN=$SLEEP_BETWEEN \
    bash scripts/run_paper_sdb.sh --desideratum PD4 --sequence $seq"
}

mapfile -t SEQUENCES < <(printf '%s\n' ${SEQUENCES:-} | grep -v '^$' || true)
if [ "${#SEQUENCES[@]}" -eq 0 ]; then
  SEQUENCES=("${DEFAULT_SEQUENCES[@]}")
fi

mkdir -p "$OUT_DIR"
MANIFEST="$OUT_DIR/manifest.json"
python3 - "$MANIFEST" "$RUN_ID" "$REPS" "${SEQUENCES[@]}" <<'PY'
import json, sys
path, run_id, reps, *seqs = sys.argv[1:]
open(path, "w").write(json.dumps({
    "run_id": run_id,
    "reps": int(reps),
    "sequences": seqs,
    "guest": "kvmhost",
    "desideratum": "PD4",
    "config": "src/vm/configs/task/isol_bench/paper_sdb/d4_burst_lc.yml",
}, indent=2))
PY

echo "=== D4 multirep: run_id=$RUN_ID reps=$REPS out=$OUT_DIR ==="
echo "=== rsync ioext-bench -> guest ==="
if [ "$DRY_RUN" -eq 0 ]; then
  rsync_cmd --exclude .venv --exclude results --exclude .git \
    --exclude '__pycache__' --exclude '*.pyc' \
    "$ROOT/" "$SSH_USER@$SSH_HOST:$GUEST_ROOT/"
else
  echo "[dry-run] rsync skipped"
fi

FAIL=0
for rep in $(seq 1 "$REPS"); do
  for seq in "${SEQUENCES[@]}"; do
    tag="${seq}_r${rep}"
    dest="$OUT_DIR/paper_sdb_${tag}.tar.gz"
  if [ -f "$dest" ]; then
      echo "=== SKIP $tag (local tar exists) ==="
      continue
    fi
    echo "=== $tag $(date -Is) ==="
    if [ "$DRY_RUN" -eq 1 ]; then
      echo "[dry-run] would run $seq rep $rep"
      continue
    fi
    remote_sudo "rm -f /tmp/paper_sdb_${seq}.tar.gz"
    if ! run_remote_seq "$seq"; then
      echo ">>> FAIL $tag"
      FAIL=$((FAIL + 1))
      continue
    fi
    scp_cmd "$SSH_USER@$SSH_HOST:/tmp/paper_sdb_${seq}.tar.gz" "$dest"
    echo "=== saved $dest ==="
    sleep "$SLEEP_BETWEEN"
  done
done

echo "=== done failures=$FAIL manifest=$MANIFEST ==="
exit "$FAIL"