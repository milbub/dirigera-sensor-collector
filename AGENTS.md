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

Small, dependency-light Python service – no package, no framework. Source
files are flat in the repository root, tests are in `tests/`:

- `read_sensors.py` – main long-running service.
  - `Settings.from_environment()` loads and validates all env variables;
    invalid configuration raises `ValueError` → exit code 2.
  - `SensorReading.from_sensor()` maps a `dirigera.EnvironmentSensor`
    model to a normalized reading (UTC timestamp).
  - `merge_sensor_readings()` groups logical endpoints `<id>_N` into one
    physical device; endpoint `_1` has priority for the name and for
    values, then the un-suffixed ID, then higher endpoints.
  - `ReachabilityGate` decides per physical device whether a reading is
    written: unreachable readings are skipped, and a longer outage starts
    an in-memory recovery hold (monotonic clock) after the reconnect. The
    rules are documented in README "Reachability and recovery hold" – keep
    both in sync.
  - `InfluxOutput` builds `influxdb_client.Point`s; it imports
    `influxdb_client` lazily so the service runs without it when InfluxDB
    is disabled. Dry-run never opens a connection.
  - `LocalAlpstugaDisplayScheduler` + `run()` – single-threaded loop that
    wakes at least once per second for the display schedule and polls
    sensors on a monotonic clock.
- `alpstuga_display.py` – importable library + CLI (`on`/`off`). Uses raw
  `hub.get()` / `hub.patch()` on `/devices/<id>` with `{"isOn": bool}`
  because the `dirigera` library has no typed API for the ALPSTUGA
  display. All devices are validated before the first PATCH; the state is
  re-read after the PATCH to verify it. Scheduling itself lives in
  `read_sensors.py` (`LocalAlpstugaDisplayScheduler`); this module only
  provides the pure `desired_display_state()` helper.
- `install_service.sh` – idempotent systemd installer/uninstaller. Nothing
  is hard-coded: project dir defaults to the script's directory, user to
  `$SUDO_USER` (or the project dir owner), all overridable by options.
  Keep it generic – no personal usernames, hosts or paths.

## Conventions

- Python ≥ 3.10, `from __future__ import annotations`, full type hints,
  frozen `slots=True` dataclasses, docstrings on every function/class.
- Code, comments, logs and docs in English.
- Keep the existing defensive style: a failing poll or display change must
  be logged and retried, never crash the loop.
- Time handling: InfluxDB timestamps are **UTC**; the display schedule and
  log timestamps use **system local time**. Do not mix them.
- Configuration only via environment variables; document every new
  variable in the README configuration table.

## Dependencies

- Versions are pinned exactly in `requirements.txt` and
  `requirements-dev.txt` (`==`). When updating,
  diff the library source between versions and reflect useful changes.
- `dirigera` ≥ 1.3 reuses one `requests.Session` per `Hub` (keep-alive,
  connections idle > 3 s are dropped, GET/PATCH retried once on a
  connection error). `Hub` is not meant to be shared across threads
  without care – the service is single-threaded.
- `dirigera` models are pydantic v2; constructing them requires a real
  `AbstractSmartHomeHub` subclass as `dirigeraClient` (relevant for fakes
  in tests).

## Tests

The pytest suite lives in `tests/` and needs no hub, InfluxDB, root or
systemd:

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
- InfluxDB writes are tested by monkeypatching
  `influxdb_client.InfluxDBClient`; dry-run output is asserted as exact
  line protocol.

Every new feature or bug fix comes with tests. Run the whole suite before
each commit; it must pass.

Never write to a real InfluxDB or toggle real displays without the user's
consent.
