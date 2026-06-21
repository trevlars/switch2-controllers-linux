"""Shared control-plane helpers for GTK GUI and Decky plugin."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

PROJECT_DIR = Path(os.environ.get("NGC_PROJECT_DIR", Path.home() / "nso-gc-bazzite"))
PY = Path(os.environ.get("NGC_PYTHON", PROJECT_DIR / ".venv312" / "bin" / "python"))
SERVICE = "nso-gc.service"
STATE_PATH = Path.home() / ".config" / "nso-gc" / "state.json"
STATE_STALE_S = 8.0


@dataclass
class PadStatus:
    player: int
    name: str
    mac: str
    bonded: bool
    connected: bool = False
    battery_pct: Optional[int] = None


def _run(cmd: list[str], *, timeout: float = 30.0, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd or str(PROJECT_DIR))


def service_state() -> str:
    r = _run(["systemctl", "--user", "is-active", SERVICE], timeout=5)
    return (r.stdout or "inactive").strip() or "inactive"


def ensure_service() -> None:
    _run(["systemctl", "--user", "reset-failed", SERVICE], timeout=5)
    _run(["systemctl", "--user", "enable", "--now", SERVICE], timeout=15)


def restart_service() -> None:
    _run(["systemctl", "--user", "reset-failed", SERVICE], timeout=5)
    _run(["systemctl", "--user", "restart", SERVICE], timeout=15)


def load_pads() -> list[PadStatus]:
    if PY.is_file():
        r = _run([str(PY), "-m", "ngc", "list"], timeout=10)
        pads: list[PadStatus] = []
        row_re = re.compile(
            r"P(\d+)\s+([0-9A-F:]{17})\s+(.+?)\s+\[(.+)\]\s*$", re.IGNORECASE
        )
        for line in (r.stdout or "").splitlines():
            m = row_re.match(line.strip())
            if not m:
                continue
            flags = m.group(4).lower()
            pads.append(
                PadStatus(
                    int(m.group(1)),
                    m.group(3).strip(),
                    m.group(2).upper(),
                    "bonded" in flags and "needs bond" not in flags,
                )
            )
        if pads:
            return pads
    cfg_path = Path.home() / ".config" / "nso-gc" / "config.json"
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text())
            return [
                PadStatus(
                    c.get("player", i + 1),
                    c.get("name") or "Switch 2 Controller",
                    c["mac"].upper(),
                    bool(c.get("bonded", False)),
                )
                for i, c in enumerate(data.get("controllers") or [])
            ]
        except Exception:
            pass
    return []


def read_bridge_state() -> Optional[dict]:
    if not STATE_PATH.is_file():
        return None
    try:
        data = json.loads(STATE_PATH.read_text())
        updated = float(data.get("updated_at") or 0)
        if updated and time.time() - updated > STATE_STALE_S:
            return None
        return data
    except Exception:
        return None


def merge_state(pads: list[PadStatus], state: Optional[dict]) -> list[PadStatus]:
    if not state:
        return pads
    by_mac = {c.get("mac", "").upper(): c for c in state.get("controllers") or []}
    out: list[PadStatus] = []
    for pad in pads:
        live = by_mac.get(pad.mac, {})
        out.append(
            PadStatus(
                pad.player,
                live.get("name") or pad.name,
                pad.mac,
                pad.bonded,
                connected=bool(live.get("connected")),
                battery_pct=live.get("battery_pct"),
            )
        )
    return out


def pad_status_line(pad: PadStatus, service: str) -> str:
    if service != "active":
        return "Bridge stopped"
    if pad.connected:
        pct = f" · {pad.battery_pct}%" if pad.battery_pct is not None else ""
        return f"Connected{pct}"
    if not pad.bonded:
        return "Needs setup"
    return "Hold Sync to connect"


def get_status() -> dict[str, Any]:
    svc = service_state()
    pads = sorted(merge_state(load_pads(), read_bridge_state() if svc == "active" else None), key=lambda p: p.player)
    connected = sum(1 for p in pads if p.connected)
    state = read_bridge_state() if svc == "active" else None

    if svc != "active":
        headline, detail = "Bridge is off", "Start the bridge to connect controllers."
    elif state and state.get("hub_error"):
        headline, detail = "Needs attention", str(state.get("hub_error", ""))[:200]
    elif state:
        headline = str(state.get("headline") or "Ready")
        detail = str(state.get("detail") or "Hold Sync on a saved controller to connect.")
    elif not pads:
        headline, detail = "Get started", "Add your first controller with Sync."
    elif connected:
        names = ", ".join(f"P{p.player}" for p in pads if p.connected)
        headline, detail = f"{connected} connected", f"{names} — ready in Steam"
    else:
        headline, detail = "Ready", "Hold Sync on a saved controller to connect."

    return {
        "service": svc,
        "headline": headline,
        "detail": detail,
        "connected_count": connected,
        "pads": [
            {
                **asdict(p),
                "status": pad_status_line(p, svc),
            }
            for p in pads
        ],
    }


def run_ngc(args: list[str], *, timeout: float = 120.0, stop_service: bool = False) -> tuple[int, str]:
    if stop_service:
        _run(["systemctl", "--user", "stop", SERVICE], timeout=10)
    r = _run([str(PY), "-m", "ngc", *args], timeout=timeout)
    ensure_service()
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def run_config(args: list[str], *, restart: bool = True) -> tuple[int, str]:
    r = _run([str(PY), "-m", "ngc", *args], timeout=30)
    if restart:
        restart_service()
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def recent_logs(lines: int = 35) -> str:
    r = _run(
        ["journalctl", "--user", "-u", SERVICE, "-n", str(lines), "--no-pager", "-o", "cat"],
        timeout=10,
    )
    return (r.stdout or r.stderr or "(empty)").strip()[-3000:]
