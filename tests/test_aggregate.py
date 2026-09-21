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
