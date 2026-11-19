# Demo script (about 5 minutes)

## Before the demo

```bash
sudo mn -c ; sudo pkill -f osken-manager      # clean slate
python3 -m unittest tests/test_controller_logic.py   # sanity, 1 s
```
Have these ready as a fallback: `results/mininet_tests-<time>/report.md`, the charts in `results/analysis-<time>/`, and a screen recording of one full run.

## Option A – narrated automatic demo

```bash
scripts/run_demo.sh --step        # press Enter between steps
```
It starts the controller, builds the network and walks through: connectivity → one exam → flow rules → normal load → flood → controller reaction → recovery.

## Option B – manual (more flexible for questions)

Terminal 1: `scripts/start_controller.sh dynamic`
Terminal 2: `sudo python3 src/exam_topology.py`

| Step | Command | Say |
|---|---|---|
| 1 | `mininet> pingall` | 3 subnets, Linux router, 3 OpenFlow switches, one controller |
| 2 | `mininet> student1 python3 src/client.py` (answers 3, 2, 1) | TCP exam, JSON lines; score at the end |
| 3 | `mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1` | priority 20 = exam traffic with its own counters; 30 = signalling; 0 = table-miss |
| 4 | Terminal 3: `tail -f results/controller_events.jsonl \| grep -v STATS` | the controller's own log: heartbeats from the application |
| 5 | `mininet> student3 python3 src/load_test.py --clients 6 --duration 40 --live &` | steady exam load, response time ≈ a few ms |
| 6 | `mininet> rogue python3 src/traffic_gen.py flood --mbps 20 --duration 25 &` | a noisy host saturates the 10 Mbit/s link: the live response time jumps |
| 7 | watch terminal 3 | `CONGESTION_DETECTED` → `MITIGATION_INSTALLED` (offender 10.0.1.66) a few seconds later; response time falls back |
| 8 | `mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1 \| grep priority=18` | the drop rule for the rogue host |
| 9 | wait ~15 s after the flood ends | `MITIGATION_RELEASED`; `mininet> rogue ping -c 2 examserver` works again |

For the **baseline** comparison, restart the controller with `static` and repeat steps 5-6: detection is logged, nothing is blocked, latency stays high. Then show `results/analysis-*/summary.md` and `timeline_n10.png`.

## If something goes wrong

* Switches not connected → the controller is not running or another one holds port 6653 (`sudo pkill -f osken-manager`).
* Odd Mininet state → `sudo mn -c`.
* Show the saved evidence instead of arguing with the live system.
