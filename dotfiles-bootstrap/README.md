# dotfiles-bootstrap

Provisions a fresh dev machine in one command: installs a curated set of
common CLI tools for your OS, then symlinks starter dotfiles into `$HOME`
— safely backing up anything that already exists first.

## Why

Setting up a new laptop or VM the same way every time is tedious and easy
to get subtly wrong by hand. This script makes it reproducible: run it
once on a fresh box and you get the same shell, editor, and git config
every time, with no risk of silently clobbering files that were already
there.

## What it does

1. Detects your OS (Debian/Ubuntu, Fedora/RHEL, Arch, or macOS)
2. Installs packages from the matching list in `packages/` using the
   right package manager (`apt`, `dnf`, `pacman`, or `brew`)
3. Symlinks each file in `dotfiles/` into `$HOME` — if a file already
   exists there and isn't already the correct symlink, it's moved to a
   timestamped backup folder first, never overwritten in place
4. Skips anything already correctly linked (safe to re-run any time)

## Requirements

- Bash 4+
- `sudo` access (for package installation) — or Homebrew already
  installed on macOS
- No other dependencies

## Usage

```bash
chmod +x bootstrap.sh

# Always preview first
./bootstrap.sh --dry-run

# Run for real, with confirmation prompt
./bootstrap.sh

# Run for real, no prompts (e.g. in an automated VM provisioning step)
./bootstrap.sh --yes

# Only re-link dotfiles, skip package installation
./bootstrap.sh --skip-packages --yes

# Only install packages, skip dotfiles
./bootstrap.sh --skip-dotfiles --yes
```

`--os` is mainly a development/testing aid — it lets you verify the
package-manager command generated for each OS branch (apt/dnf/pacman/brew)
without needing a VM for each one. It doesn't let you meaningfully "preview"
a real install on an OS you're not actually running, since it can't check
real package availability there:

```bash
./bootstrap.sh --dry-run --os fedora   # check the dnf command it would generate
```

### Options

| Flag | Description |
|---|---|
| `-n, --dry-run` | Show what would happen, change nothing |
| `-y, --yes` | Skip the confirmation prompt |
| `--skip-packages` | Only link dotfiles, don't install packages |
| `--skip-dotfiles` | Only install packages, don't touch dotfiles |
| `--os OS` | Override OS detection: `debian` \| `fedora` \| `arch` \| `macos` |
| `-h, --help` | Show usage |

## What's included

### Dotfiles (`dotfiles/`)

| File | Covers |
|---|---|
| `.bashrc` | Sane history settings, a git-branch-aware prompt, common aliases, `EDITOR`/`VISUAL`, optional fzf key bindings |
| `.gitconfig` | Useful aliases (`git st`, `git lg`, ...), sane defaults (`init.defaultBranch=main`, `pull.rebase=false`). Identity (name/email) is deliberately **not** set here — see below |
| `.vimrc` | Line numbers, sane search/indent defaults, persistent undo, no swap/backup file clutter |
| `.tmux.conf` | Mouse support, vim-style pane navigation, `Ctrl-a` prefix, sensible status bar |

These are meant as a starting point — edit them to match your own taste
before or after running the script. They're plain text, not templates.

### Packages (`packages/`)

One package-manager-specific list per OS (`apt.txt`, `dnf.txt`,
`pacman.txt`, `brew.txt`) — plain text, one package per line, `#` for
comments. Add or remove packages by editing these files directly; no code
changes needed.

Default set: `git curl wget vim tmux htop jq ripgrep fzf bat tree unzip`
plus a compiler toolchain (`build-essential` / `base-devel`, where
applicable).

## Setting your git identity

`.gitconfig` intentionally leaves `[user]` unset so the tracked dotfile
stays generic and shareable. Create `~/.gitconfig.local` (not tracked)
with your details:

```ini
[user]
    name = Your Name
    email = you@example.com
```

`.gitconfig` includes this file automatically once it's linked.

## Safety notes

- Nothing is overwritten in place. If `~/.bashrc` already exists and
  isn't already the symlink this script would create, it's moved to
  `~/.dotfiles_backup/<timestamp>/` before the new symlink is made.
- Re-running the script is safe — already-correct symlinks are left alone
  and won't trigger a new backup.
- Package installation and dotfile linking are independent steps
  (`--skip-packages` / `--skip-dotfiles`) so you can use just the part
  you need, e.g. only re-linking dotfiles on a machine that already has
  everything installed.
- Always run `--dry-run` first on a machine you're unfamiliar with.

## Customizing

Fork this folder and edit freely:
- Add dotfiles by dropping more `.something` files into `dotfiles/`
- Add/remove packages by editing the relevant file in `packages/`
- Add a new OS by adding a case in `detect_os()` / `pkg_*` functions in
  `bootstrap.sh` and a matching package list file

## License

MIT — see the top-level [LICENSE](../LICENSE) in this repo.
