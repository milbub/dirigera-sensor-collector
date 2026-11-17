# AGENTS.md

Guidance for AI coding agents working in this repository. See
[README.md](README.md) for user-facing documentation.

## Git workflow

- **Commit automatically after each logical change.** One self-contained
  change = one commit (e.g. dependency bump, bug fix, refactor, docs).
  Do not batch unrelated changes into one commit.
- **Never push.** Pushing is done manually by the user.
- Commit messages: short imperative subject line in English (≤ 72 chars),
  blank line, optional body explaining *why*.
- Never commit secrets or personal data: `.env`, tokens, real device IDs,
  IP addresses, usernames or paths. Use obvious placeholders in docs.

## Project overview

Small Python service packaged with a src layout (`pyproject.toml`,
setuptools). Package `src/dirigera_sensor_collector/`, one concern per
module:

- `config.py` – `Settings.from_environment()` loads and validates all env
  variables; invalid configuration raises `ValueError` → exit code 2.
- `sensors.py` – `SensorReading.from_sensor()` maps a
  `dirigera.EnvironmentSensor` to a normalized reading (UTC timestamp);
  `merge_sensor_readings()` groups logical endpoints `<id>_N` into one
  physical device (endpoint `_1` first, then the un-suffixed ID, then
  higher endpoints).
- `reachability.py` – `ReachabilityGate` decides per physical device
  whether a reading is written: unreachable readings are skipped, and a
  longer outage starts an in-memory recovery hold (monotonic clock) after
  the reconnect. Rules are documented in README "Reachability and recovery
  hold" – keep both in sync.
- `influx.py` – `InfluxOutput` builds `influxdb_client.Point`s, imported
  lazily; dry-run never opens a connection.
- `display.py` – ALPSTUGA device logic. Uses raw `hub.get()` /
  `hub.patch()` on `/devices/<id>` with `{"isOn": bool}` because the
  `dirigera` library has no typed API for the display. All devices are
  validated before the first PATCH and re-read afterwards to verify.
- `display_cli.py` – `alpstuga-display on|off` command.
- `scheduler.py` – `LocalAlpstugaDisplayScheduler` (local wall clock) and
  the glue that applies it without stopping polling.
- `service.py` – single-threaded loop that wakes at least once per second
  for the display schedule and polls sensors on a monotonic clock; signal
  handling, logging setup, `main()`.

Entry points (`[project.scripts]`): `dirigera-sensor-collector` →
`service:main`, `alpstuga-display` → `display_cli:main`;
`python -m dirigera_sensor_collector` runs the collector. Logger names
(`dirigera-sensors`, `alpstuga-display`) are part of the log output users
see – do not rename them casually.

`deploy/install_service.sh` – idempotent systemd installer/uninstaller.
The unit runs `python -m dirigera_sensor_collector` from the project venv.
Nothing is hard-coded: project dir defaults to the parent of `deploy/`,
user to `$SUDO_USER` (or the project dir owner), all overridable by
options. Keep it generic – no personal usernames, hosts or paths.

## Conventions

- Python ≥ 3.10, `from __future__ import annotations`, full type hints,
  frozen `slots=True` dataclasses, docstrings on every function/class.
- Code, comments, logs and docs in English.
- Keep the existing defensive style: a failing poll or display change must
  be logged and retried, never crash the loop.
- Time handling: InfluxDB timestamps are **UTC**; the display schedule and
  log timestamps use **system local time**. Do not mix them.
- Configuration only via environment variables; document every new
  variable in the README configuration table and in `.env.example`
  (`tests/test_env_example.py` enforces this).
- Keep one concern per module; add a matching `tests/test_<module>.py`.

## Dependencies

- Runtime versions are pinned exactly (`==`) in `requirements.txt`, the
  single source of truth: `pyproject.toml` reads it via
  `[tool.setuptools.dynamic]`. Dev tools are pinned in
  `requirements-dev.txt`, which also installs the package in editable
  mode. When updating, diff the library source between versions and
  reflect useful changes.
- `dirigera` ≥ 1.3 reuses one `requests.Session` per `Hub` (keep-alive,
  connections idle > 3 s are dropped, GET/PATCH retried once on a
  connection error). `Hub` is not meant to be shared across threads
  without care – the service is single-threaded.
- `dirigera` models are pydantic v2; constructing them requires a real
  `AbstractSmartHomeHub` subclass as `dirigeraClient` (relevant for fakes
  in tests).

## Tests

The pytest suite lives in `tests/` (one test module per source module)
and runs against the installed package; it needs no hub, InfluxDB, root
or systemd:

```bash
.venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/pytest
```

- `tests/fakes.py` – raw DIRIGERA payload builders and `FakeHub`, an
  in-memory hub (subclass of `AbstractSmartHomeHub`, required by the
  pydantic models) that applies PATCHes and can simulate ignored patches
  or connection errors.
- `tests/conftest.py` – autouse fixture that clears every application
  environment variable, plus `fake_hub`, `base_env` and `make_reading`.
- Time-dependent logic takes an injectable time (`now=` monotonic for
  `ReachabilityGate`, `local_now=` for the display scheduler); tests pass
  explicit values instead of sleeping.
- Patch names where they are looked up, e.g. `service.Hub`,
  `display_cli.Hub`. InfluxDB writes are tested by monkeypatching
  `influxdb_client.InfluxDBClient`; dry-run output is asserted as exact
  line protocol.

Every new feature or bug fix comes with tests. Run the whole suite before
each commit; it must pass.

Never write to a real InfluxDB or toggle real displays without the user's
consent.
