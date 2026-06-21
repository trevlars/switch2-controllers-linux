#!/usr/bin/env bash
# Sync the project source to the Bazzite box for running/testing.
# Usage: scripts/deploy.sh [ssh-host]
set -euo pipefail

HOST="${1:-bazzite}"
DEST="~/nso-gc-bazzite"
SRC="$(cd "$(dirname "$0")/.." && pwd)/"

rsync -az --delete \
  --exclude '.venv*' \
  --exclude 'references/' \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "$SRC" "$HOST:$DEST/"

echo "synced -> $HOST:$DEST"
