# SPDX-License-Identifier: MIT
"""Unit tests for endpoint-watchdog/watchdog.py.

Covers the pure logic: endpoint normalization, HTTP/TCP/cert evaluation,
state transition detection, and rendering. Probes are fed fake outcomes —
no sockets, no network. The live path runs against a local http.server
in CI's smoke job.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parent.parent / "endpoint-watchdog" / "watchdog.py"
    spec = importlib.util.spec_from_file_location("endpoint_watchdog", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


wd = _load_module()


def http_endpoint(**overrides):
    cfg = {"url": "https://example.com/"}
    cfg.update(overrides)
    return wd.normalize_endpoint(cfg, {})


def outcome(status_code=200, latency_ms=100.0, body=None, error=None):
    return {"status_code": status_code, "latency_ms": latency_ms,
            "body": body, "error": error}


# ---------------------------------------------------------------------------
# Endpoint normalization
# ---------------------------------------------------------------------------

class NormalizeEndpointTest(unittest.TestCase):
    def test_name_defaults_to_url(self):
        ep = http_endpoint()
        self.assertEqual(ep["name"], "https://example.com/")
        self.assertEqual(ep["check"], "http")

    def test_tcp_name_defaults_to_host_port(self):
        ep = wd.normalize_endpoint({"check": "tcp", "host": "db1", "port": 5432}, {})
        self.assertEqual(ep["name"], "db1:5432")
        self.assertEqual(ep["target"], "tcp://db1:5432")

    def test_int_expect_status_becomes_list(self):
        self.assertEqual(http_endpoint(expect_status=200)["expect_status"], [200])
        self.assertEqual(http_endpoint(expect_status=[200, 301])["expect_status"], [200, 301])
        self.assertIsNone(http_endpoint()["expect_status"])

    def test_config_defaults_override_builtins_but_not_endpoint(self):
        ep = wd.normalize_endpoint(
            {"url": "https://x/", "timeout_seconds": 3}, {"timeout_seconds": 30, "cert_warn_days": 7},
        )
        self.assertEqual(ep["timeout_seconds"], 3)
        self.assertEqual(ep["cert_warn_days"], 7)
        self.assertEqual(ep["warn_latency_ms"], 2000)  # builtin survives

    def test_http_endpoint_without_url_is_fatal(self):
        with self.assertRaises(SystemExit):
            wd.normalize_endpoint({"name": "oops"}, {})

    def test_tcp_endpoint_without_port_is_fatal(self):
        with self.assertRaises(SystemExit):
            wd.normalize_endpoint({"check": "tcp", "host": "db1"}, {})


# ---------------------------------------------------------------------------
# HTTP evaluation
# ---------------------------------------------------------------------------

class EvaluateHttpTest(unittest.TestCase):
    def test_healthy_endpoint_is_ok(self):
        r = wd.evaluate_http(http_endpoint(), outcome())
        self.assertEqual(r.status, "ok")
        self.assertIn("HTTP 200", r.message)

    def test_default_expectation_fails_on_4xx_5xx(self):
        self.assertEqual(wd.evaluate_http(http_endpoint(), outcome(status_code=503)).status, "fail")
        self.assertEqual(wd.evaluate_http(http_endpoint(), outcome(status_code=302)).status, "ok")

    def test_explicit_status_mismatch_fails(self):
        r = wd.evaluate_http(http_endpoint(expect_status=200), outcome(status_code=301))
        self.assertEqual(r.status, "fail")
        self.assertIn("expected 200", r.message)

    def test_connection_error_fails(self):
        r = wd.evaluate_http(http_endpoint(), outcome(status_code=None, error="connection refused"))
        self.assertEqual(r.status, "fail")
        self.assertIn("connection refused", r.message)

    def test_slow_response_warns(self):
        r = wd.evaluate_http(http_endpoint(warn_latency_ms=500), outcome(latency_ms=1200.0))
        self.assertEqual(r.status, "warn")
        self.assertIn("slow", r.message)

    def test_body_mismatch_fails_even_when_status_ok(self):
        ep = http_endpoint(expect_body='"status":"ok"')
        r = wd.evaluate_http(ep, outcome(body='{"status":"degraded"}'))
        self.assertEqual(r.status, "fail")
        r = wd.evaluate_http(ep, outcome(body='{"status":"ok"}'))
        self.assertEqual(r.status, "ok")

    def test_fail_beats_warn_when_both_present(self):
        ep = http_endpoint(expect_status=200, warn_latency_ms=500)
        r = wd.evaluate_http(ep, outcome(status_code=500, latency_ms=1000.0))
        self.assertEqual(r.status, "fail")
        self.assertIn("HTTP 500", r.message)
        self.assertIn("slow", r.message)


class EvaluateTcpTest(unittest.TestCase):
    def test_connect_ok(self):
        ep = wd.normalize_endpoint({"check": "tcp", "host": "db1", "port": 5432}, {})
        r = wd.evaluate_tcp(ep, {"latency_ms": 5.0, "error": None})
        self.assertEqual(r.status, "ok")

    def test_connect_error_fails(self):
        ep = wd.normalize_endpoint({"check": "tcp", "host": "db1", "port": 5432}, {})
        r = wd.evaluate_tcp(ep, {"latency_ms": 5.0, "error": "timed out"})
        self.assertEqual(r.status, "fail")


# ---------------------------------------------------------------------------
# Certificate evaluation
# ---------------------------------------------------------------------------

class EvaluateCertTest(unittest.TestCase):
    NOW = 1_800_000_000.0
    DAY = 86400.0

    def _issue(self, days_left, warn_days=21):
        ep = http_endpoint(cert_warn_days=warn_days)
        return wd.evaluate_cert(ep, self.NOW + days_left * self.DAY, self.NOW)

    def test_far_expiry_is_fine(self):
        self.assertIsNone(self._issue(days_left=90))

    def test_soon_expiry_warns(self):
        issue = self._issue(days_left=10)
        self.assertEqual(issue[0], "warn")
        self.assertIn("expires in 10d", issue[1])

    def test_expired_fails(self):
        issue = self._issue(days_left=-3)
        self.assertEqual(issue[0], "fail")
        self.assertIn("EXPIRED", issue[1])

    def test_threshold_is_inclusive(self):
        self.assertIsNotNone(self._issue(days_left=21))
        self.assertIsNone(self._issue(days_left=22))


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def result(name, status):
    return wd.CheckResult(name=name, target=name, status=status,
                          latency_ms=10.0, message="msg")


class TransitionsTest(unittest.TestCase):
    NOW = 1_800_000_000.0

    def test_ok_to_fail_alerts(self):
        state = {"a": {"status": "ok", "since": self.NOW - 60}}
        changed = wd.transitions(state, [result("a", "fail")], self.NOW)
        self.assertEqual([(r.name, prev) for r, prev in changed], [("a", "ok")])
        self.assertEqual(state["a"], {"status": "fail", "since": self.NOW})

    def test_still_failing_stays_silent_and_keeps_since(self):
        state = {"a": {"status": "fail", "since": self.NOW - 600}}
        changed = wd.transitions(state, [result("a", "fail")], self.NOW)
        self.assertEqual(changed, [])
        self.assertEqual(state["a"]["since"], self.NOW - 600)

    def test_recovery_alerts(self):
        state = {"a": {"status": "fail", "since": self.NOW - 600}}
        changed = wd.transitions(state, [result("a", "ok")], self.NOW)
        self.assertEqual([(r.name, prev) for r, prev in changed], [("a", "fail")])

    def test_new_healthy_endpoint_is_silent(self):
        state = {}
        changed = wd.transitions(state, [result("new", "ok")], self.NOW)
        self.assertEqual(changed, [])
        self.assertIn("new", state)  # baseline recorded

    def test_new_broken_endpoint_alerts(self):
        changed = wd.transitions({}, [result("new", "fail")], self.NOW)
        self.assertEqual([(r.name, prev) for r, prev in changed], [("new", "ok")])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class RenderTest(unittest.TestCase):
    def _results(self):
        return [result("a", "ok"), result("b", "fail"), result("c", "warn")]

    def test_json_output_is_valid_and_sorted_worst_first(self):
        payload = json.loads(wd.render_json(self._results()))
        self.assertEqual(payload["summary"], {"ok": 1, "warn": 1, "fail": 1})
        self.assertEqual([r["status"] for r in payload["results"]], ["fail", "warn", "ok"])

    def test_table_contains_counts(self):
        out = wd.render_table(self._results(), color=False)
        self.assertIn("3 endpoint(s): 1 ok, 1 warn, 1 fail", out)
        self.assertLess(out.index("FAIL"), out.index("WARN"))

    def test_table_color_can_be_disabled(self):
        out = wd.render_table(self._results(), color=False)
        self.assertNotIn("\033[", out)


if __name__ == "__main__":
    unittest.main()
