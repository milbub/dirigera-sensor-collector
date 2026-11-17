"""Tests for configuration loading (read_sensors.Settings and env helpers)."""

from __future__ import annotations

from datetime import time

import pytest

from dirigera_sensor_collector.config import (
    Settings,
    env_bool,
    env_csv_set,
    env_float,
    env_int,
)


class TestEnvHelpers:
    @pytest.mark.parametrize("raw", ["1", "true", " YES ", "On"])
    def test_env_bool_true(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("FLAG", raw)
        assert env_bool("FLAG") is True

    @pytest.mark.parametrize("raw", ["0", "false", "No", "off"])
    def test_env_bool_false(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("FLAG", raw)
        assert env_bool("FLAG", default=True) is False

    def test_env_bool_default_and_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FLAG", raising=False)
        assert env_bool("FLAG", default=True) is True

        monkeypatch.setenv("FLAG", "maybe")
        with pytest.raises(ValueError, match="FLAG must be one of"):
            env_bool("FLAG")

    def test_env_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NUM", raising=False)
        assert env_float("NUM", default=1.5) == 1.5

        monkeypatch.setenv("NUM", " ")
        assert env_float("NUM", default=1.5) == 1.5

        monkeypatch.setenv("NUM", "2.25")
        assert env_float("NUM", default=1.5) == 2.25

        monkeypatch.setenv("NUM", "abc")
        with pytest.raises(ValueError, match="NUM must be a number"):
            env_float("NUM", default=1.5)

    def test_env_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NUM", "3")
        assert env_int("NUM", default=1) == 3

        monkeypatch.setenv("NUM", "3.5")
        with pytest.raises(ValueError, match="NUM must be an integer"):
            env_int("NUM", default=1)

    def test_env_csv_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIST", " a, b ,,a ")
        assert env_csv_set("LIST") == frozenset({"a", "b"})

        monkeypatch.delenv("LIST")
        assert env_csv_set("LIST") == frozenset()


@pytest.mark.usefixtures("base_env")
class TestSettings:
    def test_defaults(self) -> None:
        settings = Settings.from_environment()

        assert settings.dirigera_ip == "192.0.2.10"
        assert settings.dirigera_token == "test-token"
        assert settings.poll_interval_seconds == 10.0
        assert settings.influx_enabled is False
        assert settings.influx_dry_run is False
        assert settings.influx_url == "http://localhost:8086"
        assert settings.influx_bucket == "sensors"
        assert settings.influx_measurement == "ikea_environment_sensor"
        assert settings.influx_ignored_fields == frozenset()
        assert settings.influx_recovery_hold_seconds == 1800.0
        assert settings.influx_recovery_hold_min_unreachable_samples == 2
        assert settings.alpstuga_display_control_ids == ()
        assert settings.alpstuga_display_control_off_time is None

    @pytest.mark.parametrize("name", ["DIRIGERA_IP", "DIRIGERA_TOKEN"])
    def test_required_dirigera_values(
        self, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        monkeypatch.setenv(name, " ")

        with pytest.raises(ValueError, match=f"{name} is empty"):
            Settings.from_environment()

    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_poll_interval_must_be_positive(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("POLL_INTERVAL_SECONDS", value)

        with pytest.raises(ValueError, match="POLL_INTERVAL_SECONDS"):
            Settings.from_environment()

    def test_influx_enabled_requires_connection_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INFLUX_ENABLED", "true")

        with pytest.raises(ValueError, match="INFLUX_TOKEN, INFLUX_ORG"):
            Settings.from_environment()

    def test_influx_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INFLUX_ENABLED", "true")
        monkeypatch.setenv("INFLUX_TOKEN", "t")
        monkeypatch.setenv("INFLUX_ORG", "home")

        settings = Settings.from_environment()

        assert settings.influx_enabled is True
        assert settings.influx_dry_run is False

    def test_dry_run_overrides_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("INFLUX_ENABLED", "true")
        monkeypatch.setenv("INFLUX_DRY_RUN", "true")

        # No token/org needed: dry-run never connects.
        settings = Settings.from_environment()

        assert settings.influx_enabled is False
        assert settings.influx_dry_run is True
        assert "overrides INFLUX_ENABLED" in caplog.text

    def test_empty_measurement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INFLUX_MEASUREMENT", " ")

        with pytest.raises(ValueError, match="INFLUX_MEASUREMENT"):
            Settings.from_environment()

    def test_recovery_hold_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INFLUX_RECOVERY_HOLD_SECONDS", "0")
        monkeypatch.setenv("INFLUX_RECOVERY_HOLD_MIN_UNREACHABLE_SAMPLES", "1")

        settings = Settings.from_environment()

        assert settings.influx_recovery_hold_seconds == 0
        assert settings.influx_recovery_hold_min_unreachable_samples == 1

    @pytest.mark.parametrize(
        ("name", "value", "message"),
        [
            ("INFLUX_RECOVERY_HOLD_SECONDS", "-1", "must not be negative"),
            ("INFLUX_RECOVERY_HOLD_SECONDS", "soon", "must be a number"),
            ("INFLUX_RECOVERY_HOLD_MIN_UNREACHABLE_SAMPLES", "0", "at least 1"),
        ],
    )
    def test_invalid_recovery_hold_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        value: str,
        message: str,
    ) -> None:
        monkeypatch.setenv(name, value)

        with pytest.raises(ValueError, match=message):
            Settings.from_environment()

    def test_display_schedule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALPSTUGA_DISPLAY_CONTROL_IDS", "a_1, b_1")
        monkeypatch.setenv("ALPSTUGA_DISPLAY_CONTROL_OFF_TIME", "22:00:00")
        monkeypatch.setenv("ALPSTUGA_DISPLAY_CONTROL_ON_TIME", "06:30:00")

        settings = Settings.from_environment()

        assert settings.alpstuga_display_control_ids == ("a_1", "b_1")
        assert settings.alpstuga_display_control_off_time == time(22)
        assert settings.alpstuga_display_control_on_time == time(6, 30)

    def test_display_schedule_requires_times(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALPSTUGA_DISPLAY_CONTROL_IDS", "a_1")
        monkeypatch.setenv("ALPSTUGA_DISPLAY_CONTROL_OFF_TIME", "22:00:00")

        with pytest.raises(ValueError, match="ALPSTUGA_DISPLAY_CONTROL_ON_TIME"):
            Settings.from_environment()

    def test_display_schedule_times_must_differ(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALPSTUGA_DISPLAY_CONTROL_IDS", "a_1")
        monkeypatch.setenv("ALPSTUGA_DISPLAY_CONTROL_OFF_TIME", "06:00:00")
        monkeypatch.setenv("ALPSTUGA_DISPLAY_CONTROL_ON_TIME", "06:00:00")

        with pytest.raises(ValueError, match="must be different"):
            Settings.from_environment()

    def test_display_times_ignored_without_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALPSTUGA_DISPLAY_CONTROL_OFF_TIME", "invalid")

        settings = Settings.from_environment()

        assert settings.alpstuga_display_control_off_time is None
