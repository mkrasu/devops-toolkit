#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# collect.sh — run a toolkit tool and store its JSON output for the dashboard.
#
# Writes <data_dir>/<hostname>/<tool>/<YYYYmmdd-HHMMSS>.json and prunes old
# results. Meant to be called from cron or a systemd timer, one line per tool.
#
# Usage:
#   collect.sh DATA_DIR TOOL_NAME [--keep N] -- COMMAND [ARGS...]
#
# Examples:
#   ./collect.sh /var/lib/devops-dashboard sys-triage \
#       -- python3 /opt/devops-toolkit/sys-triage/triage.py --output json
#
#   ./collect.sh /var/lib/devops-dashboard endpoint-watchdog --keep 500 \
#       -- python3 /opt/devops-toolkit/endpoint-watchdog/watchdog.py \
#          --config /etc/endpoint-watchdog/config.json --output json
#
# The tool's exit code is preserved, so cron's mail-on-failure behavior and
# any --fail-on flags keep working; the result is stored either way so the
# dashboard shows the failure too.
set -euo pipefail

KEEP=200

[[ $# -ge 4 ]] || { echo "Usage: collect.sh DATA_DIR TOOL_NAME [--keep N] -- COMMAND [ARGS...]" >&2; exit 2; }
DATA_DIR="$1"; TOOL="$2"; shift 2
if [[ "${1:-}" == "--keep" ]]; then
    KEEP="${2:?--keep requires a value}"; shift 2
fi
[[ "${1:-}" == "--" ]] || { echo "Error: expected '--' before the command." >&2; exit 2; }
shift

OUT_DIR="${DATA_DIR}/$(hostname)/${TOOL}"
mkdir -p "$OUT_DIR"

TMP="$(mktemp "${OUT_DIR}/.collect.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

# Run the tool; keep its exit code but store the output regardless, so a
# failing run (e.g. db-backup verify failure) is visible on the dashboard.
rc=0
"$@" > "$TMP" || rc=$?

if [[ -s "$TMP" ]]; then
    mv "$TMP" "${OUT_DIR}/$(date +%Y%m%d-%H%M%S).json"
    trap - EXIT
else
    echo "collect.sh: '${TOOL}' produced no output (exit ${rc}); nothing stored." >&2
fi

# Prune: keep the newest $KEEP results. Timestamped names sort
# chronologically, and glob expansion returns them sorted.
shopt -s nullglob
results=( "$OUT_DIR"/[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9].json )
count=${#results[@]}
if (( count > KEEP )); then
    for old in "${results[@]:0:count-KEEP}"; do
        rm -f "$old"
    done
fi

exit "$rc"
