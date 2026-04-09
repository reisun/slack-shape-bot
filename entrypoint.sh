#!/bin/bash
set -e

# Symlink host auth directories so token refreshes propagate immediately
if [ -d /mnt/claude ]; then
  rm -rf "$HOME/.claude"
  ln -s /mnt/claude "$HOME/.claude"
fi

# Symlink .claude.json from .claude dir or backups
if [ -f /mnt/claude/.claude.json ]; then
  ln -sf /mnt/claude/.claude.json "$HOME/.claude.json"
elif ls /mnt/claude/backups/.claude.json.backup.* 1>/dev/null 2>&1; then
  latest=$(ls -t /mnt/claude/backups/.claude.json.backup.* | head -1)
  ln -sf "$latest" "$HOME/.claude.json"
fi

if [ -d /mnt/gh ]; then
  mkdir -p "$HOME/.config"
  rm -rf "$HOME/.config/gh"
  ln -s /mnt/gh "$HOME/.config/gh"
fi

# Use gh as git credential helper so git push works with gh auth
gh auth setup-git 2>/dev/null || true

exec "$@"
