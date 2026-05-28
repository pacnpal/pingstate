"""Probes turn "is this thing healthy?" into a yes-or-no event for the FSM.

Two built-ins ship: TCPProbe (connect and you're up) and HTTPProbe (status code,
optional body match, optional custom callable). A dev who wants something else
implements the Probe protocol — a `.name` and a `.check() -> ProbeResult`.

The `probe(...)` factory is the dev-facing front door: name a protocol, an
address, a port, and what to check, get back a ready-to-use Probe.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol, Union, runtime_checkable


# --- result and protocol -----------------------------------------------------


@dataclass
class ProbeResult:
    """Outcome of one probe attempt."""

    ok: bool
    latency_ms: float | None
    detail: str | None = None


@runtime_checkable
class Probe(Protocol):
    """A probe knows its name and can be checked on demand."""

    name: str

    def check(self) -> ProbeResult: ...


@dataclass
class HTTPResponseSnapshot:
    """What a check callable sees for HTTP probes.

    Kept tiny on purpose. `body` is the raw bytes (capped at the probe's
    max_body_bytes); `text` decodes with a Content-Type charset guess.
    `elapsed_ms` covers headers + body read.
    """

    status: int
    headers: dict
    body: bytes
    text: str
    elapsed_ms: float


# Check callables can return bool, or (bool, detail) for richer reporting.
CheckResult = Union[bool, tuple[bool, "str | None"]]
HTTPCheck = Callable[[HTTPResponseSnapshot], CheckResult]
TCPCheck = Callable[[socket.socket], CheckResult]


def _normalize(check_out: CheckResult) -> tuple[bool, str | None]:
    if isinstance(check_out, tuple):
        ok, detail = check_out
        return bool(ok), detail
    return bool(check_out), None


# --- mini-DSL for the `check=` string form -----------------------------------


def _parse_int_list(value: str) -> tuple[int, ...]:
    return tuple(int(v.strip()) for v in value.split(",") if v.strip())


def _http_check_from_string(spec: str | None) -> HTTPCheck:
    # Built-in checks set a `.needs_body` attribute so HTTPProbe can skip the
    # body read when only the status code matters. Custom callables get the
    # body by default (treated as if they need it) since we can't introspect.
    if spec in (None, "", "default", "status_2xx"):
        def _default(resp: HTTPResponseSnapshot) -> CheckResult:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
        _default.needs_body = False  # type: ignore[attr-defined]
        return _default

    if spec.startswith("status:"):
        codes = frozenset(_parse_int_list(spec[len("status:"):]))

        def _status(resp: HTTPResponseSnapshot) -> CheckResult:
            return resp.status in codes, f"HTTP {resp.status}"

        _status.needs_body = False  # type: ignore[attr-defined]
        return _status

    if spec.startswith("body_contains:"):
        needle = spec[len("body_contains:"):].encode()

        def _body(resp: HTTPResponseSnapshot) -> CheckResult:
            ok = 200 <= resp.status < 300 and needle in resp.body
            return ok, f"HTTP {resp.status}"

        _body.needs_body = True  # type: ignore[attr-defined]
        return _body

    raise ValueError(f"unknown http check spec: {spec!r}")


def _tcp_check_from_string(spec: str | None) -> TCPCheck | None:
    if spec in (None, "", "default", "port_open"):
        return None  # connect alone is the check

    if spec.startswith("banner_contains:"):
        needle = spec[len("banner_contains:"):].encode()

        def _banner(sock: socket.socket) -> CheckResult:
            # Keep the timeout that create_connection set on the socket so the
            # caller's --timeout / TCPProbe(timeout=...) applies to the read.
            try:
                data = sock.recv(256)
            except OSError as e:
                return False, f"recv failed: {e}"
            ok = needle in data
            return ok, f"banner: {data[:64]!r}"

        return _banner

    raise ValueError(f"unknown tcp check spec: {spec!r}")


# --- TCP probe ---------------------------------------------------------------


class TCPProbe:
    """Connect to host:port and (optionally) inspect the open socket.

    `check` is one of:
      None / "default" / "port_open" — successful connect is up.
      "banner_contains:<text>"       — connect + read; up if banner matches.
      callable(socket) -> bool       — full control after connect.
      callable(socket) -> (bool, str)— same, with a custom detail string.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float = 2.0,
        check: TCPCheck | str | None = None,
        name: str | None = None,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.name = name or f"tcp://{host}:{port}"
        if isinstance(check, str) or check is None:
            self._check_fn = _tcp_check_from_string(check)
        else:
            self._check_fn = check

    def check(self) -> ProbeResult:
        start = time.perf_counter()
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            ) as sock:
                latency_ms = round((time.perf_counter() - start) * 1000, 2)
                if self._check_fn is None:
                    return ProbeResult(True, latency_ms, "connect ok")
                ok, detail = _normalize(self._check_fn(sock))
                if detail is None:
                    detail = "connect ok" if ok else "check failed"
                return ProbeResult(ok, latency_ms, detail)
        except OSError as e:
            return ProbeResult(False, None, str(e))


# --- HTTP probe --------------------------------------------------------------


_DEFAULT_USER_AGENT = "pingstate/0.0.2"
_MAX_BODY_BYTES = 64 * 1024  # cap to keep memory bounded


class HTTPProbe:
    """Send a request and decide ok-ness from status (and optionally body).

    `check` is one of:
      None / "default" / "status_2xx"  — 2xx is up.
      "status:200,204"                 — listed codes are up.
      "body_contains:pong"             — 2xx AND body contains the substring.
      callable(HTTPResponseSnapshot) -> bool             — full custom.
      callable(HTTPResponseSnapshot) -> (bool, str)      — same, with detail.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 5.0,
        method: str = "GET",
        headers: dict | None = None,
        verify_tls: bool = True,
        check: HTTPCheck | str | None = None,
        name: str | None = None,
        max_body_bytes: int = _MAX_BODY_BYTES,
    ):
        self.url = url
        self.timeout = timeout
        self.method = method
        self.headers = headers or {}
        self.verify_tls = verify_tls
        self.max_body_bytes = max_body_bytes
        self.name = name or url

        if isinstance(check, str) or check is None:
            self._check_fn: HTTPCheck = _http_check_from_string(check)
        else:
            self._check_fn = check

        self._ssl_ctx: ssl.SSLContext | None = None
        if url.startswith("https://") and not verify_tls:
            self._ssl_ctx = ssl.create_default_context()
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def check(self) -> ProbeResult:
        hdrs = {"User-Agent": _DEFAULT_USER_AGENT, **self.headers}
        req = urllib.request.Request(self.url, method=self.method, headers=hdrs)
        # Built-in status checks set needs_body=False so we don't burn the
        # socket timeout reading bodies we don't care about. Custom callables
        # default to needing the body.
        needs_body = getattr(self._check_fn, "needs_body", True)
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout, context=self._ssl_ctx
            ) as resp:
                body = resp.read(self.max_body_bytes) if needs_body else b""
                elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
                snap = self._snapshot(resp.status, dict(resp.headers), body, elapsed_ms)
        except urllib.error.HTTPError as e:
            # HTTPError is also a response object — let the check decide.
            body = b""
            if needs_body:
                try:
                    body = e.read(self.max_body_bytes)
                except Exception:
                    pass
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            snap = self._snapshot(e.code, dict(e.headers or {}), body, elapsed_ms)
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,  # BadStatusLine, IncompleteRead, etc.
        ) as e:
            return ProbeResult(False, None, str(e) or type(e).__name__)

        ok, detail = _normalize(self._check_fn(snap))
        return ProbeResult(ok, snap.elapsed_ms, detail or f"HTTP {snap.status}")

    def _snapshot(
        self, status: int, headers: dict, body: bytes, elapsed_ms: float
    ) -> HTTPResponseSnapshot:
        # HTTP field names are case-insensitive; the dict from
        # `dict(resp.headers)` is not, so look up case-insensitively.
        charset = "utf-8"
        ctype = ""
        for k, v in headers.items():
            if k.lower() == "content-type":
                ctype = v
                break
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        return HTTPResponseSnapshot(
            status=status, headers=headers, body=body, text=text, elapsed_ms=elapsed_ms
        )


# --- factory -----------------------------------------------------------------


def probe(
    protocol: str,
    address: str,
    port: int | None = None,
    *,
    path: str = "/",
    check: str | HTTPCheck | TCPCheck | None = None,
    timeout: float | None = None,
    name: str | None = None,
    **kwargs,
) -> Probe:
    """Build a Probe from a protocol + address + port + what to check.

    >>> probe("tcp", "1.1.1.1", 443)
    >>> probe("http", "example.com", 80, path="/healthz", check="status:200,204")
    >>> probe("https", "api.example.com", 443, path="/v1/ping",
    ...       check=lambda r: r.status == 200 and b"pong" in r.body)
    """
    p = protocol.lower()
    if p == "tcp":
        if port is None:
            raise ValueError("tcp probe needs a port")
        kw: dict = {"host": address, "port": port, "check": check}
        if timeout is not None:
            kw["timeout"] = timeout
        if name is not None:
            kw["name"] = name
        kw.update(kwargs)
        return TCPProbe(**kw)

    if p in ("http", "https"):
        if port is None:
            port = 443 if p == "https" else 80
        if not path.startswith("/"):
            path = "/" + path
        # IPv6 literals must be bracketed in URLs (RFC 3986). Hostnames can't
        # contain ":", so the colon is a reliable signal.
        host_in_url = address
        if ":" in address and not address.startswith("["):
            host_in_url = f"[{address}]"
        url = f"{p}://{host_in_url}:{port}{path}"
        kw = {"url": url, "check": check}
        if timeout is not None:
            kw["timeout"] = timeout
        if name is not None:
            kw["name"] = name
        kw.update(kwargs)
        return HTTPProbe(**kw)

    raise ValueError(f"unknown protocol: {protocol!r}")
