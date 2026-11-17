"""Tests for InfluxDB point creation, dry-run and writing (read_sensors.py)."""

from __future__ import annotations

from typing import Any, Callable

import pytest

from read_sensors import InfluxOutput, SensorReading, Settings

ReadingFactory = Callable[..., SensorReading]


class RecordingWriteApi:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def write(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)


class RecordingClient:
    """Stand-in for ``influxdb_client.InfluxDBClient``."""

    instances: list["RecordingClient"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.write_api_instance = RecordingWriteApi()
        self.closed = False
        RecordingClient.instances.append(self)

    def write_api(self, **_kwargs: Any) -> RecordingWriteApi:
        return self.write_api_instance

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def recording_client(monkeypatch: pytest.MonkeyPatch) -> type[RecordingClient]:
    RecordingClient.instances = []
    monkeypatch.setattr("influxdb_client.InfluxDBClient", RecordingClient)
    return RecordingClient


def settings_with(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("DIRIGERA_IP", "192.0.2.10")
    monkeypatch.setenv("DIRIGERA_TOKEN", "t")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings.from_environment()


def test_disabled_output_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
    make_reading: ReadingFactory,
    recording_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    influx = InfluxOutput(settings_with(monkeypatch))

    influx.process([make_reading()])

    assert influx.active is False
    assert recording_client.instances == []
    assert capsys.readouterr().out == ""


def test_dry_run_prints_line_protocol_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
    make_reading: ReadingFactory,
    recording_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    influx = InfluxOutput(settings_with(monkeypatch, INFLUX_DRY_RUN="true"))

    influx.process(
        [
            make_reading(
                sensor_id="dev",
                name="Living room",
                room="Living room",
                temperature=21.5,
                co2=650,
            )
        ]
    )

    assert influx.dry_run is True
    assert influx.enabled is False
    assert recording_client.instances == []
    assert capsys.readouterr().out == (
        "INFLUX RAW DATA: ikea_environment_sensor,model=MODEL,"
        "name=Living\\ room,room=Living\\ room,sensor_id=dev "
        "co2=650i,humidity=45i,temperature=21.5 1767225600000000000\n"
    )


def test_point_has_no_reachable_field(
    monkeypatch: pytest.MonkeyPatch, make_reading: ReadingFactory
) -> None:
    influx = InfluxOutput(settings_with(monkeypatch, INFLUX_DRY_RUN="true"))

    line = influx.create_point(make_reading()).to_line_protocol()

    assert "reachable" not in line


def test_ignored_fields_and_tags(
    monkeypatch: pytest.MonkeyPatch, make_reading: ReadingFactory
) -> None:
    influx = InfluxOutput(
        settings_with(
            monkeypatch,
            INFLUX_DRY_RUN="true",
            INFLUX_MEASUREMENT="env",
            INFLUX_IGNORED_FIELDS="humidity",
            INFLUX_IGNORED_TAGS="model,name",
        )
    )

    line = influx.create_point(make_reading(temperature=21.5)).to_line_protocol()

    assert line == "env,sensor_id=sensor-1 temperature=21.5 1767225600000000000"


def test_point_skipped_when_no_fields_remain(
    monkeypatch: pytest.MonkeyPatch,
    make_reading: ReadingFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    influx = InfluxOutput(
        settings_with(
            monkeypatch,
            INFLUX_DRY_RUN="true",
            INFLUX_IGNORED_FIELDS="temperature,humidity",
        )
    )

    assert influx.create_point(make_reading()) is None
    assert "no fields remain" in caplog.text


def test_unknown_ignored_names_are_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    InfluxOutput(
        settings_with(
            monkeypatch,
            INFLUX_IGNORED_FIELDS="reachable,temperature",
            INFLUX_IGNORED_TAGS="location",
        )
    )

    assert "INFLUX_IGNORED_FIELDS contains unknown fields: reachable" in caplog.text
    assert "INFLUX_IGNORED_TAGS contains unknown tags: location" in caplog.text


def test_writes_points_to_influxdb(
    monkeypatch: pytest.MonkeyPatch,
    make_reading: ReadingFactory,
    recording_client: type[RecordingClient],
) -> None:
    influx = InfluxOutput(
        settings_with(
            monkeypatch,
            INFLUX_ENABLED="true",
            INFLUX_URL="http://influx:8086",
            INFLUX_TOKEN="secret",
            INFLUX_ORG="home",
            INFLUX_BUCKET="env",
        )
    )

    influx.process([make_reading(sensor_id="a"), make_reading(sensor_id="b")])
    influx.close()

    [client] = recording_client.instances
    assert client.kwargs == {
        "url": "http://influx:8086",
        "token": "secret",
        "org": "home",
    }
    [write] = client.write_api_instance.writes
    assert write["bucket"] == "env"
    assert write["org"] == "home"
    assert len(write["record"]) == 2
    assert client.closed is True


def test_empty_readings_are_not_written(
    monkeypatch: pytest.MonkeyPatch,
    recording_client: type[RecordingClient],
) -> None:
    influx = InfluxOutput(
        settings_with(
            monkeypatch,
            INFLUX_ENABLED="true",
            INFLUX_TOKEN="secret",
            INFLUX_ORG="home",
        )
    )

    influx.process([])

    assert recording_client.instances[0].write_api_instance.writes == []
