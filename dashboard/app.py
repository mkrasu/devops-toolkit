# SPDX-License-Identifier: MIT
"""app.py — the dashboard web layer (FastAPI + Jinja2 + htmx).

Phase 2: results live in SQLite (db.py). They arrive either via the HTTP
ingest endpoint (remote hosts POSTing JSON with a per-host bearer token) or
via the phase-1 file collectors — the data directory is imported into the
database automatically, so NFS/rsync setups keep working unchanged.

Still read-only where it matters: the dashboard never executes a tool.

Configuration (env):
    DASHBOARD_DATA_DIR   file-collector drop directory to import (default /data)
    DASHBOARD_DB         SQLite path (default /db/dashboard.sqlite3)
    DASHBOARD_TOKENS     ingest tokens, "host:token,host2:token2" ("*" = any host)
    DASHBOARD_TOKENS_FILE  same format, one per line — takes precedence
    DASHBOARD_NOTIFY_SLACK    Slack-compatible webhook for tile state changes
    DASHBOARD_NOTIFY_WEBHOOK  generic JSON webhook for tile state changes

Run locally:
    DASHBOARD_DATA_DIR=./data DASHBOARD_DB=./dashboard.sqlite3 uvicorn app:app --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import notify
import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMPORT_INTERVAL = 30          # seconds between directory-import sweeps
MAX_INGEST_BYTES = 1_000_000  # a tool report should be far smaller

# Hostnames and tool names become database keys and UI labels; keep them sane.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def load_tokens() -> dict[str, str]:
    path = os.environ.get("DASHBOARD_TOKENS_FILE")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return db.parse_tokens(f.read())
        except OSError:
            return {}
    return db.parse_tokens(os.environ.get("DASHBOARD_TOKENS", ""))


def create_app(data_dir: str, db_path: str, tokens: dict[str, str] | None = None,
               notify_slack: str | None = None, notify_webhook: str | None = None,
               notify_interval: float = 60.0) -> FastAPI:

    async def notify_loop() -> None:
        """Periodically sweep for tile state changes and send notifications.
        This is what catches a collector going silent (stale) even when
        nobody has the dashboard open."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                await loop.run_in_executor(
                    None, notify.check_and_notify, db_path, data_dir, notify_slack, notify_webhook,
                )
            except Exception as e:  # never let one bad sweep kill the loop
                print(f"notify loop error: {e}", flush=True)
            await asyncio.sleep(notify_interval)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        if notify_slack or notify_webhook:
            task = asyncio.create_task(notify_loop())
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="devops-toolkit dashboard", docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
    templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
    templates.env.filters["age"] = store.format_age
    templates.env.filters["ts"] = store.format_timestamp
    ingest_tokens = load_tokens() if tokens is None else tokens

    last_import = {"at": 0.0}

    def refresh() -> None:
        """Pick up anything file collectors dropped since the last sweep."""
        now = time.monotonic()
        if now - last_import["at"] >= IMPORT_INTERVAL:
            last_import["at"] = now
            db.import_from_dir(db_path, data_dir)

    def grid_context(request: Request) -> dict:
        refresh()
        hosts = db.latest_states(db_path)
        return {"request": request, "hosts": hosts, "overall": store.overall_status(hosts)}

    # -- pages ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request, "index.html", grid_context(request))

    @app.get("/fragment/grid", response_class=HTMLResponse)
    def grid_fragment(request: Request):
        """The tile grid alone — htmx swaps this in every 60s."""
        return templates.TemplateResponse(request, "_grid.html", grid_context(request))

    @app.get("/host/{host}/{tool}", response_class=HTMLResponse)
    def detail(request: Request, host: str, tool: str):
        refresh()
        state = db.latest_state(db_path, host, tool)
        if state is None:
            raise HTTPException(status_code=404, detail=f"No results for {host}/{tool}")
        return templates.TemplateResponse(request, "detail.html", {
            "request": request,
            "state": state,
            "history": db.history(db_path, host, tool),
            "series": db.series(db_path, host, tool),
            "pretty_payload": json.dumps(state.payload, indent=2),
        })

    # -- API -----------------------------------------------------------------

    @app.post("/api/v1/ingest/{host}/{tool}")
    async def ingest(request: Request, host: str, tool: str):
        if not ingest_tokens:
            raise HTTPException(status_code=503,
                                detail="Ingest is disabled: no DASHBOARD_TOKENS configured.")
        if not NAME_RE.match(host) or not NAME_RE.match(tool):
            raise HTTPException(status_code=400,
                                detail="host and tool must be 1-64 chars of [A-Za-z0-9._-], "
                                       "starting alphanumeric.")
        auth = request.headers.get("authorization", "")
        presented = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not db.token_allows(ingest_tokens, host, presented):
            raise HTTPException(status_code=401, detail="Bad or missing bearer token for this host.")

        body = await request.body()
        if len(body) > MAX_INGEST_BYTES:
            raise HTTPException(status_code=413, detail="Result payload too large.")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Body is not valid JSON.")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object.")

        ts = time.strftime("%Y%m%d-%H%M%S")
        inserted = db.insert_result(db_path, host, tool, ts, payload)
        status, headline = store.derive_status(tool, payload)
        return JSONResponse({"stored": inserted, "host": host, "tool": tool,
                             "timestamp": ts, "status": status, "headline": headline},
                            status_code=201 if inserted else 200)

    @app.get("/api/v1/summary")
    def api_summary():
        refresh()
        hosts = db.latest_states(db_path)
        return JSONResponse({
            "overall": store.overall_status(hosts),
            "hosts": {
                host: [
                    {"tool": s.tool, "status": s.status, "headline": s.headline,
                     "timestamp": s.timestamp, "age_seconds": round(s.age_seconds), "stale": s.stale}
                    for s in states
                ]
                for host, states in hosts.items()
            },
        })

    @app.get("/api/v1/series/{host}/{tool}")
    def api_series(host: str, tool: str):
        return JSONResponse({"host": host, "tool": tool, "points": db.series(db_path, host, tool)})

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "db": db_path, "data_dir": data_dir,
                "ingest_enabled": bool(ingest_tokens),
                "notify_enabled": bool(notify_slack or notify_webhook)}

    return app


app = create_app(
    data_dir=os.environ.get("DASHBOARD_DATA_DIR", "/data"),
    db_path=os.environ.get("DASHBOARD_DB", "/db/dashboard.sqlite3"),
    notify_slack=os.environ.get("DASHBOARD_NOTIFY_SLACK"),
    notify_webhook=os.environ.get("DASHBOARD_NOTIFY_WEBHOOK"),
)
