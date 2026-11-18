#!/usr/bin/env bash
# Re-render the curated M1/M2' motivation figures from the curated data in
# results/m1m2_bm/data (bare-metal run, cache=on campaign).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
FIG="$ROOT/experiments/m1m2_bm/figs"
DATA="$ROOT/results/m1m2_bm/data"
M1_CACHE="$DATA/m1cacheon_bm_20261105"
M1_MATRIX="$DATA/m1matrix_bm_20261105"
M2_RUN="$DATA/m2fig2_bmr2644_20261105"
HERE="$ROOT/results/m1m2_bm/scripts"

python3 "$HERE/render_m1_timeseries_bm.py" \
  --root "$M1_CACHE" \
  --fig-dir "$FIG" \
  --layout 1x4

python3 "$HERE/render_m1_2_bandwidth_bm.py" \
  --data-dir "$M1_MATRIX" \
  --fig-dir "$FIG"

python3 "$HERE/plot_m2_fig2.py" \
  --results "$M2_RUN" \
  --calibration "$M2_RUN/calibration.json" \
  --layout eight-row \
  --out "$FIG/m2_motivation_bmr2644_1x8_corrected"

python3 "$ROOT/experiments/m1m2_bm/scripts/plot_m1_timeseries_m12_bw_row.py" \
  --fig-dir "$FIG"

echo "Wrote figures under $FIG"
