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

import os
import pathlib
import re
from dataclasses import dataclass

# rclone's own default when --rc is given without an address.
DEFAULT_RC_ADDR = "127.0.0.1:5572"

MOUNT_SUBCOMMANDS = frozenset({"mount", "mount2", "cmount"})
FSTYPE = "fuse.rclone"

# Flags rclone accepts before the subcommand that take a separate,
# space-delimited value. Without this, the value (e.g. the path after
# --config) would be mistaken for the subcommand. The --flag=value form
# needs no such list: it carries its value inline in one argv element.
SUBCOMMAND_VALUE_FLAGS = frozenset({"--config", "--log-file", "--log-level", "--cache-dir"})

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


def _normalize_rc_addr(addr: str) -> str | None:
    """Turn an rc-addr value into something a URL can be built from.

    rclone itself treats an empty host, 0.0.0.0 and [::] as "listen on every
    interface"; none of those are dialable, so map them to loopback. A
    unix:// address is a real rc server but not one we support (no HTTP
    client here for it), so it returns None like an absent one.
    """
    if addr.startswith("unix://"):
        return None
    host, sep, port = addr.rpartition(":")
    if not sep:
        return addr
    if host in ("", "0.0.0.0", "[::]"):
        host = "127.0.0.1"
    return f"{host}:{port}"


def rc_addr_from_argv(argv: list[str]) -> str | None:
    """The mount's rc endpoint, or None when it exposes no stats.

    Only --rc actually starts rclone's rc server ("--rc  Enable the remote
    control server", per `rclone help flags`); --rc-addr alone just names an
    address nothing is listening on, so it is meaningless without --rc.
    """
    if "--rc" not in argv:
        return None
    for i, arg in enumerate(argv):
        if arg.startswith("--rc-addr="):
            return _normalize_rc_addr(arg.split("=", 1)[1])
        if arg == "--rc-addr":
            if i + 1 < len(argv):
                return _normalize_rc_addr(argv[i + 1])
            return None
    return DEFAULT_RC_ADDR


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


def _read_cwd(root: pathlib.Path, pid: int) -> str | None:
    """The process's working directory, for resolving relative argv paths."""
    try:
        return os.readlink(root / str(pid) / "cwd")
    except OSError:
        return None


def _subcommand_index(argv: list[str]) -> int | None:
    """Index of rclone's subcommand: the first element after argv[0] that
    does not start with '-'.

    A flag using --flag=value carries its value inline and costs one slot.
    A handful of flags rclone accepts before the subcommand take their value
    as a separate argv element (SUBCOMMAND_VALUE_FLAGS); those cost two, or
    the value would be mistaken for the subcommand. -v/-vv take no value.
    """
    i = 1
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("-"):
            return i
        if "=" not in arg and arg in SUBCOMMAND_VALUE_FLAGS:
            i += 2
        else:
            i += 1
    return None


def _normalized_argv_paths(argv: list[str], cwd: str | None) -> set[str]:
    """Every non-flag argv element as an absolute, normalized path.

    Relative elements are resolved against the process's own cwd, so
    `rclone mount od: OD --rc` run from /home/u matches a mountpoint of
    /home/u/OD. Flags are skipped; the remaining elements (remote names,
    paths) are cheap to normalize even when some of them are not paths.
    """
    paths = set()
    for arg in argv:
        if arg.startswith("-"):
            continue
        candidate = arg
        if not os.path.isabs(candidate) and cwd:
            candidate = os.path.join(cwd, candidate)
        paths.add(os.path.normpath(candidate))
    return paths


def _mount_processes(root: pathlib.Path) -> list[tuple[int, list[str], str, set[str]]]:
    """Every rclone mount process: (pid, argv, cgroup text, normalized argv paths)."""
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
        idx = _subcommand_index(argv)
        if idx is None or argv[idx] not in MOUNT_SUBCOMMANDS:
            continue
        pid = int(entry.name)
        cwd = _read_cwd(root, pid)
        procs.append((pid, argv, _read(entry / "cgroup"), _normalized_argv_paths(argv, cwd)))
    return procs


def _match_process(
    procs: list[tuple[int, list[str], str, set[str]]], remote: str, mountpoint: str
) -> tuple[int, list[str], str, set[str]] | None:
    """The process serving this mount.

    Requires argv to name BOTH the remote and the mountpoint: matching on
    the mountpoint alone lets any process that merely mentions that path --
    `--cache-dir /home/u/OneDrive`, say -- steal another mount's identity.
    Falls back to a mountpoint-only match when exactly one process is a
    candidate, so an unusual device name in /proc/mounts does not lose the
    process entirely. Mountpoints are compared as normalized, absolute
    paths so a trailing slash or a relative argv path still matches.
    """
    target = os.path.normpath(mountpoint)
    for proc in procs:
        _, argv, _, norm_paths = proc
        if target in norm_paths and remote in argv:
            return proc

    candidates = [p for p in procs if target in p[3]]
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
        pid, argv, cgroup, _ = proc
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
