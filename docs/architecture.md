# Architecture

## Goal

Keep exam traffic responsive when the network is congested. The exam application (TCP sockets) and the SDN controller cooperate:
the application tells the network when exams are running, and the controller watches the network and removes the cause of congestion.

## Components

```mermaid
flowchart LR
  subgraph Hosts
    S1[student1 10.0.1.1]
    S2[student2 10.0.1.2]
    R[rogue 10.0.1.66<br/>noisy host]
    S3[student3 10.0.2.1]
    S4[student4 10.0.2.2]
    SRV[examserver 10.0.3.100<br/>server.py :5050 TCP]
  end
  S1 & S2 & R --- SW1[s1 OVS]
  S3 & S4 --- SW2[s2 OVS]
  SRV --- SW3[s3 OVS]
  SW1 --- RT{{r1 Linux router}}
  SW2 --- RT
  RT == "10 Mbit/s bottleneck" === SW3
  CTL[[OS-Ken controller<br/>exam_controller.py]]
  CTL -. OpenFlow 1.3 .- SW1 & SW2 & SW3
  SRV -. "UDP :9999 signalling<br/>(consumed as packet-in)" .-> SW3
```

| Component | File | Role |
|---|---|---|
| Exam server | `src/server.py` | Accepts TCP connections, one thread each. Runs the exam, scores it, sends signalling datagrams. |
| Exam client | `src/client.py` | Interactive student client. `src/load_test.py` is the scripted multi-client version. |
| Noisy host | `src/traffic_gen.py` | `rogue` sends paced UDP at a chosen rate (the congestion source). |
| Topology | `src/exam_topology.py` | Hosts, three OVS switches, Linux router, TC-shaped bottleneck with fixed port numbers. |
| Controller | `src/exam_controller.py` | Learning switch, flow classification, monitoring, signalling endpoint, congestion control. |

## Communication flow

1. A student connects to `10.0.3.100:5050`; the packet crosses s1 → r1 → s3. The first packet of each flow is a packet-in; the controller
   learns MACs and installs rules, then later packets are switched in hardware without the controller.
2. The server sends the exam over TCP (`docs/protocol.md`). On every exam start/end, and once per second, it also sends a small UDP datagram
   (`exam_start`, `exam_end`, `heartbeat{active, completed}`) to `10.0.3.254:9999`.
3. Every switch has a priority-30 rule *UDP dst port 9999 → controller*. The datagram therefore reaches the controller as a packet-in and is
   consumed there (never forwarded). The controller accepts it only if the IP source is the exam server, so another host cannot fake it (tested in T5).
4. Every second the controller requests flow and port statistics from all switches.
5. A noisy host starts sending UDP through the bottleneck. Queues fill and exam response time rises.
6. The controller sees bottleneck utilisation ≥ 80 % twice in a row, checks the application gate, finds the top non-exam talker and installs a drop rule on all switches.
7. Utilisation falls and exam response time recovers. After the quarantine time the rule is removed.

## Flow table design

| Priority | Match | Action | Installed |
|---|---|---|---|
| 30 | `udp, udp_dst=9999` | to controller | on switch connect |
| 20 | `in_port, eth_dst, ipv4_src, ipv4_dst, tcp, tcp_dst=5050` (and `tcp_src=5050` for replies) | output | on first exam packet |
| 18 | `ipv4_src = <offender>` | **drop** | by the controller during congestion (quarantine) |
| 15 | `in_port, eth_dst, ipv4_src, ipv4_dst, ip_proto` | output | on first packet of any other IP flow |
| 0 | any | to controller | on switch connect |

Rules carry the IP source, so the switch counters give bytes **per host**. ARP and other non-IP frames get no flow rule (packet-out only).
The quarantine priority sits between ordinary traffic (15) and exam traffic (20): the offender's exam sessions keep working.

## Control algorithm (`ExamSDNController.evaluate`)

```
every STATS_INTERVAL:
    util   = max(rx, tx) of bottleneck port / BOTTLENECK_MBPS
    rates  = bytes/s per source IP of non-exam flows entering the bottleneck switch from the uplink
    hot    = hot + 1 if util >= CONGESTION_THRESHOLD else 0
    if hot >= CONGESTION_POLLS:
        offender = argmax(rates) if rate >= MIN_OFFENDER_MBPS and not already quarantined
        log CONGESTION_DETECTED
        if EXAM_MODE == dynamic and (no live heartbeat or exams_active > 0):
            install drop rule (priority 18) on every switch; hot = 0; log MITIGATION_INSTALLED
    release any quarantine older than QUARANTINE_SECONDS; log MITIGATION_RELEASED
```

Why these choices: two polls avoid reacting to a one-second burst; the `MIN_OFFENDER` floor avoids blaming a host for a few kbit/s;
the timer release avoids permanent blocking without needing a "calm" detector, and a host that keeps flooding is simply caught again.
The application gate is the Socket–SDN link: no exam running means no need to interfere.

## Baseline

`EXAM_MODE=static` is the same controller (same rules, same statistics, same log) with the quarantine step disabled, so differences in the
experiments are caused only by the dynamic response.

## Event log

The controller appends JSON lines to `EXAM_EVENT_LOG`: `SWITCH_CONNECTED`, `APP_HEARTBEAT`, `EXAM_FLOW_INSTALLED`, `STATS` (every second),
`CONGESTION_DETECTED`, `MITIGATION_INSTALLED`, `MITIGATION_RELEASED`, `CONTROL_REJECTED`, `STATE_RESET`. Tests and experiments read this file, so
the evidence for "the controller reacted" is the controller's own record.
