"""The launcher scripts must work from any working directory.

Regression test: the scripts originally ran `python3 -m odstatus.window`
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
LAUNCHERS = ["onedrive-status", "onedrive-status-tray"]


def run_briefly(cmd, cwd, seconds=4):
    """Start a GUI process, let it settle, then stop it.

    Returns (early_exit_code_or_None, output). A launcher that cannot import
    its package dies immediately; a healthy one is still running at the end.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
def test_launcher_starts_from_any_directory(name, cwd):
    code, out = run_briefly([str(REPO / "bin" / name)], cwd=cwd)
    assert "ModuleNotFoundError" not in out, out
    assert "Traceback" not in out, out
    # None means it was still alive when we stopped it, which is success.
    assert code is None, f"exited early ({code}) from {cwd}: {out}"


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launcher_works_through_a_symlink(name, tmp_path):
    link = tmp_path / name
    link.symlink_to(REPO / "bin" / name)
    code, out = run_briefly([str(link)], cwd="/tmp")
    assert "ModuleNotFoundError" not in out, out
    assert code is None, f"exited early ({code}) via symlink: {out}"


class TestDesktopEntries:
    """The installed entries must point at launchers that actually exist."""

    @pytest.mark.parametrize("filename", [
        os.path.expanduser("~/.local/share/applications/dev.matheoatche.OneDriveStatus.desktop"),
        os.path.expanduser("~/.config/autostart/onedrive-status-tray.desktop"),
    ])
    def test_exec_path_exists_and_is_executable(self, filename):
        path = pathlib.Path(filename)
        if not path.exists():
            pytest.skip(f"{path} not installed")
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path)
        exec_line = parser["Desktop Entry"]["Exec"].split()[0]
        assert os.path.isfile(exec_line), f"Exec target missing: {exec_line}"
        assert os.access(exec_line, os.X_OK), f"Exec target not executable: {exec_line}"
