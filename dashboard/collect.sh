#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# collect.sh — run a toolkit tool and deliver its JSON output to the dashboard.
#
# Two delivery modes:
#
#   File mode (dashboard on the same box / shared filesystem):
#     collect.sh DATA_DIR TOOL_NAME [--keep N] -- COMMAND [ARGS...]
#   writes <DATA_DIR>/<hostname>/<TOOL>/<YYYYmmdd-HHMMSS>.json and prunes
#   old results.
#
#   Post mode (remote host -> dashboard ingest API, needs curl):
#     collect.sh --post URL --token TOKEN TOOL_NAME -- COMMAND [ARGS...]
#   POSTs the output to URL/api/v1/ingest/<hostname>/<TOOL> with the
#   bearer token. Configure tokens on the dashboard via DASHBOARD_TOKENS.
#
# Examples:
#   ./collect.sh /var/lib/devops-dashboard sys-triage \
#       -- python3 /opt/devops-toolkit/sys-triage/triage.py --output json
#
#   ./collect.sh --post https://dash.internal:8080 --token "$(cat /etc/dashboard.token)" \
#       sys-triage -- python3 /opt/devops-toolkit/sys-triage/triage.py --output json
#
# The tool's exit code is preserved (delivery failures escalate it), so
# cron's mail-on-failure behavior and --fail-on flags keep working; the
# result is delivered either way so the dashboard shows failures too.
set -euo pipefail

usage() {
    echo "Usage: collect.sh DATA_DIR TOOL [--keep N] -- COMMAND..." >&2
    echo "       collect.sh --post URL --token TOKEN TOOL -- COMMAND..." >&2
    exit 2
}

MODE="file"
DATA_DIR=""
URL=""
TOKEN=""
KEEP=200

[[ $# -ge 4 ]] || usage
if [[ "$1" == "--post" ]]; then
    MODE="post"
    URL="${2:?--post requires a URL}"; shift 2
    [[ "${1:-}" == "--token" ]] || { echo "Error: --post requires --token TOKEN." >&2; exit 2; }
    TOKEN="${2:?--token requires a value}"; shift 2
    TOOL="${1:?missing TOOL name}"; shift
else
    DATA_DIR="$1"; TOOL="$2"; shift 2
    if [[ "${1:-}" == "--keep" ]]; then
        KEEP="${2:?--keep requires a value}"; shift 2
    fi
fi
[[ "${1:-}" == "--" ]] || { echo "Error: expected '--' before the command." >&2; usage; }
shift

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# Run the tool; keep its exit code but deliver the output regardless, so a
# failing run (e.g. db-backup verify failure) is visible on the dashboard.
rc=0
"$@" > "$TMP" || rc=$?

if [[ ! -s "$TMP" ]]; then
    echo "collect.sh: '${TOOL}' produced no output (exit ${rc}); nothing delivered." >&2
    exit "$rc"
fi

if [[ "$MODE" == "post" ]]; then
    if ! curl -sfS -X POST \
            -H "Authorization: Bearer ${TOKEN}" \
            -H "Content-Type: application/json" \
            --data-binary @"$TMP" \
            "${URL%/}/api/v1/ingest/$(hostname)/${TOOL}" > /dev/null; then
        echo "collect.sh: POST to ${URL} failed; result NOT delivered." >&2
        exit 1
    fi
    exit "$rc"
fi

OUT_DIR="${DATA_DIR}/$(hostname)/${TOOL}"
mkdir -p "$OUT_DIR"
mv "$TMP" "${OUT_DIR}/$(date +%Y%m%d-%H%M%S).json"
trap - EXIT

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
