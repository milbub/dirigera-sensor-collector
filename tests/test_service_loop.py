"""Tests for the polling cycle, display scheduling and entry point."""

from __future__ import annotations

from datetime import datetime, time

import pytest

import read_sensors
from fakes import ALPSTUGA_ID, TIMMERFLOTE_ID, FakeHub
from read_sensors import (
    InfluxOutput,
    LocalAlpstugaDisplayScheduler,
    ReachabilityGate,
    Settings,
    apply_display_schedule,
    create_display_scheduler,
    poll_and_output_sensors,
)

NIGHT = datetime(2026, 1, 1, 23, 0, 0)
DAY = datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture
def scheduler(fake_hub: FakeHub) -> LocalAlpstugaDisplayScheduler:
    return LocalAlpstugaDisplayScheduler(
        hub=fake_hub,
        device_ids=(ALPSTUGA_ID,),
        off_time=time(22),
        on_time=time(6),
    )


def is_on(hub: FakeHub) -> bool:
    return hub.devices[ALPSTUGA_ID]["attributes"]["isOn"]


class TestDisplayScheduler:
    def test_switches_only_when_desired_state_changes(
        self, scheduler: LocalAlpstugaDisplayScheduler, fake_hub: FakeHub
    ) -> None:
        assert len(scheduler.apply(local_now=DAY)) == 1  # first call applies
        assert scheduler.apply(local_now=DAY) == []  # unchanged: no request

        [result] = scheduler.apply(local_now=NIGHT)

        assert result.requested_state is False
        assert is_on(fake_hub) is False
        assert fake_hub.patched_ids() == [ALPSTUGA_ID, ALPSTUGA_ID]

    def test_force_reapplies_same_state(
        self, scheduler: LocalAlpstugaDisplayScheduler, fake_hub: FakeHub
    ) -> None:
        scheduler.apply(local_now=DAY)
        scheduler.apply(local_now=DAY, force=True)

        assert len(fake_hub.patched_ids()) == 2

    def test_failed_update_is_retried(
        self, scheduler: LocalAlpstugaDisplayScheduler, fake_hub: FakeHub
    ) -> None:
        fake_hub.ignore_patches = True
        with pytest.raises(RuntimeError):
            scheduler.apply(local_now=NIGHT)

        fake_hub.ignore_patches = False
        [result] = scheduler.apply(local_now=NIGHT)

        assert result.reported_state is False

    def test_apply_display_schedule_logs_instead_of_raising(
        self,
        scheduler: LocalAlpstugaDisplayScheduler,
        fake_hub: FakeHub,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fake_hub.get_error = ConnectionError("hub down")

        apply_display_schedule(scheduler, force=True)

        assert "will be retried" in caplog.text

    def test_apply_display_schedule_without_scheduler(self) -> None:
        apply_display_schedule(None, force=True)

    def test_create_display_scheduler(
        self, monkeypatch: pytest.MonkeyPatch, base_env: None, fake_hub: FakeHub
    ) -> None:
        assert create_display_scheduler(fake_hub, Settings.from_environment()) is None

        monkeypatch.setenv("ALPSTUGA_DISPLAY_CONTROL_IDS", ALPSTUGA_ID)
        monkeypatch.setenv("ALPSTUGA_DISPLAY_CONTROL_OFF_TIME", "22:00:00")
        monkeypatch.setenv("ALPSTUGA_DISPLAY_CONTROL_ON_TIME", "06:00:00")

        scheduler = create_display_scheduler(fake_hub, Settings.from_environment())

        assert scheduler is not None
        assert scheduler.device_ids == (ALPSTUGA_ID,)


class TestPollAndOutputSensors:
    @pytest.fixture
    def dry_run_influx(
        self, monkeypatch: pytest.MonkeyPatch, base_env: None
    ) -> InfluxOutput:
        monkeypatch.setenv("INFLUX_DRY_RUN", "true")
        return InfluxOutput(Settings.from_environment())

    @pytest.fixture
    def gate(self) -> ReachabilityGate:
        return ReachabilityGate(hold_seconds=1800, min_unreachable_samples=2)

    def test_prints_and_writes_reachable_sensors(
        self,
        fake_hub: FakeHub,
        dry_run_influx: InfluxOutput,
        gate: ReachabilityGate,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        failures = poll_and_output_sensors(fake_hub, dry_run_influx, gate, 3)

        out = capsys.readouterr().out
        assert failures == 0  # a successful poll resets the counter
        assert out.count("(InfluxDB: sent)") == 2
        assert out.count("INFLUX RAW DATA:") == 2

    def test_unreachable_sensor_is_printed_but_not_written(
        self,
        fake_hub: FakeHub,
        dry_run_influx: InfluxOutput,
        gate: ReachabilityGate,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_hub.devices[TIMMERFLOTE_ID]["isReachable"] = False

        poll_and_output_sensors(fake_hub, dry_run_influx, gate, 0)

        out = capsys.readouterr().out
        assert "Bedroom [timmerflote-0002, offline" in out
        assert "(InfluxDB: skipped, device offline)" in out
        raw_lines = [line for line in out.splitlines() if line.startswith("INFLUX RAW")]
        assert len(raw_lines) == 1
        assert "sensor_id=alpstuga-0001" in raw_lines[0]

    def test_recovery_hold_across_polls(
        self,
        fake_hub: FakeHub,
        dry_run_influx: InfluxOutput,
        gate: ReachabilityGate,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_hub.devices[TIMMERFLOTE_ID]["isReachable"] = False
        poll_and_output_sensors(fake_hub, dry_run_influx, gate, 0)
        poll_and_output_sensors(fake_hub, dry_run_influx, gate, 0)
        fake_hub.devices[TIMMERFLOTE_ID]["isReachable"] = True
        capsys.readouterr()

        poll_and_output_sensors(fake_hub, dry_run_influx, gate, 0)

        out = capsys.readouterr().out
        assert "(InfluxDB: recovery hold, 30m 00s left)" in out
        assert "sensor_id=timmerflote-0002" not in out

    def test_no_annotation_when_influx_inactive(
        self,
        fake_hub: FakeHub,
        base_env: None,
        gate: ReachabilityGate,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        influx = InfluxOutput(Settings.from_environment())

        poll_and_output_sensors(fake_hub, influx, gate, 0)

        assert "InfluxDB" not in capsys.readouterr().out

    def test_failure_increments_counter(
        self,
        fake_hub: FakeHub,
        dry_run_influx: InfluxOutput,
        gate: ReachabilityGate,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fake_hub.get_error = ConnectionError("hub down")

        assert poll_and_output_sensors(fake_hub, dry_run_influx, gate, 2) == 3
        assert "consecutive failures: 3" in caplog.text

    def test_empty_hub_logs_warning(
        self,
        dry_run_influx: InfluxOutput,
        gate: ReachabilityGate,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        assert poll_and_output_sensors(FakeHub(), dry_run_influx, gate, 0) == 0
        assert "No environment sensors" in caplog.text


class TestMain:
    def test_invalid_configuration_exits_with_2(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        assert read_sensors.main() == 2
        assert "DIRIGERA_IP is empty" in caplog.text

    def test_run_stops_on_stop_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        base_env: None,
        fake_hub: FakeHub,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(read_sensors, "Hub", lambda **_kwargs: fake_hub)
        # Stop after the first polling cycle.
        original = read_sensors.poll_and_output_sensors

        def poll_once(*args, **kwargs):  # type: ignore[no-untyped-def]
            result = original(*args, **kwargs)
            read_sensors.STOP_EVENT.set()
            return result

        monkeypatch.setattr(read_sensors, "poll_and_output_sensors", poll_once)
        monkeypatch.setattr(read_sensors, "install_signal_handlers", lambda: None)
        read_sensors.STOP_EVENT.clear()

        try:
            assert read_sensors.main() == 0
        finally:
            read_sensors.STOP_EVENT.clear()

        assert "Living room [alpstuga-0001, online" in capsys.readouterr().out
