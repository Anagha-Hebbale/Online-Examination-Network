"""Run with the controller already started in dynamic mode:
    scripts/start_controller.sh dynamic        # terminal 1
    sudo python3 experiments/live_demo.py      # terminal 2   (add --step to press Enter between steps)
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import mn_common as mc  # noqa: E402

SHOW = {"CONGESTION_DETECTED", "MITIGATION_INSTALLED", "MITIGATION_RELEASED"}


def say(text):
    print(f"\n\033[1;36m>>> {text}\033[0m", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", default=str(ROOT / "results" / "controller_events.jsonl"))
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--step", action="store_true", help="wait for Enter between steps")
    ap.add_argument("--bw", type=float, default=10)
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("Mininet needs root: run with sudo.")

    from mininet.log import setLogLevel
    from exam_topology import build_network, start_server, start_sink, wait_for_switches
    setLogLevel("warning")

    def pause():
        if args.step:
            input("    [Enter to continue] ")

    subprocess.run(["mn", "-c"], capture_output=True)
    net = build_network(args.controller_ip, 6653, args.bw)
    net.start()
    out = ROOT / "results" / time.strftime("live_demo-%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    try:
        if not wait_for_switches(net):
            sys.exit("Switches did not connect. Is the controller running?")
        server = start_server(net, 60)
        sink = start_sink(net)
        time.sleep(3)

        say("1. Topology: 3 subnets, 3 OpenFlow switches, one router, a bottleneck link r1 - s3 (10 Mbit/s)")
        net.pingAll(timeout=3)
        loss = net.pingAll(timeout=3)
        print(f"    pingall packet loss: {loss}%")
        pause()

        say("2. A student takes an exam over TCP (JSON lines, port 5050)")
        print(net.get("student1").cmd(f"python3 {mc.LOAD_TEST} --host {mc.SERVER_IP} --clients 1 "
                                      f"--label demo --tag student1 --out {out} | grep -E 'exams_ok|mean'"))
        pause()

        say("3. SDN flow rules on s1: exam traffic (priority 20) has its own rule and counters")
        for line in mc.flow_lines(mc.dump_flows("s1"), 20)[:4]:
            print("    " + line)
        pause()

        say("4. Normal load: 8 students on 4 hosts (live response time of student1's clients below)")
        mc.reset_controller(net)
        duration = 45
        t0 = time.time()
        procs = []
        for host, count in mc.split_clients(8, mc.STUDENTS):
            cmd = ["python3", mc.LOAD_TEST, "--host", mc.SERVER_IP, "--clients", str(count), "--duration", str(duration),
                   "--label", "demo-load", "--tag", host, "--out", str(out)]
            if host == "student1":
                cmd.append("--live")
                procs.append((net.get(host).popen(cmd, stdout=None, stderr=None), open(os.devnull, "w")))
            else:
                procs.append((net.get(host).popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                              open(os.devnull, "w")))
        time.sleep(8)
        pause()

        say("5. The noisy host `rogue` starts flooding 20 Mbit/s of UDP towards the server")
        flood = mc.spawn_flood(net, 20, 25)
        flood_start = time.time()
        shown_rule = False
        last = flood_start - 1
        while time.time() - t0 < duration + 1:
            for rec in mc.read_events(args.events, last, SHOW):
                if rec["ts"] <= last:
                    continue
                last = rec["ts"]
                print(f"\033[1;33m    [controller +{rec['ts'] - flood_start:5.1f}s] {rec['event']} "
                      f"{ {k: v for k, v in rec.items() if k in ('offender', 'util', 'offender_mbps', 'reason')} }\033[0m",
                      flush=True)
                if rec["event"] == "MITIGATION_INSTALLED" and not shown_rule:
                    time.sleep(0.5)
                    shown_rule = True
                    for line in mc.flow_lines(mc.dump_flows("s1"), 18):
                        print("    drop rule on s1: " + line)
            time.sleep(0.4)
        mc.wait_all(procs + [flood], timeout=30)

        say("6. After the flood the quarantine expires and the rogue host is allowed again")
        rel = mc.wait_for_event(args.events, "MITIGATION_RELEASED", flood_start, 30)
        print("    " + (f"released {rel['ts'] - flood_start:.1f}s after the flood began" if rel else "no release seen yet"))
        print(net.get("rogue").cmd(f"ping -c 2 -W 2 {mc.SERVER_IP} | tail -2"))
        server.terminate()
        sink.terminate()
    finally:
        net.stop()
        subprocess.run(["mn", "-c"], capture_output=True)
        mc.fix_ownership(out)
    print(f"Demo data saved in {out}")


if __name__ == "__main__":
    main()
