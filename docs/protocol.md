# Application protocol

Transport: TCP, server port 5050. Framing: one JSON object per line (`\n` terminated, UTF-8). A message larger than 64 KiB without a newline closes the connection.

## Message sequence

```
client                                server
  |<------------- welcome ------------|
  |-------------- join -------------->|    must arrive within JOIN_TIMEOUT (30 s) or the connection is closed
  |<------------ exam_start ----------|    exam timer starts now (EXAM_DURATION, default 60 s)
  |<------------- question 1 ---------|
  |-------------- answer ------------>|    invalid answer -> error, same question again (timer keeps running)
  |<------------- question 2 ---------|
  ...
  |<------------- result -------------|    after the last answer or when the timer expires; server closes
```

## Messages

| Direction | Message | Fields |
|---|---|---|
| S → C | `welcome` | `message` |
| C → S | `join` | `name` (trimmed, max 40 chars, default "Student") |
| S → C | `exam_start` | `duration` (s), `total` (questions) |
| S → C | `question` | `number`, `text`, `options[]` |
| C → S | `answer` | `choice` (integer, 1-based option number; booleans, strings, out-of-range are invalid) |
| S → C | `error` | `message` |
| S → C | `result` | `name`, `score`, `total`, `answered`, `time_expired` |

Error handling: malformed JSON or a non-object during the join phase closes that connection only; a non-object during the exam gets an `error`;
a first message other than `join` closes the connection; a client that disconnects mid-exam is cleaned up. All cases are covered by `tests/app_tests.py` (A4–A11).

## Signalling datagrams (server → controller)

UDP, destination `10.0.3.254:9999`, JSON payload, fire-and-forget; enabled by `SDN_HOOK_ADDR`. Consumed by the controller (packet-in), never delivered to a socket.

| `type` | Fields | Meaning |
|---|---|---|
| `heartbeat` | `active`, `completed`, `ts` | once per second; the controller uses `active` as the number of exams in progress |
| `exam_start` / `exam_end` | `student`, `ip`, `ts` | counted by the controller |
| `reset` | – | experiment helper: release all quarantines (accepted only from the server's IP) |

The controller drops datagrams whose IP source is not `EXAM_SERVER_IP` and logs `CONTROL_REJECTED`.
