#!/bin/bash
# run_campaign.sh — run the FULL io_ext reproduction campaign inside the guest:
#   for each experiment (in the fixed order below), for each rep 1..5,
#   run the arm set (rotation order per rep) back-to-back with data resets
#   between runs and sentinel bracketing inside each run.
#
# Usage: run_campaign.sh [--reps 5] [--only <algo,...>] [--diag] [--dry-run]
#
# Experiment order (主会话定): io_latency is the plan's highest priority;
# simple steady-state ones first to validate the pipeline, then step/FSM ones.
# LinnOS runs its diagnostic (band-coverage) pass FIRST (--diag) — the
# diagnostic result is NOT part of the formal 5 reps.

set -u

REPS=5
ONLY=""
DIAG=0
DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
    --reps) REPS="$2"; shift 2;;
    --only) ONLY="$2"; shift 2;;
    --diag) DIAG=1; shift;;
    --dry-run) DRY=1; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
    esac
done

LIB_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$LIB_DIR/ioext_repro_lib.sh"

# algo -> arm list (d2fq/linnos have no native arm in the VM: plan decision
# "VM 内 Native N/A"; the bare-metal front_n_d2fq three-arm campaign is a
# separate follow-up outside this tree).
ARMS_mq_deadline="none native iox"
ARMS_kyber="none native iox"
ARMS_io_max="none native iox"
ARMS_io_weight="none native iox"
ARMS_io_cost="none native iox"
ARMS_io_latency="none native iox"
ARMS_d2fq="none iox"
ARMS_linnos="none iox"

ORDER_DEFAULT="mq_deadline io_max io_latency kyber io_cost io_weight d2fq linnos"
if [ -n "$ONLY" ]; then
    ORDER="$(echo "$ONLY" | tr ',' ' ')"
else
    ORDER="$ORDER_DEFAULT"
fi

CAMPAIGN_ID="repro_$(date +%Y%m%d_%H%M%S)"
CAMPAIGN_ROOT="${RESULTS_ROOT}/${CAMPAIGN_ID}"
mkdir -p "$CAMPAIGN_ROOT"
echo "[campaign] id=$CAMPAIGN_ID reps=$REPS order=[$ORDER] diag=$DIAG" | tee "$CAMPAIGN_ROOT/campaign.log"

if [ "$DRY" = 1 ]; then
    for algo in $ORDER; do
        echo "DRY: $algo arms: $(eval echo \$ARMS_$algo)"
    done
    exit 0
fi

# LinnOS diagnostic: one --diag run per arm to verify band coverage, recorded
# separately (diag_ prefix; excluded from formal reps by the analyzer).
if [ "$DIAG" = 1 ] && echo "$ORDER" | grep -q linnos; then
    for arm in $ARMS_linnos; do
        d="$CAMPAIGN_ROOT/linnos/diag/rep0/$arm"
        mkdir -p "$d"
        RUN_DIR="$d" bash "$LIB_DIR/run_experiment.sh" --algo linnos --arm "$arm" \
            --rep 0 --out "$d" >> "$CAMPAIGN_ROOT/campaign.log" 2>&1 \
            || echo "[campaign] linnos diag $arm FAILED" >> "$CAMPAIGN_ROOT/campaign.log"
    done
fi

for algo in $ORDER; do
    arms_var="ARMS_$algo"
    arms="${!arms_var:-none native iox}"
    for rep in $(seq 1 "$REPS"); do
        # rotation order per rep (order bias control; same conditions each rep)
        # rep1 = given order; rep2 = reversed; rep3/4 = rotate left 1/2; rep5 = given
        rot() { local n=$1; shift; local arr=("$@"); local len=${#arr[@]}; local out=""; local i; for ((i=0;i<len;i++)); do out="$out ${arr[$(( (i+n) % len ))]}"; done; echo "$out"; }
        case $rep in
        1) arm_list="$arms" ;;
        2) arm_list="$(rot 1 $arms)" ;;
        3) arm_list="$(rot 2 $arms)" ;;
        4) arm_list="$(rot 1 $arms)" ;;
        *) arm_list="$arms" ;;
        esac
        for arm in $arm_list; do
            d="$CAMPAIGN_ROOT/$algo/rep$rep/$arm"
            mkdir -p "$d"
            echo "[campaign] run $algo rep$rep $arm -> $d" >> "$CAMPAIGN_ROOT/campaign.log"
            RUN_DIR="$d" bash "$LIB_DIR/run_experiment.sh" \
                --algo "$algo" --arm "$arm" --rep "$rep" --out "$d" \
                >> "$d/runner_stdout.log" 2>&1
            rc=$?
            echo "[campaign] $algo rep$rep $arm rc=$rc" >> "$CAMPAIGN_ROOT/campaign.log"
            if [ $rc -ne 0 ]; then
                # fail-stop on unexpected failures, but never on the two
                # declared N/A arms (activate returns 2)
                echo "[campaign] FAIL-STOP at $algo rep$rep $arm" >> "$CAMPAIGN_ROOT/campaign.log"
                exit 1
            fi
        done
    done
done

echo "[campaign] DONE $CAMPAIGN_ROOT" | tee -a "$CAMPAIGN_ROOT/campaign.log"
