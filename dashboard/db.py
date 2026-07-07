# SPDX-License-Identifier: MIT
"""db.py — SQLite result store for the dashboard (phase 2).

The database is the single source of truth for what the UI shows. Results
get in two ways:

  - HTTP ingest: remote hosts POST their JSON to the dashboard (app.py),
    authenticated with per-host bearer tokens
  - directory import: anything the phase-1 file collectors wrote into the
    data directory is imported idempotently (unique on host/tool/timestamp),
    so NFS/rsync setups keep working unchanged

Standard library only, like store.py — the web layer stays the only place
with third-party dependencies. Connections are opened per operation, which
is plenty at this scale and sidesteps sqlite threading rules.
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
import time

import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    host TEXT NOT NULL,
    tool TEXT NOT NULL,
    ts TEXT NOT NULL,              -- collector timestamp, YYYYmmdd-HHMMSS
    status TEXT NOT NULL,
    headline TEXT NOT NULL,
    payload TEXT NOT NULL,         -- raw result JSON
    created_at REAL NOT NULL       -- unix epoch, when the row was stored
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_results_host_tool_ts ON results(host, tool, ts);
CREATE INDEX IF NOT EXISTS ix_results_lookup ON results(host, tool, created_at DESC);
"""

DEFAULT_KEEP = 1000   # rows kept per host/tool


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def insert_result(db_path: str, host: str, tool: str, ts: str, payload: dict,
                  created_at: float | None = None, keep: int = DEFAULT_KEEP) -> bool:
    """Store one result; returns False if this host/tool/timestamp already
    exists (idempotent import). Prunes history beyond `keep` rows."""
    status, headline = store.derive_status(tool, payload)
    with connect(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO results (host, tool, ts, status, headline, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (host, tool, ts, status, headline, json.dumps(payload),
                 time.time() if created_at is None else created_at),
            )
        except sqlite3.IntegrityError:
            return False
        conn.execute(
            "DELETE FROM results WHERE host = ? AND tool = ? AND id NOT IN ("
            "  SELECT id FROM results WHERE host = ? AND tool = ?"
            "  ORDER BY created_at DESC LIMIT ?)",
            (host, tool, host, tool, keep),
        )
    return True


def latest_states(db_path: str, now: float | None = None) -> dict[str, list[store.ToolState]]:
    """Newest result per host/tool as ToolStates, shaped like store.scan()."""
    now = time.time() if now is None else now
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM results r1 WHERE NOT EXISTS ("
            "  SELECT 1 FROM results r2 WHERE r2.host = r1.host AND r2.tool = r1.tool"
            "  AND (r2.created_at > r1.created_at"
            "       OR (r2.created_at = r1.created_at AND r2.id > r1.id)))",
        ).fetchall()

    hosts: dict[str, list[store.ToolState]] = {}
    for row in rows:
        age = max(0.0, now - row["created_at"])
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            payload = {}
        hosts.setdefault(row["host"], []).append(store.ToolState(
            host=row["host"], tool=row["tool"], status=row["status"],
            headline=row["headline"], timestamp=row["ts"], age_seconds=age,
            stale=age > store.STALE_AFTER.get(row["tool"], store.DEFAULT_STALE_AFTER),
            payload=payload,
        ))
    for states in hosts.values():
        states.sort(key=lambda s: (-store.STATUS_RANK[s.status], not s.stale, s.tool))
    return dict(sorted(hosts.items()))


def latest_state(db_path: str, host: str, tool: str) -> store.ToolState | None:
    for state in latest_states(db_path).get(host, []):
        if state.tool == tool:
            return state
    return None


def history(db_path: str, host: str, tool: str, limit: int = 50) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ts, status, headline FROM results WHERE host = ? AND tool = ? "
            "ORDER BY created_at DESC LIMIT ?", (host, tool, limit),
        ).fetchall()
    return [{"timestamp": r["ts"], "status": r["status"], "headline": r["headline"]} for r in rows]


# ---------------------------------------------------------------------------
# Trend series for the detail-page charts. Extracted from stored payloads at
# read time — a few hundred small JSON rows, no separate metrics table needed.
# ---------------------------------------------------------------------------

def _point_severity(payload: dict) -> dict | None:
    s = payload.get("summary")
    if isinstance(s, dict) and "high" in s:
        return {"high": s.get("high", 0), "medium": s.get("medium", 0), "low": s.get("low", 0)}
    return None


def _point_watchdog(payload: dict) -> dict | None:
    s = payload.get("summary")
    if not isinstance(s, dict) or "fail" not in s:
        return None
    point = {"ok": s.get("ok", 0), "warn": s.get("warn", 0), "fail": s.get("fail", 0)}
    latencies = [r.get("latency_ms") for r in payload.get("results", [])
                 if isinstance(r, dict) and isinstance(r.get("latency_ms"), (int, float))]
    if latencies:
        point["avg_latency_ms"] = round(sum(latencies) / len(latencies), 1)
    return point


def _point_db_backup(payload: dict) -> dict | None:
    backup = payload.get("backup")
    if not isinstance(backup, dict):
        return None
    size = backup.get("size_bytes")
    if not isinstance(size, (int, float)):
        return None
    verify = payload.get("verify") or {}
    return {"size_mib": round(size / 1024**2, 2),
            "verify_ok": 1 if verify.get("ok") else 0}


SERIES_EXTRACTORS = [_point_watchdog, _point_db_backup, _point_severity]


def series(db_path: str, host: str, tool: str, limit: int = 200) -> list[dict]:
    """Chart points, oldest first: [{'t': ts, ...metric fields}, ...].
    Field names depend on the tool family; an empty list means 'nothing
    chartable' and the UI hides the chart."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ts, payload FROM results WHERE host = ? AND tool = ? "
            "ORDER BY created_at DESC LIMIT ?", (host, tool, limit),
        ).fetchall()

    points = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            continue
        for extractor in SERIES_EXTRACTORS:
            point = extractor(payload)
            if point is not None:
                point["t"] = store.format_timestamp(row["ts"])
                points.append(point)
                break
    points.reverse()
    return points


# ---------------------------------------------------------------------------
# Directory import: phase-1 file collectors keep working — anything in the
# data directory that isn't in the database yet gets imported on scan.
# ---------------------------------------------------------------------------

def import_from_dir(db_path: str, data_dir: str) -> int:
    """Idempotently import result files into the database. Returns how many
    new rows were added. Uses the file's mtime as created_at so ages and
    staleness stay truthful for rsync'd results."""
    imported = 0
    try:
        host_names = sorted(os.listdir(data_dir))
    except OSError:
        return 0
    for host in host_names:
        host_dir = os.path.join(data_dir, host)
        if not os.path.isdir(host_dir):
            continue
        for tool in sorted(os.listdir(host_dir)):
            tool_dir = os.path.join(host_dir, tool)
            if not os.path.isdir(tool_dir):
                continue
            for name in os.listdir(tool_dir):
                ts = store.parse_result_name(name)
                if ts is None:
                    continue
                path = os.path.join(tool_dir, name)
                payload = store.load_result(path)
                if payload is None:
                    continue
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    mtime = time.time()
                if insert_result(db_path, host, tool, ts, payload, created_at=mtime):
                    imported += 1
    return imported


# ---------------------------------------------------------------------------
# Ingest tokens: "host:token" pairs, comma-separated in an env var or
# one per line in a file. "*" as the host is a shared any-host token.
# ---------------------------------------------------------------------------

def parse_tokens(text: str) -> dict[str, str]:
    tokens = {}
    for entry in text.replace("\n", ",").split(","):
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        host, sep, token = entry.partition(":")
        if sep and host.strip() and token.strip():
            tokens[host.strip()] = token.strip()
    return tokens


def token_allows(tokens: dict[str, str], host: str, presented: str) -> bool:
    if not presented:
        return False
    expected = tokens.get(host) or tokens.get("*")
    return expected is not None and hmac.compare_digest(presented, expected)
