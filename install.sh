#!/usr/bin/env bash
# Install desktop entries for OneDrive Status. Safe to re-run.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart"
EXT="appindicatorsupport@rgcjonas.gmail.com"

mkdir -p "$APPS" "$AUTOSTART"

sed "s|^Exec=.*|Exec=$REPO/bin/onedrive-status|" \
    "$REPO/data/dev.matheoatche.OneDriveStatus.desktop" \
    > "$APPS/dev.matheoatche.OneDriveStatus.desktop"
echo "installed: app launcher"

if [[ "${1:-}" == "--no-tray" ]]; then
    rm -f "$AUTOSTART/onedrive-status-tray.desktop"
    echo "skipped:   tray autostart (--no-tray)"
else
    sed "s|^Exec=.*|Exec=$REPO/bin/onedrive-status-tray|" \
        "$REPO/data/onedrive-status-tray.desktop" \
        > "$AUTOSTART/onedrive-status-tray.desktop"
    echo "installed: tray autostart"

    # The tray is invisible on GNOME without this extension: it provides the
    # StatusNotifierWatcher that AppIndicator3 registers against.
    if command -v gnome-extensions >/dev/null 2>&1; then
        if ! gnome-extensions info "$EXT" >/dev/null 2>&1; then
            echo "WARNING:   $EXT is not installed; the tray icon will not appear."
            echo "           Install it from https://extensions.gnome.org/extension/615/appindicator-support/"
        elif [[ "$(gnome-extensions info "$EXT" | awk -F': *' '/Enabled/{print $2}')" != "Yes" ]]; then
            gnome-extensions enable "$EXT" && echo "enabled:   $EXT"
        fi
    fi
fi

update-desktop-database "$APPS" 2>/dev/null || true
echo
echo "Done. Launch 'OneDrive Status' from the app grid, or run:"
echo "  $REPO/bin/onedrive-status"
echo "Start the tray now without logging out:"
echo "  $REPO/bin/onedrive-status-tray &"
