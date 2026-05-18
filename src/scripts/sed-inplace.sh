#!/bin/bash
#
# Symlink-safe `sed -i` wrapper.
#
# Sourced by configure-shell-helper.sh, migrate-from-claudeq.sh, and
# uninstall-helper.sh — every place we edit the user's ~/.zshrc or
# ~/.bashrc.  Without this, users whose RC file is a symlink (chezmoi /
# stow / dotbot / any hand-rolled dotfiles repo) hit BSD sed's
#
#   sed: <path>: in-place editing only works for regular files
#
# and `make install` / `leap --update` / migration / uninstall all blow
# up halfway through.
#
# sed_inplace tries the normal `sed -i.bak` path first (preserves
# atomic-rename semantics for regular files); on failure it falls back
# to writing the result to a tempfile and `cat`-ing it through the
# symlink to the underlying target.  The fallback covers any
# non-regular file BSD sed refuses (symlink, FIFO, device, …) without
# us having to enumerate them.
#
# Usage:
#   . "$(dirname "$0")/sed-inplace.sh"
#   sed_inplace '/pattern/d' "$RC_FILE"

sed_inplace() {
    local pattern="$1" file="$2" tmp
    # Fast path: regular file. BSD sed errors out atomically (no
    # partial write) before touching the file when the target isn't
    # regular, so the fallback runs on the original content.
    if sed -i.bak "$pattern" "$file" 2>/dev/null; then
        rm -f "$file.bak"
        return 0
    fi
    # Fallback: write through whatever the path resolves to. `>` follows
    # symlinks, so this updates the dotfile-repo target while leaving the
    # symlink at ~/.zshrc intact.
    tmp=$(mktemp) || return 1
    if sed "$pattern" "$file" > "$tmp" && cat "$tmp" > "$file"; then
        rm -f "$tmp"
        return 0
    fi
    rm -f "$tmp"
    return 1
}

# Install the contents of $1 as the new content of $2, preserving any
# symlink chain at $2. For a regular file (or missing target), this is
# an atomic `mv`; for a symlink, it writes through via `cat >` so the
# user's dotfile-manager setup (chezmoi / stow / dotbot / hand-rolled
# `ln -s`) keeps working.
#
# Usage:
#   awk '...' "$RC_FILE" > "$RC_FILE.trim" && replace_file "$RC_FILE.trim" "$RC_FILE"
replace_file() {
    local src="$1" dst="$2"
    if [ -L "$dst" ]; then
        cat "$src" > "$dst" && rm -f "$src"
    else
        mv "$src" "$dst"
    fi
}
