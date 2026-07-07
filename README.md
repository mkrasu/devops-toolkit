# devops-toolkit

[![CI](https://github.com/mkrasu/devops-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/mkrasu/devops-toolkit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)

Small, self-contained scripts I keep reaching for when I'm doing ops work —
cleaning up after Docker, sanity-checking a Kubernetes cluster, watching logs,
or setting up a fresh machine. Nothing fancy and no frameworks; just the things
that saved me enough time that I bothered to tidy them up and keep them in one
place.

Each tool lives in its own folder with its own README, and none of them depend
on each other. If you only want one, copy that folder out and use it on its own.

## What's in here

| Tool | What it does | Language |
|---|---|---|
| [docker-cleanup](./docker-cleanup) | Prunes stale Docker containers, images, volumes, networks, and build cache — with a real dry-run preview and an age cutoff so it doesn't touch anything recent | Bash |
| [k8s-resource-auditor](./k8s-resource-auditor) | Read-only pass over a cluster that flags pods with no resource limits, workloads with no readiness probes, and PVCs nothing is using | Python 3 |
| [log-tailer-alert](./log-tailer-alert) | Follows log files (or stdin), matches regex patterns, and alerts to Slack/Discord/webhook/email once a pattern trips a threshold | Python 3 |
| [dotfiles-bootstrap](./dotfiles-bootstrap) | Sets up a new dev box: installs my usual CLI tools and symlinks starter dotfiles, backing up anything already there | Bash |
| [db-backup-rotate](./db-backup-rotate) | Dumps Postgres/MySQL, verifies the backup (up to a full restore into a scratch DB), rotates with daily/weekly/monthly retention, optionally uploads to S3 | Python 3 |
| [endpoint-watchdog](./endpoint-watchdog) | Checks HTTP endpoints and TCP ports — status, latency, TLS cert expiry — and alerts on state changes only: one message when it breaks, one when it recovers | Python 3 |

## How they're built

A few things I tried to keep consistent across all of them:

- They don't need anything exotic — a shell, `kubectl`, or a stock Python 3 is
  enough. No `pip install`, no extra runtime.
- Anything that deletes is opt-in and has a `--dry-run`. I've removed the wrong
  thing before and didn't enjoy it, so the destructive flags are never the
  default.
- They behave in scripts and cron: real exit codes, JSON output where it's
  useful, and no surprise prompts when nothing's attached to a terminal.
- Each one has a README that explains why it exists, not just how to run it.
- CI runs on every push: `shellcheck` on the shell scripts, `ruff` on the
  Python, a unit test suite for the Python tools (stdlib `unittest`, in
  [tests/](./tests)), and dry-run smoke tests of every script. Run the tests
  locally with `python3 -m unittest discover -s tests`.

## Layout

```
devops-toolkit/
├── LICENSE                     # MIT, covers the whole repo
├── README.md                   # this file
├── .github/workflows/ci.yml    # lint + tests + dry-run smoke tests
├── tests/                      # unit tests for the Python tools
├── docker-cleanup/
│   ├── docker-cleanup.sh
│   └── README.md
├── k8s-resource-auditor/
│   ├── k8s-audit.py
│   └── README.md
├── log-tailer-alert/
│   ├── log-alert.py
│   ├── example.config.json
│   └── README.md
├── dotfiles-bootstrap/
│   ├── bootstrap.sh
│   ├── dotfiles/                # .bashrc .gitconfig .gitignore_global .vimrc .tmux.conf
│   ├── packages/                # apt.txt dnf.txt pacman.txt brew.txt
│   └── README.md
├── db-backup-rotate/
│   ├── db-backup.py
│   └── README.md
└── endpoint-watchdog/
    ├── watchdog.py
    ├── example.config.json
    └── README.md
```

## Using them

Clone the repo and `cd` into whichever tool you need — each folder's README has
the exact options and requirements.

```bash
git clone https://github.com/mkrasu/devops-toolkit.git
cd devops-toolkit/<tool-name>
cat README.md
```

## On the list

Things I'll probably add when I hit the need again:

- GitHub Actions templates for the stacks I use most

## Contributing

This is mostly my own toolkit, but if you spot a bug, hit an edge case, or have
a small script that fits the same spirit, issues and PRs are welcome.

## License

MIT — see [LICENSE](./LICENSE). Applies to everything in the repo.
