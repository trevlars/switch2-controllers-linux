#!/usr/bin/env bash
# Launch the Switch 2 Controllers control panel (GTK on Wayland, zenity fallback).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ngc-gui-common.sh
source "$SCRIPT_DIR/ngc-gui-common.sh"

LOG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nso-gc"
LOG_FILE="$LOG_DIR/gui.log"
mkdir -p "$LOG_DIR"

ngc_setup_gui_env() {
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
  if [[ -z "${WAYLAND_DISPLAY:-}" && -d "$XDG_RUNTIME_DIR" ]]; then
    local sock
    for sock in "$XDG_RUNTIME_DIR"/wayland-*; do
      [[ -S "$sock" ]] || continue
      export WAYLAND_DISPLAY="${sock##*/}"
      break
    done
  fi
  if [[ -z "${DISPLAY:-}" && -n "${WAYLAND_DISPLAY:-}" ]]; then
    export DISPLAY="${DISPLAY:-:0}"
  fi
  export GDK_BACKEND="${GDK_BACKEND:-wayland}"
}

ngc_show_error() {
  local text="$1"
  printf '%s\n' "$text" >>"$LOG_FILE"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --no-wrap --width=460 --title="Switch 2 Controllers" --text="$text" 2>/dev/null || true
  elif command -v kdialog >/dev/null 2>&1; then
    kdialog --error "$text" 2>/dev/null || true
  else
    echo "ERROR: $text" >&2
  fi
}

ngc_launch_gtk() {
  local py=""
  for candidate in python3 python3.14 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c "import gi; gi.require_version('Gtk','3.0')" 2>/dev/null; then
      py="$candidate"
      break
    fi
  done
  if [[ -z "$py" ]]; then
    echo "=== $(date -Iseconds) no python with Gtk found ===" >>"$LOG_FILE"
    return 1
  fi
  ngc_setup_gui_env
  export NGC_PYTHON="$PY"
  {
    echo "=== $(date -Iseconds) launch via $py WAYLAND=${WAYLAND_DISPLAY:-} DISPLAY=${DISPLAY:-} ==="
    exec "$py" "$SCRIPT_DIR/ngc_gui.py"
  } >>"$LOG_FILE" 2>&1
}

# --- zenity fallback (no GTK / display unavailable) ---
ngc_simple_status() {
  local svc live pads
  svc="$(systemctl --user is-active "$SERVICE" 2>/dev/null || echo inactive)"
  live=""
  if [[ "$svc" == "active" && -f "$LOG_DIR/state.json" ]]; then
    live="$("$PY" - <<'PY' 2>/dev/null || true
import json, os
p = os.path.expanduser("~/.config/nso-gc/state.json")
try:
    d = json.load(open(p))
    names = [c.get("name") or "Controller" for c in d.get("controllers") or [] if c.get("connected")]
    print(", ".join(names))
except Exception:
    pass
PY
)"
  fi
  pads=""
  if [[ -x "$PY" ]]; then
    pads="$(cd "$PROJECT_DIR" && "$PY" -m ngc list 2>/dev/null | tail -n +2 || true)"
  fi
  if [[ -n "$live" ]]; then
    echo "● Connected — $live"
  elif [[ -z "$pads" ]]; then
    echo "● Add your first controller (hold Sync once)"
  else
    echo "● Ready — press any button on your pad"
  fi
  echo
  echo "Bridge: $svc"
  [[ -n "$pads" ]] && echo "$pads"
}

ngc_ensure_running() {
  systemctl --user reset-failed "$SERVICE" 2>/dev/null || true
  if [[ -x "$PY" ]]; then
    systemctl --user enable --now "$SERVICE" 2>/dev/null || true
  else
    bash "$PROJECT_DIR/scripts/install.sh"
    systemctl --user enable --now "$SERVICE" 2>/dev/null || true
  fi
}

ngc_zenity_pair() {
  ngc_require_project
  ngc_info "Hold Sync until the player LEDs sweep, then click OK."
  systemctl --user stop "$SERVICE" 2>/dev/null || true
  LOG="$(mktemp /tmp/ngc-pair.XXXXXX.log)"
  set +e
  (cd "$PROJECT_DIR" && "$PY" -m ngc pair --timeout 45) >"$LOG" 2>&1
  local rc=$?
  set -e
  systemctl --user enable --now "$SERVICE" 2>/dev/null || true
  if [[ $rc -eq 0 ]]; then
    ngc_info "Controller added.\n\nPress any button on the pad to connect."
  else
    ngc_error "Could not add controller.\n\n$(tail -15 "$LOG")"
  fi
  rm -f "$LOG"
}

ngc_zenity_loop() {
  ngc_ensure_running
  while true; do
    status="$(ngc_simple_status)"
    if command -v zenity >/dev/null 2>&1; then
      action="$(zenity --list --title="Switch 2 Controllers" --width=460 --height=340 \
        --text="$status" \
        --column="" --column="" \
        "Add controller" "Hold Sync once per pad" \
        "Restart bridge" "If controllers won't connect" \
        "Close" "" \
        2>/dev/null || echo Close)"
    else
      echo "$status"
      read -r -p "Add controller? [y/N] " ans
      [[ "$ans" =~ ^[Yy]$ ]] && action="Add controller" || action="Close"
    fi
    [[ -z "$action" || "$action" == "Close" ]] && exit 0
    case "$action" in
      "Add controller") ngc_zenity_pair ;;
      "Restart bridge") systemctl --user restart "$SERVICE" 2>/dev/null || true ;;
      *) exit 0 ;;
    esac
  done
}

ngc_ensure_running

if ngc_launch_gtk; then
  exit 0
fi

ngc_show_error "Could not open the control panel window.

Falling back to a simple menu instead.

Details: $LOG_FILE"
ngc_zenity_loop
