#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""triage.py — one-pass Linux performance triage: what's abnormal on this box?

Automates the "first 60 seconds on a broken machine" sweep. Samples /proc
over a short interval and reports, with severity-rated findings instead of
raw numbers you have to interpret at 3am:

  CPU      load vs. cores, iowait, CPU steal (the invisible VM killer),
           pressure stall info (PSI), top CPU consumers
  Memory   available vs. total, swap in/out rates, memory PSI,
           recent OOM kills from the kernel log, top RSS consumers
  Disk     space AND inodes per filesystem, per-device utilization and
           average I/O latency
  Network  TCP retransmission rate, connection counts by state,
           conntrack table fill
  System   failed systemd units, file-descriptor exhaustion, zombies

Everything comes from /proc, /sys, and (best-effort) dmesg/systemctl — no
root required for the core checks, no external dependencies, Linux only.

Usage:
    python3 triage.py [OPTIONS]

Examples:
    # The 3am command: full sweep, human-readable
    python3 triage.py

    # Longer sample window for steadier rates
    python3 triage.py --interval 5

    # JSON snapshot — capture during an incident, diff against a good one
    python3 triage.py --output json > incident.json

    # Cron/CI canary: exit 1 if anything HIGH severity shows up
    python3 triage.py --fail-on high

Exit codes:
    0  ran fine (regardless of findings, unless --fail-on is set)
    1  findings at/above the --fail-on threshold
    2  preflight failure (not Linux / cannot read /proc)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}
COLORS = {"low": "\033[36m", "medium": "\033[33m", "high": "\033[31m",
          "head": "\033[34m", "reset": "\033[0m"}

PSEUDO_FS = {
    "proc", "sysfs", "devtmpfs", "devpts", "securityfs", "cgroup", "cgroup2",
    "pstore", "efivarfs", "bpf", "tracefs", "debugfs", "mqueue", "hugetlbfs",
    "fusectl", "configfs", "autofs", "binfmt_misc", "rpc_pipefs", "nsfs",
    "fuse.portal", "squashfs", "ramfs", "tmpfs",
}

TCP_STATES = {
    "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1",
    "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT",
    "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING",
}

# Device names whose I/O stats are noise (virtual/removable) or double-counted
# (partitions — the whole-disk line already covers them).
SKIP_DEV_RE = re.compile(
    r"^(loop|ram|zram|sr|fd)\d*$|^(sd[a-z]+|vd[a-z]+|xvd[a-z]+)\d+$"
    r"|^(nvme\d+n\d+|mmcblk\d+)p\d+$"
)


@dataclass
class Finding:
    severity: str    # low | medium | high
    category: str    # cpu | memory | disk | network | system
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
    """Best-effort command runner: None if missing, denied, or failed."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


# ---------------------------------------------------------------------------
# Parsers — pure functions over /proc text, unit-tested with fixtures
# ---------------------------------------------------------------------------

def parse_loadavg(text: str) -> tuple[float, float, float]:
    parts = text.split()
    return float(parts[0]), float(parts[1]), float(parts[2])


def parse_stat_cpu(text: str) -> dict[str, int]:
    """The aggregate 'cpu ' line of /proc/stat as named jiffy counters."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            v += [0] * (8 - len(v))
            keys = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
            return dict(zip(keys, v[:8]))
    return {}


def cpu_percentages(before: dict[str, int], after: dict[str, int]) -> dict[str, float]:
    total = sum(after.values()) - sum(before.values())
    if total <= 0:
        return {k: 0.0 for k in before}
    return {k: 100.0 * (after[k] - before[k]) / total for k in before}


def parse_meminfo(text: str) -> dict[str, int]:
    """Values in kB."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def parse_pressure(text: str) -> dict[str, float]:
    """PSI file -> {'some_avg10': x, 'full_avg10': y} (percent stalled)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"(some|full) avg10=([\d.]+)", line)
        if m:
            out[f"{m.group(1)}_avg10"] = float(m.group(2))
    return out


def parse_vmstat(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def parse_diskstats(text: str) -> dict[str, dict[str, int]]:
    devices = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) < 14:
            continue
        name = f[2]
        if SKIP_DEV_RE.match(name):
            continue
        devices[name] = {
            "ios": int(f[3]) + int(f[7]),                 # completed reads+writes
            "ticks": int(f[6]) + int(f[10]),              # ms spent on reads+writes
            "io_ticks": int(f[12]),                       # ms the device was busy
        }
    return devices


def disk_rates(before: dict, after: dict, interval: float) -> dict[str, dict[str, float]]:
    rates = {}
    for dev, b in before.items():
        a = after.get(dev)
        if a is None:
            continue
        d_ios = a["ios"] - b["ios"]
        rates[dev] = {
            "util_pct": min(100.0, 100.0 * (a["io_ticks"] - b["io_ticks"]) / (interval * 1000)),
            "await_ms": (a["ticks"] - b["ticks"]) / d_ios if d_ios > 0 else 0.0,
            "iops": d_ios / interval,
        }
    return rates


def parse_net_snmp(text: str) -> dict[str, int]:
    """Tcp counters from /proc/net/snmp (header line + value line pairs)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("Tcp:") and i + 1 < len(lines) and lines[i + 1].startswith("Tcp:"):
            keys = line.split()[1:]
            vals = lines[i + 1].split()[1:]
            return dict(zip(keys, (int(v) for v in vals)))
    return {}


def tcp_state_counts(*texts: str) -> dict[str, int]:
    """Connection counts by state from /proc/net/tcp[6] contents."""
    counts: dict[str, int] = {}
    for text in texts:
        if not text:
            continue
        for line in text.splitlines()[1:]:
            f = line.split()
            if len(f) > 3:
                state = TCP_STATES.get(f[3].upper(), "OTHER")
                counts[state] = counts.get(state, 0) + 1
    return counts


def parse_file_nr(text: str) -> tuple[int, int]:
    f = text.split()
    return int(f[0]), int(f[2])


def parse_pid_stat(text: str) -> dict | None:
    """One /proc/<pid>/stat line. comm can contain spaces and parentheses,
    so split at the LAST ')'."""
    rp = text.rfind(")")
    lp = text.find("(")
    if lp < 0 or rp < 0:
        return None
    rest = text[rp + 1:].split()
    if len(rest) < 22:
        return None
    return {
        "comm": text[lp + 1:rp],
        "state": rest[0],
        "cpu_jiffies": int(rest[11]) + int(rest[12]),   # utime + stime
        "rss_pages": int(rest[21]),
    }


def scan_oom(kernel_log: str) -> list[str]:
    """Lines in a kernel log that record OOM kills."""
    hits = []
    for line in kernel_log.splitlines():
        if "Out of memory" in line or "oom-kill" in line or "oom_reaper" in line:
            hits.append(line.strip())
    return hits


# ---------------------------------------------------------------------------
# Checks — pure threshold logic over parsed values, unit-tested
# ---------------------------------------------------------------------------

def check_cpu(load1: float, cores: int, cpu_pct: dict[str, float],
              psi: dict[str, float]) -> list[Finding]:
    f = []
    if cores and load1 > 2 * cores:
        f.append(Finding("high", "cpu", f"load {load1:.1f} is over 2x the {cores} cores — heavy run-queue contention"))
    elif cores and load1 > cores:
        f.append(Finding("medium", "cpu", f"load {load1:.1f} exceeds the {cores} cores"))
    steal = cpu_pct.get("steal", 0.0)
    if steal > 20:
        f.append(Finding("high", "cpu", f"CPU steal {steal:.0f}% — the hypervisor is starving this VM"))
    elif steal > 5:
        f.append(Finding("medium", "cpu", f"CPU steal {steal:.0f}% — noisy neighbor on the host"))
    iowait = cpu_pct.get("iowait", 0.0)
    if iowait > 30:
        f.append(Finding("high", "cpu", f"iowait {iowait:.0f}% — CPUs mostly waiting on disk"))
    elif iowait > 15:
        f.append(Finding("medium", "cpu", f"iowait {iowait:.0f}% — significant time waiting on disk"))
    if psi.get("some_avg10", 0.0) > 25:
        f.append(Finding("medium", "cpu", f"CPU pressure: tasks stalled {psi['some_avg10']:.0f}% of the last 10s (PSI)"))
    return f


def check_memory(meminfo: dict[str, int], psi: dict[str, float],
                 swap_in_rate: float, swap_out_rate: float,
                 oom_lines: list[str]) -> list[Finding]:
    f = []
    total = meminfo.get("MemTotal", 0)
    avail = meminfo.get("MemAvailable", 0)
    if total:
        pct = 100.0 * avail / total
        if pct < 5:
            f.append(Finding("high", "memory", f"only {pct:.0f}% of memory available — OOM kill territory"))
        elif pct < 10:
            f.append(Finding("medium", "memory", f"only {pct:.0f}% of memory available"))
    if swap_out_rate > 1000:
        f.append(Finding("high", "memory", f"swapping out {swap_out_rate:.0f} pages/s — actively thrashing"))
    elif swap_out_rate + swap_in_rate > 100:
        f.append(Finding("medium", "memory", f"swap activity: {swap_in_rate:.0f} in / {swap_out_rate:.0f} out pages/s"))
    full = psi.get("full_avg10", 0.0)
    if full > 10:
        f.append(Finding("high", "memory", f"memory pressure: ALL tasks stalled {full:.0f}% of the last 10s (PSI full)"))
    elif psi.get("some_avg10", 0.0) > 10:
        f.append(Finding("medium", "memory", f"memory pressure: tasks stalled {psi['some_avg10']:.0f}% of the last 10s (PSI)"))
    if oom_lines:
        f.append(Finding("high", "memory",
                         f"{len(oom_lines)} OOM kill record(s) in the kernel log — last: {oom_lines[-1][:120]}"))
    return f


def check_filesystems(mounts: dict[str, dict[str, float]]) -> list[Finding]:
    f = []
    for mount, m in sorted(mounts.items()):
        for kind, pct in (("space", m["used_pct"]), ("inodes", m.get("inode_pct", 0.0))):
            if pct > 95:
                f.append(Finding("high", "disk", f"{mount}: {pct:.0f}% of {kind} used"))
            elif pct > 85:
                f.append(Finding("medium", "disk", f"{mount}: {pct:.0f}% of {kind} used"))
    return f


def check_disk_io(rates: dict[str, dict[str, float]]) -> list[Finding]:
    f = []
    for dev, r in sorted(rates.items()):
        if r["util_pct"] > 95 and r["iops"] > 1:
            f.append(Finding("high", "disk", f"{dev}: {r['util_pct']:.0f}% busy, await {r['await_ms']:.0f}ms — saturated"))
        elif r["util_pct"] > 80 and r["iops"] > 1:
            f.append(Finding("medium", "disk", f"{dev}: {r['util_pct']:.0f}% busy, await {r['await_ms']:.0f}ms"))
        elif r["await_ms"] > 100 and r["iops"] > 1:
            f.append(Finding("medium", "disk", f"{dev}: average I/O latency {r['await_ms']:.0f}ms"))
    return f


def check_network(retrans_pct: float, states: dict[str, int],
                  conntrack: tuple[int, int] | None) -> list[Finding]:
    f = []
    if retrans_pct > 5:
        f.append(Finding("high", "network", f"TCP retransmission rate {retrans_pct:.1f}% — lossy or congested path"))
    elif retrans_pct > 1:
        f.append(Finding("medium", "network", f"TCP retransmission rate {retrans_pct:.1f}%"))
    if states.get("TIME_WAIT", 0) > 20000:
        f.append(Finding("medium", "network", f"{states['TIME_WAIT']} sockets in TIME_WAIT — connection churn"))
    if states.get("CLOSE_WAIT", 0) > 1000:
        f.append(Finding("medium", "network", f"{states['CLOSE_WAIT']} sockets in CLOSE_WAIT — something isn't closing connections"))
    if states.get("SYN_RECV", 0) > 256:
        f.append(Finding("high", "network", f"{states['SYN_RECV']} sockets in SYN_RECV — possible SYN flood or dying backend"))
    if conntrack:
        count, maximum = conntrack
        if maximum and count / maximum > 0.8:
            f.append(Finding("high", "network",
                             f"conntrack table {100 * count / maximum:.0f}% full ({count}/{maximum}) — new connections will be dropped at 100%"))
    return f


def check_system(file_nr: tuple[int, int] | None, zombies: int,
                 failed_units: list[str]) -> list[Finding]:
    f = []
    if file_nr:
        allocated, maximum = file_nr
        if maximum and allocated / maximum > 0.8:
            f.append(Finding("high", "system",
                             f"file descriptors {100 * allocated / maximum:.0f}% of system max ({allocated}/{maximum})"))
    if zombies > 100:
        f.append(Finding("medium", "system", f"{zombies} zombie processes — a parent isn't reaping children"))
    elif zombies > 10:
        f.append(Finding("low", "system", f"{zombies} zombie processes"))
    for unit in failed_units:
        f.append(Finding("medium", "system", f"systemd unit failed: {unit}"))
    return f


# ---------------------------------------------------------------------------
# Collectors — the thin I/O layer around the parsers
# ---------------------------------------------------------------------------

def collect_mounts() -> dict[str, dict[str, float]]:
    mounts: dict[str, dict[str, float]] = {}
    text = read_file("/proc/mounts") or ""
    seen_devices = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, fstype = parts[0], parts[1], parts[2]
        if fstype in PSEUDO_FS or device in seen_devices:
            continue
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        if st.f_blocks == 0:
            continue
        seen_devices.add(device)
        entry = {
            "size_bytes": st.f_blocks * st.f_frsize,
            "used_pct": 100.0 * (1 - st.f_bavail / st.f_blocks),
        }
        if st.f_files:
            entry["inode_pct"] = 100.0 * (1 - st.f_favail / st.f_files)
        mounts[mount] = entry
    return mounts


def sample_processes() -> dict[int, dict]:
    procs = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        text = read_file(f"/proc/{pid}/stat")
        if text:
            parsed = parse_pid_stat(text)
            if parsed:
                procs[int(pid)] = parsed
    return procs


def collect_failed_units() -> list[str]:
    out = run_cmd(["systemctl", "--failed", "--plain", "--no-legend", "--no-pager"])
    if not out:
        return []
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def collect_conntrack() -> tuple[int, int] | None:
    count = read_file("/proc/sys/net/netfilter/nf_conntrack_count")
    maximum = read_file("/proc/sys/net/netfilter/nf_conntrack_max")
    if count and maximum:
        return int(count), int(maximum)
    return None


def snapshot() -> dict:
    return {
        "stat": read_file("/proc/stat") or "",
        "vmstat": read_file("/proc/vmstat") or "",
        "diskstats": read_file("/proc/diskstats") or "",
        "snmp": read_file("/proc/net/snmp") or "",
        "procs": sample_processes(),
        "time": time.monotonic(),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def colorize(text: str, key: str, enabled: bool) -> str:
    if enabled and COLORS.get(key):
        return f"{COLORS[key]}{text}{COLORS['reset']}"
    return text


def render_report(vitals: dict, findings: list[Finding], color: bool) -> str:
    L = []

    def head(t):
        L.append(colorize(f"== {t} ==", "head", color))

    head("System")
    L.append(f"  load {vitals['load'][0]:.2f} / {vitals['load'][1]:.2f} / {vitals['load'][2]:.2f}"
             f"  on {vitals['cores']} core(s), up {vitals['uptime_days']:.1f}d")
    c = vitals["cpu_pct"]
    L.append(f"  cpu: {c['user']:.0f}% user, {c['system']:.0f}% sys, {c['iowait']:.0f}% iowait,"
             f" {c['steal']:.0f}% steal, {c['idle']:.0f}% idle")
    m = vitals["memory"]
    L.append(f"  mem: {m['available_gib']:.1f}/{m['total_gib']:.1f} GiB available"
             f" ({m['available_pct']:.0f}%), swap used {m['swap_used_pct']:.0f}%")

    head("Top processes (cpu)")
    for p in vitals["top_cpu"]:
        L.append(f"  {p['cpu_pct']:5.1f}%  {p['pid']:>7}  {p['comm']}")
    head("Top processes (memory)")
    for p in vitals["top_mem"]:
        L.append(f"  {p['rss_mib']:6.0f}M  {p['pid']:>7}  {p['comm']}")

    head("Filesystems")
    for mount, e in sorted(vitals["filesystems"].items()):
        inode = f", {e['inode_pct']:.0f}% inodes" if "inode_pct" in e else ""
        L.append(f"  {mount}: {e['used_pct']:.0f}% of {e['size_bytes'] / 1024**3:.1f} GiB{inode}")

    if vitals["disk_io"]:
        head("Disk I/O (sampled)")
        for dev, r in sorted(vitals["disk_io"].items()):
            L.append(f"  {dev}: {r['util_pct']:.0f}% busy, {r['iops']:.0f} iops, await {r['await_ms']:.1f}ms")

    head("Network")
    states = ", ".join(f"{k}={v}" for k, v in sorted(vitals["tcp_states"].items(), key=lambda kv: -kv[1])[:5])
    L.append(f"  tcp retransmits: {vitals['retrans_pct']:.2f}% of segments; states: {states or 'n/a'}")

    head(f"Findings ({len(findings)})")
    if not findings:
        L.append("  Nothing abnormal by the built-in thresholds. If it still feels slow,")
        L.append("  the bottleneck is likely inside an application, not the box.")
    else:
        ordered = sorted(findings, key=lambda x: -SEVERITY_ORDER[x.severity])
        for x in ordered:
            L.append(colorize(f"  [{x.severity.upper():6}] {x.category}: {x.message}", x.severity, color))
    return "\n".join(L)


def render_json(vitals: dict, findings: list[Finding]) -> str:
    return json.dumps({
        "vitals": vitals,
        "summary": {s: sum(1 for x in findings if x.severity == s) for s in ("high", "medium", "low")},
        "findings": [{"severity": x.severity, "category": x.category, "message": x.message}
                     for x in sorted(findings, key=lambda x: -SEVERITY_ORDER[x.severity])],
    }, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="One-pass Linux performance triage with severity-rated findings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--interval", type=float, default=2.0, metavar="SEC",
                   help="Sampling window for CPU/disk/network rates (default: 2)")
    p.add_argument("--top", type=int, default=5, metavar="N",
                   help="How many top processes to show (default: 5)")
    p.add_argument("--output", choices=["table", "json"], default="table")
    p.add_argument("--fail-on", choices=["high", "medium", "low", "none"], default="none",
                   help="Exit 1 if any finding at/above this severity exists")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not os.path.isdir("/proc") or read_file("/proc/stat") is None:
        die("cannot read /proc — this tool only works on Linux.")

    before = snapshot()
    time.sleep(max(0.5, args.interval))
    after = snapshot()
    interval = after["time"] - before["time"]

    # --- CPU
    load1, load5, load15 = parse_loadavg(read_file("/proc/loadavg") or "0 0 0 0/0 0")
    cores = os.cpu_count() or 1
    cpu_pct = cpu_percentages(parse_stat_cpu(before["stat"]), parse_stat_cpu(after["stat"]))
    psi_cpu = parse_pressure(read_file("/proc/pressure/cpu") or "")
    psi_mem = parse_pressure(read_file("/proc/pressure/memory") or "")

    # --- memory
    meminfo = parse_meminfo(read_file("/proc/meminfo") or "")
    vm_b, vm_a = parse_vmstat(before["vmstat"]), parse_vmstat(after["vmstat"])
    swap_in = (vm_a.get("pswpin", 0) - vm_b.get("pswpin", 0)) / interval
    swap_out = (vm_a.get("pswpout", 0) - vm_b.get("pswpout", 0)) / interval
    oom_lines = scan_oom(run_cmd(["dmesg", "--level=err,warn,info"]) or "")

    # --- processes
    hz = os.sysconf("SC_CLK_TCK")
    page_kib = os.sysconf("SC_PAGESIZE") / 1024
    proc_rates = []
    for pid, a in after["procs"].items():
        b = before["procs"].get(pid)
        cpu = 100.0 * (a["cpu_jiffies"] - b["cpu_jiffies"]) / hz / interval if b else 0.0
        proc_rates.append({"pid": pid, "comm": a["comm"], "cpu_pct": cpu,
                           "rss_mib": a["rss_pages"] * page_kib / 1024, "state": a["state"]})
    zombies = sum(1 for p in proc_rates if p["state"] == "Z")
    top_cpu = sorted(proc_rates, key=lambda p: -p["cpu_pct"])[:args.top]
    top_mem = sorted(proc_rates, key=lambda p: -p["rss_mib"])[:args.top]

    # --- disk
    filesystems = collect_mounts()
    io_rates = disk_rates(parse_diskstats(before["diskstats"]),
                          parse_diskstats(after["diskstats"]), interval)

    # --- network
    tcp_b, tcp_a = parse_net_snmp(before["snmp"]), parse_net_snmp(after["snmp"])
    d_out = tcp_a.get("OutSegs", 0) - tcp_b.get("OutSegs", 0)
    d_retrans = tcp_a.get("RetransSegs", 0) - tcp_b.get("RetransSegs", 0)
    retrans_pct = 100.0 * d_retrans / d_out if d_out > 0 else 0.0
    states = tcp_state_counts(read_file("/proc/net/tcp") or "", read_file("/proc/net/tcp6") or "")
    conntrack = collect_conntrack()

    # --- system
    file_nr_text = read_file("/proc/sys/fs/file-nr")
    file_nr = parse_file_nr(file_nr_text) if file_nr_text else None
    failed_units = collect_failed_units()
    uptime = float((read_file("/proc/uptime") or "0").split()[0])

    findings = (
        check_cpu(load1, cores, cpu_pct, psi_cpu)
        + check_memory(meminfo, psi_mem, swap_in, swap_out, oom_lines)
        + check_filesystems(filesystems)
        + check_disk_io(io_rates)
        + check_network(retrans_pct, states, conntrack)
        + check_system(file_nr, zombies, failed_units)
    )

    total_kib = meminfo.get("MemTotal", 0)
    avail_kib = meminfo.get("MemAvailable", 0)
    swap_total = meminfo.get("SwapTotal", 0)
    swap_free = meminfo.get("SwapFree", 0)
    vitals = {
        "load": [load1, load5, load15],
        "cores": cores,
        "uptime_days": uptime / 86400,
        "cpu_pct": {k: round(v, 1) for k, v in cpu_pct.items()},
        "memory": {
            "total_gib": total_kib / 1024**2,
            "available_gib": avail_kib / 1024**2,
            "available_pct": 100.0 * avail_kib / total_kib if total_kib else 0.0,
            "swap_used_pct": 100.0 * (swap_total - swap_free) / swap_total if swap_total else 0.0,
            "swap_in_per_s": round(swap_in, 1),
            "swap_out_per_s": round(swap_out, 1),
            "psi": psi_mem,
        },
        "top_cpu": [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in p.items()} for p in top_cpu],
        "top_mem": [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in p.items()} for p in top_mem],
        "filesystems": filesystems,
        "disk_io": {d: {k: round(v, 1) for k, v in r.items()} for d, r in io_rates.items()},
        "retrans_pct": round(retrans_pct, 2),
        "tcp_states": states,
        "zombies": zombies,
        "failed_units": failed_units,
    }

    if args.output == "json":
        print(render_json(vitals, findings))
    else:
        print(render_report(vitals, findings, color=sys.stdout.isatty()))

    if args.fail_on != "none":
        threshold = SEVERITY_ORDER[args.fail_on]
        if any(SEVERITY_ORDER[x.severity] >= threshold for x in findings):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
