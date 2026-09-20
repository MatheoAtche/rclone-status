"""Tray indicator for the OneDrive mount.

GTK3-only by necessity: AppIndicator3 links against GTK3, which cannot be
loaded alongside the window's GTK4 in one process. The window is therefore
launched as a subprocess.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
from gi.repository import AppIndicator3, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from odstatus.probe import (  # noqa: E402
    MOUNTPOINT,
    UNIT,
    Health,
    Probe,
    format_bytes,
    format_duration,
)

POLL_SECONDS = 2

# Icons verified present in the Adwaita theme on this system.
ICONS = {
    Health.OK: "object-select-symbolic",
    Health.DEGRADED: "dialog-warning-symbolic",
    Health.ERROR: "dialog-error-symbolic",
    Health.DOWN: "network-offline-symbolic",
}
UPLOADING_ICON = "network-transmit-symbolic"


class Tray:
    def __init__(self):
        self.probe = Probe()
        self.indicator = AppIndicator3.Indicator.new(
            "onedrive-status",
            ICONS[Health.OK],
            AppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

        self.menu = Gtk.Menu()
        self.summary_item = Gtk.MenuItem(label="Checking…")
        self.summary_item.set_sensitive(False)
        self.menu.append(self.summary_item)

        self.detail_item = Gtk.MenuItem(label="")
        self.detail_item.set_sensitive(False)
        self.menu.append(self.detail_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        open_window = Gtk.MenuItem(label="Open OneDrive Status")
        open_window.connect("activate", self.open_window)
        self.menu.append(open_window)

        open_folder = Gtk.MenuItem(label="Open OneDrive Folder")
        open_folder.connect(
            "activate", lambda *_: subprocess.Popen(["xdg-open", MOUNTPOINT])
        )
        self.menu.append(open_folder)

        restart = Gtk.MenuItem(label="Restart Mount")
        restart.connect(
            "activate",
            lambda *_: subprocess.Popen(["systemctl", "--user", "restart", UNIT]),
        )
        self.menu.append(restart)

        self.menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        self.menu.append(quit_item)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        # Middle-click opens the window without navigating the menu.
        self.indicator.set_secondary_activate_target(open_window)

        self.refresh()
        GLib.timeout_add_seconds(POLL_SECONDS, self.refresh)

    def open_window(self, *_):
        # PYTHONPATH rather than cwd: the tray may be started from anywhere,
        # and a child inheriting the wrong cwd cannot import odstatus.
        repo = str(pathlib.Path(__file__).resolve().parents[1])
        env = dict(os.environ)
        env["PYTHONPATH"] = repo + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        subprocess.Popen([sys.executable, "-m", "odstatus.window"], cwd=repo, env=env)

    def refresh(self):
        snap = self.probe.poll()

        icon = ICONS[snap.health]
        if snap.health is Health.OK and snap.transfers:
            icon = UPLOADING_ICON
        self.indicator.set_icon_full(icon, snap.summary)

        # The label sits next to the icon in the top bar; keep it short and
        # show it only while something is actually moving.
        if snap.transfers:
            self.indicator.set_label(f"{len(snap.transfers)}↑", "99↑")
        else:
            self.indicator.set_label("", "")

        self.summary_item.set_label(snap.summary)

        if snap.transfers:
            detail = "  ·  ".join(
                f"{t.name[:28]} {t.percent}%" for t in snap.transfers[:3]
            )
            if snap.overall_eta_seconds is not None:
                detail += f"  ·  {format_duration(snap.overall_eta_seconds)} left"
        elif snap.health is Health.DOWN:
            detail = "systemctl --user start " + UNIT
        else:
            cache = format_bytes(snap.cache_bytes)
            quota = format_bytes(snap.quota_used)
            detail = f"Cache {cache}  ·  OneDrive {quota} used"

        self.detail_item.set_label(detail)
        self.indicator.set_title(f"OneDrive — {snap.summary}")
        return GLib.SOURCE_CONTINUE


def main():
    Tray()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
