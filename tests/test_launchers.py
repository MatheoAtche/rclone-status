"""The launcher scripts must work from any working directory.

Regression test: the scripts originally ran `python3 -m rcstatus.window`
with no PYTHONPATH, which only worked when the caller happened to be inside
the repo. Launched from $HOME or by the desktop entry, they died with
ModuleNotFoundError.
"""

import configparser
import os
import pathlib
import signal
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
LAUNCHERS = ["rclone-status", "rclone-status-tray"]


def isolated_env(runtime_dir):
    """An env whose tray lock is private to one test.

    Without this a real tray running on the developer's desktop holds the
    lock, and every launcher test sees "already running".
    """
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    return env


def run_briefly(cmd, cwd=None, seconds=4, env=None):
    """Start a GUI process, let it settle, then stop it.

    Returns (early_exit_code_or_None, output). A launcher that cannot import
    its package dies immediately; a healthy one is still running at the end.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        out = proc.communicate(timeout=seconds)[0]
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        out = proc.communicate()[0]
        return None, out


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launcher_is_executable(name):
    assert os.access(REPO / "bin" / name, os.X_OK)


@pytest.mark.parametrize("name", LAUNCHERS)
@pytest.mark.parametrize("cwd", ["/", os.path.expanduser("~"), "/tmp"])
def test_launcher_starts_from_any_directory(name, cwd, tmp_path):
    """The regression this guards: no PYTHONPATH meant ModuleNotFoundError
    anywhere outside the repo."""
    code, out = run_briefly(
        [str(REPO / "bin" / name)], cwd=cwd, env=isolated_env(tmp_path)
    )
    assert "ModuleNotFoundError" not in out, out
    assert "Traceback" not in out, out
    # A clean exit is legitimate (GApplication hands off to a running window);
    # a non-zero exit is not.
    assert code in (None, 0), f"exited {code} from {cwd}: {out}"


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launcher_works_through_a_symlink(name, tmp_path):
    link = tmp_path / name
    link.symlink_to(REPO / "bin" / name)
    code, out = run_briefly([str(link)], cwd="/tmp", env=isolated_env(tmp_path))
    assert "ModuleNotFoundError" not in out, out
    assert code in (None, 0), f"exited {code} via symlink: {out}"


class TestInstalledEntries:
    """Installed entries must point at launchers that actually exist."""

    def test_app_desktop_exec_is_executable(self):
        path = pathlib.Path(os.path.expanduser(
            "~/.local/share/applications/dev.matheoatche.RcloneStatus.desktop"))
        if not path.exists():
            pytest.skip(f"{path} not installed")
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path)
        target = parser["Desktop Entry"]["Exec"].split()[0]
        assert os.path.isfile(target), f"Exec target missing: {target}"
        assert os.access(target, os.X_OK), f"Exec target not executable: {target}"

    def test_tray_unit_execstart_is_executable(self):
        path = pathlib.Path(os.path.expanduser(
            "~/.config/systemd/user/rclone-status-tray.service"))
        if not path.exists():
            pytest.skip(f"{path} not installed")
        line = next(l for l in path.read_text().splitlines()
                    if l.startswith("ExecStart="))
        target = line.split("=", 1)[1].split()[0]
        assert os.path.isfile(target), f"ExecStart target missing: {target}"
        assert os.access(target, os.X_OK), f"ExecStart not executable: {target}"

    def test_old_autostart_entry_is_gone(self):
        # It would start a second tray beside the unit.
        stale = pathlib.Path(os.path.expanduser(
            "~/.config/autostart/rclone-status-tray.desktop"))
        assert not stale.exists(), f"stale autostart entry: {stale}"


class TestSingleInstance:
    """Only one tray may run: two icons for one machine is a confusing bug,
    and the enable/disable toggle makes it easy to trigger."""

    def test_second_instance_exits_instead_of_duplicating(self, tmp_path):
        env = isolated_env(tmp_path)
        first = subprocess.Popen(
            [str(REPO / "bin" / "rclone-status-tray")], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
        try:
            import time
            time.sleep(4)  # let the first take the lock
            assert first.poll() is None, "first instance died unexpectedly"

            second = subprocess.run(
                [str(REPO / "bin" / "rclone-status-tray")], env=env,
                capture_output=True, text=True, timeout=15,
            )
            assert second.returncode == 0, second.stdout + second.stderr
            assert "already running" in (second.stdout + second.stderr).lower()
        finally:
            os.killpg(os.getpgid(first.pid), signal.SIGTERM)
            first.wait(timeout=10)

    def test_lock_is_released_so_a_later_instance_can_start(self, tmp_path):
        env = isolated_env(tmp_path)
        for attempt in range(2):
            code, out = run_briefly(
                [str(REPO / "bin" / "rclone-status-tray")], env=env
            )
            assert "already running" not in out.lower(), f"attempt {attempt}: {out}"
            assert code is None, f"attempt {attempt} exited early: {out}"
