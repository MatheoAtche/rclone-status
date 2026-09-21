"""Reads an rclone mount's state from its rc API and systemd.

Pure data layer: no GUI imports, so both the GTK3 tray and the GTK4 window
can share it. Every public entry point degrades to a usable MountSnapshot
rather than raising -- "the mount is down" is precisely the state worth
displaying, so a failed poll must never crash a caller.
"""

from __future__ import annotations

import enum
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from rcstatus.discovery import Mount

# The rc API is loopback-only and started with --rc-no-auth, so a short
# timeout is enough; anything slower means the mount is wedged.
RC_TIMEOUT = 2.0
# operations/about is a live call to Microsoft, unlike the local stats
# endpoints. Poll it sparingly to stay clear of API rate limits.
QUOTA_TTL = 60.0


class Health(enum.Enum):
    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"
    DOWN = "down"


def format_bytes(value) -> str:
    if value is None:
        return "—"
    value = float(value)
    if value < 1024:
        return f"{int(value)} B"
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} EB"


def format_duration(seconds) -> str:
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


@dataclass
class Transfer:
    """One file currently moving between the local cache and OneDrive."""

    name: str
    path: str
    bytes: int
    size: int
    speed_bps: float
    eta_seconds: int | None

    @property
    def fraction(self) -> float:
        if not self.size:
            return 0.0
        return min(1.0, self.bytes / self.size)

    @property
    def percent(self) -> int:
        return int(self.fraction * 100)


@dataclass
class MountSnapshot:
    """Everything the UIs render for one mount, captured at one instant."""

    health: Health
    mount: Mount | None = None
    stats_available: bool = False
    transfers: list[Transfer] = field(default_factory=list)
    uploads_in_progress: int = 0
    uploads_queued: int = 0
    errors: int = 0
    last_error: str = ""
    cache_bytes: int | None = None
    cache_max_bytes: int | None = None
    cache_files: int = 0
    quota_used: int | None = None
    quota_total: int | None = None
    uptime_seconds: int | None = None
    mounted: bool = False
    taken_at: float = field(default_factory=time.time)

    @property
    def pending(self) -> bool:
        return bool(self.transfers) or self.uploads_in_progress > 0 or self.uploads_queued > 0

    @property
    def total_speed_bps(self) -> float:
        return sum(t.speed_bps for t in self.transfers)

    @property
    def overall_eta_seconds(self) -> int | None:
        etas = [t.eta_seconds for t in self.transfers if t.eta_seconds is not None]
        return max(etas) if etas else None

    @property
    def cache_fraction(self) -> float | None:
        if not self.cache_max_bytes or self.cache_bytes is None:
            return None
        return min(1.0, self.cache_bytes / self.cache_max_bytes)

    @property
    def quota_fraction(self) -> float | None:
        if not self.quota_total or self.quota_used is None:
            return None
        return min(1.0, self.quota_used / self.quota_total)

    @property
    def summary(self) -> str:
        if self.health is Health.DOWN:
            return "Not mounted"
        if not self.stats_available:
            if self.mount and not self.mount.has_stats:
                return "Mounted · no stats (add --rc)"
            return "Mounted · stats unavailable"
        parts = []
        if self.transfers:
            noun = "file" if len(self.transfers) == 1 else "files"
            parts.append(f"Uploading {len(self.transfers)} {noun}")
            parts.append(f"{format_bytes(self.total_speed_bps)}/s")
        elif self.uploads_queued or self.uploads_in_progress:
            parts.append("Preparing upload")
        else:
            parts.append("Up to date")
        if self.uploads_queued:
            parts.append(f"{self.uploads_queued} queued")
        if self.errors:
            noun = "error" if self.errors == 1 else "errors"
            parts.append(f"{self.errors} {noun}")
        return " · ".join(parts)


def build_snapshot(mount, core, vfs, about, mounted, uptime_seconds) -> MountSnapshot:
    """Assemble a MountSnapshot. core/vfs/about may each be None."""
    # Capture this BEFORE the coercion below: once `core` is {} there is no way
    # to tell "the rc API could not be reached" from "the rc API answered with
    # an empty object". Only the first is a fault.
    stats_available = core is not None

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
