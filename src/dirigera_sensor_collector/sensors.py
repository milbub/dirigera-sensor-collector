"""Normalize DIRIGERA environment sensors and merge logical endpoints."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from dirigera import EnvironmentSensor, Hub


LOGGER = logging.getLogger("dirigera-sensors")


# DIRIGERA may expose one physical device as multiple logical endpoints:
#
#   <physical-device-id>_1
#   <physical-device-id>_2
#
# The endpoint suffix is removed from the normalized physical device ID.
ENDPOINT_ID_PATTERN = re.compile(
    r"^(?P<device_id>.+)_(?P<endpoint>[1-9]\d*)$"
)


def split_endpoint_id(sensor_id: str) -> tuple[str, int | None]:
    """Split a logical endpoint ID into physical ID and endpoint number."""
    match = ENDPOINT_ID_PATTERN.fullmatch(sensor_id)

    if match is None:
        return sensor_id, None

    return (
        match.group("device_id"),
        int(match.group("endpoint")),
    )


@dataclass(frozen=True, slots=True)
class SensorReading:
    """Normalized snapshot of a DIRIGERA environment sensor endpoint."""

    sensor_id: str
    name: str
    model: str | None
    room: str | None
    reachable: bool

    temperature: float | None
    humidity: int | None
    co2: int | None
    pm25: int | None
    voc_index: int | None
    battery_percentage: int | None

    # This timestamp is always UTC and is used for InfluxDB.
    observed_at: datetime

    @classmethod
    def from_sensor(cls, sensor: EnvironmentSensor) -> "SensorReading":
        """Create a normalized reading from a DIRIGERA sensor model.

        ``EnvironmentSensor`` is a typed pydantic model exported by the
        dirigera library (since 1.3). All measurement attributes are optional
        and default to ``None`` when a device does not report them, e.g.
        TIMMERFLOTE reports no CO2 and ALPSTUGA reports no battery level.
        """
        attributes = sensor.attributes

        sensor_id = str(sensor.id)

        # Clean whitespace before displaying values or using them as
        # InfluxDB tags.
        custom_name = (attributes.custom_name or "").strip() or None
        model = (attributes.model or "").strip() or None
        room_name = (
            (sensor.room.name or "").strip() or None
            if sensor.room is not None
            else None
        )

        return cls(
            sensor_id=sensor_id,
            name=custom_name or model or sensor_id,
            model=model,
            room=room_name,
            reachable=bool(sensor.is_reachable),
            temperature=attributes.current_temperature,
            humidity=attributes.current_r_h,
            co2=attributes.current_c_o2,
            pm25=attributes.current_p_m25,
            voc_index=attributes.voc_index,
            battery_percentage=attributes.battery_percentage,
            observed_at=datetime.now(timezone.utc),
        )

    def measurement_fields(self) -> dict[str, int | float]:
        """Return all available values suitable for InfluxDB fields.

        Reachability is intentionally not a field: readings of unreachable
        devices are never written (see ``ReachabilityGate``).
        """
        possible_fields: dict[str, int | float | None] = {
            "temperature": self.temperature,
            "humidity": self.humidity,
            "co2": self.co2,
            "pm25": self.pm25,
            "voc_index": self.voc_index,
            "battery_percentage": self.battery_percentage,
        }

        return {
            key: value
            for key, value in possible_fields.items()
            if value is not None
        }

    def display_line(self) -> str:
        """Create a human-readable terminal output line."""
        values: list[str] = []

        if self.temperature is not None:
            values.append(f"{self.temperature:.1f} °C")

        if self.humidity is not None:
            values.append(f"{self.humidity} % RH")

        if self.co2 is not None:
            values.append(f"CO₂ {self.co2} ppm")

        if self.pm25 is not None:
            values.append(f"PM2.5 {self.pm25} µg/m³")

        if self.voc_index is not None:
            values.append(f"VOC index {self.voc_index}")

        if self.battery_percentage is not None:
            values.append(
                f"battery {self.battery_percentage} %"
            )

        if not values:
            values.append("no measurements available")

        status = "online" if self.reachable else "offline"
        location = f", room={self.room}" if self.room else ""

        return (
            f"{self.name} "
            f"[{self.sensor_id}, {status}{location}]: "
            + ", ".join(values)
        )


def endpoint_priority(reading: SensorReading) -> tuple[int, int]:
    """Return sorting priority for a logical DIRIGERA endpoint."""
    _, endpoint = split_endpoint_id(reading.sensor_id)

    if endpoint == 1:
        return 0, 1

    if endpoint is None:
        return 1, 0

    return 2, endpoint


def merge_sensor_readings(
    readings: list[SensorReading],
) -> list[SensorReading]:
    """Normalize physical IDs and merge logical sensor endpoints."""
    grouped: dict[str, list[SensorReading]] = {}
    group_order: list[str] = []

    for reading in readings:
        physical_device_id, _ = split_endpoint_id(
            reading.sensor_id
        )

        if physical_device_id not in grouped:
            grouped[physical_device_id] = []
            group_order.append(physical_device_id)

        grouped[physical_device_id].append(reading)

    merged_readings: list[SensorReading] = []

    for physical_device_id in group_order:
        device_readings = grouped[physical_device_id]
        prioritized = sorted(
            device_readings,
            key=endpoint_priority,
        )
        primary = prioritized[0]

        def first_available(attribute_name: str) -> Any:
            """Return the first value available in endpoint-priority order."""
            for reading in prioritized:
                value = getattr(reading, attribute_name)

                if value is not None:
                    return value

            return None

        merged = SensorReading(
            # Always use the normalized physical device ID.
            sensor_id=physical_device_id,

            # Endpoint _1 is first, so its name has priority.
            name=primary.name,
            model=first_available("model"),
            room=first_available("room"),

            # The physical device is reachable when at least one of its
            # logical endpoints is reachable.
            reachable=any(
                reading.reachable
                for reading in prioritized
            ),

            temperature=first_available("temperature"),
            humidity=first_available("humidity"),
            co2=first_available("co2"),
            pm25=first_available("pm25"),
            voc_index=first_available("voc_index"),
            battery_percentage=first_available(
                "battery_percentage"
            ),
            observed_at=max(
                reading.observed_at
                for reading in prioritized
            ),
        )

        if len(prioritized) > 1:
            LOGGER.debug(
                "Merged DIRIGERA endpoints %s into physical device %s; "
                "using name %r",
                [
                    reading.sensor_id
                    for reading in prioritized
                ],
                physical_device_id,
                merged.name,
            )
        elif primary.sensor_id != physical_device_id:
            LOGGER.debug(
                "Normalized DIRIGERA endpoint ID %s to physical ID %s",
                primary.sensor_id,
                physical_device_id,
            )

        merged_readings.append(merged)

    return merged_readings


def read_sensors(hub: Hub) -> list[SensorReading]:
    """Fetch, normalize and merge environment sensor readings."""
    endpoint_readings = [
        SensorReading.from_sensor(sensor)
        for sensor in hub.get_environment_sensors()
    ]

    return merge_sensor_readings(endpoint_readings)
