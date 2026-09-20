"""Tests for the tray unit's lifecycle layer.

The systemctl calls are injected, so none of this touches real units.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from odstatus import service


@pytest.fixture
def fake(monkeypatch):
    """Replace the systemctl runner with a scriptable recorder."""

    class Fake:
        def __init__(self):
            self.calls = []
            self.responses = {}
            self.available = True

        def respond(self, verb, returncode, stdout=""):
            self.responses[verb] = (returncode, stdout)

        def __call__(self, args, timeout=5):
            self.calls.append(list(args))
            if not self.available:
                raise FileNotFoundError("systemctl")
            verb = args[0] if args else ""
            return self.responses.get(verb, (0, ""))

        def verbs(self):
            return [c[0] for c in self.calls]

    f = Fake()
    monkeypatch.setattr(service, "_run_systemctl", f)
    return f


class TestEnabledParsing:
    @pytest.mark.parametrize("out,expected", [
        ("enabled", True),
        ("enabled-runtime", True),
        ("linked", True),
        ("disabled", False),
        ("static", False),
        ("masked", False),
        ("not-found", False),
        ("", False),
    ])
    def test_parses_is_enabled_output(self, out, expected):
        assert service.parse_enabled(out) is expected

    def test_tolerates_trailing_newline_and_spaces(self):
        assert service.parse_enabled("  enabled \n") is True


class TestActiveParsing:
    @pytest.mark.parametrize("out,expected", [
        ("active", True),
        ("activating", True),
        ("inactive", False),
        ("failed", False),
        ("deactivating", False),
        ("unknown", False),
        ("", False),
    ])
    def test_parses_is_active_output(self, out, expected):
        assert service.parse_active(out) is expected


class TestState:
    def test_reports_enabled_and_active(self, fake, monkeypatch):
        monkeypatch.setattr(service, "is_installed", lambda: True)
        fake.respond("is-enabled", 0, "enabled\n")
        fake.respond("is-active", 0, "active\n")
        st = service.state()
        assert st.installed and st.enabled and st.active and st.available

    def test_reports_disabled_and_inactive(self, fake, monkeypatch):
        monkeypatch.setattr(service, "is_installed", lambda: True)
        # systemctl exits non-zero for disabled/inactive; the word is the truth.
        fake.respond("is-enabled", 1, "disabled\n")
        fake.respond("is-active", 3, "inactive\n")
        st = service.state()
        assert st.installed and not st.enabled and not st.active

    def test_enabled_but_not_running_is_representable(self, fake, monkeypatch):
        monkeypatch.setattr(service, "is_installed", lambda: True)
        fake.respond("is-enabled", 0, "enabled\n")
        fake.respond("is-active", 3, "inactive\n")
        st = service.state()
        assert st.enabled and not st.active

    def test_missing_unit_is_not_installed(self, fake, monkeypatch):
        monkeypatch.setattr(service, "is_installed", lambda: False)
        st = service.state()
        assert not st.installed
        assert st.detail

    def test_missing_systemctl_is_unavailable_not_a_crash(self, fake, monkeypatch):
        monkeypatch.setattr(service, "is_installed", lambda: True)
        fake.available = False
        st = service.state()
        assert st.available is False
        assert st.detail


class TestToggle:
    def test_enable_uses_enable_now(self, fake):
        service.enable()
        assert fake.calls[0][:3] == ["enable", "--now", service.UNIT_NAME]

    def test_disable_uses_disable_now(self, fake):
        service.disable()
        assert fake.calls[0][:3] == ["disable", "--now", service.UNIT_NAME]

    def test_disable_without_stopping_omits_now(self, fake):
        # The tray disables itself, then exits on its own terms.
        service.disable(stop=False)
        assert "--now" not in fake.calls[0]
        assert fake.calls[0][:2] == ["disable", service.UNIT_NAME]

    def test_enable_reports_failure_instead_of_raising(self, fake):
        fake.respond("enable", 1, "Failed to enable unit")
        ok, message = service.enable()
        assert ok is False
        assert "Failed" in message

    def test_enable_reports_success(self, fake):
        fake.respond("enable", 0, "")
        ok, _ = service.enable()
        assert ok is True

    def test_toggle_survives_missing_systemctl(self, fake):
        fake.available = False
        ok, message = service.enable()
        assert ok is False
        assert message


class TestUnitInstall:
    def test_writes_unit_with_the_given_exec_path(self, tmp_path, fake, monkeypatch):
        monkeypatch.setattr(service, "unit_path", lambda: tmp_path / service.UNIT_NAME)
        template = tmp_path / "template.service"
        template.write_text(
            "[Service]\nExecStart=/usr/local/bin/onedrive-status-tray\n"
        )
        monkeypatch.setattr(service, "unit_template_path", lambda: template)

        service.install_unit("/opt/app/bin/onedrive-status-tray")

        written = (tmp_path / service.UNIT_NAME).read_text()
        assert "ExecStart=/opt/app/bin/onedrive-status-tray" in written
        assert "/usr/local/bin" not in written

    def test_reloads_the_daemon_after_writing(self, tmp_path, fake, monkeypatch):
        monkeypatch.setattr(service, "unit_path", lambda: tmp_path / service.UNIT_NAME)
        template = tmp_path / "template.service"
        template.write_text("[Service]\nExecStart=/usr/local/bin/onedrive-status-tray\n")
        monkeypatch.setattr(service, "unit_template_path", lambda: template)

        service.install_unit("/opt/app/bin/onedrive-status-tray")
        assert "daemon-reload" in fake.verbs()


class TestRealUnitTemplate:
    """The committed template must be a valid, installable unit."""

    def test_template_exists(self):
        assert service.unit_template_path().is_file()

    def test_template_has_placeholder_exec_and_install_section(self):
        text = service.unit_template_path().read_text()
        assert "ExecStart=" in text
        assert "[Install]" in text
        assert "WantedBy=" in text

    def test_template_hardcodes_no_home_directory(self):
        assert "/home/" not in service.unit_template_path().read_text()
