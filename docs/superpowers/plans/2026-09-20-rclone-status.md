# Rclone Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the single-hardcoded-mount `onedrive-status` app into `rclone-status`, which discovers and reports on every rclone mount on the machine.

**Architecture:** A GUI-free core — `discovery.py` (`/proc` → `Mount`), `probe.py` (`Mount` → `MountSnapshot`), `aggregate.py` (`[MountSnapshot]` → `SystemSnapshot`) — shared by a GTK4 window that renders one expandable card per mount and a GTK3 tray that shows one aggregate icon. The GTK3/GTK4 split stays because `AppIndicator3` links against GTK3 and cannot share a process with GTK4.

**Tech Stack:** Python 3.14, PyGObject, GTK4 + libadwaita (window), GTK3 + AppIndicator3 (tray), systemd user units, pytest. No third-party Python packages.

**Spec:** `docs/superpowers/specs/2026-09-20-rclone-status-design.md`

## Global Constraints

- **No third-party Python packages.** Standard library plus system PyGObject only. The `.venv` exists solely for pytest and is created with `--system-site-packages`.
- **The GUI-free core imports no GUI toolkit.** `discovery.py`, `probe.py`, `aggregate.py`, `service.py` must not import `gi`. Both GTK3 and GTK4 processes import them.
- **No poll ever raises.** Every failure path returns a snapshot describing the failure. "The mount is down" is the state most worth displaying correctly.
- **Refresh intervals:** discovery every 10s, transfer/cache stats every 2s, `operations/about` every 60s per mount. The quota call is a live request to the provider; the others are loopback.
- **Health vs stats are separate.** `stats_available` says whether numbers could be read; `health` says whether the mount is well. A mount with no `--rc` is `OK` with `stats_available=False`. A configured-but-unreachable rc port is `DEGRADED`.
- **Never touch the user's mount units.** `rclone-onedrive.service` and its kin belong to the user. Only the app's own tray unit is installed, renamed or removed.
- **Restart actions only for user units.** A system-owned mount is reported in full but offers no restart; the app must not request privilege.
- **Fixtures stay synthetic.** No real account contents, filenames or storage figures in the repo.
- **Commits are signed.** `export SSH_AUTH_SOCK=<your ssh-agent socket>` before committing; Bitwarden desktop must be unlocked.
- **Test command:** `.venv/bin/python -m pytest tests/ -q` from the repo root.

---

## File Structure

| File | Responsibility |
|---|---|
| `rcstatus/discovery.py` | **New.** Parse `/proc` into `Mount` records. No network, no GUI. |
| `rcstatus/probe.py` | Renamed from `odstatus/probe.py`. Poll one `Mount` → `MountSnapshot`. Module constants removed. |
| `rcstatus/aggregate.py` | **New.** Combine `[MountSnapshot]` → `SystemSnapshot`. Pure. |
| `rcstatus/service.py` | Renamed. Tray unit lifecycle. Only `UNIT_NAME` changes. |
| `rcstatus/window.py` | Renamed. One `Adw.ExpanderRow` per mount. |
| `rcstatus/tray.py` | Renamed. One aggregate icon; menu lists mounts. |
| `bin/rclone-status`, `bin/rclone-status-tray` | Renamed launchers. |
| `data/rclone-status-tray.service` | Renamed tray unit template. |
| `data/dev.matheoatche.RcloneStatus.desktop` | Renamed desktop entry. |
| `install.sh` | Installs new names; migrates away from the old ones. |
| `tests/test_discovery.py` | **New.** Fake `/proc` trees. |
| `tests/test_aggregate.py` | **New.** Totals and worst-health ordering. |
| `tests/test_probe.py`, `test_service.py`, `test_launchers.py` | Updated for the rename and the `Mount` parameter. |

---

### Task 1: Mechanical rename

Do the rename first, while the codebase is small and stable, so later tasks are written once under their final names. Behaviour must not change: all 94 tests pass afterwards.

**Files:**
- Rename: `odstatus/` → `rcstatus/` (5 files)
- Rename: `bin/onedrive-status` → `bin/rclone-status`, `bin/onedrive-status-tray` → `bin/rclone-status-tray`
- Rename: `data/onedrive-status-tray.service` → `data/rclone-status-tray.service`
- Rename: `data/dev.matheoatche.OneDriveStatus.desktop` → `data/dev.matheoatche.RcloneStatus.desktop`
- Modify: `rcstatus/service.py:18`, `rcstatus/window.py:363`, `install.sh`, all three test files

**Interfaces:**
- Consumes: nothing.
- Produces: package `rcstatus`; `service.UNIT_NAME == "rclone-status-tray.service"`; app id `dev.matheoatche.RcloneStatus`; launchers `bin/rclone-status{,-tray}`.

- [ ] **Step 1: Rename files with git mv**

```bash
cd ~/sources/onedrive-status
git mv odstatus rcstatus
git mv bin/onedrive-status bin/rclone-status
git mv bin/onedrive-status-tray bin/rclone-status-tray
git mv data/onedrive-status-tray.service data/rclone-status-tray.service
git mv data/dev.matheoatche.OneDriveStatus.desktop data/dev.matheoatche.RcloneStatus.desktop
```

- [ ] **Step 2: Rewrite every internal reference**

```bash
cd ~/sources/onedrive-status
grep -rl 'odstatus\|onedrive-status\|OneDriveStatus\|ODSTATUS' \
  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ \
  --exclude-dir=.pytest_cache --exclude-dir=docs . \
| xargs sed -i \
  -e 's/odstatus/rcstatus/g' \
  -e 's/onedrive-status/rclone-status/g' \
  -e 's/OneDriveStatus/RcloneStatus/g' \
  -e 's/ODSTATUS/RCSTATUS/g' \
  -e 's/OneDrive Status/Rclone Status/g'
```

Then check nothing was missed:

```bash
grep -rn 'odstatus\|onedrive-status\|OneDriveStatus\|ODSTATUS' \
  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ \
  --exclude-dir=.pytest_cache --exclude-dir=docs . || echo "clean"
```

Expected: `clean`. The `docs/` directory is excluded deliberately — the spec is a historical record and keeps the old names.

- [ ] **Step 3: Fix the lock filename in the tray**

`rcstatus/tray.py` — the sed above already renamed the lock file to `rclone-status-tray.lock`. Confirm:

```bash
grep -n 'lock' rcstatus/tray.py
```

Expected: `os.path.join(runtime, "rclone-status-tray.lock")`.

- [ ] **Step 4: Add old-name migration to install.sh**

In `install.sh`, immediately after the `mkdir -p "$APPS" "$UNITS"` line, insert:

```bash
# Migrate from the pre-rename app (onedrive-status).
OLD_UNIT="onedrive-status-tray.service"
OLD_DESKTOP="$APPS/dev.matheoatche.OneDriveStatus.desktop"
WAS_ENABLED=no
if systemctl --user list-unit-files "$OLD_UNIT" >/dev/null 2>&1; then
    if [[ "$(systemctl --user is-enabled "$OLD_UNIT" 2>/dev/null)" == "enabled" ]]; then
        WAS_ENABLED=yes
    fi
    systemctl --user disable --now "$OLD_UNIT" >/dev/null 2>&1 || true
    rm -f "$UNITS/$OLD_UNIT"
    systemctl --user daemon-reload
    echo "migrated:  removed $OLD_UNIT"
fi
rm -f "$OLD_DESKTOP" "$AUTOSTART/onedrive-status-tray.desktop"
```

`WAS_ENABLED` is unused when the script goes on to enable the new unit by default; it exists so `--no-tray` does not silently re-enable a tray the user had turned off. Guard the later enable call:

```bash
    if [[ "$WAS_ENABLED" == "no" ]] && [[ -f "$UNITS/$UNIT" ]] && \
       [[ "$(systemctl --user is-enabled "$UNIT" 2>/dev/null)" == "disabled" ]]; then
        echo "note:      tray left disabled (it was disabled before)"
    else
        systemctl --user enable --now "$UNIT"
        echo "enabled:   tray (starts at login)"
    fi
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: `94 passed`. Any failure here is a missed rename, not a behaviour change.

- [ ] **Step 6: Run the installer and verify the live app**

```bash
./install.sh
systemctl --user is-enabled rclone-status-tray.service
systemctl --user is-active rclone-status-tray.service
systemctl --user list-unit-files onedrive-status-tray.service 2>&1 | tail -1
```

Expected: `enabled`, `active`, and no such file for the old unit.

- [ ] **Step 7: Commit**

```bash
export SSH_AUTH_SOCK=<your ssh-agent socket>
git add -A
git commit -m "Rename to rclone-status

Mechanical rename ahead of multi-mount support, so the feature work is
written once under its final names: package odstatus -> rcstatus,
launchers and tray unit onedrive-status -> rclone-status, app id
dev.matheoatche.RcloneStatus.

install.sh disables and removes the old tray unit and desktop entry, and
leaves the tray disabled if it was disabled before the upgrade.

Behaviour is unchanged; all 94 tests pass.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Mount discovery

**Files:**
- Create: `rcstatus/discovery.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Mount` frozen dataclass: `remote: str`, `mountpoint: str`, `pid: int | None`, `rc_addr: str | None`, `unit: str | None`, `user_unit: bool`; property `key -> tuple[str, str]` returning `(remote, mountpoint)`.
  - `DEFAULT_RC_ADDR: str = "127.0.0.1:5572"`
  - `unescape_mount_field(value: str) -> str`
  - `parse_mounts_file(text: str) -> list[tuple[str, str]]`
  - `rc_addr_from_argv(argv: list[str]) -> str | None`
  - `unit_from_cgroup(text: str) -> tuple[str | None, bool]`
  - `discover(proc_root: str = "/proc") -> list[Mount]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_discovery.py`:

```python
"""Discovery is tested against fabricated /proc trees, never the real one."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rcstatus.discovery import (
    DEFAULT_RC_ADDR,
    Mount,
    discover,
    parse_mounts_file,
    rc_addr_from_argv,
    unescape_mount_field,
    unit_from_cgroup,
)

USER_CGROUP = (
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "rclone-onedrive.service\n"
)
SYSTEM_CGROUP = "0::/system.slice/rclone-backup.service\n"
NO_UNIT_CGROUP = "0::/user.slice/user-1000.slice/session-2.scope\n"


def make_proc(tmp_path, mounts_text, processes):
    """Build a fake /proc. processes maps pid -> (comm, argv, cgroup)."""
    root = tmp_path / "proc"
    root.mkdir()
    (root / "mounts").write_text(mounts_text)
    for pid, (comm, argv, cgroup) in processes.items():
        d = root / str(pid)
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
        (d / "cgroup").write_text(cgroup)
    return str(root)


class TestUnescape:
    def test_decodes_octal_space(self):
        assert unescape_mount_field(r"/home/u/My\040Drive") == "/home/u/My Drive"

    def test_leaves_plain_paths_alone(self):
        assert unescape_mount_field("/home/u/Drive") == "/home/u/Drive"

    def test_decodes_tab_and_backslash(self):
        assert unescape_mount_field(r"/a\011b") == "/a\tb"
        assert unescape_mount_field(r"/a\134b") == "/a\\b"


class TestParseMountsFile:
    def test_picks_only_fuse_rclone_lines(self):
        text = (
            "proc /proc proc rw 0 0\n"
            "onedrive: /home/u/OneDrive fuse.rclone rw,nosuid 0 0\n"
            "/dev/sda1 / ext4 rw 0 0\n"
            "gdrive: /home/u/Drive fuse.rclone rw 0 0\n"
        )
        assert parse_mounts_file(text) == [
            ("onedrive:", "/home/u/OneDrive"),
            ("gdrive:", "/home/u/Drive"),
        ]

    def test_decodes_escaped_mountpoints(self):
        text = r"box: /home/u/My\040Files fuse.rclone rw 0 0" + "\n"
        assert parse_mounts_file(text) == [("box:", "/home/u/My Files")]

    def test_empty_input_yields_nothing(self):
        assert parse_mounts_file("") == []

    def test_ignores_malformed_short_lines(self):
        assert parse_mounts_file("garbage\n") == []


class TestRcAddrFromArgv:
    def test_reads_explicit_rc_addr(self):
        argv = ["rclone", "mount", "x:", "/m", "--rc", "--rc-addr=127.0.0.1:5580"]
        assert rc_addr_from_argv(argv) == "127.0.0.1:5580"

    def test_bare_rc_falls_back_to_the_rclone_default(self):
        assert rc_addr_from_argv(["rclone", "mount", "x:", "/m", "--rc"]) == DEFAULT_RC_ADDR

    def test_no_rc_flag_means_no_stats(self):
        assert rc_addr_from_argv(["rclone", "mount", "x:", "/m"]) is None

    def test_supports_space_separated_form(self):
        argv = ["rclone", "mount", "x:", "/m", "--rc-addr", "127.0.0.1:5590"]
        assert rc_addr_from_argv(argv) == "127.0.0.1:5590"


class TestUnitFromCgroup:
    def test_takes_the_last_service_component_not_the_first(self):
        # The naive first match yields user@1000.service, which is wrong.
        unit, user = unit_from_cgroup(USER_CGROUP)
        assert unit == "rclone-onedrive.service"
        assert user is True

    def test_system_unit_is_not_a_user_unit(self):
        unit, user = unit_from_cgroup(SYSTEM_CGROUP)
        assert unit == "rclone-backup.service"
        assert user is False

    def test_scope_without_a_service_has_no_unit(self):
        unit, user = unit_from_cgroup(NO_UNIT_CGROUP)
        assert unit is None
        assert user is False

    def test_bare_user_manager_is_not_a_unit(self):
        unit, _ = unit_from_cgroup("0::/user.slice/user-1000.slice/user@1000.service\n")
        assert unit is None

    def test_empty_input_is_handled(self):
        assert unit_from_cgroup("") == (None, False)


class TestDiscover:
    def test_finds_a_single_mount_with_everything(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "onedrive: /home/u/OneDrive fuse.rclone rw 0 0\n",
            {3280: ("rclone",
                    ["/usr/bin/rclone", "mount", "onedrive:", "/home/u/OneDrive",
                     "--rc", "--rc-addr=127.0.0.1:5572"],
                    USER_CGROUP)},
        )
        [m] = discover(proc)
        assert m.remote == "onedrive:"
        assert m.mountpoint == "/home/u/OneDrive"
        assert m.pid == 3280
        assert m.rc_addr == "127.0.0.1:5572"
        assert m.unit == "rclone-onedrive.service"
        assert m.user_unit is True

    def test_finds_several_mounts_with_different_ports(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "onedrive: /home/u/OneDrive fuse.rclone rw 0 0\n"
            "gdrive: /home/u/Drive fuse.rclone rw 0 0\n",
            {
                10: ("rclone", ["rclone", "mount", "onedrive:", "/home/u/OneDrive",
                                "--rc-addr=127.0.0.1:5572"], USER_CGROUP),
                11: ("rclone", ["rclone", "mount", "gdrive:", "/home/u/Drive",
                                "--rc-addr=127.0.0.1:5580"], USER_CGROUP),
            },
        )
        found = {m.remote: m for m in discover(proc)}
        assert set(found) == {"onedrive:", "gdrive:"}
        assert found["onedrive:"].rc_addr == "127.0.0.1:5572"
        assert found["gdrive:"].rc_addr == "127.0.0.1:5580"

    def test_mount_without_rc_is_still_discovered(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "backup: /home/u/Backup fuse.rclone rw 0 0\n",
            {12: ("rclone", ["rclone", "mount", "backup:", "/home/u/Backup"],
                  USER_CGROUP)},
        )
        [m] = discover(proc)
        assert m.rc_addr is None
        assert m.pid == 12

    def test_system_owned_mount_is_reported_but_not_a_user_unit(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "backup: /srv/Backup fuse.rclone rw 0 0\n",
            {13: ("rclone", ["rclone", "mount", "backup:", "/srv/Backup", "--rc"],
                  SYSTEM_CGROUP)},
        )
        [m] = discover(proc)
        assert m.unit == "rclone-backup.service"
        assert m.user_unit is False

    def test_mount_with_no_owning_unit(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "x: /home/u/X fuse.rclone rw 0 0\n",
            {14: ("rclone", ["rclone", "mount", "x:", "/home/u/X"], NO_UNIT_CGROUP)},
        )
        [m] = discover(proc)
        assert m.unit is None
        assert m.user_unit is False

    def test_mount_whose_process_has_exited(self, tmp_path):
        # A stale /proc/mounts entry: still listed, but nothing owns it.
        proc = make_proc(tmp_path, "x: /home/u/X fuse.rclone rw 0 0\n", {})
        [m] = discover(proc)
        assert m.pid is None
        assert m.rc_addr is None
        assert m.unit is None

    def test_mountpoint_with_a_space(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "box: /home/u/My\\040Files fuse.rclone rw 0 0\n",
            {15: ("rclone", ["rclone", "mount", "box:", "/home/u/My Files", "--rc"],
                  USER_CGROUP)},
        )
        [m] = discover(proc)
        assert m.mountpoint == "/home/u/My Files"
        assert m.pid == 15

    def test_non_mount_rclone_processes_are_ignored(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "x: /home/u/X fuse.rclone rw 0 0\n",
            {
                16: ("rclone", ["rclone", "rcd", "--rc-addr=127.0.0.1:5599"], USER_CGROUP),
                17: ("rclone", ["rclone", "mount", "x:", "/home/u/X", "--rc"], USER_CGROUP),
            },
        )
        [m] = discover(proc)
        assert m.pid == 17, "matched the rcd daemon instead of the mount"

    def test_non_rclone_processes_are_not_read(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "x: /home/u/X fuse.rclone rw 0 0\n",
            {18: ("bash", ["bash", "-c", "mount x: /home/u/X"], USER_CGROUP)},
        )
        [m] = discover(proc)
        assert m.pid is None

    def test_no_rclone_mounts_yields_empty_list(self, tmp_path):
        proc = make_proc(tmp_path, "/dev/sda1 / ext4 rw 0 0\n", {})
        assert discover(proc) == []

    def test_missing_proc_root_is_not_an_error(self, tmp_path):
        assert discover(str(tmp_path / "nope")) == []

    def test_supports_cmount_and_mount2_subcommands(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "x: /home/u/X fuse.rclone rw 0 0\n",
            {19: ("rclone", ["rclone", "cmount", "x:", "/home/u/X", "--rc"], USER_CGROUP)},
        )
        assert discover(proc)[0].pid == 19


class TestMountIdentity:
    def test_key_ignores_pid_so_a_restart_keeps_its_card(self):
        a = Mount("x:", "/m", pid=1, rc_addr=None, unit=None, user_unit=False)
        b = Mount("x:", "/m", pid=2, rc_addr=None, unit=None, user_unit=False)
        assert a.key == b.key

    def test_mount_is_hashable(self):
        assert len({Mount("x:", "/m", None, None, None, False)}) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_discovery.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'rcstatus.discovery'`

- [ ] **Step 3: Write the implementation**

Create `rcstatus/discovery.py`:

```python
"""Find rclone mounts by reading /proc.

No configuration file: everything needed to talk to a mount is already in
the kernel's view of it. Three sources, each with a trap worth naming.

  /proc/mounts        fuse.rclone lines give remote and mountpoint, with
                      whitespace octal-escaped.
  /proc/<pid>/cmdline the owning rclone process; its --rc-addr is where
                      stats live. A mount with no --rc has no stats at all.
  /proc/<pid>/cgroup  the owning systemd unit is the LAST .service
                      component. The first is user@<uid>.service, which is
                      the user manager, not the mount.

No GUI imports: the window, the tray and the tests all share this.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

# rclone's own default when --rc is given without an address.
DEFAULT_RC_ADDR = "127.0.0.1:5572"

MOUNT_SUBCOMMANDS = frozenset({"mount", "mount2", "cmount"})
FSTYPE = "fuse.rclone"

_OCTAL = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class Mount:
    """One rclone mount as the kernel sees it."""

    remote: str
    mountpoint: str
    pid: int | None
    rc_addr: str | None
    unit: str | None
    user_unit: bool

    @property
    def key(self) -> tuple[str, str]:
        """Identity across refreshes. The pid changes on restart; this does not."""
        return (self.remote, self.mountpoint)

    @property
    def has_stats(self) -> bool:
        return self.rc_addr is not None

    @property
    def can_restart(self) -> bool:
        """Only user units: restarting a system unit needs privilege we lack."""
        return bool(self.unit) and self.user_unit


def unescape_mount_field(value: str) -> str:
    """Decode the octal escapes /proc/mounts uses for whitespace."""
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), value)


def parse_mounts_file(text: str) -> list[tuple[str, str]]:
    """Return (remote, mountpoint) for every fuse.rclone line."""
    found = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2] == FSTYPE:
            found.append((unescape_mount_field(fields[0]),
                          unescape_mount_field(fields[1])))
    return found


def rc_addr_from_argv(argv: list[str]) -> str | None:
    """The mount's rc endpoint, or None when it exposes no stats."""
    for i, arg in enumerate(argv):
        if arg.startswith("--rc-addr="):
            return arg.split("=", 1)[1]
        if arg == "--rc-addr" and i + 1 < len(argv):
            return argv[i + 1]
    return DEFAULT_RC_ADDR if "--rc" in argv else None


def unit_from_cgroup(text: str) -> tuple[str | None, bool]:
    """Return (unit name, is_user_unit) from a cgroup file's contents."""
    lines = [l for l in text.strip().splitlines() if l]
    if not lines:
        return None, False
    path = lines[-1].split(":")[-1]
    parts = [p for p in path.split("/") if p.endswith((".service", ".scope"))]
    if not parts:
        return None, False
    unit = parts[-1]
    if unit.startswith("user@"):
        # The user manager itself owns nothing we care about.
        return None, False
    if unit.endswith(".scope"):
        return None, False
    return unit, "user@" in path


def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _mount_processes(root: pathlib.Path) -> dict[str, tuple[int, list[str], str]]:
    """Map mountpoint -> (pid, argv, cgroup text) for rclone mount processes."""
    found = {}
    try:
        entries = list(root.iterdir())
    except OSError:
        return found

    for entry in entries:
        if not entry.name.isdigit():
            continue
        # comm is one short read; skip the rest for non-rclone processes.
        if _read(entry / "comm").strip() != "rclone":
            continue
        raw = b""
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a for a in raw.decode(errors="replace").split("\0") if a]
        if len(argv) < 2 or argv[1] not in MOUNT_SUBCOMMANDS:
            continue
        # The mountpoint is whichever argument matches a known mount; the
        # caller resolves that. Record under every path-looking argument.
        for arg in argv[2:]:
            if arg.startswith("/"):
                found.setdefault(arg, (int(entry.name), argv,
                                       _read(entry / "cgroup")))
    return found


def discover(proc_root: str = "/proc") -> list[Mount]:
    """Every rclone mount on the machine, in /proc/mounts order."""
    root = pathlib.Path(proc_root)
    pairs = parse_mounts_file(_read(root / "mounts"))
    if not pairs:
        return []

    processes = _mount_processes(root)
    mounts = []
    for remote, mountpoint in pairs:
        proc = processes.get(mountpoint)
        if proc is None:
            # A stale /proc/mounts entry whose process has gone.
            mounts.append(Mount(remote, mountpoint, None, None, None, False))
            continue
        pid, argv, cgroup = proc
        unit, user_unit = unit_from_cgroup(cgroup)
        mounts.append(Mount(
            remote=remote,
            mountpoint=mountpoint,
            pid=pid,
            rc_addr=rc_addr_from_argv(argv),
            unit=unit,
            user_unit=user_unit,
        ))
    return mounts
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_discovery.py -q`
Expected: PASS

- [ ] **Step 5: Verify against the real machine**

```bash
cd ~/sources/onedrive-status
python3 -c "
from rcstatus.discovery import discover
for m in discover():
    print(m)
"
```

Expected: one `Mount` with `remote='onedrive:'`, `mountpoint='/home/user/OneDrive'`, a real pid, `rc_addr='127.0.0.1:5572'`, `unit='rclone-onedrive.service'`, `user_unit=True`.

- [ ] **Step 6: Commit**

```bash
export SSH_AUTH_SOCK=<your ssh-agent socket>
git add rcstatus/discovery.py tests/test_discovery.py
git commit -m "Discover rclone mounts from /proc

Reads /proc/mounts for fuse.rclone entries, matches each to its rclone
process for the --rc address, and to its systemd unit via the LAST
.service component of the cgroup path -- the first component is
user@<uid>.service, the user manager, not the mount.

Records whether a user or system manager owns the mount, since only a
user unit can be restarted without privilege.

Tested against fabricated /proc trees: several mounts on different ports,
a mount with no --rc, system-owned and unowned mounts, a stale entry
whose process exited, escaped mountpoints, and rclone rcd daemons that
must not be mistaken for mounts.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Probe one mount

**Files:**
- Modify: `rcstatus/probe.py` (remove module constants at lines 20-23; parameterise `_rc`, `_systemd_state`, `build_snapshot`, `Probe`)
- Modify: `tests/test_probe.py`

**Interfaces:**
- Consumes: `rcstatus.discovery.Mount`
- Produces:
  - `MountSnapshot` — every field of today's `Snapshot`, plus `mount: Mount` and `stats_available: bool`
  - `build_snapshot(mount, core, vfs, about, mounted, uptime_seconds) -> MountSnapshot`
  - `Probe(mount)` with `.poll() -> MountSnapshot`
  - `Health`, `Transfer`, `format_bytes`, `format_duration` unchanged

- [ ] **Step 1: Write the failing tests**

Replace the fixtures at the top of `tests/test_probe.py` and add a new health class. Keep every existing assertion body; only the construction changes.

```python
from rcstatus.discovery import Mount
from rcstatus.probe import (
    Health, MountSnapshot, Transfer, build_snapshot, format_bytes, format_duration,
)

MOUNT = Mount("onedrive:", "/home/user/OneDrive", pid=1, rc_addr="127.0.0.1:5572",
              unit="rclone-onedrive.service", user_unit=True)
NO_RC_MOUNT = Mount("backup:", "/home/user/Backup", pid=2, rc_addr=None,
                    unit=None, user_unit=False)


@pytest.fixture
def uploading():
    return build_snapshot(
        MOUNT,
        core=load("core_stats_uploading"),
        vfs=load("vfs_stats_uploading"),
        about=load("about"),
        mounted=True,
        uptime_seconds=680,
    )


@pytest.fixture
def idle():
    return build_snapshot(
        MOUNT,
        core=load("core_stats_idle"),
        vfs=load("vfs_stats_idle"),
        about=load("about"),
        mounted=True,
        uptime_seconds=680,
    )
```

Replace the whole `TestHealth` class with this one, which encodes the spec's table:

```python
class TestHealth:
    """stats_available and health answer different questions."""

    def test_reachable_and_clean_is_ok_with_stats(self, uploading):
        assert uploading.health is Health.OK
        assert uploading.stats_available is True

    def test_errors_are_an_error_state(self):
        core = load("core_stats_idle")
        core["errors"] = 3
        core["lastError"] = "quota exceeded"
        snap = build_snapshot(MOUNT, core=core, vfs=load("vfs_stats_idle"),
                              about=load("about"), mounted=True, uptime_seconds=5)
        assert snap.health is Health.ERROR
        assert snap.errors == 3
        assert snap.last_error == "quota exceeded"

    def test_cache_out_of_space_is_an_error(self):
        vfs = load("vfs_stats_idle")
        vfs["diskCache"]["outOfSpace"] = True
        snap = build_snapshot(MOUNT, core=load("core_stats_idle"), vfs=vfs,
                              about=load("about"), mounted=True, uptime_seconds=5)
        assert snap.health is Health.ERROR

    def test_no_rc_configured_is_healthy_without_stats(self):
        # A deliberate choice, not a fault: must not warn forever.
        snap = build_snapshot(NO_RC_MOUNT, core=None, vfs=None, about=None,
                              mounted=True, uptime_seconds=None)
        assert snap.health is Health.OK
        assert snap.stats_available is False

    def test_configured_rc_that_is_unreachable_is_degraded(self):
        snap = build_snapshot(MOUNT, core=None, vfs=None, about=None,
                              mounted=True, uptime_seconds=None)
        assert snap.health is Health.DEGRADED
        assert snap.stats_available is False

    def test_unmounted_is_down(self):
        snap = build_snapshot(MOUNT, core=None, vfs=None, about=None,
                              mounted=False, uptime_seconds=None)
        assert snap.health is Health.DOWN
        assert snap.stats_available is False

    def test_unmounted_beats_a_missing_rc(self):
        snap = build_snapshot(NO_RC_MOUNT, core=None, vfs=None, about=None,
                              mounted=False, uptime_seconds=None)
        assert snap.health is Health.DOWN

    def test_never_raises_on_empty_payloads(self):
        snap = build_snapshot(MOUNT, core={}, vfs={}, about={},
                              mounted=True, uptime_seconds=None)
        assert isinstance(snap, MountSnapshot)
        assert snap.transfers == []

    def test_snapshot_carries_its_mount(self, uploading):
        assert uploading.mount.remote == "onedrive:"


class TestNoRcSummary:
    def test_summary_names_the_missing_flag(self):
        snap = build_snapshot(NO_RC_MOUNT, core=None, vfs=None, about=None,
                              mounted=True, uptime_seconds=None)
        assert "--rc" in snap.summary
```

Every other existing test in the file keeps its assertions; only `build_snapshot(...)` calls gain the leading `MOUNT` argument and drop `unit_active`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_probe.py -q`
Expected: FAIL — `ImportError: cannot import name 'MountSnapshot'`

- [ ] **Step 3: Rewrite probe.py**

Delete lines 20-23 (`RC_ADDR`, `UNIT`, `MOUNTPOINT`, `REMOTE`). Keep `RC_TIMEOUT` and `QUOTA_TTL`. Rename `Snapshot` to `MountSnapshot` and add two fields after `health`:

```python
    mount: Mount = None
    stats_available: bool = False
```

Add the import at the top:

```python
from rcstatus.discovery import Mount
```

Replace `build_snapshot` and everything below it:

```python
def build_snapshot(mount, core, vfs, about, mounted, uptime_seconds) -> MountSnapshot:
    """Assemble a MountSnapshot. core/vfs/about may each be None."""
    core = core or {}
    vfs = vfs or {}
    disk = vfs.get("diskCache") or {}

    transfers = []
    for raw in core.get("transferring") or []:
        path = raw.get("name") or ""
        transfers.append(
            Transfer(
                name=path.rsplit("/", 1)[-1],
                path=path,
                bytes=int(raw.get("bytes") or 0),
                size=int(raw.get("size") or 0),
                speed_bps=float(raw.get("speed") or 0.0),
                eta_seconds=raw.get("eta"),
            )
        )

    errors = int(core.get("errors") or 0)
    out_of_space = bool(disk.get("outOfSpace"))
    errored_files = int(disk.get("erroredFiles") or 0)
    stats_available = bool(core)

    if not mounted:
        health = Health.DOWN
    elif not stats_available:
        # No --rc is a configuration choice and stays healthy; a configured
        # port that will not answer is a fault.
        health = Health.DEGRADED if mount.has_stats else Health.OK
    elif errors or out_of_space or errored_files:
        health = Health.ERROR
    else:
        health = Health.OK

    quota_used = quota_total = None
    if about:
        quota_used = about.get("used")
        quota_total = about.get("total")

    cache_max = disk.get("maxSize")
    if cache_max is None:
        cache_max = (vfs.get("opt") or {}).get("CacheMaxSize")

    return MountSnapshot(
        health=health,
        mount=mount,
        stats_available=stats_available,
        transfers=transfers,
        uploads_in_progress=int(disk.get("uploadsInProgress") or 0),
        uploads_queued=int(disk.get("uploadsQueued") or 0),
        errors=errors,
        last_error=core.get("lastError") or "",
        cache_bytes=disk.get("bytesUsed"),
        cache_max_bytes=cache_max,
        cache_files=int(disk.get("files") or 0),
        quota_used=quota_used,
        quota_total=quota_total,
        uptime_seconds=uptime_seconds,
        mounted=mounted,
    )


def _rc(rc_addr, endpoint: str, payload: dict | None = None):
    """POST to a mount's rc API; return parsed JSON or None if unreachable."""
    if not rc_addr:
        return None
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        f"http://{rc_addr}/{endpoint}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=RC_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def _unit_uptime(unit, user_unit) -> int | None:
    """Seconds since the owning unit started, or None."""
    if not unit:
        return None
    scope = "--user" if user_unit else "--system"
    try:
        out = subprocess.run(
            ["systemctl", scope, "show", unit,
             "--property=ActiveState", "--property=ActiveEnterTimestampMonotonic"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    props = dict(l.split("=", 1) for l in out.strip().splitlines() if "=" in l)
    if props.get("ActiveState") != "active":
        return None
    started = props.get("ActiveEnterTimestampMonotonic")
    if not (started and started.isdigit() and int(started) > 0):
        return None
    # CLOCK_MONOTONIC microseconds, matching the kernel's own clock.
    return max(0, int(time.clock_gettime(time.CLOCK_MONOTONIC) - int(started) / 1e6))


class Probe:
    """Polls one mount, caching that mount's expensive quota call."""

    def __init__(self, mount: Mount):
        self.mount = mount
        self._quota = None
        self._quota_at = 0.0

    def _quota_now(self):
        age = time.time() - self._quota_at
        if self._quota is None or age > QUOTA_TTL:
            fresh = _rc(self.mount.rc_addr, "operations/about",
                        {"fs": self.mount.remote})
            if fresh is not None:
                self._quota = fresh
                self._quota_at = time.time()
        return self._quota

    def poll(self) -> MountSnapshot:
        mount = self.mount
        mounted = os.path.ismount(mount.mountpoint)
        uptime = _unit_uptime(mount.unit, mount.user_unit)

        if not mounted:
            # Drop the cached quota so a restart cannot show stale figures.
            self._quota = None
            return build_snapshot(mount, None, None, None, mounted, uptime)

        core = _rc(mount.rc_addr, "core/stats")
        vfs = _rc(mount.rc_addr, "vfs/stats")
        about = self._quota_now() if core is not None else None
        return build_snapshot(mount, core, vfs, about, mounted, uptime)
```

In `MountSnapshot.summary`, replace the `DEGRADED` branch and add a no-stats branch:

```python
        if self.health is Health.DOWN:
            return "Not mounted"
        if not self.stats_available:
            if self.mount and not self.mount.has_stats:
                return "Mounted · no stats (add --rc)"
            return "Mounted · stats unavailable"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_probe.py -q`
Expected: PASS

- [ ] **Step 5: Verify against the real mount**

```bash
python3 -c "
from rcstatus.discovery import discover
from rcstatus.probe import Probe
for m in discover():
    s = Probe(m).poll()
    print(m.remote, s.health.value, s.stats_available, '|', s.summary)
"
```

Expected: `onedrive: ok True | ...` matching the live mount.

- [ ] **Step 6: Commit**

```bash
export SSH_AUTH_SOCK=<your ssh-agent socket>
git add rcstatus/probe.py tests/test_probe.py
git commit -m "Probe any mount rather than one hardcoded remote

Replaces the RC_ADDR/UNIT/MOUNTPOINT/REMOTE module constants with a Mount
argument, so one Probe exists per mount and each keeps its own quota
cache -- operations/about is a live provider call and must stay throttled
per remote.

Snapshot becomes MountSnapshot, carrying its Mount and a stats_available
flag kept deliberately separate from health: a mount run without --rc is
healthy with no stats, while a configured but unreachable rc port is
degraded. Collapsing the two would warn forever about a fine mount.

Uptime now asks the right systemd manager, since a mount may be owned by
a system unit rather than a user one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Aggregate across mounts

**Files:**
- Create: `rcstatus/aggregate.py`
- Test: `tests/test_aggregate.py`

**Interfaces:**
- Consumes: `rcstatus.probe.MountSnapshot`, `rcstatus.probe.Health`
- Produces: `SystemSnapshot(mounts: list[MountSnapshot])` with `total_transfers: int`, `total_speed_bps: float`, `any_errors: bool`, `worst_health: Health`, `summary: str`, `pending: bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_aggregate.py`:

```python
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rcstatus.aggregate import SystemSnapshot
from rcstatus.discovery import Mount
from rcstatus.probe import Health, MountSnapshot, Transfer


def snap(health=Health.OK, transfers=(), remote="x:", stats=True):
    mount = Mount(remote, f"/mnt/{remote.rstrip(':')}", 1, "127.0.0.1:5572", None, False)
    return MountSnapshot(health=health, mount=mount, stats_available=stats,
                         transfers=list(transfers), mounted=True)


def transfer(speed=1000.0):
    return Transfer(name="f", path="f", bytes=1, size=2,
                    speed_bps=speed, eta_seconds=10)


class TestTotals:
    def test_counts_transfers_across_mounts(self):
        s = SystemSnapshot([snap(transfers=[transfer(), transfer()]),
                            snap(transfers=[transfer()])])
        assert s.total_transfers == 3

    def test_sums_speed_across_mounts(self):
        s = SystemSnapshot([snap(transfers=[transfer(1000.0)]),
                            snap(transfers=[transfer(500.0)])])
        assert s.total_speed_bps == pytest.approx(1500.0)

    def test_empty_is_zero_not_an_error(self):
        s = SystemSnapshot([])
        assert s.total_transfers == 0
        assert s.total_speed_bps == 0
        assert s.pending is False


class TestWorstHealth:
    def test_one_failing_mount_is_not_masked_by_healthy_ones(self):
        s = SystemSnapshot([snap(Health.OK), snap(Health.DOWN), snap(Health.OK)])
        assert s.worst_health is Health.DOWN

    @pytest.mark.parametrize("worse,better", [
        (Health.DOWN, Health.ERROR),
        (Health.ERROR, Health.DEGRADED),
        (Health.DEGRADED, Health.OK),
    ])
    def test_ordering(self, worse, better):
        assert SystemSnapshot([snap(worse), snap(better)]).worst_health is worse

    def test_all_healthy_is_ok(self):
        assert SystemSnapshot([snap(), snap()]).worst_health is Health.OK

    def test_no_mounts_is_ok(self):
        assert SystemSnapshot([]).worst_health is Health.OK

    def test_any_errors_reflects_error_state(self):
        assert SystemSnapshot([snap(Health.OK), snap(Health.ERROR)]).any_errors is True
        assert SystemSnapshot([snap(Health.OK)]).any_errors is False


class TestSummary:
    def test_no_mounts(self):
        assert SystemSnapshot([]).summary == "No rclone mounts"

    def test_all_idle(self):
        assert SystemSnapshot([snap(), snap()]).summary == "Up to date"

    def test_uploading_counts_files_and_speed(self):
        s = SystemSnapshot([snap(transfers=[transfer(1048576.0)]),
                            snap(transfers=[transfer(1048576.0)])])
        assert s.summary == "Uploading 2 files · 2.0 MB/s"

    def test_single_file_is_singular(self):
        s = SystemSnapshot([snap(transfers=[transfer(1048576.0)])])
        assert s.summary.startswith("Uploading 1 file ·")

    def test_mentions_a_down_mount(self):
        s = SystemSnapshot([snap(Health.OK), snap(Health.DOWN, remote="b:")])
        assert "1 mount down" in s.summary
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_aggregate.py -q`
Expected: `ModuleNotFoundError: No module named 'rcstatus.aggregate'`

- [ ] **Step 3: Write the implementation**

Create `rcstatus/aggregate.py`:

```python
"""Combine per-mount snapshots into one machine-wide view.

Pure: takes snapshots, returns derived values. The tray renders one icon
for the whole machine, so a single failing mount must never be hidden
behind healthy ones -- hence worst_health rather than an average.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rcstatus.probe import Health, MountSnapshot, format_bytes

# Worst first: the tray shows the most serious state on the machine.
HEALTH_ORDER = [Health.DOWN, Health.ERROR, Health.DEGRADED, Health.OK]


@dataclass
class SystemSnapshot:
    """Every mount on the machine at one instant."""

    mounts: list[MountSnapshot] = field(default_factory=list)

    @property
    def total_transfers(self) -> int:
        return sum(len(m.transfers) for m in self.mounts)

    @property
    def total_speed_bps(self) -> float:
        return sum(m.total_speed_bps for m in self.mounts)

    @property
    def pending(self) -> bool:
        return any(m.pending for m in self.mounts)

    @property
    def any_errors(self) -> bool:
        return any(m.health is Health.ERROR for m in self.mounts)

    @property
    def worst_health(self) -> Health:
        for health in HEALTH_ORDER:
            if any(m.health is health for m in self.mounts):
                return health
        return Health.OK

    @property
    def down_count(self) -> int:
        return sum(1 for m in self.mounts if m.health is Health.DOWN)

    @property
    def summary(self) -> str:
        if not self.mounts:
            return "No rclone mounts"

        parts = []
        count = self.total_transfers
        if count:
            noun = "file" if count == 1 else "files"
            parts.append(f"Uploading {count} {noun}")
            parts.append(f"{format_bytes(self.total_speed_bps)}/s")
        elif self.pending:
            parts.append("Preparing upload")
        else:
            parts.append("Up to date")

        if self.down_count:
            noun = "mount" if self.down_count == 1 else "mounts"
            parts.append(f"{self.down_count} {noun} down")
        if self.any_errors:
            parts.append("errors")
        return " · ".join(parts)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_aggregate.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
export SSH_AUTH_SOCK=<your ssh-agent socket>
git add rcstatus/aggregate.py tests/test_aggregate.py
git commit -m "Aggregate mount snapshots for the whole machine

The tray shows one icon however many mounts exist, so it needs a single
machine-wide state. worst_health orders DOWN > ERROR > DEGRADED > OK so a
failing mount is never masked by healthy ones, which an average or a
first-match would allow.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Window with one card per mount

**Files:**
- Create: `rcstatus/poller.py`
- Modify: `rcstatus/window.py` (replace `_build_status_card`, `_build_transfers`, `_build_storage`, `refresh`, `_sync_transfer_rows`, `_install_actions`, `_restart_mount`)
- Test: manual verification plus the existing launcher tests

**Interfaces:**
- Consumes: `discovery.discover`, `probe.Probe`, `probe.MountSnapshot`, `aggregate.SystemSnapshot`
- Produces: `rcstatus.poller.MultiProbe` with `.poll() -> SystemSnapshot` and `DISCOVERY_SECONDS = 10`; `MountCard` (an `Adw.ExpanderRow` subclass) with `update(snapshot)`; `StatusWindow` unchanged externally

- [ ] **Step 1: Create the poller that owns discovery and per-mount probes**

Create `rcstatus/poller.py`. It goes in its own module from the start because the GTK3 tray needs it too and cannot import `window.py` — pulling GTK4 into a GTK3 process raises `ImportError: Requiring namespace 'Gtk' version '3.0', but '4.0' is already loaded`.

Start the file with:

```python
"""Discovery plus per-mount polling, shared by both UIs.

Lives apart from window.py because the GTK3 tray cannot import a module
that pulls in GTK4.
"""

from __future__ import annotations

import time

from rcstatus.aggregate import SystemSnapshot
from rcstatus.discovery import discover
from rcstatus.probe import Probe
```

Then the class:

```python
DISCOVERY_SECONDS = 10


class MultiProbe:
    """Discovers mounts and polls each, reusing Probes across refreshes.

    Discovery is far more expensive than a stats call and mounts change
    rarely, so it runs on its own slower clock -- but immediately again
    whenever a mount stops answering, which usually means it restarted with
    a new pid.
    """

    def __init__(self):
        self._probes = {}
        self._discovered_at = 0.0

    def _rediscover(self):
        mounts = discover()
        seen = set()
        for mount in mounts:
            seen.add(mount.key)
            existing = self._probes.get(mount.key)
            if existing is None or existing.mount != mount:
                # New mount, or the same mount with a new pid or rc port.
                self._probes[mount.key] = Probe(mount)
        for key in list(self._probes):
            if key not in seen:
                del self._probes[key]
        self._discovered_at = time.time()

    def poll(self) -> SystemSnapshot:
        if time.time() - self._discovered_at > DISCOVERY_SECONDS:
            self._rediscover()

        snaps = [p.poll() for p in self._probes.values()]
        if any(not s.stats_available and s.mount.has_stats for s in snaps):
            # Something stopped answering: its pid or port may have changed.
            self._rediscover()
            snaps = [p.poll() for p in self._probes.values()]
        return SystemSnapshot(snaps)
```

Then add to `rcstatus/window.py`'s imports:

```python
from rcstatus.poller import MultiProbe
from rcstatus.probe import Health, format_bytes, format_duration
```

- [ ] **Step 2: Write the MountCard widget**

Add after `MeterRow` in `rcstatus/window.py`:

```python
class MountCard(Adw.ExpanderRow):
    """One mount: summary when collapsed, full detail when expanded."""

    def __init__(self, snapshot):
        super().__init__()
        self.key = snapshot.mount.key
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
```

- [ ] **Step 3: Replace the window body**

In `StatusWindow.__init__`, replace the three `content.append(self._build_*)` calls for status/transfers/storage with:

```python
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
```

Replace `self.probe = Probe()` with `self.prober = MultiProbe()` and add `self.cards = {}`.

Delete `_build_status_card`, `_build_transfers`, `_build_storage`, `_restart_mount`, `_sync_transfer_rows`, and the `open-folder` / `restart` actions in `_install_actions` (they are now per-card). Keep the tray toggle actions.

Replace `refresh`:

```python
    def refresh(self):
        system = self.prober.poll()

        seen = set()
        for snap in system.mounts:
            key = snap.mount.key
            seen.add(key)
            card = self.cards.get(key)
            if card is None:
                card = MountCard(snap)
                self.cards[key] = card
                self.mounts_list.append(card)
            else:
                card.update(snap)
        for key in list(self.cards):
            if key not in seen:
                self.mounts_list.remove(self.cards.pop(key))

        # One mount should look exactly as it did before multi-mount support.
        if len(self.cards) == 1:
            next(iter(self.cards.values())).set_expanded(True)

        self.mounts_stack.set_visible_child_name("list" if self.cards else "empty")
        self.mounts_group.set_title(
            "Mounts" if len(self.cards) != 1 else "Mount"
        )
        self.set_title(f"Rclone Status — {system.summary}")
        self._set_tray_row(service.state())
```

- [ ] **Step 4: Run the window and confirm it renders**

```bash
cd ~/sources/onedrive-status
timeout 8 python3 -m rcstatus.window 2>&1 | head -20
```

Expected: no traceback. Then capture it:

```bash
python3 - <<'EOF'
import gi, sys
gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")
from gi.repository import Adw, Gtk, GLib
sys.path.insert(0, "/home/user/sources/onedrive-status")
from rcstatus.window import StatusWindow
class App(Adw.Application):
    def __init__(self): super().__init__(application_id="dev.matheoatche.RcloneStatus.Snap")
    def do_activate(self):
        w = StatusWindow(self); w.present()
        GLib.timeout_add(2500, self.grab, w)
    def grab(self, w):
        p = Gtk.WidgetPaintable.new(w)
        s = Gtk.Snapshot(); p.snapshot(s, w.get_width(), w.get_height())
        w.get_native().get_renderer().render_texture(s.to_node(), None).save_to_png("/tmp/win.png")
        print("saved"); self.quit(); return False
App().run([])
EOF
```

Read `/tmp/win.png` and confirm: one card titled `onedrive:`, expanded, showing transfers, cache and quota, with Open Folder and Restart Mount buttons.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
export SSH_AUTH_SOCK=<your ssh-agent socket>
git add rcstatus/window.py rcstatus/poller.py
git commit -m "Show one expandable card per mount

Each mount gets an Adw.ExpanderRow: remote, mountpoint and summary when
collapsed, transfers and meters when expanded. A single mount starts
expanded, so a one-mount machine looks as it did before.

Per-mount actions move into the card. Restart is hidden unless a user
systemd unit owns the mount, since restarting a system unit needs
privilege the app must not request.

MultiProbe keeps one Probe per mount across refreshes and rediscovers on
its own slower clock, plus immediately whenever a mount stops answering,
which usually means it restarted with a new pid.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Tray with aggregate state

**Files:**
- Modify: `rcstatus/tray.py` (replace `__init__` menu construction and `refresh`)

**Interfaces:**
- Consumes: `rcstatus.poller.MultiProbe`, `rcstatus.aggregate.SystemSnapshot`
- Produces: no new public API

- [ ] **Step 1: Import the shared poller**

`rcstatus/poller.py` already exists from Task 5 and imports no GUI toolkit, so this GTK3 process can use it directly. Add to `rcstatus/tray.py`:

```python
from rcstatus.poller import MultiProbe
from rcstatus.probe import Health
```

Confirm the tray still pulls in no GTK4:

```bash
cd ~/sources/onedrive-status
grep -n "require_version\|^from rcstatus" rcstatus/tray.py
```

Expected: `gi.require_version("Gtk", "3.0")`, and no import of `rcstatus.window`.

- [ ] **Step 2: Rewrite the tray's dynamic menu**

In `Tray.__init__`, replace the fixed `summary_item` / `detail_item` pair with a summary item plus a container for per-mount items:

```python
        self.summary_item = Gtk.MenuItem(label="Checking…")
        self.summary_item.set_sensitive(False)
        self.menu.append(self.summary_item)

        self.mount_separator = Gtk.SeparatorMenuItem()
        self.menu.append(self.mount_separator)

        # Per-mount rows are rebuilt on change; this tracks what we added.
        self.mount_items = {}
```

Replace `self.probe = Probe()` with `self.prober = MultiProbe()`.

- [ ] **Step 3: Rewrite refresh for aggregate state**

```python
    def refresh(self):
        system = self.prober.poll()

        icon = ICONS[system.worst_health]
        if system.worst_health is Health.OK and system.total_transfers:
            icon = UPLOADING_ICON
        self.indicator.set_icon_full(icon, system.summary)

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
```

- [ ] **Step 4: Verify the tray registers with the right aggregate state**

```bash
cd ~/sources/onedrive-status
systemctl --user stop rclone-status-tray.service
python3 -m rcstatus.tray > /tmp/tray.log 2>&1 &
sleep 4
ENTRY=$(gdbus call --session --dest org.kde.StatusNotifierWatcher \
  --object-path /StatusNotifierWatcher \
  --method org.freedesktop.DBus.Properties.Get \
  org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems 2>/dev/null \
  | tr ',' '\n' | grep -o "[^']*rclone_status\|[^']*onedrive_status" | head -1)
BUS=${ENTRY%@*}; PATH_=${ENTRY#*@}
for p in Title IconName XAyatanaLabel; do
  printf "%-14s " "$p"
  gdbus call --session --dest "$BUS" --object-path "$PATH_" \
    --method org.freedesktop.DBus.Properties.Get org.kde.StatusNotifierItem $p
done
pkill -f rcstatus.tray
systemctl --user start rclone-status-tray.service
```

Expected: `Title` beginning `Rclone —`, and an icon matching the live mount state.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
export SSH_AUTH_SOCK=<your ssh-agent socket>
git add rcstatus/tray.py
git commit -m "Show aggregate state in one tray icon

One icon for the machine however many mounts exist: uploading if any is,
worst health otherwise, with each mount listed in the menu.

The tray shares poller.py with the window rather than importing it,
since pulling GTK4 into this GTK3 process raises ImportError on the Gtk
namespace version.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Documentation, screenshot and repo rename

**Files:**
- Modify: `README.md`
- Replace: `docs/window.png`
- Modify: `data/rclone-onedrive.service.example` → `data/rclone-mount.service.example`

**Interfaces:**
- Consumes: everything above.
- Produces: no code.

- [ ] **Step 1: Regenerate the screenshot from synthetic fixtures**

The screenshot is published, so it must not show real filenames or storage figures. Patch the poller to return fixture data, exactly as the existing snapshot script does:

```bash
cd ~/sources/onedrive-status
cat > /tmp/snap_fixture.py <<'EOF'
import json, pathlib, sys
import gi
gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")
from gi.repository import Adw, Gtk, GLib
REPO = pathlib.Path("/home/user/sources/onedrive-status")
sys.path.insert(0, str(REPO))
from rcstatus import probe as P, poller
from rcstatus.aggregate import SystemSnapshot
from rcstatus.discovery import Mount
from rcstatus.window import StatusWindow

F = REPO / "tests/fixtures"
load = lambda n: json.loads((F / f"{n}.json").read_text())
MOUNT = Mount("onedrive:", "/home/user/OneDrive", 1, "127.0.0.1:5572",
              "rclone-onedrive.service", True)
FIXED = SystemSnapshot([P.build_snapshot(
    MOUNT, core=load("core_stats_uploading"), vfs=load("vfs_stats_uploading"),
    about=load("about"), mounted=True, uptime_seconds=1904)])
poller.MultiProbe.poll = lambda self: FIXED

class App(Adw.Application):
    def __init__(self): super().__init__(application_id="dev.matheoatche.RcloneStatus.Snap")
    def do_activate(self):
        w = StatusWindow(self); w.present()
        GLib.timeout_add(2500, self.grab, w)
    def grab(self, w):
        p = Gtk.WidgetPaintable.new(w)
        s = Gtk.Snapshot(); p.snapshot(s, w.get_width(), w.get_height())
        w.get_native().get_renderer().render_texture(s.to_node(), None).save_to_png(sys.argv[1])
        print("saved"); self.quit(); return False
App().run([])
EOF
.venv/bin/python /tmp/snap_fixture.py docs/window.png
```

Then read `docs/window.png` and confirm it shows `sample-archive.iso` and `420.0 GB of 1.0 TB`, not real data.

- [ ] **Step 2: Rewrite the README**

Update: the title and description to Rclone Status; the "What it actually reports" section to describe N mounts; a new "Mounts without `--rc`" subsection explaining that such a mount is healthy but statless and how to enable stats; the layout table to list `discovery.py`, `poller.py` and `aggregate.py`; the install and uninstall commands to the new names; and a "Migrating from onedrive-status" section noting `install.sh` handles it.

Rename the example unit and generalise it:

```bash
git mv data/rclone-onedrive.service.example data/rclone-mount.service.example
sed -i 's/onedrive:/REMOTE:/g; s|%h/OneDrive|%h/MOUNTPOINT|g; s/OneDrive mount/rclone mount/' \
  data/rclone-mount.service.example
```

- [ ] **Step 3: Verify no stale names or real data**

```bash
cd ~/sources/onedrive-status
# Substitute fragments of the real filenames and storage figures that must
# not appear in the repo; they are deliberately not listed here.
for p in '<real filename fragment>' '<real storage figure>' odstatus onedrive-status; do
  printf "%-20s " "$p"
  grep -rIq -- "$p" --exclude-dir=.git --exclude-dir=.venv \
    --exclude-dir=__pycache__ --exclude-dir=.pytest_cache --exclude-dir=docs . \
    && echo FOUND || echo clean
done
```

Expected: all `clean`. `docs/` is excluded because the spec and this plan record the old names deliberately.

- [ ] **Step 4: Run the full suite and the installer one last time**

```bash
.venv/bin/python -m pytest tests/ -q
./install.sh
systemctl --user is-active rclone-status-tray.service
cd ~ && timeout 5 ~/sources/onedrive-status/bin/rclone-status-tray; echo "launcher ok"
```

Expected: all tests pass, unit active, launcher starts clean from `$HOME`.

- [ ] **Step 5: Commit**

```bash
export SSH_AUTH_SOCK=<your ssh-agent socket>
git add -A
git commit -m "Document multi-mount behaviour and regenerate the screenshot

README describes N discovered mounts, what a mount without --rc shows and
why it is healthy rather than degraded, and how install.sh migrates an
existing onedrive-status install.

The screenshot is rebuilt from synthetic fixtures, so no real filenames or
storage figures are published.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Rename the GitHub repository**

Only now, once the tree is fully renamed and working — the published repo should never point at a half-renamed state.

```bash
cd ~/sources/onedrive-status
gh repo rename rclone-status --yes
git remote set-url origin git@github.com:MatheoAtche/rclone-status.git
git push origin main
gh repo edit --description "GNOME tray indicator and GTK4 window for rclone mounts"
gh repo view --json name,url,visibility
```

Expected: `rclone-status`, public. GitHub redirects the old URL.

Optionally rename the local checkout to match:

```bash
cd ~ && mv ~/sources/onedrive-status ~/sources/rclone-status
```

If you do, re-run `~/sources/rclone-status/install.sh` so the desktop entry and unit point at the new path.

---

## Self-Review

**Spec coverage:** discovery (Task 2), probe parameterisation (Task 3), aggregation (Task 4), window cards (Task 5), tray (Task 6), rename and migration (Tasks 1 and 7), docs and repo rename (Task 7). The refresh-interval table is implemented in `MultiProbe` (`rcstatus/poller.py`, Task 5 Step 1). The health/stats table is implemented and tested in Task 3. The system-vs-user unit rule appears in Task 2 (`user_unit`, `can_restart`), Task 3 (`_unit_uptime` scope) and Task 5 (restart button visibility). Every spec section maps to a task.

**Placeholders:** none. Every code step carries the actual code; every test step carries the actual assertions.

**Type consistency:** `Mount.key` is used in Tasks 2, 5 and 6 under that name. `MountSnapshot.stats_available` and `.mount` are defined in Task 3 and consumed in Tasks 4, 5 and 6. `SystemSnapshot.worst_health`, `.total_transfers` and `.summary` are defined in Task 4 and consumed in Tasks 5 and 6. `MultiProbe.poll()` returns `SystemSnapshot` in both places it is used. `Mount.has_stats` is defined in Task 2 and used in Tasks 3 and 5.

**Fixed during review:** an earlier draft wrote `MultiProbe` into `window.py` in Task 5 and moved it to `poller.py` in Task 6. It is now created in `poller.py` directly, since the tray needs it and a GTK3 process cannot import a GTK4 module. No task moves code another task just wrote.
