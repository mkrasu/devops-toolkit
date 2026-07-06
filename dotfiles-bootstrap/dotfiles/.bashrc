# ~/.bashrc — starter config linked by bootstrap.sh
# Customize freely; this is meant as a sane baseline, not gospel.

case $- in
    *i*) ;;
      *) return ;;
esac

# --- History ---------------------------------------------------------------
HISTSIZE=10000
HISTFILESIZE=20000
HISTCONTROL=ignoreboth:erasedups
shopt -s histappend
shopt -s checkwinsize

# --- Prompt ------------------------------------------------------------------
# user@host path (git-branch) $
parse_git_branch() {
    git branch 2>/dev/null | sed -n '/^\*/s/^\* //p'
}
PS1='\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\[\033[33m\]$(b=$(parse_git_branch); [ -n "$b" ] && echo " ($b)")\[\033[00m\]\$ '

# --- Aliases -----------------------------------------------------------------
alias ll='ls -alFh'
alias la='ls -A'
alias l='ls -CF'
alias grep='grep --color=auto'
alias ..='cd ..'
alias ...='cd ../..'
alias df='df -h'
alias du='du -h'
alias ports='netstat -tulanp 2>/dev/null || ss -tulanp'

alias gs='git status'
alias gd='git diff'
alias ga='git add'
alias gc='git commit'
alias gp='git push'
alias gl='git log --oneline --graph --decorate -n 20'

if command -v bat >/dev/null 2>&1; then
    alias cat='bat --paging=never'
fi

# --- Editor / misc -------------------------------------------------------------
export EDITOR=vim
export VISUAL=vim
export LESS='-R'

# --- fzf (if installed) ---------------------------------------------------------
[ -f /usr/share/doc/fzf/examples/key-bindings.bash ] && source /usr/share/doc/fzf/examples/key-bindings.bash
[ -f ~/.fzf.bash ] && source ~/.fzf.bash

# --- Local overrides -------------------------------------------------------------
# Keep machine-specific stuff out of the tracked dotfile.
[ -f ~/.bashrc.local ] && source ~/.bashrc.local
