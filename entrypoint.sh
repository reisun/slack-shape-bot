#!/bin/bash
set -e

# Copy read-only mounted auth files to writable locations
if [ -d /mnt/claude ]; then
  cp -r /mnt/claude/. "$HOME/.claude/"
fi
if [ -f /mnt/claude.json ]; then
  cp /mnt/claude.json "$HOME/.claude.json"
fi
if [ -d /mnt/gh ]; then
  mkdir -p "$HOME/.config/gh"
  cp -r /mnt/gh/. "$HOME/.config/gh/"
fi

exec "$@"
