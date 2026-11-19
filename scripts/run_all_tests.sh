# Runs every test case and saves the evidence under results/.   usage: scripts/run_all_tests.sh
#   1. controller logic unit tests (no Mininet)
#   2. application tests on localhost (no Mininet)
#   3. Mininet end-to-end tests T1-T8 with the controller in dynamic mode (needs sudo)
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need python3 ""; need mn "Install Mininet (see README)."
cd "$ROOT"; mkdir -p "$RESULTS"
status=0

echo "=== [1/3] Controller logic unit tests"
python3 -m unittest tests/test_controller_logic.py 2>&1 | tee "$RESULTS/unit_tests.txt" | tail -4 || status=1

echo; echo "=== [2/3] Application tests (localhost)"
python3 tests/app_tests.py --out "$RESULTS" || status=1

echo; echo "=== [3/3] Mininet tests T1-T8 (controller: dynamic)"
start_controller_bg dynamic
sudo python3 tests/mininet_tests.py --expect dynamic --events "$EVENTS" --bw "$BW" || status=1
stop_controller

echo; [ $status -eq 0 ] && echo "ALL TESTS PASSED" || echo "SOME TESTS FAILED - see results/"
exit $status
