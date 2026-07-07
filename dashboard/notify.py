# SPDX-License-Identifier: MIT
"""notify.py — server-side notifications for tile state changes.

The dashboard is the only component that can notice a *silent* collector: a
dead cron on a monitored host produces no failing run, no alert, nothing —
just a tile quietly going stale. This module closes that loop: a background
task (started by app.py) periodically imports fresh file results, computes
which tiles changed state (db.notify_transitions), and sends one message per
transition into or out of crit/stale — including the recovery.

Two destinations, both optional (set at least one to enable the loop):

    DASHBOARD_NOTIFY_SLACK     Slack-compatible incoming-webhook URL
                               (Slack, Mattermost, Discord's /slack endpoint)
    DASHBOARD_NOTIFY_WEBHOOK   generic URL; receives the transition as JSON

Standard library only, same retry/backoff shape as the tools' notifiers.
"""

from __future__ import annotations

import json
import time
import urllib.request

import db


def post_json(url: str, payload: dict, timeout: int = 10, retries: int = 2) -> bool:
    data = json.dumps(payload).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
            return True
        except Exception as e:  # transient network/HTTP error — retry with backoff
            last_err = e
            if attempt < retries:
                time.sleep(2 ** attempt)
    print(f"notify: POST to {url} failed after {retries + 1} attempt(s): {last_err}", flush=True)
    return False


def build_text(t: dict) -> str:
    """One-line human message for a transition."""
    current, previous = t["current"], t["previous"]
    if db.NOTIFY_RANK[current] < db.NOTIFY_RANK[previous]:
        label = "RECOVERED" if current == "ok" else current.upper()
        return (f"[{label}] {t['host']}/{t['tool']}: {t['headline']} (was {previous})")
    if current == "stale":
        return (f"[STALE] {t['host']}/{t['tool']}: no results arriving — "
                f"is the collector still running? (was {previous})")
    return f"[{current.upper()}] {t['host']}/{t['tool']}: {t['headline']} (was {previous})"


def send_transition(t: dict, slack_url: str | None, webhook_url: str | None) -> None:
    text = build_text(t)
    if slack_url:
        post_json(slack_url, {"text": text})
    if webhook_url:
        post_json(webhook_url, {**t, "text": text})


def check_and_notify(db_path: str, data_dir: str,
                     slack_url: str | None, webhook_url: str | None) -> list[dict]:
    """One sweep: import fresh file results, compute transitions, send them.
    Returns the transitions (mainly for tests/observability)."""
    db.import_from_dir(db_path, data_dir)
    transitions = db.notify_transitions(db_path)
    for t in transitions:
        send_transition(t, slack_url, webhook_url)
    return transitions
