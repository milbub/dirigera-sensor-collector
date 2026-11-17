# dirigera-sensor-collector

A small Python service that reads IKEA environment sensors
(**ALPSTUGA** air quality monitor, **TIMMERFLOTE** temperature/humidity
sensor, and any other device the hub reports as `environmentSensor`, e.g.
VINDSTYRKA) through an **IKEA DIRIGERA** hub and writes the readings to
**InfluxDB 2.x**.

It can also switch the ALPSTUGA display off at night and back on in the
morning on a local-time schedule.

## Features

- Polls all environment sensors every `POLL_INTERVAL_SECONDS` (default 10 s).
- Merges logical DIRIGERA endpoints (`<id>_1`, `<id>_2`, …) of one physical
  device into a single reading tagged with the physical device ID.
- Prints a human-readable line per sensor to stdout (journald when running
  as a service).
- Writes to InfluxDB with configurable measurement name and optional
  filtering of fields and tags; a dry-run mode prints line protocol instead.
- Skips readings of unreachable devices and, after a longer outage, holds
  writes for a recovery interval so stale or unsettled values never reach
  InfluxDB (see [Reachability and recovery hold](#reachability-and-recovery-hold)).
- ALPSTUGA display scheduler (overnight intervals supported), evaluated once
  per second independently of sensor polling, with validation before and
  verification after every change.
- Standalone `alpstuga-display on|off` command to switch displays manually.
- Robust long-running loop: transient hub or InfluxDB errors are logged and
  retried, clean shutdown on `SIGINT`/`SIGTERM`.
- systemd installer with hardening options.

## Requirements

- Python **3.10+** (uses `dataclass(slots=True)` and `X | Y` type syntax)
- IKEA DIRIGERA hub reachable on the local network and an API token
- InfluxDB 2.x (optional – only when writing is enabled)

Runtime dependencies are pinned exactly in `requirements.txt`, which
`pyproject.toml` reads, so installing the package installs these versions:

| Package | Version |
| --- | --- |
| [`dirigera`](https://github.com/Leggin/dirigera) | 1.6.1 |
| [`influxdb-client`](https://github.com/influxdata/influxdb-client-python) | 1.50.0 |

## Installation

```bash
git clone <repository-url>
cd dirigera-sensor-collector
python3 -m venv .venv
.venv/bin/pip install -e .
```

The editable install (`-e`) makes a `git pull` take effect after a service
restart, without reinstalling. It provides two commands in `.venv/bin/`:

| Command | Purpose |
| --- | --- |
| `dirigera-sensor-collector` | The collector service (same as `python -m dirigera_sensor_collector`) |
| `alpstuga-display on\|off` | Switch ALPSTUGA displays manually |

Create the configuration from the example and fill in at least
`DIRIGERA_IP` and `DIRIGERA_TOKEN`:

```bash
cp .env.example .env && chmod 640 .env
```

### Get a DIRIGERA token

The `dirigera` package installs a `generate-token` command. Run it, press the
action button on the bottom of the hub when prompted, and confirm:

```bash
.venv/bin/generate-token <hub-ip>
```

The token grants full control over the hub – keep it only in `.env`
(already ignored by git).

### Find device IDs

Sensor IDs appear in the output of `dirigera-sensor-collector`. For the
ALPSTUGA display scheduler you need the **exact endpoint ID including the
`_1` suffix** (the normalized ID printed by the collector has the suffix
removed). List them with:

```bash
.venv/bin/python -c "import os, dirigera; h = dirigera.Hub(token=os.environ['DIRIGERA_TOKEN'], ip_address=os.environ['DIRIGERA_IP']); [print(d['id'], d['attributes'].get('model'), d['attributes'].get('customName')) for d in h.get('/devices') if d['deviceType'] == 'environmentSensor']"
```

## Configuration

All configuration is read from environment variables. For the systemd
service they live in `.env` in the project directory, using plain
`NAME=value` lines (no `export`, no `source`). [`.env.example`](.env.example)
lists every variable with its default.

| Variable | Default | Description |
| --- | --- | --- |
| `DIRIGERA_IP` | – (required) | Hub IP address |
| `DIRIGERA_TOKEN` | – (required) | Hub API token |
| `POLL_INTERVAL_SECONDS` | `10` | Sensor polling interval, must be > 0 |
| `LOG_LEVEL` | `INFO` | Python log level (`DEBUG` shows endpoint merging) |
| `INFLUX_ENABLED` | `false` | Write points to InfluxDB |
| `INFLUX_DRY_RUN` | `false` | Print line protocol, never connect; overrides `INFLUX_ENABLED` |
| `INFLUX_URL` | `http://localhost:8086` | InfluxDB URL |
| `INFLUX_TOKEN` | – | Required when writing is enabled |
| `INFLUX_ORG` | – | Required when writing is enabled |
| `INFLUX_BUCKET` | `sensors` | Target bucket |
| `INFLUX_MEASUREMENT` | `ikea_environment_sensor` | Measurement name |
| `INFLUX_IGNORED_FIELDS` | – | Comma-separated fields to skip |
| `INFLUX_IGNORED_TAGS` | – | Comma-separated tags to skip |
| `INFLUX_RECOVERY_HOLD_SECONDS` | `1800` | Recovery hold after a longer outage; `0` disables the hold |
| `INFLUX_RECOVERY_HOLD_MIN_UNREACHABLE_SAMPLES` | `2` | Consecutive unreachable readings that trigger the hold, ≥ 1 |
| `ALPSTUGA_DISPLAY_CONTROL_IDS` | – | Comma-separated ALPSTUGA endpoint IDs (with `_1`); enables the scheduler |
| `ALPSTUGA_DISPLAY_CONTROL_OFF_TIME` | – | Display off time, `HH:MM:SS`, system local time |
| `ALPSTUGA_DISPLAY_CONTROL_ON_TIME` | – | Display on time, `HH:MM:SS`, system local time |

Boolean variables accept `true/false`, `1/0`, `yes/no`, `on/off`.

Example `.env` with InfluxDB and the display schedule enabled:

```ini
DIRIGERA_IP=192.168.1.20
DIRIGERA_TOKEN=replace-me

INFLUX_ENABLED=true
INFLUX_URL=http://localhost:8086
INFLUX_TOKEN=replace-me
INFLUX_ORG=home
INFLUX_BUCKET=sensors

ALPSTUGA_DISPLAY_CONTROL_IDS=00000000-0000-0000-0000-000000000000_1
ALPSTUGA_DISPLAY_CONTROL_OFF_TIME=22:00:00
ALPSTUGA_DISPLAY_CONTROL_ON_TIME=06:30:00
```

## Usage

Run in the foreground (loads variables from `.env` into the shell first):

```bash
set -a; . ./.env; set +a; .venv/bin/dirigera-sensor-collector
```

Example output:

```text
Living room [a1b2c3…, online, room=Living room]: 22.5 °C, 41 % RH, CO₂ 650 ppm, PM2.5 3 µg/m³ (InfluxDB: sent)
Bedroom [d4e5f6…, online, room=Bedroom]: 19.1 °C, 55 % RH, battery 87 % (InfluxDB: recovery hold, 12m 30s left)
```

The `(InfluxDB: …)` suffix is shown only when InfluxDB writing or dry-run is
enabled.

Test the InfluxDB mapping without writing anything:

```bash
INFLUX_DRY_RUN=true .venv/bin/dirigera-sensor-collector
```

Switch ALPSTUGA displays manually:

```bash
.venv/bin/alpstuga-display off
```

```bash
.venv/bin/alpstuga-display on
```

## InfluxDB data model

One point per physical device and polling cycle:

- **Measurement:** `INFLUX_MEASUREMENT`
- **Tags:** `sensor_id` (physical ID without endpoint suffix), `name`,
  `model`, `room`
- **Fields:** `temperature` (float, °C), `humidity` (int, % RH), `co2`
  (int, ppm), `pm25` (int, µg/m³), `voc_index` (int), `battery_percentage`
  (int, %)
- **Timestamp:** UTC, taken when the reading was fetched

Fields a device does not report are omitted (e.g. TIMMERFLOTE has no `co2`,
ALPSTUGA has no `battery_percentage`).

Reachability is not stored as a field: points are written only for
reachable devices outside a recovery hold, so a gap in the series means the
device was offline or settling.

## Reachability and recovery hold

When the hub reports a device as unreachable it still returns the last known
values, and right after reconnecting the measurements may need time to
settle. Each polling cycle therefore decides per physical device:

1. **Unreachable reading** (`reachable=False`) – not written.
2. **Longer outage** – when a device is unreachable for at least
   `INFLUX_RECOVERY_HOLD_MIN_UNREACHABLE_SAMPLES` consecutive readings
   (default 2), a recovery hold of `INFLUX_RECOVERY_HOLD_SECONDS` (default
   30 minutes) starts at the first reachable reading afterwards. No readings
   are written until the hold ends; then everything works as usual.
3. **Outage during the hold** – any unreachable reading cancels the running
   hold; it starts again from zero after the next reconnect.

A single unreachable reading between two reachable ones is skipped but
does not start a hold. Failed polls (hub not responding) produce no
readings, so they neither count towards nor interrupt a series of
unreachable readings. Transitions are logged at `INFO` level.

The state is kept in memory only; after a service restart all devices
start without a hold.

## Running as a systemd service

`deploy/install_service.sh` creates, validates, enables and (re)starts
`dirigera-sensors.service`, which runs `python -m dirigera_sensor_collector`
from the virtual environment. It is idempotent – run it again after changes.

Complete the [installation](#installation) steps first (package installed
in `.venv/` and a `.env` file), then run from the project directory:

```bash
sudo deploy/install_service.sh
```

The installer checks that the package imports as the service user and
warns if it is not an editable install of the project directory. By
default the service:

- uses the repository containing `deploy/` as the project directory,
- runs as the user who invoked `sudo` (or the owner of the project
  directory) and that user's primary group – never as root,
- uses `.venv/bin/python` and `.env` from the project directory.

Override any of these with options:

| Option | Default |
| --- | --- |
| `-u`, `--user USER` | `$SUDO_USER`, otherwise owner of the project directory |
| `-g`, `--group GROUP` | primary group of the user |
| `-d`, `--project-dir DIR` | parent of `deploy/` |
| `-p`, `--python PATH` | `DIR/.venv/bin/python` |
| `-e`, `--env-file PATH` | `DIR/.env` |
| `-n`, `--service-name NAME` | `dirigera-sensors` |

```bash
sudo deploy/install_service.sh --user sensors --project-dir /opt/dirigera-sensor-collector
```

Paths must not contain whitespace, `%`, `"` or `\` (they would need escaping
in the systemd unit).

Follow the logs:

```bash
journalctl -u dirigera-sensors.service -f
```

Remove the service (project files are left untouched):

```bash
sudo deploy/install_service.sh --uninstall
```

### Updating

```bash
git pull
```

```bash
.venv/bin/pip install -e .
```

```bash
sudo systemctl restart dirigera-sensors.service
```

Reinstalling is only needed when dependencies or entry points changed, but
it is always safe.

### Upgrading from the single-file layout

Earlier versions ran `read_sensors.py` directly. After pulling this
version, install the package and re-run the installer so the unit uses the
new command; `.env` stays unchanged:

```bash
.venv/bin/pip install -e .
```

```bash
sudo deploy/install_service.sh
```

## Project structure

```text
├── pyproject.toml           package metadata, commands, pytest config
├── requirements.txt         pinned runtime dependencies
├── requirements-dev.txt     + pytest and an editable install
├── .env.example             configuration template
├── deploy/
│   └── install_service.sh   systemd service installer / uninstaller
├── src/dirigera_sensor_collector/
│   ├── __main__.py          python -m dirigera_sensor_collector
│   ├── config.py            Settings loaded from environment variables
│   ├── sensors.py           sensor normalization and endpoint merging
│   ├── reachability.py      unreachable filtering and recovery hold
│   ├── influx.py            InfluxDB points, writes and dry-run
│   ├── display.py           ALPSTUGA validation and display switching
│   ├── display_cli.py       alpstuga-display command
│   ├── scheduler.py         local-time display schedule
│   └── service.py           polling loop and entry point
└── tests/                   pytest suite, one module per source module
```

## Development

Install the development dependencies and run the test suite. It uses an
in-memory fake hub, so no DIRIGERA hub, InfluxDB, root or systemd is needed:

```bash
.venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/pytest
```

## License

[MIT](LICENSE)
