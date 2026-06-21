#!/usr/bin/env python3
"""Watch BLE advertisements from configured Switch 2 controllers.

Run on the Bazzite box (stop nso-gc.service first for a clean scan):
    .venv312/bin/python tools/adv_watch.py 30

Press a normal button (not Sync) on a paired pad while this runs.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakScanner

from ngc import protocol as P
from ngc.config import Config


async def main() -> int:
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    cfg = Config.load()
    want = {e.mac.upper() for e in cfg.entries()}
    host = P.mac_to_int(cfg.adapter_mac) if cfg.adapter_mac else None
    print(f"Watching {want or 'ALL'} for {timeout:.0f}s (host reconnect id={host:#x})")
    print("Press a button on the controller now (not Sync)...")

    seen = 0

    def _cb(device, adv) -> None:
        nonlocal seen
        addr = device.address.upper()
        if want and addr not in want:
            return
        reconnect = P.reconnect_mac_from_advertisement(adv)
        manu = adv.manufacturer_data.get(P.NINTENDO_COMPANY_ID)
        manu_hex = manu.hex() if manu else "(none)"
        mode = "?"
        if reconnect == 0:
            mode = "PAIRING"
        elif reconnect is not None and host is not None and reconnect == host:
            mode = "BONDED-WAKE"
        elif reconnect is not None:
            mode = f"OTHER-HOST({reconnect:#x})"
        seen += 1
        print(f"  [{seen}] {addr} rssi={adv.rssi} mode={mode} manu={manu_hex}")

    scanner = BleakScanner(detection_callback=_cb)
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
    finally:
        await scanner.stop()
    print(f"Done — {seen} advert(s) from target pad(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
