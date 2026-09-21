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
    lines = [line for line in text.strip().splitlines() if line]
    if not lines:
        return None, False
    path = lines[-1].split(":")[-1]
    parts = [p for p in path.split("/") if p.endswith(".service")]
    if not parts:
        return None, False
    unit = parts[-1]
    if unit.startswith("user@"):
        # The user manager itself owns nothing we care about.
        return None, False
    return unit, "user@" in path


def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _mount_processes(root: pathlib.Path) -> list[tuple[int, list[str], str]]:
    """Every rclone mount process on the machine: (pid, argv, cgroup text)."""
    procs = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return procs

    for entry in entries:
        if not entry.name.isdigit():
            continue
        # comm is one short read; skip the rest for non-rclone processes.
        if _read(entry / "comm").strip() != "rclone":
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a for a in raw.decode(errors="replace").split("\0") if a]
        if len(argv) < 2 or argv[1] not in MOUNT_SUBCOMMANDS:
            continue
        procs.append((int(entry.name), argv, _read(entry / "cgroup")))
    return procs


def _match_process(
    procs: list[tuple[int, list[str], str]], remote: str, mountpoint: str
) -> tuple[int, list[str], str] | None:
    """The process serving this mount.

    Requires argv to name BOTH the remote and the mountpoint: matching on
    the mountpoint alone lets any process that merely mentions that path --
    `--cache-dir /home/u/OneDrive`, say -- steal another mount's identity.
    Falls back to a mountpoint-only match when exactly one process is a
    candidate, so an unusual device name in /proc/mounts does not lose the
    process entirely.
    """
    for proc in procs:
        _, argv, _ = proc
        if mountpoint in argv and remote in argv:
            return proc

    candidates = [p for p in procs if mountpoint in p[1]]
    return candidates[0] if len(candidates) == 1 else None


def discover(proc_root: str = "/proc") -> list[Mount]:
    """Every rclone mount on the machine, in /proc/mounts order."""
    root = pathlib.Path(proc_root)
    pairs = parse_mounts_file(_read(root / "mounts"))
    if not pairs:
        return []

    processes = _mount_processes(root)
    mounts = []
    for remote, mountpoint in pairs:
        proc = _match_process(processes, remote, mountpoint)
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
