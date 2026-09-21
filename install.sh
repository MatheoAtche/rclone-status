#!/usr/bin/env bash
# Install desktop entry and tray service for Rclone Status. Safe to re-run.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart"
UNITS="$HOME/.config/systemd/user"
UNIT="rclone-status-tray.service"
EXT="appindicatorsupport@rgcjonas.gmail.com"

mkdir -p "$APPS" "$UNITS"

OLD_UNIT="onedrive-status-tray.service"
OLD_DESKTOP="$APPS/dev.matheoatche.OneDriveStatus.desktop"

# Read this BEFORE writing any unit file: a freshly written unit always
# reports "disabled", so asking afterwards cannot tell "the user turned it
# off" from "it was never installed".
if [[ -f "$UNITS/$UNIT" ]]; then
    PREV_STATE=$(systemctl --user is-enabled "$UNIT" 2>/dev/null) || true
elif [[ -f "$UNITS/$OLD_UNIT" ]]; then
    PREV_STATE=$(systemctl --user is-enabled "$OLD_UNIT" 2>/dev/null) || true
else
    PREV_STATE=absent
fi

# Migrate from the pre-rename app (onedrive-status).
if [[ -f "$UNITS/$OLD_UNIT" ]]; then
    systemctl --user disable --now "$OLD_UNIT" >/dev/null 2>&1 || true
    rm -f "$UNITS/$OLD_UNIT"
    systemctl --user daemon-reload
    echo "migrated:  removed $OLD_UNIT"
fi
rm -f "$OLD_DESKTOP" "$AUTOSTART/onedrive-status-tray.desktop"

sed "s|^Exec=.*|Exec=$REPO/bin/rclone-status|" \
    "$REPO/data/dev.matheoatche.RcloneStatus.desktop" \
    > "$APPS/dev.matheoatche.RcloneStatus.desktop"
echo "installed: app launcher"

# Earlier versions autostarted the tray from a .desktop file. Leaving it would
# start a second tray beside the systemd unit, so remove it on upgrade.
if [[ -f "$AUTOSTART/rclone-status-tray.desktop" ]]; then
    rm -f "$AUTOSTART/rclone-status-tray.desktop"
    echo "removed:   old autostart entry (superseded by $UNIT)"
fi

sed "s|^ExecStart=.*|ExecStart=$REPO/bin/rclone-status-tray|" \
    "$REPO/data/$UNIT" > "$UNITS/$UNIT"
systemctl --user daemon-reload
echo "installed: $UNIT"

if [[ "${1:-}" == "--no-tray" ]]; then
    systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
    echo "disabled:  tray (--no-tray)"
else
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
    if [[ "$PREV_STATE" == "disabled" ]]; then
        echo "note:      tray left disabled (it was disabled before)"
    else
        systemctl --user enable --now "$UNIT"
        echo "enabled:   tray (starts at login)"
    fi
fi

update-desktop-database "$APPS" 2>/dev/null || true
echo
echo "Done. Launch 'Rclone Status' from the app grid, or run:"
echo "  $REPO/bin/rclone-status"
echo "Toggle the tray from the window's Tray switch, or with:"
echo "  systemctl --user enable --now $UNIT"
echo "  systemctl --user disable --now $UNIT"
