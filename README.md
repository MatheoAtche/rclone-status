# Rclone Status

A GNOME status app for rclone mounts — a tray indicator plus a GTK4 window.
It discovers every rclone mount running on the machine by reading `/proc`;
there is no config file naming which mount to watch. Point it at a machine
with one mount, or five, and it shows a card per mount either way.

![window](docs/window.png)

The screenshot shows one card expanded (the mount with transfers in
progress) and one collapsed — that is the app's real default with more than
one mount: only a lone mount expands itself automatically.

## What it actually reports

This targets `rclone mount --vfs-cache-mode=full`, which is **not** a
two-way sync client. Files are fetched on demand and local writes queue in a
write-back cache that uploads in the background. There is no "percent of a
mirrored folder synced" to show.

What matters instead, and what this app displays, per mount:

| Signal | Source |
|---|---|
| Files currently uploading, with progress, speed and ETA | `core/stats` |
| Uploads in progress and queued, cache errors | `vfs/stats` |
| Local cache usage against `--vfs-cache-max-size` | `vfs/stats` |
| Remote space used and total | `operations/about` |
| Mount health and uptime | `systemctl` + `os.path.ismount` |

The window shows one expandable card per mount — collapsed to a one-line
summary, expanded to the full detail above. The tray shows a single icon for
the whole machine: whichever mount is in the worst state decides what the
icon and tooltip show, so a failing mount is never hidden behind healthy
ones.

Health resolves to one of four states:

- **ok** — unit active, mount present, no errors
- **error** — transfer errors, cache errors, or the cache is out of space
- **degraded** — mounted and running, but the rc API is unreachable
- **down** — the unit is dead, *or* the FUSE mount vanished while the unit
  still reports active (this happens, and it is just as unusable)

### Mounts without `--rc`

A mount is discovered from `/proc` alone, so it shows up even without the rc
API enabled — it just has nothing to report beyond "mounted". Its card shows
**Mounted · no stats (add --rc)** and its health is **ok**, not degraded: not
exposing `--rc` is a configuration choice, and it stays healthy until it
actually gives us a reason not to. Compare that to a mount whose `--rc`
*is* configured but does not answer, which is treated as **degraded** — that
port not responding is a fault, not a choice.

To get stats, add `--rc --rc-addr=127.0.0.1:PORT --rc-no-auth` to the
mount's own command line (see `data/rclone-mount.service.example`). Running
several mounts needs a distinct port per mount; discovery reads whatever
port each mount's own process was started with, so they never need to agree
on one.

## Install

```bash
./install.sh            # app launcher + tray autostart
./install.sh --no-tray  # window only
```

This adds a launcher to the app grid and an autostart entry for the tray. It
also enables the `appindicatorsupport` GNOME extension if it is installed but
off — without it GNOME provides no `StatusNotifierWatcher`, and the tray icon
silently never appears.

Start the window without logging out:

```bash
./bin/rclone-status
```

## Turning the tray on and off

The tray runs as the systemd user unit `rclone-status-tray.service`, so
"is it running" and "does it start at login" have one authoritative answer.
Toggle it from the **Tray icon** switch in the window, from **Hide Tray Icon**
in the tray's own menu, or directly:

```bash
systemctl --user enable --now rclone-status-tray.service   # on
systemctl --user disable --now rclone-status-tray.service  # off
```

The switch reads `is-enabled` and `is-active` rather than scanning the process
list, and re-reads them after every toggle instead of trusting its own state.

Only one tray may run at a time: it holds an `flock` in `$XDG_RUNTIME_DIR`, so
a second launch exits quietly rather than adding a duplicate icon. The lock is
released by the kernel when the process dies, so a crash cannot wedge it.

Uninstall: delete `~/.local/share/applications/dev.matheoatche.RcloneStatus.desktop`,
then `systemctl --user disable --now rclone-status-tray.service` and delete
`~/.config/systemd/user/rclone-status-tray.service`.

## Layout

    rcstatus/discovery.py finds every rclone mount by reading /proc. No GUI imports.
    rcstatus/probe.py     polls one mount: rc API + systemd -> MountSnapshot. No GUI imports.
    rcstatus/poller.py    MultiProbe: discovery + per-mount polling. No GUI imports.
    rcstatus/aggregate.py combines per-mount snapshots into one machine-wide SystemSnapshot.
    rcstatus/service.py   tray unit lifecycle (enable/disable/state). No GUI imports.
    rcstatus/window.py    GTK4 + libadwaita window.
    rcstatus/tray.py      GTK3 + AppIndicator3 tray daemon.

Everything above `window.py`/`tray.py` is GUI-free by design: the window and
the tray are two different, mutually incompatible GTK major versions, and
both need the same discovery and polling logic, so that logic cannot import
either of them.

**Why two processes.** `AppIndicator3` links against GTK3, and one Python
process cannot load GTK 3 and GTK 4 together — importing both raises
`ImportError: Requiring namespace 'Gtk' version '3.0', but '4.0' is already
loaded`. The tray therefore launches the window as a subprocess when asked to
open it. Both import `poller.MultiProbe`, and either runs standalone.

**Discovery and polling cadence.** Mounts are rediscovered every 10s —
walking `/proc` and matching processes to `/proc/mounts` entries is far more
expensive than a stats call, and mounts change rarely. Rediscovery also
happens sooner, but no more often than that same 10s floor, whenever a mount
stops answering: it may have restarted under a new pid or rc port, and
without the floor a merely-unreachable mount would make every tick re-walk
`/proc` and re-poll every other mount too. Transfer and cache stats
(`core/stats`, `vfs/stats`) poll every 2s per mount; they are loopback calls
and effectively free. `operations/about` (remote quota) is cached for 60s
per mount, because unlike the other two it is a live call to the storage
provider, not a local one, and polling it every 2s would risk hitting the
provider's own rate limits for no benefit.

**Restart.** The restart button only appears for a mount whose owning
systemd unit is a **user** unit (`Mount.can_restart` in `discovery.py`).
Restarting a system unit needs privilege this app does not ask for and
should not need, so a system-unit mount is shown without one.

**Failure handling.** A failed poll never raises — it returns a
`MountSnapshot` describing the failure, because "the mount is down" is the
state most worth displaying correctly.

## Migrating from onedrive-status

This project was renamed from `onedrive-status`, which watched exactly one
configured OneDrive mount. `install.sh` handles the upgrade automatically:
on first run under the new name it disables and removes the old
`onedrive-status-tray.service` unit and the old desktop launcher, then
installs the new ones. If the tray was disabled before, it stays disabled
after migrating — install.sh reads the old unit's `enabled` state before
touching anything, since a freshly written unit always reports `disabled`
and cannot otherwise be told apart from one the user turned off on purpose.

Nothing needs migrating on the mount side: any existing `rclone mount --rc`
unit is picked up by discovery as-is, whatever it is named.

## Tests

```bash
python3 -m venv --system-site-packages .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

`discovery.py` and `probe.py` are pure functions over `/proc` text and JSON,
tested against synthetic fixtures shaped like real rc API responses
mid-upload. The launcher tests start both entry points from several working
directories and through a symlink. `--system-site-packages` lets the venv
see the system PyGObject; the app itself needs no venv and no third-party
packages.

## Requirements

No third-party Python packages. On Fedora: `python3-gobject`, `gtk4`,
`libadwaita`, and `libappindicator-gtk3` (for the `AppIndicator3` typelib),
plus the AppIndicator GNOME extension for the tray. Adjust package names for
other distributions.

Screenshots and test fixtures use synthetic data, not real account contents.

## Licence

MIT — see [LICENSE](LICENSE).
