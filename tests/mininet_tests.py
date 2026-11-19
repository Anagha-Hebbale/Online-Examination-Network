import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import mn_common as mc  # noqa: E402

APP_TESTS = str(ROOT / "tests" / "app_tests.py")

class Ctx:
    pass

class Fail(AssertionError):
    pass

def check(condition, message):
    if not condition:
        raise Fail(message)


def loss_percent(text):
    found = re.search(r"(\d+(?:\.\d+)?)% packet loss", text)
    return float(found.group(1)) if found else 100.0


def t1_connectivity(ctx):
    ctx.net.pingAll(timeout=3)                      
    loss = ctx.net.pingAll(timeout=3)
    ping = ctx.net.get("student1").cmd(f"ping -c 3 -W 2 {mc.SERVER_IP}")
    (ctx.out / "T1_ping_student1_server.txt").write_text(ping)
    details = ctx.details = {"pingall_loss_percent": loss, "student1_to_server_loss_percent": loss_percent(ping)}
    check(loss == 0, f"pingall lost {loss}% of packets")
    check(loss_percent(ping) == 0, "student1 -> examserver ping lost packets")
    return details


def t2_application(ctx):
    log = ctx.out / "T2_app_tests_from_student1.txt"
    with open(log, "w") as handle:
        proc = ctx.net.get("student1").popen(
            ["python3", APP_TESTS, "--host", mc.SERVER_IP, "--exam-duration", str(ctx.exam_duration),
             "--out", str(ctx.out / "T2")], stdout=handle, stderr=subprocess.STDOUT)
        rc = proc.wait(timeout=240)
    text = log.read_text()
    summary = [line for line in text.splitlines() if "passed" in line]
    check(rc == 0, "application tests failed - see " + log.name)
    return {"result": summary[-1] if summary else "", "log": log.name}


def t3_concurrency(ctx):
    mc.reset_controller(ctx.net)
    procs = mc.spawn_load(ctx.net, 20, ctx.out / "T3", "t3", duration=None)
    mc.wait_all(procs, timeout=120)
    rows = mc.load_samples(ctx.out / "T3", "t3")
    exams = [r for r in rows if r["kind"] == "exam"]
    ok = sum(r["ok"] for r in exams)
    stats = mc.window_stats(rows, rows[0]["t"], rows[-1]["t"] + 1) if rows else {}
    details = ctx.details = {"clients": 20, "hosts": 4, "exams_ok": ok, "exams_attempted": len(exams),
               "answer_rtt_mean_ms": stats.get("rtt_mean_ms"), "answer_rtt_p95_ms": stats.get("rtt_p95_ms")}
    check(len(exams) == 20 and ok == 20, f"only {ok}/{len(exams)} exams completed")
    return details


def t4_flow_rules(ctx):
    proc = ctx.net.get("student1").popen(["python3", mc.LOAD_TEST, "--host", mc.SERVER_IP, "--clients", "1",
                                          "--label", "t4", "--tag", "student1", "--out", str(ctx.out / "T4")],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait(timeout=30)
    dumps = {sw: mc.dump_flows(sw) for sw in ("s1", "s2", "s3")}
    for sw, text in dumps.items():
        (ctx.out / f"T4_dump_flows_{sw}.txt").write_text(text)
    s1, s3 = dumps["s1"], dumps["s3"]
    exam_rules = mc.flow_lines(s1, 20, r"tp_dst=5050", r"nw_src=10\.0\.1\.1,")
    reverse_rules = mc.flow_lines(s1, 20, r"tp_src=5050")
    details = ctx.details = {
        "s1_exam_rule": exam_rules[:1], "s1_exam_rule_packets": sum(mc.packets_of(l) for l in exam_rules),
        "s1_reverse_rules": len(reverse_rules),
        "s3_signalling_rule": mc.flow_lines(s3, 30, r"tp_dst=9999")[:1],
        "s3_table_miss": mc.flow_lines(s3, 0, "CONTROLLER")[:1],
    }
    check(exam_rules, "no priority-20 rule for TCP port 5050 on s1")
    check(details["s1_exam_rule_packets"] > 0, "exam rule on s1 has no packets counted")
    check(reverse_rules, "no priority-20 reverse (tp_src=5050) rule on s1")
    for sw in ("s1", "s2", "s3"):
        check(mc.flow_lines(dumps[sw], 30, r"tp_dst=9999"), f"signalling rule (priority 30) missing on {sw}")
        check(mc.flow_lines(dumps[sw], 0, "CONTROLLER"), f"table-miss rule missing on {sw}")
    return details


def t5_signalling(ctx):
    check(os.path.exists(ctx.events), f"controller events log not found: {ctx.events}")
    hb = mc.read_events(ctx.events, ctx.t_start, {"APP_HEARTBEAT"})
    stats = mc.read_events(ctx.events, ctx.t_start, {"STATS"})
    check(hb, "controller never received a server heartbeat (is SDN_HOOK_ADDR set? is the controller running?)")
    check(stats and stats[-1].get("app_starts", 0) > 0, "controller counted no exam_start events")
    # spoofing: a non-server host sends a fake signal; it must be rejected
    before = time.time()
    ctx.net.get("rogue").cmd(f"python3 {mc.TRAFFIC_GEN} ctl --addr {mc.GATEWAY_HOOK} "
                             f"--json '{{\"type\":\"heartbeat\",\"active\":99}}'")
    rejected = mc.wait_for_event(ctx.events, "CONTROL_REJECTED", before, 5, lambda r: r.get("src") == mc.ROGUE_IP)
    check(rejected, "spoofed signal from the rogue host was not rejected")
    return {"heartbeat_events": len(hb), "exam_starts_seen": stats[-1]["app_starts"],
            "exam_ends_seen": stats[-1]["app_ends"], "spoofed_signal": "rejected"}


def t6_congestion(ctx):
    dynamic = ctx.expect == "dynamic"
    mc.reset_controller(ctx.net)
    time.sleep(1)
    duration, flood_after, flood_len = 28, 6, 21
    outdir = ctx.out / "T6"
    procs = mc.spawn_load(ctx.net, 8, outdir, "t6", duration=duration)
    t_load = time.time()
    time.sleep(flood_after)
    flood_start = time.time()
    flood = mc.spawn_flood(ctx.net, 20, flood_len)
    ctx.t6_start = flood_start
    mit = mc.wait_for_event(ctx.events, "MITIGATION_INSTALLED", flood_start, 15) if dynamic else None
    dump = ""
    if mit:
        time.sleep(1.0)
        dump = mc.dump_flows("s1")
        (ctx.out / "T6_dump_flows_s1_during_quarantine.txt").write_text(dump)
    else:
        time.sleep(12)
    detected = mc.read_events(ctx.events, flood_start, {"CONGESTION_DETECTED"})
    mc.wait_all(procs + [flood], timeout=duration + 30)
    rows = mc.load_samples(outdir, "t6")
    mit_ts = mit["ts"] if mit else None
    flood_end = flood_start + flood_len
    pre = mc.window_stats(rows, t_load + 1, flood_start)
    attack = mc.window_stats(rows, flood_start, mit_ts or flood_end)
    details = ctx.details = {"pre_flood": pre, "attack_unmitigated": attack,
               "detected_events": len(detected), "mitigation_event": mit}
    stats_after = mc.read_events(ctx.events, flood_start, {"STATS"})
    details["peak_bottleneck_utilisation"] = max((s["util"] for s in stats_after), default=None)
    if dynamic:
        post = mc.window_stats(rows, mit_ts + 2, t_load + duration)
        details["post_mitigation"] = post
        details["seconds_to_mitigate"] = round(mit_ts - flood_start, 2)
        check(detected, "congestion was never detected")
        check(mit, "no MITIGATION_INSTALLED within 15 s of the flood starting (dynamic mode expected)")
        check(mit["offender"] == mc.ROGUE_IP, f"wrong offender {mit['offender']}")
        quarantine = mc.flow_lines(dump, 18, r"nw_src=10\.0\.1\.66")
        check(quarantine and all("drop" in l for l in quarantine), "no drop rule for the rogue host on s1 (priority 18)")
        if attack["rtt_mean_ms"] is not None and post["rtt_mean_ms"] is not None:
            check(post["rtt_mean_ms"] < attack["rtt_mean_ms"],
                  f"latency did not improve after mitigation ({attack['rtt_mean_ms']} -> {post['rtt_mean_ms']} ms)")
        check(post["rtt_mean_ms"] is not None, "no exam traffic completed after mitigation")
    else:
        check(not mc.read_events(ctx.events, flood_start, {"MITIGATION_INSTALLED"}),
              "controller acted although it was started in static mode")
        check(detected, "congestion was not even observed by the controller")
        if pre["rtt_mean_ms"] and attack["rtt_mean_ms"] is not None:
            check(attack["rtt_mean_ms"] > 2 * pre["rtt_mean_ms"],
                  f"flood did not degrade latency ({pre['rtt_mean_ms']} -> {attack['rtt_mean_ms']} ms)")
    return details


def t7_recovery(ctx):
    check(getattr(ctx, "t6_start", None), "T6 did not run, nothing to recover from")
    wait = mc.wait_for_event(ctx.events, "MITIGATION_RELEASED", ctx.t6_start, 45)
    check(wait, "quarantine was never released")
    time.sleep(1)
    dump = mc.dump_flows("s1")
    (ctx.out / "T7_dump_flows_s1_after_release.txt").write_text(dump)
    check(not mc.flow_lines(dump, 18, r"nw_src=10\.0\.1\.66"), "drop rule still present on s1 after release")
    ping = ctx.net.get("rogue").cmd(f"ping -c 3 -W 2 {mc.SERVER_IP}")
    (ctx.out / "T7_ping_rogue_server.txt").write_text(ping)
    check(loss_percent(ping) == 0, "rogue host still cannot reach the server after release")
    return {"released_after_s": round(wait["ts"] - ctx.t6_start, 1), "rogue_ping_loss_percent": loss_percent(ping)}


def t8_server_failure(ctx):
    from exam_topology import start_server
    mc.reset_controller(ctx.net)
    outdir = ctx.out / "T8"
    procs = mc.spawn_load(ctx.net, 3, outdir, "t8", duration=18, hosts=["student1", "student3"])
    t_load = time.time()
    time.sleep(4)
    killed = time.time()
    ctx.server.terminate()
    ctx.server.wait(timeout=5)
    time.sleep(4)
    restarted = time.time()
    ctx.server = start_server(ctx.net, ctx.exam_duration, str(ctx.out / "exam_results.jsonl"))
    mc.wait_all(procs, timeout=60)
    rows = mc.load_samples(outdir, "t8")
    before = mc.window_stats(rows, t_load, killed)
    during = mc.window_stats(rows, killed, restarted)
    after = mc.window_stats(rows, restarted + 2, t_load + 18)
    alive = mc.read_events(ctx.events, time.time() - 5, {"STATS"})
    details = ctx.details = {"before_failure": before, "during_outage": during, "after_restart": after,
               "controller_still_reporting": bool(alive)}
    check(before["exams_ok"] > 0, "no exam completed before the failure")
    check(during["exams_failed"] > 0, "clients did not notice the outage")
    check(after["exams_ok"] > 0, "clients did not recover after the server restarted")
    check(alive, "controller stopped reporting statistics")
    return details


TESTS = [
    ("T1", "Connectivity across three subnets", t1_connectivity),
    ("T2", "Application behaviour from a student host", t2_application),
    ("T3", "Concurrency: 20 students on 4 hosts", t3_concurrency),
    ("T4", "SDN flow rules and counters", t4_flow_rules),
    ("T5", "Socket-SDN signalling", t5_signalling),
    ("T6", "Congestion: detection and mitigation", t6_congestion),
    ("T7", "Recovery after quarantine", t7_recovery),
    ("T8", "Server failure and recovery", t8_server_failure),
]


def write_report(ctx, rows):
    (ctx.out / "report.json").write_text(json.dumps(rows, indent=2, default=str) + "\n")
    lines = [f"# Mininet test report ({time.strftime('%Y-%m-%d %H:%M:%S')}, controller mode: {ctx.expect})", "",
             "| ID | Test | Result | Notes |", "|---|---|---|---|"]
    for r in rows:
        note = r["error"] if r["status"] != "PASS" else ""
        lines.append(f"| {r['id']} | {r['title']} | **{r['status']}** | {note} |")
    passed = sum(r["status"] == "PASS" for r in rows)
    lines += ["", f"{passed}/{len(rows)} passed. Measurements for each test are in `report.json`."]
    (ctx.out / "report.md").write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--controller-port", type=int, default=6653)
    ap.add_argument("--expect", choices=["dynamic", "static"], default="dynamic",
                    help="mode the controller was started in (decides what T6 expects)")
    ap.add_argument("--events", default=str(ROOT / "results" / "controller_events.jsonl"),
                    help="the controller's EXAM_EVENT_LOG file")
    ap.add_argument("--bw", type=float, default=10, help="bottleneck Mbit/s (must match BOTTLENECK_MBPS)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", help="comma-separated test ids")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("Mininet needs root: run with sudo.")
    from mininet.log import setLogLevel
    from exam_topology import build_network, start_server, start_sink, wait_for_switches

    setLogLevel("warning")
    ctx = Ctx()
    ctx.expect, ctx.events = args.expect, args.events
    ctx.exam_duration = 8
    ctx.out = Path(args.out or ROOT / "results" / time.strftime("mininet_tests-%Y%m%d-%H%M%S"))
    ctx.out.mkdir(parents=True, exist_ok=True)
    selected = set(args.only.split(",")) if args.only else None

    subprocess.run(["mn", "-c"], capture_output=True)
    ctx.net = build_network(args.controller_ip, args.controller_port, args.bw)
    ctx.net.start()
    rows = []
    try:
        if not wait_for_switches(ctx.net):
            sys.exit("Switches did not connect to the controller. Is it running on port 6653?")
        ctx.server = start_server(ctx.net, ctx.exam_duration, str(ctx.out / "exam_results.jsonl"))
        sink = start_sink(ctx.net)
        time.sleep(2.5)
        ctx.t_start = time.time()
        for tid, title, fn in TESTS:
            if selected and tid not in selected:
                continue
            if tid == "T7" and ctx.expect == "static":
                rows.append({"id": tid, "title": title, "status": "SKIP", "error": "static controller never quarantines"})
                continue
            began = time.time()
            print(f"\n=== {tid}: {title}", flush=True)
            ctx.details = None
            try:
                details = fn(ctx)
                rows.append({"id": tid, "title": title, "status": "PASS", "details": details, "error": ""})
            except Fail as exc:
                rows.append({"id": tid, "title": title, "status": "FAIL", "error": str(exc),
                             "details": ctx.details})
            except Exception as exc:  # noqa: BLE001
                rows.append({"id": tid, "title": title, "status": "ERROR",
                             "error": f"{type(exc).__name__}: {exc}", "details": ctx.details,
                             "trace": traceback.format_exc()})
            print(f"    -> {rows[-1]['status']} ({time.time() - began:.0f}s) {rows[-1].get('error', '')}", flush=True)
        sink.terminate()
        ctx.server.terminate()
    finally:
        ctx.net.stop()
        subprocess.run(["mn", "-c"], capture_output=True)
    if os.path.exists(ctx.events):
        with open(ctx.events) as src, open(ctx.out / "controller_events_during_tests.jsonl", "w") as dst:
            for line in src:
                try:
                    if json.loads(line).get("ts", 0) >= ctx.t_start and json.loads(line).get("event") != "STATS":
                        dst.write(line)
                except ValueError:
                    pass
    write_report(ctx, rows)
    mc.fix_ownership(ctx.out)
    print("\n" + (ctx.out / "report.md").read_text())
    print(f"Evidence saved in {ctx.out}")
    sys.exit(0 if all(r["status"] in ("PASS", "SKIP") for r in rows) else 1)


if __name__ == "__main__":
    main()
