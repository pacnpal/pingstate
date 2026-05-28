"""HTTP API for serving a Monitor's current state.

Endpoints:
  GET /          — full snapshot as JSON (alias for /status)
  GET /status    — full snapshot as JSON
  GET /state     — just the state word, plain text
  GET /healthz   — 200 if up, 503 otherwise; standard health-probe shape

`serve(monitor)` returns a ThreadingHTTPServer you call `.serve_forever()` on.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_handler(monitor):
    """Build a BaseHTTPRequestHandler subclass bound to `monitor`."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body, content_type: str = "application/json") -> None:
            payload = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.path in ("/", "/status"):
                self._send(200, json.dumps(monitor.snapshot(), indent=2))
            elif self.path == "/state":
                self._send(200, monitor.snapshot()["state"] + "\n", "text/plain")
            elif self.path == "/healthz":
                state = monitor.snapshot()["state"]
                if state == "up":
                    self._send(200, "ok\n", "text/plain")
                else:
                    self._send(503, state + "\n", "text/plain")
            else:
                self._send(404, "not found\n", "text/plain")

        def log_message(self, *args) -> None:
            pass  # silence per-request logging

    return Handler


def serve(monitor, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    """Return a ThreadingHTTPServer bound to (host, port). Caller drives it."""
    return ThreadingHTTPServer((host, port), make_handler(monitor))
