# db-backup-rotate

Dumps a PostgreSQL or MySQL/MariaDB database, streams it straight into a
gzip file, **verifies the backup** (up to a full restore into a scratch
database), applies grandfather-father-son retention to old backups, and
optionally uploads the new one to S3. One cron line, the whole chore.

## Why

Every team has a backup script. Far fewer have ever restored from one — and
a backup that has never been restored is a hope, not a backup. The
differentiator here is `--verify restore`: after dumping, the script loads
the dump into a throwaway database, counts the tables that arrived, and
drops it. If the restore fails, the run exits non-zero and your cron
monitoring hears about it *now*, not on the day you actually need the file.

The rest is the plumbing everyone rewrites: safe compression without an
uncompressed intermediate, retention that keeps dailies/weeklies/monthlies
instead of a dumb "delete older than N", and an S3 push for off-box copies.

## Features

- **PostgreSQL** (`pg_dump`) and **MySQL/MariaDB** (`mysqldump`, with
  `--single-transaction` for a consistent InnoDB snapshot)
- Dump is streamed through gzip — no temporary uncompressed file, and the
  final filename only ever appears once the dump completed (a failed dump
  leaves nothing that looks like a valid backup)
- **Three verification levels**: `none`, `gzip` (integrity read of the whole
  file, the default), and `restore` (full load into a scratch database that
  is dropped afterwards)
- **GFS retention** — keep the newest backup of each of the last N days,
  N ISO weeks, and N months (`--keep-daily/--keep-weekly/--keep-monthly`)
- Rotation only ever deletes files matching this tool's own naming pattern
  (`<prefix>-YYYYmmdd-HHMMSS.sql.gz`) — anything else in the directory is
  never touched
- Optional **S3 upload** via the `aws` CLI (`--s3-uri s3://bucket/prefix`)
- `--dry-run` shows the dump command and rotation deletions without doing
  anything; `--json` prints a machine-readable summary
- Distinct exit codes so cron/CI can tell "backup failed" from "backup
  succeeded but did not verify"
- No Python dependencies — standard library only

## Requirements

- Python 3.10+
- Engine client tools on PATH: `pg_dump` + `psql`, or `mysqldump` + `mysql`
  (the second tool is only needed for `--verify restore`)
- `aws` CLI, only if using `--s3-uri`

## Usage

```bash
chmod +x db-backup.py

# Nightly: dump, gzip-verify, rotate with the default 7d/4w/6m retention
./db-backup.py --engine postgres --database shop --backup-dir /var/backups/db

# The full treatment: restore-check the dump, then push it to S3
./db-backup.py --engine postgres --database shop --backup-dir /var/backups/db \
    --verify restore --s3-uri s3://my-backups/shop

# MySQL, remote host, custom retention
./db-backup.py --engine mysql --database shop --host db1 --user backup \
    --backup-dir /var/backups/db --keep-daily 14 --keep-weekly 8 --keep-monthly 12

# Preview what rotation would delete, touching nothing
./db-backup.py --engine postgres --database shop --backup-dir /var/backups/db \
    --rotate-only --dry-run
```

### Options

| Flag | Description |
|---|---|
| `--engine {postgres,mysql}` | Which dump/restore tools to use (required) |
| `--database NAME` | Database to back up (required) |
| `--host`, `--port`, `--user` | Connection details (defaults: engine defaults / local socket) |
| `--backup-dir DIR` | Where backups live (required; created if missing) |
| `--prefix NAME` | Backup filename prefix (default: the database name) |
| `--keep-daily N` | Keep the newest backup of each of the last N days (default: 7) |
| `--keep-weekly N` | Keep the newest backup of each of the last N ISO weeks (default: 4) |
| `--keep-monthly N` | Keep the newest backup of each of the last N months (default: 6) |
| `--no-rotate` | Skip rotation entirely |
| `--rotate-only` | Only apply retention to existing backups; don't dump |
| `--verify {none,gzip,restore}` | How hard to check the new backup (default: `gzip`) |
| `--maintenance-db DB` | PostgreSQL database used for `CREATE/DROP DATABASE` during the restore check (default: `postgres`) |
| `--s3-uri s3://BUCKET/PREFIX` | Also upload the new backup here via the aws CLI |
| `--extra-arg ARG` | Extra argument passed through to `pg_dump`/`mysqldump` (repeatable, e.g. `--extra-arg --no-owner`) |
| `--dry-run` | Show what would happen, change nothing |
| `--json` | Print a JSON summary to stdout (human logs go to stderr) |

### Exit codes

- `0` — backup written, verified, rotated, uploaded
- `1` — dump, rotation, or upload failed
- `2` — **verification failed** — the backup file exists but is suspect; treat the run as failed
- `3` — bad usage / preflight failure (missing tools, bad arguments)

## How the restore check works

With `--verify restore`, after the dump completes the script:

1. Creates a scratch database named `<db>_restorecheck_<timestamp>`
   (PostgreSQL: via the `--maintenance-db`; MySQL: directly)
2. Streams the gunzipped dump into it with errors set to fail fast
   (`psql -v ON_ERROR_STOP=1` / `mysql`)
3. Counts the user tables that arrived — zero tables fails the check
4. Drops the scratch database (also on failure)

The account used therefore needs `CREATEDB` (PostgreSQL) or `CREATE`
privileges (MySQL) for this mode. The scratch database briefly consumes
roughly the same disk/IO as the real one — schedule accordingly on big
databases, or stick with the default `gzip` verification there and do
restore checks on a replica or staging host.

## Credentials

Passwords are deliberately **not accepted as flags** — they would be visible
in `ps` output and shell history. Use the engine's standard mechanisms:

```bash
# PostgreSQL: environment variable or ~/.pgpass
PGPASSWORD=... ./db-backup.py --engine postgres ...

# MySQL: environment variable or ~/.my.cnf [client] section
MYSQL_PWD=... ./db-backup.py --engine mysql ...
```

For cron, prefer `~/.pgpass` / `~/.my.cnf` (mode 600) over environment
variables in the crontab.

## Retention, explained

`--keep-daily 7 --keep-weekly 4 --keep-monthly 6` (the default) means: the
newest backup of each of the last 7 calendar days, plus the newest of each
of the last 4 ISO weeks, plus the newest of each of the last 6 months — one
file can satisfy several rules at once. With nightly backups that settles
at roughly 13–15 files. The newest backup is always kept, regardless of
settings, and files not created by this tool are never deleted.

## Running from cron

```cron
# Nightly at 02:30, full restore check, off-box copy, log the summary
30 2 * * * /usr/bin/python3 /opt/scripts/db-backup.py --engine postgres --database shop --backup-dir /var/backups/db --verify restore --s3-uri s3://my-backups/shop --json >> /var/log/db-backup.log 2>&1
```

Runs are not designed to overlap — schedule one at a time per database
(at nightly cadence this is a non-issue).

## CI

The repo's CI runs this tool end-to-end against a real PostgreSQL 16
service container on every push: seed a database, back it up with
`--verify restore`, validate the JSON summary, and exercise rotation.
See [.github/workflows/ci.yml](../.github/workflows/ci.yml).

## License

MIT — see the top-level [LICENSE](../LICENSE) in this repo.
