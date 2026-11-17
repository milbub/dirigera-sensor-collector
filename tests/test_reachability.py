"""Tests for reachability filtering and the recovery hold (reachability.py)."""

from __future__ import annotations

from typing import Callable

import pytest

from dirigera_sensor_collector.reachability import (
    InfluxDecision,
    InfluxStatus,
    ReachabilityGate,
    format_duration,
)
from dirigera_sensor_collector.sensors import SensorReading

ReadingFactory = Callable[..., SensorReading]

SEND = InfluxStatus.SEND
OFFLINE = InfluxStatus.OFFLINE
HOLD = InfluxStatus.RECOVERY_HOLD


def run_steps(
    gate: ReachabilityGate,
    make_reading: ReadingFactory,
    steps: list[tuple[float, bool, InfluxStatus]],
) -> None:
    """Feed ``(monotonic time, reachable, expected status)`` steps to a gate."""
    for now, reachable, expected in steps:
        decision = gate.evaluate(make_reading(reachable=reachable), now=now)
        assert decision.status is expected, (now, reachable)


@pytest.fixture
def gate() -> ReachabilityGate:
    return ReachabilityGate(hold_seconds=1800, min_unreachable_samples=2)


def test_reachable_device_is_sent(gate: ReachabilityGate, make_reading: ReadingFactory) -> None:
    run_steps(gate, make_reading, [(0, True, SEND), (10, True, SEND)])


def test_single_unreachable_reading_is_skipped_without_hold(
    gate: ReachabilityGate, make_reading: ReadingFactory
) -> None:
    run_steps(
        gate,
        make_reading,
        [(0, True, SEND), (10, False, OFFLINE), (20, True, SEND)],
    )


def test_two_unreachable_readings_start_hold_after_reconnect(
    gate: ReachabilityGate, make_reading: ReadingFactory
) -> None:
    run_steps(
        gate,
        make_reading,
        [
            (0, False, OFFLINE),
            (10, False, OFFLINE),
            (20, True, HOLD),  # countdown starts here
            (1819, True, HOLD),
            (1820, True, SEND),  # exactly 30 minutes later
            (1830, True, SEND),
        ],
    )


def test_long_outage_does_not_shorten_hold(
    gate: ReachabilityGate, make_reading: ReadingFactory
) -> None:
    steps = [(float(t), False, OFFLINE) for t in range(0, 7200, 10)]
    steps += [(7200, True, HOLD), (8999, True, HOLD), (9000, True, SEND)]

    run_steps(gate, make_reading, steps)


def test_outage_during_hold_restarts_countdown_from_zero(
    gate: ReachabilityGate, make_reading: ReadingFactory
) -> None:
    run_steps(
        gate,
        make_reading,
        [
            (0, False, OFFLINE),
            (10, False, OFFLINE),
            (20, True, HOLD),
            (1000, False, OFFLINE),  # even a single reading resets the hold
            (1010, True, HOLD),
            (1820, True, HOLD),  # the original hold would have ended here
            (2809, True, HOLD),
            (2810, True, SEND),
        ],
    )


def test_gaps_between_readings_do_not_break_unreachable_series(
    gate: ReachabilityGate, make_reading: ReadingFactory
) -> None:
    # Failed polls produce no readings; consecutive obtained readings count.
    run_steps(
        gate,
        make_reading,
        [(0, False, OFFLINE), (600, False, OFFLINE), (610, True, HOLD)],
    )


def test_devices_are_tracked_independently(
    gate: ReachabilityGate, make_reading: ReadingFactory
) -> None:
    for now in (0, 10):
        gate.evaluate(make_reading(sensor_id="a", reachable=False), now=now)
        gate.evaluate(make_reading(sensor_id="b", reachable=True), now=now)

    assert gate.evaluate(make_reading(sensor_id="a"), now=20).status is HOLD
    assert gate.evaluate(make_reading(sensor_id="b"), now=20).status is SEND


def test_threshold_of_one(make_reading: ReadingFactory) -> None:
    gate = ReachabilityGate(hold_seconds=60, min_unreachable_samples=1)

    run_steps(
        gate,
        make_reading,
        [(0, False, OFFLINE), (10, True, HOLD), (70, True, SEND)],
    )


def test_threshold_of_three(make_reading: ReadingFactory) -> None:
    gate = ReachabilityGate(hold_seconds=60, min_unreachable_samples=3)

    run_steps(
        gate,
        make_reading,
        [
            (0, False, OFFLINE),
            (10, False, OFFLINE),
            (20, True, SEND),  # two readings are not enough
            (30, False, OFFLINE),
            (40, False, OFFLINE),
            (50, False, OFFLINE),
            (60, True, HOLD),
        ],
    )


def test_zero_hold_disables_hold(make_reading: ReadingFactory) -> None:
    gate = ReachabilityGate(hold_seconds=0, min_unreachable_samples=2)

    run_steps(
        gate,
        make_reading,
        [(0, False, OFFLINE), (10, False, OFFLINE), (20, True, SEND)],
    )


def test_hold_reports_remaining_time(
    gate: ReachabilityGate, make_reading: ReadingFactory
) -> None:
    run_steps(gate, make_reading, [(0, False, OFFLINE), (10, False, OFFLINE)])

    gate.evaluate(make_reading(), now=20)
    decision = gate.evaluate(make_reading(), now=770)

    assert decision.hold_remaining_seconds == pytest.approx(1050)
    assert decision.describe() == "InfluxDB: recovery hold, 17m 30s left"


def test_transitions_are_logged(
    gate: ReachabilityGate,
    make_reading: ReadingFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")

    run_steps(
        gate,
        make_reading,
        [(0, False, OFFLINE), (10, False, OFFLINE), (20, True, HOLD), (1820, True, SEND)],
    )

    assert "is unreachable" in caplog.text
    assert "2 consecutive reading(s)" in caplog.text
    assert "holding InfluxDB writes for 30m 00s" in caplog.text
    assert "recovery hold finished" in caplog.text


@pytest.mark.parametrize(
    ("hold_seconds", "min_samples"),
    [(-1, 2), (10, 0)],
)
def test_invalid_arguments(hold_seconds: float, min_samples: int) -> None:
    with pytest.raises(ValueError):
        ReachabilityGate(hold_seconds=hold_seconds, min_unreachable_samples=min_samples)


@pytest.mark.parametrize(
    ("decision", "text"),
    [
        (InfluxDecision(SEND), "InfluxDB: sent"),
        (InfluxDecision(OFFLINE), "InfluxDB: skipped, device offline"),
        (InfluxDecision(HOLD, 5.2), "InfluxDB: recovery hold, 6s left"),
    ],
)
def test_decision_describe(decision: InfluxDecision, text: str) -> None:
    assert decision.describe() == text
    assert decision.send is (decision.status is SEND)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (0.1, "1s"),  # rounded up so a running hold never shows 0s
        (59, "59s"),
        (60, "1m 00s"),
        (1800, "30m 00s"),
        (3725, "1h 02m 05s"),
        (-5, "0s"),
    ],
)
def test_format_duration(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text
