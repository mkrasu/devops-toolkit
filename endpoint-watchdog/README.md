# endpoint-watchdog

Checks a list of HTTP endpoints and TCP ports — status codes, response
latency, TLS certificate expiry, plain connectivity — in one parallel pass,
and alerts to Slack/Discord/webhook/email **only when something changes
state**: one message when an endpoint goes down, one when it recovers,
silence in between.

## Why

Full uptime monitoring (Prometheus + Blackbox, Uptime Kuma, a SaaS pinger)
is the right answer at scale — and overkill for three services on a VPS or
a homelab. A cron line running this script gives you the useful 80%:
you find out *when* something breaks and *when* the certificate will expire,
instead of finding out from a user. And because alerting is keyed to state
transitions, a service that's down for an hour produces two messages, not
sixty.

## Features

- **HTTP(S) checks** — expected status code (or default "anything below
  400"), response latency with a warn threshold, optional body substring
  match (e.g. `"status":"ok"` in a health endpoint)
- **TLS certificate expiry** — warns N days before it becomes an outage
  (default 21), fails when expired; checked automatically on `https://`
  endpoints
- **TCP checks** — plain connect test for databases, SMTP, anything not HTTP
- **Transition-based alerting** — state is tracked between runs; notifiers
  fire on `OK -> FAIL` and `FAIL -> OK`, not on every cron tick while
  something stays broken. Recovery messages carry the severity of the state
  they recover from, so whoever was paged for the outage also hears the
  all-clear
- Same notifier types and config schema as
  [log-tailer-alert](../log-tailer-alert): Slack, Discord, generic JSON
  webhook, email — with `min_severity` floors and `${ENV_VAR}` secrets
- Checks run in parallel (a hung endpoint doesn't serialize the run)
- `table` (colored on a TTY) or `json` output; exit code reflects the worst
  finding, so the bare script already works as a cron/CI check
- Quick mode: `--url` needs no config file at all
- Zero dependencies — Python 3 standard library only

## Requirements

- Python 3.10+
- No `pip install` needed

## Quick start

```bash
chmod +x watchdog.py

# One-off check, no config needed
./watchdog.py --url https://example.com --url https://api.example.com/healthz

# Full config with thresholds and notifiers
./watchdog.py --config config.json

# See what would be alerted without sending anything
./watchdog.py --config config.json --dry-run
```

## Sample output

```
STATUS  NAME         LATENCY  MESSAGE
------  -----------  -------  -------------------------------------------
FAIL    api-health   2013ms   request failed: <urlopen error timed out>
WARN    homepage     310ms    certificate expires in 12d
OK      postgres     4ms      connected in 4ms

3 endpoint(s): 1 ok, 1 warn, 1 fail
```

And on a state change (to stderr, and to the configured notifiers):

```
[OK -> FAIL] api-health: request failed: <urlopen error timed out>
[FAIL -> OK] api-health: HTTP 200 in 121ms (was fail for 47m)
```

## Config format

```json
{
  "defaults": {
    "timeout_seconds": 10,
    "warn_latency_ms": 2000,
    "cert_warn_days": 21
  },
  "endpoints": [
    { "name": "homepage",   "url": "https://example.com/", "expect_status": 200 },
    { "name": "api-health", "url": "https://api.example.com/healthz",
      "expect_status": 200, "expect_body": "\"status\":\"ok\"", "warn_latency_ms": 500 },
    { "name": "postgres",   "check": "tcp", "host": "db1.internal", "port": 5432 }
  ],
  "notifiers": {
    "slack":   { "min_severity": "high", "webhook_url": "${SLACK_WEBHOOK_URL}" },
    "webhook": { "url": "https://example.com/hooks/endpoint-watchdog" }
  }
}
```

A full working example is included as `example.config.json`.

### Endpoint fields

| Field | Meaning |
|---|---|
| `name` | Identifier in output and alerts (default: the URL / `host:port`). Must be unique — state tracking is keyed by it |
| `check` | `http` (default) or `tcp` |
| `url` | Required for `http` checks |
| `host`, `port` | Required for `tcp` checks |
| `method` | HTTP method (default: `GET`; use `HEAD` for heavy pages) |
| `expect_status` | Integer or list (e.g. `[200, 301]`). Default: any status below 400 passes |
| `expect_body` | Substring that must appear in the response body (first 64 KiB) |
| `timeout_seconds` | Per-check timeout (default: 10) |
| `warn_latency_ms` | Warn when the response takes longer (default: 2000) |
| `cert_warn_days` | Warn when the TLS cert expires within N days (default: 21; `0` disables) |
| `verify_tls` | Set `false` for self-signed certs — disables verification **and** the expiry check |

Everything except `url`/`host`/`port` can also be set once under
`"defaults"` and overridden per endpoint.

### Notifiers

Same schema as log-tailer-alert: keyed by type (`slack`, `discord`,
`webhook`, `email`), each with `enabled` (default `true`) and an optional
`min_severity` floor (`low`/`medium`/`high`). Status maps to severity as
fail → high, warn → medium, ok → low; a recovery uses the severity of the
state it recovers from. Secrets belong in `${ENV_VAR}` references —
disabled notifiers don't need their variables set.

The generic `webhook` notifier POSTs JSON:

```json
{"name": "api-health", "target": "https://api.example.com/healthz",
 "status": "fail", "previous_status": "ok",
 "message": "HTTP 500", "latency_ms": 87.3}
```

### Options

| Flag | Description |
|---|---|
| `--config FILE` | JSON config (endpoints, defaults, notifiers) |
| `--url URL` | Check this URL with default settings (repeatable; works without a config) |
| `--output {table,json}` | Output format (default: table) |
| `--fail-on {fail,warn,none}` | Exit 1 if any check is at/above this level (default: `fail`) |
| `--timeout SEC` | Override the default per-check timeout |
| `--cert-warn-days N` | Override the certificate warning threshold |
| `--state-file FILE` | Where transition state lives (default: `~/.cache/endpoint-watchdog/state.json`) |
| `--no-state` | Report only — no state tracking, no transition alerts |
| `--dry-run` | Print transition alerts but send nothing |

### Exit codes

- `0` — everything at/below the `--fail-on` threshold
- `1` — at least one check at/above the threshold (something is down)
- `2` — bad usage or config error

## Running on a schedule

```cron
# Every 2 minutes; alerts fire on state changes only
*/2 * * * * SLACK_WEBHOOK_URL=... /usr/bin/python3 /opt/scripts/watchdog.py --config /etc/endpoint-watchdog/config.json --output json >> /var/log/endpoint-watchdog.log 2>&1
```

The first run records a baseline: healthy endpoints stay silent, an
endpoint that is *already broken* on first sight does alert. After that,
each run compares against the saved state and only speaks up on changes.
Removing an endpoint from the config simply stops updating its state entry.

For ad-hoc watching during an incident, skip cron entirely:

```bash
watch -n 30 ./watchdog.py --config config.json --no-state
```

## Behavior notes & limitations

- The certificate check opens its own TLS connection (it isn't derived from
  the HTTP request), and is skipped when the endpoint already failed, when
  `verify_tls` is `false`, or when `cert_warn_days` is `0`.
- Redirects are followed (Python's default); `expect_status` applies to the
  final response. Add the redirect codes to `expect_status` if you want to
  assert on them instead.
- This is a poller, not a prober mesh: it tells you the endpoint is down
  *from where it runs*. For "is it down for everyone", you still want an
  external check.

## License

MIT — see the top-level [LICENSE](../LICENSE) in this repo.
