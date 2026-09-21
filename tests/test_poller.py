"""Tests for MultiProbe: discovery cadence, Probe reuse, and the F4 carry-
forward. `poller.discover`, `poller.Probe` and time are all monkeypatched, so
nothing here touches the real /proc or network.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import rcstatus.poller as poller
import rcstatus.probe as probe
from rcstatus.discovery import Mount
from rcstatus.probe import Health, MountSnapshot, Probe

MOUNT_A = Mount("a:", "/mnt/a", pid=1, rc_addr="127.0.0.1:5572",
                unit="rclone-a.service", user_unit=True)
MOUNT_A_NEW_PID = Mount("a:", "/mnt/a", pid=2, rc_addr="127.0.0.1:5572",
                         unit="rclone-a.service", user_unit=True)
MOUNT_A_NEW_PORT = Mount("a:", "/mnt/a", pid=1, rc_addr="127.0.0.1:5599",
                          unit="rclone-a.service", user_unit=True)
MOUNT_A_CRASHED = Mount("a:", "/mnt/a", pid=None, rc_addr=None, unit=None, user_unit=False)
MOUNT_B = Mount("b:", "/mnt/b", pid=3, rc_addr=None, unit=None, user_unit=False)


class FakeClock:
    """A stand-in for time.monotonic that only moves when told to."""

    def __init__(self, start=1_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeProbe:
    """Stands in for probe.Probe: records identity, never touches the OS."""

    instances = []
    stats_available_overrides = {}

    def __init__(self, mount):
        self.mount = mount
        FakeProbe.instances.append(self)

    def poll(self):
        stats_available = FakeProbe.stats_available_overrides.get(self.mount.key, True)
        health = Health.OK if stats_available else Health.DEGRADED
        return MountSnapshot(
            health=health, mount=self.mount, stats_available=stats_available, mounted=True,
        )


class FakeDiscover:
    """Stands in for discovery.discover: returns a scripted list, counts calls."""

    def __init__(self, mounts=()):
        self.mounts = list(mounts)
        self.call_count = 0

    def __call__(self):
        self.call_count += 1
        return list(self.mounts)


@pytest.fixture
def env(monkeypatch):
    FakeProbe.instances = []
    FakeProbe.stats_available_overrides = {}
    clock = FakeClock()
    fake_discover = FakeDiscover()
    monkeypatch.setattr(poller.time, "monotonic", clock)
    monkeypatch.setattr(poller, "discover", fake_discover)
    monkeypatch.setattr(poller, "Probe", FakeProbe)
    return clock, fake_discover


class TestDiscoveryCadence:
    def test_discovery_runs_once_on_the_first_poll(self, env):
        clock, fake_discover = env
        fake_discover.mounts = [MOUNT_A]
        mp = poller.MultiProbe()
        mp.poll()
        assert fake_discover.call_count == 1

    def test_discovery_does_not_rerun_within_the_floor(self, env):
        clock, fake_discover = env
        fake_discover.mounts = [MOUNT_A]
        mp = poller.MultiProbe()
        mp.poll()
        clock.advance(poller.DISCOVERY_SECONDS - 1)
        mp.poll()
        assert fake_discover.call_count == 1

    def test_discovery_reruns_after_the_floor(self, env):
        clock, fake_discover = env
        fake_discover.mounts = [MOUNT_A]
        mp = poller.MultiProbe()
        mp.poll()
        clock.advance(poller.DISCOVERY_SECONDS + 1)
        mp.poll()
        assert fake_discover.call_count == 2


class TestProbeReuse:
    def test_probe_is_reused_for_an_unchanged_mount(self, env):
        clock, fake_discover = env
        fake_discover.mounts = [MOUNT_A]
        mp = poller.MultiProbe()
        mp.poll()
        first = mp._probes[MOUNT_A.key]

        clock.advance(poller.DISCOVERY_SECONDS + 1)
        mp.poll()
        assert mp._probes[MOUNT_A.key] is first

    def test_probe_is_replaced_when_the_pid_changes(self, env):
        clock, fake_discover = env
        fake_discover.mounts = [MOUNT_A]
        mp = poller.MultiProbe()
        mp.poll()
        first = mp._probes[MOUNT_A.key]

        clock.advance(poller.DISCOVERY_SECONDS + 1)
        fake_discover.mounts = [MOUNT_A_NEW_PID]
        mp.poll()
        assert mp._probes[MOUNT_A.key] is not first
        assert mp._probes[MOUNT_A.key].mount.pid == 2

    def test_probe_is_replaced_when_the_rc_addr_changes(self, env):
        clock, fake_discover = env
        fake_discover.mounts = [MOUNT_A]
        mp = poller.MultiProbe()
        mp.poll()
        first = mp._probes[MOUNT_A.key]

        clock.advance(poller.DISCOVERY_SECONDS + 1)
        fake_discover.mounts = [MOUNT_A_NEW_PORT]
        mp.poll()
        assert mp._probes[MOUNT_A.key] is not first
        assert mp._probes[MOUNT_A.key].mount.rc_addr == "127.0.0.1:5599"

    def test_probe_is_dropped_when_its_mount_disappears(self, env):
        clock, fake_discover = env
        fake_discover.mounts = [MOUNT_A, MOUNT_B]
        mp = poller.MultiProbe()
        mp.poll()
        assert set(mp._probes) == {MOUNT_A.key, MOUNT_B.key}

        clock.advance(poller.DISCOVERY_SECONDS + 1)
        fake_discover.mounts = [MOUNT_A]
        mp.poll()
        assert set(mp._probes) == {MOUNT_A.key}


class TestNoPollStorm:
    def test_an_unreachable_rc_mount_causes_no_extra_discovery(self, env):
        # Regression guard: the old "rediscover sooner on failure" branch
        # made an unreachable-but-configured mount re-walk /proc and
        # re-poll every other mount on every tick. That branch is gone;
        # discovery now runs on its floor and nowhere else.
        clock, fake_discover = env
        fake_discover.mounts = [MOUNT_A]
        FakeProbe.stats_available_overrides = {MOUNT_A.key: False}
        mp = poller.MultiProbe()

        for _ in range(5):
            mp.poll()
        assert fake_discover.call_count == 1

        clock.advance(poller.DISCOVERY_SECONDS + 1)
        mp.poll()
        assert fake_discover.call_count == 2


class TestCrashedMountKeepsItsUnit:
    """F4: a stale /proc/mounts entry must not lose its Restart button."""

    def test_unit_and_user_unit_carry_forward_when_the_process_dies(self, env):
        clock, fake_discover = env
        fake_discover.mounts = [MOUNT_A]
        mp = poller.MultiProbe()
        mp.poll()

        clock.advance(poller.DISCOVERY_SECONDS + 1)
        fake_discover.mounts = [MOUNT_A_CRASHED]
        mp.poll()

        carried = mp._probes[MOUNT_A.key].mount
        assert carried.pid is None
        assert carried.unit == MOUNT_A.unit
        assert carried.user_unit == MOUNT_A.user_unit

    def test_no_carry_forward_when_there_was_never_a_unit(self, env):
        clock, fake_discover = env
        fake_discover.mounts = [MOUNT_B]
        mp = poller.MultiProbe()
        mp.poll()

        clock.advance(poller.DISCOVERY_SECONDS + 1)
        gone = Mount("b:", "/mnt/b", pid=None, rc_addr=None, unit=None, user_unit=False)
        fake_discover.mounts = [gone]
        mp.poll()

        assert mp._probes[MOUNT_B.key].mount.unit is None


class TestProbePollWithoutRc:
    def test_a_mount_with_no_rc_makes_zero_rc_calls(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(probe, "_rc", lambda *a, **k: calls.append(1))
        monkeypatch.setattr(probe.os.path, "ismount", lambda p: True)

        mount = Mount("b:", str(tmp_path), pid=1, rc_addr=None, unit=None, user_unit=False)
        snap = Probe(mount).poll()

        assert calls == []
        assert snap.stats_available is False
