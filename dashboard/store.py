# SPDX-License-Identifier: MIT
"""store.py — filesystem result store for the dashboard.

The collectors (cron/systemd timers running the toolkit's tools with JSON
output) write timestamped files into:

    <data_dir>/<host>/<tool>/<YYYYmmdd-HHMMSS>.json

This module scans that tree and derives a per-tool status the UI can
render. Standard library only — the web layer (app.py) is the only part of
the repo with third-party dependencies, and this split keeps everything
testable without them.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

RESULT_FILE_RE = re.compile(r"^(\d{8}-\d{6})\.json$")

# How old the newest result may be before the tile is marked stale.
DEFAULT_STALE_AFTER = 26 * 3600           # daily collectors with slack
STALE_AFTER = {
    "endpoint-watchdog": 15 * 60,         # runs every few minutes
}

STATUS_RANK = {"ok": 0, "unknown": 1, "warn": 2, "crit": 3}


@dataclass
class ToolState:
    host: str
    tool: str
    status: str                 # ok | warn | crit | unknown
    headline: str               # one-line summary for the tile
    timestamp: str              # from the result filename
    age_seconds: float
    stale: bool
    payload: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Status derivation per tool. Each adapter takes the tool's JSON payload and
# returns (status, headline).
# ---------------------------------------------------------------------------

def _from_severity_summary(payload: dict) -> tuple[str, str]:
    s = payload.get("summary", {})
    high, medium, low = s.get("high", 0), s.get("medium", 0), s.get("low", 0)
    status = "crit" if high else ("warn" if medium else "ok")
    return status, f"{high} high / {medium} medium / {low} low"


def _from_watchdog(payload: dict) -> tuple[str, str]:
    s = payload.get("summary", {})
    ok, warn, fail = s.get("ok", 0), s.get("warn", 0), s.get("fail", 0)
    status = "crit" if fail else ("warn" if warn else "ok")
    return status, f"{ok} ok / {warn} warn / {fail} fail"


def _from_db_backup(payload: dict) -> tuple[str, str]:
    verify = payload.get("verify", {})
    backup = payload.get("backup") or {}
    size = backup.get("size_bytes")
    headline = f"{size / 1024**2:.1f} MiB, verify: {verify.get('mode', '?')}" if size else "no new backup"
    if verify.get("ok") is False:
        return "crit", f"VERIFICATION FAILED ({verify.get('mode', '?')})"
    return "ok", headline


def _from_docker_cleanup(payload: dict) -> tuple[str, str]:
    reclaimed = payload.get("total_reclaimed_human", "0B")
    errors = sum(1 for a in payload.get("actions", []) if a.get("error"))
    if errors:
        return "warn", f"{errors} prune action(s) reported errors"
    prefix = "[dry run] " if payload.get("dry_run") else ""
    return "ok", f"{prefix}reclaimed {reclaimed}"


TOOL_ADAPTERS = {
    "k8s-resource-auditor": _from_severity_summary,
    "sys-triage": _from_severity_summary,
    "host-hardening-check": _from_severity_summary,
    "endpoint-watchdog": _from_watchdog,
    "db-backup-rotate": _from_db_backup,
    "docker-cleanup": _from_docker_cleanup,
}


def derive_status(tool: str, payload: dict) -> tuple[str, str]:
    adapter = TOOL_ADAPTERS.get(tool)
    if adapter:
        try:
            return adapter(payload)
        except (TypeError, ValueError, AttributeError):
            return "unknown", "unrecognized payload shape"
    # Generic fallback: many tools share the severity-summary convention.
    summary = payload.get("summary")
    if isinstance(summary, dict):
        if "high" in summary:
            return _from_severity_summary(payload)
        if "fail" in summary:
            return _from_watchdog(payload)
    return "unknown", "no adapter for this tool — showing raw JSON"


# ---------------------------------------------------------------------------
# Filesystem scanning
# ---------------------------------------------------------------------------

def parse_result_name(filename: str) -> str | None:
    """Return the timestamp part of a result filename, or None for foreign files."""
    m = RESULT_FILE_RE.match(filename)
    return m.group(1) if m else None


def _result_files(tool_dir: str) -> list[str]:
    """Result filenames in a tool directory, newest first (names sort chronologically)."""
    try:
        names = os.listdir(tool_dir)
    except OSError:
        return []
    return sorted((n for n in names if parse_result_name(n)), reverse=True)


def load_result(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_tool_state(data_dir: str, host: str, tool: str, now: float | None = None) -> ToolState | None:
    """State of a single host/tool from its newest result file."""
    now = time.time() if now is None else now
    tool_dir = os.path.join(data_dir, host, tool)
    names = _result_files(tool_dir)
    if not names:
        return None
    newest = names[0]
    path = os.path.join(tool_dir, newest)
    payload = load_result(path)
    if payload is None:
        status, headline = "unknown", "latest result file is not valid JSON"
        payload = {}
    else:
        status, headline = derive_status(tool, payload)
    try:
        age = max(0.0, now - os.path.getmtime(path))
    except OSError:
        age = 0.0
    return ToolState(
        host=host, tool=tool, status=status, headline=headline,
        timestamp=parse_result_name(newest) or "",
        age_seconds=age,
        stale=age > STALE_AFTER.get(tool, DEFAULT_STALE_AFTER),
        payload=payload,
    )


def scan(data_dir: str, now: float | None = None) -> dict[str, list[ToolState]]:
    """All hosts and tools under data_dir: {host: [ToolState, ...]}, with each
    host's tools sorted worst-status-first so problems surface at the top."""
    hosts: dict[str, list[ToolState]] = {}
    try:
        host_names = sorted(os.listdir(data_dir))
    except OSError:
        return {}
    for host in host_names:
        host_dir = os.path.join(data_dir, host)
        if not os.path.isdir(host_dir):
            continue
        states = []
        for tool in sorted(os.listdir(host_dir)):
            if not os.path.isdir(os.path.join(host_dir, tool)):
                continue
            state = load_tool_state(data_dir, host, tool, now)
            if state:
                states.append(state)
        if states:
            states.sort(key=lambda s: (-STATUS_RANK[s.status], not s.stale, s.tool))
            hosts[host] = states
    return hosts


def history(data_dir: str, host: str, tool: str, limit: int = 50) -> list[dict]:
    """Recent results for the detail page: [{timestamp, status, headline}, ...],
    newest first."""
    tool_dir = os.path.join(data_dir, host, tool)
    entries = []
    for name in _result_files(tool_dir)[:limit]:
        payload = load_result(os.path.join(tool_dir, name))
        if payload is None:
            status, headline = "unknown", "invalid JSON"
        else:
            status, headline = derive_status(tool, payload)
        entries.append({"timestamp": parse_result_name(name), "status": status, "headline": headline})
    return entries


def overall_status(hosts: dict[str, list[ToolState]]) -> str:
    """Worst status across the board, for the page title / favicon dot."""
    worst = "ok"
    for states in hosts.values():
        for s in states:
            if STATUS_RANK[s.status] > STATUS_RANK[worst]:
                worst = s.status
    return worst


def format_age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 129600:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def format_timestamp(ts: str) -> str:
    try:
        return datetime.strptime(ts, "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts
