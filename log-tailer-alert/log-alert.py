#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
log-alert.py — tail log files, match patterns, fire alerts on thresholds.

Watches one or more log files (or stdin) for lines matching configured
regex patterns. When a pattern matches N times within a rolling time
window, it fires an alert to one or more notifiers (Slack, Discord, a
generic webhook, or email) — with a cooldown so a busy pattern doesn't
spam you every second.

No external dependencies — standard library only.

Usage:
    python3 log-alert.py --config config.json [OPTIONS]

Modes:
    (default)     Follow file(s) live, like `tail -f`, alerting as thresholds trip
    --once        Read whatever is currently new in the file(s) and exit (cron-friendly)
    --test FILE   Replay an entire file from the start against the patterns,
                  print a summary, and exit — no live tailing, no waiting

Examples:
    # Follow a log live using patterns/notifiers from config.json
    python3 log-alert.py --config config.json

    # Override which files to watch, without touching the config
    python3 log-alert.py --config config.json --file /var/log/app.log --file /var/log/nginx/error.log

    # Dry run: see what WOULD alert, without hitting Slack/email
    python3 log-alert.py --config config.json --dry-run

    # Cron-friendly: check what's new since last run, then exit
    python3 log-alert.py --config config.json --once

    # Validate your regex patterns against a historical log file
    python3 log-alert.py --config config.json --test /var/log/app.log.1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import smtplib
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from queue import Queue, Empty
from typing import Any

# ---------------------------------------------------------------------------
# Console colors (no dependency — plain ANSI, degrades harmlessly on dumb terminals)
# ---------------------------------------------------------------------------
COLORS = {
    "low": "\033[36m",      # cyan
    "medium": "\033[33m",   # yellow
    "high": "\033[31m",     # red
    "reset": "\033[0m",
}


def colorize(text: str, severity: str) -> str:
    color = COLORS.get(severity, "")
    return f"{color}{text}{COLORS['reset']}" if color else text


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr)


def die(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Config loading (JSON, with ${ENV_VAR} substitution for secrets)
# ---------------------------------------------------------------------------

ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def substitute_env(value: Any) -> Any:
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
    return substitute_env(raw)


# ---------------------------------------------------------------------------
# Pattern matching + thresholding
# ---------------------------------------------------------------------------

@dataclass
class Pattern:
    name: str
    regex: re.Pattern
    severity: str
    threshold: int
    window_seconds: int
    cooldown_seconds: int
    _hits: deque = field(default_factory=deque)
    _last_alert: float = 0.0
    _sample_lines: deque = field(default_factory=lambda: deque(maxlen=5))

    @classmethod
    def from_config(cls, cfg: dict) -> "Pattern":
        try:
            compiled = re.compile(cfg["regex"])
        except re.error as e:
            die(f"Invalid regex for pattern '{cfg.get('name', '?')}': {e}")
        return cls(
            name=cfg["name"],
            regex=compiled,
            severity=cfg.get("severity", "medium"),
            threshold=cfg.get("threshold", 1),
            window_seconds=cfg.get("window_seconds", 60),
            cooldown_seconds=cfg.get("cooldown_seconds", 300),
        )

    def check(self, line: str, source: str, now: float) -> "Alert | None":
        if not self.regex.search(line):
            return None

        self._hits.append(now)
        self._sample_lines.append(line.rstrip("\n"))

        # Drop hits outside the rolling window
        cutoff = now - self.window_seconds
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()

        if len(self._hits) < self.threshold:
            return None
        if now - self._last_alert < self.cooldown_seconds:
            return None

        self._last_alert = now
        alert = Alert(
            pattern=self.name,
            severity=self.severity,
            count=len(self._hits),
            window_seconds=self.window_seconds,
            source=source,
            samples=list(self._sample_lines),
        )
        # Require a fresh set of hits before this pattern can alert again
        self._hits.clear()
        return alert


@dataclass
class Alert:
    pattern: str
    severity: str
    count: int
    window_seconds: int
    source: str
    samples: list[str]

    def text(self) -> str:
        header = f"[{self.severity.upper()}] '{self.pattern}' matched {self.count}x in {self.window_seconds}s (source: {self.source})"
        sample_block = "\n".join(f"  > {s}" for s in self.samples[-3:])
        return f"{header}\n{sample_block}"


# ---------------------------------------------------------------------------
# Notifiers
# ---------------------------------------------------------------------------

def http_post_json(url: str, payload: dict, timeout: int = 10) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except Exception as e:
        log(f"Notifier error (HTTP POST to {url}): {e}")


def notify_slack(cfg: dict, alert: Alert) -> None:
    http_post_json(cfg["webhook_url"], {"text": alert.text()})


def notify_discord(cfg: dict, alert: Alert) -> None:
    http_post_json(cfg["webhook_url"], {"content": alert.text()})


def notify_webhook(cfg: dict, alert: Alert) -> None:
    payload = {
        "pattern": alert.pattern,
        "severity": alert.severity,
        "count": alert.count,
        "window_seconds": alert.window_seconds,
        "source": alert.source,
        "samples": alert.samples,
    }
    http_post_json(cfg["url"], payload)


def notify_email(cfg: dict, alert: Alert) -> None:
    msg = MIMEText(alert.text())
    msg["Subject"] = f"[log-alert] {alert.severity.upper()}: {alert.pattern}"
    msg["From"] = cfg["from_addr"]
    msg["To"] = ", ".join(cfg["to_addrs"])
    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg.get("smtp_port", 587), timeout=10) as server:
            if cfg.get("use_tls", True):
                server.starttls()
            if cfg.get("username"):
                server.login(cfg["username"], cfg["password"])
            server.sendmail(cfg["from_addr"], cfg["to_addrs"], msg.as_string())
    except Exception as e:
        log(f"Notifier error (email): {e}")


NOTIFIER_DISPATCH = {
    "slack": notify_slack,
    "discord": notify_discord,
    "webhook": notify_webhook,
    "email": notify_email,
}


def dispatch_alert(alert: Alert, notifiers_cfg: dict, dry_run: bool) -> None:
    banner = colorize(alert.text(), alert.severity)
    print(banner)

    if dry_run:
        print(colorize("  [DRY RUN] no notifications sent", alert.severity))
        return

    for name, cfg in notifiers_cfg.items():
        if not cfg.get("enabled", True):
            continue
        fn = NOTIFIER_DISPATCH.get(name)
        if fn is None:
            log(f"Unknown notifier in config: {name}")
            continue
        fn(cfg, alert)


# ---------------------------------------------------------------------------
# File tailing (with rotation handling)
# ---------------------------------------------------------------------------

def follow(path: str, from_start: bool, stop_event: threading.Event, out_queue: Queue) -> None:
    """Tail a file, handling truncation/rotation, pushing (line, path) tuples."""
    f = None
    inode = None
    try:
        while not stop_event.is_set():
            if f is None:
                try:
                    f = open(path, "r", encoding="utf-8", errors="replace")
                    inode = os.fstat(f.fileno()).st_ino
                    if not from_start:
                        f.seek(0, os.SEEK_END)
                except FileNotFoundError:
                    time.sleep(1)
                    continue

            line = f.readline()
            if line:
                out_queue.put((line, path))
                continue

            # No new data — check for rotation (inode changed or file shrank)
            try:
                st = os.stat(path)
                cur_inode = st.st_ino
                cur_pos = f.tell()
                if cur_inode != inode or st.st_size < cur_pos:
                    f.close()
                    f = None
                    log(f"Detected rotation on {path}, reopening.")
                    continue
            except FileNotFoundError:
                f.close()
                f = None
                continue

            time.sleep(0.5)
    finally:
        if f:
            f.close()


def follow_stdin(stop_event: threading.Event, out_queue: Queue) -> None:
    for line in sys.stdin:
        if stop_event.is_set():
            break
        out_queue.put((line, "stdin"))


def replay_file(path: str, patterns: list[Pattern], notifiers_cfg: dict, dry_run: bool) -> int:
    """--test mode: process a whole file from the start, then exit."""
    alert_count = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            now = time.time()
            for p in patterns:
                alert = p.check(line, path, now)
                if alert:
                    alert_count += 1
                    dispatch_alert(alert, notifiers_cfg, dry_run)
    return alert_count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Tail log files, match patterns, fire alerts on thresholds.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", required=True, help="Path to JSON config file")
    p.add_argument("--file", action="append", help="Override file(s) to watch (repeatable). Use '-' for stdin.")
    p.add_argument("--dry-run", action="store_true", help="Print alerts but don't send notifications")
    p.add_argument("--once", action="store_true", help="Process currently-available new lines, then exit (no follow)")
    p.add_argument("--test", metavar="FILE", help="Replay FILE from the start against the patterns, print a summary, and exit")
    p.add_argument("--from-start", action="store_true", help="In live mode, read existing file content instead of only new lines")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    config = load_config(args.config)

    patterns = [Pattern.from_config(c) for c in config.get("patterns", [])]
    if not patterns:
        die("Config has no 'patterns' defined.")
    notifiers_cfg = config.get("notifiers", {})

    if args.test:
        log(f"Replaying {args.test} against {len(patterns)} pattern(s)...")
        count = replay_file(args.test, patterns, notifiers_cfg, dry_run=True)
        print(f"\n{count} alert(s) would have fired.")
        return 0

    files = args.file or config.get("files", [])
    if not files:
        die("No files to watch. Set 'files' in the config or pass --file.")

    stop_event = threading.Event()
    out_queue: Queue = Queue()
    threads = []

    for path in files:
        if path == "-":
            t = threading.Thread(target=follow_stdin, args=(stop_event, out_queue), daemon=True)
        else:
            t = threading.Thread(
                target=follow,
                args=(path, args.from_start, stop_event, out_queue),
                daemon=True,
            )
        t.start()
        threads.append(t)
        log(f"Watching {path}")

    def handle_sigint(signum, frame):
        log("Shutting down...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    start_time = time.time()
    processed = 0
    try:
        while not stop_event.is_set():
            try:
                line, source = out_queue.get(timeout=0.5)
            except Empty:
                if args.once and time.time() - start_time > 1.0:
                    break
                continue

            processed += 1
            now = time.time()
            for p in patterns:
                alert = p.check(line, source, now)
                if alert:
                    dispatch_alert(alert, notifiers_cfg, args.dry_run)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()

    log(f"Processed {processed} line(s). Exiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
