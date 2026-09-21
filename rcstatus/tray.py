"""Tray indicator for rclone mounts.

GTK3-only by necessity: AppIndicator3 links against GTK3, which cannot be
loaded alongside the window's GTK4 in one process. The window is therefore
launched as a subprocess.
"""

from __future__ import annotations

import fcntl
import os
import pathlib
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
from gi.repository import AppIndicator3, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rcstatus import service  # noqa: E402
from rcstatus.poller import MultiProbe  # noqa: E402
from rcstatus.probe import Health  # noqa: E402

POLL_SECONDS = 2

# Icons verified present in the Adwaita theme on this system.
ICONS = {
    Health.OK: "object-select-symbolic",
    Health.DEGRADED: "dialog-warning-symbolic",
    Health.ERROR: "dialog-error-symbolic",
    Health.DOWN: "network-offline-symbolic",
}
UPLOADING_ICON = "network-transmit-symbolic"
# Zero mounts is not "all healthy" -- there is nothing to be healthy about,
# so it gets its own neutral icon rather than the OK checkmark.
NO_MOUNTS_ICON = "folder-remote-symbolic"


class Tray:
    def __init__(self):
        self.prober = MultiProbe()
        self.indicator = AppIndicator3.Indicator.new(
            "rclone-status",
            ICONS[Health.OK],
            AppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

        self.menu = Gtk.Menu()
        self.summary_item = Gtk.MenuItem(label="Checking…")
        self.summary_item.set_sensitive(False)
        self.menu.append(self.summary_item)

        self.mount_separator = Gtk.SeparatorMenuItem()
        self.menu.append(self.mount_separator)

        # Per-mount rows are rebuilt on change; this tracks what we added.
        self.mount_items = {}

        open_window = Gtk.MenuItem(label="Open Rclone Status")
        open_window.connect("activate", self.open_window)
        self.menu.append(open_window)

        self.menu.append(Gtk.SeparatorMenuItem())

        hide_item = Gtk.MenuItem(label="Hide Tray Icon")
        hide_item.connect("activate", self.hide_tray)
        self.menu.append(hide_item)

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
        # and a child inheriting the wrong cwd cannot import rcstatus.
        repo = str(pathlib.Path(__file__).resolve().parents[1])
        env = dict(os.environ)
        env["PYTHONPATH"] = repo + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        subprocess.Popen([sys.executable, "-m", "rcstatus.window"], cwd=repo, env=env)

    def hide_tray(self, *_):
        """Turn the tray off for good, then exit under our own power.

        stop=False: --now would have systemd SIGTERM us mid-call. Exiting with
        status 0 also keeps Restart=on-failure from bringing us back.
        """
        service.disable(stop=False)
        Gtk.main_quit()

    def refresh(self):
        system = self.prober.poll()

        if not system.mounts:
            icon = NO_MOUNTS_ICON
        else:
            icon = ICONS[system.worst_health]
            if system.worst_health is Health.OK and system.total_transfers:
                icon = UPLOADING_ICON
        self.indicator.set_icon_full(icon, system.summary)

        # The label sits next to the icon in the top bar; keep it short and
        # show it only while something is actually moving.
        if system.total_transfers:
            self.indicator.set_label(f"{system.total_transfers}↑", "99↑")
        else:
            self.indicator.set_label("", "")

        self.summary_item.set_label(system.summary)
        self._sync_mount_items(system)
        self.indicator.set_title(f"Rclone — {system.summary}")
        return GLib.SOURCE_CONTINUE

    def _sync_mount_items(self, system):
        """One disabled menu row per mount, in discovery order."""
        wanted = {}
        for snap in system.mounts:
            marker = {
                Health.OK: "●", Health.DEGRADED: "⚠",
                Health.ERROR: "⚠", Health.DOWN: "✕",
            }[snap.health]
            wanted[snap.mount.key] = f"{marker} {snap.mount.remote}  {snap.summary}"

        for key, label in wanted.items():
            item = self.mount_items.get(key)
            if item is None:
                item = Gtk.MenuItem(label=label)
                item.set_sensitive(False)
                self.mount_items[key] = item
                self.menu.insert(item, len(self.mount_items))
                item.show()
            else:
                item.set_label(label)

        for key in list(self.mount_items):
            if key not in wanted:
                self.menu.remove(self.mount_items.pop(key))


# Held for the process lifetime; releasing it would let a second tray start.
_LOCK_HANDLE = None


def acquire_single_instance_lock():
    """Take an exclusive lock, or return False if another tray holds it.

    Two indicators for one machine is confusing, and the enable/disable toggle
    makes it easy to start a second one beside a hand-launched first. flock is
    released automatically when the process dies, however it dies, so a crash
    cannot leave the tray permanently unstartable.
    """
    global _LOCK_HANDLE
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/rcstatus-{os.getuid()}"
    try:
        os.makedirs(runtime, exist_ok=True)
        handle = open(os.path.join(runtime, "rclone-status-tray.lock"), "w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    _LOCK_HANDLE = handle
    return True


def main():
    if not acquire_single_instance_lock():
        print("Rclone Status tray is already running.")
        return 0
    Tray()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
