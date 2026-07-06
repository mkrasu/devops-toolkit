# docker-cleanup

A safe, configurable Bash script for pruning unused Docker resources
(stopped containers, dangling/unused images, unused networks, volumes,
and build cache) — with dry-run mode, age-based filtering, and logging.

## Why

Docker installations silently accumulate disk usage over time: stopped
containers from failed test runs, dangling image layers from every build,
orphaned networks, and gigabytes of build cache. `docker system prune -a`
solves this but is blunt — it doesn't let you scope by age, preview
changes safely, or log what happened for auditing. This script fills that
gap for local dev machines, CI runners, and small self-hosted servers.

## Features

- **Dry-run mode** — lists the actual candidate resources (stopped containers,
  dangling images, unused networks/volumes, build cache) before you commit
- **Age-based filtering** — only touch resources older than N days (default: 7)
- **Opt-in destructive actions** — volume and full image pruning require
  explicit flags, since they can delete real data
- **Confirmation prompt** — unless `--yes` is passed
- **Reclaimed-space summary** — reports roughly how much disk each run freed
- **JSON output** (`--json`) for piping the summary into other tooling
- **Before/after disk usage report** via `docker system df`
- **Optional logging** to a file (e.g. for cron jobs)

> On the dry run: Docker's prune age filter (`until=`) is a *relative* age that
> `docker ... ls` can't reproduce exactly, so the preview shows the current
> candidates of each type; the real run additionally drops anything newer than
> the threshold. It's an honest preview of what kind of thing goes, not a
> byte-exact promise.

## Requirements

- Bash 4+
- Docker Engine with CLI access (`docker info` must succeed)
- `numfmt` (usually preinstalled on Linux; used only for byte formatting)

## Usage

```bash
chmod +x docker-cleanup.sh

# Preview what would be cleaned, touching nothing
./docker-cleanup.sh --dry-run

# Clean containers/images/networks older than 14 days, no prompt
./docker-cleanup.sh --days 14 --yes

# Full cleanup including volumes, images, and build cache, with a log file
./docker-cleanup.sh --all --yes --log /var/log/docker-cleanup.log

# Preview as JSON, e.g. to feed a dashboard or another script
./docker-cleanup.sh --dry-run --json
```

### Options

| Flag | Description |
|---|---|
| `-d, --days N` | Only remove resources older than N days (default: 7) |
| `-n, --dry-run` | Show what would be removed without removing anything |
| `-y, --yes` | Skip the confirmation prompt |
| `-v, --volumes` | Also prune dangling volumes (**destructive**) |
| `-i, --images` | Also prune all unused images, not just dangling ones |
| `-b, --build-cache` | Also prune the builder cache |
| `-a, --all` | Shorthand for `-v -i -b` |
| `-j, --json` | Print a machine-readable JSON summary (implies `--quiet`) |
| `-l, --log FILE` | Write a plain-text summary log to FILE |
| `-q, --quiet` | Suppress non-essential console output |
| `-h, --help` | Show usage |

## Running on a schedule

### Cron

```cron
# Run every Sunday at 3am, full cleanup, logged
0 3 * * 0 /opt/scripts/docker-cleanup.sh --all --yes --log /var/log/docker-cleanup.log
```

### systemd timer

`docker-cleanup.service`:
```ini
[Unit]
Description=Docker resource cleanup

[Service]
Type=oneshot
ExecStart=/opt/scripts/docker-cleanup.sh --all --yes --log /var/log/docker-cleanup.log
```

`docker-cleanup.timer`:
```ini
[Unit]
Description=Run docker-cleanup weekly

[Timer]
OnCalendar=Sun 03:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable with:
```bash
sudo systemctl enable --now docker-cleanup.timer
```

## Safety notes

- Volume pruning (`-v`) removes **dangling** volumes only (not attached to
  any container), but if you rely on ad-hoc `docker run -v` workflows
  without naming volumes, double-check with `docker volume ls` first.
- The age filter (`--days`) does **not** apply to volumes — `docker volume
  prune` has no age filter, so `-v` removes all dangling volumes regardless of
  age. The script prints a reminder when volume pruning runs.
- Always run `--dry-run` first on a machine you're unfamiliar with.
- This script never touches running containers or images backing them.

## License

MIT
