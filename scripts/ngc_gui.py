#!/usr/bin/env python3
"""Switch 2 Controllers — polished control panel for Bazzite."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk
except ImportError as exc:
    print(f"GTK unavailable: {exc}", file=sys.stderr)
    sys.exit(2)

PROJECT_DIR = Path(os.environ.get("NGC_PROJECT_DIR", Path.home() / "nso-gc-bazzite"))
PY = Path(os.environ.get("NGC_PYTHON", PROJECT_DIR / ".venv312" / "bin" / "python"))
SERVICE = "nso-gc.service"
STATE_PATH = Path.home() / ".config" / "nso-gc" / "state.json"
STATE_STALE_S = 8.0

CSS = b"""
window {
    background-color: #0a0a0c;
}
.hero {
    font-size: 26px;
    font-weight: 700;
    letter-spacing: -0.5px;
    color: #ffffff;
}
.subtitle {
    font-size: 13px;
    color: #8e8e93;
}
.hero-card {
    background-color: #161618;
    border-radius: 16px;
    padding: 18px 20px;
    border: 1px solid #2c2c2e;
}
.status-connected { color: #30d158; font-weight: 700; font-size: 20px; }
.status-ready { color: #0a84ff; font-weight: 700; font-size: 20px; }
.status-setup { color: #ff9f0a; font-weight: 700; font-size: 20px; }
.status-off { color: #ff453a; font-weight: 700; font-size: 20px; }
.detail {
    color: #aeaeb2;
    font-size: 13px;
}
.section-label {
    color: #636366;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.6px;
}
.pad-card {
    background-color: #161618;
    border-radius: 14px;
    padding: 14px 12px 14px 16px;
    border: 1px solid #2c2c2e;
    margin-bottom: 10px;
}
.pad-card-live {
    border-color: #1f6f3f;
    background-color: #121816;
}
.player-pill {
    background-color: #2c2c2e;
    color: #ffffff;
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 12px;
    font-weight: 700;
    min-width: 28px;
}
.player-pill-live {
    background-color: #1f6f3f;
    color: #b8f5d0;
}
.pad-title { color: #ffffff; font-size: 15px; font-weight: 600; }
.pad-meta { color: #8e8e93; font-size: 12px; }
.pad-status-connected { color: #30d158; font-size: 12px; font-weight: 600; }
.pad-status-asleep { color: #0a84ff; font-size: 12px; }
.pad-status-setup { color: #ff9f0a; font-size: 12px; }
.pad-status-off { color: #636366; font-size: 12px; }
.hint {
    color: #636366;
    font-size: 12px;
}
.primary-btn {
    background: #0a84ff;
    color: #ffffff;
    border-radius: 12px;
    padding: 12px 20px;
    font-weight: 600;
    font-size: 14px;
    border: none;
}
.primary-btn:hover { background: #409cff; }
.secondary-btn {
    background: #2c2c2e;
    color: #ffffff;
    border-radius: 12px;
    padding: 11px 18px;
    font-weight: 500;
    border: none;
}
.secondary-btn:hover { background: #3a3a3c; }
.badge-on {
    background: #1f6f3f;
    color: #b8f5d0;
    border-radius: 999px;
    padding: 5px 12px;
    font-size: 11px;
    font-weight: 700;
}
.badge-off {
    background: #3a2a2a;
    color: #ff8a80;
    border-radius: 999px;
    padding: 5px 12px;
    font-size: 11px;
    font-weight: 700;
}
"""


@dataclass
class PadStatus:
    player: int
    name: str
    mac: str
    bonded: bool
    connected: bool = False
    battery_pct: int | None = None


@dataclass
class AppStatus:
    service: str
    headline: str
    detail: str
    css_class: str
    pads: list[PadStatus]
    connected_count: int
    needs_action: bool
    source: str


def ensure_display_env() -> bool:
    """Ensure Wayland/X11 env vars are set when launched from a bare .desktop Exec."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    os.environ.setdefault("XDG_RUNTIME_DIR", runtime)
    os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    if not os.environ.get("WAYLAND_DISPLAY"):
        run_path = Path(runtime)
        if run_path.is_dir():
            for sock in sorted(run_path.glob("wayland-*")):
                if sock.is_socket():
                    os.environ["WAYLAND_DISPLAY"] = sock.name
                    break
    if not os.environ.get("DISPLAY") and os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("DISPLAY", ":0")
    os.environ.setdefault("GDK_BACKEND", "wayland")
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def _run(cmd: list[str], *, timeout: float = 30.0, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)


def ensure_installed() -> bool:
    if PY.is_file() and os.access(PY, os.X_OK):
        return True
    install = PROJECT_DIR / "scripts" / "install.sh"
    if not install.is_file():
        return False
    return _run(["bash", str(install)], timeout=600).returncode == 0 and PY.is_file()


def ensure_service() -> None:
    _run(["systemctl", "--user", "reset-failed", SERVICE], timeout=5)
    _run(["systemctl", "--user", "enable", "--now", SERVICE], timeout=15)


def service_state() -> str:
    r = _run(["systemctl", "--user", "is-active", SERVICE], timeout=5)
    return (r.stdout or "inactive").strip() or "inactive"


def load_pads_from_config() -> list[PadStatus]:
    if PY.is_file():
        r = _run([str(PY), "-m", "ngc", "list"], timeout=10, cwd=str(PROJECT_DIR))
        pads: list[PadStatus] = []
        row_re = re.compile(
            r"P(\d+)\s+([0-9A-F:]{17})\s+(.+?)\s+\[(.+)\]\s*$", re.IGNORECASE
        )
        for line in (r.stdout or "").splitlines():
            m = row_re.match(line.strip())
            if not m:
                continue
            flags = m.group(4).lower()
            pads.append(
                PadStatus(
                    int(m.group(1)),
                    m.group(3).strip(),
                    m.group(2).upper(),
                    "bonded" in flags and "needs bond" not in flags,
                )
            )
        if pads:
            return pads
    cfg_path = Path.home() / ".config" / "nso-gc" / "config.json"
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text())
            return [
                PadStatus(
                    c.get("player", i + 1),
                    c.get("name") or "Switch 2 Controller",
                    c["mac"].upper(),
                    bool(c.get("bonded", False)),
                )
                for i, c in enumerate(data.get("controllers") or [])
            ]
        except Exception:
            pass
    return []


def read_bridge_state() -> dict | None:
    if not STATE_PATH.is_file():
        return None
    try:
        data = json.loads(STATE_PATH.read_text())
        updated = float(data.get("updated_at") or 0)
        if updated and time.time() - updated > STATE_STALE_S:
            return None
        return data
    except Exception:
        return None


def merge_state_with_pads(state: dict | None, pads: list[PadStatus]) -> list[PadStatus]:
    if not state:
        return pads
    by_mac = {c.get("mac", "").upper(): c for c in state.get("controllers") or []}
    merged: list[PadStatus] = []
    for pad in pads:
        live = by_mac.get(pad.mac, {})
        merged.append(
            PadStatus(
                pad.player,
                live.get("name") or pad.name,
                pad.mac,
                pad.bonded,
                connected=bool(live.get("connected")),
                battery_pct=live.get("battery_pct"),
            )
        )
    return merged


def build_status() -> AppStatus:
    svc = service_state()
    pads = sorted(load_pads_from_config(), key=lambda p: p.player)
    state = read_bridge_state() if svc == "active" else None
    pads = merge_state_with_pads(state, pads)
    connected = sum(1 for p in pads if p.connected)

    if svc != "active":
        return AppStatus(
            service=svc,
            headline="Bridge is off",
            detail="Controllers cannot connect until the bridge is running. It stops briefly when you add a controller.",
            css_class="status-off",
            pads=pads,
            connected_count=0,
            needs_action=True,
            source="systemd",
        )

    if state:
        headline = state.get("headline") or "Ready"
        detail = state.get("detail") or ""
        hub_error = state.get("hub_error") or ""
        if hub_error:
            return AppStatus(
                service=svc,
                headline="Needs attention",
                detail=hub_error[:200],
                css_class="status-off",
                pads=pads,
                connected_count=connected,
                needs_action=True,
                source="state",
            )
        css = "status-connected" if connected else "status-ready" if pads else "status-setup"
        return AppStatus(
            service=svc,
            headline=headline,
            detail=detail,
            css_class=css,
            pads=pads,
            connected_count=connected,
            needs_action=False,
            source="state",
        )

    if not pads:
        return AppStatus(
            service=svc,
            headline="Get started",
            detail="Add your first controller — a one-time setup per pad.",
            css_class="status-setup",
            pads=pads,
            connected_count=0,
            needs_action=False,
            source="fallback",
        )

    if connected:
        names = ", ".join(f"P{p.player}" for p in pads if p.connected)
        return AppStatus(
            service=svc,
            headline=f"{connected} connected",
            detail=f"{names} — ready in Steam and emulators",
            css_class="status-connected",
            pads=pads,
            connected_count=connected,
            needs_action=False,
            source="fallback",
        )

    return AppStatus(
        service=svc,
        headline="Ready",
        detail="Hold Sync on a saved controller to connect.",
        css_class="status-ready",
        pads=pads,
        connected_count=0,
        needs_action=False,
        source="fallback",
    )


def run_ngc_cmd(args: list[str], timeout: float = 120.0) -> tuple[int, str]:
    _run(["systemctl", "--user", "stop", SERVICE], timeout=10)
    r = _run([str(PY), "-m", "ngc", *args], timeout=timeout, cwd=str(PROJECT_DIR))
    ensure_service()
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def run_ngc_config(args: list[str], *, restart: bool = True) -> tuple[int, str]:
    r = _run([str(PY), "-m", "ngc", *args], timeout=30, cwd=str(PROJECT_DIR))
    if restart:
        _run(["systemctl", "--user", "restart", SERVICE], timeout=15)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def pad_status_line(pad: PadStatus, st: AppStatus) -> tuple[str, str]:
    if st.service != "active":
        return "Bridge stopped", "pad-status-off"
    if pad.connected:
        battery = f" · {pad.battery_pct}%" if pad.battery_pct is not None else ""
        return f"Connected{battery}", "pad-status-connected"
    if not pad.bonded:
        return "Needs setup", "pad-status-setup"
    return "Hold Sync to connect", "pad-status-asleep"


class ProgressDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window, title: str, message: str) -> None:
        super().__init__(title=title, transient_for=parent, modal=True, destroy_with_parent=True)
        self.set_default_size(440, 170)
        self.set_deletable(False)
        box = self.get_content_area()
        box.set_margin_start(28)
        box.set_margin_end(28)
        box.set_margin_top(24)
        box.set_margin_bottom(16)
        box.set_spacing(14)
        lbl = Gtk.Label(label=message)
        lbl.set_line_wrap(True)
        lbl.set_xalign(0)
        lbl.get_style_context().add_class("detail")
        box.pack_start(lbl, False, False, 0)
        self.spinner = Gtk.Spinner()
        self.spinner.start()
        box.pack_start(self.spinner, False, False, 0)
        self.show_all()


class SwitchControllersApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="dev.ngc.switch2-controllers")

    def do_activate(self) -> None:
        if getattr(self, "window", None) is not None:
            self.window.present()
            self.window.set_keep_above(True)
            GLib.timeout_add(200, lambda: (self.window.set_keep_above(False), False)[1])
            self.window.refresh()
            return
        self.window = MainWindow(application=self)
        self.window.show_all()
        self.window.present()
        ensure_service()
        self.window.refresh()


class MainWindow(Gtk.ApplicationWindow):
    REFRESH_MS = 1500

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("Switch 2 Controllers")
        self.set_default_size(500, 620)
        self.set_border_width(0)

        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_screen(
                self.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        except Exception as exc:  # noqa: BLE001
            print(f"CSS load skipped: {exc}", file=sys.stderr)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header.set_margin_start(24)
        header.set_margin_end(24)
        header.set_margin_top(28)
        header.set_margin_bottom(12)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Switch 2 Controllers")
        title.set_xalign(0)
        title.get_style_context().add_class("hero")
        sub = Gtk.Label(label="Runs quietly in the background")
        sub.set_xalign(0)
        sub.get_style_context().add_class("subtitle")
        title_box.pack_start(title, False, False, 0)
        title_box.pack_start(sub, False, False, 0)
        top.pack_start(title_box, True, True, 0)

        self.badge = Gtk.Label(label="")
        self.badge.get_style_context().add_class("badge")
        top.pack_end(self.badge, False, False, 0)

        gear = Gtk.MenuButton()
        gear.set_relief(Gtk.ReliefStyle.NONE)
        gear.set_tooltip_text("More options")
        gear.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        top.pack_end(gear, False, False, 0)
        header.pack_start(top, False, False, 0)

        self._menu = Gtk.Menu()
        self._menu_items = [
            ("Add Controller…", self.on_add),
            ("Remove Controller…", self.on_remove),
            ("Remove & Set Up Again…", self.on_remove_and_repair),
            ("Swap Player 1 ↔ 2", self.on_swap_p1_p2),
            ("Re-bond Controller…", self.on_rebond),
            ("Restart Bridge", self.on_restart),
            ("View Logs", self.on_logs),
            ("Update", self.on_update),
        ]
        for label, cb in self._menu_items:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _w, fn=cb: fn())
            self._menu.append(item)
            item.show()
        self._menu.show()
        gear.set_popup(self._menu)
        outer.pack_start(header, False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_start(20)
        body.set_margin_end(20)
        body.set_margin_bottom(20)
        outer.pack_start(body, True, True, 0)

        self.hero_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.hero_card.get_style_context().add_class("hero-card")
        self.hero_card.set_margin_bottom(4)

        self.status_lbl = Gtk.Label()
        self.status_lbl.set_xalign(0)
        self._status_classes = ("status-connected", "status-ready", "status-setup", "status-off")
        self.hero_card.pack_start(self.status_lbl, False, False, 0)

        self.detail_lbl = Gtk.Label()
        self.detail_lbl.set_xalign(0)
        self.detail_lbl.get_style_context().add_class("detail")
        self.detail_lbl.set_line_wrap(True)
        self.hero_card.pack_start(self.detail_lbl, False, False, 0)
        body.pack_start(self.hero_card, False, False, 0)

        section = Gtk.Label(label="YOUR CONTROLLERS")
        section.set_xalign(0)
        section.get_style_context().add_class("section-label")
        body.pack_start(section, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(220)
        self.pads_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroll.add(self.pads_box)
        body.pack_start(scroll, True, True, 0)

        self.hint_lbl = Gtk.Label()
        self.hint_lbl.set_xalign(0)
        self.hint_lbl.get_style_context().add_class("hint")
        self.hint_lbl.set_line_wrap(True)
        body.pack_start(self.hint_lbl, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.primary_btn = Gtk.Button(label="Add Controller")
        self.primary_btn.get_style_context().add_class("primary-btn")
        self.primary_btn.connect("clicked", self.on_add)
        actions.pack_start(self.primary_btn, False, False, 0)

        self.swap_btn = Gtk.Button(label="Swap Player 1 ↔ 2")
        self.swap_btn.get_style_context().add_class("secondary-btn")
        self.swap_btn.connect("clicked", self.on_swap_p1_p2)
        actions.pack_start(self.swap_btn, False, False, 0)

        self.secondary_btn = Gtk.Button(label="Start Bridge")
        self.secondary_btn.get_style_context().add_class("secondary-btn")
        self.secondary_btn.connect("clicked", self.on_restart)
        actions.pack_start(self.secondary_btn, False, False, 0)
        body.pack_start(actions, False, False, 0)

        self._busy = False
        GLib.timeout_add(self.REFRESH_MS, self._tick)

    def _make_pad_menu(self, pad: PadStatus) -> Gtk.Menu:
        menu = Gtk.Menu()

        def add_item(label: str, cb) -> None:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _w, fn=cb: fn())
            menu.append(item)
            item.show()

        add_item("Re-bond…", lambda: self.on_rebond_pad(pad))
        add_item("Set Up Again…", lambda: self.on_repair_pad(pad))
        add_item("Remove…", lambda: self.on_remove_pad(pad))
        menu.show()
        return menu

    def _build_pad_row(self, pad: PadStatus, st: AppStatus) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        ctx = row.get_style_context()
        ctx.add_class("pad-card")
        if pad.connected:
            ctx.add_class("pad-card-live")

        pill = Gtk.Label(label=f"P{pad.player}")
        pill.get_style_context().add_class("player-pill")
        if pad.connected:
            pill.get_style_context().add_class("player-pill-live")
        row.pack_start(pill, False, False, 0)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label=pad.name or "Switch 2 Controller")
        title.set_xalign(0)
        title.get_style_context().add_class("pad-title")
        status_text, status_class = pad_status_line(pad, st)
        status = Gtk.Label(label=status_text)
        status.set_xalign(0)
        status.get_style_context().add_class(status_class)
        info.pack_start(title, False, False, 0)
        info.pack_start(status, False, False, 0)
        row.pack_start(info, True, True, 0)

        gear = Gtk.MenuButton()
        gear.set_relief(Gtk.ReliefStyle.NONE)
        gear.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        gear.set_popup(self._make_pad_menu(pad))
        row.pack_end(gear, False, False, 0)
        return row

    def _tick(self) -> bool:
        if not self._busy and service_state() != "active":
            ensure_service()
        self.refresh()
        return True

    def refresh(self) -> None:
        st = build_status()
        ctx = self.status_lbl.get_style_context()
        for cls in self._status_classes:
            ctx.remove_class(cls)
        ctx.add_class(st.css_class)
        self.status_lbl.set_text(st.headline)
        self.detail_lbl.set_text(st.detail)
        badge_ctx = self.badge.get_style_context()
        badge_ctx.remove_class("badge-on")
        badge_ctx.remove_class("badge-off")
        if st.service == "active":
            badge_ctx.add_class("badge-on")
            self.badge.set_text("Bridge running")
        else:
            badge_ctx.add_class("badge-off")
            self.badge.set_text("Bridge stopped")

        for child in self.pads_box.get_children():
            self.pads_box.remove(child)

        if not st.pads:
            empty = Gtk.Label(label="No controllers saved yet.\nTap Add Controller to get started.")
            empty.set_xalign(0)
            empty.get_style_context().add_class("pad-meta")
            empty.set_line_wrap(True)
            self.pads_box.pack_start(empty, False, False, 0)
        else:
            for pad in st.pads:
                self.pads_box.pack_start(self._build_pad_row(pad, st), False, False, 0)
        self.pads_box.show_all()

        if not st.pads:
            self.primary_btn.set_label("Add Your First Controller")
            self.primary_btn.show()
        elif len(st.pads) >= 8:
            self.primary_btn.hide()
        else:
            self.primary_btn.set_label("Add Another Controller")
            self.primary_btn.show()

        if len(st.pads) >= 2:
            self.swap_btn.show()
        else:
            self.swap_btn.hide()

        if st.needs_action:
            self.secondary_btn.set_label("Start Bridge" if st.service != "active" else "Restart Bridge")
            self.secondary_btn.show()
        else:
            self.secondary_btn.hide()

        if st.service != "active":
            self.hint_lbl.set_text(
                "The bridge stops briefly when adding a controller — that is expected, not a failure."
            )
        elif not st.pads:
            self.hint_lbl.set_text("Hold Sync until the LEDs sweep, then release. You only do this once per pad.")
        elif st.connected_count:
            self.hint_lbl.set_text(
                "Close this window anytime — controllers stay linked in the background."
            )
        elif len(st.pads) >= 2:
            self.hint_lbl.set_text(
                "Hold Sync on each pad to connect. Links stay stable while you play."
            )
        else:
            self.hint_lbl.set_text("Hold Sync to connect. Player slot is set when you add the pad.")

    def _run_task(self, title: str, message: str, fn) -> None:
        self._busy = True
        dlg = ProgressDialog(self, title, message)

        def worker() -> None:
            try:
                ok, text = fn()
            except Exception as exc:  # noqa: BLE001
                ok, text = False, str(exc)

            def done() -> None:
                dlg.destroy()
                self._busy = False
                if ok:
                    self._info("Done", text or "Success.")
                else:
                    self._error("Could not complete", text or "Unknown error.")
                ensure_service()
                self.refresh()

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _info(self, title: str, text: str) -> None:
        d = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK, text=title,
        )
        d.format_secondary_text(text)
        d.run()
        d.destroy()

    def _error(self, title: str, text: str) -> None:
        d = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK, text=title,
        )
        d.format_secondary_text(text[:2000])
        d.run()
        d.destroy()

    def _confirm(self, title: str, text: str) -> bool:
        d = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL, text=title,
        )
        d.format_secondary_text(text)
        ok = d.run() == Gtk.ResponseType.OK
        d.destroy()
        return ok

    def _pick_pad(self, title: str, message: str) -> PadStatus | None:
        pads = sorted(load_pads_from_config(), key=lambda p: p.player)
        if not pads:
            self._info(title, "No controllers saved yet.")
            return None
        if len(pads) == 1:
            return pads[0]

        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True, destroy_with_parent=True)
        dlg.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("_OK", Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(16)
        box.set_margin_bottom(8)
        box.set_spacing(10)
        lbl = Gtk.Label(label=message)
        lbl.set_line_wrap(True)
        lbl.set_xalign(0)
        lbl.get_style_context().add_class("detail")
        box.pack_start(lbl, False, False, 0)
        combo = Gtk.ComboBoxText.new()
        for pad in pads:
            combo.append(pad.mac, f"Player {pad.player} — {pad.name}")
        combo.set_active(0)
        box.pack_start(combo, False, False, 0)
        dlg.show_all()
        if dlg.run() != Gtk.ResponseType.OK:
            dlg.destroy()
            return None
        mac = combo.get_active_id()
        dlg.destroy()
        return next((p for p in pads if p.mac == mac), None)

    def on_add(self, *_args) -> None:
        n = len(load_pads_from_config())
        if not self._confirm(
            "Add Controller",
            "This pauses the bridge for about 30 seconds.\n\n"
            "• Hold Sync only on the new controller\n"
            "• LEDs should sweep, then release\n"
            "• Player slot is automatic — next is "
            f"P{n + 1 if n < 8 else 'full'}\n\n"
            "Already-connected pads will disconnect briefly. That is normal.",
        ):
            return

        def task() -> tuple[bool, str]:
            rc, out = run_ngc_cmd(["pair", "--timeout", "60"], timeout=150)
            if rc == 0:
                return True, "Controller saved.\n\nHold Sync to connect."
            return False, out[-1500:] if out else "Timed out — hold Sync on the new controller and try again."

        self._run_task("Adding Controller", "Hold Sync on the new controller now…", task)

    def on_remove(self, *_args) -> None:
        pad = self._pick_pad(
            "Remove Controller",
            "This removes the controller from this PC.\nThe bridge will restart.",
        )
        if pad is None:
            return
        if not self._confirm(
            "Remove Controller",
            f"Remove {pad.name} (Player {pad.player})?\n\n"
            "You can add it again later from the menu.",
        ):
            return

        def task() -> tuple[bool, str]:
            rc, out = run_ngc_config(["remove", "--mac", pad.mac])
            if rc == 0:
                return True, f"Removed {pad.name}."
            return False, out[-1500:] if out else "Remove failed."

        self._run_task("Removing Controller", "Updating saved controllers…", task)

    def on_remove_pad(self, pad: PadStatus) -> None:
        if not self._confirm(
            "Remove Controller",
            f"Remove {pad.name} (Player {pad.player}) from this PC?",
        ):
            return

        def task() -> tuple[bool, str]:
            rc, out = run_ngc_config(["remove", "--mac", pad.mac])
            if rc == 0:
                return True, f"Removed {pad.name}."
            return False, out[-1500:] if out else "Remove failed."

        self._run_task("Removing Controller", "Updating saved controllers…", task)

    def on_remove_and_repair(self, *_args) -> None:
        pad = self._pick_pad(
            "Remove & Set Up Again",
            "Pick a controller to forget and pair fresh (same player slot).",
        )
        if pad is None:
            return
        if not self._confirm(
            "Remove & Set Up Again",
            f"Remove {pad.name} (Player {pad.player}), then pair it again?\n\n"
            "Hold Sync on that controller when prompted.",
        ):
            return

        player = pad.player

        def task() -> tuple[bool, str]:
            rc, out = run_ngc_config(["remove", "--mac", pad.mac], restart=False)
            if rc != 0:
                return False, out[-1500:] if out else "Remove failed."
            rc, out = run_ngc_cmd(["pair", "--timeout", "60", "--player", str(player)], timeout=150)
            if rc == 0:
                return True, f"{pad.name} set up again as Player {player}.\n\nHold Sync to connect."
            return False, out[-1500:] if out else "Pairing timed out — hold Sync and try again."

        self._run_task(
            "Setting Up Again",
            f"Hold Sync on {pad.name} now…",
            task,
        )

    def on_repair_pad(self, pad: PadStatus) -> None:
        if not self._confirm(
            "Set Up Again",
            f"Remove and re-pair {pad.name} as Player {pad.player}?\n\nHold Sync when prompted.",
        ):
            return
        player = pad.player

        def task() -> tuple[bool, str]:
            rc, out = run_ngc_config(["remove", "--mac", pad.mac], restart=False)
            if rc != 0:
                return False, out[-1500:] if out else "Remove failed."
            rc, out = run_ngc_cmd(["pair", "--timeout", "60", "--player", str(player)], timeout=150)
            if rc == 0:
                return True, f"{pad.name} set up again as Player {player}.\n\nHold Sync to connect."
            return False, out[-1500:] if out else "Pairing timed out."

        self._run_task("Setting Up Again", f"Hold Sync on {pad.name} now…", task)

    def on_rebond_pad(self, pad: PadStatus) -> None:
        if not self._confirm(
            "Re-bond Controller",
            f"Re-bond {pad.name}?\n\nHold Sync until the LEDs sweep, then click OK.",
        ):
            return

        def task() -> tuple[bool, str]:
            rc, out = run_ngc_cmd(["rebond", "--timeout", "45"], timeout=120)
            if rc == 0:
                return True, f"{pad.name} re-bonded. Hold Sync to connect."
            return False, out[-1500:] if out else "Re-bond failed."

        self._run_task("Re-bonding", f"Hold Sync on {pad.name} now…", task)

    def on_swap_p1_p2(self, *_args) -> None:
        pads = load_pads_from_config()
        if len(pads) < 2:
            self._info("Swap Players", "You need at least two saved controllers.")
            return
        p1 = next((p for p in pads if p.player == 1), None)
        p2 = next((p for p in pads if p.player == 2), None)
        if not p1 or not p2:
            self._info("Swap Players", "Need both Player 1 and Player 2 saved to swap.")
            return
        if not self._confirm(
            "Swap Player 1 ↔ 2",
            f"Player 1: {p1.name}\nPlayer 2: {p2.name}\n\n"
            "This swaps their slots for Steam and Dolphin. The bridge will restart.",
        ):
            return

        def task() -> tuple[bool, str]:
            rc, out = run_ngc_config(["swap", "--players", "1", "2"])
            if rc == 0:
                return True, f"Swapped.\n\n{p1.name} is now Player 2.\n{p2.name} is now Player 1."
            return False, out[-1500:] if out else "Swap failed."

        self._run_task("Swapping Players", "Updating player order…", task)

    def on_rebond(self, *_args) -> None:
        if not self._confirm(
            "Re-bond Controller",
            "Use this if pressing a button no longer wakes the controller.\n\n"
            "Hold Sync until the LEDs sweep, then click OK.",
        ):
            return

        def task() -> tuple[bool, str]:
            rc, out = run_ngc_cmd(["rebond", "--timeout", "45"], timeout=120)
            if rc == 0:
                return True, "Re-bonded. Hold Sync to connect."
            return False, out[-1500:] if out else "Re-bond failed."

        self._run_task("Re-bonding", "Hold Sync on the controller now…", task)

    def on_restart(self, *_args) -> None:
        _run(["systemctl", "--user", "reset-failed", SERVICE], timeout=5)
        _run(["systemctl", "--user", "restart", SERVICE], timeout=15)
        self.refresh()

    def on_logs(self, *_args) -> None:
        r = _run(["journalctl", "--user", "-u", SERVICE, "-n", "40", "--no-pager"], timeout=10)
        self._info("Recent Logs", (r.stdout or r.stderr or "(empty)").strip()[-3000:])

    def on_update(self, *_args) -> None:
        def task() -> tuple[bool, str]:
            script = PROJECT_DIR / "scripts" / "install.sh"
            if not script.is_file():
                return False, f"Project not found at {PROJECT_DIR}"
            r = _run(["bash", str(script)], timeout=600, cwd=str(PROJECT_DIR))
            ensure_service()
            if r.returncode == 0:
                return True, "Updated and restarted."
            return False, ((r.stdout or "") + (r.stderr or ""))[-1500:]

        self._run_task("Updating", "Installing latest files…", task)


def main() -> int:
    ensure_display_env()
    if not ensure_installed():
        print("Install failed — run: bash ~/nso-gc-bazzite/scripts/install.sh", file=sys.stderr)
        return 1
    ensure_service()
    app = SwitchControllersApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
