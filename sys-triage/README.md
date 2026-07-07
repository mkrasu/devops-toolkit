# sys-triage

One-pass Linux performance triage. Samples `/proc` for a couple of seconds
and tells you **what's abnormal on this box** — severity-rated findings on
top of the vitals, instead of ten raw command outputs you have to interpret
under pressure.

## Why

When a machine "feels slow", everyone runs the same half-remembered sweep:
`uptime`, `free`, `df`, `top`, `iostat`, `dmesg | grep -i oom`, `ss -s`...
It takes minutes, you always forget one (inodes, CPU steal, conntrack), and
raw numbers still need interpreting. This automates the whole sweep — the
"first 60 seconds of performance analysis" checklist — in one command that
already knows what a bad number looks like:

```
== Findings (3) ==
  [HIGH  ] cpu: CPU steal 27% — the hypervisor is starving this VM
  [HIGH  ] memory: 2 OOM kill record(s) in the kernel log — last: Out of memory: Killed process 4242 (java)...
  [MEDIUM] disk: /var: 91% of space used
```

## What it checks

| Category | Signals |
|---|---|
| CPU | load vs. core count, iowait, **CPU steal** (the invisible VM killer), CPU pressure (PSI), top consumers over the sample window |
| Memory | available %, swap in/out rates (thrashing), memory pressure (PSI), **recent OOM kills** from the kernel log, top RSS consumers |
| Disk | space **and inodes** per filesystem, per-device utilization / IOPS / average I/O latency over the sample window |
| Network | TCP retransmission rate, connection pile-ups by state (TIME_WAIT churn, CLOSE_WAIT leaks, SYN floods), conntrack table fill |
| System | failed systemd units, file-descriptor exhaustion, zombie counts |

Everything comes from `/proc`, `/sys`, and best-effort `dmesg`/`systemctl`
— sources that need privileges (e.g. `dmesg` under `kernel.dmesg_restrict`)
are skipped silently rather than failing the run. No root needed for the
core checks, no packages, no agents.

## Requirements

- Linux (it reads `/proc`; exits cleanly with an explanation elsewhere)
- Python 3.10+ — standard library only

## Usage

```bash
chmod +x triage.py

# The 3am command
./triage.py

# Steadier rates: sample for 5 seconds instead of 2
./triage.py --interval 5

# JSON snapshot — capture during an incident, compare against a good one
./triage.py --output json > incident.json
diff <(jq .vitals baseline.json) <(jq .vitals incident.json)

# Cron/CI canary: exit 1 if anything HIGH severity shows up
./triage.py --fail-on high
```

### Options

| Flag | Description |
|---|---|
| `--interval SEC` | Sampling window for CPU/disk/network rates (default: 2) |
| `--top N` | How many top CPU/memory processes to show (default: 5) |
| `--output {table,json}` | Output format (default: table, colored on a TTY) |
| `--fail-on {high,medium,low,none}` | Exit 1 if any finding at/above this severity exists (default: none) |

### Exit codes

- `0` — ran fine (regardless of findings, unless `--fail-on` is set)
- `1` — findings at/above the `--fail-on` threshold
- `2` — preflight failure (not Linux / cannot read `/proc`)

## Sample output

```
== System ==
  load 6.41 / 5.80 / 4.12  on 4 core(s), up 34.2d
  cpu: 31% user, 12% sys, 4% iowait, 27% steal, 26% idle
  mem: 0.9/15.6 GiB available (6%), swap used 74%
== Top processes (cpu) ==
  212.0%     4242  java
   41.5%     1337  postgres: writer
    3.1%      812  node
== Top processes (memory) ==
   6144M     4242  java
   1024M     1337  postgres: writer
    512M      812  node
== Filesystems ==
  /: 64% of 78.7 GiB, 12% inodes
  /var: 91% of 196.7 GiB, 8% inodes
== Disk I/O (sampled) ==
  sda: 97% busy, 412 iops, await 18.3ms
== Network ==
  tcp retransmits: 0.42% of segments; states: ESTABLISHED=214, TIME_WAIT=88, LISTEN=12
== Findings (5) ==
  [HIGH  ] cpu: CPU steal 27% — the hypervisor is starving this VM
  [HIGH  ] memory: only 6% of memory available — OOM kill territory
  [HIGH  ] disk: sda: 97% busy, await 18ms — saturated
  [MEDIUM] cpu: load 6.4 exceeds the 4 cores
  [MEDIUM] disk: /var: 91% of space used
```

A healthy box prints the same vitals and:

```
== Findings (0) ==
  Nothing abnormal by the built-in thresholds. If it still feels slow,
  the bottleneck is likely inside an application, not the box.
```

## Snapshot workflow

The JSON output contains every vital the checks are computed from. Two
useful habits:

1. **Baseline while healthy**: `./triage.py --output json > baseline.json`
   in the repo/wiki for each box. During an incident, diff against it.
2. **Cron canary**: `*/5 * * * * /opt/scripts/triage.py --fail-on high ||
   notify...` — or combine with
   [log-tailer-alert](../log-tailer-alert)/[endpoint-watchdog](../endpoint-watchdog)
   notifier configs you already have.

## Behavior notes & limitations

- Rates (CPU %, disk util/await, retransmit %) are measured over the
  `--interval` window, so a burst shorter than the window is averaged down;
  bump the interval for steadier numbers, not for burst-hunting.
- PSI (pressure stall information) needs Linux ≥ 4.20 with
  `CONFIG_PSI=y` — the standard on modern distros; silently skipped
  elsewhere.
- OOM detection reads `dmesg`, which unprivileged users can't always run
  (`kernel.dmesg_restrict=1`) — run as root, or accept that this one check
  is skipped.
- Disk `util%` on NVMe reflects "device had work in flight", which
  understates parallel-capable hardware; treat it as a smell, not a verdict
  (`await` is the better signal there).
- Thresholds are deliberately opinionated and built-in. Fork and tune —
  they live in one obvious block of `check_*` functions.

## License

MIT — see the top-level [LICENSE](../LICENSE) in this repo.
