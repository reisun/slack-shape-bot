#!/bin/bash
set -e

if [ -d /mnt/gh ]; then
  mkdir -p "$HOME/.config"
  rm -rf "$HOME/.config/gh"
  ln -s /mnt/gh "$HOME/.config/gh"
fi

# Use gh as git credential helper so git push works with gh auth
gh auth setup-git 2>/dev/null || true

exec "$@"
