#!/usr/bin/env bash
# Install the NSO GameCube bridge on Bazzite (immutable-friendly, no sudo):
#   - bootstrap uv + a Python 3.12 venv with the known-good bleak 0.22.2 + evdev
#   - install the systemd --user service
# Run from inside the project directory (the rsynced ~/nso-gc-bazzite).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo ">> installing uv (user-space)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo ">> creating Python 3.12 venv (.venv312)..."
uv python install 3.12
[ -d .venv312 ] || uv venv --python 3.12 .venv312

echo ">> installing dependencies (bleak 0.22.2 pin + evdev)..."
uv pip install --python .venv312 "bleak==0.22.2" evdev

echo ">> installing systemd --user service..."
mkdir -p "$HOME/.config/systemd/user"
cp systemd/nso-gc.service "$HOME/.config/systemd/user/nso-gc.service"
systemctl --user daemon-reload

cat <<EOF

Installed. Next steps:
  1. Pair the controller (put it in pairing mode first):
       .venv312/bin/python -m ngc pair
  2. Enable + start the background service:
       systemctl --user enable --now nso-gc.service
  3. Check status / logs:
       systemctl --user status nso-gc.service
       journalctl --user -u nso-gc.service -f
EOF
