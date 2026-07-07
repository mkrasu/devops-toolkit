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

SEV_BAD = {"summary": {"high": 1, "medium": 0, "low": 0},
           "findings": [{"severity": "high", "category": "cpu", "message": "CPU steal 30%"}]}
SEV_OK = {"summary": {"high": 0, "medium": 0, "low": 0}}


@unittest.skipUnless(HAVE_DEPS, "dashboard web dependencies not installed (fastapi/httpx/jinja2)")
class DashboardAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name) / "data"
        (data / "web1" / "sys-triage").mkdir(parents=True)
        (data / "web1" / "sys-triage" / "20260707-100000.json").write_text(
            json.dumps(SEV_BAD), encoding="utf-8",
        )

        # app.py does `import store` / `import db`, so its directory must be importable.
        sys.path.insert(0, str(DASHBOARD_DIR))
        spec = importlib.util.spec_from_file_location("dashboard_app", DASHBOARD_DIR / "app.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        cls.mod = mod
        cls.client = TestClient(mod.create_app(
            data_dir=str(data),
            db_path=str(Path(cls.tmp.name) / "dash.sqlite3"),
            tokens={"web1": "sekrit", "remote1": "r3mote"},
        ))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        sys.path.remove(str(DASHBOARD_DIR))

    # -- pages (file-imported result) -----------------------------------------

    def test_index_renders_imported_file_result(self):
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

    # -- ingest API ------------------------------------------------------------

    def _post(self, host, tool, payload, token):
        return self.client.post(
            f"/api/v1/ingest/{host}/{tool}", json=payload,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )

    def test_ingest_happy_path(self):
        resp = self._post("remote1", "host-hardening-check", SEV_OK, "r3mote")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["status"], "ok")
        summary = self.client.get("/api/v1/summary").json()
        tools = [s["tool"] for s in summary["hosts"]["remote1"]]
        self.assertIn("host-hardening-check", tools)

    def test_ingest_rejects_wrong_token(self):
        self.assertEqual(self._post("remote1", "sys-triage", SEV_OK, "wrong").status_code, 401)

    def test_ingest_rejects_missing_token(self):
        self.assertEqual(self._post("remote1", "sys-triage", SEV_OK, None).status_code, 401)

    def test_ingest_rejects_token_of_other_host(self):
        # web1's token must not let anyone write results for remote1
        self.assertEqual(self._post("remote1", "sys-triage", SEV_OK, "sekrit").status_code, 401)

    def test_ingest_rejects_invalid_json(self):
        resp = self.client.post(
            "/api/v1/ingest/remote1/sys-triage", content=b"{not json",
            headers={"Authorization": "Bearer r3mote", "Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_ingest_disabled_without_tokens(self):
        app = self.mod.create_app(
            data_dir=str(Path(self.tmp.name) / "nodata"),
            db_path=str(Path(self.tmp.name) / "notokens.sqlite3"),
            tokens={},
        )
        from fastapi.testclient import TestClient
        resp = TestClient(app).post("/api/v1/ingest/x/y", json={},
                                    headers={"Authorization": "Bearer z"})
        self.assertEqual(resp.status_code, 503)

    # -- series + misc ----------------------------------------------------------

    def test_series_endpoint(self):
        resp = self.client.get("/api/v1/series/web1/sys-triage")
        self.assertEqual(resp.status_code, 200)
        points = resp.json()["points"]
        self.assertGreaterEqual(len(points), 1)
        self.assertEqual(points[0]["high"], 1)

    def test_api_summary(self):
        resp = self.client.get("/api/v1/summary")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["overall"], "crit")
        self.assertEqual(payload["hosts"]["web1"][0]["tool"], "sys-triage")

    def test_healthz_reports_ingest_enabled(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ingest_enabled"])


if __name__ == "__main__":
    unittest.main()
