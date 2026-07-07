# SPDX-License-Identifier: MIT
"""Integration tests for dashboard/app.py (the FastAPI layer).

These need the dashboard's web dependencies, which the toolkit itself does
not require — so the whole module skips itself unless fastapi and httpx are
installed. CI's dashboard job installs them and runs this for real; the
main test jobs and dependency-free dev boxes skip it cleanly.

Run from the repo root (with deps installed):
    pip install -r dashboard/requirements.txt httpx
    python3 -m unittest tests.test_dashboard_app -v
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "httpx", "jinja2"))

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


@unittest.skipUnless(HAVE_DEPS, "dashboard web dependencies not installed (fastapi/httpx/jinja2)")
class DashboardAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        (data / "web1" / "sys-triage").mkdir(parents=True)
        (data / "web1" / "sys-triage" / "20260707-100000.json").write_text(
            json.dumps({"summary": {"high": 1, "medium": 0, "low": 0},
                        "findings": [{"severity": "high", "category": "cpu", "message": "CPU steal 30%"}]}),
            encoding="utf-8",
        )

        # app.py does `import store`, so its directory must be importable.
        sys.path.insert(0, str(DASHBOARD_DIR))
        spec = importlib.util.spec_from_file_location("dashboard_app", DASHBOARD_DIR / "app.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        cls.client = TestClient(mod.create_app(str(data)))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        sys.path.remove(str(DASHBOARD_DIR))

    def test_index_renders_host_and_tool(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("web1", resp.text)
        self.assertIn("sys-triage", resp.text)
        self.assertIn("CRIT", resp.text)

    def test_grid_fragment_for_htmx_polling(self):
        resp = self.client.get("/fragment/grid")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("sys-triage", resp.text)
        self.assertNotIn("<html", resp.text)  # partial, not a full page

    def test_detail_page_shows_payload_and_history(self):
        resp = self.client.get("/host/web1/sys-triage")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("CPU steal 30%", resp.text)
        self.assertIn("History", resp.text)

    def test_detail_404_for_unknown_tool(self):
        self.assertEqual(self.client.get("/host/web1/nonexistent").status_code, 404)

    def test_api_summary(self):
        resp = self.client.get("/api/v1/summary")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["overall"], "crit")
        self.assertEqual(payload["hosts"]["web1"][0]["tool"], "sys-triage")

    def test_healthz(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["data_dir_exists"])


if __name__ == "__main__":
    unittest.main()
