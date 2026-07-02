# devops-toolkit

A collection of practical DevOps and SysAdmin scripts for automating daily
maintenance — Docker cleanup, Kubernetes hygiene audits, backups,
monitoring, and more.

Each tool lives in its own folder, is self-contained, and comes with its
own README covering usage, requirements, and examples. Nothing here needs
more than a shell, `kubectl`, or a stock Python 3 install — no heavyweight
frameworks, no config files to maintain, no vendor lock-in.

## Tools

| Tool | Description | Language |
|---|---|---|
| [docker-cleanup](./docker-cleanup) | Safely prunes unused Docker containers, images, volumes, networks, and build cache — with dry-run mode and age-based filtering | Bash |
| [k8s-resource-auditor](./k8s-resource-auditor) | Read-only cluster audit that flags pods missing resource limits, workloads missing readiness probes, and orphaned PVCs | Python 3 |
| [log-tailer-alert](./log-tailer-alert) | Tails log files (or stdin), matches regex patterns, and fires threshold-based alerts to Slack, Discord, webhook, or email | Python 3 |

More tools are added as they're built — see the roadmap below.

## Design principles

Every tool in this repo follows the same rules:

- **Minimal dependencies** — standard shell tools or the Python standard
  library only, unless a task genuinely needs more.
- **Safe by default** — destructive actions require an explicit flag
  (`--all`, `-y`, etc.) or a `--dry-run` mode to preview first.
- **Scriptable / CI-friendly** — sensible exit codes, machine-readable
  output modes (JSON) where relevant, and no interactive prompts required
  when run non-interactively.
- **Documented** — every tool ships with its own README: what it does,
  why it exists, how to run it, and what permissions it needs.

## Repository structure

```
devops-toolkit/
├── LICENSE                    # MIT, applies to the whole repo
├── README.md
├── docker-cleanup/
│   ├── docker-cleanup.sh
│   └── README.md
├── k8s-resource-auditor/
│   ├── k8s-audit.py
│   └── README.md
└── log-tailer-alert/
    ├── log-alert.py
    ├── example.config.json
    └── README.md
```

## Roadmap

Planned additions:

- CI/CD pipeline templates (GitHub Actions) for common stacks
- Database backup + rotation tool with S3 upload and restore verification
- Uptime/health-check dashboard for a list of endpoints

## Usage

Clone the repo and use whichever tool you need directly — they don't
depend on each other:

```bash
git clone https://github.com/mkrasu/devops-toolkit.git
cd devops-toolkit/docker-cleanup
./docker-cleanup.sh --dry-run
```

## Contributing

This is primarily a personal toolkit, but issues and PRs are welcome —
bug reports, edge cases, or small tools that fit the same design
principles above.

## License

MIT — see [LICENSE](./LICENSE). Applies to every tool in this repository.
