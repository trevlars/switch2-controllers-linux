"""Entry point: pair (discover + save) and run (bridge) commands.

    python -m ngc pair     # scan for a controller in pairing mode, save its address
    python -m ngc run      # run the bridge (virtual gamepad + auto-reconnect)
    python -m ngc          # run; if unconfigured, pair first
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import protocol as P
from .config import Config


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _bond_sync(mac: str, player: int, adapter: str) -> bool:
    """Connect over raw ATT and run the bonding handshake so the controller
    reconnects automatically afterward."""
    from .bridge import prepare_bluez
    from .device import SwitchController

    prepare_bluez(mac)
    ctrl = SwitchController(mac, adapter)
    connected = False
    for _ in range(15):
        if ctrl.connect(timeout=6):
            connected = True
            break
    if not connected:
        print("Bonding: could not establish raw link.")
        return False
    try:
        ctrl._resolve_handles()
        ctrl.enable_commands()
        ctrl.info = ctrl.read_controller_info()
        ctrl.set_player_leds(player)
        ctrl.bond()
        print(f"Bonded {ctrl.info.name} ({mac}) as player {player} to adapter {adapter}.")
        return True
    finally:
        ctrl.close()


async def _pair(cfg: Config, timeout: float) -> bool:
    from .scanner import find_first

    print(
        "Put a Switch 2 controller (GameCube / Pro Controller 2 / Joy-Con 2) "
        "in pairing mode (hold sync until LEDs sweep)..."
    )
    found = await find_first(timeout=timeout, require_pairing=True)
    if not found:
        print("No controller found in pairing mode.")
        return False
    if not cfg.adapter_mac:
        from .config import detect_adapter

        cfg.adapter_mac = detect_adapter()
    entry = cfg.add_controller(found.device.address, name=found.name)
    cfg.save()
    print(f"Discovered {found.name} at {entry.mac} (player {entry.player}); bonding...")
    return await asyncio.get_event_loop().run_in_executor(
        None, _bond_sync, entry.mac, entry.player, cfg.adapter_mac
    )


def _list(cfg: Config) -> int:
    entries = cfg.entries()
    if not entries:
        print("No controllers configured. Run: python -m ngc pair")
        return 0
    print(f"Adapter: {cfg.adapter_mac}")
    for e in entries:
        label = e.name or "Switch 2 Controller"
        print(f"  P{e.player}  {e.mac}  {label}")
    return 0


def _run(cfg: Config) -> int:
    from .bridge import Bridge

    bridge = Bridge(cfg)

    def _sig(_signum, _frame):
        bridge.stop()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    bridge.run()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ngc", description="Switch 2 controller bridge (GameCube / Pro Controller 2 / Joy-Con 2)")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "pair", "list"])
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0, help="pairing scan timeout")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    cfg = Config.load()

    if args.command == "pair":
        return 0 if asyncio.run(_pair(cfg, args.timeout)) else 1

    if args.command == "list":
        return _list(cfg)

    if not cfg.entries():
        print("No controller configured; scanning for one in pairing mode first.")
        if not asyncio.run(_pair(cfg, args.timeout)):
            return 1
    return _run(cfg)


if __name__ == "__main__":
    sys.exit(main())
