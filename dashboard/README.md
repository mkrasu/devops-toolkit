# dashboard

A read-only web dashboard over the toolkit: scheduled collectors run the
tools with JSON output, the dashboard renders red/green tiles per host and
tool with drill-down into findings and history. Open it in the morning,
see everything green (or not), get on with your day.

> **The dependency exception.** Every *tool* in this repo is standard
> library only. The dashboard is a separate deployable and uses FastAPI +
> Jinja2 (see `requirements.txt`) — it never has to run on the boxes being
> monitored, only wherever you host the UI.

## How it works

```
each monitored host                                  anywhere
─────────────────────────                            ────────────────────────
cron / systemd timer                                 docker container
  └─ collect.sh runs a tool ──► <data-dir>/<host>/<tool>/<ts>.json ──► dashboard (read-only)
```

- **Collectors** are just the tools you already run, wrapped in
  [collect.sh](./collect.sh), which stores each run's JSON as
  `<data-dir>/<hostname>/<tool>/<YYYYmmdd-HHMMSS>.json` and prunes old
  results.
- **The dashboard never executes anything.** It scans the data directory,
  derives a status per tile (`ok` / `warn` / `crit` / `unknown`), marks
  tiles **stale** when results stop arriving (so a dead cron looks broken,
  not green), and auto-refreshes via htmx every 60 s.
- Tools understood out of the box: `sys-triage`, `host-hardening-check`,
  `k8s-resource-auditor`, `endpoint-watchdog`, `db-backup-rotate`,
  `docker-cleanup`. Anything else that follows the
  `summary: {high/medium/low}` convention works automatically; unrecognized
  payloads render as raw JSON instead of failing.

## Quick start

```bash
cd dashboard

# 1. Start the dashboard (serves on localhost:8080, reads /var/lib/devops-dashboard)
docker compose up -d

# 2. On each monitored host, schedule collectors — e.g. in crontab:
*/5 * * * *  /opt/devops-toolkit/dashboard/collect.sh /var/lib/devops-dashboard endpoint-watchdog -- python3 /opt/devops-toolkit/endpoint-watchdog/watchdog.py --config /etc/endpoint-watchdog/config.json --output json
0 * * * *    /opt/devops-toolkit/dashboard/collect.sh /var/lib/devops-dashboard sys-triage -- python3 /opt/devops-toolkit/sys-triage/triage.py --output json
0 6 * * *    /opt/devops-toolkit/dashboard/collect.sh /var/lib/devops-dashboard host-hardening-check -- sudo python3 /opt/devops-toolkit/host-hardening-check/hardening-check.py --output json
30 2 * * *   /opt/devops-toolkit/dashboard/collect.sh /var/lib/devops-dashboard db-backup-rotate -- python3 /opt/devops-toolkit/db-backup-rotate/db-backup.py --engine postgres --database shop --backup-dir /var/backups/db --verify restore --json
```

Then open <http://localhost:8080>.

Without Docker it's a plain ASGI app:

```bash
pip install -r requirements.txt
DASHBOARD_DATA_DIR=/var/lib/devops-dashboard uvicorn app:app --port 8080
```

### One host vs. many

In phase 1 the dashboard reads a local directory, so:

- **Single host**: run the container on the same box; done.
- **Multiple hosts**: ship each host's results into the dashboard's data
  directory with whatever you already trust — an NFS mount, `rsync` over
  ssh after each collect, or a shared volume. Results are per-host
  subdirectories, so they merge cleanly. (A push-over-HTTP ingest API with
  tokens is the planned phase 2.)

## Configuration

| Setting | How | Default |
|---|---|---|
| Data directory | `DASHBOARD_DATA_DIR` env var | `/data` (the compose file mounts `/var/lib/devops-dashboard` there) |
| Results kept per tool | `collect.sh --keep N` | 200 |
| Staleness budget | per tool in `store.py` (`STALE_AFTER`) | 15 min for endpoint-watchdog, 26 h otherwise |

## Endpoints

| Path | What |
|---|---|
| `/` | The tile grid, auto-refreshing |
| `/host/{host}/{tool}` | Latest result, status history, raw JSON |
| `/api/v1/summary` | The whole grid as JSON (for scripting/alerting) |
| `/healthz` | Container health check |

## Security posture

- The dashboard is **read-only by design** — there is deliberately no "run
  tool" button. A web process that executes host commands (some of which
  want root) is an attack surface this project doesn't need; the tools'
  natural cadence suits scheduled runs.
- The container runs as a non-root user with the data volume mounted `:ro`.
- The compose file binds to `127.0.0.1` — expose it through a reverse proxy
  with auth, or keep it on a VPN/Tailscale network. Results contain
  hostnames, ports, and finding details you don't want public.

## Testing

- `tests/test_dashboard_store.py` — the scanning/status logic; stdlib-only,
  runs with the repo's normal suite.
- `tests/test_dashboard_app.py` — FastAPI routes via TestClient; skips
  itself unless the web dependencies are installed, so the toolkit's
  dependency-free workflow is unaffected. CI runs it with deps installed
  and also builds and smoke-tests the Docker image.

## Roadmap

- **Phase 2**: HTTP ingest endpoint with per-host bearer tokens (no shared
  filesystem needed), SQLite history, trend charts (endpoint latency, disk
  creep, finding counts over time).
- **Phase 3 (maybe)**: gated "run now" via an allowlisted job queue — only
  if scheduled runs ever feel insufficient.

## License

MIT — see the top-level [LICENSE](../LICENSE) in this repo.
