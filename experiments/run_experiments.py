import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import mn_common as mc  # noqa: E402


def run_trial(net, args, cond, clients, rep, run_dir):
    name = f"{args.mode}-{cond}-n{clients}-r{rep}"
    trial = run_dir / name
    trial.mkdir(parents=True, exist_ok=True)
    mc.reset_controller(net)
    time.sleep(1.5)
    t_load = time.time()
    procs = mc.spawn_load(net, clients, trial, "load", duration=args.duration, think=args.think)
    flood_start = None
    flood = None
    if cond == "flood":
        time.sleep(args.flood_at)
        flood_start = time.time()
        flood = mc.spawn_flood(net, args.flood_mbps, args.duration - args.flood_at - 1)
    mc.wait_all(procs + ([flood] if flood else []), timeout=args.duration + 60)
    t_end = time.time()
    meta = {"name": name, "mode": args.mode, "condition": cond, "clients": clients, "rep": rep,
            "t_load": t_load, "t_end": t_end, "duration": args.duration, "flood_at": args.flood_at,
            "flood_start": flood_start, "flood_mbps": args.flood_mbps if cond == "flood" else 0,
            "bottleneck_mbps": args.bw, "think": args.think}
    (trial / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    with open(trial / "controller_events.jsonl", "w") as out:
        for rec in mc.read_events(args.events, t_load - 1):
            if rec["ts"] <= t_end + 1:
                out.write(json.dumps(rec) + "\n")
    rows = mc.load_samples(trial, "load")
    start = flood_start if flood_start else t_load + args.flood_at
    stats = mc.window_stats(rows, start, t_end)
    print(f"  {name:28} rtt_mean={stats['rtt_mean_ms']} ms  p95={stats['rtt_p95_ms']} ms  "
          f"exams/s={stats['exams_per_s']}  ok={stats['exams_ok']} failed={stats['exams_failed']}", flush=True)
    mc.reset_controller(net)
    time.sleep(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["static", "dynamic"], required=True,
                    help="the mode the running controller was started in (used as a label)")
    ap.add_argument("--clients", default="5,10,20", help="comma-separated client counts")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--duration", type=float, default=30, help="seconds per trial")
    ap.add_argument("--flood-at", type=float, default=8, help="seconds after the start when the flood begins")
    ap.add_argument("--flood-mbps", type=float, default=20)
    ap.add_argument("--think", type=float, default=0.2, help="mean pause between a student's exams (s)")
    ap.add_argument("--conditions", default="baseline,flood")
    ap.add_argument("--bw", type=float, default=10, help="bottleneck Mbit/s (must match BOTTLENECK_MBPS)")
    ap.add_argument("--events", default=str(ROOT / "results" / "controller_events.jsonl"))
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--controller-port", type=int, default=6653)
    ap.add_argument("--out", default=None)
    ap.add_argument("--quick", action="store_true", help="smoke test: 1 client count, 1 repetition, 20 s")
    args = ap.parse_args()
    if args.quick:
        args.clients, args.reps, args.duration, args.flood_at = "10", 1, 20, 6
    if os.geteuid() != 0:
        sys.exit("Mininet needs root: run with sudo.")
    if args.duration - args.flood_at < 12:
        sys.exit("--duration must exceed --flood-at by at least 12 s so that the controller has time to react")

    from mininet.log import setLogLevel
    from exam_topology import build_network, start_server, start_sink, wait_for_switches
    setLogLevel("warning")

    run_dir = Path(args.out or ROOT / "results" / time.strftime(f"experiments-{args.mode}-%Y%m%d-%H%M%S"))
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(json.dumps(vars(args), indent=2) + "\n")
    print(f"Results folder: {run_dir}")

    subprocess.run(["mn", "-c"], capture_output=True)
    net = build_network(args.controller_ip, args.controller_port, args.bw)
    net.start()
    try:
        if not wait_for_switches(net):
            sys.exit("Switches did not connect to the controller. Is it running on port 6653?")
        server = start_server(net, 60)
        sink = start_sink(net)
        time.sleep(3)
        if not mc.read_events(args.events, time.time() - 5, {"APP_HEARTBEAT", "STATS"}):
            print("WARNING: no recent controller events - is the controller running with the same --events path?")
        counts = [int(x) for x in args.clients.split(",")]
        conds = args.conditions.split(",")
        total = len(counts) * len(conds) * args.reps
        print(f"{total} trials of {args.duration:g} s each (about {total * (args.duration + 5) / 60:.0f} min), mode={args.mode}")
        for n in counts:
            for rep in range(1, args.reps + 1):
                for cond in conds:
                    run_trial(net, args, cond, n, rep, run_dir)
        server.terminate()
        sink.terminate()
    finally:
        net.stop()
        subprocess.run(["mn", "-c"], capture_output=True)
    shutil.copy(args.events, run_dir / "controller_events_full.jsonl") if os.path.exists(args.events) else None
    mc.fix_ownership(run_dir)
    print(f"\nDone. Next: python3 experiments/analyze_results.py {run_dir} [other run folders]")


if __name__ == "__main__":
    main()
