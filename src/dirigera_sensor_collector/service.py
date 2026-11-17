"""Collector service: poll DIRIGERA sensors, write to InfluxDB and schedule
ALPSTUGA displays.

Run it with the ``dirigera-sensor-collector`` command or
``python -m dirigera_sensor_collector``.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time as time_module
from datetime import datetime
from typing import Any

from dirigera import Hub

from .config import Settings
from .influx import InfluxOutput
from .reachability import ReachabilityGate, format_duration
from .scheduler import (
    LocalAlpstugaDisplayScheduler,
    apply_display_schedule,
    create_display_scheduler,
)
from .sensors import SensorReading, read_sensors


LOGGER = logging.getLogger("dirigera-sensors")
STOP_EVENT = threading.Event()


# The display schedule is evaluated independently of sensor polling.
DISPLAY_SCHEDULE_CHECK_SECONDS = 1.0


def install_signal_handlers() -> None:
    """Request a clean shutdown on SIGINT or SIGTERM."""

    def stop_handler(signum: int, _frame: Any) -> None:
        LOGGER.info("Received signal %s; stopping", signum)
        STOP_EVENT.set()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)


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

    LOGGER.info(
        "Readings of unreachable sensors are not written to InfluxDB; "
        "after %d or more consecutive unreachable readings a %s recovery "
        "hold applies",
        settings.influx_recovery_hold_min_unreachable_samples,
        format_duration(settings.influx_recovery_hold_seconds),
    )

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
    gate: ReachabilityGate,
    consecutive_failures: int,
) -> int:
    """Run one sensor polling and output cycle."""
    try:
        readings = read_sensors(hub)

        if readings:
            readings_to_send: list[SensorReading] = []

            for reading in readings:
                # Reachability is tracked even when InfluxDB output is off,
                # so enabling it later does not change the semantics.
                decision = gate.evaluate(reading)
                line = reading.display_line()

                if influx.active:
                    line = f"{line} ({decision.describe()})"

                print(line, flush=True)

                if decision.send:
                    readings_to_send.append(reading)

            influx.process(readings_to_send)
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
    gate = ReachabilityGate(
        hold_seconds=settings.influx_recovery_hold_seconds,
        min_unreachable_samples=(
            settings.influx_recovery_hold_min_unreachable_samples
        ),
    )
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
                    gate=gate,
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
