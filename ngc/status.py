"""Live bridge state for the GUI — written atomically, no log scraping."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .config import CONFIG_DIR

STATE_PATH = CONFIG_DIR / "state.json"


@dataclass
class ControllerState:
    mac: str
    player: int
    name: str = ""
    bonded: bool = False
    connected: bool = False
    battery_pct: Optional[int] = None


@dataclass
class BridgeState:
    updated_at: float = 0.0
    hub_alive: bool = False
    hub_scanning: bool = False
    hub_error: str = ""
    service: str = "starting"
    headline: str = ""
    detail: str = ""
    controllers: list[ControllerState] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BridgeState":
        controllers = [
            ControllerState(**{k: c[k] for k in c if k in ControllerState.__dataclass_fields__})
            for c in data.get("controllers") or []
        ]
        return cls(
            updated_at=float(data.get("updated_at") or 0),
            hub_alive=bool(data.get("hub_alive")),
            hub_scanning=bool(data.get("hub_scanning")),
            hub_error=str(data.get("hub_error") or ""),
            service=str(data.get("service") or ""),
            headline=str(data.get("headline") or ""),
            detail=str(data.get("detail") or ""),
            controllers=controllers,
        )


def _write_atomic(path: Path, payload: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def read_state() -> Optional[BridgeState]:
    if not STATE_PATH.exists():
        return None
    try:
        data = json.loads(STATE_PATH.read_text())
        return BridgeState.from_dict(data)
    except Exception:
        return None


def write_state(state: BridgeState) -> None:
    state.updated_at = time.time()
    _write_atomic(STATE_PATH, state.to_dict())


def clear_state() -> None:
    if STATE_PATH.exists():
        try:
            STATE_PATH.unlink()
        except OSError:
            pass
