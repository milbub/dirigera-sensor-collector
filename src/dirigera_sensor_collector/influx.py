"""Build InfluxDB points and write them or print them in dry-run mode."""

from __future__ import annotations

import logging
from typing import Any

from .config import Settings
from .sensors import SensorReading


LOGGER = logging.getLogger("dirigera-sensors")


class InfluxOutput:
    """Create InfluxDB line protocol and optionally send it to InfluxDB."""

    AVAILABLE_FIELDS = frozenset(
        {
            "temperature",
            "humidity",
            "co2",
            "pm25",
            "voc_index",
            "battery_percentage",
        }
    )

    AVAILABLE_TAGS = frozenset(
        {
            "sensor_id",
            "name",
            "model",
            "room",
        }
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any = None
        self._write_api: Any = None
        self._point_class: Any = None

        self._report_unknown_ignored_items()

        if not settings.influx_enabled and not settings.influx_dry_run:
            return

        try:
            from influxdb_client import InfluxDBClient, Point
            from influxdb_client.client.write_api import SYNCHRONOUS
        except ImportError as exc:
            raise RuntimeError(
                "InfluxDB output or dry-run is enabled, but "
                "influxdb-client is not installed. "
                "Install it with: pip install influxdb-client"
            ) from exc

        self._point_class = Point

        # Dry-run builds line protocol locally and must never open a real
        # InfluxDB connection.
        if settings.influx_dry_run:
            return

        self._client = InfluxDBClient(
            url=settings.influx_url,
            token=settings.influx_token,
            org=settings.influx_org,
        )
        self._write_api = self._client.write_api(
            write_options=SYNCHRONOUS
        )

    def _report_unknown_ignored_items(self) -> None:
        """Warn about unknown ignored field or tag names."""
        unknown_fields = (
            self._settings.influx_ignored_fields
            - self.AVAILABLE_FIELDS
        )
        unknown_tags = (
            self._settings.influx_ignored_tags
            - self.AVAILABLE_TAGS
        )

        if unknown_fields:
            LOGGER.warning(
                "INFLUX_IGNORED_FIELDS contains unknown fields: %s. "
                "Available fields: %s",
                ", ".join(sorted(unknown_fields)),
                ", ".join(sorted(self.AVAILABLE_FIELDS)),
            )

        if unknown_tags:
            LOGGER.warning(
                "INFLUX_IGNORED_TAGS contains unknown tags: %s. "
                "Available tags: %s",
                ", ".join(sorted(unknown_tags)),
                ", ".join(sorted(self.AVAILABLE_TAGS)),
            )

    @property
    def enabled(self) -> bool:
        """Return whether real InfluxDB writing is enabled."""
        return self._write_api is not None

    @property
    def dry_run(self) -> bool:
        """Return whether line-protocol dry-run is enabled."""
        return self._settings.influx_dry_run

    @property
    def active(self) -> bool:
        """Return whether real output or dry-run output is enabled."""
        return self.enabled or self.dry_run

    def create_point(self, reading: SensorReading) -> Any | None:
        """Create one filtered InfluxDB point."""
        fields = {
            field_name: field_value
            for field_name, field_value
            in reading.measurement_fields().items()
            if field_name
            not in self._settings.influx_ignored_fields
        }

        # InfluxDB requires every point to contain at least one field.
        if not fields:
            LOGGER.warning(
                "Skipping InfluxDB point for sensor %s because no fields "
                "remain after applying INFLUX_IGNORED_FIELDS",
                reading.sensor_id,
            )
            return None

        available_tags: dict[str, str | None] = {
            "sensor_id": reading.sensor_id,
            "name": reading.name,
            "model": reading.model,
            "room": reading.room,
        }

        point = self._point_class(
            self._settings.influx_measurement
        )

        for tag_name, tag_value in available_tags.items():
            if tag_name in self._settings.influx_ignored_tags:
                continue

            if tag_value is not None:
                point.tag(tag_name, tag_value)

        for field_name, field_value in fields.items():
            point.field(field_name, field_value)

        # observed_at is explicitly timezone-aware UTC. The InfluxDB client
        # serializes it as a UTC nanosecond timestamp.
        point.time(reading.observed_at)

        return point

    def process(self, readings: list[SensorReading]) -> None:
        """Print line protocol in dry-run mode or send points to InfluxDB."""
        if not self.active or not readings:
            return

        points = [
            point
            for reading in readings
            if (point := self.create_point(reading)) is not None
        ]

        if not points:
            return

        if self.dry_run:
            for point in points:
                print(
                    f"INFLUX RAW DATA: {point.to_line_protocol()}",
                    flush=True,
                )

            return

        self._write_api.write(
            bucket=self._settings.influx_bucket,
            org=self._settings.influx_org,
            record=points,
        )

    def close(self) -> None:
        """Close the InfluxDB client if it was created."""
        if self._client is not None:
            self._client.close()
