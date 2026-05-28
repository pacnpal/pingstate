#!/usr/bin/env python3
"""pingstate. TCP-probe a target, track its state, serve it over a local HTTP API.

Checks whether a TCP port is accepting connections instead of using ICMP ping,
so it runs as your normal user with no external binary. "Up" means the port
answered.

Run:
    python3 pingstate.py --target 1.1.1.1 --check-port 443 --port 8787 --interval 5

Query:
    curl localhost:8787/         # full status as JSON
    curl localhost:8787/state    # just the state string
    curl localhost:8787/healthz  # 200 if up, 503 if down

Stdlib only. No dependencies.
"""

import argparse
import json
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- state machine -----------------------------------------------------------

# The state machine is a lookup table: (state, event) maps to the next state.
# Events come from probe results, either "ok" or "fail".
#
# States go unknown, then up or down, with degraded sitting in between. A host
# that's up drops to degraded on the first failure, not straight to down. That
# gives you a softer signal so a single blip doesn't flap the whole status.

TRANSITIONS = {
    ("unknown", "ok"): "up",
    ("unknown", "fail"): "down",
    ("up", "ok"): "up",
    ("up", "fail"): "degraded",
    ("degraded", "ok"): "up",
    ("degraded", "fail"): "down",
    ("down", "ok"): "up",
    ("down", "fail"): "down",
}


class PingFSM:
    def __init__(self, initial="unknown"):
        self.state = initial
        self.lock = threading.Lock()
        self.last_event = None
        self.last_change = time.time()
        self.last_check = None
        self.last_latency_ms = None
        self.transitions_count = 0
        self.recent = deque(maxlen=20)  # last 20 checks

    def fire(self, event, latency_ms=None):
        with self.lock:
            key = (self.state, event)
            next_state = TRANSITIONS.get(key, self.state)
            now = time.time()
            if next_state != self.state:
                self.state = next_state
                self.last_change = now
                self.transitions_count += 1
            self.last_event = event
            self.last_check = now
            self.last_latency_ms = latency_ms
            self.recent.append({"ts": now, "event": event, "state": self.state})
            return self.state

    def snapshot(self):
        with self.lock:
            now = time.time()
            return {
                "state": self.state,
                "last_event": self.last_event,
                "last_latency_ms": self.last_latency_ms,
                "uptime_in_state_s": round(now - self.last_change, 1),
                "last_check_ts": self.last_check,
                "last_check_age_s": (
                    round(now - self.last_check, 1) if self.last_check else None
                ),
                "transitions": self.transitions_count,
                "recent": list(self.recent),
            }


# --- tcp probe ---------------------------------------------------------------


def check_once(target, port, timeout=2):
    """Return connect latency in ms if the TCP port answers, else None.

    Stdlib only, no root, no external binary. Up means the connection
    completed. Refused, timed out, or unreachable all return None, which
    counts as a failure.
    """
    start = time.perf_counter()
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return round((time.perf_counter() - start) * 1000, 2)
    except OSError:
        return None


def poller(fsm, target, port, interval, stop_event):
    while not stop_event.is_set():
        latency = check_once(target, port)
        event = "ok" if latency is not None else "fail"
        fsm.fire(event, latency_ms=latency)
        stop_event.wait(interval)


# --- http api ----------------------------------------------------------------


def make_handler(fsm, target):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, content_type="application/json"):
            payload = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path in ("/", "/status"):
                snap = fsm.snapshot()
                snap["target"] = target
                self._send(200, json.dumps(snap, indent=2))
            elif self.path == "/state":
                self._send(200, fsm.snapshot()["state"] + "\n", "text/plain")
            elif self.path == "/healthz":
                state = fsm.snapshot()["state"]
                if state == "up":
                    self._send(200, "ok\n", "text/plain")
                else:
                    self._send(503, state + "\n", "text/plain")
            else:
                self._send(404, "not found\n", "text/plain")

        def log_message(self, *args):
            pass  # don't log every request

    return Handler


# --- main --------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="TCP-probe state machine with local HTTP API")
    ap.add_argument("--target", default="1.1.1.1", help="host/IP to probe")
    ap.add_argument("--check-port", type=int, default=443, help="TCP port to probe on target")
    ap.add_argument("--port", type=int, default=8787, help="HTTP API port")
    ap.add_argument("--host", default="127.0.0.1", help="bind address")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between probes")
    args = ap.parse_args()

    fsm = PingFSM()
    stop_event = threading.Event()

    t = threading.Thread(
        target=poller,
        args=(fsm, args.target, args.check_port, args.interval, stop_event),
        daemon=True,
    )
    t.start()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(fsm, args.target))
    print(
        f"pingstate: probing {args.target}:{args.check_port} every {args.interval}s, "
        f"serving on http://{args.host}:{args.port}",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    finally:
        stop_event.set()
        server.shutdown()


if __name__ == "__main__":
    main()
