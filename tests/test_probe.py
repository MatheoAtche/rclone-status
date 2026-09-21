"""Tests for the probe layer, driven by synthetic fixtures shaped like real
rc API responses."""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import rcstatus.probe as probe
from rcstatus.discovery import Mount
from rcstatus.probe import (
    Health,
    MountSnapshot,
    Probe,
    Transfer,
    build_snapshot,
    format_bytes,
    format_duration,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


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
    )


@pytest.fixture
def idle():
    return build_snapshot(
        MOUNT,
        core=load("core_stats_idle"),
        vfs=load("vfs_stats_idle"),
        about=load("about"),
        mounted=True,
    )


class TestTransfers:
    def test_parses_each_in_flight_upload(self, uploading):
        assert len(uploading.transfers) == 2

    def test_uses_basename_not_full_remote_path(self, uploading):
        # rclone reports "Videos/sample-archive.iso"; the UI wants the filename.
        names = [t.name for t in uploading.transfers]
        assert "sample-archive.iso" in names
        assert not any("/" in n for n in names)

    def test_keeps_full_path_for_tooltip(self, uploading):
        paths = [t.path for t in uploading.transfers]
        assert "Videos/sample-archive.iso" in paths

    def test_reads_progress_fields(self, uploading):
        t = next(t for t in uploading.transfers if t.name.startswith("sample"))
        assert t.bytes == 2147483648
        assert t.size == 8589934592
        assert t.eta_seconds == 5340
        assert t.speed_bps == pytest.approx(1205020.75, rel=1e-3)

    def test_computes_fraction_from_bytes_not_rclone_percentage(self, uploading):
        # rclone's integer "percentage" is lossy; derive from bytes for a smooth bar.
        t = next(t for t in uploading.transfers if t.name.startswith("sample"))
        assert t.fraction == pytest.approx(2147483648 / 8589934592)

    def test_fraction_is_zero_when_size_unknown(self):
        t = Transfer(name="x", path="x", bytes=10, size=0, speed_bps=0, eta_seconds=None)
        assert t.fraction == 0.0

    def test_fraction_never_exceeds_one(self):
        t = Transfer(name="x", path="x", bytes=50, size=10, speed_bps=0, eta_seconds=None)
        assert t.fraction == 1.0

    def test_idle_has_no_transfers(self, idle):
        assert idle.transfers == []


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
                               about=load("about"), mounted=True)
        assert snap.health is Health.ERROR
        assert snap.errors == 3
        assert snap.last_error == "quota exceeded"

    def test_cache_out_of_space_is_an_error(self):
        vfs = load("vfs_stats_idle")
        vfs["diskCache"]["outOfSpace"] = True
        snap = build_snapshot(MOUNT, core=load("core_stats_idle"), vfs=vfs,
                               about=load("about"), mounted=True)
        assert snap.health is Health.ERROR

    def test_no_rc_configured_is_healthy_without_stats(self):
        # A deliberate choice, not a fault: must not warn forever.
        snap = build_snapshot(NO_RC_MOUNT, core=None, vfs=None, about=None,
                               mounted=True)
        assert snap.health is Health.OK
        assert snap.stats_available is False

    def test_configured_rc_that_is_unreachable_is_degraded(self):
        snap = build_snapshot(MOUNT, core=None, vfs=None, about=None,
                               mounted=True)
        assert snap.health is Health.DEGRADED
        assert snap.stats_available is False

    def test_rc_answering_with_an_empty_object_is_ok_with_stats(self):
        # An empty {} means the rc API DID answer -- distinct from None, which
        # means it could not be reached at all. Only the latter is a fault.
        snap = build_snapshot(MOUNT, core={}, vfs={}, about=None,
                               mounted=True)
        assert snap.stats_available is True
        assert snap.health is Health.OK

    def test_unmounted_is_down(self):
        snap = build_snapshot(MOUNT, core=None, vfs=None, about=None,
                               mounted=False)
        assert snap.health is Health.DOWN
        assert snap.stats_available is False

    def test_unmounted_beats_a_missing_rc(self):
        snap = build_snapshot(NO_RC_MOUNT, core=None, vfs=None, about=None,
                               mounted=False)
        assert snap.health is Health.DOWN

    def test_never_raises_on_empty_payloads(self):
        snap = build_snapshot(MOUNT, core={}, vfs={}, about={},
                               mounted=True)
        assert isinstance(snap, MountSnapshot)
        assert snap.transfers == []

    def test_snapshot_carries_its_mount(self, uploading):
        assert uploading.mount.remote == "onedrive:"


class TestNoRcSummary:
    def test_summary_names_the_missing_flag(self):
        snap = build_snapshot(NO_RC_MOUNT, core=None, vfs=None, about=None,
                               mounted=True)
        assert "--rc" in snap.summary


class TestQueue:
    def test_reads_upload_counts(self, uploading):
        assert uploading.uploads_in_progress == 2
        assert uploading.uploads_queued == 0

    def test_pending_is_true_while_uploading(self, uploading):
        assert uploading.pending is True

    def test_pending_is_false_when_idle(self, idle):
        assert idle.pending is False


class TestCacheAndQuota:
    def test_reads_cache_usage(self, uploading):
        assert uploading.cache_bytes == 32212254720
        assert uploading.cache_max_bytes == 107374182400
        assert uploading.cache_files == 1200

    def test_cache_fraction(self, uploading):
        assert uploading.cache_fraction == pytest.approx(32212254720 / 107374182400)

    def test_reads_cloud_quota(self, uploading):
        assert uploading.quota_used == 450971566080
        assert uploading.quota_total == 1099511627776

    def test_quota_fraction(self, uploading):
        assert uploading.quota_fraction == pytest.approx(450971566080 / 1099511627776)

    def test_missing_quota_is_none_not_zero(self, idle):
        snap = build_snapshot(
            MOUNT, core=load("core_stats_idle"), vfs=load("vfs_stats_idle"), about=None,
            mounted=True,
        )
        # None means "not yet polled"; zero would wrongly render an empty bar.
        assert snap.quota_used is None
        assert snap.quota_fraction is None


class TestQuotaThrottle:
    def test_a_failing_about_call_is_still_throttled(self, monkeypatch):
        # Before the fix, a failed operations/about never updated _quota_at,
        # so it was retried on every single _quota_now() call -- forever, for
        # backends with no About and for every tick while offline.
        calls = []

        def failing_rc(rc_addr, endpoint, payload=None):
            if endpoint == "operations/about":
                calls.append(1)
            return None

        monkeypatch.setattr(probe, "_rc", failing_rc)
        p = Probe(MOUNT)
        for _ in range(5):
            p._quota_now()
        assert len(calls) == 1

    def test_a_successful_about_call_is_reused_until_ttl(self, monkeypatch):
        calls = []

        def succeeding_rc(rc_addr, endpoint, payload=None):
            if endpoint == "operations/about":
                calls.append(1)
                return {"used": 1, "total": 2}
            return None

        monkeypatch.setattr(probe, "_rc", succeeding_rc)
        p = Probe(MOUNT)
        for _ in range(5):
            assert p._quota_now() == {"used": 1, "total": 2}
        assert len(calls) == 1


class TestAggregateSpeed:
    def test_sums_speed_across_transfers(self, uploading):
        assert uploading.total_speed_bps == pytest.approx(1205020.75 + 1205811.41, rel=1e-3)

    def test_eta_is_the_slowest_transfer(self, uploading):
        # The batch is done when its last file finishes.
        assert uploading.overall_eta_seconds == 5340

    def test_idle_speed_is_zero(self, idle):
        assert idle.total_speed_bps == 0


class TestFormatting:
    @pytest.mark.parametrize("value,expected", [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1048576, "1.0 MB"),
        (607727616, "579.6 MB"),
        (8471603200, "7.9 GB"),
        (1099511627776, "1.0 TB"),
    ])
    def test_format_bytes(self, value, expected):
        assert format_bytes(value) == expected

    def test_format_bytes_handles_none(self):
        assert format_bytes(None) == "—"

    @pytest.mark.parametrize("value,expected", [
        (0, "0s"),
        (45, "45s"),
        (90, "1m 30s"),
        (3720, "1h 2m"),
        (5698, "1h 34m"),
        (90000, "1d 1h"),
    ])
    def test_format_duration(self, value, expected):
        assert format_duration(value) == expected

    def test_format_duration_handles_none(self):
        assert format_duration(None) == "—"

    def test_format_duration_rejects_negative(self):
        assert format_duration(-5) == "—"


class TestSummaryLine:
    def test_summarises_active_uploads(self, uploading):
        assert uploading.summary == "Uploading 2 files · 2.3 MB/s"

    def test_summarises_idle_as_up_to_date(self, idle):
        assert idle.summary == "Up to date"

    def test_summarises_down(self):
        snap = build_snapshot(
            MOUNT, core=None, vfs=None, about=None,
            mounted=False,
        )
        assert snap.summary == "Not mounted"

    def test_mentions_queue_when_files_are_waiting(self):
        vfs = load("vfs_stats_uploading")
        vfs["diskCache"]["uploadsQueued"] = 4
        snap = build_snapshot(
            MOUNT, core=load("core_stats_uploading"), vfs=vfs, about=load("about"),
            mounted=True,
        )
        assert "4 queued" in snap.summary
