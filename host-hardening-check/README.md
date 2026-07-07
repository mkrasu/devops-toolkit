# host-hardening-check

A read-only Linux hardening auditor — the host-level sibling of
[k8s-resource-auditor](../k8s-resource-auditor). One pass over the box,
severity-rated findings for the security misconfigurations that quietly
become incidents, and an exit code you can wire into cron or CI.

## Why

Full-blown auditors (Lynis, OpenSCAP) are thorough and correspondingly
noisy — hundreds of lines of output you triage once and never again. This
covers the short list that actually shows up in postmortems: password auth
left on, a database bound to `0.0.0.0`, a `NOPASSWD: ALL` that outlived its
reason, no firewall, ASLR off, a world-writable file in `/etc`. Small
enough to read, opinionated enough to be useful, and safe to run anywhere —
it never changes anything.

## Checks

| Check | What it flags |
|---|---|
| `ssh` | `PermitRootLogin yes` (HIGH), password auth enabled — **including when the line is absent**, since sshd defaults to yes (MEDIUM), empty passwords allowed (HIGH), X11 forwarding, generous `MaxAuthTries` |
| `accounts` | duplicate UID-0 users (HIGH), empty password hashes in `/etc/shadow` (HIGH), hashes stored in world-readable `/etc/passwd` (HIGH), `/etc/shadow` world-readable (HIGH) |
| `sudo` | `NOPASSWD: ALL` grants (MEDIUM), sudoers files writable by non-root (HIGH) |
| `network` | database/admin ports (Redis, Postgres, MySQL, Mongo, Elasticsearch, Docker API...) listening on ALL interfaces (HIGH), other wildcard listeners for review (LOW), no active firewall detected via ufw/nftables/iptables (MEDIUM) |
| `kernel` | ASLR not fully enabled (HIGH), TCP syncookies off, ICMP redirects accepted, symlink protection off (MEDIUM), rp_filter off, exposed kernel pointers, IP forwarding on (LOW) |
| `filesystem` | world-writable files in `/etc` (HIGH), world-writable temp dirs without the sticky bit (HIGH), SUID binaries outside the known-good set (MEDIUM) |
| `patching` | reboot-required marker present (MEDIUM), unattended-upgrades not enabled on apt systems (LOW) |

Each check can be run independently via `--checks`.

## Requirements

- Linux (exits cleanly with an explanation elsewhere)
- Python 3.10+ — standard library only
- Root is **optional but recommended**: without it, `/etc/shadow`,
  `/etc/sudoers`, and `sshd -T` can't be read. Skipped sources are listed
  at the end of the run — they never silently pass.

## Usage

```bash
chmod +x hardening-check.py

# Full sweep (root for full coverage)
sudo ./hardening-check.py

# Unprivileged: still covers ssh config, listeners, sysctls, filesystem, patching
./hardening-check.py

# Only specific checks
./hardening-check.py --checks ssh,network

# This box deliberately serves 8080/8443 on all interfaces
./hardening-check.py --allow-port 8080 --allow-port 8443

# CI/cron compliance canary: fail on anything HIGH
sudo ./hardening-check.py --output json --fail-on high
```

### Options

| Flag | Description |
|---|---|
| `--checks LIST` | Comma-separated checks to run (default: all — see table above) |
| `--allow-port PORT` | Port that is deliberately exposed on all interfaces (repeatable). 22, 80, 443 are always allowed |
| `--output {table,json}` | Output format (default: table, colored on a TTY) |
| `--fail-on {high,medium,low,none}` | Exit 1 if any finding at/above this severity exists (default: none) |

### Exit codes

- `0` — ran successfully (regardless of findings, unless `--fail-on` is set)
- `1` — findings at or above the `--fail-on` threshold
- `2` — preflight failure (not Linux / bad arguments)

## Sample output

```
[HIGH  ] network     Redis (port 6379) is listening on ALL interfaces — bind it to localhost or firewall it
[HIGH  ] ssh         PermitRootLogin yes — root can log in over SSH with a password
[MEDIUM] network     no active firewall detected (checked ufw/nftables/iptables) — every open port is exposed
[MEDIUM] ssh         PasswordAuthentication enabled (not set — sshd defaults to yes) — keys only is safer
[MEDIUM] sudo        /etc/sudoers.d/deploy: 'deploy' can run ANY command via sudo without a password
[LOW   ] network     listening on all interfaces (review or --allow-port): 8000, 9090
[LOW   ] patching    unattended-upgrades not enabled — security patches wait for a human

7 finding(s): 2 high, 3 medium, 2 low
```

Unreadable sources are reported on stderr rather than silently passing:

```
Warning: 2 source(s) could not be read: accounts (/etc/shadow needs root);
sudo (/etc/sudoers needs root). Run as root for full coverage.
```

## Running it regularly

```cron
# Nightly compliance canary — any HIGH finding makes the job exit 1
0 6 * * * /usr/bin/python3 /opt/scripts/hardening-check.py --fail-on high --output json >> /var/log/hardening-check.log 2>&1
```

Pair it with the notifier config you already run for
[log-tailer-alert](../log-tailer-alert) by alerting on the log, or just let
cron's mail-on-nonzero behavior do its thing.

## Behavior notes & limitations

- **Read-only, always.** The tool only reads files and runs read-only
  commands (`sshd -T`, `ufw status`, `nft list ruleset`, `iptables -S`).
- SSH settings are audited from the effective config (`sshd -T`) when
  running as root, else from `/etc/ssh/sshd_config` — global section only;
  `Match` block overrides are out of scope.
- The firewall check is presence-based: it confirms *some* ruleset is
  active, not that the rules are correct.
- The SUID check scans the standard bin directories, not the whole
  filesystem — a SUID binary hidden in `/opt` won't be seen (a full-disk
  scan is what `find / -perm -4000` is for).
- This is a hygiene check for your own hosts, not a compliance framework.
  For CIS benchmarks and audit trails, use OpenSCAP/Lynis — this is the
  tool you actually run every day.

## License

MIT — see the top-level [LICENSE](../LICENSE) in this repo.
