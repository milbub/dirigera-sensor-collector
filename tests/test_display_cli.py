"""Tests for the alpstuga-display command (display_cli.py)."""

from __future__ import annotations

import pytest

from dirigera_sensor_collector import display_cli
from dirigera_sensor_collector.display_cli import parse_display_state
from fakes import ALPSTUGA_ID, FakeHub


class TestParseDisplayState:
    @pytest.mark.parametrize("raw", ["on", "ON", " true ", "1"])
    def test_parse_display_state_on(self, raw: str) -> None:
        assert parse_display_state(raw) is True

    @pytest.mark.parametrize("raw", ["off", "False", "0"])
    def test_parse_display_state_off(self, raw: str) -> None:
        assert parse_display_state(raw) is False

    def test_parse_display_state_invalid(self) -> None:
        with pytest.raises(ValueError, match="Display state"):
            parse_display_state("maybe")


class TestCli:
    def test_usage_without_argument(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("sys.argv", ["alpstuga_display.py"])

        assert display_cli.main() == 2
        assert "Usage" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("env", "message"),
        [
            ({}, "DIRIGERA_IP is empty"),
            ({"DIRIGERA_IP": "192.0.2.10"}, "DIRIGERA_TOKEN"),
            (
                {"DIRIGERA_IP": "192.0.2.10", "DIRIGERA_TOKEN": "t"},
                "ALPSTUGA_DISPLAY_CONTROL_IDS",
            ),
        ],
    )
    def test_missing_configuration(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        env: dict[str, str],
        message: str,
    ) -> None:
        monkeypatch.setattr("sys.argv", ["alpstuga_display.py", "on"])
        for name, value in env.items():
            monkeypatch.setenv(name, value)

        assert display_cli.main() == 2
        assert message in caplog.text

    def test_switches_displays(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_hub: FakeHub,
    ) -> None:
        monkeypatch.setattr("sys.argv", ["alpstuga_display.py", "off"])
        monkeypatch.setenv("DIRIGERA_IP", "192.0.2.10")
        monkeypatch.setenv("DIRIGERA_TOKEN", "t")
        monkeypatch.setenv("ALPSTUGA_DISPLAY_CONTROL_IDS", ALPSTUGA_ID)
        monkeypatch.setattr(display_cli, "Hub", lambda **_kwargs: fake_hub)

        assert display_cli.main() == 0
        assert fake_hub.devices[ALPSTUGA_ID]["attributes"]["isOn"] is False
