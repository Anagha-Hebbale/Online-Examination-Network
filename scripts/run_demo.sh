# Narrated live demo.  usage: scripts/run_demo.sh [--step]
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need mn "Install Mininet (see README)."
cd "$ROOT"
start_controller_bg dynamic
sudo python3 experiments/live_demo.py --events "$EVENTS" --bw "$BW" "$@"
