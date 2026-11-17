"""Command-line tool to switch ALPSTUGA displays on or off.

Usage:

    alpstuga-display on
    alpstuga-display off

Required environment variables:

    DIRIGERA_IP
    DIRIGERA_TOKEN
    ALPSTUGA_DISPLAY_CONTROL_IDS

Multiple device IDs in ALPSTUGA_DISPLAY_CONTROL_IDS must be separated by
commas. Use the real DIRIGERA endpoint IDs, including their ``_1`` suffix.
"""

from __future__ import annotations

import logging
import os
import sys

from dirigera import Hub

from .display import parse_device_ids, set_alpstuga_displays


LOGGER = logging.getLogger("alpstuga-display")


def parse_display_state(raw_value: str) -> bool:
    """Convert a command-line display state to a boolean."""
    normalized = raw_value.strip().lower()

    if normalized in {"on", "true", "1"}:
        return True

    if normalized in {"off", "false", "0"}:
        return False

    raise ValueError(
        "Display state must be one of: on, off, true, false, 1, 0"
    )


def configure_logging() -> None:
    """Configure logging for standalone command-line use."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    """Run ALPSTUGA display control as a standalone command."""
    configure_logging()

    if len(sys.argv) != 2:
        print(
            f"Usage: {sys.argv[0]} on|off",
            file=sys.stderr,
        )
        return 2

    try:
        enabled = parse_display_state(sys.argv[1])

        hub_ip = os.getenv("DIRIGERA_IP", "").strip()
        token = os.getenv("DIRIGERA_TOKEN", "").strip()
        device_ids = parse_device_ids(
            os.getenv("ALPSTUGA_DISPLAY_CONTROL_IDS", "")
        )

        if not hub_ip:
            raise ValueError(
                "DIRIGERA_IP is empty. "
                "Set it to the IP address of your DIRIGERA hub."
            )

        if not token:
            raise ValueError("DIRIGERA_TOKEN must not be empty")

        if not device_ids:
            raise ValueError(
                "ALPSTUGA_DISPLAY_CONTROL_IDS must contain at least "
                "one ALPSTUGA endpoint ID"
            )

        hub = Hub(
            token=token,
            ip_address=hub_ip,
        )

        results = set_alpstuga_displays(
            hub=hub,
            device_ids=device_ids,
            enabled=enabled,
            verify=True,
        )

        requested_state = "on" if enabled else "off"

        for result in results:
            LOGGER.info(
                "ALPSTUGA display %s (%s) is now %s; "
                "previous isOn=%r, reported isOn=%r",
                result.name,
                result.device_id,
                requested_state,
                result.previous_state,
                result.reported_state,
            )

    except (ValueError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 2
    except Exception:
        LOGGER.exception("Unexpected ALPSTUGA display control error")
        return 1

    return 0
