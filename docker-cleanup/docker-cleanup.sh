#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# docker-cleanup.sh
#
# Safely prune unused Docker resources (containers, images, volumes, networks,
# build cache) older than a configurable age, with a real dry-run preview and
# a summary of space reclaimed. Designed to be run manually or via cron/systemd.
#
# Usage:
#   ./docker-cleanup.sh [OPTIONS]
#
# Options:
#   -d, --days N        Only remove resources older than N days (default: 7)
#   -n, --dry-run        Show what would be removed, but don't remove anything
#   -y, --yes            Skip confirmation prompt
#   -v, --volumes         Also prune unused (dangling) volumes  [DESTRUCTIVE]
#   -i, --images          Also prune unused (not just dangling) images
#   -b, --build-cache     Also prune builder cache
#   -a, --all              Shorthand for -v -i -b
#   -j, --json             Print a machine-readable JSON summary (implies --quiet)
#   -l, --log FILE        Write a summary log to FILE (default: none)
#   -q, --quiet            Suppress non-essential output
#   -h, --help              Show this help message
#
# Examples:
#   ./docker-cleanup.sh --dry-run
#   ./docker-cleanup.sh --days 14 --all --yes
#   ./docker-cleanup.sh -a -y -l /var/log/docker-cleanup.log
#   ./docker-cleanup.sh --dry-run --json    # preview as JSON, for tooling
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DAYS=7
DRY_RUN=false
ASSUME_YES=false
PRUNE_VOLUMES=false
PRUNE_IMAGES=false
PRUNE_BUILD_CACHE=false
JSON=false
LOG_FILE=""
QUIET=false

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log() {
    local msg="$1"
    if [[ "$QUIET" == false ]]; then
        echo -e "$msg"
    fi
    if [[ -n "$LOG_FILE" ]]; then
        # Strip color codes before writing to the log file
        echo -e "$msg" | sed -r 's/\x1B\[[0-9;]*[a-zA-Z]//g' >> "$LOG_FILE"
    fi
}

die() {
    echo -e "${RED}Error:${NC} $1" >&2
    exit 1
}

require_docker() {
    command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH."
    docker info >/dev/null 2>&1 || die "Cannot connect to the Docker daemon. Is it running / do you have permission?"
}

usage() {
    sed -n '4,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

human_bytes() {
    # Convert a raw byte count into a human-readable string (best-effort).
    local bytes="${1:-0}"
    numfmt --to=iec --suffix=B "$bytes" 2>/dev/null || echo "${bytes}B"
}

size_to_bytes() {
    # Parse a Docker "human" size (e.g. "1.5GB", "512kB", "0B") to bytes.
    # Docker uses base-1000 units, so map onto numfmt --from=si. Best-effort:
    # anything unparseable yields 0 so the summary never crashes a run.
    local s="${1:-0}"
    s="${s//[[:space:]]/}"
    s="${s%B}"                          # drop trailing 'B' (GB -> G, kB -> k)
    s="$(echo "$s" | tr 'a-z' 'A-Z')"   # numfmt SI wants uppercase (K/M/G)
    [[ -z "$s" ]] && { echo 0; return; }
    numfmt --from=si "$s" 2>/dev/null || echo 0
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--days)
            DAYS="${2:?--days requires a value}"
            shift 2
            ;;
        -n|--dry-run)
            DRY_RUN=true
            shift
            ;;
        -y|--yes)
            ASSUME_YES=true
            shift
            ;;
        -v|--volumes)
            PRUNE_VOLUMES=true
            shift
            ;;
        -i|--images)
            PRUNE_IMAGES=true
            shift
            ;;
        -b|--build-cache)
            PRUNE_BUILD_CACHE=true
            shift
            ;;
        -a|--all)
            PRUNE_VOLUMES=true
            PRUNE_IMAGES=true
            PRUNE_BUILD_CACHE=true
            shift
            ;;
        -j|--json)
            JSON=true
            QUIET=true
            shift
            ;;
        -l|--log)
            LOG_FILE="${2:?--log requires a file path}"
            shift 2
            ;;
        -q|--quiet)
            QUIET=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            die "Unknown option: $1 (use --help for usage)"
            ;;
    esac
done

[[ "$DAYS" =~ ^[0-9]+$ ]] || die "--days must be a non-negative integer."

require_docker

# Docker's --filter until= expects a duration or timestamp; convert days to hours.
FILTER_HOURS=$(( DAYS * 24 ))
FILTER="until=${FILTER_HOURS}h"

# Running totals for the summary.
TOTAL_RECLAIMED=0
declare -a ACTIONS_JSON=()

# ---------------------------------------------------------------------------
# Dry-run preview: list candidate resources instead of pruning.
#
# Note: Docker's prune `until=` filter (a relative age) can't be reproduced
# exactly by `docker ... ls`, so the preview lists current candidates and the
# real run additionally drops anything newer than the age threshold. This is a
# genuine preview of *what kind of thing* would go, not a byte-exact promise.
# ---------------------------------------------------------------------------
preview() {
    local desc="$1"; shift
    log "${GREEN}->${NC} ${desc}"
    local out
    if out="$("$@" 2>/dev/null)"; then
        if [[ -n "$out" ]]; then
            while IFS= read -r line; do log "     $line"; done <<< "$out"
        else
            log "     (nothing matches right now)"
        fi
    else
        log "     (could not list — this Docker version may not support the filter)"
    fi
}

# ---------------------------------------------------------------------------
# Execute a prune, capture reclaimed space, add it to the running total.
# ---------------------------------------------------------------------------
do_prune() {
    local action="$1" desc="$2"; shift 2
    log "${GREEN}->${NC} ${desc}"
    local out reclaimed bytes
    # Capture output so one failing prune (pipefail) doesn't abort the whole
    # run before the summary; surface a warning instead.
    if ! out="$("$@" -f 2>&1)"; then
        log "   ${YELLOW}warning: '${action}' prune reported an error:${NC}"
        while IFS= read -r line; do log "     $line"; done <<< "$out"
        ACTIONS_JSON+=("{\"action\":\"${action}\",\"reclaimed_bytes\":0,\"error\":true}")
        return 0
    fi
    [[ -n "$out" ]] && while IFS= read -r line; do log "   $line"; done <<< "$out"
    reclaimed="$(echo "$out" | sed -n 's/.*Total reclaimed space: *//p' | tail -n1)"
    bytes="$(size_to_bytes "${reclaimed:-0}")"
    TOTAL_RECLAIMED=$(( TOTAL_RECLAIMED + bytes ))
    ACTIONS_JSON+=("{\"action\":\"${action}\",\"reclaimed_bytes\":${bytes},\"error\":false}")
}

# ---------------------------------------------------------------------------
# Pre-flight report
# ---------------------------------------------------------------------------
log "${BLUE}== Docker disk usage (before) ==${NC}"
log "$(docker system df)"
log ""

log "${BLUE}== Cleanup plan ==${NC}"
log "  Age threshold : ${DAYS} day(s) (older than this will be removed)"
log "  Dry run       : ${DRY_RUN}"
log "  Prune volumes : ${PRUNE_VOLUMES}  ${YELLOW}(destructive if volumes hold data you need)${NC}"
log "  Prune images  : ${PRUNE_IMAGES}"
log "  Prune builder cache : ${PRUNE_BUILD_CACHE}"
log ""

if [[ "$DRY_RUN" == false && "$ASSUME_YES" == false ]]; then
    read -r -p "Proceed with cleanup? [y/N] " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || { log "Aborted by user."; exit 0; }
fi

# ---------------------------------------------------------------------------
# 1. Stopped containers older than threshold
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" == true ]]; then
    preview "Stopped containers that would be removed:" \
        docker container ls -a --filter "status=exited" --filter "status=created" \
        --format '{{.ID}}  {{.Image}}  {{.Status}}'
else
    do_prune "containers" "Removing stopped containers older than ${DAYS}d..." \
        docker container prune --filter "$FILTER"
fi

# ---------------------------------------------------------------------------
# 2. Dangling images (always safe: untagged, unreferenced layers)
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" == true ]]; then
    preview "Dangling images that would be removed:" \
        docker image ls --filter "dangling=true" \
        --format '{{.ID}}  {{.Repository}}:{{.Tag}}  {{.Size}}'
else
    do_prune "dangling-images" "Removing dangling images older than ${DAYS}d..." \
        docker image prune --filter "$FILTER"
fi

# ---------------------------------------------------------------------------
# 3. Unused (not just dangling) images  -- opt-in, more aggressive
# ---------------------------------------------------------------------------
if [[ "$PRUNE_IMAGES" == true ]]; then
    if [[ "$DRY_RUN" == true ]]; then
        preview "All unused images that would be removed (aggressive):" \
            docker image ls --format '{{.ID}}  {{.Repository}}:{{.Tag}}  {{.Size}}'
    else
        do_prune "unused-images" "Removing ALL unused images older than ${DAYS}d..." \
            docker image prune --all --filter "$FILTER"
    fi
fi

# ---------------------------------------------------------------------------
# 4. Unused networks
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" == true ]]; then
    preview "Custom networks (unused ones would be removed):" \
        docker network ls --filter "type=custom" \
        --format '{{.ID}}  {{.Name}}  {{.Driver}}'
else
    do_prune "networks" "Removing unused networks older than ${DAYS}d..." \
        docker network prune --filter "$FILTER"
fi

# ---------------------------------------------------------------------------
# 5. Dangling volumes -- opt-in, DESTRUCTIVE (can delete data)
# Note: `docker volume prune` has no age filter, so --days does NOT apply here.
# ---------------------------------------------------------------------------
if [[ "$PRUNE_VOLUMES" == true ]]; then
    log "${YELLOW}Warning: pruning volumes can permanently delete data not referenced by a running container.${NC}"
    log "${YELLOW}Note: the age filter (--days) does not apply to volumes; Docker prunes all dangling ones.${NC}"
    if [[ "$DRY_RUN" == true ]]; then
        preview "Dangling volumes that would be removed:" \
            docker volume ls --filter "dangling=true" --format '{{.Name}}  ({{.Driver}})'
    else
        do_prune "volumes" "Removing dangling volumes..." \
            docker volume prune
    fi
fi

# ---------------------------------------------------------------------------
# 6. Builder cache -- opt-in
# ---------------------------------------------------------------------------
if [[ "$PRUNE_BUILD_CACHE" == true ]]; then
    if [[ "$DRY_RUN" == true ]]; then
        preview "Build cache usage (older records would be removed):" \
            docker builder du
    else
        do_prune "build-cache" "Removing build cache older than ${DAYS}d..." \
            docker builder prune --filter "$FILTER"
    fi
fi

# ---------------------------------------------------------------------------
# Post-flight report
# ---------------------------------------------------------------------------
log ""
log "${BLUE}== Docker disk usage (after) ==${NC}"
log "$(docker system df)"
log ""

if [[ "$DRY_RUN" == true ]]; then
    log "${YELLOW}Dry run complete. No resources were actually removed.${NC}"
else
    log "${GREEN}Cleanup complete.${NC} Reclaimed approximately $(human_bytes "$TOTAL_RECLAIMED")."
fi

if [[ -n "$LOG_FILE" ]]; then
    log "Log written to: $LOG_FILE"
fi

# ---------------------------------------------------------------------------
# JSON summary (machine-readable) — printed to stdout when --json is set,
# even under --quiet, so it composes with other tooling.
# ---------------------------------------------------------------------------
if [[ "$JSON" == true ]]; then
    actions_joined="$(IFS=,; echo "${ACTIONS_JSON[*]:-}")"
    printf '{"dry_run":%s,"days":%s,"total_reclaimed_bytes":%s,"total_reclaimed_human":"%s","actions":[%s]}\n' \
        "$DRY_RUN" "$DAYS" "$TOTAL_RECLAIMED" "$(human_bytes "$TOTAL_RECLAIMED")" "$actions_joined"
fi
