"""Discovery plus per-mount polling, shared by both UIs.

Lives apart from window.py because the GTK3 tray cannot import a module
that pulls in GTK4.
"""

from __future__ import annotations

import dataclasses
import time

from rcstatus.aggregate import SystemSnapshot
from rcstatus.discovery import discover
from rcstatus.probe import Probe

DISCOVERY_SECONDS = 10


class MultiProbe:
    """Discovers mounts and polls each, reusing Probes across refreshes.

    Discovery is far more expensive than a stats call and mounts change
    rarely, so it runs on its own slower clock, no more often than every
    DISCOVERY_SECONDS, and at no other time: an unreachable mount does not
    trigger it sooner, so a merely-unreachable mount can never turn into a
    poll storm that re-walks /proc and re-polls every other mount too.
    """

    def __init__(self):
        self._probes = {}
        self._discovered_at = float("-inf")

    def _rediscover(self):
        mounts = discover()
        seen = set()
        for mount in mounts:
            seen.add(mount.key)
            existing = self._probes.get(mount.key)
            if existing is not None and existing.mount == mount:
                continue
            if (
                mount.unit is None
                and existing is not None
                and existing.mount.unit is not None
            ):
                # A stale /proc/mounts entry: the process died but the FUSE
                # mount is still there. Discovery can no longer see the
                # owning unit from /proc, but the previous Probe still knows
                # it -- carry it forward so the Restart button does not
                # disappear exactly when the user needs it most.
                mount = dataclasses.replace(
                    mount, unit=existing.mount.unit, user_unit=existing.mount.user_unit
                )
            self._probes[mount.key] = Probe(mount)
        for key in list(self._probes):
            if key not in seen:
                del self._probes[key]
        self._discovered_at = time.monotonic()

    def poll(self) -> SystemSnapshot:
        if time.monotonic() - self._discovered_at > DISCOVERY_SECONDS:
            self._rediscover()

        snaps = [p.poll() for p in self._probes.values()]
        return SystemSnapshot(snaps)
