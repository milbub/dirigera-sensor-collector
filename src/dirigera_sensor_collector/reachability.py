"""Decide whether readings are written to InfluxDB based on reachability."""

from __future__ import annotations

import logging
import math
import time as time_module
from dataclasses import dataclass
from enum import Enum

from .sensors import SensorReading


LOGGER = logging.getLogger("dirigera-sensors")


def format_duration(seconds: float) -> str:
    """Format a duration as a compact human-readable string, e.g. 29m 50s."""
    total = max(0, math.ceil(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"

    if minutes:
        return f"{minutes}m {secs:02d}s"

    return f"{secs}s"


class InfluxStatus(Enum):
    """Whether a reading may be written to InfluxDB."""

    SEND = "send"
    OFFLINE = "offline"
    RECOVERY_HOLD = "recovery_hold"


@dataclass(frozen=True, slots=True)
class InfluxDecision:
    """Result of ``ReachabilityGate.evaluate`` for one reading."""

    status: InfluxStatus
    hold_remaining_seconds: float | None = None

    @property
    def send(self) -> bool:
        """Return whether the reading should be written to InfluxDB."""
        return self.status is InfluxStatus.SEND

    def describe(self) -> str:
        """Return a short annotation for the terminal output line."""
        if self.status is InfluxStatus.OFFLINE:
            return "InfluxDB: skipped, device offline"

        if self.status is InfluxStatus.RECOVERY_HOLD:
            return (
                "InfluxDB: recovery hold, "
                f"{format_duration(self.hold_remaining_seconds or 0.0)} left"
            )

        return "InfluxDB: sent"


@dataclass(slots=True)
class _DeviceReachability:
    """Mutable per-device state tracked by ``ReachabilityGate``."""

    # Number of consecutive obtained readings with reachable=False. Failed
    # polls produce no readings, so they neither increase nor reset it.
    unreachable_streak: int = 0

    # The current outage qualifies for a recovery hold; the countdown starts
    # once the device is reachable again.
    hold_pending: bool = False

    # Monotonic time of the first reachable reading after a qualifying
    # outage; None when no hold is running.
    hold_started_at: float | None = None


class ReachabilityGate:
    """Decide per physical device whether its readings go to InfluxDB.

    Rules:

    * A reading with ``reachable=False`` is never written.
    * When a device is unreachable for at least ``min_unreachable_samples``
      consecutive readings, a recovery hold of ``hold_seconds`` starts at the
      first reachable reading afterwards. Readings are not written during the
      hold so measured values can settle after the reconnect.
    * If the device becomes unreachable during the hold, the hold is
      cancelled and starts again from zero once the device is reachable.
    * ``hold_seconds=0`` disables the recovery hold; unreachable readings
      are still skipped.

    State is kept in memory only and is lost on restart.
    """

    def __init__(
        self,
        hold_seconds: float,
        min_unreachable_samples: int,
    ) -> None:
        if hold_seconds < 0:
            raise ValueError("hold_seconds must not be negative")

        if min_unreachable_samples < 1:
            raise ValueError("min_unreachable_samples must be at least 1")

        self._hold_seconds = hold_seconds
        self._min_unreachable_samples = min_unreachable_samples
        self._devices: dict[str, _DeviceReachability] = {}

    @property
    def hold_seconds(self) -> float:
        """Return the recovery hold duration in seconds."""
        return self._hold_seconds

    @property
    def min_unreachable_samples(self) -> int:
        """Return how many unreachable readings trigger a recovery hold."""
        return self._min_unreachable_samples

    def evaluate(
        self,
        reading: SensorReading,
        now: float | None = None,
    ) -> InfluxDecision:
        """Update device state with one reading and return the decision.

        ``now`` is a ``time.monotonic()`` value; it can be injected in tests.
        """
        current = time_module.monotonic() if now is None else now
        state = self._devices.setdefault(
            reading.sensor_id,
            _DeviceReachability(),
        )

        if not reading.reachable:
            return self._handle_unreachable(reading, state)

        return self._handle_reachable(reading, state, current)

    def _handle_unreachable(
        self,
        reading: SensorReading,
        state: _DeviceReachability,
    ) -> InfluxDecision:
        state.unreachable_streak += 1

        if state.hold_started_at is not None:
            # Any outage during a running hold resets it; the countdown starts
            # from zero after the next reconnect.
            state.hold_started_at = None
            state.hold_pending = True
            LOGGER.info(
                "Sensor %s (%s) became unreachable during the recovery "
                "hold; the hold will restart from zero after it reconnects",
                reading.name,
                reading.sensor_id,
            )
        elif state.unreachable_streak == 1:
            LOGGER.info(
                "Sensor %s (%s) is unreachable; its readings are not "
                "written to InfluxDB",
                reading.name,
                reading.sensor_id,
            )

        if (
            self._hold_seconds > 0
            and not state.hold_pending
            and state.unreachable_streak >= self._min_unreachable_samples
        ):
            state.hold_pending = True
            LOGGER.info(
                "Sensor %s (%s) has been unreachable for %d consecutive "
                "reading(s); a %s recovery hold will apply after it "
                "reconnects",
                reading.name,
                reading.sensor_id,
                state.unreachable_streak,
                format_duration(self._hold_seconds),
            )

        return InfluxDecision(InfluxStatus.OFFLINE)

    def _handle_reachable(
        self,
        reading: SensorReading,
        state: _DeviceReachability,
        now: float,
    ) -> InfluxDecision:
        was_unreachable = state.unreachable_streak > 0
        state.unreachable_streak = 0

        if state.hold_pending:
            state.hold_pending = False
            state.hold_started_at = now
            LOGGER.info(
                "Sensor %s (%s) is reachable again; holding InfluxDB "
                "writes for %s",
                reading.name,
                reading.sensor_id,
                format_duration(self._hold_seconds),
            )
        elif was_unreachable:
            LOGGER.info(
                "Sensor %s (%s) is reachable again; resuming InfluxDB writes",
                reading.name,
                reading.sensor_id,
            )

        if state.hold_started_at is None:
            return InfluxDecision(InfluxStatus.SEND)

        remaining = self._hold_seconds - (now - state.hold_started_at)

        if remaining > 0:
            return InfluxDecision(
                InfluxStatus.RECOVERY_HOLD,
                hold_remaining_seconds=remaining,
            )

        state.hold_started_at = None
        LOGGER.info(
            "Sensor %s (%s) recovery hold finished; resuming InfluxDB writes",
            reading.name,
            reading.sensor_id,
        )

        return InfluxDecision(InfluxStatus.SEND)
