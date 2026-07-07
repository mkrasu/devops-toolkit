#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""db-backup.py — dump a database, compress, rotate old backups, and verify.

One cron-friendly pass that does the whole backup chore properly:

  1. Dump (PostgreSQL via pg_dump, MySQL/MariaDB via mysqldump), streamed
     straight into a gzip file — no uncompressed intermediate on disk
  2. Verify the backup: at minimum a gzip integrity read, or (recommended)
     a full restore into a throwaway scratch database that is dropped after
  3. Rotate old backups with grandfather-father-son retention
     (keep N daily, N weekly, N monthly)
  4. Optionally upload the new backup to S3 (via the aws CLI)

A backup that has never been restored is a hope, not a backup — hence the
--verify restore mode, which proves the dump actually loads.

No external Python dependencies — standard library only, plus the client
tools for your engine (pg_dump/psql or mysqldump/mysql) and, if uploading,
the aws CLI.

Usage:
    python3 db-backup.py --engine postgres --database mydb --backup-dir /var/backups/db [OPTIONS]

Examples:
    # Nightly: dump, gzip-verify, rotate with the default 7d/4w/6m retention
    python3 db-backup.py --engine postgres --database shop --backup-dir /var/backups/db

    # The full treatment: restore-check the dump, then push it to S3
    python3 db-backup.py --engine postgres --database shop --backup-dir /var/backups/db \
        --verify restore --s3-uri s3://my-backups/shop

    # MySQL, remote host, custom retention
    python3 db-backup.py --engine mysql --database shop --host db1 --user backup \
        --backup-dir /var/backups/db --keep-daily 14 --keep-weekly 8 --keep-monthly 12

    # See what rotation would delete, without backing up or deleting anything
    python3 db-backup.py --engine postgres --database shop --backup-dir /var/backups/db \
        --rotate-only --dry-run

Credentials are never taken as flags (they would leak into `ps` output and
shell history). Use PGPASSWORD / ~/.pgpass for PostgreSQL and MYSQL_PWD /
~/.my.cnf for MySQL.

Exit codes:
    0  success
    1  dump, rotation, or upload failed
    2  verification failed (the backup file is suspect — do not trust it)
    3  bad usage / preflight failure (missing tools, bad arguments)
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

TIMESTAMP_FMT = "%Y%m%d-%H%M%S"


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def die(msg: str, code: int = 3) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(code)


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{n}B"


# ---------------------------------------------------------------------------
# Backup file naming + retention selection (pure functions, unit-tested)
# ---------------------------------------------------------------------------

def backup_filename(prefix: str, when: datetime) -> str:
    return f"{prefix}-{when.strftime(TIMESTAMP_FMT)}.sql.gz"


def parse_backup_time(prefix: str, filename: str) -> datetime | None:
    """Return the timestamp encoded in `filename`, or None if the file does
    not look like a backup made by this tool for this prefix. Rotation only
    ever deletes files this function recognizes — anything else in the
    backup directory is left alone."""
    m = re.fullmatch(re.escape(prefix) + r"-(\d{8}-\d{6})\.sql\.gz", filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), TIMESTAMP_FMT)
    except ValueError:
        return None


def select_backups_to_keep(
    times: list[datetime],
    keep_daily: int,
    keep_weekly: int,
    keep_monthly: int,
) -> set[datetime]:
    """Grandfather-father-son retention: keep the newest backup of each of
    the most recent `keep_daily` days, `keep_weekly` ISO weeks, and
    `keep_monthly` months. The newest backup overall is always kept."""
    newest_first = sorted(times, reverse=True)
    keep: set[datetime] = set()
    if newest_first:
        keep.add(newest_first[0])

    def newest_per_bucket(key, count: int) -> list[datetime]:
        buckets: dict = {}
        for t in newest_first:  # first hit per bucket is the newest in it
            buckets.setdefault(key(t), t)
        return [t for _, t in sorted(buckets.items(), reverse=True)[:count]]

    keep.update(newest_per_bucket(lambda t: t.date(), keep_daily))
    keep.update(newest_per_bucket(lambda t: t.isocalendar()[:2], keep_weekly))
    keep.update(newest_per_bucket(lambda t: (t.year, t.month), keep_monthly))
    return keep


def s3_destination(s3_uri: str, filename: str) -> str:
    return s3_uri.rstrip("/") + "/" + filename


# ---------------------------------------------------------------------------
# Engine-specific commands
# ---------------------------------------------------------------------------

def conn_args(args: argparse.Namespace) -> list[str]:
    out: list[str] = []
    if args.host:
        out += ["-h", args.host]
    if args.port:
        out += ["-P" if args.engine == "mysql" else "-p", str(args.port)]
    if args.user:
        out += ["-u" if args.engine == "mysql" else "-U", args.user]
    return out


def build_dump_cmd(args: argparse.Namespace) -> list[str]:
    if args.engine == "postgres":
        return ["pg_dump", *conn_args(args), *args.extra_arg, args.database]
    return [
        "mysqldump", "--single-transaction", "--routines", "--triggers",
        *conn_args(args), *args.extra_arg, args.database,
    ]


def client_tool(engine: str) -> str:
    return "psql" if engine == "postgres" else "mysql"


def run_sql(args: argparse.Namespace, database: str, sql: str) -> str:
    """Run one SQL statement through the engine's client, return stdout."""
    if args.engine == "postgres":
        cmd = ["psql", *conn_args(args), "-d", database, "-t", "-A",
               "-v", "ON_ERROR_STOP=1", "-c", sql]
    else:
        cmd = ["mysql", *conn_args(args), "-N", "-e", sql, database]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"'{cmd[0]}' exited {proc.returncode}")
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# Dump
# ---------------------------------------------------------------------------

def do_dump(args: argparse.Namespace, dest_path: str) -> int:
    """Stream the dump command's stdout into a gzip file. Returns the
    compressed size in bytes. Writes to a .partial file first so a failed
    dump never leaves something that looks like a valid backup."""
    cmd = build_dump_cmd(args)
    partial = dest_path + ".partial"
    log(f"-> Dumping with: {' '.join(cmd)}")
    # stderr goes to a temp file: reading two pipes from one process without
    # threads can deadlock once a pipe buffer fills.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf)
        except FileNotFoundError:
            die(f"'{cmd[0]}' is not installed or not on PATH.")
        try:
            with gzip.open(partial, "wb", compresslevel=6) as gz:
                shutil.copyfileobj(proc.stdout, gz, length=1024 * 1024)
        except OSError as e:
            proc.kill()
            proc.wait()
            if os.path.exists(partial):
                os.unlink(partial)
            die(f"could not write backup file: {e}", 1)
        finally:
            proc.stdout.close()
            rc = proc.wait()
        errf.seek(0)
        stderr = errf.read().strip()

    if rc != 0:
        os.unlink(partial)
        log(stderr or "(no error output)")
        die(f"dump command exited with code {rc}; partial file removed.", 1)
    if stderr:
        log(f"   dump warnings:\n{stderr}")

    os.replace(partial, dest_path)  # atomic: the final name only ever holds a complete dump
    return os.path.getsize(dest_path)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_gzip(path: str) -> None:
    """Read the whole file through gzip so the CRC is checked; raises on
    corruption. Also rejects a suspiciously empty dump."""
    total = 0
    with gzip.open(path, "rb") as gz:
        while True:
            chunk = gz.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
    if total == 0:
        raise RuntimeError("backup decompresses to 0 bytes — the dump produced nothing")


def verify_restore(args: argparse.Namespace, path: str, when: datetime) -> int:
    """Restore the dump into a scratch database, count the user tables that
    arrived, then drop the scratch database. Returns the table count."""
    scratch = f"{args.database}_restorecheck_{when.strftime('%Y%m%d%H%M%S')}"
    if args.engine == "postgres":
        quoted = f'"{scratch}"'
        admin_db = args.maintenance_db
        count_sql = ("SELECT count(*) FROM information_schema.tables "
                     "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')")
        restore_cmd = ["psql", *conn_args(args), "-d", scratch, "-q", "-v", "ON_ERROR_STOP=1"]
    else:
        quoted = f"`{scratch}`"
        admin_db = "information_schema"
        count_sql = ("SELECT COUNT(*) FROM information_schema.tables "
                     f"WHERE table_schema = '{scratch}'")
        restore_cmd = ["mysql", *conn_args(args), scratch]

    log(f"-> Restore check: loading backup into scratch database '{scratch}'...")
    run_sql(args, admin_db, f"CREATE DATABASE {quoted}")
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
            proc = subprocess.Popen(
                restore_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errf,
            )
            try:
                with gzip.open(path, "rb") as gz:
                    shutil.copyfileobj(gz, proc.stdin, length=1024 * 1024)
                proc.stdin.close()
            except BrokenPipeError:
                pass  # restore died early; the exit code below tells the story
            rc = proc.wait()
            errf.seek(0)
            stderr = errf.read().strip()
        if rc != 0:
            raise RuntimeError(f"restore exited {rc}: {stderr or '(no error output)'}")

        tables = int(run_sql(args, scratch if args.engine == "postgres" else admin_db, count_sql) or 0)
        if tables < 1:
            raise RuntimeError("dump restored cleanly but contains no tables")
        return tables
    finally:
        try:
            run_sql(args, admin_db, f"DROP DATABASE {quoted}")
        except RuntimeError as e:
            log(f"Warning: could not drop scratch database '{scratch}': {e}")


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def rotate(args: argparse.Namespace) -> tuple[list[str], int]:
    """Apply retention to the backup directory. Returns (deleted filenames,
    kept count). Only files matching this tool's naming pattern for this
    prefix are ever considered."""
    entries: dict[datetime, str] = {}
    for name in os.listdir(args.backup_dir):
        when = parse_backup_time(args.prefix, name)
        if when is not None:
            entries[when] = name

    keep = select_backups_to_keep(
        list(entries), args.keep_daily, args.keep_weekly, args.keep_monthly,
    )
    doomed = sorted(t for t in entries if t not in keep)

    deleted = []
    for when in doomed:
        name = entries[when]
        if args.dry_run:
            log(f"   [dry-run] would delete {name}")
        else:
            os.unlink(os.path.join(args.backup_dir, name))
            log(f"   deleted {name}")
        deleted.append(name)
    return deleted, len(keep)


# ---------------------------------------------------------------------------
# S3 upload
# ---------------------------------------------------------------------------

def upload_s3(args: argparse.Namespace, path: str) -> str:
    dest = s3_destination(args.s3_uri, os.path.basename(path))
    cmd = ["aws", "s3", "cp", "--only-show-errors", path, dest]
    log(f"-> Uploading to {dest}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        die("--s3-uri given but the 'aws' CLI is not installed or not on PATH.")
    if proc.returncode != 0:
        log(proc.stderr.strip())
        die(f"S3 upload failed (exit {proc.returncode}). The local backup is intact.", 1)
    return dest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Dump a database, compress, verify, rotate, and optionally upload to S3.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--engine", required=True, choices=["postgres", "mysql"])
    p.add_argument("--database", required=True, help="Database to back up")
    p.add_argument("--host", help="Database host (default: engine's default / local socket)")
    p.add_argument("--port", type=int, help="Database port")
    p.add_argument("--user", help="Database user (password via PGPASSWORD/MYSQL_PWD, never a flag)")
    p.add_argument("--backup-dir", required=True, help="Directory where backups live")
    p.add_argument("--prefix", help="Backup filename prefix (default: the database name)")
    p.add_argument("--keep-daily", type=int, default=7, metavar="N",
                   help="Keep the newest backup of each of the last N days (default: 7)")
    p.add_argument("--keep-weekly", type=int, default=4, metavar="N",
                   help="Keep the newest backup of each of the last N ISO weeks (default: 4)")
    p.add_argument("--keep-monthly", type=int, default=6, metavar="N",
                   help="Keep the newest backup of each of the last N months (default: 6)")
    p.add_argument("--no-rotate", action="store_true", help="Skip rotation entirely")
    p.add_argument("--rotate-only", action="store_true",
                   help="Only apply retention to existing backups; don't dump")
    p.add_argument("--verify", choices=["none", "gzip", "restore"], default="gzip",
                   help="How hard to check the new backup: gzip integrity (default) "
                        "or a full restore into a scratch database")
    p.add_argument("--maintenance-db", default="postgres",
                   help="PostgreSQL database to connect to for CREATE/DROP DATABASE "
                        "during --verify restore (default: postgres)")
    p.add_argument("--s3-uri", metavar="s3://BUCKET/PREFIX",
                   help="Also upload the new backup here via the aws CLI")
    p.add_argument("--extra-arg", action="append", default=[], metavar="ARG",
                   help="Extra argument passed through to pg_dump/mysqldump (repeatable)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would happen (dump command, rotation deletions) without doing it")
    p.add_argument("--json", action="store_true", dest="json_out",
                   help="Print a machine-readable JSON summary to stdout")
    args = p.parse_args(argv)
    if not args.prefix:
        args.prefix = args.database
    for flag in ("keep_daily", "keep_weekly", "keep_monthly"):
        if getattr(args, flag) < 0:
            p.error(f"--{flag.replace('_', '-')} must be >= 0")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    started = time.time()
    now = datetime.now()

    if not args.dry_run:
        os.makedirs(args.backup_dir, exist_ok=True)
    elif not os.path.isdir(args.backup_dir):
        die(f"--backup-dir does not exist: {args.backup_dir} (dry run creates nothing)")

    summary: dict = {
        "engine": args.engine,
        "database": args.database,
        "dry_run": args.dry_run,
        "backup": None,
        "verify": {"mode": args.verify, "ok": None},
        "rotation": {"deleted": [], "kept": None},
        "s3": None,
    }

    # 1. Dump
    dest_path = None
    if not args.rotate_only:
        filename = backup_filename(args.prefix, now)
        dest_path = os.path.join(args.backup_dir, filename)
        if os.path.exists(dest_path):
            die(f"refusing to overwrite existing backup: {dest_path}", 1)
        if args.dry_run:
            log(f"[dry-run] Would dump with: {' '.join(build_dump_cmd(args))}")
            log(f"[dry-run] Would write: {dest_path}")
        else:
            size = do_dump(args, dest_path)
            log(f"   wrote {filename} ({human_bytes(size)})")
            summary["backup"] = {"file": dest_path, "size_bytes": size}

    # 2. Verify
    if dest_path and not args.dry_run and args.verify != "none":
        try:
            if args.verify == "restore":
                tables = verify_restore(args, dest_path, now)
                log(f"   restore check passed: {tables} table(s) restored and dropped")
            else:
                verify_gzip(dest_path)
                log("   gzip integrity check passed")
            summary["verify"]["ok"] = True
        except (RuntimeError, OSError, EOFError) as e:
            summary["verify"]["ok"] = False
            if args.json_out:
                print(json.dumps(summary, indent=2))
            die(f"verification failed: {e}", 2)

    # 3. Rotate
    if not args.no_rotate:
        log("-> Applying retention "
            f"({args.keep_daily} daily / {args.keep_weekly} weekly / {args.keep_monthly} monthly)...")
        deleted, kept = rotate(args)
        summary["rotation"] = {"deleted": deleted, "kept": kept}
        log(f"   {kept} backup(s) kept, {len(deleted)} deleted"
            + (" (dry run — nothing actually deleted)" if args.dry_run and deleted else ""))

    # 4. Upload
    if args.s3_uri and dest_path:
        if args.dry_run:
            log(f"[dry-run] Would upload to {s3_destination(args.s3_uri, os.path.basename(dest_path))}")
        else:
            summary["s3"] = upload_s3(args, dest_path)

    log(f"Done in {time.time() - started:.1f}s.")
    if args.json_out:
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
