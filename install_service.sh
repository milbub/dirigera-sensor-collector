#!/usr/bin/env bash
#
# Install, update or remove the systemd service for the DIRIGERA sensor reader.
#
# Usage:
#   sudo ./install_service.sh [options]
#   sudo ./install_service.sh --uninstall [--service-name NAME]
#
# Run with --help for all options. Without options, the service runs from the
# directory containing this script, as the user who invoked sudo.
#
# The script is idempotent: it can be run repeatedly to update the unit.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR

# Configuration; may be overridden by command-line options.
SERVICE_NAME="dirigera-sensors"
SERVICE_USER=""
SERVICE_GROUP=""
PROJECT_DIR="${SCRIPT_DIR}"
PYTHON_BIN=""
ENV_FILE=""
UNINSTALL=false

# Derived in resolve_configuration().
SERVICE_FILE=""
APPLICATION=""
DISPLAY_MODULE=""

log() {
    printf '[INFO] %s\n' "$*"
}

warn() {
    printf '[WARNING] %s\n' "$*" >&2
}

die() {
    printf '[ERROR] %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: sudo $0 [options]

Install or update a systemd service that runs read_sensors.py.

Options:
  -u, --user USER          Service user (default: the user who ran sudo,
                           otherwise the owner of the project directory)
  -g, --group GROUP        Service group (default: primary group of USER)
  -d, --project-dir DIR    Project directory (default: ${SCRIPT_DIR})
  -p, --python PATH        Python interpreter (default: DIR/.venv/bin/python)
  -e, --env-file PATH      Environment file (default: DIR/.env)
  -n, --service-name NAME  systemd unit name (default: ${SERVICE_NAME})
      --uninstall          Stop, disable and remove the service
  -h, --help               Show this help
EOF
}

parse_args() {
    while (($# > 0)); do
        case "$1" in
            -u | --user)
                [[ $# -ge 2 ]] || die "$1 requires a value"
                SERVICE_USER="$2"
                shift 2
                ;;
            -g | --group)
                [[ $# -ge 2 ]] || die "$1 requires a value"
                SERVICE_GROUP="$2"
                shift 2
                ;;
            -d | --project-dir)
                [[ $# -ge 2 ]] || die "$1 requires a value"
                PROJECT_DIR="$2"
                shift 2
                ;;
            -p | --python)
                [[ $# -ge 2 ]] || die "$1 requires a value"
                PYTHON_BIN="$2"
                shift 2
                ;;
            -e | --env-file)
                [[ $# -ge 2 ]] || die "$1 requires a value"
                ENV_FILE="$2"
                shift 2
                ;;
            -n | --service-name)
                [[ $# -ge 2 ]] || die "$1 requires a value"
                SERVICE_NAME="$2"
                shift 2
                ;;
            --uninstall)
                UNINSTALL=true
                shift
                ;;
            -h | --help)
                usage
                exit 0
                ;;
            *)
                usage >&2
                die "Unknown option: $1"
                ;;
        esac
    done
}

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        die "Run this installer as root: sudo $0"
    fi
}

validate_service_name() {
    if [[ ! "${SERVICE_NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]*$ ]]; then
        die "Invalid service name: '${SERVICE_NAME}'"
    fi

    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
}

# systemd interprets '%' as a specifier and splits unquoted values on
# whitespace, so reject paths that would need escaping in the unit file.
validate_unit_path() {
    local name="$1"
    local value="$2"

    if [[ "${value}" =~ [[:space:]%\"\\] ]]; then
        die "${name} must not contain whitespace, '%', '\"' or '\\': ${value}"
    fi
}

resolve_configuration() {
    [[ -d "${PROJECT_DIR}" ]] \
        || die "Project directory does not exist: ${PROJECT_DIR}"

    PROJECT_DIR="$(cd -- "${PROJECT_DIR}" && pwd -P)"
    PYTHON_BIN="${PYTHON_BIN:-${PROJECT_DIR}/.venv/bin/python}"
    ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"
    APPLICATION="${PROJECT_DIR}/read_sensors.py"
    DISPLAY_MODULE="${PROJECT_DIR}/alpstuga_display.py"

    if [[ -z "${SERVICE_USER}" ]]; then
        if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
            SERVICE_USER="${SUDO_USER}"
        else
            SERVICE_USER="$(stat -c '%U' "${PROJECT_DIR}")"
        fi
    fi

    if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
        die "User '${SERVICE_USER}' does not exist"
    fi

    if [[ "$(id -u "${SERVICE_USER}")" -eq 0 ]]; then
        die "Refusing to run the service as root. Pass an unprivileged user with --user"
    fi

    SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn "${SERVICE_USER}")}"

    if ! getent group "${SERVICE_GROUP}" >/dev/null 2>&1; then
        die "Group '${SERVICE_GROUP}' does not exist"
    fi

    validate_unit_path "Project directory" "${PROJECT_DIR}"
    validate_unit_path "Python interpreter" "${PYTHON_BIN}"
    validate_unit_path "Environment file" "${ENV_FILE}"

    log "Service name: ${SERVICE_NAME}"
    log "Service user: ${SERVICE_USER}:${SERVICE_GROUP}"
    log "Project directory: ${PROJECT_DIR}"
    log "Python interpreter: ${PYTHON_BIN}"
}

validate_files() {
    [[ -f "${APPLICATION}" ]] \
        || die "Application does not exist: ${APPLICATION}"

    [[ -f "${DISPLAY_MODULE}" ]] \
        || die "Required module does not exist: ${DISPLAY_MODULE}"

    [[ -f "${ENV_FILE}" ]] \
        || die "Environment file does not exist: ${ENV_FILE}"

    [[ -x "${PYTHON_BIN}" ]] \
        || die "Python interpreter is missing or not executable: ${PYTHON_BIN}. Create the virtual environment first (see README.md)"

    if ! runuser -u "${SERVICE_USER}" -- test -r "${APPLICATION}"; then
        die "User ${SERVICE_USER} cannot read ${APPLICATION}"
    fi

    if ! runuser -u "${SERVICE_USER}" -- test -r "${ENV_FILE}"; then
        die "User ${SERVICE_USER} cannot read ${ENV_FILE}"
    fi

    if ! runuser -u "${SERVICE_USER}" -- test -x "${PYTHON_BIN}"; then
        die "User ${SERVICE_USER} cannot execute ${PYTHON_BIN}"
    fi
}

validate_env_file() {
    # systemd EnvironmentFile accepts lines in the form:
    #
    #   NAME=value
    #   NAME="value"
    #   # comment
    #
    # Unlike shell scripts, do not use:
    #
    #   export NAME=value
    #   source other-file
    #
    if grep -Eq '^[[:space:]]*export[[:space:]]+' "${ENV_FILE}"; then
        die "${ENV_FILE} contains shell-style 'export' statements. Remove 'export' and use NAME=value"
    fi

    if grep -Eq '^[[:space:]]*(source|\.)[[:space:]]+' "${ENV_FILE}"; then
        die "${ENV_FILE} contains a shell source command, which systemd EnvironmentFile does not support"
    fi

    local invalid_lines

    invalid_lines="$(
        awk '
            /^[[:space:]]*$/ { next }
            /^[[:space:]]*#/ { next }
            /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/ { next }
            { print NR }
        ' "${ENV_FILE}" | paste -sd ',' -
    )"

    if [[ -n "${invalid_lines}" ]]; then
        die "${ENV_FILE} contains invalid entries on line(s): ${invalid_lines}"
    fi
}

protect_env_file() {
    local owner
    local mode

    owner="$(stat -c '%U:%G' "${ENV_FILE}")"
    mode="$(stat -c '%a' "${ENV_FILE}")"

    log "Environment file: ${ENV_FILE}"
    log "Environment file owner: ${owner}"
    log "Environment file permissions: ${mode}"

    # Do not modify the contents or ownership. Only warn if the file is
    # readable by users outside its owner/group.
    if (( (8#${mode} & 8#004) != 0 )); then
        warn "${ENV_FILE} is readable by other users"
        warn "Recommended command: chmod 640 ${ENV_FILE}"
    fi
}

validate_python_application() {
    log "Checking Python syntax"

    runuser -u "${SERVICE_USER}" -- \
        "${PYTHON_BIN}" -m py_compile \
        "${APPLICATION}" \
        "${DISPLAY_MODULE}" \
        || die "Python syntax validation failed"

    log "Checking Python dependencies"

    runuser -u "${SERVICE_USER}" -- \
        "${PYTHON_BIN}" -c 'import dirigera' \
        || die "The dirigera package is not installed. Run: ${PYTHON_BIN} -m pip install -r ${PROJECT_DIR}/requirements.txt"

    if ! runuser -u "${SERVICE_USER}" -- \
        "${PYTHON_BIN}" -c 'import influxdb_client' 2>/dev/null; then
        warn "influxdb-client is not installed; InfluxDB output and dry-run will not work"
    fi
}

create_service_file() {
    local temporary_file
    temporary_file="$(mktemp)"

    trap 'rm -f "${temporary_file:-}"' EXIT

    cat >"${temporary_file}" <<EOF
[Unit]
Description=IKEA DIRIGERA environment sensor reader
Documentation=https://github.com/Leggin/dirigera
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple

User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${PROJECT_DIR}

# Load the project environment directly, without copying or transforming it.
EnvironmentFile=${ENV_FILE}

# Use the virtual environment and write Python output immediately to journald.
Environment="PATH=$(dirname -- "${PYTHON_BIN}"):/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="PYTHONUNBUFFERED=1"
Environment="PYTHONDONTWRITEBYTECODE=1"

ExecStart=${PYTHON_BIN} ${APPLICATION}

Restart=on-failure
RestartSec=10s
TimeoutStartSec=30s
TimeoutStopSec=30s
KillSignal=SIGTERM

StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

# Security hardening.
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
EOF

    chmod 0644 "${temporary_file}"

    log "Installing systemd unit: ${SERVICE_FILE}"
    install \
        --owner=root \
        --group=root \
        --mode=0644 \
        "${temporary_file}" \
        "${SERVICE_FILE}"

    rm -f "${temporary_file}"
    trap - EXIT
}

verify_service_file() {
    log "Verifying systemd unit"

    if command -v systemd-analyze >/dev/null 2>&1; then
        systemd-analyze verify "${SERVICE_FILE}" \
            || die "The generated systemd unit is invalid"
    else
        warn "systemd-analyze is unavailable; skipping unit validation"
    fi
}

enable_service() {
    log "Reloading systemd configuration"
    systemctl daemon-reload

    log "Enabling ${SERVICE_NAME}.service at system startup"
    systemctl enable "${SERVICE_NAME}.service"

    log "Restarting ${SERVICE_NAME}.service"
    systemctl restart "${SERVICE_NAME}.service"
}

uninstall_service() {
    if [[ ! -f "${SERVICE_FILE}" ]]; then
        log "${SERVICE_FILE} does not exist; nothing to uninstall"
        return
    fi

    log "Stopping and disabling ${SERVICE_NAME}.service"
    systemctl disable --now "${SERVICE_NAME}.service" || true

    log "Removing ${SERVICE_FILE}"
    rm -f "${SERVICE_FILE}"

    systemctl daemon-reload
    systemctl reset-failed "${SERVICE_NAME}.service" 2>/dev/null || true

    log "Uninstallation completed; project files were left untouched"
}

show_result() {
    printf '\n'
    log "Installation completed"
    printf '\n'

    systemctl status \
        "${SERVICE_NAME}.service" \
        --no-pager \
        --full \
        || true

    printf '\n'
    cat <<EOF
Useful commands:

  Service status:
    systemctl status ${SERVICE_NAME}.service

  Follow logs:
    journalctl -u ${SERVICE_NAME}.service -f

  Logs from the current boot:
    journalctl -u ${SERVICE_NAME}.service -b

  Last 100 log lines:
    journalctl -u ${SERVICE_NAME}.service -n 100 --no-pager

  Restart after changing the code or ${ENV_FILE}:
    sudo systemctl restart ${SERVICE_NAME}.service

  Stop:
    sudo systemctl stop ${SERVICE_NAME}.service

  Uninstall:
    sudo $0 --uninstall --service-name ${SERVICE_NAME}
EOF
}

main() {
    parse_args "$@"
    require_root
    validate_service_name

    if [[ "${UNINSTALL}" == true ]]; then
        uninstall_service
        return
    fi

    resolve_configuration
    validate_files
    validate_env_file
    protect_env_file
    validate_python_application
    create_service_file
    verify_service_file
    enable_service
    show_result
}

main "$@"
