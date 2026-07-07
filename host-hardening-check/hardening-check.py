#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""hardening-check.py — read-only Linux host hardening auditor.

The host-level sibling of k8s-resource-auditor: sweeps a box for the
security misconfigurations that quietly become incidents, and rates them
by severity instead of dumping raw config at you.

  ssh         PermitRootLogin, PasswordAuthentication (including the
              "not set, defaults to yes" trap), empty passwords, X11
  accounts    duplicate UID-0 users, empty password hashes, world-readable
              /etc/shadow, password hashes stored in /etc/passwd
  sudo        NOPASSWD grants, sudoers.d files with unsafe permissions
  network     services listening on all interfaces (with special alarm for
              database/admin ports), no active firewall detected
  kernel      risky sysctls: ASLR off, syncookies off, ICMP redirects
              accepted, unprotected symlinks, exposed kernel pointers
  filesystem  world-writable files in /etc, missing sticky bit on /tmp,
              unexpected SUID binaries in the usual bin directories
  patching    pending reboot marker, unattended-upgrades not configured

Read-only by design: it never changes anything. Root is NOT required, but
without it some sources (/etc/shadow, /etc/sudoers, sshd -T) can't be read
— those checks are then skipped and listed at the end, k8s-audit-style,
rather than silently passing.

Usage:
    python3 hardening-check.py [OPTIONS]

Examples:
    # Full sweep, human-readable table
    sudo python3 hardening-check.py

    # Only the SSH and network checks
    python3 hardening-check.py --checks ssh,network

    # This box serves web traffic on all interfaces on purpose
    python3 hardening-check.py --allow-port 8080 --allow-port 8443

    # CI/cron compliance canary
    sudo python3 hardening-check.py --output json --fail-on high

Exit codes:
    0  ran successfully (regardless of findings, unless --fail-on is set)
    1  findings at or above the --fail-on threshold
    2  preflight failure (not Linux / bad arguments)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat as stat_mod
import subprocess
import sys
from dataclasses import dataclass

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}
COLORS = {"low": "\033[36m", "medium": "\033[33m", "high": "\033[31m", "reset": "\033[0m"}

# Ports that are normal to expose deliberately; extend with --allow-port.
DEFAULT_ALLOWED_PORTS = {22, 80, 443}

# Wildcard-bound ports that are almost never meant to face the world.
RISKY_PORTS = {
    21: "FTP", 23: "telnet", 2375: "Docker API (unauthenticated)",
    3306: "MySQL", 5432: "PostgreSQL", 5900: "VNC", 6379: "Redis",
    9200: "Elasticsearch", 11211: "memcached", 27017: "MongoDB",
}

# SUID binaries that are expected on a normal system.
KNOWN_SUID = {
    "sudo", "su", "passwd", "chsh", "chfn", "newgrp", "gpasswd", "chage",
    "expiry", "mount", "umount", "fusermount", "fusermount3", "pkexec",
    "ping", "ping6", "ssh-keysign", "unix_chkpwd", "polkit-agent-helper-1",
    "dbus-daemon-launch-helper", "Xorg.wrap", "ntfs-3g", "vmware-user-suid-wrapper",
}

SUID_SCAN_DIRS = ["/usr/bin", "/usr/sbin", "/bin", "/sbin", "/usr/local/bin", "/usr/local/sbin"]

# sysctl path, predicate on the value, severity, message. Predicate true = finding.
SYSCTL_CHECKS = [
    ("kernel.randomize_va_space", lambda v: v != "2", "high",
     "ASLR is not fully enabled (kernel.randomize_va_space={value}, want 2)"),
    ("net.ipv4.tcp_syncookies", lambda v: v != "1", "medium",
     "TCP syncookies disabled (net.ipv4.tcp_syncookies={value}) — SYN flood protection off"),
    ("net.ipv4.conf.all.accept_redirects", lambda v: v != "0", "medium",
     "ICMP redirects accepted (net.ipv4.conf.all.accept_redirects={value}) — MITM route injection risk"),
    ("net.ipv4.conf.all.rp_filter", lambda v: v == "0", "low",
     "Reverse-path filtering off (net.ipv4.conf.all.rp_filter=0) — spoofed packets not dropped"),
    ("kernel.kptr_restrict", lambda v: v == "0", "low",
     "Kernel pointers exposed to unprivileged users (kernel.kptr_restrict=0)"),
    ("fs.protected_symlinks", lambda v: v != "1", "medium",
     "Symlink protection off (fs.protected_symlinks={value}) — /tmp symlink attacks possible"),
    ("net.ipv4.ip_forward", lambda v: v == "1", "low",
     "IP forwarding enabled (net.ipv4.ip_forward=1) — expected on routers/container hosts, review otherwise"),
]

TCP_LISTEN_STATE = "0A"


@dataclass
class Finding:
    severity: str    # low | medium | high
    category: str    # ssh | accounts | sudo | network | kernel | filesystem | patching
    message: str


def die(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(2)


def read_file(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def run_cmd(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


# ---------------------------------------------------------------------------
# ssh — parse sshd_config (first occurrence wins, Match blocks are scoped
# overrides so global auditing stops there)
# ---------------------------------------------------------------------------

def parse_sshd_config(text: str) -> dict[str, str]:
    cfg: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key = parts[0].lower()
        if key == "match":
            break  # per-user/host overrides start here; globals are done
        if key not in cfg:  # sshd uses the FIRST value it sees
            cfg[key] = parts[1].strip().strip('"').lower()
    return cfg


def check_ssh(cfg: dict[str, str]) -> list[Finding]:
    f = []
    root = cfg.get("permitrootlogin", "prohibit-password")
    if root == "yes":
        f.append(Finding("high", "ssh", "PermitRootLogin yes — root can log in over SSH with a password"))
    # Upstream default is 'yes', so an absent line means password auth is ON.
    password = cfg.get("passwordauthentication", "yes")
    if password == "yes":
        suffix = "" if "passwordauthentication" in cfg else " (not set — sshd defaults to yes)"
        f.append(Finding("medium", "ssh", f"PasswordAuthentication enabled{suffix} — keys only is safer"))
    if cfg.get("permitemptypasswords", "no") == "yes":
        f.append(Finding("high", "ssh", "PermitEmptyPasswords yes — accounts without passwords can log in"))
    if cfg.get("x11forwarding", "no") == "yes":
        f.append(Finding("low", "ssh", "X11Forwarding enabled — unneeded attack surface on servers"))
    try:
        if int(cfg.get("maxauthtries", "6")) > 6:
            f.append(Finding("low", "ssh", f"MaxAuthTries {cfg['maxauthtries']} — generous brute-force budget"))
    except ValueError:
        pass
    return f


# ---------------------------------------------------------------------------
# accounts — /etc/passwd and /etc/shadow
# ---------------------------------------------------------------------------

def check_passwd(text: str) -> list[Finding]:
    f = []
    uid0 = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 7 or line.startswith("#"):
            continue
        user, pwfield, uid = parts[0], parts[1], parts[2]
        if uid == "0":
            uid0.append(user)
        if pwfield not in ("x", "*", "!", "!!", ""):
            f.append(Finding("high", "accounts",
                             f"user '{user}' has a password hash in /etc/passwd (world-readable) instead of /etc/shadow"))
        if pwfield == "":
            f.append(Finding("high", "accounts", f"user '{user}' has an EMPTY password field in /etc/passwd"))
    extra_root = [u for u in uid0 if u != "root"]
    if extra_root:
        f.append(Finding("high", "accounts", f"UID 0 duplicated by: {', '.join(extra_root)} — extra root accounts"))
    return f


def check_shadow(text: str) -> list[Finding]:
    f = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 2 or line.startswith("#"):
            continue
        user, hashfield = parts[0], parts[1]
        if hashfield == "":
            f.append(Finding("high", "accounts", f"user '{user}' has an empty password in /etc/shadow — logs in with no password"))
    return f


def check_shadow_perms(mode: int) -> list[Finding]:
    if mode & 0o004:
        return [Finding("high", "accounts", "/etc/shadow is world-readable — password hashes exposed")]
    return []


# ---------------------------------------------------------------------------
# sudo
# ---------------------------------------------------------------------------

def check_sudoers(text: str, source: str) -> list[Finding]:
    f = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("Defaults"):
            continue
        if "NOPASSWD" in line and re.search(r"NOPASSWD:\s*ALL\b", line):
            who = line.split()[0]
            f.append(Finding("medium", "sudo",
                             f"{source}: '{who}' can run ANY command via sudo without a password"))
    return f


def check_sudoers_file_mode(path: str, mode: int) -> list[Finding]:
    if mode & 0o022:
        return [Finding("high", "sudo", f"{path} is writable by non-root (mode {mode & 0o777:o})")]
    return []


# ---------------------------------------------------------------------------
# network — wildcard listeners from /proc/net/tcp[6], firewall presence
# ---------------------------------------------------------------------------

def parse_wildcard_listeners(*texts: str) -> set[int]:
    """Ports in LISTEN state bound to 0.0.0.0 / [::]."""
    ports = set()
    for text in texts:
        if not text:
            continue
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3].upper() != TCP_LISTEN_STATE:
                continue
            addr, _, port_hex = fields[1].partition(":")
            if set(addr) == {"0"}:  # all-zero address = every interface
                ports.add(int(port_hex, 16))
    return ports


def check_listeners(ports: set[int], allowed: set[int]) -> list[Finding]:
    f = []
    review = ports - allowed
    for port in sorted(review & set(RISKY_PORTS)):
        f.append(Finding("high", "network",
                         f"{RISKY_PORTS[port]} (port {port}) is listening on ALL interfaces — bind it to localhost or firewall it"))
    other = sorted(review - set(RISKY_PORTS))
    if other:
        listed = ", ".join(str(p) for p in other)
        f.append(Finding("low", "network",
                         f"listening on all interfaces (review or --allow-port): {listed}"))
    return f


def check_firewall(ufw_out: str | None, nft_out: str | None, ipt_out: str | None) -> list[Finding]:
    """Best-effort: if we could query at least one firewall frontend and none
    shows active rules, flag it. If nothing could be queried, stay silent —
    the caller reports the skip."""
    queried = [o for o in (ufw_out, nft_out, ipt_out) if o is not None]
    if not queried:
        return []
    if ufw_out and "status: active" in ufw_out.lower():
        return []
    if nft_out and re.search(r"^\s*chain\s", nft_out, re.MULTILINE):
        return []
    if ipt_out:
        rules = [ln for ln in ipt_out.splitlines() if ln.startswith("-A")]
        policies_strict = re.search(r"^-P\s+INPUT\s+(DROP|REJECT)", ipt_out, re.MULTILINE)
        if rules or policies_strict:
            return []
    return [Finding("medium", "network",
                    "no active firewall detected (checked ufw/nftables/iptables) — every open port is exposed")]


# ---------------------------------------------------------------------------
# kernel — sysctls
# ---------------------------------------------------------------------------

def check_sysctls(values: dict[str, str]) -> list[Finding]:
    f = []
    for key, is_bad, severity, template in SYSCTL_CHECKS:
        value = values.get(key)
        if value is not None and is_bad(value):
            f.append(Finding(severity, "kernel", template.format(value=value)))
    return f


# ---------------------------------------------------------------------------
# filesystem — evaluated over (path, mode) pairs collected by the caller
# ---------------------------------------------------------------------------

def check_world_writable(files: list[tuple[str, int]], limit: int = 10) -> list[Finding]:
    hits = [p for p, mode in files if mode & 0o002 and stat_mod.S_ISREG(mode)]
    return [Finding("high", "filesystem", f"world-writable file: {p}") for p in hits[:limit]]


def check_sticky_bits(dirs: list[tuple[str, int]]) -> list[Finding]:
    f = []
    for path, mode in dirs:
        if mode & 0o002 and not mode & 0o1000:
            f.append(Finding("high", "filesystem",
                             f"{path} is world-writable WITHOUT the sticky bit — anyone can delete anyone's files"))
    return f


def check_suid(binaries: list[str], known: set[str] = KNOWN_SUID) -> list[Finding]:
    unknown = sorted(b for b in binaries if os.path.basename(b) not in known)
    return [Finding("medium", "filesystem",
                    f"unexpected SUID binary: {b} — verify it's intentional") for b in unknown]


# ---------------------------------------------------------------------------
# patching
# ---------------------------------------------------------------------------

def check_patching(reboot_required: bool, is_apt_system: bool,
                   auto_upgrades_text: str | None) -> list[Finding]:
    f = []
    if reboot_required:
        f.append(Finding("medium", "patching", "reboot required — a kernel/libc security update is not active yet"))
    if is_apt_system:
        enabled = bool(auto_upgrades_text and
                       re.search(r'Unattended-Upgrade\s+"1"', auto_upgrades_text))
        if not enabled:
            f.append(Finding("low", "patching", "unattended-upgrades not enabled — security patches wait for a human"))
    return f


CHECK_REGISTRY = {
    "ssh": "SSH daemon configuration",
    "accounts": "Password and UID hygiene in passwd/shadow",
    "sudo": "Passwordless sudo grants and sudoers permissions",
    "network": "Wildcard listeners and firewall presence",
    "kernel": "Security-relevant sysctls",
    "filesystem": "World-writable files, sticky bits, SUID binaries",
    "patching": "Pending reboots and automatic updates",
}


# ---------------------------------------------------------------------------
# Collectors — the I/O layer; every unreadable source lands in `skipped`
# ---------------------------------------------------------------------------

def collect_findings(checks: list[str], allowed_ports: set[int],
                     skipped: list[str]) -> list[Finding]:
    findings: list[Finding] = []

    def unreadable(what: str) -> None:
        skipped.append(what)

    if "ssh" in checks:
        # `sshd -T` gives the effective config (root only); fall back to the file.
        text = run_cmd(["sshd", "-T"]) or read_file("/etc/ssh/sshd_config")
        if text is None:
            unreadable("ssh (/etc/ssh/sshd_config unreadable)")
        else:
            findings += check_ssh(parse_sshd_config(text))

    if "accounts" in checks:
        passwd = read_file("/etc/passwd")
        if passwd is None:
            unreadable("accounts (/etc/passwd unreadable)")
        else:
            findings += check_passwd(passwd)
        shadow = read_file("/etc/shadow")
        if shadow is None:
            unreadable("accounts (/etc/shadow needs root)")
        else:
            findings += check_shadow(shadow)
        try:
            findings += check_shadow_perms(os.stat("/etc/shadow").st_mode)
        except OSError:
            pass

    if "sudo" in checks:
        sources = ["/etc/sudoers"]
        try:
            sources += [os.path.join("/etc/sudoers.d", n) for n in sorted(os.listdir("/etc/sudoers.d"))]
        except OSError:
            pass
        readable_any = False
        for path in sources:
            text = read_file(path)
            if text is None:
                continue
            readable_any = True
            findings += check_sudoers(text, path)
            try:
                findings += check_sudoers_file_mode(path, os.stat(path).st_mode)
            except OSError:
                pass
        if not readable_any:
            unreadable("sudo (/etc/sudoers needs root)")

    if "network" in checks:
        ports = parse_wildcard_listeners(read_file("/proc/net/tcp") or "",
                                         read_file("/proc/net/tcp6") or "")
        findings += check_listeners(ports, allowed_ports)
        ufw = run_cmd(["ufw", "status"])
        nft = run_cmd(["nft", "list", "ruleset"])
        ipt = run_cmd(["iptables", "-S"])
        if ufw is None and nft is None and ipt is None:
            unreadable("network firewall (ufw/nft/iptables unavailable — may need root)")
        else:
            findings += check_firewall(ufw, nft, ipt)

    if "kernel" in checks:
        values = {}
        for key, *_ in SYSCTL_CHECKS:
            text = read_file("/proc/sys/" + key.replace(".", "/"))
            if text is not None:
                values[key] = text.strip()
        findings += check_sysctls(values)

    if "filesystem" in checks:
        etc_files = []
        for root, _dirs, names in os.walk("/etc"):
            for name in names:
                path = os.path.join(root, name)
                try:
                    etc_files.append((path, os.lstat(path).st_mode))
                except OSError:
                    continue
        findings += check_world_writable(etc_files)

        tmp_dirs = []
        for path in ("/tmp", "/var/tmp"):
            try:
                tmp_dirs.append((path, os.stat(path).st_mode))
            except OSError:
                continue
        findings += check_sticky_bits(tmp_dirs)

        suid = []
        for directory in SUID_SCAN_DIRS:
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                path = os.path.join(directory, name)
                try:
                    mode = os.lstat(path).st_mode
                except OSError:
                    continue
                if stat_mod.S_ISREG(mode) and mode & 0o4000:
                    suid.append(path)
        findings += check_suid(suid)

    if "patching" in checks:
        findings += check_patching(
            reboot_required=os.path.exists("/var/run/reboot-required"),
            is_apt_system=os.path.isdir("/etc/apt"),
            auto_upgrades_text=read_file("/etc/apt/apt.conf.d/20auto-upgrades"),
        )

    return findings


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def render_table(findings: list[Finding], color: bool) -> str:
    if not findings:
        return "No findings. The box passes every enabled check."
    ordered = sorted(findings, key=lambda x: (-SEVERITY_ORDER[x.severity], x.category, x.message))
    lines = []
    for x in ordered:
        line = f"[{x.severity.upper():6}] {x.category:11} {x.message}"
        if color and COLORS.get(x.severity):
            line = f"{COLORS[x.severity]}{line}{COLORS['reset']}"
        lines.append(line)
    counts = {s: sum(1 for x in findings if x.severity == s) for s in ("high", "medium", "low")}
    lines.append(f"\n{len(findings)} finding(s): {counts['high']} high, "
                 f"{counts['medium']} medium, {counts['low']} low")
    return "\n".join(lines)


def render_json(findings: list[Finding], skipped: list[str]) -> str:
    ordered = sorted(findings, key=lambda x: (-SEVERITY_ORDER[x.severity], x.category, x.message))
    return json.dumps({
        "summary": {s: sum(1 for x in findings if x.severity == s) for s in ("high", "medium", "low")},
        "skipped": skipped,
        "findings": [{"severity": x.severity, "category": x.category, "message": x.message}
                     for x in ordered],
    }, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Read-only Linux host hardening auditor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--checks", default="all",
                   help=f"Comma-separated checks to run: {', '.join(CHECK_REGISTRY)} (default: all)")
    p.add_argument("--allow-port", action="append", type=int, default=[], metavar="PORT",
                   help="Port that is deliberately exposed on all interfaces (repeatable). "
                        f"Always allowed: {', '.join(str(p) for p in sorted(DEFAULT_ALLOWED_PORTS))}")
    p.add_argument("--output", choices=["table", "json"], default="table")
    p.add_argument("--fail-on", choices=["high", "medium", "low", "none"], default="none",
                   help="Exit 1 if any finding at/above this severity exists (for CI/cron)")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not os.path.isdir("/proc") or not os.path.isdir("/etc"):
        die("this tool audits Linux hosts — /proc or /etc not found.")

    checks = list(CHECK_REGISTRY) if args.checks == "all" else [c.strip() for c in args.checks.split(",")]
    unknown = set(checks) - set(CHECK_REGISTRY)
    if unknown:
        die(f"unknown check(s): {', '.join(sorted(unknown))}. Valid: {', '.join(CHECK_REGISTRY)}")

    skipped: list[str] = []
    findings = collect_findings(checks, DEFAULT_ALLOWED_PORTS | set(args.allow_port), skipped)

    if args.output == "json":
        print(render_json(findings, skipped))
    else:
        print(render_table(findings, color=sys.stdout.isatty()))

    if skipped:
        print(f"\nWarning: {len(skipped)} source(s) could not be read: "
              + "; ".join(skipped) + ". Run as root for full coverage.", file=sys.stderr)

    if args.fail_on != "none":
        threshold = SEVERITY_ORDER[args.fail_on]
        if any(SEVERITY_ORDER[x.severity] >= threshold for x in findings):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
