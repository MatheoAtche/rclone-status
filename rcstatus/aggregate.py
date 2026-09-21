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
    def without_stats_count(self) -> int:
        return sum(1 for m in self.mounts if not m.stats_available)

    @property
    def summary(self) -> str:
        if not self.mounts:
            return "No rclone mounts"

        parts = []
        count = self.total_transfers
        without_stats = self.without_stats_count
        if count:
            noun = "file" if count == 1 else "files"
            parts.append(f"Uploading {count} {noun}")
            parts.append(f"{format_bytes(self.total_speed_bps)}/s")
        elif self.pending:
            parts.append("Preparing upload")
        elif without_stats == len(self.mounts):
            # Nothing is transferring, but nothing can confirm "up to date"
            # either -- every mount lacks stats.
            parts.append("Stats unavailable")
        elif without_stats:
            # Some mounts can confirm they are current; others cannot, so
            # "Up to date" alone would overclaim.
            noun = "mount" if without_stats == 1 else "mounts"
            parts.append(f"Up to date · {without_stats} {noun} without stats")
        else:
            parts.append("Up to date")

        if self.down_count:
            noun = "mount" if self.down_count == 1 else "mounts"
            parts.append(f"{self.down_count} {noun} down")
        if self.any_errors:
            parts.append("errors")
        return " · ".join(parts)
