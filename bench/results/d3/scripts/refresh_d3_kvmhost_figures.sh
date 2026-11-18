#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
python3 "$ROOT/scripts/plot_d3_kvmhost_main.py"
python3 "$ROOT/scripts/plot_d3_kvmhost_pareto_merged.py"
python3 "$ROOT/scripts/plot_d3_d4_rq13_combo.py"
python3 "$ROOT/scripts/plot_d2_d3_d4_rq13_row.py"
python3 "$ROOT/scripts/crop_fig_bottom_blank.py" --in-place --pad-pt 2.5 \
  "$ROOT/experiments/d3_kvmhost/figures/fig_d2_d3_d4_rq13_row.pdf"