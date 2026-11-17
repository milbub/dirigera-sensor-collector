"""Basic checks of install_service.sh that need neither root nor systemd."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_DIR / "deploy" / "install_service.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash is not available",
)


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_help() -> None:
    result = run_script("--help")

    assert result.returncode == 0
    assert "Usage:" in result.stdout
    for option in (
        "--user",
        "--group",
        "--project-dir",
        "--python",
        "--env-file",
        "--service-name",
        "--uninstall",
    ):
        assert option in result.stdout
    # The default project directory is the repository root, not deploy/.
    assert f"(default: {PROJECT_DIR})" in result.stdout


def test_unknown_option() -> None:
    result = run_script("--bogus")

    assert result.returncode == 1
    assert "Unknown option: --bogus" in result.stderr


def test_option_requires_value() -> None:
    result = run_script("--user")

    assert result.returncode == 1
    assert "--user requires a value" in result.stderr


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="must run as an unprivileged user",
)
def test_requires_root() -> None:
    result = run_script()

    assert result.returncode == 1
    assert "Run this installer as root" in result.stderr


def test_contains_no_hardcoded_user_paths() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert "/home/" not in content
