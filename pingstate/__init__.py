"""pingstate. A small, dependency-free state machine for health checks.

Pick a protocol, point it at an address and port, say what counts as healthy,
and you get back a Probe. Wrap it in a Monitor for background polling, or
serve the state over HTTP with `serve`. Pure stdlib.

Quick start:

    from pingstate import probe, Monitor, serve

    p = probe(
        protocol="https",
        address="api.example.com",
        port=443,
        path="/healthz",
        check="status:200,204",
    )
    m = Monitor(p, interval=5).start()
    serve(m).serve_forever()

Custom check logic is a callable:

    def is_pong(resp):
        return resp.status == 200 and b"pong" in resp.body

    p = probe("http", "127.0.0.1", 8080, path="/ping", check=is_pong)
"""

from .fsm import PingFSM, TRANSITIONS
from .probes import (
    HTTPProbe,
    HTTPResponseSnapshot,
    Probe,
    ProbeResult,
    TCPProbe,
    probe,
)
from .monitor import Monitor
from .http_api import make_handler, serve

__all__ = [
    "PingFSM",
    "TRANSITIONS",
    "Probe",
    "ProbeResult",
    "TCPProbe",
    "HTTPProbe",
    "HTTPResponseSnapshot",
    "probe",
    "Monitor",
    "make_handler",
    "serve",
]

__version__ = "0.0.2"
