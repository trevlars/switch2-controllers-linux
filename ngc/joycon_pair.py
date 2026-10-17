"""Joy-Con 2 single/paired output modes kept separate from the core bridge."""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

from evdev import ecodes as e

from . import protocol as P
from .gamepad import SwitchGamepad
from .motion_evdev import MotionEvdev

logger = logging.getLogger(__name__)

COMBINED_PRODUCT_ID = 0x2068
_JOYCON_PIDS = {P.JOYCON2_LEFT_PID, P.JOYCON2_RIGHT_PID}

_LEFT_NAMES = ("MINUS", "L_STK", "CAPTURE", "DOWN", "UP", "RIGHT", "LEFT", "L", "ZL")
_RIGHT_NAMES = ("Y", "X", "B", "A", "PLUS", "R_STK", "HOME", "C", "R", "ZR")
_LEFT_MASK = sum(P.SWITCH_BUTTONS[name] for name in _LEFT_NAMES)
_RIGHT_MASK = sum(P.SWITCH_BUTTONS[name] for name in _RIGHT_NAMES)

JOYCON_LEFT_MINI_BUTTON_MAP = {
    "RIGHT": e.BTN_NORTH,
    "LEFT": e.BTN_SOUTH,
    "UP": e.BTN_WEST,
    "DOWN": e.BTN_EAST,
    "SL_L": e.BTN_TL,
    "SR_L": e.BTN_TR,
    "MINUS": e.BTN_START,
    "CAPTURE": e.BTN_MODE,
    "L_STK": e.BTN_THUMBL,
    "L": e.BTN_TRIGGER_HAPPY2,
    "ZL": e.BTN_TRIGGER_HAPPY4,
}

JOYCON_RIGHT_MINI_BUTTON_MAP = {
    "B": e.BTN_NORTH,
    "A": e.BTN_EAST,
    "X": e.BTN_SOUTH,
    "Y": e.BTN_WEST,
    "SL_R": e.BTN_TL,
    "SR_R": e.BTN_TR,
    "PLUS": e.BTN_START,
    "HOME": e.BTN_MODE,
    "R_STK": e.BTN_THUMBL,
    "C": e.BTN_TRIGGER_HAPPY1,
    "R": e.BTN_TRIGGER_HAPPY2,
    "ZR": e.BTN_TRIGGER_HAPPY4,
}


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes", "on"}


def _is_attached(raw: bytes) -> bool:
    """Return the measured powered charging-grip attachment signature.

    The passive grip has no electrical connection and therefore cannot be
    detected. Reports from the powered GL/GR charging grip used byte 7 = 0x20,
    byte 8 in the 0x2b..0x2e range, and byte 9 = 0x02.
    """
    return (
        len(raw) > 9
        and raw[7] == 0x20
        and 0x2B <= raw[8] <= 0x2E
        and raw[9] == 0x02
    )


class JoyConPair:
    """Thread-safe latest-state merger for one left and one right Joy-Con 2."""

    def __init__(self, rumble_cb: Optional[Callable[[float, float], None]] = None) -> None:
        self._lock = threading.RLock()
        self.rumble_cb = rumble_cb
        self.gamepad: SwitchGamepad | None = None
        self._left_buttons = 0
        self._right_buttons = 0
        self._left_stick = (0.0, 0.0)
        self._right_stick = (0.0, 0.0)
        self._left_trigger = 0
        self._right_trigger = 0

    def ensure(self) -> None:
        with self._lock:
            if self.gamepad is not None:
                return
            button_map = {
                "A": e.BTN_EAST,
                "B": e.BTN_SOUTH,
                "Y": e.BTN_NORTH,
                "X": e.BTN_WEST,
                "MINUS": e.BTN_SELECT,
                "HOME": e.BTN_START,
                "PLUS": e.BTN_MODE,
                "L_STK": e.BTN_THUMBL,
                "R_STK": e.BTN_THUMBR,
                "L": e.BTN_TRIGGER_HAPPY1,
                "R": e.BTN_TRIGGER_HAPPY2,
                "CAPTURE": e.BTN_TRIGGER_HAPPY3,
                "C": e.BTN_TRIGGER_HAPPY4,
            }
            self.gamepad = SwitchGamepad(
                name="Nintendo Switch Joy-Con (L/R)",
                button_map=button_map,
                product=COMBINED_PRODUCT_ID,
            )
            self.gamepad.rumble_cb = self.rumble_cb
            logger.info("combined Joy-Con virtual gamepad ready: Nintendo Switch Joy-Con (L/R)")

    def update(
        self,
        product_id: int,
        buttons: int,
        left_stick: tuple[float, float],
        right_stick: tuple[float, float],
        left_trigger: int,
        right_trigger: int,
    ) -> None:
        with self._lock:
            self.ensure()
            if product_id == P.JOYCON2_LEFT_PID:
                self._left_buttons = buttons & _LEFT_MASK
                self._left_stick = left_stick
                self._left_trigger = left_trigger
            elif product_id == P.JOYCON2_RIGHT_PID:
                self._right_buttons = buttons & _RIGHT_MASK
                self._right_stick = right_stick
                self._right_trigger = right_trigger
            else:
                return
            assert self.gamepad is not None
            self.gamepad.update(
                self._left_buttons | self._right_buttons,
                self._left_stick,
                self._right_stick,
                self._left_trigger,
                self._right_trigger,
            )

    def disconnect(self, product_id: int) -> None:
        with self._lock:
            if product_id == P.JOYCON2_LEFT_PID:
                self._left_buttons = 0
                self._left_stick = (0.0, 0.0)
                self._left_trigger = 0
            elif product_id == P.JOYCON2_RIGHT_PID:
                self._right_buttons = 0
                self._right_stick = (0.0, 0.0)
                self._right_trigger = 0
            else:
                return
            if self.gamepad is not None:
                self.gamepad.update(
                    self._left_buttons | self._right_buttons,
                    self._left_stick,
                    self._right_stick,
                    self._left_trigger,
                    self._right_trigger,
                )

    def close(self) -> None:
        with self._lock:
            try:
                if self.gamepad is not None:
                    self.gamepad.close()
            finally:
                self.gamepad = None
                self._left_buttons = 0
                self._right_buttons = 0
                self._left_stick = (0.0, 0.0)
                self._right_stick = (0.0, 0.0)
                self._left_trigger = 0
                self._right_trigger = 0


class JoyConModeManager:
    """Optional Joy-Con behavior behind narrow hooks in the upstream bridge."""

    def __init__(self, bridge) -> None:
        self.bridge = bridge
        merge = _env_flag("NGC_MERGE_JOYCONS")
        self.auto = _env_flag("NGC_AUTO_JOYCON_MODE")
        self.pair = JoyConPair(self._rumble_pair) if merge or self.auto else None
        self.output_mode = "pair" if merge and not self.auto else "individual"
        self.require_pair = merge or self.auto or _env_flag("NGC_REQUIRE_JOYCON_PAIR")
        self.lock = threading.RLock()
        self._candidate: dict[int, bool] = {}
        self._candidate_count: dict[int, int] = {}
        self._attached: dict[int, bool] = {}
        self._partial_pair_since: Optional[float] = None

    @staticmethod
    def is_joycon(product_id: int) -> bool:
        return product_id in _JOYCON_PIDS

    def activation_context(self):
        return self.lock

    def filter_pending(self, hub, pending: list[str]) -> list[str]:
        if not self.require_pair:
            return pending
        pair_macs = {
            mac for mac, worker in hub.workers_by_mac.items()
            if "Joy-Con 2" in (worker.entry.name or "")
        }
        if len(pair_macs) == 2 and not pair_macs.issubset(set(pending)):
            return [mac for mac in pending if mac not in pair_macs]
        return pending

    def button_map(self, product_id: int):
        if product_id == P.JOYCON2_LEFT_PID:
            return JOYCON_LEFT_MINI_BUTTON_MAP
        if product_id == P.JOYCON2_RIGHT_PID:
            return JOYCON_RIGHT_MINI_BUTTON_MAP
        return None

    def pair_active(self, product_id: int) -> bool:
        return self.pair is not None and self.output_mode == "pair" and self.is_joycon(product_id)

    def route_input(self, worker, ctrl, report: P.InputReport, calibrated) -> bool:
        if not self.is_joycon(ctrl.product_id):
            return False
        with self.lock:
            if self.auto:
                self._observe(ctrl.product_id, _is_attached(report.raw))
            lx, ly, rx, ry, lt, rt = calibrated
            if self.pair_active(ctrl.product_id):
                if ctrl.product_id == P.JOYCON2_RIGHT_PID and ctrl.left_calib is not None:
                    rx, ry = ctrl.left_calib.apply(report.right_stick_raw)
                assert self.pair is not None
                self.pair.update(ctrl.product_id, report.buttons, (lx, ly), (rx, ry), lt, rt)
            elif worker.gamepad is not None:
                if ctrl.product_id == P.JOYCON2_LEFT_PID:
                    worker.gamepad.update(report.buttons, (-ly, lx), (0.0, 0.0), 0, 0, emit_dpad=False)
                else:
                    if ctrl.left_calib is not None:
                        rx, ry = ctrl.left_calib.apply(report.right_stick_raw)
                    worker.gamepad.update(report.buttons, (ry, -rx), (0.0, 0.0), 0, 0, emit_dpad=False)
            return True

    def ensure_pair_output(self, worker, ctrl) -> bool:
        if not self.pair_active(ctrl.product_id):
            return False
        assert self.pair is not None
        self.pair.ensure()
        if worker.motion is None:
            name = f"{ctrl.name} (P{worker.entry.player})"
            worker.motion = MotionEvdev(name, worker.entry.mac, product=ctrl.product_id)
        logger.info("Joy-Con half ready for combined gamepad: %s", ctrl.name)
        return True

    def bind_individual_rumble(self, worker) -> None:
        if worker.gamepad is not None and worker.config.enable_rumble:
            worker.gamepad.rumble_cb = worker._on_rumble

    def disconnect(self, worker) -> None:
        if self.pair is not None and worker.controller is not None:
            self.pair.disconnect(worker.controller.product_id)

    @staticmethod
    def release_individual(gamepad: SwitchGamepad) -> None:
        try:
            gamepad.release_all()
        except OSError as exc:
            logger.debug("gamepad already closed during teardown: %s", exc)

    def _apply_mode_leds(self) -> None:
        if not self.auto:
            return
        for worker in self.bridge.workers:
            ctrl = worker.controller
            if ctrl is None or not ctrl.is_connected or not self.is_joycon(ctrl.product_id):
                continue
            player = 1 if self.output_mode == "pair" else worker.effective_player()
            if worker._led_player == player:
                continue
            try:
                ctrl.set_player_leds(player)
                worker._led_player = player
                logger.info("automatic Joy-Con LEDs %s -> P%d", worker.entry.mac, player)
            except Exception as exc:  # noqa: BLE001
                logger.debug("automatic Joy-Con LED update failed for %s: %s", worker.entry.mac, exc)

    def tick(self) -> None:
        self._apply_mode_leds()
        if not self.require_pair:
            return
        workers = [w for w in self.bridge.workers if "Joy-Con 2" in (w.entry.name or "")]
        connected = [w for w in workers if w.is_connected()]
        if len(workers) == 2 and len(connected) == 1:
            now = time.monotonic()
            if self._partial_pair_since is None:
                self._partial_pair_since = now
            elif now - self._partial_pair_since >= 5.0:
                lone = connected[0]
                logger.warning("only one Joy-Con half remained connected; resetting pair scan")
                if lone.controller is not None:
                    lone.controller.close()
                lone._disconnected.set()
                self._partial_pair_since = None
        else:
            self._partial_pair_since = None

    def close(self) -> None:
        with self.lock:
            if self.pair is not None:
                self.pair.close()

    def _observe(self, product_id: int, attached: bool) -> None:
        with self.lock:
            if self._candidate.get(product_id) == attached:
                count = self._candidate_count.get(product_id, 0) + 1
            else:
                self._candidate[product_id] = attached
                count = 1
            self._candidate_count[product_id] = count
            if count < 3 or self._attached.get(product_id) == attached:
                return
            self._attached[product_id] = attached
            desired = "pair" if all(
                self._attached.get(pid) is True for pid in _JOYCON_PIDS
            ) else "individual"
            if desired != self.output_mode:
                self._switch(desired)

    def _switch(self, desired: str) -> None:
        with self.lock:
            self.output_mode = desired
            workers = [
                w for w in self.bridge.workers
                if w.controller is not None and self.is_joycon(w.controller.product_id)
            ]
            if desired == "pair":
                for worker in workers:
                    if worker.gamepad is not None:
                        worker.gamepad.rumble_cb = None
                        worker.gamepad.close()
                        worker.gamepad = None
                        worker._gamepad_product = None
                assert self.pair is not None
                self.pair.ensure()
            else:
                assert self.pair is not None
                self.pair.close()
                for worker in workers:
                    worker._ensure_gamepad(worker.controller)
                    self.bind_individual_rumble(worker)
            self._apply_mode_leds()
            logger.info("automatic Joy-Con output mode: %s", desired)
            self.bridge._publish_state()

    def _rumble_pair(self, strong: float, weak: float) -> None:
        for worker in self.bridge.workers:
            ctrl = worker.controller
            if (worker.config.enable_rumble and ctrl is not None
                    and self.is_joycon(ctrl.product_id)):
                worker._on_rumble(strong, weak)
