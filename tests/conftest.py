"""Shared fixtures: a clean environment, a fake hub and reading factory."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable

import pytest

from fakes import APP_ENV_PREFIXES, FakeHub, alpstuga_device, timmerflote_device
from dirigera_sensor_collector.sensors import SensorReading


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all application environment variables for every test."""
    for name in list(os.environ):
        if name.startswith(APP_ENV_PREFIXES):
            monkeypatch.delenv(name)


@pytest.fixture
def fake_hub() -> FakeHub:
    """Hub with one ALPSTUGA and one TIMMERFLOTE."""
    return FakeHub([alpstuga_device(), timmerflote_device()])


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimal valid configuration."""
    monkeypatch.setenv("DIRIGERA_IP", "192.0.2.10")
    monkeypatch.setenv("DIRIGERA_TOKEN", "test-token")


@pytest.fixture
def make_reading() -> Callable[..., SensorReading]:
    """Factory for ``SensorReading`` with sensible defaults."""

    def factory(**overrides: Any) -> SensorReading:
        values: dict[str, Any] = {
            "sensor_id": "sensor-1",
            "name": "Sensor",
            "model": "MODEL",
            "room": None,
            "reachable": True,
            "temperature": 21.0,
            "humidity": 45,
            "co2": None,
            "pm25": None,
            "voc_index": None,
            "battery_percentage": None,
            "observed_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        values.update(overrides)
        return SensorReading(**values)

    return factory
