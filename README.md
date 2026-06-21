# Switch 2 Controllers on Linux

Wireless **Nintendo Switch 2** controller support for Linux — including the
**NSO GameCube controller**, **Pro Controller 2**, and **Joy-Con 2** — with
working buttons, analog sticks/triggers, **rumble**, **gyro/accelerometer**
(via a built-in DSU/cemuhook server), battery reporting, and reliable
wake-from-sleep reconnection.

The Switch 2 controllers use a proprietary Bluetooth LE (GATT) protocol that
standard Linux gamepad stacks don't understand. This project talks to them
directly over a **raw L2CAP ATT socket** (bypassing BlueZ's GATT layer
entirely), decodes the input reports, and exposes each controller as a normal
`uinput` gamepad that Steam, SDL, and emulators see as a standard pad. It runs
fully in user space — no kernel modules, no root.

> Unofficial and not affiliated with or endorsed by Nintendo. Built from
> community protocol research (see [Credits](#credits)). Use at your own risk.

## Features

- **Multiple controllers at once** (local multiplayer): one worker per pad, each
  with its own virtual gamepad, rumble, and DSU motion slot.
- **Reliable wake-connect**: serialized LE connection coordinator so several
  asleep controllers reconnect reliably with a button press.
- **Rumble**: HD rumble for Pro Controller 2 / Joy-Con 2 (real motor packets);
  preset-based, edge-driven rumble for the NSO GameCube pad (no HD actuator).
- **Gyro everywhere**: an embedded **DSU (cemuhook) UDP server** on
  `127.0.0.1:26760` feeds accel/gyro to Dolphin, Cemu, Ryujinx, etc.
- **Analog triggers + C-stick** on the NSO GameCube pad; calibrated sticks.
- **Auto-reconnect** with bonding (no re-pairing after the first time).
- **systemd --user service** for hands-off background operation.

## Supported controllers

| Controller            | Buttons | Sticks | Triggers      | Rumble        | Gyro |
| --------------------- | ------- | ------ | ------------- | ------------- | ---- |
| NSO GameCube          | ✅      | ✅     | ✅ analog L/R | ✅ presets    | ✅   |
| Pro Controller 2      | ✅      | ✅     | digital ZL/ZR | ✅ HD rumble  | ✅   |
| Joy-Con 2 (L / R)     | ✅      | ✅     | digital       | ✅ HD rumble  | ✅   |

## How it works

```
Controller (BLE)
   │  raw L2CAP ATT socket (BT_SECURITY_LOW), no BlueZ GATT
   ▼
ngc.att        — minimal ATT client (read/write/notify) over L2CAP
ngc.protocol   — input-report parsing, calibration, vibration, button maps
ngc.device     — SwitchController: handshake, GATT discovery, LEDs, rumble
   │
   ├─► ngc.gamepad — uinput virtual pad (buttons/axes/triggers + force-feedback)
   └─► ngc.dsu     — DSU/cemuhook UDP server (motion) on 127.0.0.1:26760
   │
ngc.bridge     — multi-controller manager: connect, reconnect, own gamepad+rumble
```

Key design choices:

- **Raw L2CAP instead of BlueZ GATT.** BlueZ's service discovery is unreliable
  with these controllers; a direct ATT socket with `BT_SECURITY_LOW` connects
  fast and predictably.
- **Serialized create-connection.** Most adapters allow only one outstanding LE
  connection initiation at a time, so the bridge serializes connects behind a
  lock — the fix for "only one of my two controllers wakes up."
- **Standard `uinput` layout.** The virtual pad uses the conventional
  Xbox-style evdev button positions so Steam Input and SDL map A/B/X/Y to the
  printed labels with no custom database entry required.

## Requirements

- Linux with a Bluetooth LE adapter
- Python 3.12+
- `bleak==0.22.2` (BLE scanning during pairing) and `evdev>=1.6`
- `/dev/uinput` writable by your user (Bazzite/most distros grant this via a
  udev ACL; otherwise add a udev rule)

## Install

```bash
git clone https://github.com/trevlars/switch2-controllers-linux.git
cd switch2-controllers-linux

# Option A: simple venv
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Option B: Bazzite / immutable-friendly bootstrap (uv + Python 3.12 + service)
bash scripts/install.sh
```

## Usage

```bash
# 1) Put the controller in pairing mode, then pair (bonds for auto-reconnect):
python -m ngc pair

# 2) List configured controllers:
python -m ngc list

# 3) Run the bridge (foreground):
python -m ngc run
```

As a background service:

```bash
systemctl --user enable --now nso-gc.service
journalctl --user -u nso-gc.service -f
```

Configuration lives at `~/.config/nso-gc/config.json` (controllers, player
order, adapter MAC, rumble toggle). Pairing writes it for you.

## Gyro / motion (DSU)

When the bridge runs it starts a DSU/cemuhook server on `127.0.0.1:26760`
exposing one slot per controller (slot = player − 1). Point any cemuhook-capable
emulator at it:

- **Dolphin**: `DSUClient.ini` → add `127.0.0.1:26760`, enable in Wii motion
  bindings as needed.
- **Ryujinx**: set the controller's motion backend to **CemuHook**, host
  `127.0.0.1`, port `26760`, matching slot.
- **Cemu**: add the DSU server in the motion source settings.

## Rumble notes

- **Pro Controller 2 / Joy-Con 2** have real HD-rumble actuators driven by a
  sustaining worker thread, so effects feel continuous.
- **NSO GameCube** has no HD actuator (writing the HD characteristic powers it
  off), so it replays built-in presets. Rumble is edge-driven: a fresh onset
  fires a strong buzz, while sustained effects emit gentle light pulses — punchy
  hits without a constant slam.

## Bazzite / EmuDeck integration (optional)

The [`system/`](system/) folder contains an example integration for a Bazzite +
EmuDeck setup:

- `bazzite-controller-detect.py` — assigns emulator player order and per-pad
  profiles (recognizes the new virtual pads by name).
- `dolphin/GC_nso_gamecube.ini` — native GameCube Dolphin profile.
- `ryujinx/Switch2_Pro.json` + `patch_ryujinx_motion.py` — Switch 2 Pro profile
  and CemuHook motion wiring.

Install with `scripts/install-emulator-integration.sh`. Per-device MACs are
read from `BAZZITE_*_MAC` env vars (empty by default; name-based classification
works without them).

## Project layout

```
ngc/        core bridge (att, protocol, device, gamepad, dsu, bridge, cli)
scripts/    install / deploy / bluetooth-prep helpers
systemd/    user service unit
system/     optional Bazzite/EmuDeck emulator integration (example)
tools/      diagnostics: scanning, GATT discovery, rumble/preset tests, DSU test
```

## Credits

This stands on the shoulders of prior community work:

- [TommyWabg/switch2-controllers-windows10-gyro](https://github.com/TommyWabg/switch2-controllers-windows10-gyro)
  — Switch 2 BLE protocol research.
- [joaorb64/joycond-cemuhook](https://github.com/joaorb64/joycond-cemuhook) and
  [TheDrHax/ds4drv-cemuhook](https://github.com/TheDrHax/ds4drv-cemuhook)
  — DSU server reference.
- [v1993/cemuhook-protocol](https://github.com/v1993/cemuhook-protocol)
  — the cemuhook UDP protocol documentation.
- [bleak](https://github.com/hbldh/bleak) and
  [python-evdev](https://github.com/gvalkov/python-evdev).

## License

[MIT](LICENSE)
