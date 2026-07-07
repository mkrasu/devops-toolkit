# SPDX-License-Identifier: MIT
"""Unit tests for log-tailer-alert/log-alert.py.

Covers the threshold/window/cooldown logic, config env substitution,
notifier filtering, and --once state tracking (offsets and cross-run
pattern hit accumulation). No network calls, no real notifiers.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_module():
    path = Path(__file__).resolve().parent.parent / "log-tailer-alert" / "log-alert.py"
    spec = importlib.util.spec_from_file_location("log_alert", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


la = _load_module()


def make_pattern(**overrides):
    cfg = {
        "name": "test-pattern",
        "regex": "ERROR",
        "severity": "high",
        "threshold": 1,
        "window_seconds": 60,
        "cooldown_seconds": 300,
    }
    cfg.update(overrides)
    return la.Pattern.from_config(cfg)


# ---------------------------------------------------------------------------
# Threshold / window / cooldown
# ---------------------------------------------------------------------------

# Realistic epoch-ish base timestamp: _last_alert starts at 0.0 (i.e. 1970),
# so tiny `now` values would sit inside the initial cooldown window.
T = 1_000_000.0


class PatternCheckTest(unittest.TestCase):
    def test_non_matching_line_is_ignored(self):
        p = make_pattern()
        self.assertIsNone(p.check("all quiet", "app.log", now=T))

    def test_fires_only_at_threshold(self):
        p = make_pattern(threshold=3)
        self.assertIsNone(p.check("ERROR 1", "app.log", now=T))
        self.assertIsNone(p.check("ERROR 2", "app.log", now=T + 1))
        alert = p.check("ERROR 3", "app.log", now=T + 2)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.count, 3)
        self.assertEqual(alert.severity, "high")

    def test_hits_outside_window_do_not_count(self):
        p = make_pattern(threshold=2, window_seconds=10)
        self.assertIsNone(p.check("ERROR early", "app.log", now=T))
        # 20s later the first hit has aged out of the 10s window.
        self.assertIsNone(p.check("ERROR late", "app.log", now=T + 20))

    def test_cooldown_suppresses_refire(self):
        p = make_pattern(threshold=1, cooldown_seconds=300)
        self.assertIsNotNone(p.check("ERROR a", "app.log", now=T))
        self.assertIsNone(p.check("ERROR b", "app.log", now=T + 10))
        self.assertIsNotNone(p.check("ERROR c", "app.log", now=T + 301))

    def test_threshold_resets_after_alert(self):
        p = make_pattern(threshold=2, cooldown_seconds=0)
        self.assertIsNone(p.check("ERROR 1", "app.log", now=T))
        self.assertIsNotNone(p.check("ERROR 2", "app.log", now=T + 1))
        # A single new hit must not re-fire: the alert cleared the hit history.
        self.assertIsNone(p.check("ERROR 3", "app.log", now=T + 2))
        self.assertIsNotNone(p.check("ERROR 4", "app.log", now=T + 3))

    def test_alert_text_contains_pattern_and_samples(self):
        p = make_pattern()
        alert = p.check("ERROR boom", "app.log", now=T)
        self.assertIn("[HIGH]", alert.text())
        self.assertIn("test-pattern", alert.text())
        self.assertIn("ERROR boom", alert.text())


class PatternStateRoundtripTest(unittest.TestCase):
    def test_hits_survive_export_restore(self):
        p1 = make_pattern(threshold=3, window_seconds=3600)
        p1.check("ERROR 1", "app.log", now=1000.0)
        p1.check("ERROR 2", "app.log", now=1001.0)

        p2 = make_pattern(threshold=3, window_seconds=3600)
        p2.restore_state(p1.export_state(), now=1002.0)
        # Third hit in a "new process" completes the threshold.
        self.assertIsNotNone(p2.check("ERROR 3", "app.log", now=1002.0))

    def test_stale_hits_are_dropped_on_restore(self):
        p1 = make_pattern(threshold=2, window_seconds=10)
        p1.check("ERROR old", "app.log", now=0.0)

        p2 = make_pattern(threshold=2, window_seconds=10)
        p2.restore_state(p1.export_state(), now=100.0)
        # The restored hit is far outside the window; one new hit isn't enough.
        self.assertIsNone(p2.check("ERROR new", "app.log", now=100.0))

    def test_cooldown_survives_restore(self):
        p1 = make_pattern(threshold=1, cooldown_seconds=300)
        self.assertIsNotNone(p1.check("ERROR a", "app.log", now=1000.0))

        p2 = make_pattern(threshold=1, cooldown_seconds=300)
        p2.restore_state(p1.export_state(), now=1010.0)
        self.assertIsNone(p2.check("ERROR b", "app.log", now=1010.0))


# ---------------------------------------------------------------------------
# Config env substitution
# ---------------------------------------------------------------------------

class SubstituteEnvTest(unittest.TestCase):
    def test_substitutes_in_nested_structures(self):
        with mock.patch.dict(os.environ, {"HOOK_URL": "https://example.com/hook"}):
            cfg = la.substitute_env({"notifiers": {"slack": {"webhook_url": "${HOOK_URL}"}}})
        self.assertEqual(cfg["notifiers"]["slack"]["webhook_url"], "https://example.com/hook")

    def test_missing_variable_exits_with_error(self):
        env = {k: v for k, v in os.environ.items() if k != "DEFINITELY_NOT_SET"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit):
                la.substitute_env("${DEFINITELY_NOT_SET}")

    def test_non_strings_pass_through(self):
        self.assertEqual(la.substitute_env({"threshold": 3, "flag": True}), {"threshold": 3, "flag": True})

    def test_disabled_notifier_does_not_require_its_secrets(self):
        """--test / --dry-run must work without production secrets as long as
        the notifiers referencing them are disabled."""
        cfg = {
            "patterns": [],
            "notifiers": {
                "slack": {"enabled": False, "webhook_url": "${UNSET_SECRET_VAR}"},
                "webhook": {"url": "https://example.com/hook"},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
            env = {k: v for k, v in os.environ.items() if k != "UNSET_SECRET_VAR"}
            with mock.patch.dict(os.environ, env, clear=True):
                loaded = la.load_config(path)
        self.assertNotIn("slack", loaded["notifiers"])
        self.assertIn("webhook", loaded["notifiers"])


# ---------------------------------------------------------------------------
# Notifier dispatch filtering
# ---------------------------------------------------------------------------

class SendNotificationsTest(unittest.TestCase):
    def _alert(self, severity):
        return la.Alert(
            pattern="p", severity=severity, count=1, window_seconds=60,
            source="app.log", samples=["boom"],
        )

    def test_min_severity_floor_filters(self):
        calls = []
        fake = lambda cfg, alert: calls.append(alert.severity)  # noqa: E731
        with mock.patch.dict(la.NOTIFIER_DISPATCH, {"fake": fake}):
            cfg = {"fake": {"min_severity": "high"}}
            la.send_notifications(self._alert("low"), cfg)
            la.send_notifications(self._alert("high"), cfg)
        self.assertEqual(calls, ["high"])

    def test_disabled_notifier_is_skipped(self):
        calls = []
        fake = lambda cfg, alert: calls.append(alert)  # noqa: E731
        with mock.patch.dict(la.NOTIFIER_DISPATCH, {"fake": fake}):
            la.send_notifications(self._alert("high"), {"fake": {"enabled": False}})
        self.assertEqual(calls, [])


# ---------------------------------------------------------------------------
# --once mode: offset tracking and cross-run threshold accumulation
# ---------------------------------------------------------------------------

class OnceModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = os.path.join(self.tmp.name, "state")
        self.log_path = os.path.join(self.tmp.name, "app.log")

    def _append(self, text):
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(text)

    def test_first_run_baselines_at_eof(self):
        self._append("ERROR before baseline\n")
        processed, fired = la.process_once(
            self.log_path, [make_pattern()], {}, dry_run=True,
            from_start=False, state_dir=self.state_dir,
        )
        self.assertEqual((processed, fired), (0, 0))

    def test_second_run_reads_only_new_lines(self):
        self._append("ERROR before baseline\n")
        la.process_once(self.log_path, [make_pattern()], {}, True, False, self.state_dir)
        self._append("ok line\nERROR after baseline\n")
        processed, fired = la.process_once(
            self.log_path, [make_pattern()], {}, True, False, self.state_dir,
        )
        self.assertEqual((processed, fired), (2, 1))

    def test_truncated_file_is_reread_from_start(self):
        self._append("ERROR one\nERROR two\nERROR three\n")
        la.process_once(self.log_path, [make_pattern()], {}, True, False, self.state_dir)
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("ERROR new\n")  # shorter than the saved offset
        processed, fired = la.process_once(
            self.log_path, [make_pattern()], {}, True, False, self.state_dir,
        )
        self.assertEqual((processed, fired), (1, 1))

    def test_threshold_accumulates_across_runs(self):
        """A '3 hits in an hour' threshold must be able to trip even when the
        hits arrive across separate --once invocations (fresh processes)."""
        def run(lines):
            # Fresh Pattern objects each run, as a new process would have.
            patterns = [make_pattern(threshold=3, window_seconds=3600)]
            self._append(lines)
            return la.run_once(
                [self.log_path], patterns, {}, dry_run=True,
                from_start=False, state_dir=self.state_dir,
            )

        run("")                          # run 1: establish baseline
        run("ERROR 1\nERROR 2\n")        # run 2: two hits, below threshold
        state_file = os.path.join(self.state_dir, "patterns.json")
        with open(state_file, encoding="utf-8") as f:
            state = json.load(f)["test-pattern"]
        self.assertEqual(len(state["hits"]), 2)
        self.assertEqual(state["last_alert"], 0.0)

        run("ERROR 3\n")                 # run 3: third hit trips the threshold
        with open(state_file, encoding="utf-8") as f:
            state = json.load(f)["test-pattern"]
        self.assertEqual(state["hits"], [])      # cleared after firing
        self.assertGreater(state["last_alert"], 0.0)


if __name__ == "__main__":
    unittest.main()
