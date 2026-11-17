"""Tests for ALPSTUGA display scheduling (scheduler.py)."""

from __future__ import annotations

from datetime import datetime, time

import pytest

from dirigera_sensor_collector.config import Settings
from dirigera_sensor_collector.scheduler import (
    LocalAlpstugaDisplayScheduler,
    apply_display_schedule,
    create_display_scheduler,
)
from fakes import ALPSTUGA_ID, FakeHub

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
