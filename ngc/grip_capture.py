"""GUI -> bridge handshake for learning which button a grip (GL/GR) should act as.

The GUI writes a request file; the bridge, which owns the controller link, picks
the next physical button pressed on that controller and writes a result file.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from . import protocol as P
from .config import ASSIGNABLE_BUTTONS, CONFIG_DIR

REQUEST_PATH = CONFIG_DIR / "grip_capture.json"
RESULT_PATH = CONFIG_DIR / "grip_capture_result.json"
REQUEST_TTL_S = 30.0
_CAPTURE_MASKS = {name: P.SWITCH_BUTTONS[name] for name in ASSIGNABLE_BUTTONS}


def _write(path, payload: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def request(mac: str, grip: str, clear: bool = False) -> None:
    RESULT_PATH.unlink(missing_ok=True)
    _write(REQUEST_PATH, {"mac": mac.upper(), "grip": grip.upper(), "clear": clear, "at": time.time()})


def cancel() -> None:
    REQUEST_PATH.unlink(missing_ok=True)


def read_result(mac: str, grip: str) -> Optional[dict]:
    try:
        data = json.loads(RESULT_PATH.read_text())
    except Exception:
        return None
    if data.get("mac") == mac.upper() and data.get("grip") == grip.upper():
        return data
    return None


def pending_for(mac: str) -> Optional[dict]:
    """Return the pending request ({"grip", "clear"}) for this controller, if any."""
    try:
        data = json.loads(REQUEST_PATH.read_text())
    except Exception:
        return None
    if data.get("mac") != mac.upper():
        return None
    if time.time() - float(data.get("at") or 0) > REQUEST_TTL_S:
        REQUEST_PATH.unlink(missing_ok=True)
        return None
    return data


def first_pressed(buttons: int) -> Optional[str]:
    for name, mask in _CAPTURE_MASKS.items():
        if buttons & mask:
            return name
    return None


def finish(mac: str, grip: str, button: Optional[str]) -> None:
    _write(RESULT_PATH, {"mac": mac.upper(), "grip": grip, "button": button, "at": time.time()})
    REQUEST_PATH.unlink(missing_ok=True)
