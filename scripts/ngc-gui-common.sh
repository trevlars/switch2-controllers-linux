#!/usr/bin/env bash
# Shared helpers for Desktop-friendly ngc launchers (zenity / kdialog).
set -euo pipefail

PROJECT_DIR="${NGC_PROJECT_DIR:-$HOME/nso-gc-bazzite}"
PY="${NGC_PYTHON:-$PROJECT_DIR/.venv312/bin/python}"
SERVICE="nso-gc.service"

ngc_info() {
  local text="$1"
  if command -v zenity >/dev/null 2>&1; then
    zenity --info --no-wrap --width=420 --text="$text"
  elif command -v kdialog >/dev/null 2>&1; then
    kdialog --msgbox "$text"
  else
    echo "$text"
  fi
}

ngc_error() {
  local text="$1"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --no-wrap --width=420 --text="$text"
  elif command -v kdialog >/dev/null 2>&1; then
    kdialog --error "$text"
  else
    echo "ERROR: $text" >&2
  fi
}

ngc_yesno() {
  local text="$1"
  if command -v zenity >/dev/null 2>&1; then
    zenity --question --no-wrap --width=420 --text="$text"
    return $?
  elif command -v kdialog >/dev/null 2>&1; then
    kdialog --yesno "$text"
    return $?
  else
    read -r -p "$text [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]]
  fi
}

ngc_require_project() {
  if [[ ! -x "$PY" ]]; then
    ngc_error "Switch 2 bridge is not installed yet.

Run this once from a terminal:
  cd $PROJECT_DIR && bash scripts/install.sh

Or use the Desktop shortcut: Switch 2 Controllers — First-Time Setup"
    exit 1
  fi
}
