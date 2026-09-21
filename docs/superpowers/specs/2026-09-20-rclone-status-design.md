# Rclone Status — design

Generalise `onedrive-status` from a single hardcoded OneDrive mount into
`rclone-status`, a GNOME app that discovers and reports on every rclone mount
on the machine.

Status: approved 2026-09-20. Supersedes the single-mount design implicit in
commits `021eedd` and `3aef5b1`.

## Why

The app currently pins one mount through module constants in `probe.py`:
`RC_ADDR`, `UNIT`, `MOUNTPOINT`, `REMOTE`. Everything downstream assumes one
of everything. Any second rclone mount is invisible, and the rc port is
per-process, so a second mount could not share the first one's port even in
principle.

The machine this was written on has exactly one remote and one mount today, so
multi-mount support is speculative for its author. It is built anyway because
the published repo should be useful to others, and because the per-process rc
port forces the design to handle multiplicity regardless.

## What is being generalised

| Today | After |
|---|---|
| One mount from module constants | N mounts discovered from `/proc` |
| `Snapshot` | `MountSnapshot` (identity + stats) and `SystemSnapshot` (all mounts) |
| Window shows one mount | One expandable card per mount |
| Tray reflects one mount | One icon aggregating all mounts |
| `odstatus`, `onedrive-status*` | `rcstatus`, `rclone-status*` |

Out of scope: rclone `sync`/`copy`/`bisync` jobs, `rcd` daemons, job history,
and any form of mount creation or editing. This app observes mounts that
already exist.

## Architecture

```
rcstatus/discovery.py   /proc            -> list[Mount]
rcstatus/probe.py       Mount            -> MountSnapshot
rcstatus/aggregate.py   [MountSnapshot]  -> SystemSnapshot
rcstatus/service.py     tray unit lifecycle (enable/disable/state)
rcstatus/window.py      GTK4 + libadwaita, one card per mount
rcstatus/tray.py        GTK3 + AppIndicator3, one aggregate icon
```

The GTK3/GTK4 split stays: `AppIndicator3` links against GTK3 and cannot share
a process with the window's GTK4, so the tray continues to spawn the window as
a subprocess. `discovery`, `probe`, `aggregate` and `service` import no GUI
toolkit, so both processes and all tests share them.

### `discovery.py`

Produces the identity of each mount. Three `/proc` sources, all verified
working on the development machine:

1. `/proc/mounts` — lines whose type is `fuse.rclone` give the remote
   (`onedrive:`) and mountpoint. Octal escapes (`\040` for space) are decoded.
2. `/proc/<pid>/cmdline` — the rclone process whose argv contains the
   mountpoint and whose subcommand is `mount`, `mount2` or `cmount`. Its
   `--rc-addr=HOST:PORT` gives the rc endpoint; bare `--rc` implies the
   rclone default `127.0.0.1:5572`; neither means no stats are available.
3. `/proc/<pid>/cgroup` — the **last** `.service` or `.scope` path component
   is the owning systemd unit. Taking the first component is wrong: it yields
   `user@1000.service` rather than `rclone-onedrive.service`.

   The same cgroup path says whether it is a *user* unit: a path under
   `user.slice/user-<uid>.slice/user@<uid>.service` is managed by
   `systemctl --user`, anything else by the system manager. Only user units
   get a *Restart mount* action, because restarting a system unit needs
   privilege this app does not have and must not ask for. A system-owned
   mount is still discovered and reported in full; it simply offers no
   restart. `Mount` records this as `user_unit: bool`.

```python
@dataclass(frozen=True)
class Mount:
    remote: str          # "onedrive:"
    mountpoint: str      # "/home/user/OneDrive"
    pid: int | None
    rc_addr: str | None  # None when the mount has no --rc
    unit: str | None     # None when not run under a systemd unit
    user_unit: bool      # True when systemctl --user owns it
```

`discover(proc_root="/proc")` takes its root as an argument so tests run
against a fabricated tree rather than the real system.

`Mount` is frozen and hashable so the window can key its cards by mount
identity across refreshes. The key is `(remote, mountpoint)`: a restarted
mount keeps its card, since the pid changes but identity does not.

### `probe.py`

Keeps today's parsing logic, with module constants replaced by a `Mount`
argument. `Probe` becomes per-mount and holds that mount's quota cache, since
`operations/about` is a live call to the provider and must stay throttled per
remote.

`Snapshot` becomes `MountSnapshot`, gaining the `Mount` it describes and a
`stats_available: bool`. Everything else — transfers, cache, quota, health,
formatting — is unchanged.

`stats_available` and `health` answer different questions and must not be
collapsed into one another. `stats_available` says whether numbers could be
read at all; `health` says whether the mount is well. Their combinations:

| Situation | `stats_available` | `health` |
|---|---|---|
| Mounted, rc reachable, no errors | `True` | `OK` |
| Mounted, rc reachable, errors or cache full | `True` | `ERROR` |
| Mounted, no `--rc` configured | `False` | `OK` |
| Mounted, `--rc` configured but unreachable | `False` | `DEGRADED` |
| Mountpoint gone or process exited | `False` | `DOWN` |

A mount deliberately run without `--rc` is healthy, not degraded: the user
chose not to expose stats. An unreachable configured port is a fault. Keeping
these apart is what stops the tray showing a permanent warning for a
perfectly fine mount.

### `aggregate.py`

```python
@dataclass
class SystemSnapshot:
    mounts: list[MountSnapshot]
```

Derived properties: `total_transfers`, `total_speed_bps`, `any_errors`,
`worst_health`, and a `summary` line for the tray. Worst-health ordering is
`DOWN > ERROR > DEGRADED > OK`, so a single failing mount is never masked by
healthy ones.

## Refresh strategy

| Data | Interval | Why |
|---|---|---|
| Discovery | 10s | Mounts change rarely; scanning `/proc/<pid>/cmdline` for every pid is wasteful at 2s |
| Transfer and cache stats | 2s | Loopback calls, effectively free |
| Quota (`operations/about`) | 60s per mount | A live request to the provider; rate limits apply |

An earlier version also re-ran discovery immediately whenever any mount's rc
call failed, on the theory that it usually meant the mount had restarted
with a new pid or disappeared. That branch was removed in the final fix
wave (2026-09-20 final-fix-brief, F6): it could not fire without either
causing a poll storm (an unreachable-but-configured mount re-walking /proc
and re-polling every other mount on every tick) or being bounded by the
same 10s discovery floor, at which point it was dead code duplicating what
the floor already did. A 10s worst-case recovery time for a restarted mount
is acceptable.

## Mount states

Three per-mount states, all rendered rather than hidden:

- **Full stats** — mounted, rc reachable. Transfers, cache and quota as today.
- **Mounted, no stats** — either the mount has no `--rc`, or its rc port is
  unreachable. The card shows the mount, its unit state, and a one-line hint
  naming the flags to add (`--rc --rc-addr=127.0.0.1:PORT`). Distinguishing
  these two causes matters and drives different health, per the table above:
  no `--rc` is a configuration choice and stays `OK`, an unreachable
  configured port is a fault and is `DEGRADED`.
- **Gone** — a `/proc/mounts` entry whose process has exited, or a mount that
  vanished between discovery and polling. Removed from the list on the next
  discovery.

A failed poll never raises. As today, it produces a snapshot describing the
failure, because a broken mount is the state most worth displaying correctly.

## UI

### Window

One `Adw.ExpanderRow` per mount:

- Collapsed: remote, mountpoint, one-line summary, health icon.
- Expanded: transfers with progress and ETA, cache meter, quota meter —
  the current single-mount layout, unchanged.

**A single mount starts expanded**, so a one-mount machine looks and behaves
as it does today. With several mounts all start collapsed.

Per-mount actions move from the header menu into each card: *Open folder*, and
*Restart mount* shown only when a **user** systemd unit owns that mount, per
the privilege rule above. The header
keeps only app-level items. The tray toggle stays as it is.

Zero mounts renders an `Adw.StatusPage` explaining that no rclone mounts were
found, not an empty window.

Cards are keyed by `(remote, mountpoint)` and updated in place, so progress
bars animate smoothly instead of being rebuilt every 2s.

### Tray

One icon for the machine:

- Uploading if any mount is transferring, with the total file count as label.
- Error if any mount reports errors or is down.
- Otherwise idle.

The menu lists every mount with its own one-line state, then *Open Rclone
Status*, *Hide Tray Icon* and *Quit*. The single-instance `flock` and the
self-disabling hide behaviour are unchanged.

## Rename and migration

| Thing | From | To |
|---|---|---|
| Package | `odstatus` | `rcstatus` |
| Binaries | `onedrive-status`, `onedrive-status-tray` | `rclone-status`, `rclone-status-tray` |
| App id | `dev.matheoatche.OneDriveStatus` | `dev.matheoatche.RcloneStatus` |
| Tray unit | `onedrive-status-tray.service` | `rclone-status-tray.service` |
| Repo | `onedrive-status` | `rclone-status` (GitHub redirects the old URL) |

`install.sh` migrates an existing install before installing the new one:
disable and remove `onedrive-status-tray.service`, remove the old desktop
entry and any stale autostart entry, then install the renamed unit and entry
and re-enable the tray if it was enabled.

The user's own mount units, such as `rclone-onedrive.service`, are **not**
touched. They belong to the user, not the app; only the app's own tray unit is
renamed.

## Testing

`discovery.py` is tested against fabricated `/proc` trees covering:

- several mounts at once, each with a different rc port
- a mount with no `--rc` at all
- a bare `--rc` with no `--rc-addr`, which must default to `127.0.0.1:5572`
- a mount not owned by any systemd unit
- a mount owned by a *system* unit, which must be reported but offer no restart
- a `/proc/mounts` entry whose process has exited
- a mountpoint containing a space, escaped as `\040`
- a cgroup line where the naive first-match would yield `user@1000.service`
- non-mount rclone processes, such as `rclone rcd`, which must be ignored

`probe.py` keeps its existing fixture tests, parameterised by `Mount`.
`aggregate.py` gets tests for totals and for worst-health ordering, including
that one failing mount outranks several healthy ones. Launcher, service and
single-instance tests follow the rename. The synthetic fixtures stay
synthetic: no real account contents in the repo.

## Delivery

One branch, with the app working and tests passing at each commit rather than
a single large change. Rough order: discovery with tests, then probe
parameterised by `Mount`, then aggregation, then window, then tray, then the
rename and migration, then docs and the repo rename.

## Risks

- **Scanning `/proc` for every pid** is the one new per-cycle cost. Mitigated
  by discovering every 10s rather than every 2s, and by reading `cmdline`
  only for processes whose executable name is `rclone`.
- **A machine with many mounts** makes the window long. Cards are collapsed by
  default beyond the first mount, and the list scrolls.
- **The rename breaks existing links.** GitHub redirects the repository URL,
  and `install.sh` migrates a local install, but anyone who cloned by the old
  name keeps a stale remote until they update it.
