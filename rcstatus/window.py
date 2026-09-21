"""GTK4 + libadwaita window showing OneDrive mount status.

Runs as its own process: the tray needs GTK3 via AppIndicator3, and a single
Python process cannot load both GTK major versions.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rcstatus import service  # noqa: E402
from rcstatus.poller import MultiProbe  # noqa: E402
from rcstatus.probe import Health, format_bytes, format_duration  # noqa: E402

POLL_SECONDS = 2

HEALTH_PRESENTATION = {
    Health.OK: ("object-select-symbolic", "success", "Mount active"),
    Health.DEGRADED: ("dialog-warning-symbolic", "warning", "Status unavailable"),
    Health.ERROR: ("dialog-error-symbolic", "error", "Sync errors"),
    Health.DOWN: ("network-offline-symbolic", "error", "Mount is not running"),
}

CSS = b"""
.status-dot { min-width: 12px; min-height: 12px; }
.transfer-name { font-weight: 600; }
.metric { font-size: 1.6rem; font-weight: 300; }
progressbar trough { min-height: 8px; }
progressbar progress { min-height: 8px; }
"""


class TransferRow(Gtk.ListBoxRow):
    """One in-flight file. Updated in place so the bar animates smoothly."""

    def __init__(self, transfer):
        super().__init__(activatable=False, selectable=False)
        self.path = transfer.path

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.name_label = Gtk.Label(xalign=0, ellipsize=3, hexpand=True)
        self.name_label.add_css_class("transfer-name")
        self.percent_label = Gtk.Label(xalign=1)
        self.percent_label.add_css_class("numeric")
        self.percent_label.add_css_class("dim-label")
        header.append(self.name_label)
        header.append(self.percent_label)

        self.bar = Gtk.ProgressBar(show_text=False)
        self.detail_label = Gtk.Label(xalign=0)
        self.detail_label.add_css_class("caption")
        self.detail_label.add_css_class("dim-label")

        box.append(header)
        box.append(self.bar)
        box.append(self.detail_label)
        self.set_child(box)
        self.update(transfer)

    def update(self, t):
        self.name_label.set_text(t.name)
        self.name_label.set_tooltip_text(t.path)
        self.percent_label.set_text(f"{t.percent}%")
        self.bar.set_fraction(t.fraction)
        self.detail_label.set_text(
            f"{format_bytes(t.bytes)} of {format_bytes(t.size)}"
            f" · {format_bytes(t.speed_bps)}/s"
            f" · {format_duration(t.eta_seconds)} left"
        )


class MeterRow(Gtk.Box):
    """A labelled usage bar for cache or cloud storage."""

    def __init__(self, title):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        label = Gtk.Label(label=title, xalign=0, hexpand=True)
        label.add_css_class("transfer-name")
        self.value_label = Gtk.Label(xalign=1)
        self.value_label.add_css_class("numeric")
        self.value_label.add_css_class("dim-label")
        header.append(label)
        header.append(self.value_label)

        self.bar = Gtk.ProgressBar(show_text=False)
        self.append(header)
        self.append(self.bar)

    def update(self, used, total, fraction, suffix=""):
        if fraction is None:
            self.value_label.set_text("—")
            self.bar.set_fraction(0)
            return
        self.value_label.set_text(
            f"{format_bytes(used)} of {format_bytes(total)}{suffix}"
        )
        self.bar.set_fraction(fraction)
        for cls in ("warning", "error"):
            self.bar.remove_css_class(cls)
        if fraction >= 0.95:
            self.bar.add_css_class("error")
        elif fraction >= 0.85:
            self.bar.add_css_class("warning")


class MountCard(Adw.ExpanderRow):
    """One mount: summary when collapsed, full detail when expanded."""

    def __init__(self, snapshot, window):
        super().__init__()
        self.window = window
        self.rows = {}

        self.icon = Gtk.Image(icon_name="object-select-symbolic", pixel_size=16)
        self.add_prefix(self.icon)

        self.transfers_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.transfers_list.add_css_class("boxed-list")
        self.empty_label = Gtk.Label(label="No transfers in progress")
        self.empty_label.add_css_class("dim-label")
        self.empty_label.set_margin_top(12)
        self.empty_label.set_margin_bottom(12)
        self.stack = Gtk.Stack()
        self.stack.add_named(self.empty_label, "empty")
        self.stack.add_named(self.transfers_list, "list")

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(6)
        body.set_margin_bottom(12)
        body.set_margin_start(12)
        body.set_margin_end(12)
        body.append(self.stack)

        self.cache_meter = MeterRow("Local cache")
        self.quota_meter = MeterRow("Remote")
        body.append(self.cache_meter)
        body.append(self.quota_meter)

        self.actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.open_button = Gtk.Button(label="Open Folder")
        self.open_button.connect(
            "clicked",
            lambda *_: subprocess.Popen(["xdg-open", self.mountpoint]),
        )
        self.restart_button = Gtk.Button(label="Restart Mount")
        self.restart_button.connect("clicked", self._restart)
        self.actions.append(self.open_button)
        self.actions.append(self.restart_button)
        body.append(self.actions)

        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        row.set_child(body)
        self.add_row(row)

        self.update(snapshot)

    def _restart(self, *_):
        if self._unit and self._user_unit:
            subprocess.Popen(["systemctl", "--user", "restart", self._unit])
            self.window.banner.set_title("Restarting the mount…")
            self.window.banner.set_revealed(True)

    def update(self, snap):
        mount = snap.mount
        self.mountpoint = mount.mountpoint
        self._unit = mount.unit
        self._user_unit = mount.user_unit

        self.set_title(mount.remote)
        self.set_subtitle(f"{mount.mountpoint} · {snap.summary}")

        icon, css, _ = HEALTH_PRESENTATION[snap.health]
        if snap.health is Health.OK and snap.transfers:
            icon = "network-transmit-symbolic"
        self.icon.set_from_icon_name(icon)
        for cls in ("success", "warning", "error"):
            self.icon.remove_css_class(cls)
        self.icon.add_css_class(css)

        # A mount may legitimately offer no restart: only user units can be
        # restarted without privilege we do not have.
        self.restart_button.set_visible(mount.can_restart)

        self._sync_transfers(snap)
        self.cache_meter.update(
            snap.cache_bytes, snap.cache_max_bytes, snap.cache_fraction,
            suffix=f" · {snap.cache_files} files" if snap.cache_files else "",
        )
        self.quota_meter.update(snap.quota_used, snap.quota_total, snap.quota_fraction)

        for meter in (self.cache_meter, self.quota_meter):
            meter.set_visible(snap.stats_available)

    def _sync_transfers(self, snap):
        seen = set()
        for t in snap.transfers:
            seen.add(t.path)
            row = self.rows.get(t.path)
            if row is None:
                row = TransferRow(t)
                self.rows[t.path] = row
                self.transfers_list.append(row)
            else:
                row.update(t)
        for path in list(self.rows):
            if path not in seen:
                self.transfers_list.remove(self.rows.pop(path))
        self.stack.set_visible_child_name("list" if self.rows else "empty")


class StatusWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Rclone Status")
        self.set_default_size(460, 740)
        self.prober = MultiProbe()
        self.cards = {}

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh now")
        refresh.connect("clicked", lambda *_: self.refresh())
        header.pack_start(refresh)

        view.add_top_bar(header)

        self.banner = Adw.Banner(revealed=False)
        view.add_top_bar(self.banner)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=520)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(12)
        content.set_margin_end(12)

        self.mounts_group = Adw.PreferencesGroup(title="Mounts")
        self.mounts_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.mounts_list.add_css_class("boxed-list")
        self.empty_page = Adw.StatusPage(
            icon_name="folder-remote-symbolic",
            title="No rclone mounts found",
            description="Mount a remote with `rclone mount` and it will appear here.",
        )
        self.mounts_stack = Gtk.Stack()
        self.mounts_stack.add_named(self.empty_page, "empty")
        self.mounts_stack.add_named(self.mounts_list, "list")
        self.mounts_group.add(self.mounts_stack)
        content.append(self.mounts_group)
        content.append(self._build_tray_toggle())

        clamp.set_child(content)
        scroller.set_child(clamp)
        view.set_content(scroller)
        self.set_content(view)

        self._install_css()
        self.refresh()
        GLib.timeout_add_seconds(POLL_SECONDS, self._tick)

    def _build_tray_toggle(self):
        group = Adw.PreferencesGroup(title="Tray")
        self.tray_row = Adw.SwitchRow(
            title="Tray icon",
            subtitle="Show in the top bar and start at login",
        )
        self.tray_handler = self.tray_row.connect("notify::active", self._on_tray_toggled)
        group.add(self.tray_row)
        return group

    def _set_tray_row(self, st):
        """Reflect unit state without the update re-triggering the handler."""
        self.tray_row.handler_block(self.tray_handler)
        self.tray_row.set_active(st.enabled or st.active)
        self.tray_row.set_sensitive(st.usable)
        self.tray_row.set_subtitle(st.detail)
        self.tray_row.handler_unblock(self.tray_handler)

    def _on_tray_toggled(self, row, _param):
        wanted = row.get_active()
        ok, message = service.enable() if wanted else service.disable()
        if not ok:
            self.banner.set_title(message or "Could not change the tray setting")
            self.banner.set_revealed(True)
        # Re-read rather than trusting the switch: systemd is the truth.
        self._set_tray_row(service.state())

    def _install_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _tick(self):
        self.refresh()
        return GLib.SOURCE_CONTINUE

    def refresh(self):
        system = self.prober.poll()

        seen = set()
        created = []
        for snap in system.mounts:
            key = snap.mount.key
            seen.add(key)
            card = self.cards.get(key)
            if card is None:
                card = MountCard(snap, self)
                self.cards[key] = card
                self.mounts_list.append(card)
                created.append(card)
            else:
                card.update(snap)
        for key in list(self.cards):
            if key not in seen:
                self.mounts_list.remove(self.cards.pop(key))

        # Expand a lone mount when its card first appears, so a one-mount
        # machine looks as it did before multi-mount support. Only on
        # creation -- doing it every refresh would fight a user trying to
        # collapse it.
        if len(self.cards) == 1 and created:
            created[0].set_expanded(True)

        self.mounts_stack.set_visible_child_name("list" if self.cards else "empty")
        self.mounts_group.set_title(
            "Mounts" if len(self.cards) != 1 else "Mount"
        )
        self.set_title(f"Rclone Status — {system.summary}")
        self._set_tray_row(service.state())


class StatusApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="dev.matheoatche.RcloneStatus")

    def do_activate(self):
        window = self.props.active_window or StatusWindow(self)
        window.present()


def main():
    return StatusApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
