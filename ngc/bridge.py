"""Bridge: connect to one or more Switch 2 controllers over raw L2CAP and feed a
uinput virtual gamepad each, with automatic reconnection. Pure userspace; no
BlueZ GATT, no kernel modules.

Multiple controllers run concurrently (local multiplayer): one worker thread per
configured controller, each owning its own ATT link, gamepad, and rumble.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import protocol as P
from .config import Config, ControllerEntry
from .device import SwitchController
from .dsu import DSUServer
from .gamepad import SwitchGamepad


def _stick_to_dsu(value: float) -> int:
    """Map a calibrated -1.0..1.0 axis to DSU's 0..255 range (128 neutral)."""
    return max(0, min(255, int(round(128 + value * 127))))

logger = logging.getLogger(__name__)

# Most adapters allow only ONE outstanding LE create-connection at a time, so
# the raw L2CAP connect initiation must be serialized across workers. Without
# this, two asleep controllers fail to both wake-connect reliably.
_CONNECT_LOCK = threading.Lock()


def prepare_bluez_global() -> None:
    """Stop background scanning so raw LE connections can be initiated. Global
    actions that should run once before any worker connects."""
    subprocess.run(["pkill", "-f", "decky-bluetooth-wake-control"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["bluetoothctl", "scan", "off"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# Emulator port assignment depends on which pads are currently connected, so we
# re-run the system reorder hook whenever the set of connected controllers
# changes. This keeps Dolphin's GCPad1-4 (and friends) from going stale when a
# controller is woken after launch. Disable with NGC_AUTO_REORDER=0.
_REORDER_SCRIPTS = [
    "~/.local/bin/bazzite-dolphin-apply-gcpad1.sh",
]


def _reorder_enabled() -> bool:
    return os.environ.get("NGC_AUTO_REORDER", "1").lower() not in {"0", "false", "no"}


def run_emulator_reorder() -> None:
    """Best-effort: re-apply emulator player order for the connected pads."""
    if not _reorder_enabled():
        return
    for raw in _REORDER_SCRIPTS:
        path = Path(os.path.expanduser(raw))
        if not path.is_file():
            continue
        try:
            subprocess.run([str(path)], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("emulator reorder applied (%s)", path.name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("reorder hook %s failed: %s", path.name, exc)


def prepare_bluez(mac: str) -> None:
    """Per-controller BlueZ prep: stop scanning and clear any stale record."""
    prepare_bluez_global()
    subprocess.run(["bluetoothctl", "remove", mac],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class _Worker:
    """Owns the full lifecycle of a single controller: connect, virtual gamepad,
    input streaming, rumble, and auto-reconnect."""

    def __init__(self, entry: ControllerEntry, config: Config, stop: threading.Event,
                 dsu: Optional[DSUServer] = None,
                 on_topology_change: Optional[callable] = None):
        self.entry = entry
        self.config = config
        self._stop = stop
        self.dsu = dsu
        self.on_topology_change = on_topology_change
        self.slot = max(0, min(3, entry.player - 1))
        self.gamepad: Optional[SwitchGamepad] = None
        self.controller: Optional[SwitchController] = None
        self._disconnected = threading.Event()

    # -- callbacks ---------------------------------------------------- #

    def _on_input(self, ctrl: SwitchController, report: P.InputReport) -> None:
        (lx, ly), (rx, ry), lt, rt = ctrl.calibrated_input(report)
        if self.gamepad is not None:
            self.gamepad.update(report.buttons, (lx, ly), (rx, ry), lt, rt)
        if self.dsu is not None:
            sticks = (_stick_to_dsu(lx), _stick_to_dsu(ly),
                      _stick_to_dsu(rx), _stick_to_dsu(ry))
            self.dsu.update(self.slot, report, sticks, (lt, rt))

    def _on_disconnect(self) -> None:
        logger.warning("controller %s disconnected", self.entry.mac)
        self._disconnected.set()

    def _on_rumble(self, strong: float, weak: float) -> None:
        ctrl = self.controller
        if ctrl is None or not ctrl.is_connected:
            return
        try:
            ctrl.set_rumble(strong, weak)
        except Exception as exc:  # noqa: BLE001
            logger.debug("rumble failed: %s", exc)

    # -- connection --------------------------------------------------- #

    def _ensure_gamepad(self, ctrl: SwitchController) -> None:
        if self.gamepad is not None:
            return
        name = f"{ctrl.name} (P{self.entry.player})"
        self.gamepad = SwitchGamepad(
            name=name,
            button_map=self.config_button_map(),
            product=ctrl.product_id,
        )
        logger.info("virtual gamepad ready: %s", name)

    def config_button_map(self):
        from .gamepad import DEFAULT_BUTTON_MAP
        from evdev import ecodes as e

        if not self.config.button_map:
            return DEFAULT_BUTTON_MAP
        resolved = {}
        for switch_name, code in self.config.button_map.items():
            resolved[switch_name] = getattr(e, code) if isinstance(code, str) else code
        return resolved

    def _connect_once(self) -> bool:
        mac = self.entry.mac
        adapter = self.config.adapter_mac
        if not adapter:
            raise RuntimeError("adapter_mac not configured")

        ctrl = SwitchController(mac, adapter)
        # Serialize the create-connection across workers: only one initiation may
        # be in flight on the adapter. We hold the lock only for the connect
        # window (a moderate timeout so workers alternate and each gets a chance
        # to catch its controller the moment it advertises), then release it so
        # GATT initialization runs concurrently with other controllers.
        with _CONNECT_LOCK:
            if self._stop.is_set():
                ctrl.close()
                return False
            prepare_bluez(mac)
            if not ctrl.connect(timeout=8):
                ctrl.close()
                return False
        if self._stop.is_set():
            ctrl.close()
            return False

        logger.info("connected to %s (MTU %d)", mac, ctrl.att.mtu)
        ctrl.input_callback = self._on_input
        ctrl.disconnect_callback = self._on_disconnect
        self._disconnected.clear()
        ctrl.initialize(player=self.entry.player)
        self.controller = ctrl
        self._ensure_gamepad(ctrl)
        if self.gamepad is not None and self.config.enable_rumble:
            self.gamepad.rumble_cb = self._on_rumble
        if self.dsu is not None:
            self.dsu.set_slot(self.slot, True, mac=mac, battery_mv=ctrl.battery_mv or 0)
        if self.on_topology_change is not None:
            self.on_topology_change()
        return True

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if self._connect_once():
                    backoff = 1.0
                    while not self._stop.is_set() and not self._disconnected.is_set():
                        self._disconnected.wait(0.5)
                    if self.gamepad is not None:
                        self.gamepad.rumble_cb = None
                    if self.dsu is not None:
                        self.dsu.set_slot(self.slot, False)
                    if self.controller:
                        self.controller.close()
                        self.controller = None
                    if self.on_topology_change is not None:
                        self.on_topology_change()
                else:
                    logger.info("%s not found; press a button to wake (retry %.0fs)",
                                self.entry.mac, backoff)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 1.5, 5.0)
            except Exception as exc:  # noqa: BLE001
                logger.exception("connection error on %s: %s", self.entry.mac, exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 1.5, 5.0)

    def cleanup(self) -> None:
        if self.controller:
            self.controller.close()
        if self.gamepad:
            self.gamepad.close()


class Bridge:
    def __init__(self, config: Config):
        self.config = config
        self._stop = threading.Event()
        self.workers: list[_Worker] = []
        self.dsu: Optional[DSUServer] = None
        self._reorder_timer: Optional[threading.Timer] = None
        self._reorder_lock = threading.Lock()

    def _schedule_reorder(self) -> None:
        """Debounce reorder hooks so multiple near-simultaneous connects (both
        pads waking together) coalesce into a single re-apply."""
        if self._stop.is_set():
            return
        with self._reorder_lock:
            if self._reorder_timer is not None:
                self._reorder_timer.cancel()
            self._reorder_timer = threading.Timer(2.0, run_emulator_reorder)
            self._reorder_timer.daemon = True
            self._reorder_timer.start()

    def run(self) -> None:
        entries = self.config.entries()
        if not entries:
            raise RuntimeError("no controllers configured (run pairing first)")

        prepare_bluez_global()

        # Motion server for emulators (Dolphin/Cemu/Ryujinx). Optional: if the
        # port is taken the bridge still runs (without gyro).
        self.dsu = DSUServer()
        if not self.dsu.start():
            self.dsu = None

        logger.info("starting %d controller worker(s)", len(entries))
        threads: list[threading.Thread] = []
        for i, entry in enumerate(entries):
            worker = _Worker(entry, self.config, self._stop, dsu=self.dsu,
                             on_topology_change=self._schedule_reorder)
            self.workers.append(worker)
            t = threading.Thread(target=worker.run, name=f"ctrl-{entry.player}", daemon=True)
            t.start()
            threads.append(t)
            # Small stagger so the workers' connect attempts interleave cleanly;
            # the shared connect lock handles the actual serialization.
            if i + 1 < len(entries):
                time.sleep(0.3)

        while not self._stop.is_set():
            self._stop.wait(0.5)

        with self._reorder_lock:
            if self._reorder_timer is not None:
                self._reorder_timer.cancel()
        for worker in self.workers:
            worker.cleanup()
        if self.dsu is not None:
            self.dsu.stop()

    def stop(self) -> None:
        self._stop.set()
