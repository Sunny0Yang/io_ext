#!/usr/bin/env bash
# Canonical cg4 twopanel figure (kvmhost D2 rerun data).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
exec python3 "$ROOT/results/d2/scripts/plot_kvmhost_d2_cg_scale.py"