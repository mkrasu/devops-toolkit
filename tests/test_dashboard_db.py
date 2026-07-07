# SPDX-License-Identifier: MIT
"""Unit tests for dashboard/db.py (the SQLite result store).

sqlite3 is standard library, so like test_dashboard_store.py this runs
everywhere without the dashboard's web dependencies.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


def _load_module():
    # db.py does `import store`, so the dashboard dir must be importable.
    sys.path.insert(0, str(DASHBOARD_DIR))
    spec = importlib.util.spec_from_file_location("dashboard_db", DASHBOARD_DIR / "db.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


db = _load_module()

SEV_OK = {"summary": {"high": 0, "medium": 0, "low": 0}}
SEV_BAD = {"summary": {"high": 2, "medium": 1, "low": 0}}


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "test.sqlite3")


class InsertAndLatestTest(DbTestCase):
    def test_insert_derives_status(self):
        self.assertTrue(db.insert_result(self.db, "web1", "sys-triage", "20260707-100000", SEV_BAD))
        states = db.latest_states(self.db)
        self.assertEqual(states["web1"][0].status, "crit")
        self.assertIn("2 high", states["web1"][0].headline)

    def test_duplicate_timestamp_is_idempotent(self):
        self.assertTrue(db.insert_result(self.db, "web1", "sys-triage", "20260707-100000", SEV_OK))
        self.assertFalse(db.insert_result(self.db, "web1", "sys-triage", "20260707-100000", SEV_BAD))
        self.assertEqual(len(db.history(self.db, "web1", "sys-triage")), 1)

    def test_latest_wins_by_created_at(self):
        db.insert_result(self.db, "web1", "sys-triage", "20260707-100000", SEV_BAD, created_at=1000.0)
        db.insert_result(self.db, "web1", "sys-triage", "20260707-110000", SEV_OK, created_at=2000.0)
        state = db.latest_state(self.db, "web1", "sys-triage")
        self.assertEqual(state.status, "ok")
        self.assertEqual(state.timestamp, "20260707-110000")

    def test_equal_created_at_breaks_tie_deterministically(self):
        db.insert_result(self.db, "web1", "sys-triage", "20260707-100000", SEV_BAD, created_at=1000.0)
        db.insert_result(self.db, "web1", "sys-triage", "20260707-100001", SEV_OK, created_at=1000.0)
        states = db.latest_states(self.db)
        self.assertEqual(len(states["web1"]), 1)          # exactly one row per tool
        self.assertEqual(states["web1"][0].status, "ok")  # the later insert

    def test_stale_detection_from_created_at(self):
        db.insert_result(self.db, "web1", "sys-triage", "20260707-100000", SEV_OK,
                         created_at=time.time() - 30 * 3600)
        self.assertTrue(db.latest_state(self.db, "web1", "sys-triage").stale)

    def test_prune_keeps_newest(self):
        for i in range(5):
            db.insert_result(self.db, "web1", "sys-triage", f"20260707-10000{i}", SEV_OK,
                             created_at=1000.0 + i, keep=3)
        hist = db.history(self.db, "web1", "sys-triage")
        self.assertEqual([h["timestamp"] for h in hist],
                         ["20260707-100004", "20260707-100003", "20260707-100002"])

    def test_missing_tool_returns_none(self):
        self.assertIsNone(db.latest_state(self.db, "web1", "nope"))


class SeriesTest(DbTestCase):
    def test_severity_series(self):
        db.insert_result(self.db, "web1", "sys-triage", "20260707-100000", SEV_BAD, created_at=1.0)
        db.insert_result(self.db, "web1", "sys-triage", "20260707-110000", SEV_OK, created_at=2.0)
        points = db.series(self.db, "web1", "sys-triage")
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["high"], 2)   # oldest first
        self.assertEqual(points[1]["high"], 0)

    def test_watchdog_series_includes_avg_latency(self):
        payload = {"summary": {"ok": 2, "warn": 0, "fail": 0},
                   "results": [{"latency_ms": 100.0}, {"latency_ms": 300.0}]}
        db.insert_result(self.db, "web1", "endpoint-watchdog", "20260707-100000", payload)
        points = db.series(self.db, "web1", "endpoint-watchdog")
        self.assertEqual(points[0]["avg_latency_ms"], 200.0)
        self.assertEqual(points[0]["ok"], 2)

    def test_db_backup_series_tracks_size(self):
        payload = {"backup": {"size_bytes": 10 * 1024**2}, "verify": {"mode": "restore", "ok": True}}
        db.insert_result(self.db, "db1", "db-backup-rotate", "20260707-100000", payload)
        points = db.series(self.db, "db1", "db-backup-rotate")
        self.assertEqual(points[0]["size_mib"], 10.0)
        self.assertEqual(points[0]["verify_ok"], 1)

    def test_unchartable_payload_gives_empty_series(self):
        db.insert_result(self.db, "web1", "mystery", "20260707-100000", {"foo": 1})
        self.assertEqual(db.series(self.db, "web1", "mystery"), [])


class ImportFromDirTest(DbTestCase):
    def _write(self, host, tool, name, payload):
        d = Path(self.tmp.name, "data", host, tool)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(payload), encoding="utf-8")

    def test_import_and_idempotency(self):
        self._write("web1", "sys-triage", "20260707-100000.json", SEV_OK)
        self._write("web1", "sys-triage", "20260707-110000.json", SEV_BAD)
        self._write("db1", "db-backup-rotate", "20260707-100000.json",
                    {"backup": None, "verify": {"mode": "gzip", "ok": True}})
        data_dir = os.path.join(self.tmp.name, "data")
        self.assertEqual(db.import_from_dir(self.db, data_dir), 3)
        self.assertEqual(db.import_from_dir(self.db, data_dir), 0)  # nothing new
        self.assertEqual(set(db.latest_states(self.db)), {"web1", "db1"})

    def test_foreign_and_invalid_files_are_skipped(self):
        self._write("web1", "sys-triage", "notes.txt", {})
        d = Path(self.tmp.name, "data", "web1", "sys-triage")
        (d / "20260707-100000.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(db.import_from_dir(self.db, os.path.join(self.tmp.name, "data")), 0)

    def test_missing_dir_is_harmless(self):
        self.assertEqual(db.import_from_dir(self.db, os.path.join(self.tmp.name, "nope")), 0)


class TokenTest(unittest.TestCase):
    def test_parse_env_style(self):
        tokens = db.parse_tokens("web1:abc, db1:def")
        self.assertEqual(tokens, {"web1": "abc", "db1": "def"})

    def test_parse_file_style_with_comments(self):
        tokens = db.parse_tokens("# per-host tokens\nweb1:abc\n\n*:shared\n")
        self.assertEqual(tokens, {"web1": "abc", "*": "shared"})

    def test_malformed_entries_are_dropped(self):
        self.assertEqual(db.parse_tokens("no-colon, :empty-host, host:"), {})

    def test_token_allows_exact_host(self):
        tokens = {"web1": "abc"}
        self.assertTrue(db.token_allows(tokens, "web1", "abc"))
        self.assertFalse(db.token_allows(tokens, "web1", "wrong"))
        self.assertFalse(db.token_allows(tokens, "db1", "abc"))
        self.assertFalse(db.token_allows(tokens, "web1", ""))

    def test_wildcard_token(self):
        tokens = {"*": "shared"}
        self.assertTrue(db.token_allows(tokens, "anyhost", "shared"))
        self.assertFalse(db.token_allows(tokens, "anyhost", "nope"))


if __name__ == "__main__":
    unittest.main()
