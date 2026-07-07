# SPDX-License-Identifier: MIT
"""app.py — the dashboard web layer (FastAPI + Jinja2 + htmx).

Read-only by design: it renders the JSON results that scheduled collectors
wrote into DASHBOARD_DATA_DIR (see collect.sh) and never executes a tool
itself. All logic lives in store.py; this file is routes and templates.

Run locally:
    DASHBOARD_DATA_DIR=./data uvicorn app:app --reload

Or via the Dockerfile / docker-compose.yml in this directory.
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app(data_dir: str) -> FastAPI:
    app = FastAPI(title="devops-toolkit dashboard", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
    templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
    templates.env.filters["age"] = store.format_age
    templates.env.filters["ts"] = store.format_timestamp

    def grid_context(request: Request) -> dict:
        hosts = store.scan(data_dir)
        return {"request": request, "hosts": hosts, "overall": store.overall_status(hosts)}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request, "index.html", grid_context(request))

    @app.get("/fragment/grid", response_class=HTMLResponse)
    def grid_fragment(request: Request):
        """The tile grid alone — htmx swaps this in every 60s."""
        return templates.TemplateResponse(request, "_grid.html", grid_context(request))

    @app.get("/host/{host}/{tool}", response_class=HTMLResponse)
    def detail(request: Request, host: str, tool: str):
        state = store.load_tool_state(data_dir, host, tool)
        if state is None:
            raise HTTPException(status_code=404, detail=f"No results for {host}/{tool}")
        return templates.TemplateResponse(request, "detail.html", {
            "request": request,
            "state": state,
            "history": store.history(data_dir, host, tool),
            "pretty_payload": json.dumps(state.payload, indent=2),
        })

    @app.get("/api/v1/summary")
    def api_summary():
        hosts = store.scan(data_dir)
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

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "data_dir": data_dir, "data_dir_exists": os.path.isdir(data_dir)}

    return app


app = create_app(os.environ.get("DASHBOARD_DATA_DIR", "/data"))
