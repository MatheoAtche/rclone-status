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
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from odstatus import service  # noqa: E402
from odstatus.probe import (  # noqa: E402
    MOUNTPOINT,
    UNIT,
    Health,
    Probe,
    format_bytes,
    format_duration,
)

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


class StatusWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="OneDrive Status")
        self.set_default_size(460, 740)
        self.probe = Probe()
        self.rows: dict[str, TransferRow] = {}

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh now")
        refresh.connect("clicked", lambda *_: self.refresh())
        header.pack_start(refresh)

        menu = Gio.Menu()
        menu.append("Open OneDrive Folder", "win.open-folder")
        menu.append("Restart Mount", "win.restart")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_button)
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

        content.append(self._build_status_card())
        content.append(self._build_transfers())
        content.append(self._build_storage())
        content.append(self._build_tray_toggle())

        clamp.set_child(content)
        scroller.set_child(clamp)
        view.set_content(scroller)
        self.set_content(view)

        self._install_actions()
        self._install_css()
        self.refresh()
        GLib.timeout_add_seconds(POLL_SECONDS, self._tick)

    def _build_status_card(self):
        group = Adw.PreferencesGroup()
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        card.set_margin_top(12)
        card.set_margin_bottom(12)
        card.set_margin_start(12)
        card.set_margin_end(12)

        self.status_icon = Gtk.Image(icon_name="object-select-symbolic", pixel_size=32)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.status_title = Gtk.Label(xalign=0)
        self.status_title.add_css_class("title-4")
        self.status_subtitle = Gtk.Label(xalign=0, wrap=True)
        self.status_subtitle.add_css_class("dim-label")
        text.append(self.status_title)
        text.append(self.status_subtitle)

        card.append(self.status_icon)
        card.append(text)

        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        row.set_child(card)
        listbox.append(row)
        group.add(listbox)
        return group

    def _build_transfers(self):
        self.transfers_group = Adw.PreferencesGroup(title="Transfers")
        self.transfers_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.transfers_list.add_css_class("boxed-list")

        self.empty_label = Gtk.Label(label="No transfers in progress")
        self.empty_label.add_css_class("dim-label")
        self.empty_label.set_margin_top(24)
        self.empty_label.set_margin_bottom(24)

        self.transfers_stack = Gtk.Stack()
        self.transfers_stack.add_named(self.empty_label, "empty")
        self.transfers_stack.add_named(self.transfers_list, "list")
        self.transfers_group.add(self.transfers_stack)
        return self.transfers_group

    def _build_storage(self):
        group = Adw.PreferencesGroup(title="Storage")
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")

        self.cache_meter = MeterRow("Local cache")
        self.quota_meter = MeterRow("OneDrive")
        for meter in (self.cache_meter, self.quota_meter):
            row = Gtk.ListBoxRow(activatable=False, selectable=False)
            row.set_child(meter)
            listbox.append(row)

        group.add(listbox)
        return group

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

    def _install_actions(self):
        open_folder = Gio.SimpleAction.new("open-folder", None)
        open_folder.connect(
            "activate", lambda *_: subprocess.Popen(["xdg-open", MOUNTPOINT])
        )
        self.add_action(open_folder)

        restart = Gio.SimpleAction.new("restart", None)
        restart.connect("activate", lambda *_: self._restart_mount())
        self.add_action(restart)

    def _restart_mount(self):
        subprocess.Popen(["systemctl", "--user", "restart", UNIT])
        self.banner.set_title("Restarting the mount…")
        self.banner.set_revealed(True)

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
        snap = self.probe.poll()

        icon, css, title = HEALTH_PRESENTATION[snap.health]
        if snap.health is Health.OK and snap.transfers:
            icon, title = "network-transmit-symbolic", "Uploading"
        self.status_icon.set_from_icon_name(icon)
        for cls in ("success", "warning", "error"):
            self.status_icon.remove_css_class(cls)
        self.status_icon.add_css_class(css)
        self.status_title.set_text(title)

        # The title already says "Uploading", so the subtitle drops that prefix.
        if snap.transfers:
            noun = "file" if len(snap.transfers) == 1 else "files"
            parts = [
                f"{len(snap.transfers)} {noun}",
                f"{format_bytes(snap.total_speed_bps)}/s",
                f"{format_duration(snap.overall_eta_seconds)} left",
            ]
        else:
            parts = [snap.summary]
        if snap.health is Health.OK and snap.uptime_seconds is not None:
            parts.append(f"up {format_duration(snap.uptime_seconds)}")
        self.status_subtitle.set_text(" · ".join(parts))

        if snap.last_error:
            self.banner.set_title(snap.last_error)
            self.banner.set_revealed(True)
        elif snap.health is not Health.DOWN:
            self.banner.set_revealed(False)

        self._sync_transfer_rows(snap)

        self.cache_meter.update(
            snap.cache_bytes, snap.cache_max_bytes, snap.cache_fraction,
            suffix=f" · {snap.cache_files} files" if snap.cache_files else "",
        )
        self.quota_meter.update(snap.quota_used, snap.quota_total, snap.quota_fraction)

        title = "Transfers"
        if snap.uploads_queued:
            title = f"Transfers · {snap.uploads_queued} queued"
        self.transfers_group.set_title(title)

        self._set_tray_row(service.state())

    def _sync_transfer_rows(self, snap):
        """Update rows in place; only add and remove what actually changed."""
        seen = set()
        for transfer in snap.transfers:
            seen.add(transfer.path)
            row = self.rows.get(transfer.path)
            if row is None:
                row = TransferRow(transfer)
                self.rows[transfer.path] = row
                self.transfers_list.append(row)
            else:
                row.update(transfer)

        for path in list(self.rows):
            if path not in seen:
                self.transfers_list.remove(self.rows.pop(path))

        self.transfers_stack.set_visible_child_name("list" if self.rows else "empty")


class StatusApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="dev.matheoatche.OneDriveStatus")

    def do_activate(self):
        window = self.props.active_window or StatusWindow(self)
        window.present()


def main():
    return StatusApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
