"""Tests for the polling cycle and entry point (service.py)."""

from __future__ import annotations

import pytest

from dirigera_sensor_collector import service
from dirigera_sensor_collector.config import Settings
from dirigera_sensor_collector.influx import InfluxOutput
from dirigera_sensor_collector.reachability import ReachabilityGate
from dirigera_sensor_collector.service import poll_and_output_sensors
from fakes import TIMMERFLOTE_ID, FakeHub


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
        assert service.main() == 2
        assert "DIRIGERA_IP is empty" in caplog.text

    def test_run_stops_on_stop_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        base_env: None,
        fake_hub: FakeHub,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(service, "Hub", lambda **_kwargs: fake_hub)
        # Stop after the first polling cycle.
        original = service.poll_and_output_sensors

        def poll_once(*args, **kwargs):  # type: ignore[no-untyped-def]
            result = original(*args, **kwargs)
            service.STOP_EVENT.set()
            return result

        monkeypatch.setattr(service, "poll_and_output_sensors", poll_once)
        monkeypatch.setattr(service, "install_signal_handlers", lambda: None)
        service.STOP_EVENT.clear()

        try:
            assert service.main() == 0
        finally:
            service.STOP_EVENT.clear()

        assert "Living room [alpstuga-0001, online" in capsys.readouterr().out
