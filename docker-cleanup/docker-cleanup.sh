#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# docker-cleanup.sh
#
# Safely prune unused Docker resources (containers, images, volumes, networks,
# build cache) older than a configurable age, with dry-run support and a
# summary report. Designed to be run manually or via cron/systemd timer.
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
#   -l, --log FILE        Write a summary log to FILE (default: none)
#   -q, --quiet            Suppress non-essential output
#   -h, --help              Show this help message
#
# Examples:
#   ./docker-cleanup.sh --dry-run
#   ./docker-cleanup.sh --days 14 --all --yes
#   ./docker-cleanup.sh -a -y -l /var/log/docker-cleanup.log
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
    sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

human_bytes() {
    # Convert a raw byte count into a human-readable string (best-effort).
    local bytes="${1:-0}"
    numfmt --to=iec --suffix=B "$bytes" 2>/dev/null || echo "${bytes}B"
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

FILTER="until=${DAYS}h"
# Docker's --filter until= expects a duration or timestamp; convert days to hours.
FILTER_HOURS=$(( DAYS * 24 ))
FILTER="until=${FILTER_HOURS}h"

# ---------------------------------------------------------------------------
# Pre-flight report: what does the system look like right now?
# ---------------------------------------------------------------------------
log "${BLUE}== Docker disk usage (before) ==${NC}"
BEFORE_DF=$(docker system df)
log "$BEFORE_DF"
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

DRY_FLAG=""
if [[ "$DRY_RUN" == true ]]; then
    DRY_FLAG="--dry-run"
fi

run() {
    local desc="$1"; shift
    log "${GREEN}->${NC} ${desc}"
    if [[ "$DRY_RUN" == true ]]; then
        # docker prune supports --dry-run natively on recent versions;
        # fall back to just printing the command if unsupported.
        if "$@" --dry-run >/tmp/docker-cleanup-out 2>&1; then
            cat /tmp/docker-cleanup-out | while read -r line; do log "   $line"; done
        else
            log "   (dry-run not supported by this Docker version for this command; skipping actual call)"
            log "   Would run: $*"
        fi
    else
        "$@" -f 2>&1 | while read -r line; do log "   $line"; done
    fi
}

# ---------------------------------------------------------------------------
# 1. Stopped containers older than threshold
# ---------------------------------------------------------------------------
run "Removing stopped containers older than ${DAYS}d..." \
    docker container prune --filter "$FILTER"

# ---------------------------------------------------------------------------
# 2. Dangling images (always safe: untagged, unreferenced layers)
# ---------------------------------------------------------------------------
run "Removing dangling images older than ${DAYS}d..." \
    docker image prune --filter "$FILTER"

# ---------------------------------------------------------------------------
# 3. Unused (not just dangling) images  -- opt-in, more aggressive
# ---------------------------------------------------------------------------
if [[ "$PRUNE_IMAGES" == true ]]; then
    run "Removing ALL unused images older than ${DAYS}d..." \
        docker image prune --all --filter "$FILTER"
fi

# ---------------------------------------------------------------------------
# 4. Unused networks
# ---------------------------------------------------------------------------
run "Removing unused networks older than ${DAYS}d..." \
    docker network prune --filter "$FILTER"

# ---------------------------------------------------------------------------
# 5. Dangling volumes -- opt-in, DESTRUCTIVE (can delete data)
# ---------------------------------------------------------------------------
if [[ "$PRUNE_VOLUMES" == true ]]; then
    log "${YELLOW}Warning: pruning volumes can permanently delete data not referenced by a running container.${NC}"
    run "Removing dangling volumes..." \
        docker volume prune
fi

# ---------------------------------------------------------------------------
# 6. Builder cache -- opt-in
# ---------------------------------------------------------------------------
if [[ "$PRUNE_BUILD_CACHE" == true ]]; then
    run "Removing build cache older than ${DAYS}d..." \
        docker builder prune --filter "$FILTER"
fi

# ---------------------------------------------------------------------------
# Post-flight report
# ---------------------------------------------------------------------------
log ""
log "${BLUE}== Docker disk usage (after) ==${NC}"
AFTER_DF=$(docker system df)
log "$AFTER_DF"

log ""
if [[ "$DRY_RUN" == true ]]; then
    log "${YELLOW}Dry run complete. No resources were actually removed.${NC}"
else
    log "${GREEN}Cleanup complete.${NC}"
fi

if [[ -n "$LOG_FILE" ]]; then
    log "Log written to: $LOG_FILE"
fi
