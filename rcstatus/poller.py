"""Discovery plus per-mount polling, shared by both UIs.

Lives apart from window.py because the GTK3 tray cannot import a module
that pulls in GTK4.
"""

from __future__ import annotations

import time

from rcstatus.aggregate import SystemSnapshot
from rcstatus.discovery import discover
from rcstatus.probe import Probe

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

        # A mount that stopped answering may have restarted under a new pid or
        # port, so look again -- but no more often than the normal discovery
        # cadence. Without this bound, a mount that is simply unreachable makes
        # every tick re-walk /proc and re-poll every other mount as well.
        stale = any(not s.stats_available and s.mount.has_stats for s in snaps)
        if stale and time.time() - self._discovered_at > DISCOVERY_SECONDS:
            self._rediscover()
            snaps = [p.poll() for p in self._probes.values()]

        return SystemSnapshot(snaps)
