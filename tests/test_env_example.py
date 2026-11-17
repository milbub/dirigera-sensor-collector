"""Keep .env.example and the README in sync with the variables the code reads."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dirigera_sensor_collector.config import Settings

PROJECT_DIR = Path(__file__).resolve().parent.parent
SOURCE_DIR = PROJECT_DIR / "src" / "dirigera_sensor_collector"
ENV_EXAMPLE = PROJECT_DIR / ".env.example"
README = PROJECT_DIR / "README.md"

ENV_READ_PATTERN = re.compile(
    r'(?:getenv|env_bool|env_float|env_int|env_csv_set)\(\s*"([A-Z][A-Z0-9_]+)"'
)


def variables_read_by_code() -> set[str]:
    names: set[str] = set()

    for path in SOURCE_DIR.glob("*.py"):
        names.update(ENV_READ_PATTERN.findall(path.read_text(encoding="utf-8")))

    return names


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip()

    return values


def test_code_reads_known_variables() -> None:
    # Guards the regex above: if it stops matching, the sync tests below
    # would pass vacuously.
    assert {"DIRIGERA_IP", "INFLUX_RECOVERY_HOLD_SECONDS"} <= variables_read_by_code()


@pytest.mark.parametrize("document", [ENV_EXAMPLE, README], ids=lambda p: p.name)
def test_every_variable_is_documented(document: Path) -> None:
    text = document.read_text(encoding="utf-8")

    missing = sorted(
        name for name in variables_read_by_code() if name not in text
    )

    assert missing == []


def test_env_example_uses_systemd_syntax() -> None:
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", stripped), line


def test_env_example_is_a_valid_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in parse_env_file(ENV_EXAMPLE).items():
        monkeypatch.setenv(name, value)

    settings = Settings.from_environment()

    assert settings.influx_enabled is False
