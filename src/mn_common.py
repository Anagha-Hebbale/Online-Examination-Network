import csv
import glob
import json
import os
import re
import statistics
import subprocess
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent
LOAD_TEST = str(SRC / "load_test.py")
TRAFFIC_GEN = str(SRC / "traffic_gen.py")
SERVER_IP = "10.0.3.100"
ROGUE_IP = "10.0.1.66"
GATEWAY_HOOK = "10.0.3.254:9999"
STUDENTS = ["student1", "student2", "student3", "student4"]


def read_events(path, since=0.0, names=None):
    """Return controller events (dicts) with ts >= since, optionally filtered by event name."""
    out = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("ts", 0) >= since and (names is None or rec.get("event") in names):
                    out.append(rec)
    except FileNotFoundError:
        pass
    return out


def wait_for_event(path, name, since, timeout, predicate=None):
    """Poll the events file until an event appears. Returns the event or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for rec in read_events(path, since, {name}):
            if predicate is None or predicate(rec):
                return rec
        time.sleep(0.25)
    return None


def dump_flows(switch):
    """Text of `ovs-ofctl dump-flows` for a switch (runs in the root namespace, where the switches live)."""
    result = subprocess.run(["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", switch],
                            capture_output=True, text=True, timeout=10)
    return result.stdout + result.stderr


def flow_lines(dump, priority, *needles):
    """Flow lines in a dump that have the given priority and contain all needle regexes."""
    hits = []
    for line in dump.splitlines():
        if re.search(rf"priority={priority}[, ]", line) and all(re.search(n, line) for n in needles):
            hits.append(line.strip())
    return hits


def packets_of(line):
    match = re.search(r"n_packets=(\d+)", line)
    return int(match.group(1)) if match else 0

def send_control(net, payload):
    """Send a signalling datagram from the exam server (the only trusted source)."""
    net.get("examserver").cmd(f"python3 {TRAFFIC_GEN} ctl --addr {GATEWAY_HOOK} --json '{json.dumps(payload)}'")


def reset_controller(net):
    send_control(net, {"type": "reset"})
    time.sleep(0.5)


def split_clients(total, hosts):
    base, extra = divmod(total, len(hosts))
    return [(h, base + (1 if i < extra else 0)) for i, h in enumerate(hosts) if base + (1 if i < extra else 0) > 0]


def spawn_load(net, total_clients, outdir, label, duration=None, think=0.2, hosts=None, live=False):
    """Start load_test.py on several student hosts. Returns [(Popen, logfile)]."""
    os.makedirs(outdir, exist_ok=True)
    procs = []
    for host_name, count in split_clients(total_clients, hosts or STUDENTS):
        cmd = ["python3", LOAD_TEST, "--host", SERVER_IP, "--clients", str(count), "--label", label,
               "--tag", host_name, "--out", str(outdir), "--think", str(think)]
        if duration is not None:
            cmd += ["--duration", str(duration)]
        if live and host_name == (hosts or STUDENTS)[0]:
            cmd.append("--live")
        log = open(os.path.join(outdir, f"{label}-{host_name}.log"), "w")
        procs.append((net.get(host_name).popen(cmd, stdout=log, stderr=subprocess.STDOUT), log))
    return procs


def spawn_flood(net, mbps, duration, host="rogue", port=9000):
    log = open(f"/tmp/flood-{host}.log", "w")
    cmd = ["python3", TRAFFIC_GEN, "flood", "--host", SERVER_IP, "--port", str(port),
           "--mbps", str(mbps), "--duration", str(duration)]
    return net.get(host).popen(cmd, stdout=log, stderr=subprocess.STDOUT), log


def wait_all(procs, timeout=None):
    for proc, log in procs:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

def load_samples(outdir, label):
    """All sample rows of a label: dicts with t (epoch), kind, value (ms), ok, client."""
    rows = []
    for path in glob.glob(os.path.join(outdir, f"{label}-*-samples.csv")):
        with open(path, newline="", encoding="utf-8") as handle:
            for rec in csv.DictReader(handle):
                rows.append({"t": float(rec["t_epoch"]), "client": rec["client"], "kind": rec["kind"],
                             "value": float(rec["value_ms"]), "ok": int(rec["ok"]), "error": rec["error"]})
    rows.sort(key=lambda r: r["t"])
    return rows


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(q / 100 * (len(ordered) - 1)))))]


def window_stats(rows, t_from, t_to):
    """Metrics for samples whose timestamp lies in [t_from, t_to)."""
    sel = [r for r in rows if t_from <= r["t"] < t_to]
    rtts = [r["value"] for r in sel if r["kind"] == "answer"]
    exams = [r for r in sel if r["kind"] == "exam"]
    ok = sum(r["ok"] for r in exams)
    span = max(t_to - t_from, 1e-9)
    return {
        "seconds": round(span, 2),
        "answers": len(rtts),
        "rtt_mean_ms": round(statistics.mean(rtts), 2) if rtts else None,
        "rtt_p50_ms": round(percentile(rtts, 50), 2) if rtts else None,
        "rtt_p95_ms": round(percentile(rtts, 95), 2) if rtts else None,
        "exams_ok": ok,
        "exams_failed": len(exams) - ok,
        "success_rate": round(ok / len(exams), 4) if exams else None,
        "exams_per_s": round(ok / span, 2),
    }


def fix_ownership(path):
    """Mininet runs as root (sudo); give the output back to the invoking user so git can add it."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return
    for root, dirs, files in os.walk(path):
        for name in [""] + dirs + files:
            try:
                os.chown(os.path.join(root, name), int(uid), int(gid))
            except OSError:
                pass
