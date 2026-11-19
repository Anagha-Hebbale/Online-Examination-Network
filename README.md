# Online Examination over an SDN Network

A multi-student online exam that runs over TCP sockets inside a Mininet network controlled by an OpenFlow 1.3 (OS-Ken) controller.
When a noisy host congests the link to the exam server, the controller detects it and temporarily blocks that host so exams stay responsive.

## What is in it

- `src/server.py`, `src/client.py` – threaded TCP exam server and client
- `src/exam_topology.py` – Mininet topology (4 students, 1 noisy host, exam server, router, 3 switches)
- `src/exam_controller.py` – OS-Ken controller (learning switch, congestion detection, quarantine)
- `tests/`, `experiments/`, `scripts/` – tests, performance experiments and run scripts
- `docs/` – architecture, protocol, test plan
- `results/` – saved output of our runs

## Requirements

Linux, Python 3, Mininet and Open vSwitch (`sudo apt install mininet openvswitch-switch`), then `pip install -r requirements.txt`.

## How to run

Exam without Mininet:

```bash
python3 src/server.py          # terminal 1
python3 src/client.py          # terminal 2 (answers: 3, 2, 1)
```

Full system in Mininet:

```bash
scripts/start_controller.sh dynamic     # terminal 1
sudo python3 src/exam_topology.py       # terminal 2, opens the mininet> prompt
mininet> student1 env EXAM_HOST=10.0.3.100 python3 src/client.py
```

Tests and experiments:

```bash
scripts/run_all_tests.sh        # unit, application and Mininet tests
scripts/run_experiments.sh      # static vs dynamic controller comparison (~25 min)
scripts/run_demo.sh --step      # narrated demo
```

Cleanup after a crashed run: `sudo mn -c`
