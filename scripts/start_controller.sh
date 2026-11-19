# Start the SDN controller in the foreground.   usage: scripts/start_controller.sh [dynamic|static]
# Use a second terminal for Mininet. Ctrl-C stops it.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-dynamic}"
command -v osken-manager >/dev/null || { echo "osken-manager not found: pip install -r requirements.txt"; exit 1; }
mkdir -p "$ROOT/results"
: > "$ROOT/results/controller_events.jsonl"
echo "Controller mode: $MODE   (events -> results/controller_events.jsonl)"
export EXAM_MODE="$MODE" EXAM_EVENT_LOG="$ROOT/results/controller_events.jsonl" BOTTLENECK_MBPS="${BOTTLENECK_MBPS:-10}"
exec osken-manager --ofp-tcp-listen-port 6653 "$ROOT/src/exam_controller.py"
