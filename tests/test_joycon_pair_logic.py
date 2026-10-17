"""Dependency-free regression tests for the isolated Joy-Con mode manager."""
from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch


class _Codes:
    BTN_NORTH = 307
    BTN_SOUTH = 304
    BTN_EAST = 305
    BTN_WEST = 308
    BTN_TL = 310
    BTN_TR = 311
    BTN_START = 315
    BTN_MODE = 316
    BTN_THUMBL = 317
    BTN_THUMBR = 318
    BTN_SELECT = 314
    BTN_TRIGGER_HAPPY1 = 704
    BTN_TRIGGER_HAPPY2 = 705
    BTN_TRIGGER_HAPPY3 = 706
    BTN_TRIGGER_HAPPY4 = 707


class _Pad:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.rumble_cb = None
        self.updates = []
        self.closed = False
        self.released = False

    def update(self, *args, **kwargs):
        self.updates.append(args)

    def close(self):
        self.closed = True

    def release_all(self):
        self.released = True


class _Motion:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


_evdev = types.ModuleType("evdev")
_evdev.ecodes = _Codes
sys.modules.setdefault("evdev", _evdev)
_gamepad = types.ModuleType("ngc.gamepad")
_gamepad.SwitchGamepad = _Pad
sys.modules["ngc.gamepad"] = _gamepad
_motion = types.ModuleType("ngc.motion_evdev")
_motion.MotionEvdev = _Motion
sys.modules["ngc.motion_evdev"] = _motion

from ngc import protocol as P  # noqa: E402
modes = importlib.import_module("ngc.joycon_pair")


class _Entry:
    def __init__(self, mac, name, player):
        self.mac = mac
        self.name = name
        self.player = player


class _Config:
    enable_rumble = True


class _Calibration:
    def apply(self, _raw):
        return (0.25, -0.5)


class _Controller:
    def __init__(self, product_id, name):
        self.product_id = product_id
        self.name = name
        self.left_calib = _Calibration()
        self.is_connected = True
        self.leds = []

    def set_player_leds(self, player):
        self.leds.append(player)


class _Worker:
    def __init__(self, product_id, player):
        self.entry = _Entry(f"00:00:00:00:00:0{player}", f"Joy-Con 2 P{player}", player)
        self.config = _Config()
        self.controller = _Controller(product_id, self.entry.name)
        self.gamepad = _Pad()
        self.motion = None
        self._gamepad_product = product_id
        self._led_player = player
        self._disconnected = types.SimpleNamespace(set=lambda: None)
        self.rumbles = []

    def is_connected(self):
        return self.controller.is_connected

    def effective_player(self):
        return self.entry.player

    def _on_rumble(self, strong, weak):
        self.rumbles.append((strong, weak))

    def _ensure_gamepad(self, _ctrl):
        self.gamepad = _Pad()


class _Bridge:
    def __init__(self):
        self.workers = []
        self.led_updates = 0
        self.state_updates = 0

    def _apply_led_overrides(self):
        self.led_updates += 1

    def _publish_state(self):
        self.state_updates += 1


class _Report:
    def __init__(self, attached=True):
        raw = bytearray(63)
        raw[7] = 0x20 if attached else 0xE0
        raw[8] = 0x2E if attached else 0xFF
        raw[9] = 0x02 if attached else 0x0F
        self.raw = bytes(raw)
        self.buttons = 0
        self.right_stick_raw = (0, 0)


class JoyConModeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "NGC_AUTO_JOYCON_MODE": "1",
            "NGC_MERGE_JOYCONS": "0",
            "NGC_REQUIRE_JOYCON_PAIR": "1",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.bridge = _Bridge()
        self.left = _Worker(P.JOYCON2_LEFT_PID, 1)
        self.right = _Worker(P.JOYCON2_RIGHT_PID, 2)
        self.bridge.workers = [self.left, self.right]
        self.manager = modes.JoyConModeManager(self.bridge)

    def test_grip_signature(self):
        self.assertTrue(modes._is_attached(_Report(True).raw))
        self.assertFalse(modes._is_attached(_Report(False).raw))
        outside_range = bytearray(_Report(True).raw)
        outside_range[8] = 0x2A
        self.assertFalse(modes._is_attached(bytes(outside_range)))

    def test_single_pad_rotation_and_maps(self):
        handled = self.manager.route_input(
            self.left, self.left.controller, _Report(False), (0.5, -0.25, 0.0, 0.0, 0, 0)
        )
        self.assertTrue(handled)
        self.assertEqual(self.left.gamepad.updates[-1][1], (0.25, 0.5))
        self.manager.route_input(
            self.right, self.right.controller, _Report(False), (0.0, 0.0, 0.0, 0.0, 0, 0)
        )
        self.assertEqual(self.right.gamepad.updates[-1][1], (-0.5, -0.25))
        self.assertEqual(
            self.manager.button_map(P.JOYCON2_LEFT_PID)["RIGHT"], _Codes.BTN_NORTH
        )
        self.assertEqual(
            self.manager.button_map(P.JOYCON2_RIGHT_PID)["B"], _Codes.BTN_NORTH
        )

    def test_debounced_live_pair_and_unpair_with_rumble(self):
        for _ in range(3):
            self.manager._observe(P.JOYCON2_LEFT_PID, True)
        self.assertEqual(self.manager.output_mode, "individual")
        for _ in range(3):
            self.manager._observe(P.JOYCON2_RIGHT_PID, True)
        self.assertEqual(self.manager.output_mode, "pair")
        self.assertTrue(self.left.gamepad is None and self.right.gamepad is None)
        self.assertEqual(self.left._led_player, 1)
        self.assertEqual(self.right._led_player, 1)
        self.manager._rumble_pair(0.75, 0.25)
        self.assertEqual(self.left.rumbles, [(0.75, 0.25)])
        self.assertEqual(self.right.rumbles, [(0.75, 0.25)])
        for _ in range(3):
            self.manager._observe(P.JOYCON2_LEFT_PID, False)
        self.assertEqual(self.manager.output_mode, "individual")
        self.assertEqual(self.left._led_player, 1)
        self.assertEqual(self.right._led_player, 2)
        self.assertIsNotNone(self.left.gamepad)
        self.assertIsNotNone(self.right.gamepad)
        self.left.rumbles.clear()
        self.right.rumbles.clear()
        self.left.gamepad.rumble_cb(0.5, 0.1)
        self.right.gamepad.rumble_cb(0.25, 0.05)
        self.assertEqual(self.left.rumbles, [(0.5, 0.1)])
        self.assertEqual(self.right.rumbles, [(0.25, 0.05)])

    def test_pair_wake_filter(self):
        hub = types.SimpleNamespace(workers_by_mac={
            "L": self.left,
            "R": self.right,
        })
        self.assertEqual(self.manager.filter_pending(hub, ["L"]), [])
        self.assertEqual(set(self.manager.filter_pending(hub, ["L", "R"])), {"L", "R"})


if __name__ == "__main__":
    unittest.main()
