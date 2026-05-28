"""CLI entry: `python -m pingstate ...` or, after install, `pingstate ...`.

Wraps the library API so the same probe types are usable from the command
line. For richer behavior (custom check callables, custom transition tables,
multiple probes) import the library directly.
"""

from __future__ import annotations

import argparse
import sys

from . import Monitor, probe, serve


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pingstate",
        description="Probe a target, track up/down with a state machine, serve via HTTP.",
    )
    ap.add_argument(
        "--protocol",
        choices=("tcp", "http", "https"),
        default="tcp",
        help="probe protocol (default: tcp)",
    )
    ap.add_argument(
        "--address",
        default="1.1.1.1",
        help="host or IP to probe (default: 1.1.1.1)",
    )
    ap.add_argument(
        "--port",
        type=int,
        default=None,
        help="port to probe (default: 443 for tcp/https, 80 for http)",
    )
    ap.add_argument(
        "--path",
        default="/",
        help="URL path for http/https probes (default: /)",
    )
    ap.add_argument(
        "--check",
        default=None,
        help=(
            "what counts as up. http: 'status:200,204', 'body_contains:pong', "
            "or omit for any 2xx. tcp: 'banner_contains:SSH-' or omit for "
            "successful connect."
        ),
    )
    ap.add_argument(
        "--timeout", type=float, default=None, help="per-probe timeout in seconds"
    )
    ap.add_argument(
        "--no-verify-tls",
        action="store_true",
        help="skip TLS cert verification (https only)",
    )
    ap.add_argument(
        "--interval", type=float, default=5.0, help="seconds between probes"
    )
    ap.add_argument(
        "--api-host", default="127.0.0.1", help="HTTP API bind address"
    )
    ap.add_argument(
        "--api-port", type=int, default=8787, help="HTTP API port"
    )
    return ap


def main() -> None:
    args = _build_parser().parse_args()

    # The factory requires an explicit port for TCP. The CLI documents 443 as
    # the TCP default (matching the original single-file script), so apply it
    # here before handing off.
    if args.protocol == "tcp" and args.port is None:
        args.port = 443

    extra: dict = {}
    if args.protocol in ("http", "https") and args.no_verify_tls:
        extra["verify_tls"] = False

    p = probe(
        protocol=args.protocol,
        address=args.address,
        port=args.port,
        path=args.path,
        check=args.check,
        timeout=args.timeout,
        **extra,
    )

    monitor = Monitor(p, interval=args.interval).start()
    server = serve(monitor, host=args.api_host, port=args.api_port)

    print(
        f"pingstate: probing {p.name} every {args.interval}s, "
        f"serving on http://{args.api_host}:{args.api_port}",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    finally:
        monitor.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
