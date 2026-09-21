"""Discovery is tested against fabricated /proc trees, never the real one."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rcstatus.discovery import (
    DEFAULT_RC_ADDR,
    Mount,
    discover,
    parse_mounts_file,
    rc_addr_from_argv,
    unescape_mount_field,
    unit_from_cgroup,
)

USER_CGROUP = (
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "rclone-onedrive.service\n"
)
SYSTEM_CGROUP = "0::/system.slice/rclone-backup.service\n"
NO_UNIT_CGROUP = "0::/user.slice/user-1000.slice/session-2.scope\n"


def make_proc(tmp_path, mounts_text, processes):
    """Build a fake /proc. processes maps pid -> (comm, argv, cgroup[, cwd]).

    cwd, if given, becomes a `cwd` symlink under the fake pid directory, so
    discovery can resolve a relative mountpoint the same way it would read
    /proc/<pid>/cwd on a real system.
    """
    root = tmp_path / "proc"
    root.mkdir()
    (root / "mounts").write_text(mounts_text)
    for pid, spec in processes.items():
        comm, argv, cgroup = spec[:3]
        cwd = spec[3] if len(spec) > 3 else None
        d = root / str(pid)
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
        (d / "cgroup").write_text(cgroup)
        if cwd is not None:
            (d / "cwd").symlink_to(cwd)
    return str(root)


class TestUnescape:
    def test_decodes_octal_space(self):
        assert unescape_mount_field(r"/home/u/My\040Drive") == "/home/u/My Drive"

    def test_leaves_plain_paths_alone(self):
        assert unescape_mount_field("/home/u/Drive") == "/home/u/Drive"

    def test_decodes_tab_and_backslash(self):
        assert unescape_mount_field(r"/a\011b") == "/a\tb"
        assert unescape_mount_field(r"/a\134b") == "/a\\b"


class TestParseMountsFile:
    def test_picks_only_fuse_rclone_lines(self):
        text = (
            "proc /proc proc rw 0 0\n"
            "onedrive: /home/u/OneDrive fuse.rclone rw,nosuid 0 0\n"
            "/dev/sda1 / ext4 rw 0 0\n"
            "gdrive: /home/u/Drive fuse.rclone rw 0 0\n"
        )
        assert parse_mounts_file(text) == [
            ("onedrive:", "/home/u/OneDrive"),
            ("gdrive:", "/home/u/Drive"),
        ]

    def test_decodes_escaped_mountpoints(self):
        text = r"box: /home/u/My\040Files fuse.rclone rw 0 0" + "\n"
        assert parse_mounts_file(text) == [("box:", "/home/u/My Files")]

    def test_empty_input_yields_nothing(self):
        assert parse_mounts_file("") == []

    def test_ignores_malformed_short_lines(self):
        assert parse_mounts_file("garbage\n") == []


class TestRcAddrFromArgv:
    def test_reads_explicit_rc_addr(self):
        argv = ["rclone", "mount", "x:", "/m", "--rc", "--rc-addr=127.0.0.1:5580"]
        assert rc_addr_from_argv(argv) == "127.0.0.1:5580"

    def test_bare_rc_falls_back_to_the_rclone_default(self):
        assert rc_addr_from_argv(["rclone", "mount", "x:", "/m", "--rc"]) == DEFAULT_RC_ADDR

    def test_no_rc_flag_means_no_stats(self):
        assert rc_addr_from_argv(["rclone", "mount", "x:", "/m"]) is None

    def test_supports_space_separated_form(self):
        argv = ["rclone", "mount", "x:", "/m", "--rc", "--rc-addr", "127.0.0.1:5590"]
        assert rc_addr_from_argv(argv) == "127.0.0.1:5590"

    def test_rc_addr_as_final_element_is_safe(self):
        # --rc-addr as the final element with no value should not raise.
        argv = ["rclone", "mount", "x:", "/m", "--rc", "--rc-addr"]
        assert rc_addr_from_argv(argv) is None

    def test_rc_addr_without_rc_is_no_stats(self):
        # Only --rc starts rclone's rc server; --rc-addr alone binds nothing.
        argv = ["rclone", "mount", "x:", "/m", "--rc-addr=127.0.0.1:5572"]
        assert rc_addr_from_argv(argv) is None

    @pytest.mark.parametrize("raw,expected", [
        (":5572", "127.0.0.1:5572"),
        ("0.0.0.0:5572", "127.0.0.1:5572"),
        ("[::]:5572", "127.0.0.1:5572"),
    ])
    def test_wildcard_hosts_map_to_loopback(self, raw, expected):
        argv = ["rclone", "mount", "x:", "/m", "--rc", f"--rc-addr={raw}"]
        assert rc_addr_from_argv(argv) == expected

    def test_unix_socket_rc_addr_is_out_of_scope(self):
        argv = ["rclone", "mount", "x:", "/m", "--rc",
                "--rc-addr=unix:///run/user/1000/rclone.sock"]
        assert rc_addr_from_argv(argv) is None


class TestUnitFromCgroup:
    def test_takes_the_last_service_component_not_the_first(self):
        # The naive first match yields user@1000.service, which is wrong.
        unit, user = unit_from_cgroup(USER_CGROUP)
        assert unit == "rclone-onedrive.service"
        assert user is True

    def test_system_unit_is_not_a_user_unit(self):
        unit, user = unit_from_cgroup(SYSTEM_CGROUP)
        assert unit == "rclone-backup.service"
        assert user is False

    def test_scope_without_a_service_has_no_unit(self):
        unit, user = unit_from_cgroup(NO_UNIT_CGROUP)
        assert unit is None
        assert user is False

    def test_bare_user_manager_is_not_a_unit(self):
        unit, _ = unit_from_cgroup("0::/user.slice/user-1000.slice/user@1000.service\n")
        assert unit is None

    def test_empty_input_is_handled(self):
        assert unit_from_cgroup("") == (None, False)

    def test_nested_scope_under_service_keeps_service(self):
        # A scope nested under a service should still extract the service.
        cgroup = (
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
            "rclone-onedrive.service/sub.scope\n"
        )
        unit, user = unit_from_cgroup(cgroup)
        assert unit == "rclone-onedrive.service"
        assert user is True


class TestDiscover:
    def test_finds_a_single_mount_with_everything(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "onedrive: /home/u/OneDrive fuse.rclone rw 0 0\n",
            {3280: ("rclone",
                    ["/usr/bin/rclone", "mount", "onedrive:", "/home/u/OneDrive",
                     "--rc", "--rc-addr=127.0.0.1:5572"],
                    USER_CGROUP)},
        )
        [m] = discover(proc)
        assert m.remote == "onedrive:"
        assert m.mountpoint == "/home/u/OneDrive"
        assert m.pid == 3280
        assert m.rc_addr == "127.0.0.1:5572"
        assert m.unit == "rclone-onedrive.service"
        assert m.user_unit is True

    def test_finds_several_mounts_with_different_ports(self, tmp_path):
        # --rc-addr alone starts nothing; --rc is what turns on the rc server
        # (F2), so both processes need it for this to exercise multi-port
        # discovery rather than the "no --rc" path.
        proc = make_proc(
            tmp_path,
            "onedrive: /home/u/OneDrive fuse.rclone rw 0 0\n"
            "gdrive: /home/u/Drive fuse.rclone rw 0 0\n",
            {
                10: ("rclone", ["rclone", "mount", "onedrive:", "/home/u/OneDrive",
                                "--rc", "--rc-addr=127.0.0.1:5572"], USER_CGROUP),
                11: ("rclone", ["rclone", "mount", "gdrive:", "/home/u/Drive",
                                "--rc", "--rc-addr=127.0.0.1:5580"], USER_CGROUP),
            },
        )
        found = {m.remote: m for m in discover(proc)}
        assert set(found) == {"onedrive:", "gdrive:"}
        assert found["onedrive:"].rc_addr == "127.0.0.1:5572"
        assert found["gdrive:"].rc_addr == "127.0.0.1:5580"

    def test_mount_without_rc_is_still_discovered(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "backup: /home/u/Backup fuse.rclone rw 0 0\n",
            {12: ("rclone", ["rclone", "mount", "backup:", "/home/u/Backup"],
                  USER_CGROUP)},
        )
        [m] = discover(proc)
        assert m.rc_addr is None
        assert m.pid == 12

    def test_system_owned_mount_is_reported_but_not_a_user_unit(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "backup: /srv/Backup fuse.rclone rw 0 0\n",
            {13: ("rclone", ["rclone", "mount", "backup:", "/srv/Backup", "--rc"],
                  SYSTEM_CGROUP)},
        )
        [m] = discover(proc)
        assert m.unit == "rclone-backup.service"
        assert m.user_unit is False

    def test_mount_with_no_owning_unit(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "x: /home/u/X fuse.rclone rw 0 0\n",
            {14: ("rclone", ["rclone", "mount", "x:", "/home/u/X"], NO_UNIT_CGROUP)},
        )
        [m] = discover(proc)
        assert m.unit is None
        assert m.user_unit is False

    def test_mount_whose_process_has_exited(self, tmp_path):
        # A stale /proc/mounts entry: still listed, but nothing owns it.
        proc = make_proc(tmp_path, "x: /home/u/X fuse.rclone rw 0 0\n", {})
        [m] = discover(proc)
        assert m.pid is None
        assert m.rc_addr is None
        assert m.unit is None

    def test_mountpoint_with_a_space(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "box: /home/u/My\\040Files fuse.rclone rw 0 0\n",
            {15: ("rclone", ["rclone", "mount", "box:", "/home/u/My Files", "--rc"],
                  USER_CGROUP)},
        )
        [m] = discover(proc)
        assert m.mountpoint == "/home/u/My Files"
        assert m.pid == 15

    def test_non_mount_rclone_processes_are_ignored(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "x: /home/u/X fuse.rclone rw 0 0\n",
            {
                16: ("rclone", ["rclone", "rcd", "--rc-addr=127.0.0.1:5599"], USER_CGROUP),
                17: ("rclone", ["rclone", "mount", "x:", "/home/u/X", "--rc"], USER_CGROUP),
            },
        )
        [m] = discover(proc)
        assert m.pid == 17, "matched the rcd daemon instead of the mount"

    def test_non_rclone_processes_are_not_read(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "x: /home/u/X fuse.rclone rw 0 0\n",
            {18: ("bash", ["bash", "-c", "mount x: /home/u/X"], USER_CGROUP)},
        )
        [m] = discover(proc)
        assert m.pid is None

    def test_no_rclone_mounts_yields_empty_list(self, tmp_path):
        proc = make_proc(tmp_path, "/dev/sda1 / ext4 rw 0 0\n", {})
        assert discover(proc) == []

    def test_missing_proc_root_is_not_an_error(self, tmp_path):
        assert discover(str(tmp_path / "nope")) == []

    def test_supports_cmount_and_mount2_subcommands(self, tmp_path):
        proc = make_proc(
            tmp_path,
            "x: /home/u/X fuse.rclone rw 0 0\n",
            {19: ("rclone", ["rclone", "cmount", "x:", "/home/u/X", "--rc"], USER_CGROUP)},
        )
        assert discover(proc)[0].pid == 19

    def test_misattribution_from_cache_dir(self, tmp_path):
        # A gdrive process with --cache-dir /home/u/OneDrive must not steal
        # the onedrive mount. This requires matching both remote and mountpoint.
        proc = make_proc(
            tmp_path,
            "onedrive: /home/u/OneDrive fuse.rclone rw 0 0\n"
            "gdrive: /home/u/Drive fuse.rclone rw 0 0\n",
            {
                100: ("rclone", ["rclone", "mount", "onedrive:", "/home/u/OneDrive",
                                 "--rc", "--rc-addr=127.0.0.1:5599"], USER_CGROUP),
                200: ("rclone", ["rclone", "mount", "gdrive:", "/home/u/Drive",
                                 "--cache-dir", "/home/u/OneDrive", "--rc",
                                 "--rc-addr=127.0.0.1:5572"],
                      USER_CGROUP),
            },
        )
        found = {m.remote: m for m in discover(proc)}
        assert found["onedrive:"].pid == 100
        assert found["onedrive:"].rc_addr == "127.0.0.1:5599"
        assert found["gdrive:"].pid == 200
        assert found["gdrive:"].rc_addr == "127.0.0.1:5572"

    def test_ambiguous_mountpoint_without_remote_fails_over(self, tmp_path):
        # Two processes both name the mountpoint; neither names the remote.
        # Fall back to None rather than guessing.
        proc = make_proc(
            tmp_path,
            "x: /home/u/X fuse.rclone rw 0 0\n",
            {
                30: ("rclone", ["rclone", "mount", "/home/u/X", "--rc"], USER_CGROUP),
                31: ("rclone", ["rclone", "mount", "/home/u/X", "--rc"], USER_CGROUP),
            },
        )
        [m] = discover(proc)
        assert m.pid is None

    def test_fallback_matches_on_mountpoint_alone_if_unique(self, tmp_path):
        # A device name that doesn't appear in argv (e.g., "unusual:") can
        # still match when exactly one process names the mountpoint.
        proc = make_proc(
            tmp_path,
            "unusual: /home/u/M fuse.rclone rw 0 0\n",
            {40: ("rclone", ["rclone", "mount", "/home/u/M", "--rc"], USER_CGROUP)},
        )
        [m] = discover(proc)
        assert m.pid == 40

    def test_flag_before_the_subcommand_is_skipped(self, tmp_path):
        # -v is a boolean flag with no value; the subcommand still follows it.
        proc = make_proc(
            tmp_path,
            "od: /home/u/OD fuse.rclone rw 0 0\n",
            {50: ("rclone", ["rclone", "-v", "mount", "od:", "/home/u/OD", "--rc"],
                  USER_CGROUP)},
        )
        [m] = discover(proc)
        assert m.pid == 50
        assert m.rc_addr == DEFAULT_RC_ADDR

    def test_value_taking_flag_before_the_subcommand_is_skipped(self, tmp_path):
        # --config takes a separate value; that value must not be mistaken
        # for the subcommand.
        proc = make_proc(
            tmp_path,
            "od: /home/u/OD fuse.rclone rw 0 0\n",
            {51: ("rclone", ["rclone", "--config", "/etc/rclone.conf", "mount",
                             "od:", "/home/u/OD", "--rc"], USER_CGROUP)},
        )
        [m] = discover(proc)
        assert m.pid == 51

    def test_mountpoint_with_a_trailing_slash_still_matches(self, tmp_path):
        # /proc/mounts reports the canonical path; argv may carry a trailing
        # slash. Compare after normpath, not as raw strings.
        proc = make_proc(
            tmp_path,
            "od: /home/u/OD fuse.rclone rw 0 0\n",
            {52: ("rclone", ["rclone", "mount", "od:", "/home/u/OD/", "--rc"],
                  USER_CGROUP)},
        )
        [m] = discover(proc)
        assert m.pid == 52

    def test_relative_mountpoint_resolves_against_process_cwd(self, tmp_path):
        # A mount started with a relative mountpoint argument is resolved
        # against /proc/<pid>/cwd, not matched as a literal relative string.
        proc = make_proc(
            tmp_path,
            "od: /home/u/OD fuse.rclone rw 0 0\n",
            {53: ("rclone", ["rclone", "mount", "od:", "OD", "--rc"],
                  USER_CGROUP, "/home/u")},
        )
        [m] = discover(proc)
        assert m.pid == 53


class TestMountIdentity:
    def test_key_ignores_pid_so_a_restart_keeps_its_card(self):
        a = Mount("x:", "/m", pid=1, rc_addr=None, unit=None, user_unit=False)
        b = Mount("x:", "/m", pid=2, rc_addr=None, unit=None, user_unit=False)
        assert a.key == b.key

    def test_mount_is_hashable(self):
        assert len({Mount("x:", "/m", None, None, None, False)}) == 1
