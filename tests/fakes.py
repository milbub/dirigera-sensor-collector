"""Test doubles: raw DIRIGERA payload builders and an in-memory hub."""

from __future__ import annotations

import copy
from typing import Any

from dirigera import EnvironmentSensor
from dirigera.hub.abstract_smart_home_hub import AbstractSmartHomeHub


# Every environment variable read by the application. Each test starts with
# none of them set, so the developer's shell cannot influence results.
APP_ENV_PREFIXES = (
    "DIRIGERA_",
    "INFLUX_",
    "ALPSTUGA_",
    "POLL_INTERVAL_SECONDS",
    "LOG_LEVEL",
)

ALPSTUGA_ID = "alpstuga-0001_1"
TIMMERFLOTE_ID = "timmerflote-0002"


def make_device(
    device_id: str,
    *,
    model: str = "ALPSTUGA air quality monitor",
    custom_name: str = "Living room",
    product_code: str | None = "E2495",
    reachable: bool = True,
    room: str | None = "Living room",
    can_receive: list[str] | None = None,
    **attributes: Any,
) -> dict[str, Any]:
    """Build a raw ``/devices/<id>`` payload as returned by DIRIGERA."""
    return {
        "id": device_id,
        "type": "sensor",
        "deviceType": "environmentSensor",
        "createdAt": "2026-01-01T00:00:00.000Z",
        "isReachable": reachable,
        "lastSeen": "2026-01-01T00:00:00.000Z",
        "attributes": {
            "customName": custom_name,
            "model": model,
            "manufacturer": "IKEA of Sweden",
            "firmwareVersion": "1.0.0",
            "hardwareVersion": "1",
            "productCode": product_code,
            **attributes,
        },
        "capabilities": {
            "canSend": [],
            "canReceive": (
                ["customName", "isOn"]
                if can_receive is None
                else can_receive
            ),
        },
        "room": (
            {"id": "room-1", "name": room, "color": "c", "icon": "i"}
            if room is not None
            else None
        ),
        "deviceSet": [],
        "remoteLinks": [],
    }


def alpstuga_device(**overrides: Any) -> dict[str, Any]:
    """Raw payload of an ALPSTUGA air quality monitor with display on."""
    values: dict[str, Any] = {
        "currentTemperature": 22.5,
        "currentRH": 41,
        "currentCO2": 650,
        "currentPM25": 3,
        "isOn": True,
    }
    values.update(overrides)
    return make_device(ALPSTUGA_ID, **values)


def timmerflote_device(**overrides: Any) -> dict[str, Any]:
    """Raw payload of a battery-powered TIMMERFLOTE sensor."""
    values: dict[str, Any] = {
        "model": "TIMMERFLOTE temp/hmd sensor",
        "custom_name": "Bedroom",
        "product_code": "E2112",
        "room": "Bedroom",
        "can_receive": ["customName"],
        "currentTemperature": 19.1,
        "currentRH": 55,
        "batteryPercentage": 87,
    }
    values.update(overrides)
    return make_device(TIMMERFLOTE_ID, **values)


class FakeHub(AbstractSmartHomeHub):
    """In-memory DIRIGERA hub implementing the subset used by the code."""

    def __init__(self, devices: list[dict[str, Any]] | None = None) -> None:
        self.devices: dict[str, dict[str, Any]] = {
            device["id"]: device for device in devices or []
        }
        self.calls: list[tuple[str, str]] = []
        # When set, PATCH requests are recorded but not applied, simulating
        # a device that ignores the command.
        self.ignore_patches = False
        # When set, every GET raises this exception.
        self.get_error: Exception | None = None

    def get(self, route: str) -> Any:
        self.calls.append(("GET", route))

        if self.get_error is not None:
            raise self.get_error

        if route == "/devices":
            return copy.deepcopy(list(self.devices.values()))

        device_id = route.removeprefix("/devices/")
        return copy.deepcopy(self.devices[device_id])

    def patch(self, route: str, data: list[dict[str, Any]]) -> Any:
        self.calls.append(("PATCH", route))

        if not self.ignore_patches:
            device_id = route.removeprefix("/devices/")
            for change in data:
                self.devices[device_id]["attributes"].update(
                    change["attributes"]
                )

        return ""

    def post(self, route: str, data: Any = None) -> Any:
        raise NotImplementedError

    def delete(self, route: str, data: Any = None) -> Any:
        raise NotImplementedError

    def get_environment_sensors(self) -> list[EnvironmentSensor]:
        """Mirror ``dirigera.Hub.get_environment_sensors``."""
        return [
            EnvironmentSensor(dirigeraClient=self, **device)
            for device in self.get("/devices")
            if device["deviceType"] == "environmentSensor"
        ]

    def patched_ids(self) -> list[str]:
        """Return device IDs that received a PATCH, in order."""
        return [
            route.removeprefix("/devices/")
            for method, route in self.calls
            if method == "PATCH"
        ]
