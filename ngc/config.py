"""Persistent configuration for the bridge.

Supports multiple bonded controllers (for local multiplayer): each entry has a
MAC, an assigned player slot, and a remembered name. Stored as JSON under XDG
config. Legacy single-controller configs are migrated automatically on load.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "nso-gc"
CONFIG_PATH = CONFIG_DIR / "config.json"


def detect_adapter() -> Optional[str]:
    """Return the first Bluetooth adapter's address from sysfs (no root needed)."""
    base = Path("/sys/class/bluetooth")
    if not base.exists():
        return None
    for hci in sorted(base.iterdir()):
        addr = hci / "address"
        if addr.exists():
            try:
                return addr.read_text().strip().upper()
            except OSError:
                continue
    return None


@dataclass
class ControllerEntry:
    mac: str
    player: int = 1
    name: str = ""


@dataclass
class Config:
    controllers: list = field(default_factory=list)  # list of {mac, player, name}
    adapter_mac: Optional[str] = None
    button_map: dict = field(default_factory=dict)
    # Rumble: GameCube uses safe presets; Pro/Joy-Con use the real HD motor.
    enable_rumble: bool = True
    # Legacy single-controller fields (migrated into `controllers` on load).
    controller_mac: Optional[str] = None
    player: int = 1

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                cfg = cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})
            except Exception:
                cfg = cls()
        else:
            cfg = cls()
        cfg._migrate()
        if not cfg.adapter_mac:
            cfg.adapter_mac = detect_adapter()
        return cfg

    def _migrate(self) -> None:
        """Fold a legacy single controller into the controllers list."""
        if not self.controllers and self.controller_mac:
            self.controllers = [
                {"mac": self.controller_mac, "player": self.player, "name": ""}
            ]
            self.controller_mac = None

    def entries(self) -> list[ControllerEntry]:
        return [
            ControllerEntry(
                mac=c["mac"],
                player=c.get("player", i + 1),
                name=c.get("name", ""),
            )
            for i, c in enumerate(self.controllers)
        ]

    def add_controller(self, mac: str, name: str = "") -> ControllerEntry:
        """Add (or update) a controller, assigning the next free player slot."""
        for c in self.controllers:
            if c["mac"].upper() == mac.upper():
                if name:
                    c["name"] = name
                return ControllerEntry(c["mac"], c.get("player", 1), c.get("name", ""))
        used = {c.get("player", 0) for c in self.controllers}
        player = next(p for p in range(1, 9) if p not in used)
        entry = {"mac": mac, "player": player, "name": name}
        self.controllers.append(entry)
        return ControllerEntry(mac, player, name)

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Drop legacy fields from the serialized form once migrated.
        data = asdict(self)
        if self.controllers:
            data.pop("controller_mac", None)
        self.save_path(data)

    def save_path(self, data: dict) -> None:
        CONFIG_PATH.write_text(json.dumps(data, indent=2))
