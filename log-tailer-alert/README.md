# log-tailer-alert

Tails log files (or stdin), matches configurable regex patterns, and fires
alerts to Slack, Discord, a generic webhook, or email when a pattern
matches too many times in too short a window — with a cooldown so a noisy
pattern doesn't spam you every second.

## Why

`grep`-ing logs after something breaks tells you what already happened.
This watches continuously and tells you *while* it's happening — configurable
per pattern, so a single 500 error doesn't page anyone but five in a minute
does. It's the small building block behind most "alert on log pattern"
setups, without needing to stand up a full log pipeline (ELK/Loki/etc.) for
a single box or small project.

## Features

- Multiple regex patterns, each with its own severity, threshold, time
  window, and cooldown
- Tails multiple files at once, or stdin (`--file -`) so it composes with
  `journalctl -f | log-alert.py ...` or `kubectl logs -f ... | log-alert.py ...`
- Handles log rotation (detects truncation/inode change and reopens)
- Four notifier types: Slack webhook, Discord webhook, generic JSON
  webhook, email (SMTP) — mix and match, or add your own
- `--dry-run` — see what would alert without sending anything
- `--test FILE` — replay a historical log file against your patterns to
  validate them before deploying
- `--once` — process only what's new since the last run and exit, tracking each
  file's position (inode + offset) on disk so cron runs don't miss or re-read
  lines
- Per-notifier severity floor (`min_severity`) — e.g. page Slack on `high`
  only while email gets everything
- Notifier calls retry with backoff, so a transient webhook/SMTP blip doesn't
  silently drop an alert
- Secrets kept out of the config file via `${ENV_VAR}` substitution
- Zero dependencies — Python 3 standard library only

## Requirements

- Python 3.10+
- No `pip install` needed

## Quick start

```bash
chmod +x log-alert.py

# 1. Validate your patterns against a historical log first
python3 log-alert.py --config example.config.json --test /var/log/app.log.1

# 2. Then run it live, dry-run first to confirm behavior
python3 log-alert.py --config example.config.json --dry-run

# 3. Once you trust it, run for real
python3 log-alert.py --config example.config.json
```

## Config format

`config.json`:

```json
{
  "files": ["/var/log/app.log"],
  "patterns": [
    {
      "name": "http-5xx",
      "regex": "\\s5\\d{2}\\s",
      "severity": "high",
      "threshold": 5,
      "window_seconds": 60,
      "cooldown_seconds": 300
    },
    {
      "name": "oom-killer",
      "regex": "Out of memory|OOM",
      "severity": "high",
      "threshold": 1,
      "window_seconds": 60,
      "cooldown_seconds": 600
    }
  ],
  "notifiers": {
    "slack": {
      "enabled": true,
      "webhook_url": "${SLACK_WEBHOOK_URL}"
    },
    "email": {
      "enabled": false,
      "smtp_host": "smtp.example.com",
      "smtp_port": 587,
      "use_tls": true,
      "username": "${SMTP_USER}",
      "password": "${SMTP_PASS}",
      "from_addr": "alerts@example.com",
      "to_addrs": ["oncall@example.com"]
    }
  }
}
```

A full working example is included as `example.config.json`.

### Pattern fields

| Field | Meaning |
|---|---|
| `name` | Identifier shown in alerts |
| `regex` | Python regex, matched against each line with `re.search` |
| `severity` | `low` / `medium` / `high` — cosmetic, shown in output and alert text |
| `threshold` | How many matches within `window_seconds` before it fires |
| `window_seconds` | Rolling window for counting matches |
| `cooldown_seconds` | Minimum time between alerts for this pattern, once fired |

### Notifier fields

Each notifier is keyed by type (`slack`, `discord`, `webhook`, `email`) and
takes its own settings (webhook URL, SMTP details, etc.). Two options apply to
any notifier:

| Field | Meaning |
|---|---|
| `enabled` | Set `false` to keep the config but stop sending (default: `true`) |
| `min_severity` | Only send alerts at this severity or higher (`low`/`medium`/`high`). Omit to receive all. E.g. Slack `high`, email unset. |

Notifier calls (HTTP and SMTP) retry a couple of times with a short backoff, so
a transient failure logs a warning instead of silently dropping the alert.

### Secrets

Never put webhook URLs or SMTP passwords directly in the config file if
you're committing it anywhere. Reference an environment variable instead:

```json
"webhook_url": "${SLACK_WEBHOOK_URL}"
```

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
python3 log-alert.py --config config.json
```

If a referenced variable isn't set, the script exits immediately with a
clear error rather than silently sending to a broken URL.

## Modes

| Mode | Behavior |
|---|---|
| (default) | Follows file(s) live, like `tail -f`, alerting as thresholds trip |
| `--once` | Reads only lines added since the last `--once` run, then exits — good for cron. Tracks each file's position on disk. |
| `--test FILE` | Replays a whole file from the start, prints a summary, exits |
| `--dry-run` | Prints what would alert without calling any notifier |
| `--from-start` | Read existing content instead of only new lines (live mode, or the first `--once` run) |
| `--state-dir DIR` | Where `--once` stores per-file position state (default: `$XDG_CACHE_HOME/log-alert` or `~/.cache/log-alert`) |

## Running it continuously

### systemd service

```ini
[Unit]
Description=log-alert
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/scripts/log-alert.py --config /etc/log-alert/config.json
Restart=on-failure
Environment=SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

[Install]
WantedBy=multi-user.target
```

### Cron (--once mode)

```cron
* * * * * SLACK_WEBHOOK_URL=... /usr/bin/python3 /opt/scripts/log-alert.py --config /etc/log-alert/config.json --once >> /var/log/log-alert.log 2>&1
```

`--once` remembers where it stopped last time. On each run it seeks to the
saved offset for each file and processes only what's new, then records the new
position — so a plain append-only log works fine in cron without re-scanning
the whole file or missing lines between runs. It handles rotation too: if the
file's inode changes or it shrinks (truncation), the next run reads from the
top of the new file.

The first run on a file establishes a baseline at end-of-file and alerts on
nothing (add `--from-start` if you want that first run to scan existing
content). State lives under `~/.cache/log-alert/` by default; point it
elsewhere with `--state-dir` or a `"state_dir"` key in the config.

## Adding a custom notifier

Add a function with the signature `(cfg: dict, alert: Alert) -> None` and
register it in `NOTIFIER_DISPATCH` in `log-alert.py`. `Alert` has
`.pattern`, `.severity`, `.count`, `.window_seconds`, `.source`, `.samples`,
and a `.text()` convenience method.

## License

MIT — see the top-level [LICENSE](../LICENSE) in this repo.
