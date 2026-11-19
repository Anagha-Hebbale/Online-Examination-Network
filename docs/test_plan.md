# Test plan

Run everything with `scripts/run_all_tests.sh`. Evidence goes to `results/`. Fill the **Actual** column from your own runs.

## Level 1 – controller logic (no Mininet)  `python3 -m unittest tests/test_controller_logic.py -v`

Fake switches record every OpenFlow message; fake statistics replies are fed to the real handlers. Proves the logic, not Open vSwitch behaviour.

| Group | Checks | Expected | Actual |
|---|---|---|---|
| Handshake and forwarding (4) | table-miss + signalling rule on connect; exam traffic → priority 20 with `tcp_dst`/`tcp_src`; other IP → priority 15 only; ARP → no rule | pass | |
| Signalling (4) | heartbeat updates state and is consumed; datagram from non-server rejected; malformed rejected; start/end counted | pass | |
| Dynamic mitigation (11) | no false positive; top talker quarantined on all switches with a drop rule; needs consecutive hot polls; chooses the heaviest of several; server never chosen; app gate (no active exam → no action); stale-heartbeat fallback; release after timer; reset message; STATS records | pass | |
| Static baseline (1) | congestion observed, nothing installed | pass | |

## Level 2 – application (no Mininet)  `python3 tests/app_tests.py`  (also run from a Mininet host in T2)

| ID | Scenario | Expected | Actual |
|---|---|---|---|
| A1 | all answers correct | 3/3, `time_expired=false` | |
| A2 | all wrong | 0/3 | |
| A3 | mixed | 2/3 | |
| A4 | invalid choices `0, 9, -1, "2", null, true, 2.5` | each gets `error`; same question; exam completes | |
| A5 | wrong message type mid-exam | `error` | |
| A6 | malformed JSON | that connection closed; server still serves | |
| A7 | non-object JSON | handled without crash | |
| A8 | first message is not `join` | connection closed | |
| A9 | 200-character name | truncated to 40 | |
| A10 | exam timer expires | `result` with `time_expired=true`, answered=1 | |
| A11 | client disconnects mid-exam | server keeps serving | |
| A12 | 30 simultaneous students | all 3/3 | |
| A13 | signalling datagrams (local only) | `exam_start`, `exam_end`, `heartbeat` received | |
| A14 | results file (local only) | one line per finished exam | |

## Level 3 – Mininet + controller  `sudo python3 tests/mininet_tests.py`

| ID | Procedure | Pass criterion | Evidence | Actual |
|---|---|---|---|---|
| T1 | `pingAll` twice, `student1 → server` ping | 0 % loss | `T1_ping_*.txt` | |
| T2 | `app_tests.py` run from `student1` against `10.0.3.100` | exit code 0 | `T2_app_tests_from_student1.txt` | |
| T3 | 20 clients from 4 hosts, barrier-synchronised | 20/20 exams completed | `T3/*.csv` | |
| T4 | one exam, then `dump-flows` on s1, s2, s3 | exam rule (priority 20, `tp_dst=5050`, packets > 0), reverse rule, signalling rule, table-miss on every switch | `T4_dump_flows_s*.txt` | |
| T5 | heartbeat + start/end reach the controller; rogue sends a fake signal | events present; `CONTROL_REJECTED` for 10.0.1.66 | `controller_events_during_tests.jsonl` | |
| T6 | 8 students loop for 28 s; at 6 s `rogue` floods 20 Mbit/s | `CONGESTION_DETECTED`; `MITIGATION_INSTALLED` for 10.0.1.66 within 15 s; drop rule on s1; mean response time after mitigation < during attack | `T6_dump_flows_s1_during_quarantine.txt`, `report.json` | |
| T7 | wait for the quarantine to expire | `MITIGATION_RELEASED`; no priority-18 rule on s1; rogue pings server with 0 % loss | `T7_*` | |
| T8 | 3 clients loop; server killed at 4 s, restarted at 8 s | failures during the outage, successes afterwards, controller still reporting | `T8/*.csv`, `report.json` | |

`--expect static` (controller started with `scripts/start_controller.sh static`): T6 instead expects detection **without** mitigation and a clear latency increase; T7 is skipped.

## Level 4 – performance  `scripts/run_experiments.sh`

| Variable | Values |
|---|---|
| Controller | static (baseline), dynamic |
| Traffic | students only; students + 20 Mbit/s flood from `rogue` at t = 8 s |
| Concurrent students | 5, 10, 20 |
| Repetitions | 3 per cell, 30 s each |
| Metrics | mean / p50 / p95 answer response time, completed exams per second, success rate, time to mitigate |
| Comparison | dynamic vs static under the same flood; both vs no-flood to show no side effects |
