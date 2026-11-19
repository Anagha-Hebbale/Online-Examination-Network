import json
import os
import select
import socket
import threading
import time

HOST = "0.0.0.0"
PORT = int(os.environ.get("EXAM_PORT", "5050"))
EXAM_DURATION = float(os.environ.get("EXAM_DURATION", "60"))
JOIN_TIMEOUT = float(os.environ.get("JOIN_TIMEOUT", "30"))
RESULTS_FILE = os.environ.get("EXAM_RESULTS_FILE", "")
HOOK_ADDR = os.environ.get("SDN_HOOK_ADDR", "")
HEARTBEAT_SECONDS = 1.0

QUESTIONS = [
    {
        "question": "What is 2 + 2?",
        "options": ["1", "3", "4", "5"],
        "answer": 3
    },
    {
        "question": "Which protocol provides reliable delivery?",
        "options": ["UDP", "TCP", "IP", "ARP"],
        "answer": 2
    },
    {
        "question": "What does CPU stand for?",
        "options": [
            "Central Processing Unit",
            "Computer Personal Unit",
            "Control Program Utility",
            "Central Process Utility"
        ],
        "answer": 1
    }
]

state_lock = threading.Lock()
active_exams = 0
completed_exams = 0
results_lock = threading.Lock()

class SdnHook:
    
    def __init__(self, addr):
        self.target = None
        self.sock = None
        if addr and addr.lower() != "off":
            host, _, port = addr.rpartition(":")
            self.target = (host, int(port))
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setblocking(False)

    def send(self, payload):
        if not self.sock:
            return
        payload["ts"] = time.time()
        try:
            self.sock.sendto(json.dumps(payload).encode("utf-8"), self.target)
        except OSError:
            pass  

hook = SdnHook(HOOK_ADDR)

def heartbeat_loop():
    while True:
        with state_lock:
            snapshot = {"type": "heartbeat", "active": active_exams, "completed": completed_exams}
        hook.send(snapshot)
        time.sleep(HEARTBEAT_SECONDS)

class Connection:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""

    def send(self, data):
        message = json.dumps(data).encode("utf-8") + b"\n"
        self.sock.sendall(message)

    def receive(self, deadline=None):
        """Return one decoded JSON message, or None if the deadline passes."""
        while b"\n" not in self.buffer:
            timeout = None

            if deadline is not None:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    return None

            ready, _, _ = select.select([self.sock], [], [], timeout)

            if not ready:
                return None

            chunk = self.sock.recv(4096)

            if not chunk:
                raise ConnectionError("Client disconnected")

            self.buffer += chunk

            if len(self.buffer) > 65536 and b"\n" not in self.buffer:
                raise ValueError("Message too large")

        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))


def send_result(conn, name, score, answered, expired):
    conn.send({
        "type": "result",
        "name": name,
        "score": score,
        "total": len(QUESTIONS),
        "answered": answered,
        "time_expired": expired
    })


def record_result(name, address, score, answered, expired):
    if not RESULTS_FILE:
        return
    line = json.dumps({
        "ts": time.time(), "name": name, "ip": address[0], "score": score,
        "total": len(QUESTIONS), "answered": answered, "time_expired": expired,
    })
    with results_lock:
        with open(RESULTS_FILE, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def handle_student(sock, address):
    global active_exams, completed_exams
    conn = Connection(sock)
    name = "Student"
    in_exam = False

    try:
        conn.send({
            "type": "welcome",
            "message": "Connected to the examination server."
        })

        join_deadline = time.monotonic() + JOIN_TIMEOUT
        data = conn.receive(join_deadline)

        if not isinstance(data, dict) or data.get("type") != "join":
            return

        name = str(data.get("name", "Student")).strip()[:40]
        if not name:
            name = "Student"

        deadline = time.monotonic() + EXAM_DURATION

        with state_lock:
            active_exams += 1
        in_exam = True
        hook.send({"type": "exam_start", "student": name, "ip": address[0]})

        conn.send({
            "type": "exam_start",
            "duration": EXAM_DURATION,
            "total": len(QUESTIONS)
        })

        score = 0
        answered = 0
        expired = False

        for index, question in enumerate(QUESTIONS):
            if time.monotonic() >= deadline:
                expired = True
                break

            conn.send({
                "type": "question",
                "number": index + 1,
                "text": question["question"],
                "options": question["options"]
            })

            while True:
                data = conn.receive(deadline)

                if data is None:
                    expired = True
                    break

                choice = data.get("choice") if isinstance(data, dict) else None

                if (
                    not isinstance(data, dict)
                    or data.get("type") != "answer"
                    or not isinstance(choice, int)
                    or isinstance(choice, bool)
                    or not 1 <= choice <= len(question["options"])
                ):
                    conn.send({
                        "type": "error",
                        "message": "Invalid choice. Enter a valid option number."
                    })
                    continue

                answered += 1

                if choice == question["answer"]:
                    score += 1

                break

            if expired:
                break

        send_result(conn, name, score, answered, expired)
        record_result(name, address, score, answered, expired)

        print(
            f"{name} ({address[0]}): "
            f"{score}/{len(QUESTIONS)}, "
            f"answered {answered}, expired={expired}",
            flush=True
        )

    except (
        OSError,
        ConnectionError,
        ValueError,
        json.JSONDecodeError
    ) as error:
        print(f"{name}: connection ended ({error})", flush=True)

    finally:
        if in_exam:
            with state_lock:
                active_exams -= 1
                completed_exams += 1
            hook.send({"type": "exam_end", "student": name, "ip": address[0]})
        sock.close()


def main():
    with socket.socket(
        socket.AF_INET, socket.SOCK_STREAM
    ) as server:
        server.setsockopt(
            socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
        )
        server.bind((HOST, PORT))
        server.listen(128)

        print(f"Exam server running on port {PORT}", flush=True)
        print(f"Exam duration: {EXAM_DURATION:g} seconds", flush=True)
        print(f"SDN hook: {HOOK_ADDR or 'off'}", flush=True)
        print("Waiting for students...", flush=True)

        if hook.sock:
            threading.Thread(target=heartbeat_loop, daemon=True).start()

        while True:
            sock, address = server.accept()

            threading.Thread(
                target=handle_student,
                args=(sock, address),
                daemon=True
            ).start()


if __name__ == "__main__":
    main()
