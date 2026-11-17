"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, time

from .display import parse_device_ids


LOGGER = logging.getLogger("dirigera-sensors")


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


def env_float(name: str, default: float) -> float:
    """Read and validate a floating-point environment variable."""
    value = os.getenv(name, "").strip()

    if not value:
        return default

    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be a number; received {value!r}"
        ) from exc


def env_int(name: str, default: int) -> int:
    """Read and validate an integer environment variable."""
    value = os.getenv(name, "").strip()

    if not value:
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be an integer; received {value!r}"
        ) from exc


def env_csv_set(name: str) -> frozenset[str]:
    """Read a comma-separated environment variable as a set."""
    return frozenset(
        item.strip()
        for item in os.getenv(name, "").split(",")
        if item.strip()
    )


def parse_clock_time(raw_value: str, variable_name: str) -> time:
    """Parse an exact local clock time in HH:MM:SS format."""
    value = raw_value.strip()

    if not value:
        raise ValueError(f"{variable_name} must not be empty")

    try:
        return datetime.strptime(value, "%H:%M:%S").time()
    except ValueError as exc:
        raise ValueError(
            f"{variable_name} must use the HH:MM:SS format, "
            f"for example 22:00:00; received {value!r}"
        ) from exc


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
    influx_recovery_hold_seconds: float
    influx_recovery_hold_min_unreachable_samples: int

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
                "",
            ).strip(),
            dirigera_token=os.getenv(
                "DIRIGERA_TOKEN",
                "",
            ).strip(),
            poll_interval_seconds=env_float(
                "POLL_INTERVAL_SECONDS",
                default=10.0,
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
            influx_recovery_hold_seconds=env_float(
                "INFLUX_RECOVERY_HOLD_SECONDS",
                default=1800.0,
            ),
            influx_recovery_hold_min_unreachable_samples=env_int(
                "INFLUX_RECOVERY_HOLD_MIN_UNREACHABLE_SAMPLES",
                default=2,
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
            raise ValueError(
                "DIRIGERA_IP is empty. "
                "Set it to the IP address of your DIRIGERA hub."
            )

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

        if self.influx_recovery_hold_seconds < 0:
            raise ValueError(
                "INFLUX_RECOVERY_HOLD_SECONDS must not be negative"
            )

        if self.influx_recovery_hold_min_unreachable_samples < 1:
            raise ValueError(
                "INFLUX_RECOVERY_HOLD_MIN_UNREACHABLE_SAMPLES must be "
                "at least 1"
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
