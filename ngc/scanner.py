"""Discovery of advertising Switch 2 controllers over BLE."""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import Optional

from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from . import protocol as P

logger = logging.getLogger(__name__)

_SUPPORTED_PIDS = {
    P.JOYCON2_RIGHT_PID,
    P.JOYCON2_LEFT_PID,
    P.PRO_CONTROLLER2_PID,
    P.NSO_GAMECUBE_PID,
}


@dataclass
class Discovered:
    device: BLEDevice
    vendor_id: int
    product_id: int

    @property
    def name(self) -> str:
        return P.CONTROLLER_NAMES.get(self.product_id, "Switch 2 Controller")


def _parse_manufacturer(adv) -> Optional[tuple[int, int, bool]]:
    """Return (vid, pid, is_advertising_to_pair) from manufacturer data, if it
    looks like a Switch 2 controller."""
    manu = adv.manufacturer_data.get(P.NINTENDO_COMPANY_ID)
    if not manu or len(manu) < 13:
        return None
    vid = struct.unpack_from("<H", manu, 3)[0]
    pid = struct.unpack_from("<H", manu, 5)[0]
    # manu[12] == 0 indicates the controller is in pairing/advertising mode.
    is_pairing = manu[12] == 0
    return vid, pid, is_pairing


async def scan(
    timeout: float = 10.0,
    only_pids: Optional[set[int]] = None,
    require_pairing: bool = False,
) -> list[Discovered]:
    """Scan for advertising Switch 2 controllers for up to ``timeout`` seconds."""
    only_pids = only_pids or _SUPPORTED_PIDS
    found: dict[str, Discovered] = {}

    def _cb(device: BLEDevice, adv) -> None:
        parsed = _parse_manufacturer(adv)
        if not parsed:
            return
        vid, pid, is_pairing = parsed
        if vid != P.NINTENDO_VENDOR_ID or pid not in only_pids:
            return
        if require_pairing and not is_pairing:
            return
        if device.address not in found:
            found[device.address] = Discovered(device, vid, pid)
            logger.info("found %s %04X:%04X (%s)", device.address, vid, pid, P.CONTROLLER_NAMES.get(pid))

    scanner = BleakScanner(detection_callback=_cb)
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
    finally:
        await scanner.stop()
    return list(found.values())


async def find_first(
    timeout: float = 15.0,
    only_pids: Optional[set[int]] = None,
    require_pairing: bool = False,
) -> Optional[Discovered]:
    """Return the first matching controller as soon as it is seen."""
    only_pids = only_pids or _SUPPORTED_PIDS
    result: dict[str, Discovered] = {}
    stop = asyncio.Event()

    def _cb(device: BLEDevice, adv) -> None:
        parsed = _parse_manufacturer(adv)
        if not parsed:
            return
        vid, pid, is_pairing = parsed
        if vid != P.NINTENDO_VENDOR_ID or pid not in only_pids:
            return
        if require_pairing and not is_pairing:
            return
        result["found"] = Discovered(device, vid, pid)
        stop.set()

    scanner = BleakScanner(detection_callback=_cb)
    await scanner.start()
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        await scanner.stop()
    return result.get("found")
