#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""watchdog.py — check HTTP endpoints and TCP ports, alert on state changes.

Probes a list of endpoints and reports on each:

  - HTTP(S): status code (against an expected code or "anything below 400"),
    response latency against a warn threshold, optional body substring match
  - TLS: days until certificate expiry, warning before it becomes an outage
  - TCP: plain connect check for things that aren't HTTP (databases, SMTP...)

Checks run in parallel, results come out as a table or JSON, and the exit
code says whether anything is down — so a bare cron line already gives you
monitoring. Add notifiers (Slack, Discord, generic webhook, email) and it
alerts on STATE CHANGES only: one message when something goes down, one when
it recovers, silence otherwise — no repeat spam every minute while an
endpoint stays broken.

No external dependencies — Python standard library only.

Usage:
    python3 watchdog.py --config config.json [OPTIONS]
    python3 watchdog.py --url https://example.com [--url ...]   # quick mode

Examples:
    # Quick one-off check of a couple of URLs
    python3 watchdog.py --url https://example.com --url https://api.example.com/healthz

    # Full config: endpoints, thresholds, notifiers
    python3 watchdog.py --config config.json

    # Cron: table to a log, alerts to Slack on state changes
    python3 watchdog.py --config config.json --output json

    # See what would be alerted without sending anything
    python3 watchdog.py --config config.json --dry-run

Exit codes:
    0  everything OK (or only findings below --fail-on)
    1  at least one check at/above the --fail-on level (default: fail)
    2  bad usage / config error
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import smtplib
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.mime.text import MIMEText

USER_AGENT = "endpoint-watchdog/1.0 (+https://github.com/mkrasu/devops-toolkit)"

# Check status ordering; "worst wins" when one endpoint has several findings.
STATUS_ORDER = {"ok": 0, "warn": 1, "fail": 2}
# Map a check status onto the notifier severity scale shared with log-alert.
STATUS_SEVERITY = {"ok": "low", "warn": "medium", "fail": "high"}
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}

COLORS = {"ok": "\033[32m", "warn": "\033[33m", "fail": "\033[31m", "reset": "\033[0m"}

ENDPOINT_DEFAULTS = {
    "check": "http",
    "method": "GET",
    "timeout_seconds": 10,
    "warn_latency_ms": 2000,
    "cert_warn_days": 21,
    "verify_tls": True,
    "expect_status": None,   # None = any status below 400
    "expect_body": None,
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def die(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Config loading (JSON, with ${ENV_VAR} substitution — same scheme as
# log-tailer-alert, so notifier blocks can be copied between the two configs)
# ---------------------------------------------------------------------------

ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def substitute_env(value):
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            var = m.group(1)
            if var not in os.environ:
                die(f"Config references ${{{var}}} but that environment variable is not set.")
            return os.environ[var]
        return ENV_VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_env(v) for v in value]
    return value


def load_config(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        die(f"Config file not found: {path}")
    except json.JSONDecodeError as e:
        die(f"Config file is not valid JSON: {e}")
    # Disabled notifiers don't need their secrets to exist (see log-alert).
    notifiers = raw.get("notifiers")
    if isinstance(notifiers, dict):
        raw["notifiers"] = {
            name: cfg for name, cfg in notifiers.items()
            if not isinstance(cfg, dict) or cfg.get("enabled", True)
        }
    return substitute_env(raw)


def normalize_endpoint(cfg: dict, defaults: dict) -> dict:
    """Merge an endpoint entry with config-level defaults and built-ins,
    fill in the derived fields, and validate the essentials."""
    ep = {**ENDPOINT_DEFAULTS, **defaults, **cfg}

    if ep["check"] not in ("http", "tcp"):
        die(f"Endpoint '{cfg}': unknown check type '{ep['check']}' (use 'http' or 'tcp').")
    if ep["check"] == "http":
        if not ep.get("url"):
            die(f"HTTP endpoint needs a 'url': {cfg}")
        ep.setdefault("name", ep["url"])
        ep["target"] = ep["url"]
    else:
        if not ep.get("host") or not ep.get("port"):
            die(f"TCP endpoint needs 'host' and 'port': {cfg}")
        ep.setdefault("name", f"{ep['host']}:{ep['port']}")
        ep["target"] = f"tcp://{ep['host']}:{ep['port']}"

    expect = ep["expect_status"]
    if isinstance(expect, int):
        ep["expect_status"] = [expect]
    elif expect is not None and not isinstance(expect, list):
        die(f"Endpoint '{ep['name']}': expect_status must be an integer or a list.")
    return ep


# ---------------------------------------------------------------------------
# Probes: talk to the network, return raw facts. Evaluation happens
# separately so the pass/warn/fail logic is unit-testable without sockets.
# ---------------------------------------------------------------------------

def probe_http(ep: dict) -> dict:
    """Fetch the URL. Returns {'status_code', 'latency_ms', 'body', 'error'} —
    an HTTP error status is a result, not an error; 'error' means we never
    got a response at all."""
    req = urllib.request.Request(ep["url"], method=ep["method"],
                                 headers={"User-Agent": USER_AGENT})
    ctx = ssl.create_default_context()
    if not ep["verify_tls"]:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    need_body = ep["expect_body"] is not None
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=ep["timeout_seconds"], context=ctx) as resp:
            body = resp.read(65536).decode("utf-8", errors="replace") if need_body else None
            code = resp.status
    except urllib.error.HTTPError as e:
        body = e.read(65536).decode("utf-8", errors="replace") if need_body else None
        code = e.code
    except Exception as e:  # DNS, refused, timeout, TLS failure, ...
        return {"status_code": None, "latency_ms": (time.monotonic() - start) * 1000,
                "body": None, "error": str(e) or type(e).__name__}
    return {"status_code": code, "latency_ms": (time.monotonic() - start) * 1000,
            "body": body, "error": None}


def probe_tcp(ep: dict) -> dict:
    start = time.monotonic()
    try:
        with socket.create_connection((ep["host"], ep["port"]), timeout=ep["timeout_seconds"]):
            pass
    except Exception as e:
        return {"latency_ms": (time.monotonic() - start) * 1000,
                "error": str(e) or type(e).__name__}
    return {"latency_ms": (time.monotonic() - start) * 1000, "error": None}


def probe_cert_expiry(host: str, port: int, timeout: float) -> float:
    """Return the certificate's notAfter as a Unix timestamp. Raises on any
    connection/TLS problem (the HTTP probe reports those on its own)."""
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    return ssl.cert_time_to_seconds(cert["notAfter"])


# ---------------------------------------------------------------------------
# Evaluation (pure logic, unit-tested)
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    target: str
    status: str            # ok | warn | fail
    latency_ms: float | None
    message: str

    def row(self) -> list[str]:
        latency = f"{self.latency_ms:.0f}ms" if self.latency_ms is not None else "-"
        return [self.status.upper(), self.name, latency, self.message]


def evaluate_http(ep: dict, outcome: dict) -> CheckResult:
    issues: list[tuple[str, str]] = []  # (status, message)

    if outcome["error"] is not None:
        issues.append(("fail", f"request failed: {outcome['error']}"))
    else:
        code = outcome["status_code"]
        expected = ep["expect_status"]
        if expected is not None:
            if code not in expected:
                want = "/".join(str(c) for c in expected)
                issues.append(("fail", f"HTTP {code} (expected {want})"))
        elif code >= 400:
            issues.append(("fail", f"HTTP {code}"))
        if ep["expect_body"] is not None and outcome["body"] is not None:
            if ep["expect_body"] not in outcome["body"]:
                issues.append(("fail", f"body does not contain '{ep['expect_body']}'"))
        if outcome["latency_ms"] > ep["warn_latency_ms"]:
            issues.append(("warn", f"slow: {outcome['latency_ms']:.0f}ms > {ep['warn_latency_ms']}ms"))

    return _combine(ep, issues, outcome["latency_ms"],
                    ok_message=f"HTTP {outcome['status_code']} in {outcome['latency_ms']:.0f}ms")


def evaluate_tcp(ep: dict, outcome: dict) -> CheckResult:
    issues = []
    if outcome["error"] is not None:
        issues.append(("fail", f"connect failed: {outcome['error']}"))
    return _combine(ep, issues, outcome["latency_ms"],
                    ok_message=f"connected in {outcome['latency_ms']:.0f}ms")


def evaluate_cert(ep: dict, expiry_ts: float, now_ts: float) -> tuple[str, str] | None:
    """Return an (status, message) issue for the certificate, or None if fine."""
    days_left = (expiry_ts - now_ts) / 86400
    if days_left < 0:
        return ("fail", f"certificate EXPIRED {abs(days_left):.0f}d ago")
    if days_left <= ep["cert_warn_days"]:
        return ("warn", f"certificate expires in {days_left:.0f}d")
    return None


def _combine(ep: dict, issues: list[tuple[str, str]], latency_ms: float | None,
             ok_message: str) -> CheckResult:
    if not issues:
        return CheckResult(ep["name"], ep["target"], "ok", latency_ms, ok_message)
    status = max((s for s, _ in issues), key=lambda s: STATUS_ORDER[s])
    return CheckResult(ep["name"], ep["target"], status, latency_ms,
                       "; ".join(m for _, m in issues))


def run_check(ep: dict) -> CheckResult:
    if ep["check"] == "tcp":
        return evaluate_tcp(ep, probe_tcp(ep))

    result = evaluate_http(ep, probe_http(ep))

    # Certificate expiry: only meaningful for verified https, and only worth
    # probing when the endpoint itself was reachable.
    if (ep["url"].startswith("https://") and ep["verify_tls"]
            and ep["cert_warn_days"] > 0 and result.status != "fail"):
        parsed = urllib.parse.urlsplit(ep["url"])
        try:
            expiry = probe_cert_expiry(parsed.hostname, parsed.port or 443, ep["timeout_seconds"])
        except Exception as e:
            issue = ("warn", f"could not read certificate: {e}")
        else:
            issue = evaluate_cert(ep, expiry, time.time())
        if issue:
            status = max((result.status, issue[0]), key=lambda s: STATUS_ORDER[s])
            message = issue[1] if result.status == "ok" else f"{result.message}; {issue[1]}"
            result = CheckResult(result.name, result.target, status, result.latency_ms, message)
    return result


# ---------------------------------------------------------------------------
# State tracking: alert on transitions, not on every run
# ---------------------------------------------------------------------------

def default_state_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "endpoint-watchdog", "state.json")


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: str, state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        log(f"Warning: could not save state to {path}: {e}")


def transitions(state: dict, results: list[CheckResult], now: float) -> list[tuple[CheckResult, str]]:
    """Update `state` in place and return [(result, previous_status)] for
    every endpoint whose status changed. An endpoint never seen before
    counts as previously 'ok', so a brand-new broken endpoint still alerts
    while a brand-new healthy one stays silent."""
    changed = []
    for r in results:
        prev = state.get(r.name, {}).get("status", "ok")
        if r.status != prev:
            changed.append((r, prev))
            state[r.name] = {"status": r.status, "since": now}
        elif r.name not in state:
            state[r.name] = {"status": r.status, "since": now}
    return changed


def transition_text(result: CheckResult, prev: str, since: float | None) -> str:
    arrow = f"{prev.upper()} -> {result.status.upper()}"
    duration = ""
    if since is not None:
        mins = (time.time() - since) / 60
        duration = f" (was {prev} for {mins / 60:.1f}h)" if mins >= 90 else f" (was {prev} for {mins:.0f}m)"
    return f"[{arrow}] {result.name}: {result.message}{duration}"


# ---------------------------------------------------------------------------
# Notifiers (same shapes and config schema as log-tailer-alert)
# ---------------------------------------------------------------------------

def http_post_json(url: str, payload: dict, timeout: int = 10, retries: int = 2) -> None:
    data = json.dumps(payload).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
            return
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 ** attempt)
    log(f"Notifier error (HTTP POST to {url}) after {retries + 1} attempt(s): {last_err}")


def notify_slack(cfg: dict, text: str, payload: dict) -> None:
    http_post_json(cfg["webhook_url"], {"text": text})


def notify_discord(cfg: dict, text: str, payload: dict) -> None:
    http_post_json(cfg["webhook_url"], {"content": text})


def notify_webhook(cfg: dict, text: str, payload: dict) -> None:
    http_post_json(cfg["url"], payload)


def notify_email(cfg: dict, text: str, payload: dict) -> None:
    username = cfg.get("username")
    password = cfg.get("password")
    if username and not password:
        log("Notifier error (email): 'username' set but 'password' missing in config.")
        return
    msg = MIMEText(text)
    msg["Subject"] = f"[endpoint-watchdog] {payload['name']}: {payload['previous_status']} -> {payload['status']}"
    msg["From"] = cfg["from_addr"]
    msg["To"] = ", ".join(cfg["to_addrs"])
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with smtplib.SMTP(cfg["smtp_host"], cfg.get("smtp_port", 587), timeout=10) as server:
                if cfg.get("use_tls", True):
                    server.starttls()
                if username:
                    server.login(username, password)
                server.sendmail(cfg["from_addr"], cfg["to_addrs"], msg.as_string())
            return
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(2 ** attempt)
    log(f"Notifier error (email) after 3 attempt(s): {last_err}")


NOTIFIER_DISPATCH = {
    "slack": notify_slack,
    "discord": notify_discord,
    "webhook": notify_webhook,
    "email": notify_email,
}


def send_notifications(text: str, payload: dict, severity: str, notifiers_cfg: dict) -> None:
    for name, cfg in notifiers_cfg.items():
        if not cfg.get("enabled", True):
            continue
        floor = cfg.get("min_severity")
        if floor and SEVERITY_ORDER.get(severity, 0) < SEVERITY_ORDER.get(floor, 0):
            continue
        fn = NOTIFIER_DISPATCH.get(name)
        if fn is None:
            log(f"Unknown notifier in config: {name}")
            continue
        fn(cfg, text, payload)


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------

def summarize(results: list[CheckResult]) -> dict:
    return {s: sum(1 for r in results if r.status == s) for s in ("ok", "warn", "fail")}


def render_table(results: list[CheckResult], color: bool) -> str:
    headers = ["STATUS", "NAME", "LATENCY", "MESSAGE"]
    ordered = sorted(results, key=lambda r: (-STATUS_ORDER[r.status], r.name))
    rows = [r.row() for r in ordered]
    widths = [max(len(h), *(len(row[i]) for row in rows)) if rows else len(h)
              for i, h in enumerate(headers)]

    def fmt(row: list[str], status: str | None = None) -> str:
        line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        if color and status and COLORS.get(status):
            return f"{COLORS[status]}{line}{COLORS['reset']}"
        return line

    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(row, ordered[i].status) for i, row in enumerate(rows)]
    counts = summarize(results)
    lines.append(f"\n{len(results)} endpoint(s): {counts['ok']} ok, "
                 f"{counts['warn']} warn, {counts['fail']} fail")
    return "\n".join(lines)


def render_json(results: list[CheckResult]) -> str:
    return json.dumps({
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": summarize(results),
        "results": [
            {"name": r.name, "target": r.target, "status": r.status,
             "latency_ms": round(r.latency_ms, 1) if r.latency_ms is not None else None,
             "message": r.message}
            for r in sorted(results, key=lambda r: (-STATUS_ORDER[r.status], r.name))
        ],
    }, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Check HTTP endpoints and TCP ports; alert on state changes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", help="JSON config with endpoints/defaults/notifiers")
    p.add_argument("--url", action="append", default=[], metavar="URL",
                   help="Check this URL with default settings (repeatable; no config needed)")
    p.add_argument("--output", choices=["table", "json"], default="table")
    p.add_argument("--fail-on", choices=["fail", "warn", "none"], default="fail",
                   help="Exit 1 if any check is at/above this level (default: fail)")
    p.add_argument("--timeout", type=float, metavar="SEC",
                   help="Override the default per-check timeout")
    p.add_argument("--cert-warn-days", type=int, metavar="N",
                   help="Override the certificate expiry warning threshold")
    p.add_argument("--state-file", help=f"Where transition state lives (default: {default_state_path()})")
    p.add_argument("--no-state", action="store_true",
                   help="Don't read/write state; report only, no transition alerts")
    p.add_argument("--dry-run", action="store_true",
                   help="Print transition alerts but don't send notifications")
    args = p.parse_args(argv)
    if not args.config and not args.url:
        p.error("nothing to check: pass --config and/or --url")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    config = load_config(args.config) if args.config else {}
    defaults = dict(config.get("defaults", {}))
    if args.timeout:
        defaults["timeout_seconds"] = args.timeout
    if args.cert_warn_days is not None:
        defaults["cert_warn_days"] = args.cert_warn_days

    entries = list(config.get("endpoints", [])) + [{"url": u} for u in args.url]
    endpoints = [normalize_endpoint(e, defaults) for e in entries]
    if not endpoints:
        die("Config has no 'endpoints' and no --url was given.")
    names = [ep["name"] for ep in endpoints]
    if len(set(names)) != len(names):
        die("Endpoint names must be unique (state tracking is keyed by name).")

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(endpoints))) as pool:
        results = list(pool.map(run_check, endpoints))

    print(render_table(results, color=sys.stdout.isatty()) if args.output == "table"
          else render_json(results))

    if not args.no_state:
        state_path = args.state_file or config.get("state_file") or default_state_path()
        state = load_state(state_path)
        now = time.time()
        # Remember when the previous status started, before transitions() overwrites it.
        prev_since = {r.name: state.get(r.name, {}).get("since") for r in results}
        changed = transitions(state, results, now)
        save_state(state_path, state)

        notifiers_cfg = config.get("notifiers", {})
        for result, prev in changed:
            text = transition_text(result, prev, prev_since[result.name])
            log(text)
            if args.dry_run:
                log("  [DRY RUN] no notifications sent")
                continue
            payload = {
                "name": result.name, "target": result.target,
                "status": result.status, "previous_status": prev,
                "message": result.message,
                "latency_ms": round(result.latency_ms, 1) if result.latency_ms is not None else None,
            }
            # A recovery is as important as the state it recovers from:
            # whoever was paged for the outage must also hear the all-clear.
            worst = max(result.status, prev, key=lambda s: STATUS_ORDER[s])
            send_notifications(text, payload, STATUS_SEVERITY[worst], notifiers_cfg)

    threshold = {"fail": 2, "warn": 1, "none": 3}[args.fail_on]
    if any(STATUS_ORDER[r.status] >= threshold for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
