import argparse
import csv
import json
import random
import socket
import statistics
import threading
import time
from pathlib import Path

ANSWERS = (3, 2, 1)  


class JsonLineConnection:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""

    def send(self, payload):
        self.sock.sendall(json.dumps(payload).encode() + b"\n")

    def receive(self):
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.buffer += chunk
            if len(self.buffer) > 65536 and b"\n" not in self.buffer:
                raise ValueError("server message exceeded 64 KiB")
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))


class Recorder:
    def __init__(self):
        self.rows = []
        self.lock = threading.Lock()

    def add(self, client, kind, value_ms, ok, error=""):
        with self.lock:
            self.rows.append((time.time(), client, kind, value_ms, ok, error))


def take_exam(args, client, rec, barrier=None):
    """One full exam. Returns True on success. Never raises."""
    began = time.monotonic()
    try:
        t0 = time.monotonic()
        with socket.create_connection((args.host, args.port), timeout=args.connect_timeout) as sock:
            rec.add(client, "connect", (time.monotonic() - t0) * 1000, 1)
            sock.settimeout(args.socket_timeout)
            conn = JsonLineConnection(sock)
            welcome = conn.receive()
            if welcome.get("type") != "welcome":
                raise RuntimeError(f"unexpected welcome: {welcome!r}")
            conn.send({"type": "join", "name": f"load-{client}"})
            start = conn.receive()
            if start.get("type") != "exam_start":
                raise RuntimeError(f"exam did not start: {start!r}")
            if barrier is not None:
                barrier.wait(timeout=args.start_timeout)
            message = conn.receive()
            while message.get("type") == "question":
                n = message.get("number", 0)
                choice = ANSWERS[n - 1] if 1 <= n <= len(ANSWERS) else 1
                sent = time.monotonic()
                conn.send({"type": "answer", "choice": choice})
                message = conn.receive()
                rec.add(client, "answer", (time.monotonic() - sent) * 1000, 1)
                if message.get("type") == "error":
                    raise RuntimeError(message.get("message", "server rejected answer"))
            if message.get("type") != "result":
                raise RuntimeError(f"unexpected final response: {message!r}")
        rec.add(client, "exam", (time.monotonic() - began) * 1000, 1)
        return True
    except Exception as exc: 
        rec.add(client, "exam", (time.monotonic() - began) * 1000, 0, f"{type(exc).__name__}: {exc}")
        return False


def worker_once(args, client, rec, barrier):
    take_exam(args, client, rec, barrier)


def worker_loop(args, client, rec, stop_at):
    rng = random.Random(client)
    time.sleep(rng.random() * 0.3)         
    while time.time() < stop_at:
        ok = take_exam(args, client, rec)
        pause = args.think * (0.5 + rng.random()) if ok else 0.3
        time.sleep(pause)


def live_monitor(rec, stop_event):
    seen = 0
    second = 0
    while not stop_event.wait(1.0):
        second += 1
        with rec.lock:
            new = rec.rows[seen:]
            seen = len(rec.rows)
        rtts = [r[3] for r in new if r[2] == "answer"]
        done = sum(1 for r in new if r[2] == "exam" and r[4])
        failed = sum(1 for r in new if r[2] == "exam" and not r[4])
        mean = f"{statistics.mean(rtts):8.1f} ms" if rtts else "      n/a   "
        print(f"[load t={second:3d}s] answer RTT {mean}   exams ok={done:3d} failed={failed:3d}", flush=True)


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q / 100 * (len(ordered) - 1)))))
    return ordered[index]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="10.0.3.100", help="exam server IP")
    p.add_argument("--port", type=int, default=5050)
    p.add_argument("--clients", type=int, default=10)
    p.add_argument("--duration", type=float, default=None, help="loop mode: run for this many seconds")
    p.add_argument("--think", type=float, default=0.2, help="loop mode: mean pause between exams (s)")
    p.add_argument("--connect-timeout", type=float, default=5)
    p.add_argument("--socket-timeout", type=float, default=10)
    p.add_argument("--start-timeout", type=float, default=30)
    p.add_argument("--label", default="run")
    p.add_argument("--tag", default="local", help="name of this load-generating host (file name part)")
    p.add_argument("--out", default="results")
    p.add_argument("--live", action="store_true", help="print one summary line per second")
    args = p.parse_args()
    if args.clients < 1:
        p.error("--clients must be at least 1")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rec = Recorder()
    began_epoch = time.time()
    began = time.monotonic()
    stop_live = threading.Event()
    if args.live:
        threading.Thread(target=live_monitor, args=(rec, stop_live), daemon=True).start()

    threads = []
    if args.duration is None:
        barrier = threading.Barrier(args.clients)
        for i in range(args.clients):
            threads.append(threading.Thread(target=worker_once, args=(args, f"{args.tag}-{i + 1}", rec, barrier)))
    else:
        stop_at = began_epoch + args.duration
        for i in range(args.clients):
            threads.append(threading.Thread(target=worker_loop, args=(args, f"{args.tag}-{i + 1}", rec, stop_at)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop_live.set()
    wall = time.monotonic() - began

    stem = f"{args.label}-{args.tag}"
    with (out / f"{stem}-samples.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["t_epoch", "client", "kind", "value_ms", "ok", "error"])
        for row in sorted(rec.rows):
            writer.writerow([f"{row[0]:.4f}", row[1], row[2], f"{row[3]:.3f}", row[4], row[5]])

    exams = [r for r in rec.rows if r[2] == "exam"]
    ok = [r for r in exams if r[4]]
    rtts = [r[3] for r in rec.rows if r[2] == "answer"]
    errors = sorted({r[5] for r in exams if not r[4]})[:5]
    summary = {
        "label": args.label, "tag": args.tag, "host": args.host, "port": args.port, "clients": args.clients,
        "mode": "once" if args.duration is None else "loop", "start_epoch": began_epoch,
        "wall_seconds": wall, "exams_attempted": len(exams), "exams_ok": len(ok),
        "success_rate": (len(ok) / len(exams)) if exams else 0.0,
        "exams_per_second": len(ok) / wall if wall else 0.0,
        "answer_rtt_ms": {"mean": statistics.mean(rtts) if rtts else None, "p50": percentile(rtts, 50),
                          "p95": percentile(rtts, 95), "p99": percentile(rtts, 99),
                          "max": max(rtts) if rtts else None},
        "sample_errors": errors,
    }
    (out / f"{stem}-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
