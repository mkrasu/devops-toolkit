# SPDX-License-Identifier: MIT
"""Unit tests for dashboard/notify.py and db.notify_transitions.

Covers the transition detection (including staleness — the case only the
dashboard can catch), message building, and dispatch with a patched
post_json. Standard library only.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


def _load(name: str):
    sys.path.insert(0, str(DASHBOARD_DIR))
    try:
        spec = importlib.util.spec_from_file_location(f"dashboard_{name}", DASHBOARD_DIR / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(DASHBOARD_DIR))


db = _load("db")
notify = _load("notify")

SEV_OK = {"summary": {"high": 0, "medium": 0, "low": 0}}
SEV_WARN = {"summary": {"high": 0, "medium": 2, "low": 0}}
SEV_BAD = {"summary": {"high": 3, "medium": 0, "low": 0}}


class NotifyTransitionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "test.sqlite3")

    def _insert(self, payload, ts="20260707-100000", host="web1", tool="sys-triage", age=60):
        db.insert_result(self.db, host, tool, ts, payload, created_at=time.time() - age)

    def test_new_healthy_tile_is_silent_but_baselined(self):
        self._insert(SEV_OK)
        self.assertEqual(db.notify_transitions(self.db), [])
        # ...and stays silent on the next sweep too
        self.assertEqual(db.notify_transitions(self.db), [])

    def test_new_broken_tile_alerts_once(self):
        self._insert(SEV_BAD)
        transitions = db.notify_transitions(self.db)
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["previous"], "ok")
        self.assertEqual(transitions[0]["current"], "crit")
        # same state next sweep: no repeat
        self.assertEqual(db.notify_transitions(self.db), [])

    def test_recovery_alerts(self):
        self._insert(SEV_BAD, ts="20260707-100000")
        db.notify_transitions(self.db)
        self._insert(SEV_OK, ts="20260707-110000", age=30)
        transitions = db.notify_transitions(self.db)
        self.assertEqual(transitions[0]["previous"], "crit")
        self.assertEqual(transitions[0]["current"], "ok")

    def test_ok_to_warn_is_silent_but_tracked(self):
        self._insert(SEV_OK, ts="20260707-100000")
        db.notify_transitions(self.db)
        self._insert(SEV_WARN, ts="20260707-110000", age=30)
        self.assertEqual(db.notify_transitions(self.db), [])   # warn alone: no page
        self._insert(SEV_BAD, ts="20260707-120000", age=10)
        transitions = db.notify_transitions(self.db)
        self.assertEqual(transitions[0]["previous"], "warn")   # tracked, not lost
        self.assertEqual(transitions[0]["current"], "crit")

    def test_stale_tile_alerts(self):
        # healthy result, but far older than sys-triage's 26h staleness budget
        self._insert(SEV_OK, age=30 * 3600)
        transitions = db.notify_transitions(self.db)
        self.assertEqual(transitions[0]["current"], "stale")

    def test_stale_recovery_alerts(self):
        self._insert(SEV_OK, ts="20260707-100000", age=30 * 3600)
        db.notify_transitions(self.db)
        self._insert(SEV_OK, ts="20260708-100000", age=30)
        transitions = db.notify_transitions(self.db)
        self.assertEqual(transitions[0]["previous"], "stale")
        self.assertEqual(transitions[0]["current"], "ok")


class BuildTextTest(unittest.TestCase):
    def _t(self, previous, current, headline="2 high / 0 medium / 0 low"):
        return {"host": "web1", "tool": "sys-triage", "previous": previous,
                "current": current, "headline": headline, "timestamp": "20260707-100000"}

    def test_crit_message(self):
        text = notify.build_text(self._t("ok", "crit"))
        self.assertIn("[CRIT]", text)
        self.assertIn("web1/sys-triage", text)
        self.assertIn("(was ok)", text)

    def test_stale_message_mentions_collector(self):
        text = notify.build_text(self._t("ok", "stale"))
        self.assertIn("[STALE]", text)
        self.assertIn("collector", text)

    def test_recovery_message(self):
        text = notify.build_text(self._t("crit", "ok"))
        self.assertIn("[RECOVERED]", text)
        self.assertIn("(was crit)", text)


class CheckAndNotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "test.sqlite3")
        self.data_dir = os.path.join(self.tmp.name, "data")
        self.posts = []
        self._orig = notify.post_json
        notify.post_json = lambda url, payload, **kw: self.posts.append((url, payload)) or True
        self.addCleanup(setattr, notify, "post_json", self._orig)

    def test_sends_to_both_destinations(self):
        db.insert_result(self.db, "web1", "sys-triage", "20260707-100000", SEV_BAD)
        transitions = notify.check_and_notify(self.db, self.data_dir,
                                              "http://slack.example/hook", "http://generic.example/hook")
        self.assertEqual(len(transitions), 1)
        self.assertEqual(len(self.posts), 2)
        slack_payload = dict(self.posts)["http://slack.example/hook"]
        self.assertIn("[CRIT]", slack_payload["text"])
        generic_payload = dict(self.posts)["http://generic.example/hook"]
        self.assertEqual(generic_payload["current"], "crit")

    def test_quiet_when_nothing_changed(self):
        db.insert_result(self.db, "web1", "sys-triage", "20260707-100000", SEV_OK)
        notify.check_and_notify(self.db, self.data_dir, "http://slack.example/hook", None)
        self.assertEqual(self.posts, [])

    def test_imports_files_before_checking(self):
        tool_dir = Path(self.data_dir, "filehost", "sys-triage")
        tool_dir.mkdir(parents=True)
        (tool_dir / "20260707-100000.json").write_text(
            '{"summary": {"high": 1, "medium": 0, "low": 0}}', encoding="utf-8")
        transitions = notify.check_and_notify(self.db, self.data_dir, None, "http://generic.example/hook")
        self.assertEqual(transitions[0]["host"], "filehost")
        self.assertEqual(len(self.posts), 1)


if __name__ == "__main__":
    unittest.main()
