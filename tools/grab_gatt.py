#!/usr/bin/env python3
"""Reliable GATT grab: use BlueZ (bluetoothctl) to establish the link, then
attach with bleak and force/await service discovery, then dump char UUIDs."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient

MAC = sys.argv[1] if len(sys.argv) > 1 else "AA:BB:CC:DD:EE:01"


async def sh(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


async def bluez_connected() -> bool:
    return "Connected: yes" in await sh("bluetoothctl", "info", MAC)


async def ensure_link() -> bool:
    for i in range(1, 21):
        await sh("bluetoothctl", "--timeout", "4", "scan", "on")
        res = (await sh("bluetoothctl", "connect", MAC)).strip().splitlines()
        tail = res[-1] if res else ""
        print(f"[link {i}] {tail}", flush=True)
        if await bluez_connected():
            print("=== BlueZ link established ===", flush=True)
            return True
        await sh("bluetoothctl", "remove", MAC)
        await asyncio.sleep(0.5)
    return False


async def main() -> int:
    if not await ensure_link():
        print("could not establish BlueZ link (keep controller in pairing mode)")
        return 1

    client = BleakClient(MAC, timeout=25.0)
    await client.connect()
    print(f"bleak attached: connected={client.is_connected}", flush=True)

    services = []
    for _ in range(20):
        try:
            collection = await client.get_services()
        except Exception:  # noqa: BLE001
            collection = client.services
        services = list(getattr(collection, "services", {}).values())
        if services:
            break
        await asyncio.sleep(1)

    print(f"\ndiscovered {len(services)} services\n", flush=True)
    for s in services:
        print(f"[service] {s.uuid}  h={s.handle:#06x}")
        for ch in s.characteristics:
            print(f"    [char] {ch.uuid}  h={ch.handle:#06x}  {ch.properties}")
            for d in ch.descriptors:
                print(f"        [desc] {d.uuid}  h={d.handle:#06x}")

    await client.disconnect()
    return 0 if services else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
