# pingstate

A small Python module and daemon that probes a target, tracks whether it's up or down with a state machine, and serves the current state over a local HTTP API. Standard library only. No dependencies.

It ships with two probes — TCP (does the port answer?) and HTTP/HTTPS (does the URL return what you expect?). Writing your own probe is one method on one class.

There are three ways to use it:

1. [As a quick CLI tool](#1-simple-usage-cli) — one command, one target.
2. [As a Python module](#2-use-as-a-module) — import it into your own code.
3. [As a long-running service](#3-run-as-a-service) — systemd unit on a homelab box.

---

## Install

```bash
pip install git+https://github.com/pacnpal/pingstate
```

Or, for development:

```bash
git clone https://github.com/pacnpal/pingstate
cd pingstate
pip install -e .
```

That gives you a `pingstate` console script and an importable `pingstate` package.

---

## 1. Simple usage (CLI)

The CLI takes a protocol, address, port, and (optionally) what counts as "up".

```bash
# Is a TCP port answering?
pingstate --protocol tcp --address 1.1.1.1 --port 443
```

In another terminal:

```bash
curl localhost:8787/         # full status as JSON
curl localhost:8787/state    # just the state string
curl localhost:8787/healthz  # 200 if up, 503 otherwise
```

That's it. The daemon polls every five seconds, runs the state through `unknown → up/degraded/down`, and serves it.

A few more shapes:

```bash
# HTTP — any 2xx counts as up
pingstate --protocol http --address api.example.com --port 80 --path /healthz

# HTTPS — only specific status codes count
pingstate --protocol https --address api.example.com --port 443 \
    --path /v1/ping --check status:200,204

# HTTPS — body must contain a literal string
pingstate --protocol https --address example.com --port 443 \
    --path /health --check body_contains:OK

# TCP with a banner check — useful for SSH, SMTP, etc.
pingstate --protocol tcp --address git.example.com --port 22 \
    --check banner_contains:SSH-
```

See [CLI flags](#cli-flags) below for the full list.

---

## 2. Use as a module

The module exports a `probe(...)` factory, a `Monitor` for background polling, and a `serve(...)` helper for the HTTP API.

### The shortest possible thing

```python
from pingstate import probe

p = probe("https", "api.example.com", 443, path="/healthz")
result = p.check()
print(result.ok, result.latency_ms, result.detail)
```

No state machine, no threads — just one probe call, get back a `ProbeResult`.

### Run it on a timer with state tracking

```python
from pingstate import probe, Monitor

p = probe("https", "api.example.com", 443, path="/healthz",
          check="status:200,204")

monitor = Monitor(p, interval=5).start()

# ...do other work...

snap = monitor.snapshot()
print(snap["state"])           # "up", "degraded", "down", or "unknown"
print(snap["last_latency_ms"]) # last observed latency
print(snap["recent"])          # rolling log of the last 20 checks
```

`Monitor` runs the probe on a background thread and feeds results into a `PingFSM`. Call `.snapshot()` whenever you want the current state.

### Add the HTTP API

```python
from pingstate import probe, Monitor, serve

p = probe("tcp", "192.168.86.3", 5432)
monitor = Monitor(p, interval=10).start()

server = serve(monitor, host="127.0.0.1", port=8787)
server.serve_forever()
```

Same endpoints as the CLI: `/`, `/status`, `/state`, `/healthz`.

### Custom check logic

For anything the string mini-DSL can't express, hand `check=` a callable.

**HTTP** — the callable gets an `HTTPResponseSnapshot` with `status`, `headers`, `body` (bytes), `text` (decoded), and `elapsed_ms`:

```python
def is_healthy(resp):
    return resp.status == 200 and b'"ready": true' in resp.body

p = probe("https", "api.example.com", 443, path="/status", check=is_healthy)
```

**TCP** — the callable gets the open socket, so you can read a banner, send a probe byte, whatever:

```python
def is_ssh(sock):
    sock.settimeout(1.0)
    banner = sock.recv(64)
    return banner.startswith(b"SSH-")

p = probe("tcp", "git.example.com", 22, check=is_ssh)
```

Either form may return `(bool, detail_string)` instead of just `bool` if you want a custom message in the snapshot.

### Write a probe from scratch

The `Probe` protocol is a `.name` and a `.check()` that returns `ProbeResult`. That's the whole contract — anything that matches it works as a probe.

```python
from pingstate import Probe, ProbeResult, Monitor

class PostgresHealth:
    name = "postgres"
    def check(self) -> ProbeResult:
        # connect, run `SELECT 1`, time it
        ok, latency_ms = ...
        return ProbeResult(ok=ok, latency_ms=latency_ms, detail="select 1")

Monitor(PostgresHealth(), interval=10).start()
```

### Skip the Monitor entirely

If you want to drive the cadence yourself, wire the FSM and probe directly:

```python
from pingstate import probe, PingFSM

p = probe("tcp", "1.1.1.1", 443)
fsm = PingFSM()

while True:
    result = p.check()
    fsm.fire("ok" if result.ok else "fail",
             latency_ms=result.latency_ms,
             detail=result.detail)
    do_other_work()
```

`PingFSM.snapshot()` returns the same dict shape `Monitor` does.

---

## 3. Run as a service

For homelab use, the typical setup is a systemd unit that starts after the network is up and runs as an unprivileged user. A sample unit lives at [`pingstate.service`](pingstate.service).

```bash
# 1. install the package system-wide (or into a dedicated venv)
sudo pip install git+https://github.com/pacnpal/pingstate

# 2. create an unprivileged user for the daemon
sudo useradd --system --no-create-home --shell /usr/sbin/nologin pingstate

# 3. drop the unit in place
sudo curl -o /etc/systemd/system/pingstate.service \
    https://raw.githubusercontent.com/pacnpal/pingstate/main/pingstate.service

# 4. edit the ExecStart line — point it at your target
sudo systemctl edit --full pingstate.service

# 5. start it
sudo systemctl daemon-reload
sudo systemctl enable --now pingstate.service

# 6. check it
systemctl status pingstate.service
curl localhost:8787/state
```

The shipped unit:

- Waits on `network-online.target` so the first probe doesn't fail because the network isn't up yet.
- Runs as the `pingstate` user with no shell and no home directory.
- Restarts on failure with a five-second backoff.
- Applies systemd hardening (`NoNewPrivileges`, `ProtectSystem=strict`, `RestrictAddressFamilies=AF_INET AF_INET6`, etc.).

If you'd rather front it with nginx or wire `/healthz` into a dashboard, the API binds to `127.0.0.1` by default. Change `--api-host` if you want it on the LAN, or reverse-proxy it.

---

## State machine

States: `unknown`, `up`, `degraded`, `down`.

```
                      ok
                ┌─────────────┐
                │             ▼
unknown ──ok──▶ up ──fail──▶ degraded ──fail──▶ down
                ▲              │                   │
                └─────ok───────┘                   │
                ▲                                  │
                └────────────── ok ────────────────┘
```

A single failure from `up` drops to `degraded`, not straight to `down`. A second failure marks `down`. Any success snaps back to `up`. The degraded tier means a one-off blip surfaces as degraded instead of flapping the status.

Transitions are a plain dict. Pass your own to `PingFSM(transitions=...)` or `Monitor(transitions=...)` if you want a different policy — no degraded tier, N failures before flipping, whatever.

---

## HTTP API

```
GET /        full snapshot as JSON (alias for /status)
GET /status  full snapshot as JSON
GET /state   just the state word, plain text
GET /healthz 200 if up, 503 otherwise
```

A full snapshot:

```json
{
  "state": "up",
  "last_event": "ok",
  "last_detail": "HTTP 200",
  "last_latency_ms": 42.5,
  "uptime_in_state_s": 312.0,
  "last_check_age_s": 1.2,
  "transitions": 3,
  "recent": [
    { "ts": 1738200000.0, "event": "ok", "state": "up", "detail": "HTTP 200" }
  ],
  "probe": "https://api.example.com:443/healthz"
}
```

`recent` is a rolling log of the last 20 checks.

---

## CLI flags

| Flag | Default | What it does |
|------|---------|--------------|
| `--protocol` | `tcp` | one of `tcp`, `http`, `https` |
| `--address` | `1.1.1.1` | host or IP to probe |
| `--port` | `443` for tcp/https, `80` for http | port to probe |
| `--path` | `/` | URL path for http/https |
| `--check` | none (defaults to "connect ok" or "any 2xx") | `status:200,204`, `body_contains:OK`, `banner_contains:SSH-`, or omit |
| `--timeout` | probe default | per-probe timeout, seconds |
| `--no-verify-tls` | off | skip TLS verification (https only) |
| `--interval` | `5.0` | seconds between probes |
| `--api-host` | `127.0.0.1` | bind address for the HTTP API |
| `--api-port` | `8787` | port for the HTTP API |

---

## Limitations

One instance runs one probe. If you want to watch several services from a single Python process, compose multiple `Monitor` objects in your own script — there's no special multiplexing in the API, by design.

It's a connect-or-status check, not a deep protocol check. The HTTP probe can match status codes and body substrings; if you need real protocol health (a Postgres `SELECT 1`, a Redis `PING`), write a custom probe — the `Probe` protocol is two things.

## License

MIT. See [LICENSE](LICENSE).
