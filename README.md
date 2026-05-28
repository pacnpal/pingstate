# pingstate

A small daemon that probes a target, tracks whether it's up or down with a state machine, and serves the current state over a local HTTP API. Standard library only. No dependencies.

It checks a TCP port rather than ICMP ping, so it runs as your normal user with no root and no `ping` binary. "Up" means the port answered the connection. For homelab monitoring this is usually what you actually want to know, since a host can reply to ping while the service behind it is dead.

## How it works

A poller opens a TCP connection to the target on a set interval. Success or failure becomes an event (`ok` or `fail`) fed into a four-state machine. An HTTP server reads the current state and hands it back as JSON or plain text.

The states are `unknown`, `up`, `degraded`, and `down`. A single failure from `up` drops to `degraded`, not straight to `down`. A second failure marks it `down`. Any success snaps back to `up` immediately. The degraded tier means a one-off blip shows up as degraded instead of flapping the whole status.

Transitions are a plain lookup table. If you want different behavior, like a hard up/down with no degraded tier, or N failures before flipping, it's a small edit to that table.

## Running it

```bash
python3 pingstate.py --target 192.168.86.3 --check-port 443 --port 8787 --interval 5
```

Point `--check-port` at whatever service matters on the target. 443 for a reverse proxy, 53 for DNS, 3306 for a database, and so on. The `--port` flag is the HTTP API port, separate from the port being checked.

## Querying it

```bash
curl localhost:8787/         # full status as JSON
curl localhost:8787/state    # just the state string
curl localhost:8787/healthz  # 200 if up, 503 otherwise
```

The `/healthz` endpoint returns standard HTTP health codes, so it drops into any uptime check or dashboard that expects an HTTP probe.

Full status looks like this:

```json
{
  "state": "up",
  "last_event": "ok",
  "last_latency_ms": 0.33,
  "uptime_in_state_s": 42.0,
  "last_check_age_s": 1.0,
  "transitions": 1,
  "recent": [ ... ],
  "target": "192.168.86.3"
}
```

`recent` is a rolling log of the last 20 checks so you can see the recent history without standing up any external logging.

## Flags

| Flag | Default | What it does |
|------|---------|--------------|
| `--target` | `1.1.1.1` | host or IP to probe |
| `--check-port` | `443` | TCP port to probe on the target |
| `--port` | `8787` | HTTP API port |
| `--host` | `127.0.0.1` | bind address for the API |
| `--interval` | `5.0` | seconds between probes |

## Limitations

One instance probes one target/port pair. If you want to watch several services from a single daemon, that's the next step: a dict of independent state machines keyed by name, with endpoints like `/status/adguard`.

It's a TCP connect check, not a protocol check. It tells you the port accepted a connection, not that the service behind it is healthy. For most cases the connection answer is enough. If you need real protocol health, you'd swap the probe function for something that speaks the actual protocol.

## License

MIT. See [LICENSE](LICENSE).
