"""Tests for ALPSTUGA device logic (display.py)."""

from __future__ import annotations

from datetime import time

import pytest

from dirigera_sensor_collector.display import (
    desired_display_state,
    is_alpstuga_device,
    parse_device_ids,
    set_alpstuga_displays,
    validate_alpstuga_display,
)
from fakes import ALPSTUGA_ID, TIMMERFLOTE_ID, FakeHub, alpstuga_device, make_device


class TestParseDeviceIds:
    def test_parse_device_ids_strips_dedupes_and_keeps_order(self) -> None:
        assert parse_device_ids(" b_1, a_1,, b_1 ,c_1 ") == ("b_1", "a_1", "c_1")

    def test_parse_device_ids_empty(self) -> None:
        assert parse_device_ids("") == ()
        assert parse_device_ids(" , ,") == ()


class TestDesiredDisplayState:
    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            (time(21, 59, 59), True),
            (time(22, 0, 0), False),  # off time is inclusive
            (time(3, 0, 0), False),
            (time(5, 59, 59), False),
            (time(6, 0, 0), True),  # on time ends the off interval
            (time(12, 0, 0), True),
        ],
    )
    def test_overnight_schedule(self, current: time, expected: bool) -> None:
        assert (
            desired_display_state(current, off_time=time(22), on_time=time(6))
            is expected
        )

    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            (time(12, 59, 59), True),
            (time(13, 0, 0), False),
            (time(13, 59, 59), False),
            (time(14, 0, 0), True),
        ],
    )
    def test_same_day_schedule(self, current: time, expected: bool) -> None:
        assert (
            desired_display_state(current, off_time=time(13), on_time=time(14))
            is expected
        )

    def test_equal_times_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            desired_display_state(time(1), off_time=time(6), on_time=time(6))


class TestDeviceValidation:
    def test_recognized_by_model(self) -> None:
        assert is_alpstuga_device(alpstuga_device(product_code=None))

    def test_recognized_by_product_code(self) -> None:
        device = make_device(ALPSTUGA_ID, model="Unknown", product_code="E2495")
        assert is_alpstuga_device(device)

    def test_other_device_not_recognized(self) -> None:
        device = make_device(TIMMERFLOTE_ID, model="TIMMERFLOTE", product_code="E2112")
        assert not is_alpstuga_device(device)
        assert not is_alpstuga_device({"attributes": None})

    def test_id_mismatch(self) -> None:
        with pytest.raises(RuntimeError, match="but DIRIGERA returned"):
            validate_alpstuga_display(alpstuga_device(), "other_1")

    def test_missing_is_on_capability(self) -> None:
        device = alpstuga_device(can_receive=["customName"])
        with pytest.raises(RuntimeError, match="isOn capability"):
            validate_alpstuga_display(device, ALPSTUGA_ID)


class TestSetAlpstugaDisplays:
    def test_turns_display_off_and_verifies(self, fake_hub: FakeHub) -> None:
        results = set_alpstuga_displays(fake_hub, [ALPSTUGA_ID], enabled=False)

        assert fake_hub.devices[ALPSTUGA_ID]["attributes"]["isOn"] is False
        assert fake_hub.patched_ids() == [ALPSTUGA_ID]
        [result] = results
        assert result.device_id == ALPSTUGA_ID
        assert result.name == "Living room"
        assert result.previous_state is True
        assert result.requested_state is False
        assert result.reported_state is False

    def test_validates_all_devices_before_any_patch(self, fake_hub: FakeHub) -> None:
        with pytest.raises(RuntimeError, match="not recognized as ALPSTUGA"):
            set_alpstuga_displays(
                fake_hub, [ALPSTUGA_ID, TIMMERFLOTE_ID], enabled=False
            )

        assert fake_hub.patched_ids() == []
        assert fake_hub.devices[ALPSTUGA_ID]["attributes"]["isOn"] is True

    def test_verification_failure_raises(self, fake_hub: FakeHub) -> None:
        fake_hub.ignore_patches = True

        with pytest.raises(RuntimeError, match="did not report"):
            set_alpstuga_displays(fake_hub, [ALPSTUGA_ID], enabled=False)

    def test_without_verify_does_not_reread(self, fake_hub: FakeHub) -> None:
        fake_hub.ignore_patches = True

        [result] = set_alpstuga_displays(
            fake_hub, [ALPSTUGA_ID], enabled=False, verify=False
        )

        assert result.reported_state is None
        assert fake_hub.calls.count(("GET", f"/devices/{ALPSTUGA_ID}")) == 1

    def test_no_ids(self, fake_hub: FakeHub) -> None:
        with pytest.raises(ValueError, match="No ALPSTUGA"):
            set_alpstuga_displays(fake_hub, [" ", ""], enabled=True)
