#!/usr/bin/env bash
# scripts/orchestrate_d2_rerun_kvmhost.sh
# Wait for the kvmhost window, confirm idle, then launch the D2 rerun campaign.
#
# Contract (user decision 2026-11-14): prepare locally, wait 2h, confirm
# kvmhost has NO experiment running, then start. The in-flight dyn_tenant
# x5-x7 batch holds a 7200 s self-timeout, so 2h covers it; after that we
# still verify idleness ourselves and keep polling if not idle (give up 4h).
set -uo pipefail
BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_TAG="[orch]"
WAIT_SEC="${WAIT_SEC:-7200}"
IDLE_NEEDED="${IDLE_NEEDED:-2}"
IDLE_GAP="${IDLE_GAP:-300}"
GIVEUP_SEC="${GIVEUP_SEC:-14400}"

echo "$LOG_TAG waiting ${WAIT_SEC}s before touching kvmhost ($(date -Is) -> $(date -d "+${WAIT_SEC} sec" -Is 2>/dev/null || echo '+2h'))"
sleep "$WAIT_SEC"

idle_checks=0
t0=$(date +%s)
while true; do
    now=$(date +%s)
    if [ $((now - t0)) -gt "$GIVEUP_SEC" ]; then
        echo "$LOG_TAG GIVEUP: kvmhost still busy after $((GIVEUP_SEC/60)) min — not starting D2 rerun"
        exit 3
    fi
    # local control-side drivers
    if pgrep -af "run_dyn_tenant_kvmhost|run_homo_v2_kvmhost|run_ycsb_iox_kvmhost|run_rq5_arm_sweep_kvmhost" \
        | grep -v orchestrate_ >/dev/null 2>&1; then
        echo "$LOG_TAG busy: local kvmhost driver still running"; sleep "$IDLE_GAP"; continue
    fi
    # remote: executor / fio / loaders
    # shellcheck disable=SC1090
    source /home/anon/ioext-ssh/ssh_env.sh
    remote=$(timeout 25 ssh -p "$SSH_PORT" -o ConnectTimeout=10 "$SSH_USER@$SSH_HOST" \
        "pgrep -c -f 'vm.executor|loader_' 2>/dev/null; pgrep -c -x fio 2>/dev/null; echo SEP" 2>/dev/null)
    rc=$?
    if [ "$rc" -ne 0 ] || ! echo "$remote" | grep -q SEP; then
        echo "$LOG_TAG warn: kvmhost unreachable (rc=$rc) — retry in 5 min"; sleep 300; continue
    fi
    n_exec=$(echo "$remote" | sed -n 1p); n_fio=$(echo "$remote" | sed -n 2p)
    if [ "${n_exec:-1}" != "0" ] || [ "${n_fio:-1}" != "0" ]; then
        echo "$LOG_TAG busy: remote exec=$n_exec fio=$n_fio"; sleep "$IDLE_GAP"; continue
    fi
    idle_checks=$((idle_checks + 1))
    echo "$LOG_TAG idle check $idle_checks/$IDLE_NEEDED OK"
    if [ "$idle_checks" -ge "$IDLE_NEEDED" ]; then break; fi
    sleep "$IDLE_GAP"
done

echo "$LOG_TAG kvmhost confirmed idle — launching D2 rerun campaign"
bash "$BASE_DIR/scripts/run_d2_rerun_kvmhost.sh" --sync
rc=$?
echo "$LOG_TAG campaign finished rc=$rc"
exit "$rc"
