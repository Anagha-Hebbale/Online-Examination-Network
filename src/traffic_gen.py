import argparse
import json
import socket
import time


def sink(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    total = 0
    print(f"UDP sink listening on {port}", flush=True)
    while True:
        data, _ = sock.recvfrom(65535)
        total += len(data)


def flood(host, port, mbps, duration, size):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = b"x" * size
    interval = size * 8 / (mbps * 1e6)
    start = time.monotonic()
    next_send = start
    sent = 0
    while time.monotonic() - start < duration:
        try:
            sock.sendto(payload, (host, port))
            sent += 1
        except OSError:
            pass 
        next_send += interval
        delay = next_send - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    elapsed = time.monotonic() - start
    print(json.dumps({"sent_packets": sent, "sent_mbit": round(sent * size * 8 / 1e6, 2),
                      "offered_mbps": round(sent * size * 8 / 1e6 / elapsed, 2), "seconds": round(elapsed, 2)}))


def ctl(addr, payload):
    host, _, port = addr.rpartition(":")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(payload.encode("utf-8"), (host, int(port)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sink")
    p.add_argument("--port", type=int, default=9000)
    p = sub.add_parser("flood")
    p.add_argument("--host", default="10.0.3.100")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--mbps", type=float, default=20)
    p.add_argument("--duration", type=float, default=20)
    p.add_argument("--size", type=int, default=1200)
    p = sub.add_parser("ctl")
    p.add_argument("--addr", default="10.0.3.254:9999")
    p.add_argument("--json", required=True)
    args = parser.parse_args()
    if args.cmd == "sink":
        sink(args.port)
    elif args.cmd == "flood":
        flood(args.host, args.port, args.mbps, args.duration, args.size)
    else:
        ctl(args.addr, args.json)


if __name__ == "__main__":
    main()
