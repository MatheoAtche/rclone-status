"""Reads OneDrive mount state from rclone's rc API and systemd.

Pure data layer: no GUI imports, so both the GTK3 tray and the GTK4 window
can share it. Every public entry point degrades to a usable Snapshot rather
than raising -- "the mount is down" is precisely the state worth displaying,
so a failed poll must never crash a caller.
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

RC_ADDR = os.environ.get("ODSTATUS_RC_ADDR", "127.0.0.1:5572")
UNIT = os.environ.get("ODSTATUS_UNIT", "rclone-onedrive.service")
MOUNTPOINT = os.environ.get("ODSTATUS_MOUNT", os.path.expanduser("~/OneDrive"))
REMOTE = os.environ.get("ODSTATUS_REMOTE", "onedrive:")

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
class Snapshot:
    """Everything the UIs render, captured at one instant."""

    health: Health
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
    unit_active: bool = False
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
            return "Mount is not running"
        if self.health is Health.DEGRADED:
            return "Mount running, status unavailable"
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


def build_snapshot(core, vfs, about, unit_active, uptime_seconds, mounted) -> Snapshot:
    """Assemble a Snapshot from raw payloads. Any of them may be None."""
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

    if not unit_active or not mounted:
        # A live unit with a vanished FUSE mount is still unusable.
        health = Health.DOWN
    elif core is None or not core:
        # Mounted but the rc API gave us nothing: running, status unknown.
        health = Health.DEGRADED
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

    return Snapshot(
        health=health,
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
        unit_active=unit_active,
    )


def _rc(endpoint: str, payload: dict | None = None):
    """POST to the rclone rc API; return parsed JSON or None if unreachable."""
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        f"http://{RC_ADDR}/{endpoint}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=RC_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def _systemd_state() -> tuple[bool, int | None]:
    """Return (unit_active, uptime_seconds)."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", UNIT,
             "--property=ActiveState", "--property=ActiveEnterTimestampMonotonic"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False, None

    props = dict(
        line.split("=", 1) for line in out.strip().splitlines() if "=" in line
    )
    active = props.get("ActiveState") == "active"

    uptime = None
    started = props.get("ActiveEnterTimestampMonotonic")
    if active and started and started.isdigit() and int(started) > 0:
        # CLOCK_MONOTONIC microseconds, matching the kernel's own clock.
        uptime = max(0, int(time.clock_gettime(time.CLOCK_MONOTONIC) - int(started) / 1e6))
    return active, uptime


class Probe:
    """Polls the mount, caching the expensive quota call."""

    def __init__(self):
        self._quota = None
        self._quota_at = 0.0

    def _quota_now(self):
        age = time.time() - self._quota_at
        if self._quota is None or age > QUOTA_TTL:
            fresh = _rc("operations/about", {"fs": REMOTE})
            if fresh is not None:
                self._quota = fresh
                self._quota_at = time.time()
        return self._quota

    def poll(self) -> Snapshot:
        unit_active, uptime = _systemd_state()
        mounted = os.path.ismount(MOUNTPOINT)

        if not unit_active or not mounted:
            # Drop the cached quota so a restart doesn't show stale figures.
            self._quota = None
            return build_snapshot(None, None, None, unit_active, uptime, mounted)

        core = _rc("core/stats")
        vfs = _rc("vfs/stats")
        about = self._quota_now() if core is not None else None
        return build_snapshot(core, vfs, about, unit_active, uptime, mounted)
