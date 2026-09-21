"""Lifecycle of the tray's systemd user unit.

GUI-free, like probe.py, so the window, the tray and the tests all share it.
systemctl is the single source of truth: `is-enabled` answers "does it start
at login", `is-active` answers "is it running now". Nothing here scans the
process list, which cannot distinguish a real tray from a grep of its name.

Every call reports failure as a value rather than raising, so a missing or
broken systemd cannot take a UI down with it.
"""

from __future__ import annotations

import pathlib
import subprocess
from dataclasses import dataclass

UNIT_NAME = "rclone-status-tray.service"

# systemctl is-enabled words that mean "will start at login".
ENABLED_WORDS = frozenset({"enabled", "enabled-runtime", "linked", "linked-runtime"})
# is-active words that mean "running, or on its way there".
ACTIVE_WORDS = frozenset({"active", "activating"})


def unit_path() -> pathlib.Path:
    return pathlib.Path.home() / ".config/systemd/user" / UNIT_NAME


def unit_template_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / "data" / UNIT_NAME


def parse_enabled(output: str) -> bool:
    return output.strip() in ENABLED_WORDS


def parse_active(output: str) -> bool:
    return output.strip() in ACTIVE_WORDS


@dataclass
class TrayState:
    """What the UI needs to render the toggle."""

    installed: bool = False
    enabled: bool = False
    active: bool = False
    available: bool = True
    detail: str = ""

    @property
    def usable(self) -> bool:
        """Whether the toggle should accept input at all."""
        return self.available and self.installed


def _run_systemctl(args, timeout=5):
    """Run `systemctl --user <args>`; return (returncode, stdout+stderr).

    Replaced wholesale in tests, so it stays the only process boundary.
    """
    proc = subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def is_installed() -> bool:
    return unit_path().is_file()


def state() -> TrayState:
    if not is_installed():
        return TrayState(
            installed=False,
            detail="Tray service not installed. Run install.sh.",
        )
    try:
        _, enabled_out = _run_systemctl(["is-enabled", UNIT_NAME])
        _, active_out = _run_systemctl(["is-active", UNIT_NAME])
    except (OSError, subprocess.SubprocessError) as exc:
        return TrayState(
            installed=True, available=False,
            detail=f"systemd unavailable: {exc}",
        )

    # systemctl exits non-zero for disabled/inactive, so the printed word is
    # the signal, not the return code.
    enabled = parse_enabled(enabled_out)
    active = parse_active(active_out)

    if enabled and active:
        detail = "Running · starts at login"
    elif enabled:
        detail = "Starts at login · not running"
    elif active:
        detail = "Running · will not start at login"
    else:
        detail = "Not running"

    return TrayState(installed=True, enabled=enabled, active=active, detail=detail)


def _toggle(verb: str, stop: bool) -> tuple[bool, str]:
    args = [verb]
    if stop:
        args.append("--now")
    args.append(UNIT_NAME)
    try:
        code, out = _run_systemctl(args)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run systemctl: {exc}"
    if code != 0:
        return False, out.strip() or f"systemctl {verb} failed"
    return True, ""


def enable(start: bool = True) -> tuple[bool, str]:
    return _toggle("enable", start)


def disable(stop: bool = True) -> tuple[bool, str]:
    """Disable the unit. stop=False lets a self-disabling tray exit cleanly."""
    return _toggle("disable", stop)


def install_unit(exec_path: str) -> tuple[bool, str]:
    """Write the unit with ExecStart pointing at this checkout, then reload."""
    template = unit_template_path()
    if not template.is_file():
        return False, f"missing unit template: {template}"

    lines = []
    for line in template.read_text().splitlines():
        if line.startswith("ExecStart="):
            line = f"ExecStart={exec_path}"
        lines.append(line)

    target = unit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n")

    try:
        _run_systemctl(["daemon-reload"])
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"unit written but daemon-reload failed: {exc}"
    return True, ""
