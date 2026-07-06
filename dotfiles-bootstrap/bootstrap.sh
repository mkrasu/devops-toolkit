#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# bootstrap.sh — provision a fresh dev machine: install common CLI tools
# and symlink dotfiles into $HOME, safely and idempotently.
#
# Supports Debian/Ubuntu (apt), Fedora/RHEL (dnf), Arch (pacman), and
# macOS (Homebrew). Existing dotfiles are backed up before being replaced,
# never silently overwritten.
#
# Usage:
#   ./bootstrap.sh [OPTIONS]
#
# Options:
#   -n, --dry-run          Show what would happen, change nothing
#   -y, --yes                Skip confirmation prompts
#   --skip-packages          Only link dotfiles, don't install packages
#   --skip-dotfiles          Only install packages, don't touch dotfiles
#   --os OS                  Override OS detection: debian|fedora|arch|macos
#   -h, --help                Show this help message
#
# Examples:
#   ./bootstrap.sh --dry-run
#   ./bootstrap.sh --yes
#   ./bootstrap.sh --skip-packages          # just re-link dotfiles
#   ./bootstrap.sh --os debian --dry-run    # preview on a different OS
#
set -euo pipefail

# Require Bash 4+ (we use `mapfile`). macOS ships 3.2 as /bin/bash, so Mac
# users need a newer bash — `brew install bash` — and to run this with it.
if ((BASH_VERSINFO[0] < 4)); then
    echo "Error: this script needs Bash 4+ (you have ${BASH_VERSION}). On macOS: brew install bash." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Dotfiles live in dotfiles/ and the per-OS package lists in packages/,
# both alongside this script.
DOTFILES_DIR="${SCRIPT_DIR}/dotfiles"
PACKAGES_DIR="${SCRIPT_DIR}/packages"
BACKUP_DIR="${HOME}/.dotfiles_backup/$(date +%Y%m%d-%H%M%S)"

VALID_OSES="debian fedora arch macos"

DRY_RUN=false
ASSUME_YES=false
SKIP_PACKAGES=false
SKIP_DOTFILES=false
OS_OVERRIDE=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}==>${NC} $*"; }
ok()    { echo -e "${GREEN}  ✓${NC} $*"; }
warn()  { echo -e "${YELLOW}  !${NC} $*"; }
die()   { echo -e "${RED}Error:${NC} $*" >&2; exit 1; }

usage() {
    sed -n '4,26p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--dry-run) DRY_RUN=true; shift ;;
        -y|--yes) ASSUME_YES=true; shift ;;
        --skip-packages) SKIP_PACKAGES=true; shift ;;
        --skip-dotfiles) SKIP_DOTFILES=true; shift ;;
        --os) OS_OVERRIDE="${2:?--os requires a value}"; shift 2 ;;
        -h|--help) usage ;;
        *) die "Unknown option: $1 (use --help for usage)" ;;
    esac
done

if [[ -n "$OS_OVERRIDE" && " $VALID_OSES " != *" $OS_OVERRIDE "* ]]; then
    die "Unknown --os value '$OS_OVERRIDE'. Valid: ${VALID_OSES// /, }."
fi

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------
detect_os() {
    if [[ -n "$OS_OVERRIDE" ]]; then
        echo "$OS_OVERRIDE"
        return
    fi
    case "$(uname -s)" in
        Darwin) echo "macos"; return ;;
    esac
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        source /etc/os-release
        case "${ID:-}" in
            ubuntu|debian) echo "debian"; return ;;
            fedora|rhel|centos|rocky|almalinux) echo "fedora"; return ;;
            arch|manjaro) echo "arch"; return ;;
        esac
        case "${ID_LIKE:-}" in
            *debian*) echo "debian"; return ;;
            *fedora*|*rhel*) echo "fedora"; return ;;
            *arch*) echo "arch"; return ;;
        esac
    fi
    echo "unknown"
}

OS="$(detect_os)"
[[ "$OS" != "unknown" ]] || die "Could not detect OS. Re-run with --os debian|fedora|arch|macos."

pkg_install_cmd() {
    case "$OS" in
        debian) echo "sudo apt-get install -y" ;;
        fedora) echo "sudo dnf install -y" ;;
        arch)   echo "sudo pacman -S --needed --noconfirm" ;;
        macos)  echo "brew install" ;;
    esac
}

pkg_update_cmd() {
    case "$OS" in
        debian) echo "sudo apt-get update" ;;
        fedora) echo "sudo dnf check-update || true" ;;
        arch)   echo "sudo pacman -Sy" ;;
        macos)  echo "brew update" ;;
    esac
}

pkg_list_file() {
    case "$OS" in
        debian) echo "${PACKAGES_DIR}/apt.txt" ;;
        fedora) echo "${PACKAGES_DIR}/dnf.txt" ;;
        arch)   echo "${PACKAGES_DIR}/pacman.txt" ;;
        macos)  echo "${PACKAGES_DIR}/brew.txt" ;;
    esac
}

# ---------------------------------------------------------------------------
# Package installation
# ---------------------------------------------------------------------------
install_packages() {
    local list_file
    list_file="$(pkg_list_file)"
    [[ -f "$list_file" ]] || { warn "No package list for OS '$OS' at $list_file, skipping."; return; }

    # Strip comments/blank lines
    mapfile -t packages < <(grep -vE '^\s*(#|$)' "$list_file")
    [[ ${#packages[@]} -gt 0 ]] || { warn "Package list $list_file is empty, skipping."; return; }

    info "Installing ${#packages[@]} package(s) for $OS: ${packages[*]}"

    if [[ "$DRY_RUN" == true ]]; then
        warn "[dry-run] Would run: $(pkg_update_cmd)"
        warn "[dry-run] Would run: $(pkg_install_cmd) ${packages[*]}"
        return
    fi

    if [[ "$OS" == "macos" ]] && ! command -v brew >/dev/null 2>&1; then
        die "Homebrew not found. Install it first: https://brew.sh"
    fi

    eval "$(pkg_update_cmd)"
    eval "$(pkg_install_cmd) ${packages[*]}"
    ok "Packages installed."
}

# ---------------------------------------------------------------------------
# Dotfile linking (safe: backs up existing files, idempotent)
# ---------------------------------------------------------------------------
link_dotfiles() {
    [[ -d "$DOTFILES_DIR" ]] || die "Dotfiles directory not found: $DOTFILES_DIR"

    local files
    # Every file in dotfiles/ is a dotfile to link (they're all hidden '.' files).
    files=$(find "$DOTFILES_DIR" -maxdepth 1 -type f -name '.*')
    [[ -n "$files" ]] || { warn "No dotfiles found in $DOTFILES_DIR, skipping."; return; }

    local backed_up=false
    local n_linked=0 n_backed=0 n_skipped=0

    while IFS= read -r src; do
        local name target
        name="$(basename "$src")"
        target="${HOME}/${name}"

        if [[ -L "$target" && "$(readlink "$target")" == "$src" ]]; then
            ok "$name already linked, skipping."
            ((n_skipped++)) || true
            continue
        fi

        if [[ -e "$target" || -L "$target" ]]; then
            if [[ "$DRY_RUN" == true ]]; then
                warn "[dry-run] Would back up existing $target -> $BACKUP_DIR/$name"
            else
                mkdir -p "$BACKUP_DIR"
                mv "$target" "$BACKUP_DIR/$name"
                backed_up=true
                warn "Backed up existing $target -> $BACKUP_DIR/$name"
            fi
            ((n_backed++)) || true
        fi

        if [[ "$DRY_RUN" == true ]]; then
            warn "[dry-run] Would symlink $target -> $src"
        else
            ln -s "$src" "$target"
            ok "Linked $name"
        fi
        ((n_linked++)) || true
    done <<< "$files"

    if [[ "$backed_up" == true ]]; then
        info "Existing dotfiles backed up to: $BACKUP_DIR"
    fi
    info "Dotfiles: ${n_linked} linked, ${n_backed} backed up, ${n_skipped} already correct."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
info "Detected OS: $OS"
info "Dry run: $DRY_RUN"
echo ""

if [[ "$ASSUME_YES" == false && "$DRY_RUN" == false ]]; then
    read -r -p "Proceed with bootstrap? [y/N] " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

if [[ "$SKIP_PACKAGES" == false ]]; then
    info "Installing packages..."
    install_packages
    echo ""
else
    warn "Skipping package installation (--skip-packages)."
fi

if [[ "$SKIP_DOTFILES" == false ]]; then
    info "Linking dotfiles..."
    link_dotfiles
    echo ""
else
    warn "Skipping dotfile linking (--skip-dotfiles)."
fi

# Tidy up: drop the timestamped backup dir (and its parent) if nothing landed
# there, so repeated clean runs don't litter ~/.dotfiles_backup.
rmdir "$BACKUP_DIR" 2>/dev/null || true
rmdir "${HOME}/.dotfiles_backup" 2>/dev/null || true

if [[ "$DRY_RUN" == true ]]; then
    echo -e "${YELLOW}Dry run complete. Nothing was changed.${NC}"
else
    echo -e "${GREEN}Bootstrap complete.${NC} Open a new shell (or 'source ~/.bashrc') to pick up changes."
fi
