"""Regression coverage for output races, sideways buttons and rumble policy."""
import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch

import test_joycon_pair_logic as fixtures
from test_joycon_pair_logic import _Report, _Codes, P, modes


class OutputRegressionTests(unittest.TestCase):
    setUp = fixtures.JoyConModeTests.setUp

    def test_pair_reentry_starts_other_half_neutral(self):
        pair = modes.JoyConPair()
        pair.update(
            P.JOYCON2_LEFT_PID,
            P.SWITCH_BUTTONS['UP'],
            (0.75, -0.5),
            (0, 0),
            1,
            0,
        )
        first_pad = pair.gamepad

        pair.close()
        pair.update(
            P.JOYCON2_RIGHT_PID,
            0,
            (0, 0),
            (0.25, -0.25),
            0,
            1,
        )

        self.assertTrue(first_pad.closed)
        self.assertEqual(
            pair.gamepad.updates[-1],
            (0, (0.0, 0.0), (0.25, -0.25), 0, 1),
        )

    def test_input_and_mode_switch_are_atomic(self):
        self.manager._switch('pair')
        entered = threading.Event()
        resume = threading.Event()
        errors = []
        original = self.manager.pair_active

        def paused(pid):
            result = original(pid)
            entered.set()
            if not resume.wait(3):
                raise TimeoutError('test did not resume input')
            return result

        def route():
            try:
                self.manager.route_input(self.right, self.right.controller,
                                         _Report(), (0, 0, 0, 0, 0, 0))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(self.manager, 'pair_active', paused):
            thread = threading.Thread(target=route)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                acquired = self.manager.lock.acquire(blocking=False)
                if acquired:
                    self.manager.lock.release()
                self.assertFalse(acquired, 'routing must hold the topology lock')
            finally:
                resume.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.manager._switch('individual')
        self.assertIsNone(self.manager.pair.gamepad)
        self.assertTrue(all(w.gamepad is not None for w in self.bridge.workers))

    def test_pair_rumble_respects_each_worker_setting(self):
        self.left.config.enable_rumble = False
        self.right.config.enable_rumble = False
        self.manager._switch('pair')
        self.manager.pair.gamepad.rumble_cb(1, 0.5)
        self.assertEqual(self.left.rumbles, [])
        self.assertEqual(self.right.rumbles, [])
        self.right.config.enable_rumble = True
        self.manager.pair.gamepad.rumble_cb(0.5, 0.25)
        self.assertEqual(self.left.rumbles, [])
        self.assertEqual(self.right.rumbles, [(0.5, 0.25)])

    def test_real_gamepad_sideways_buttons_without_hat(self):
        # Load production update logic; replace only the kernel-facing device.
        ev = sys.modules['evdev']
        codes = dict(BTN_TL2=312, BTN_TR2=313, BTN_Z=309, BTN_C=306,
                     ABS_HAT0X=16, ABS_HAT0Y=17, ABS_X=0, ABS_Y=1,
                     ABS_RX=3, ABS_RY=4, ABS_Z=2, ABS_RZ=5)
        with patch.multiple(ev, UInput=object, AbsInfo=object, create=True), \
             patch.multiple(_Codes, **codes, create=True), \
             patch.object(sys.modules['ngc.motion_evdev'], 'phys_for_mac',
                          lambda mac: mac, create=True):
            spec = importlib.util.spec_from_file_location(
                'ngc.real_gamepad_test', Path(modes.__file__).with_name('gamepad.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            pad = module.SwitchGamepad.__new__(module.SwitchGamepad)
            pad.button_map = modes.JOYCON_LEFT_MINI_BUTTON_MAP
            keys, axes = {}, {}
            pad._emit_key = lambda k, v: keys.__setitem__(k, v) or True
            pad._emit_abs = lambda k, v: axes.__setitem__(k, v) or True
            pad.ui = types.SimpleNamespace(syn=lambda: None)
            self.left.gamepad = pad
            for name, code in [('RIGHT', _Codes.BTN_NORTH),
                               ('LEFT', _Codes.BTN_SOUTH),
                               ('UP', _Codes.BTN_WEST),
                               ('DOWN', _Codes.BTN_EAST)]:
                report = _Report(False)
                report.buttons = P.SWITCH_BUTTONS[name]
                self.manager.route_input(self.left, self.left.controller, report,
                                         (0, 0, 0, 0, 0, 0))
                self.assertEqual(keys[code], 1)
                self.assertEqual((axes[16], axes[17]), (0, 0))
                report.buttons = 0
                self.manager.route_input(self.left, self.left.controller, report,
                                         (0, 0, 0, 0, 0, 0))
                self.assertEqual(keys[code], 0)
            # Default output (paired/other controllers) retains its D-pad.
            pad.update(P.SWITCH_BUTTONS['RIGHT'], (0, 0), (0, 0), 0, 0)
            self.assertEqual(axes[16], 1)
