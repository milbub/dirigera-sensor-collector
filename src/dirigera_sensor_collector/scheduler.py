"""Schedule ALPSTUGA display changes using local wall-clock time."""

from __future__ import annotations

import logging
from datetime import datetime, time

from dirigera import Hub

from .config import Settings
from .display import (
    DisplayUpdateResult,
    desired_display_state,
    set_alpstuga_displays,
)


LOGGER = logging.getLogger("dirigera-sensors")


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
