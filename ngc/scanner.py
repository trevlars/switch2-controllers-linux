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
    reconnect = P.reconnect_mac_from_advertisement(adv)
    is_pairing = reconnect == 0
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


async def wait_for_addresses(
    addresses: set[str],
    timeout: float = 12.0,
    stop: Optional[asyncio.Event] = None,
    host_mac: Optional[int] = None,
    bonded_wake_only: bool = False,
) -> Optional[str]:
    """Return the first address from ``addresses`` seen advertising.

    When ``bonded_wake_only`` is set, only adverts whose embedded reconnect MAC
    matches ``host_mac`` count (button-press wake). Pairing-mode adverts
    (reconnect MAC == 0) are ignored.
    """
    if not addresses:
        return None
    want = {a.upper() for a in addresses}
    found: dict[str, str] = {}
    done = asyncio.Event()

    def _cb(device: BLEDevice, adv) -> None:
        addr = device.address.upper()
        if addr not in want or addr in found:
            return
        reconnect = P.reconnect_mac_from_advertisement(adv)
        if bonded_wake_only and host_mac is not None:
            if reconnect == 0:
                return  # pairing mode (Sync held) — not a button wake
            if reconnect is not None and reconnect != host_mac:
                return  # bonded to a different host
            # reconnect == host_mac, or no manufacturer block: accept
        elif reconnect is not None and host_mac is not None and reconnect not in (0, host_mac):
            return
        found["addr"] = addr
        done.set()

    scanner = BleakScanner(detection_callback=_cb)
    await scanner.start()
    try:
        waiters = [asyncio.create_task(done.wait())]
        if stop is not None:
            waiters.append(asyncio.create_task(stop.wait()))
        done_task, pending = await asyncio.wait(
            waiters,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if done.is_set():
            return found.get("addr")
        return None
    finally:
        await scanner.stop()
