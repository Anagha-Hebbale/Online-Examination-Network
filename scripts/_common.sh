# Shared helpers for the run scripts. Source this file; do not execute it.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RESULTS="$ROOT/results"
EVENTS="$RESULTS/controller_events.jsonl"
BW="${BOTTLENECK_MBPS:-10}"
CONTROLLER_PID=""

need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: '$1' not found. $2"; exit 1; }; }

start_controller_bg() {   # $1 = dynamic | static
  need osken-manager "Activate the Python environment where os-ken is installed (pip install -r requirements.txt)."
  mkdir -p "$RESULTS"; : > "$EVENTS"
  EXAM_MODE="$1" EXAM_EVENT_LOG="$EVENTS" BOTTLENECK_MBPS="$BW" \
    osken-manager --ofp-tcp-listen-port 6653 "$ROOT/src/exam_controller.py" > "$RESULTS/controller_$1.log" 2>&1 &
  CONTROLLER_PID=$!
  sleep 3
  if ! kill -0 "$CONTROLLER_PID" 2>/dev/null; then
    echo "Controller failed to start:"; cat "$RESULTS/controller_$1.log"; exit 1
  fi
  echo "Controller ($1) running, pid $CONTROLLER_PID, log: results/controller_$1.log"
}

stop_controller() {
  if [ -n "$CONTROLLER_PID" ]; then kill "$CONTROLLER_PID" 2>/dev/null || true; wait "$CONTROLLER_PID" 2>/dev/null || true; CONTROLLER_PID=""; fi
}
trap stop_controller EXIT INT TERM
