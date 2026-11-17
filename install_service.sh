#!/usr/bin/env bash
#
# Install or update the systemd service for the DIRIGERA sensor reader.
#
# Usage:
#   chmod +x install_service.sh
#   sudo ./install_service.sh
#
# The script is idempotent: it can be run repeatedly to update the unit.

set -Eeuo pipefail

readonly SERVICE_NAME="dirigera-sensors"
readonly SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

readonly SERVICE_USER="cecilek"
readonly SERVICE_GROUP="cecilek"

readonly PROJECT_DIR="/home/cecilek/dirigera"
readonly PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
readonly APPLICATION="${PROJECT_DIR}/read_sensors.py"
readonly ENV_FILE="${PROJECT_DIR}/.env"

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

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        die "Run this installer as root: sudo $0"
    fi
}

validate_user() {
    if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
        die "User '${SERVICE_USER}' does not exist"
    fi

    if ! getent group "${SERVICE_GROUP}" >/dev/null 2>&1; then
        die "Group '${SERVICE_GROUP}' does not exist"
    fi
}

validate_files() {
    [[ -d "${PROJECT_DIR}" ]] \
        || die "Project directory does not exist: ${PROJECT_DIR}"

    [[ -f "${APPLICATION}" ]] \
        || die "Application does not exist: ${APPLICATION}"

    [[ -f "${ENV_FILE}" ]] \
        || die "Environment file does not exist: ${ENV_FILE}"

    [[ -x "${PYTHON_BIN}" ]] \
        || die "Virtual-environment Python is missing or not executable: ${PYTHON_BIN}"

    [[ -f "${PROJECT_DIR}/alpstuga_display.py" ]] \
        || die "Required module does not exist: ${PROJECT_DIR}/alpstuga_display.py"

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
        "${PROJECT_DIR}/alpstuga_display.py" \
        || die "Python syntax validation failed"
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
Environment="PATH=${PROJECT_DIR}/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
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

  Restart after changing read_sensors.py or .env:
    sudo systemctl restart ${SERVICE_NAME}.service

  Stop:
    sudo systemctl stop ${SERVICE_NAME}.service

  Disable:
    sudo systemctl disable --now ${SERVICE_NAME}.service
EOF
}

main() {
    require_root
    validate_user
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

