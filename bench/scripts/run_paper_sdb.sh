#!/bin/bash
# Run paper_sdb (CloudLab dress-rehearsal) sequences on the kvmhost guest.
# Thin sibling of run_rig_iox.sh: same output/tar conventions, rig untouched.
# Usage: run_paper_sdb.sh --desideratum P1|P2|PD2|PD3|PD4|PQ3|PQ4K|PQ4M|PQ5|PQ6|PI6
#                         [--sequence SEQ] [--list]
# Env: SLEEP_BETWEEN cooldown seconds between sequences (default 10).
set -uo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_BASE="/tmp/paper_sdb"
DESIDERATUM=""
SEQUENCE=""
LIST_ONLY=0
SLEEP_BETWEEN="${SLEEP_BETWEEN:-10}"
case "$SLEEP_BETWEEN" in ''|*[!0-9]*) SLEEP_BETWEEN=10 ;; esac
if [ -x "$BASE_DIR/.venv/bin/python" ]; then
    PYTHON="${PYTHON:-$BASE_DIR/.venv/bin/python}"
else
    PYTHON="${PYTHON:-python3}"
fi
LOCK_FILE="/tmp/paper_sdb_executor.lock"

declare -A CONFIG_MAP=(
    [P1]="src/vm/configs/task/isol_bench/paper_sdb/d1_q1_latency.yml"
    [P2]="src/vm/configs/task/isol_bench/paper_sdb/d1_q2_bandwidth.yml"
    [PD2]="src/vm/configs/task/isol_bench/paper_sdb/d2_fairness.yml"
    [PD3]="src/vm/configs/task/isol_bench/paper_sdb/d3_prio_util_lc.yml"
    [PD4]="src/vm/configs/task/isol_bench/paper_sdb/d4_burst_lc.yml"
    [PQ3]="src/vm/configs/task/isol_bench/paper_sdb/d1_q3_decomposition.yml"
    [PQ4K]="src/vm/configs/task/isol_bench/paper_sdb/d1_q4_homomorphism_kyber.yml"
    [PQ4M]="src/vm/configs/task/isol_bench/paper_sdb/d1_q4_homomorphism_mq_deadline.yml"
    [PQ5]="src/vm/configs/task/isol_bench/paper_sdb/d1_q5_tenant_scaling.yml"
    [PQ6]="src/vm/configs/task/isol_bench/paper_sdb/d1_q6_hierarchy_depth.yml"
    [PI6]="src/vm/configs/task/isol_bench/rig_iomaxsim/iomax_fidelity_sdb.yml"
    # 2026-11-02 D2/D3/D4 addendum batch (q+arb arms / hetero points / D4 bw-log fix)
    [PD2QB]="src/vm/configs/task/isol_bench/paper_sdb/addendum/d2qb_qarb.yml"
    [PD3QB]="src/vm/configs/task/isol_bench/paper_sdb/addendum/d3qb_qarb.yml"
    [PD4BW]="src/vm/configs/task/isol_bench/paper_sdb/addendum/d4bw_fix.yml"
)

usage() { sed -n '2,10p' "$0"; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --desideratum) DESIDERATUM="$2"; shift 2 ;;
        --sequence)    SEQUENCE="$2"; shift 2 ;;
        --list)        LIST_ONLY=1; shift ;;
        -h|--help)     usage ;;
        *) echo "unknown: $1" >&2; usage ;;
    esac
done

[ -n "$DESIDERATUM" ] || usage
CONFIG="${CONFIG_MAP[$DESIDERATUM]:-}"
[ -n "$CONFIG" ] || { echo "unknown desideratum: $DESIDERATUM" >&2; exit 1; }
CONFIG_PATH="$BASE_DIR/$CONFIG"
[ -f "$CONFIG_PATH" ] || { echo "missing config: $CONFIG_PATH" >&2; exit 1; }

list_sequences() {
    "$PYTHON" - "$CONFIG_PATH" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1]))
for s in cfg.get("test_sequences", []):
    print(s["sequence_id"])
PY
}

run_one() {
    local seq="$1"
    local out="${OUTPUT_BASE}_${seq}"
    if [ "${RESUME:-1}" = "1" ] && [ -f "${out}.tar.gz" ]; then
        echo "=== SKIP $seq (result tar exists) ==="
        return 2
    fi
    echo "=== START $seq $(date -Is) ==="
    rm -rf "$out" "${out}.tar.gz"
    if ! (cd "$BASE_DIR" && PERF_ARENA_WORKSPACE="$BASE_DIR" PYTHONPATH="$BASE_DIR/src" \
          "$PYTHON" -m vm.executor --config "$CONFIG" --sequence "$seq" \
          --output "$out" --log-level INFO); then
        echo ">>> FAIL $seq"
        return 1
    fi
    # fio-json validity gate (P20-equivalent for the paper track): every
    # produced fio json must parse; a truncated json is a failed sequence so
    # the resume path can rerun it.
    if ! "$PYTHON" - "$out" <<'PYGATE'
import json, glob, sys
bad = []
for f in glob.glob(sys.argv[1] + "/step_*/*/*fio.json"):
    try:
        json.load(open(f))
    except Exception:
        bad.append(f)
if bad:
    print("fio json gate FAIL:", *bad, sep="\n  ")
    sys.exit(1)
PYGATE
    then
        echo ">>> FAIL $seq (fio json gate)"
        return 1
    fi
    # The untarred result dir (bpftrace raw logs compress ~100:1 in the
    # tarball) is what filled the 7.8G tmpfs on 20261022 -- delete it once
    # the tar + json gate confirm the sequence is safely archived.
    rm -rf "$out"
    # controller writes ${out}.tar.gz via its archive step; keep naming aligned
    if [ -d "$out" ] && [ ! -f "${out}.tar.gz" ]; then
        tar czf "${out}.tar.gz" -C "$(dirname "$out")" "$(basename "$out")"
    fi
    # durable copy: /tmp is tmpfs and dies with the VM (allocation releases
    # roll the disk back); stash every result tar on the root disk so a
    # disaster costs at most the in-flight sequence.
    mkdir -p /opt/ioext-bench/psdb_tars 2>/dev/null && cp -n "${out}.tar.gz" /opt/ioext-bench/psdb_tars/ 2>/dev/null
    echo "=== PASS $seq $(date -Is) ==="
}

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "another paper_sdb executor holds $LOCK_FILE" >&2
    exit 1
fi

if [ "$LIST_ONLY" -eq 1 ]; then
    list_sequences
    exit 0
fi

if [ -n "$SEQUENCE" ]; then
    run_one "$SEQUENCE"
    exit $?
fi

mapfile -t SEQS < <(list_sequences)
echo "batch $DESIDERATUM: ${#SEQS[@]} sequences from $CONFIG"
FAILS=0
for seq in "${SEQS[@]}"; do
    run_one "$seq"
    rc=$?
    if [ "$rc" = "1" ]; then
        FAILS=$((FAILS + 1))
    elif [ "$rc" = "2" ]; then
        continue  # skipped: no cooldown sleep on the resume path
    fi
    sleep "$SLEEP_BETWEEN"
done
echo "batch $DESIDERATUM done: ${#SEQS[@]} seqs, $FAILS failures"
exit "$FAILS"