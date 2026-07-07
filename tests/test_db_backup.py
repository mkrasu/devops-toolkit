# SPDX-License-Identifier: MIT
"""Unit tests for db-backup-rotate/db-backup.py.

Covers the pure logic: backup filename round-tripping, GFS retention
selection, rotation safety (never touching foreign files, honoring
--dry-run), and gzip verification. The actual dump/restore path is
exercised against a real PostgreSQL in CI.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import gzip
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    path = Path(__file__).resolve().parent.parent / "db-backup-rotate" / "db-backup.py"
    spec = importlib.util.spec_from_file_location("db_backup", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


db = _load_module()


# ---------------------------------------------------------------------------
# Filename round-tripping
# ---------------------------------------------------------------------------

class FilenameTest(unittest.TestCase):
    def test_roundtrip(self):
        when = datetime(2026, 7, 7, 3, 15, 42)
        name = db.backup_filename("shop", when)
        self.assertEqual(name, "shop-20260707-031542.sql.gz")
        self.assertEqual(db.parse_backup_time("shop", name), when)

    def test_other_prefix_is_not_recognized(self):
        name = db.backup_filename("shop", datetime(2026, 7, 7, 3, 0, 0))
        self.assertIsNone(db.parse_backup_time("blog", name))

    def test_foreign_files_are_not_recognized(self):
        for name in ("README.md", "shop-latest.sql.gz", "shop-20260707.sql.gz",
                     "shop-20260707-031542.sql", "shop-20260707-031542.sql.gz.partial"):
            self.assertIsNone(db.parse_backup_time("shop", name), name)

    def test_prefix_with_regex_chars_is_literal(self):
        # 'my.db' must not match 'myxdb-...'
        name = db.backup_filename("myxdb", datetime(2026, 7, 7, 3, 0, 0))
        self.assertIsNone(db.parse_backup_time("my.db", name))

    def test_impossible_date_is_rejected(self):
        self.assertIsNone(db.parse_backup_time("shop", "shop-20261340-250000.sql.gz"))


# ---------------------------------------------------------------------------
# GFS retention selection
# ---------------------------------------------------------------------------

def daily_times(start: datetime, days: int, per_day: int = 1) -> list:
    """`days` consecutive days ending at `start`, `per_day` backups each."""
    out = []
    for d in range(days):
        for h in range(per_day):
            out.append(start - timedelta(days=d, hours=h))
    return out


class RetentionSelectionTest(unittest.TestCase):
    NOW = datetime(2026, 7, 7, 3, 0, 0)

    def test_daily_keeps_newest_of_each_recent_day(self):
        times = daily_times(self.NOW, days=10)
        keep = db.select_backups_to_keep(times, keep_daily=7, keep_weekly=0, keep_monthly=0)
        self.assertEqual(keep, set(daily_times(self.NOW, days=7)))

    def test_multiple_backups_per_day_keeps_only_newest(self):
        times = daily_times(self.NOW, days=3, per_day=4)
        keep = db.select_backups_to_keep(times, keep_daily=3, keep_weekly=0, keep_monthly=0)
        # One survivor per day, and it's the newest (hour offset 0) of that day.
        self.assertEqual(keep, {self.NOW - timedelta(days=d) for d in range(3)})

    def test_weekly_keeps_newest_per_iso_week(self):
        times = daily_times(self.NOW, days=21)  # spans 4 ISO weeks
        keep = db.select_backups_to_keep(times, keep_daily=0, keep_weekly=2, keep_monthly=0)
        weeks = {t.isocalendar()[:2] for t in keep}
        self.assertEqual(len(weeks), 2)
        for t in keep:
            same_week = [x for x in times if x.isocalendar()[:2] == t.isocalendar()[:2]]
            self.assertEqual(t, max(same_week))

    def test_monthly_keeps_newest_per_month(self):
        times = [datetime(2026, m, d, 12, 0, 0) for m in (4, 5, 6, 7) for d in (1, 15)]
        keep = db.select_backups_to_keep(times, keep_daily=0, keep_weekly=0, keep_monthly=2)
        self.assertEqual(keep, {datetime(2026, 7, 15, 12, 0, 0), datetime(2026, 6, 15, 12, 0, 0)})

    def test_newest_is_always_kept(self):
        times = daily_times(self.NOW, days=5)
        keep = db.select_backups_to_keep(times, keep_daily=0, keep_weekly=0, keep_monthly=0)
        self.assertEqual(keep, {self.NOW})

    def test_empty_input(self):
        self.assertEqual(db.select_backups_to_keep([], 7, 4, 6), set())


# ---------------------------------------------------------------------------
# Rotation against a real directory
# ---------------------------------------------------------------------------

class RotateTest(unittest.TestCase):
    NOW = datetime(2026, 7, 7, 3, 0, 0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _touch(self, name):
        Path(self.tmp.name, name).write_bytes(b"")

    def _args(self, **overrides):
        base = dict(backup_dir=self.tmp.name, prefix="shop",
                    keep_daily=2, keep_weekly=0, keep_monthly=0, dry_run=False)
        base.update(overrides)
        return SimpleNamespace(**base)

    def _seed(self, days):
        names = [db.backup_filename("shop", self.NOW - timedelta(days=d)) for d in range(days)]
        for n in names:
            self._touch(n)
        return names

    def test_deletes_only_beyond_retention(self):
        names = self._seed(days=4)
        deleted, kept = db.rotate(self._args())
        self.assertEqual(sorted(deleted), sorted(names[2:]))
        self.assertEqual(kept, 2)
        remaining = set(os.listdir(self.tmp.name))
        self.assertEqual(remaining, set(names[:2]))

    def test_foreign_files_survive(self):
        self._seed(days=4)
        for name in ("README.md", "blog-20260101-000000.sql.gz", "shop-latest.sql.gz"):
            self._touch(name)
        db.rotate(self._args())
        remaining = set(os.listdir(self.tmp.name))
        self.assertLessEqual(
            {"README.md", "blog-20260101-000000.sql.gz", "shop-latest.sql.gz"}, remaining,
        )

    def test_dry_run_deletes_nothing_but_reports(self):
        names = self._seed(days=4)
        deleted, _ = db.rotate(self._args(dry_run=True))
        self.assertEqual(sorted(deleted), sorted(names[2:]))
        self.assertEqual(set(os.listdir(self.tmp.name)), set(names))


# ---------------------------------------------------------------------------
# gzip verification
# ---------------------------------------------------------------------------

class VerifyGzipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write_gz(self, content: bytes) -> str:
        path = os.path.join(self.tmp.name, "b.sql.gz")
        with gzip.open(path, "wb") as gz:
            gz.write(content)
        return path

    def test_valid_file_passes(self):
        db.verify_gzip(self._write_gz(b"CREATE TABLE t (id int);\n"))

    def test_empty_dump_is_rejected(self):
        with self.assertRaises(RuntimeError):
            db.verify_gzip(self._write_gz(b""))

    def test_truncated_file_is_rejected(self):
        path = self._write_gz(b"CREATE TABLE t (id int);\n" * 10000)
        data = Path(path).read_bytes()
        Path(path).write_bytes(data[: len(data) // 2])
        with self.assertRaises((OSError, EOFError)):
            db.verify_gzip(path)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

class HelperTest(unittest.TestCase):
    def test_s3_destination_joins_cleanly(self):
        self.assertEqual(
            db.s3_destination("s3://bkt/db/", "shop-20260707-031542.sql.gz"),
            "s3://bkt/db/shop-20260707-031542.sql.gz",
        )
        self.assertEqual(
            db.s3_destination("s3://bkt/db", "x.sql.gz"), "s3://bkt/db/x.sql.gz",
        )

    def test_dump_cmd_postgres(self):
        args = SimpleNamespace(engine="postgres", host="db1", port=5433,
                               user="backup", extra_arg=["--no-owner"], database="shop")
        self.assertEqual(
            db.build_dump_cmd(args),
            ["pg_dump", "-h", "db1", "-p", "5433", "-U", "backup", "--no-owner", "shop"],
        )

    def test_dump_cmd_mysql_uses_consistent_snapshot(self):
        args = SimpleNamespace(engine="mysql", host=None, port=None,
                               user="backup", extra_arg=[], database="shop")
        cmd = db.build_dump_cmd(args)
        self.assertEqual(cmd[0], "mysqldump")
        self.assertIn("--single-transaction", cmd)
        self.assertEqual(cmd[-1], "shop")

    def test_human_bytes(self):
        self.assertEqual(db.human_bytes(512), "512B")
        self.assertEqual(db.human_bytes(1536), "1.5KiB")
        self.assertEqual(db.human_bytes(5 * 1024**3), "5.0GiB")


if __name__ == "__main__":
    unittest.main()
