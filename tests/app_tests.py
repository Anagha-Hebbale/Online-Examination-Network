import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "src" / "server.py"
CORRECT = (3, 2, 1)


class Session:
    """A raw protocol client used to drive the server."""

    def __init__(self, host, port, timeout=15):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""

    def send(self, obj):
        self.sock.sendall(json.dumps(obj).encode() + b"\n")

    def send_raw(self, raw):
        self.sock.sendall(raw)

    def recv(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                return None            
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def close(self):
        self.sock.close()


def start(host, port, name="tester"):
    s = Session(host, port)
    assert s.recv()["type"] == "welcome", "no welcome message"
    s.send({"type": "join", "name": name})
    start_msg = s.recv()
    assert start_msg["type"] == "exam_start" and start_msg["total"] == 3, f"bad exam_start {start_msg}"
    return s, start_msg


def take_exam(host, port, choices, name="tester"):
    s, _ = start(host, port, name)
    try:
        for choice in choices:
            q = s.recv()
            assert q["type"] == "question", f"expected question, got {q}"
            s.send({"type": "answer", "choice": choice})
        result = s.recv()
        assert result["type"] == "result", f"expected result, got {result}"
        return result
    finally:
        s.close()

def t_all_correct(c):
    r = take_exam(c.host, c.port, CORRECT)
    assert (r["score"], r["total"], r["answered"], r["time_expired"]) == (3, 3, 3, False), r


def t_all_wrong(c):
    r = take_exam(c.host, c.port, (1, 1, 2))
    assert (r["score"], r["answered"]) == (0, 3), r


def t_mixed(c):
    r = take_exam(c.host, c.port, (3, 1, 1))
    assert (r["score"], r["answered"]) == (2, 3), r


def t_invalid_input_keeps_question(c):
    s, _ = start(c.host, c.port)
    try:
        q = s.recv()
        assert q["number"] == 1
        for bad in (0, 9, -1, "2", None, True, 2.5):
            s.send({"type": "answer", "choice": bad})
            e = s.recv()
            assert e["type"] == "error", f"{bad!r} should be rejected, got {e}"
        s.send({"type": "answer", "choice": 3})
        q2 = s.recv()
        assert q2["type"] == "question" and q2["number"] == 2, f"still on question 1? {q2}"
        s.send({"type": "answer", "choice": 2})
        assert s.recv()["number"] == 3
        s.send({"type": "answer", "choice": 1})
        r = s.recv()
        assert r["score"] == 3 and r["answered"] == 3, r
    finally:
        s.close()


def t_wrong_message_type_rejected(c):
    s, _ = start(c.host, c.port)
    try:
        s.recv()
        s.send({"type": "hello"})
        assert s.recv()["type"] == "error"
    finally:
        s.close()


def t_malformed_json_does_not_kill_server(c):
    s = Session(c.host, c.port)
    s.recv()
    s.send_raw(b"this is not json\n")
    assert s.recv() is None, "server should close a connection that sends malformed JSON"
    s.close()
    assert take_exam(c.host, c.port, CORRECT)["score"] == 3, "server not healthy after malformed input"


def t_non_object_json_handled(c):
    s = Session(c.host, c.port)
    s.recv()
    s.send_raw(b"[1, 2, 3]\n")
    assert s.recv() is None
    s.close()
    s, _ = start(c.host, c.port)
    try:
        s.recv()
        s.send_raw(b"[1, 2]\n")              
        assert s.recv()["type"] == "error"
    finally:
        s.close()
    assert take_exam(c.host, c.port, CORRECT)["score"] == 3


def t_bad_first_message_closes(c):
    s = Session(c.host, c.port)
    s.recv()
    s.send({"type": "answer", "choice": 1})
    assert s.recv() is None, "connection should be closed when the first message is not 'join'"
    s.close()


def t_long_name_truncated(c):
    r = take_exam(c.host, c.port, CORRECT, name="A" * 200)
    assert len(r["name"]) == 40, len(r["name"])


def t_timeout_expires(c):
    s, start_msg = start(c.host, c.port)
    try:
        s.sock.settimeout(c.exam_duration + 10)
        q = s.recv()
        s.send({"type": "answer", "choice": 3})
        s.recv()                                 
        t0 = time.monotonic()
        r = s.recv()
        waited = time.monotonic() - t0
        assert r["type"] == "result" and r["time_expired"] is True, r
        assert r["answered"] == 1 and r["score"] == 1, r
        assert waited <= c.exam_duration + 2, f"result came after {waited:.1f}s"
    finally:
        s.close()


def t_disconnect_mid_exam(c):
    s, _ = start(c.host, c.port)
    s.recv()
    s.close()                                    
    time.sleep(0.3)
    assert take_exam(c.host, c.port, CORRECT)["score"] == 3, "server not healthy after a client vanished"


def t_concurrent_clients(c):
    n = 30
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(lambda i: take_exam(c.host, c.port, CORRECT, f"c{i}"), range(n)))
    assert all(r["score"] == 3 for r in results) and len(results) == n


def t_sdn_signalling(c):
    """Local mode only: the server must emit exam_start / exam_end / heartbeat datagrams."""
    c.hook_messages.clear()
    take_exam(c.host, c.port, CORRECT, name="hooktest")
    time.sleep(1.5)                                # allow a heartbeat
    kinds = {m["type"] for m in c.hook_messages}
    assert {"exam_start", "exam_end", "heartbeat"} <= kinds, f"got {kinds}"
    ends = [m for m in c.hook_messages if m["type"] == "exam_end"]
    assert ends and ends[0]["student"] == "hooktest"


def t_results_file(c):
    """Local mode only: every finished exam is appended to the results file."""
    before = len(Path(c.results_file).read_text().splitlines()) if os.path.exists(c.results_file) else 0
    take_exam(c.host, c.port, CORRECT, name="persist")
    time.sleep(0.3)
    lines = Path(c.results_file).read_text().splitlines()
    assert len(lines) == before + 1 and json.loads(lines[-1])["name"] == "persist"


TESTS = [
    ("A1", "All answers correct -> 3/3", t_all_correct, False),
    ("A2", "All answers wrong -> 0/3", t_all_wrong, False),
    ("A3", "Mixed answers -> 2/3", t_mixed, False),
    ("A4", "Invalid choices (0, 9, -1, '2', null, true, 2.5) get an error; question and timer continue", t_invalid_input_keeps_question, False),
    ("A5", "Wrong message type during the exam gets an error", t_wrong_message_type_rejected, False),
    ("A6", "Malformed JSON closes that connection only", t_malformed_json_does_not_kill_server, False),
    ("A7", "Non-object JSON handled without crashing", t_non_object_json_handled, False),
    ("A8", "First message not 'join' -> connection closed", t_bad_first_message_closes, False),
    ("A9", "Student name longer than 40 chars is truncated", t_long_name_truncated, False),
    ("A10", "Exam timer expires -> result with time_expired=true", t_timeout_expires, False),
    ("A11", "Client disconnects mid-exam; server keeps serving", t_disconnect_mid_exam, False),
    ("A12", "30 simultaneous students all score 3/3", t_concurrent_clients, False),
    ("A13", "Server sends exam_start/exam_end/heartbeat signalling datagrams", t_sdn_signalling, True),
    ("A14", "Finished exams are appended to the results file", t_results_file, True),
]


class Ctx:
    host = "127.0.0.1"
    port = 5050
    exam_duration = 60.0
    hook_messages = []
    results_file = ""


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", help="test an already running server at this address (remote mode)")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--exam-duration", type=float, default=3.0, help="server's EXAM_DURATION in seconds")
    ap.add_argument("--out", default=str(ROOT / "results"))
    ap.add_argument("--only", help="comma separated test ids, e.g. A1,A4")
    args = ap.parse_args()

    ctx = Ctx()
    ctx.exam_duration = args.exam_duration
    proc = None
    listener_stop = threading.Event()
    local = args.host is None
    tmp = tempfile.mkdtemp(prefix="apptests-")
    if local:
        ctx.port = free_port(socket.SOCK_STREAM)
        hook_port = free_port(socket.SOCK_DGRAM)
        ctx.results_file = os.path.join(tmp, "results.jsonl")

        def listen():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", hook_port))
            sock.settimeout(0.5)
            while not listener_stop.is_set():
                try:
                    data, _ = sock.recvfrom(4096)
                    ctx.hook_messages.append(json.loads(data))
                except socket.timeout:
                    pass
        threading.Thread(target=listen, daemon=True).start()
        env = dict(os.environ, EXAM_PORT=str(ctx.port), EXAM_DURATION=str(args.exam_duration),
                   EXAM_RESULTS_FILE=ctx.results_file, SDN_HOOK_ADDR=f"127.0.0.1:{hook_port}")
        proc = subprocess.Popen([sys.executable, str(SERVER)], env=env,
                                stdout=open(os.path.join(tmp, "server.log"), "w"), stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", ctx.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
    else:
        ctx.host, ctx.port = args.host, args.port

    selected = set(args.only.split(",")) if args.only else None
    rows = []
    for tid, title, fn, local_only in TESTS:
        if selected and tid not in selected:
            continue
        if local_only and not local:
            rows.append({"id": tid, "title": title, "status": "SKIP", "detail": "needs local server"})
            continue
        began = time.monotonic()
        try:
            fn(ctx)
            status, detail = "PASS", ""
        except AssertionError as exc:
            status, detail = "FAIL", str(exc)
        except Exception as exc:  # noqa: BLE001
            status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
        rows.append({"id": tid, "title": title, "status": status, "detail": detail,
                     "seconds": round(time.monotonic() - began, 2)})
        print(f"{status:4}  {tid:4} {title}" + (f"   <- {detail}" if detail else ""), flush=True)

    listener_stop.set()
    if proc:
        proc.terminate()
        proc.wait(timeout=5)

    passed = sum(r["status"] == "PASS" for r in rows)
    failed = sum(r["status"] == "FAIL" for r in rows)
    skipped = sum(r["status"] == "SKIP" for r in rows)
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = "app_tests" if local else "app_tests_remote"
    (out / f"{stem}.json").write_text(json.dumps(rows, indent=2) + "\n")
    (out / f"{stem}.txt").write_text("\n".join(
        f"{r['status']:4}  {r['id']:4} {r['title']}" + (f"  <- {r['detail']}" if r['detail'] else "")
        for r in rows) + f"\n\n{passed} passed, {failed} failed, {skipped} skipped\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
