# Performance experiments for both controller modes, then the analysis.
#   scripts/run_experiments.sh          full run (about 25-30 minutes)
#   scripts/run_experiments.sh quick    smoke test (about 3 minutes)
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need mn "Install Mininet (see README)."
cd "$ROOT"
STAMP="$(date +%Y%m%d-%H%M%S)"
EXTRA=""; [ "${1:-}" = "quick" ] && EXTRA="--quick"
for mode in static dynamic; do
  echo; echo "=== Controller mode: $mode"
  start_controller_bg "$mode"
  sudo python3 experiments/run_experiments.py --mode "$mode" --events "$EVENTS" --bw "$BW" \
       --out "$RESULTS/experiments-$mode-$STAMP" $EXTRA
  stop_controller
done
python3 experiments/analyze_results.py "$RESULTS/experiments-static-$STAMP" "$RESULTS/experiments-dynamic-$STAMP" \
       --out "$RESULTS/analysis-$STAMP"
