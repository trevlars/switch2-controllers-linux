#!/usr/bin/env bash
# Install / update the Decky Loader plugin for Game Mode control.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$PROJECT_DIR/decky/switch2-controllers"
PLUGIN_NAME="Switch2Controllers"
ZIP="$PROJECT_DIR/decky/${PLUGIN_NAME}.zip"

if [[ ! -d "$SRC" ]]; then
  echo "Decky plugin source missing at $SRC" >&2
  exit 1
fi

find_decky_plugins() {
  local candidates=(
    "$HOME/homebrew/plugins"
    "$HOME/.local/share/decky/plugins"
    "$HOME/homebrew/decky-loader/plugins"
  )
  for d in "${candidates[@]}"; do
    if [[ -d "$d" ]]; then
      echo "$d"
      return 0
    fi
  done
  return 1
}

build_frontend() {
  if [[ -f "$SRC/dist/index.js" ]]; then
    return 0
  fi
  if command -v pnpm >/dev/null 2>&1; then
    echo ">> building Decky frontend (pnpm)..."
    (cd "$SRC" && pnpm install && pnpm run build)
  elif command -v npm >/dev/null 2>&1; then
    echo ">> building Decky frontend (npm)..."
    (cd "$SRC" && npm install && npm run build)
  else
    echo "ERROR: dist/index.js missing and pnpm/npm not available." >&2
    return 1
  fi
}

make_zip() {
  echo ">> packaging $ZIP"
  rm -f "$ZIP"
  tmp="$(mktemp -d)"
  mkdir -p "$tmp/$PLUGIN_NAME/dist"
  cp "$SRC/main.py" "$SRC/plugin.json" "$SRC/package.json" "$tmp/$PLUGIN_NAME/"
  cp "$SRC/dist/index.js" "$tmp/$PLUGIN_NAME/dist/"
  [[ -f "$PROJECT_DIR/LICENSE" ]] && cp "$PROJECT_DIR/LICENSE" "$tmp/$PLUGIN_NAME/"
  (cd "$tmp" && zip -qr "$ZIP" "$PLUGIN_NAME")
  rm -rf "$tmp"
}

install_to() {
  local dest="$1/$PLUGIN_NAME"
  echo ">> installing Decky plugin -> $dest"
  mkdir -p "$dest/dist"
  cp "$SRC/main.py" "$SRC/plugin.json" "$SRC/package.json" "$dest/"
  cp "$SRC/dist/index.js" "$dest/dist/index.js"
  [[ -f "$PROJECT_DIR/LICENSE" ]] && cp "$PROJECT_DIR/LICENSE" "$dest/"
}

if [[ "${1:-}" == "--install-only" ]]; then
  install_to "${2:?plugins dir required}"
  exit 0
fi

build_frontend
make_zip

PLUGINS_DIR="$(find_decky_plugins || true)"
if [[ -z "${PLUGINS_DIR:-}" ]]; then
  echo "Decky plugins directory not found."
  echo "Install the zip manually in Decky → Settings → Developer → Install plugin."
  echo "Zip: $ZIP"
  exit 0
fi

if install_to "$PLUGINS_DIR" 2>/dev/null; then
  :
else
  echo ""
  echo "Decky plugins folder is root-owned on Bazzite. Run once in a terminal:"
  echo "  sudo bash $PROJECT_DIR/scripts/install-decky.sh --install-only $PLUGINS_DIR"
  echo ""
  echo "Or in Decky: Settings → Developer → Install plugin from zip"
  echo "  $ZIP"
  exit 0
fi

cat <<EOF
Decky plugin installed.

Game Mode: Quick Access (⋯) → Decky → Switch 2 Controllers

If it does not appear, restart Decky or reinstall from:
  $ZIP
EOF
