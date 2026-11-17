"""Control IKEA ALPSTUGA displays through a DIRIGERA hub.

Displays are switched by patching the ``isOn`` attribute of the ALPSTUGA
endpoint, because the dirigera library offers no typed API for it. All
devices are validated before the first change and verified afterwards.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import time
from typing import Any, Protocol, Sequence


LOGGER = logging.getLogger("alpstuga-display")


class DirigeraHubProtocol(Protocol):
    """Minimal interface required from a DIRIGERA Hub instance."""

    def get(self, route: str) -> Any:
        """Send a GET request to the hub."""

    def patch(self, route: str, data: Any) -> Any:
        """Send a PATCH request to the hub."""


@dataclass(frozen=True, slots=True)
class DisplayUpdateResult:
    """Result of one ALPSTUGA display update."""

    device_id: str
    name: str
    previous_state: bool | None
    requested_state: bool
    reported_state: bool | None


def parse_device_ids(raw_value: str) -> tuple[str, ...]:
    """Parse a comma-separated list of DIRIGERA device IDs.

    Empty entries and surrounding whitespace are ignored. Duplicate IDs are
    removed while preserving their original order.
    """
    unique_ids: list[str] = []
    seen: set[str] = set()

    for raw_item in raw_value.split(","):
        device_id = raw_item.strip()

        if not device_id or device_id in seen:
            continue

        seen.add(device_id)
        unique_ids.append(device_id)

    return tuple(unique_ids)


def is_alpstuga_device(device: dict[str, Any]) -> bool:
    """Return whether raw DIRIGERA data represents an ALPSTUGA device."""
    attributes = device.get("attributes")

    if not isinstance(attributes, dict):
        return False

    model = str(attributes.get("model", "")).strip().upper()
    product_code = str(
        attributes.get("productCode", "")
    ).strip().upper()

    return "ALPSTUGA" in model or product_code == "E2495"


def get_device(
    hub: DirigeraHubProtocol,
    device_id: str,
) -> dict[str, Any]:
    """Fetch one raw DIRIGERA device and validate its response."""
    response = hub.get(route=f"/devices/{device_id}")

    if not isinstance(response, dict):
        raise RuntimeError(
            f"DIRIGERA returned an invalid response for device {device_id!r}"
        )

    return response


def validate_alpstuga_display(
    device: dict[str, Any],
    expected_device_id: str,
) -> None:
    """Ensure a device is an ALPSTUGA supporting display control."""
    actual_device_id = str(device.get("id", ""))

    if actual_device_id != expected_device_id:
        raise RuntimeError(
            f"Requested device {expected_device_id!r}, but DIRIGERA returned "
            f"{actual_device_id!r}"
        )

    if not is_alpstuga_device(device):
        attributes = device.get("attributes", {})
        model = (
            attributes.get("model")
            if isinstance(attributes, dict)
            else None
        )

        raise RuntimeError(
            f"Device {expected_device_id!r} is not recognized as ALPSTUGA "
            f"(reported model: {model!r})"
        )

    capabilities = device.get("capabilities")

    if not isinstance(capabilities, dict):
        raise RuntimeError(
            f"Device {expected_device_id!r} has no valid capabilities"
        )

    can_receive = capabilities.get("canReceive", [])

    if not isinstance(can_receive, list) or "isOn" not in can_receive:
        raise RuntimeError(
            f"ALPSTUGA device {expected_device_id!r} does not advertise "
            "the isOn capability required for display control"
        )


def set_alpstuga_displays(
    hub: DirigeraHubProtocol,
    device_ids: Sequence[str],
    enabled: bool,
    *,
    verify: bool = True,
) -> list[DisplayUpdateResult]:
    """Turn one or more ALPSTUGA displays on or off.

    All devices are fetched and validated before the first PATCH request is
    sent. This prevents a malformed ID later in the list from causing an
    avoidable partial update.

    Args:
        hub: Connected ``dirigera.Hub`` instance.
        device_ids: Exact ALPSTUGA endpoint IDs, usually ending in ``_1``.
        enabled: ``True`` to turn displays on, ``False`` to turn them off.
        verify: Read devices after PATCH and verify their reported ``isOn``.

    Returns:
        A result for every updated display.

    Raises:
        ValueError: If no device IDs were provided.
        RuntimeError: If validation or post-write verification fails.
    """
    normalized_ids = parse_device_ids(",".join(device_ids))

    if not normalized_ids:
        raise ValueError("No ALPSTUGA display device IDs were provided")

    devices: list[dict[str, Any]] = []

    # Validate all configured devices before changing any of them.
    for device_id in normalized_ids:
        device = get_device(hub, device_id)
        validate_alpstuga_display(device, device_id)
        devices.append(device)

    results: list[DisplayUpdateResult] = []

    for device in devices:
        device_id = str(device["id"])
        attributes = device.get("attributes", {})
        name = str(
            attributes.get("customName")
            or attributes.get("model")
            or device_id
        ).strip()

        raw_previous_state = attributes.get("isOn")
        previous_state = (
            raw_previous_state
            if isinstance(raw_previous_state, bool)
            else None
        )

        hub.patch(
            route=f"/devices/{device_id}",
            data=[
                {
                    "attributes": {
                        "isOn": enabled,
                    }
                }
            ],
        )

        reported_state: bool | None = None

        if verify:
            updated_device = get_device(hub, device_id)
            updated_attributes = updated_device.get("attributes", {})
            raw_reported_state = (
                updated_attributes.get("isOn")
                if isinstance(updated_attributes, dict)
                else None
            )

            reported_state = (
                raw_reported_state
                if isinstance(raw_reported_state, bool)
                else None
            )

            if reported_state is not enabled:
                requested = "on" if enabled else "off"

                raise RuntimeError(
                    f"ALPSTUGA device {device_id!r} did not report the "
                    f"requested display state {requested!r}; "
                    f"reported isOn={raw_reported_state!r}"
                )

        results.append(
            DisplayUpdateResult(
                device_id=device_id,
                name=name,
                previous_state=previous_state,
                requested_state=enabled,
                reported_state=reported_state,
            )
        )

    return results


def desired_display_state(
    current_time: time,
    off_time: time,
    on_time: time,
) -> bool:
    """Calculate whether displays should currently be on.

    The interval beginning at ``off_time`` and ending at ``on_time`` is the
    off interval. Overnight schedules are supported.

    Example:

        off_time = 22:00:00
        on_time  = 06:00:00

    Displays are off from 22:00:00 through 05:59:59 and on otherwise.
    """
    if off_time == on_time:
        raise ValueError(
            "ALPSTUGA display on and off times must be different"
        )

    if off_time < on_time:
        is_off_interval = off_time <= current_time < on_time
    else:
        is_off_interval = (
            current_time >= off_time
            or current_time < on_time
        )

    return not is_off_interval
