import json
import os
import socket
import time
import select
import sys
import math

HOST = os.environ.get("EXAM_HOST", "127.0.0.1")
PORT = int(os.environ.get("EXAM_PORT", "5050"))


class Connection:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""

    def send(self, data):
        message = json.dumps(data).encode("utf-8") + b"\n"
        self.sock.sendall(message)

    def receive(self):
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(4096)

            if not chunk:
                raise ConnectionError("Server disconnected")

            self.buffer += chunk

        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))


def ask_answer(question, deadline):
    print(f"\nQuestion {question['number']}: {question['text']}")

    for number, option in enumerate(question["options"], 1):
        print(f"  {number}. {option}")

    last_displayed = None
    while True:
        remaining = deadline - time.monotonic()

        if remaining <= 0:
            print("\nTime is up.")
            return None

        seconds = math.ceil(remaining)
        if last_displayed is None or (seconds != last_displayed and (seconds % 5 == 0 or seconds <= 5)):
            minutes, seconds_part = divmod(seconds, 60)
            print(f"\nTime remaining: {minutes}:{seconds_part:02d}")
            print("Enter option number: ", end="", flush=True)
            last_displayed = seconds

        ready, _, _ = select.select(
            [sys.stdin], [], [], min(1.0, remaining)
        )

        if not ready:
            continue

        answer = sys.stdin.readline()

        if not answer:
            return None

        try:
            return int(answer.strip())
        except ValueError:
            print("Please enter a number.")
            last_displayed = None


def show_result(message):
    print("\n--- EXAM RESULT ---")
    print(f"Student: {message['name']}")
    print(f"Score: {message['score']}/{message['total']}")
    print(f"Questions answered: {message['answered']}")
    print(f"Time expired: {message['time_expired']}")


def main():
    name = input("Enter student name: ").strip() or "Student"

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((HOST, PORT))
        conn = Connection(sock)

        welcome = conn.receive()
        print(welcome["message"])

        conn.send({"type": "join", "name": name})

        start = conn.receive()

        if start.get("type") != "exam_start":
            print("Could not start the exam.")
            return

        deadline = time.monotonic() + start["duration"]

        print(
            f"\nExam started! {start['total']} questions, "
            f"{start['duration']} seconds."
        )

        message = conn.receive()

        while True:
            kind = message.get("type")

            if kind == "result":
                show_result(message)
                break

            if kind == "question":
                question = message

                while True:
                    choice = ask_answer(question, deadline)

                    if choice is None:
                        print("Waiting for the final result...")
                        message = conn.receive()
                        break

                    conn.send({
                        "type": "answer",
                        "choice": choice
                    })

                    response = conn.receive()

                    if response.get("type") == "error":
                        print(response["message"])
                        # Retry the same question without resetting the timer.
                        continue

                    message = response
                    break

                continue

            if kind == "error":
                print(message["message"])
                message = conn.receive()
                continue

            print("Unexpected server response.")
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExam client stopped.")
    except (OSError, ConnectionError, json.JSONDecodeError) as error:
        print(f"\nConnection error: {error}")
