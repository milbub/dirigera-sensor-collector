#!/usr/bin/env python3
"""Read IKEA DIRIGERA sensors, control ALPSTUGA displays and write to InfluxDB."""

from __future__ import annotations

import logging
import os
import re
import signal
import sys
import threading
import time as time_module
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any

from dirigera import EnvironmentSensor, Hub

from alpstuga_display import (
    DisplayUpdateResult,
    desired_display_state,
    parse_clock_time,
    parse_device_ids,
    set_alpstuga_displays,
)


LOGGER = logging.getLogger("dirigera-sensors")
STOP_EVENT = threading.Event()

# The display schedule is evaluated independently of sensor polling.
DISPLAY_SCHEDULE_CHECK_SECONDS = 1.0

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


def env_bool(name: str, default: bool = False) -> bool:
    """Read and validate a boolean environment variable."""
    value = os.getenv(name)

    if value is None:
        return default

    normalized = value.strip().lower()

    if normalized in {"1", "true", "yes", "on"}:
        return True

    if normalized in {"0", "false", "no", "off"}:
        return False

    raise ValueError(
        f"{name} must be one of: "
        "true, false, 1, 0, yes, no, on, off"
    )


def env_csv_set(name: str) -> frozenset[str]:
    """Read a comma-separated environment variable as a set."""
    return frozenset(
        item.strip()
        for item in os.getenv(name, "").split(",")
        if item.strip()
    )


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration loaded from environment variables."""

    dirigera_ip: str
    dirigera_token: str
    poll_interval_seconds: float

    influx_enabled: bool
    influx_dry_run: bool
    influx_url: str
    influx_token: str
    influx_org: str
    influx_bucket: str
    influx_measurement: str
    influx_ignored_fields: frozenset[str]
    influx_ignored_tags: frozenset[str]

    alpstuga_display_control_ids: tuple[str, ...]
    alpstuga_display_control_off_time: time | None
    alpstuga_display_control_on_time: time | None

    @classmethod
    def from_environment(cls) -> "Settings":
        """Create and validate settings from environment variables."""
        influx_dry_run = env_bool(
            "INFLUX_DRY_RUN",
            default=False,
        )
        requested_influx_enabled = env_bool(
            "INFLUX_ENABLED",
            default=False,
        )

        # Dry-run and real writes are mutually exclusive. Dry-run always wins.
        effective_influx_enabled = (
            requested_influx_enabled and not influx_dry_run
        )

        display_ids = parse_device_ids(
            os.getenv("ALPSTUGA_DISPLAY_CONTROL_IDS", "")
        )

        raw_display_off_time = os.getenv(
            "ALPSTUGA_DISPLAY_CONTROL_OFF_TIME",
            "",
        ).strip()
        raw_display_on_time = os.getenv(
            "ALPSTUGA_DISPLAY_CONTROL_ON_TIME",
            "",
        ).strip()

        display_off_time: time | None = None
        display_on_time: time | None = None

        if display_ids:
            display_off_time = parse_clock_time(
                raw_display_off_time,
                "ALPSTUGA_DISPLAY_CONTROL_OFF_TIME",
            )
            display_on_time = parse_clock_time(
                raw_display_on_time,
                "ALPSTUGA_DISPLAY_CONTROL_ON_TIME",
            )

        settings = cls(
            dirigera_ip=os.getenv(
                "DIRIGERA_IP",
                "192.168.0.6",
            ).strip(),
            dirigera_token=os.getenv(
                "DIRIGERA_TOKEN",
                "",
            ).strip(),
            poll_interval_seconds=float(
                os.getenv("POLL_INTERVAL_SECONDS", "10")
            ),
            influx_enabled=effective_influx_enabled,
            influx_dry_run=influx_dry_run,
            influx_url=os.getenv(
                "INFLUX_URL",
                "http://localhost:8086",
            ).strip(),
            influx_token=os.getenv(
                "INFLUX_TOKEN",
                "",
            ).strip(),
            influx_org=os.getenv(
                "INFLUX_ORG",
                "",
            ).strip(),
            influx_bucket=os.getenv(
                "INFLUX_BUCKET",
                "sensors",
            ).strip(),
            influx_measurement=os.getenv(
                "INFLUX_MEASUREMENT",
                "ikea_environment_sensor",
            ).strip(),
            influx_ignored_fields=env_csv_set(
                "INFLUX_IGNORED_FIELDS"
            ),
            influx_ignored_tags=env_csv_set(
                "INFLUX_IGNORED_TAGS"
            ),
            alpstuga_display_control_ids=display_ids,
            alpstuga_display_control_off_time=display_off_time,
            alpstuga_display_control_on_time=display_on_time,
        )

        settings.validate()

        if influx_dry_run and requested_influx_enabled:
            LOGGER.warning(
                "INFLUX_DRY_RUN=true overrides INFLUX_ENABLED=true; "
                "no data will be sent to InfluxDB"
            )

        return settings

    def validate(self) -> None:
        """Validate configuration before connecting to external services."""
        if not self.dirigera_ip:
            raise ValueError("DIRIGERA_IP must not be empty")

        if not self.dirigera_token:
            raise ValueError(
                "DIRIGERA_TOKEN is empty. "
                "Set it before starting the script."
            )

        if self.poll_interval_seconds <= 0:
            raise ValueError(
                "POLL_INTERVAL_SECONDS must be greater than zero"
            )

        if not self.influx_measurement:
            raise ValueError(
                "INFLUX_MEASUREMENT must not be empty"
            )

        if self.influx_enabled:
            required_values = {
                "INFLUX_URL": self.influx_url,
                "INFLUX_TOKEN": self.influx_token,
                "INFLUX_ORG": self.influx_org,
                "INFLUX_BUCKET": self.influx_bucket,
            }

            missing = [
                name
                for name, value in required_values.items()
                if not value
            ]

            if missing:
                raise ValueError(
                    "InfluxDB is enabled, but these variables are empty: "
                    + ", ".join(missing)
                )

        if self.alpstuga_display_control_ids:
            if self.alpstuga_display_control_off_time is None:
                raise ValueError(
                    "ALPSTUGA_DISPLAY_CONTROL_OFF_TIME is required when "
                    "ALPSTUGA_DISPLAY_CONTROL_IDS is configured"
                )

            if self.alpstuga_display_control_on_time is None:
                raise ValueError(
                    "ALPSTUGA_DISPLAY_CONTROL_ON_TIME is required when "
                    "ALPSTUGA_DISPLAY_CONTROL_IDS is configured"
                )

            if (
                self.alpstuga_display_control_off_time
                == self.alpstuga_display_control_on_time
            ):
                raise ValueError(
                    "ALPSTUGA_DISPLAY_CONTROL_OFF_TIME and "
                    "ALPSTUGA_DISPLAY_CONTROL_ON_TIME must be different"
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

    def measurement_fields(self) -> dict[str, int | float | bool]:
        """Return all available values suitable for InfluxDB fields."""
        possible_fields: dict[str, int | float | bool | None] = {
            "temperature": self.temperature,
            "humidity": self.humidity,
            "co2": self.co2,
            "pm25": self.pm25,
            "voc_index": self.voc_index,
            "battery_percentage": self.battery_percentage,

            # Store reachability as a native InfluxDB boolean field.
            # The line protocol representation is:
            #
            #   reachable=true
            #   reachable=false
            "reachable": self.reachable,
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
            "reachable",
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


class LocalAlpstugaDisplayScheduler:
    """Schedule ALPSTUGA display changes using local wall-clock time.

    The configured on/off values are interpreted in the operating system's
    local timezone. The schedule is evaluated once per second independently
    of sensor polling.

    A request is made only when the desired state changes or when ``force`` is
    requested during application startup.
    """

    def __init__(
        self,
        hub: Hub,
        device_ids: tuple[str, ...],
        off_time: time,
        on_time: time,
    ) -> None:
        if not device_ids:
            raise ValueError(
                "At least one ALPSTUGA display ID is required"
            )

        if off_time == on_time:
            raise ValueError(
                "ALPSTUGA display off and on times must be different"
            )

        self._hub = hub
        self._device_ids = device_ids
        self._off_time = off_time
        self._on_time = on_time

        # This is updated only after all devices have been successfully
        # changed and verified.
        self._last_applied_state: bool | None = None

    @property
    def device_ids(self) -> tuple[str, ...]:
        """Return configured ALPSTUGA endpoint IDs."""
        return self._device_ids

    @property
    def off_time(self) -> time:
        """Return the configured local display-off time."""
        return self._off_time

    @property
    def on_time(self) -> time:
        """Return the configured local display-on time."""
        return self._on_time

    def current_desired_state(
        self,
        local_now: datetime | None = None,
    ) -> bool:
        """Return the display state required at the current local time."""
        # datetime.now() returns the current system-local wall-clock value.
        # The configured schedule values are also local wall-clock values, so
        # no UTC conversion must be performed here.
        current = local_now if local_now is not None else datetime.now()

        return desired_display_state(
            current_time=current.time(),
            off_time=self._off_time,
            on_time=self._on_time,
        )

    def apply(
        self,
        *,
        force: bool = False,
        local_now: datetime | None = None,
    ) -> list[DisplayUpdateResult]:
        """Apply the display state required at the current local time."""
        desired_state = self.current_desired_state(local_now)

        if (
            not force
            and desired_state == self._last_applied_state
        ):
            return []

        results = set_alpstuga_displays(
            hub=self._hub,
            device_ids=self._device_ids,
            enabled=desired_state,
            verify=True,
        )

        # Keep the previous value when the operation raises an exception.
        # Consequently, a failed operation is retried on the next iteration.
        self._last_applied_state = desired_state

        return results


def install_signal_handlers() -> None:
    """Request a clean shutdown on SIGINT or SIGTERM."""

    def stop_handler(signum: int, _frame: Any) -> None:
        LOGGER.info("Received signal %s; stopping", signum)
        STOP_EVENT.set()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)


def read_sensors(hub: Hub) -> list[SensorReading]:
    """Fetch, normalize and merge environment sensor readings."""
    endpoint_readings = [
        SensorReading.from_sensor(sensor)
        for sensor in hub.get_environment_sensors()
    ]

    return merge_sensor_readings(endpoint_readings)


def create_display_scheduler(
    hub: Hub,
    settings: Settings,
) -> LocalAlpstugaDisplayScheduler | None:
    """Create the local-time ALPSTUGA display scheduler when configured."""
    if not settings.alpstuga_display_control_ids:
        return None

    if (
        settings.alpstuga_display_control_off_time is None
        or settings.alpstuga_display_control_on_time is None
    ):
        raise RuntimeError(
            "ALPSTUGA display schedule is incomplete"
        )

    return LocalAlpstugaDisplayScheduler(
        hub=hub,
        device_ids=settings.alpstuga_display_control_ids,
        off_time=settings.alpstuga_display_control_off_time,
        on_time=settings.alpstuga_display_control_on_time,
    )


def log_display_results(
    results: list[DisplayUpdateResult],
) -> None:
    """Log completed ALPSTUGA display changes."""
    for result in results:
        state = "on" if result.requested_state else "off"

        LOGGER.info(
            "ALPSTUGA display %s (%s) switched %s; "
            "previous isOn=%r, reported isOn=%r",
            result.name,
            result.device_id,
            state,
            result.previous_state,
            result.reported_state,
        )


def apply_display_schedule(
    scheduler: LocalAlpstugaDisplayScheduler | None,
    *,
    force: bool = False,
) -> None:
    """Apply the display schedule without stopping sensor polling."""
    if scheduler is None:
        return

    try:
        results = scheduler.apply(force=force)
        log_display_results(results)
    except Exception:
        # Sensor polling and InfluxDB output remain operational if display
        # control temporarily fails. The update is retried on the next loop.
        LOGGER.exception(
            "Failed to apply ALPSTUGA display schedule; "
            "the operation will be retried"
        )


def log_configuration(
    settings: Settings,
    influx: InfluxOutput,
    display_scheduler: LocalAlpstugaDisplayScheduler | None,
) -> None:
    """Log the effective application configuration."""
    LOGGER.info(
        "InfluxDB measurement: %s",
        settings.influx_measurement,
    )

    LOGGER.info(
        "Ignored InfluxDB fields: %s",
        (
            ", ".join(sorted(settings.influx_ignored_fields))
            if settings.influx_ignored_fields
            else "none"
        ),
    )

    LOGGER.info(
        "Ignored InfluxDB tags: %s",
        (
            ", ".join(sorted(settings.influx_ignored_tags))
            if settings.influx_ignored_tags
            else "none"
        ),
    )

    if influx.dry_run:
        LOGGER.info(
            "InfluxDB dry-run is enabled; "
            "data will be printed but not sent"
        )
        LOGGER.info("InfluxDB writing is disabled")
    else:
        LOGGER.info(
            "InfluxDB writing is %s",
            "enabled" if influx.enabled else "disabled",
        )

    local_now = datetime.now().astimezone()

    LOGGER.info(
        "System local time: %s",
        local_now.strftime("%Y-%m-%d %H:%M:%S %Z (%z)"),
    )

    LOGGER.info("InfluxDB timestamps are generated in UTC")

    if display_scheduler is None:
        LOGGER.info("ALPSTUGA display scheduling is disabled")
        return

    LOGGER.info(
        "ALPSTUGA display scheduling is enabled for: %s",
        ", ".join(display_scheduler.device_ids),
    )

    LOGGER.info(
        "ALPSTUGA displays off at %s, on at %s "
        "(system local time)",
        display_scheduler.off_time.strftime("%H:%M:%S"),
        display_scheduler.on_time.strftime("%H:%M:%S"),
    )

    desired_state = display_scheduler.current_desired_state()

    LOGGER.info(
        "Current scheduled ALPSTUGA display state: %s",
        "on" if desired_state else "off",
    )


def poll_and_output_sensors(
    hub: Hub,
    influx: InfluxOutput,
    consecutive_failures: int,
) -> int:
    """Run one sensor polling and output cycle."""
    try:
        readings = read_sensors(hub)

        if readings:
            for reading in readings:
                print(
                    reading.display_line(),
                    flush=True,
                )

            influx.process(readings)
        else:
            LOGGER.warning(
                "No environment sensors were returned by the hub"
            )

        return 0

    except Exception:
        consecutive_failures += 1

        LOGGER.exception(
            "Sensor polling failed "
            "(consecutive failures: %d). "
            "The script will retry.",
            consecutive_failures,
        )

        return consecutive_failures


def run(settings: Settings) -> None:
    """Run sensor polling and local-time ALPSTUGA display scheduling."""
    hub = Hub(
        token=settings.dirigera_token,
        ip_address=settings.dirigera_ip,
    )

    influx = InfluxOutput(settings)
    display_scheduler = create_display_scheduler(
        hub,
        settings,
    )

    LOGGER.info(
        "Connecting to DIRIGERA at %s",
        settings.dirigera_ip,
    )

    log_configuration(
        settings,
        influx,
        display_scheduler,
    )

    # Synchronize all configured displays immediately at startup according
    # to the current local wall-clock time.
    apply_display_schedule(
        display_scheduler,
        force=True,
    )

    consecutive_sensor_failures = 0

    # Sensor polling uses a monotonic clock so changes to the system wall
    # clock do not corrupt the polling interval. Display scheduling uses the
    # local wall clock separately.
    next_sensor_poll = time_module.monotonic()

    try:
        while not STOP_EVENT.is_set():
            # Evaluate display scheduling independently of sensor polling.
            # A DIRIGERA request is sent only when the desired state changes.
            apply_display_schedule(display_scheduler)

            monotonic_now = time_module.monotonic()

            if monotonic_now >= next_sensor_poll:
                poll_started = monotonic_now

                consecutive_sensor_failures = poll_and_output_sensors(
                    hub=hub,
                    influx=influx,
                    consecutive_failures=consecutive_sensor_failures,
                )

                # Check the schedule again after network operations. This
                # reduces delay if a slow sensor or InfluxDB request crossed
                # an on/off boundary.
                apply_display_schedule(display_scheduler)

                next_sensor_poll = (
                    poll_started
                    + settings.poll_interval_seconds
                )

                # Do not replay polling cycles missed by a slow request.
                if next_sensor_poll <= time_module.monotonic():
                    next_sensor_poll = time_module.monotonic()

            seconds_until_sensor_poll = max(
                0.0,
                next_sensor_poll - time_module.monotonic(),
            )

            # Wake at least once per second to evaluate the ALPSTUGA schedule,
            # regardless of POLL_INTERVAL_SECONDS.
            wait_seconds = min(
                DISPLAY_SCHEDULE_CHECK_SECONDS,
                seconds_until_sensor_poll,
            )

            if wait_seconds <= 0:
                continue

            STOP_EVENT.wait(wait_seconds)

    finally:
        influx.close()
        LOGGER.info("Sensor reader stopped")


def configure_logging() -> None:
    """Configure application logs to use the system-local timezone."""
    # Python logging already uses local time by default. Assigning the
    # converter explicitly documents and enforces the intended behavior.
    logging.Formatter.converter = time_module.localtime

    logging.basicConfig(
        level=os.getenv(
            "LOG_LEVEL",
            "INFO",
        ).upper(),
        format=(
            "%(asctime)s %(levelname)s "
            "%(name)s: %(message)s"
        ),
    )


def main() -> int:
    """Configure and start the application."""
    configure_logging()

    try:
        settings = Settings.from_environment()
        install_signal_handlers()
        run(settings)
    except (ValueError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 2
    except Exception:
        LOGGER.exception("Fatal error")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

