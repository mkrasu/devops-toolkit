# SPDX-License-Identifier: MIT
"""Unit tests for dashboard/store.py.

store.py is deliberately stdlib-only, so these run everywhere without the
dashboard's web dependencies. The FastAPI layer is tested separately in
test_dashboard_app.py (skipped unless fastapi is installed).

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


def _load_module():
    path = Path(__file__).resolve().parent.parent / "dashboard" / "store.py"
    spec = importlib.util.spec_from_file_location("dashboard_store", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ds = _load_module()


# ---------------------------------------------------------------------------
# Status derivation
# ---------------------------------------------------------------------------

class DeriveStatusTest(unittest.TestCase):
    def test_severity_summary_tools(self):
        for tool in ("sys-triage", "host-hardening-check", "k8s-resource-auditor"):
            status, headline = ds.derive_status(tool, {"summary": {"high": 2, "medium": 1, "low": 0}})
            self.assertEqual(status, "crit", tool)
            self.assertIn("2 high", headline)
        status, _ = ds.derive_status("sys-triage", {"summary": {"high": 0, "medium": 3, "low": 1}})
        self.assertEqual(status, "warn")
        status, _ = ds.derive_status("sys-triage", {"summary": {"high": 0, "medium": 0, "low": 0}})
        self.assertEqual(status, "ok")

    def test_endpoint_watchdog(self):
        status, headline = ds.derive_status("endpoint-watchdog", {"summary": {"ok": 4, "warn": 0, "fail": 1}})
        self.assertEqual(status, "crit")
        self.assertIn("1 fail", headline)
        status, _ = ds.derive_status("endpoint-watchdog", {"summary": {"ok": 5, "warn": 1, "fail": 0}})
        self.assertEqual(status, "warn")

    def test_db_backup_verify_failure_is_crit(self):
        payload = {"backup": {"file": "x", "size_bytes": 1048576}, "verify": {"mode": "restore", "ok": False}}
        status, headline = ds.derive_status("db-backup-rotate", payload)
        self.assertEqual(status, "crit")
        self.assertIn("FAILED", headline)

    def test_db_backup_success_shows_size(self):
        payload = {"backup": {"file": "x", "size_bytes": 5 * 1048576}, "verify": {"mode": "restore", "ok": True}}
        status, headline = ds.derive_status("db-backup-rotate", payload)
        self.assertEqual(status, "ok")
        self.assertIn("5.0 MiB", headline)

    def test_docker_cleanup(self):
        status, headline = ds.derive_status("docker-cleanup", {
            "dry_run": False, "total_reclaimed_human": "1.2GB",
            "actions": [{"action": "containers", "error": False}],
        })
        self.assertEqual(status, "ok")
        self.assertIn("1.2GB", headline)

    def test_docker_cleanup_with_errors_warns(self):
        status, _ = ds.derive_status("docker-cleanup", {
            "total_reclaimed_human": "0B", "actions": [{"action": "volumes", "error": True}],
        })
        self.assertEqual(status, "warn")

    def test_unknown_tool_with_severity_convention_falls_back(self):
        status, _ = ds.derive_status("future-tool", {"summary": {"high": 1, "medium": 0, "low": 0}})
        self.assertEqual(status, "crit")

    def test_unknown_tool_without_summary(self):
        status, headline = ds.derive_status("mystery", {"whatever": 1})
        self.assertEqual(status, "unknown")

    def test_malformed_payload_does_not_crash(self):
        status, _ = ds.derive_status("db-backup-rotate", {"backup": "not-a-dict", "verify": None})
        self.assertEqual(status, "unknown")


# ---------------------------------------------------------------------------
# Filesystem scanning
# ---------------------------------------------------------------------------

class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, host, tool, name, payload, age_seconds=60):
        d = Path(self.tmp.name, host, tool)
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text(json.dumps(payload) if isinstance(payload, dict) else payload, encoding="utf-8")
        mtime = time.time() - age_seconds
        os.utime(p, (mtime, mtime))

    def test_parse_result_name(self):
        self.assertEqual(ds.parse_result_name("20260707-120000.json"), "20260707-120000")
        self.assertIsNone(ds.parse_result_name("latest.json"))
        self.assertIsNone(ds.parse_result_name("20260707-120000.json.tmp"))

    def test_scan_picks_newest_result(self):
        self._write("web1", "sys-triage", "20260707-100000.json", {"summary": {"high": 5, "medium": 0, "low": 0}})
        self._write("web1", "sys-triage", "20260707-110000.json", {"summary": {"high": 0, "medium": 0, "low": 0}})
        hosts = ds.scan(self.tmp.name)
        self.assertEqual(list(hosts), ["web1"])
        self.assertEqual(hosts["web1"][0].status, "ok")
        self.assertEqual(hosts["web1"][0].timestamp, "20260707-110000")

    def test_worst_status_sorts_first_within_host(self):
        self._write("web1", "sys-triage", "20260707-110000.json", {"summary": {"high": 0, "medium": 0, "low": 0}})
        self._write("web1", "endpoint-watchdog", "20260707-110001.json", {"summary": {"ok": 1, "warn": 0, "fail": 2}})
        hosts = ds.scan(self.tmp.name)
        self.assertEqual([s.tool for s in hosts["web1"]], ["endpoint-watchdog", "sys-triage"])

    def test_stale_detection_uses_per_tool_threshold(self):
        self._write("web1", "endpoint-watchdog", "20260707-100000.json",
                    {"summary": {"ok": 1, "warn": 0, "fail": 0}}, age_seconds=3600)
        self._write("web1", "sys-triage", "20260707-100000.json",
                    {"summary": {"high": 0, "medium": 0, "low": 0}}, age_seconds=3600)
        states = {s.tool: s for s in ds.scan(self.tmp.name)["web1"]}
        self.assertTrue(states["endpoint-watchdog"].stale)   # 1h old, 15m budget
        self.assertFalse(states["sys-triage"].stale)         # 1h old, 26h budget

    def test_invalid_json_is_unknown_not_fatal(self):
        self._write("web1", "sys-triage", "20260707-100000.json", "{not json")
        state = ds.scan(self.tmp.name)["web1"][0]
        self.assertEqual(state.status, "unknown")
        self.assertIn("not valid JSON", state.headline)

    def test_foreign_files_are_ignored(self):
        self._write("web1", "sys-triage", "README.txt", "hello")
        self.assertEqual(ds.scan(self.tmp.name), {})

    def test_missing_data_dir_returns_empty(self):
        self.assertEqual(ds.scan(os.path.join(self.tmp.name, "nope")), {})

    def test_history_is_newest_first(self):
        self._write("web1", "sys-triage", "20260707-100000.json", {"summary": {"high": 1, "medium": 0, "low": 0}})
        self._write("web1", "sys-triage", "20260707-110000.json", {"summary": {"high": 0, "medium": 0, "low": 0}})
        hist = ds.history(self.tmp.name, "web1", "sys-triage")
        self.assertEqual([h["timestamp"] for h in hist], ["20260707-110000", "20260707-100000"])
        self.assertEqual([h["status"] for h in hist], ["ok", "crit"])

    def test_overall_status_is_worst(self):
        self._write("web1", "sys-triage", "20260707-110000.json", {"summary": {"high": 0, "medium": 1, "low": 0}})
        self._write("db1", "db-backup-rotate", "20260707-110000.json",
                    {"backup": None, "verify": {"mode": "restore", "ok": False}})
        self.assertEqual(ds.overall_status(ds.scan(self.tmp.name)), "crit")


class FormattingTest(unittest.TestCase):
    def test_format_age(self):
        self.assertEqual(ds.format_age(30), "30s ago")
        self.assertEqual(ds.format_age(600), "10m ago")
        self.assertEqual(ds.format_age(7200), "2.0h ago")
        self.assertEqual(ds.format_age(3 * 86400), "3.0d ago")

    def test_format_timestamp(self):
        self.assertEqual(ds.format_timestamp("20260707-101500"), "2026-07-07 10:15:00")
        self.assertEqual(ds.format_timestamp("garbage"), "garbage")


if __name__ == "__main__":
    unittest.main()
