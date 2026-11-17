"""Tests for sensor normalization and endpoint merging (read_sensors.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

import pytest
from dirigera import EnvironmentSensor

from fakes import ALPSTUGA_ID, TIMMERFLOTE_ID, FakeHub, alpstuga_device, timmerflote_device
from read_sensors import (
    SensorReading,
    merge_sensor_readings,
    read_sensors,
    split_endpoint_id,
)

ReadingFactory = Callable[..., SensorReading]


def sensor_from(raw: dict) -> EnvironmentSensor:
    return EnvironmentSensor(dirigeraClient=FakeHub(), **raw)


class TestSplitEndpointId:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("abc_1", ("abc", 1)),
            ("abc_12", ("abc", 12)),
            ("a_b_2", ("a_b", 2)),
            ("abc", ("abc", None)),
            ("abc_0", ("abc_0", None)),  # endpoints start at 1
            ("abc_x", ("abc_x", None)),
        ],
    )
    def test_split(self, raw: str, expected: tuple[str, int | None]) -> None:
        assert split_endpoint_id(raw) == expected


class TestFromSensor:
    def test_alpstuga(self) -> None:
        reading = SensorReading.from_sensor(sensor_from(alpstuga_device()))

        assert reading.sensor_id == ALPSTUGA_ID
        assert reading.name == "Living room"
        assert reading.model == "ALPSTUGA air quality monitor"
        assert reading.room == "Living room"
        assert reading.reachable is True
        assert reading.temperature == 22.5
        assert reading.humidity == 41
        assert reading.co2 == 650
        assert reading.pm25 == 3
        assert reading.voc_index is None
        assert reading.battery_percentage is None
        assert reading.observed_at.tzinfo is timezone.utc

    def test_timmerflote(self) -> None:
        reading = SensorReading.from_sensor(sensor_from(timmerflote_device()))

        assert reading.sensor_id == TIMMERFLOTE_ID
        assert reading.co2 is None
        assert reading.battery_percentage == 87

    def test_strips_whitespace_and_handles_missing_room(self) -> None:
        raw = timmerflote_device(custom_name="  Bedroom  ", room=None)
        raw["attributes"]["model"] = " TIMMERFLOTE "

        reading = SensorReading.from_sensor(sensor_from(raw))

        assert reading.name == "Bedroom"
        assert reading.model == "TIMMERFLOTE"
        assert reading.room is None

    def test_name_falls_back_to_model(self) -> None:
        reading = SensorReading.from_sensor(
            sensor_from(timmerflote_device(custom_name=" "))
        )

        assert reading.name == "TIMMERFLOTE temp/hmd sensor"

    def test_unreachable(self) -> None:
        reading = SensorReading.from_sensor(
            sensor_from(timmerflote_device(reachable=False))
        )

        assert reading.reachable is False


class TestMeasurementFields:
    def test_omits_missing_values_and_reachable(
        self, make_reading: ReadingFactory
    ) -> None:
        reading = make_reading(co2=500, reachable=False)

        assert reading.measurement_fields() == {
            "temperature": 21.0,
            "humidity": 45,
            "co2": 500,
        }


class TestDisplayLine:
    def test_all_values(self, make_reading: ReadingFactory) -> None:
        line = make_reading(
            room="Office",
            co2=650,
            pm25=3,
            voc_index=100,
            battery_percentage=87,
        ).display_line()

        assert line == (
            "Sensor [sensor-1, online, room=Office]: 21.0 °C, 45 % RH, "
            "CO₂ 650 ppm, PM2.5 3 µg/m³, VOC index 100, battery 87 %"
        )

    def test_offline_without_values(self, make_reading: ReadingFactory) -> None:
        line = make_reading(
            reachable=False, temperature=None, humidity=None
        ).display_line()

        assert line == "Sensor [sensor-1, offline]: no measurements available"


class TestMergeSensorReadings:
    def test_merges_endpoints_with_priority(self, make_reading: ReadingFactory) -> None:
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        readings = [
            make_reading(
                sensor_id="dev_2",
                name="Second",
                temperature=None,
                pm25=7,
                reachable=False,
                observed_at=t0 + timedelta(seconds=1),
            ),
            make_reading(
                sensor_id="dev_1",
                name="Primary",
                temperature=20.0,
                pm25=None,
                reachable=True,
                observed_at=t0,
            ),
            make_reading(sensor_id="other", name="Other"),
        ]

        merged = merge_sensor_readings(readings)

        assert [reading.sensor_id for reading in merged] == ["dev", "other"]
        device = merged[0]
        assert device.name == "Primary"  # endpoint _1 wins
        assert device.temperature == 20.0
        assert device.pm25 == 7  # filled from the next endpoint
        assert device.reachable is True  # any endpoint reachable
        assert device.observed_at == t0 + timedelta(seconds=1)

    def test_unreachable_when_all_endpoints_unreachable(
        self, make_reading: ReadingFactory
    ) -> None:
        merged = merge_sensor_readings(
            [
                make_reading(sensor_id="dev_1", reachable=False),
                make_reading(sensor_id="dev_2", reachable=False),
            ]
        )

        assert merged[0].reachable is False

    def test_unsuffixed_id_ranks_between_first_and_other_endpoints(
        self, make_reading: ReadingFactory
    ) -> None:
        merged = merge_sensor_readings(
            [
                make_reading(sensor_id="dev_3", name="Third"),
                make_reading(sensor_id="dev", name="Plain"),
            ]
        )

        assert merged[0].name == "Plain"

    def test_single_endpoint_is_normalized(self, make_reading: ReadingFactory) -> None:
        merged = merge_sensor_readings([make_reading(sensor_id="dev_1")])

        assert merged[0].sensor_id == "dev"


def test_read_sensors_merges_hub_endpoints() -> None:
    hub = FakeHub(
        [
            alpstuga_device(),
            # Second ALPSTUGA endpoint reporting only PM2.5.
            {
                **alpstuga_device(),
                "id": "alpstuga-0001_2",
            },
            timmerflote_device(),
        ]
    )

    readings = read_sensors(hub)

    assert [reading.sensor_id for reading in readings] == [
        "alpstuga-0001",
        TIMMERFLOTE_ID,
    ]
