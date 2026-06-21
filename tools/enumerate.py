#!/usr/bin/env python3
"""Connect and dump the full GATT table so we can learn this controller's real
UUIDs. Re-scans before each connect attempt (the controller connects reliably
only from a freshly-discovered device object)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient
from ngc import protocol as P
from ngc.scanner import find_first


async def dump(client: BleakClient) -> None:
    collection = client.services
    services = list(getattr(collection, "services", {}).values()) or list(collection)
    print(f"\ndiscovered {len(services)} services\n", flush=True)
    for service in services:
        print(f"[service] {service.uuid}  (handle {service.handle:#06x})")
        for ch in service.characteristics:
            props = ",".join(ch.properties)
            print(f"    [char] {ch.uuid}  handle={ch.handle:#06x}  props=[{props}]")
            for d in ch.descriptors:
                print(f"        [desc] {d.uuid}  handle={d.handle:#06x}")


async def main() -> int:
    for attempt in range(1, 6):
        print(f"[{attempt}] scanning for controller in pairing mode...", flush=True)
        found = await find_first(
            timeout=25.0, only_pids={P.NSO_GAMECUBE_PID}, require_pairing=True
        )
        if not found:
            print("  not seen; re-trigger pairing mode", flush=True)
            continue
        print(f"  found {found.device.address}; connecting...", flush=True)
        client = BleakClient(found.device, timeout=20.0)
        try:
            await client.connect()
            print(f"  connected={client.is_connected}", flush=True)
            await dump(client)
            await client.disconnect()
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"  connect failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            # Clear stale BlueZ device state so the next attempt starts clean.
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "remove", found.device.address,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            await asyncio.sleep(1.5)
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
