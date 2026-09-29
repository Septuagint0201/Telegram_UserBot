#!/usr/bin/env bash
set -Eeuo pipefail
set +x

fail() {
  printf '%s\n' "SYSTEMD_INSTALL_FAILED:$1" >&2
  exit 2
}

[[ "${EUID:-$(id -u)}" == 0 ]] || fail "ROOT_REQUIRED"
[[ -r /etc/os-release && ! -L /etc/os-release ]] || fail "OS_RELEASE_INVALID"
# shellcheck disable=SC1091 -- fixed operating-system identity file
source /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 26.04 ]] || fail "OS_UNSUPPORTED"
[[ "$(dpkg --print-architecture)" == amd64 ]] || fail "ARCHITECTURE_UNSUPPORTED"
command -v docker >/dev/null || fail "DOCKER_UNAVAILABLE"
docker compose version >/dev/null || fail "COMPOSE_UNAVAILABLE"
command -v systemctl >/dev/null || fail "SYSTEMD_UNAVAILABLE"
[[ -x /opt/telegram-userbot/venv/bin/python ]] || fail "PREFLIGHT_VENV_UNAVAILABLE"
/opt/telegram-userbot/venv/bin/python -c \
  'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 14) else 1)' >/dev/null || \
  fail "PREFLIGHT_PYTHON_UNSUPPORTED"

readonly source_root=/opt/telegram-userbot/current/deploy/systemd
[[ -d "$source_root" && ! -L "$source_root" ]] || fail "SOURCE_ROOT_INVALID"
[[ -f /etc/telegram-userbot/ops.env && ! -L /etc/telegram-userbot/ops.env ]] || \
  fail "OPS_ENV_REQUIRED"
[[ "$(stat -c '%u:%g:%a' /etc/telegram-userbot/ops.env)" == "0:0:600" ]] || \
  fail "OPS_ENV_PERMISSION_INVALID"
[[ -S /run/docker.sock && ! -L /run/docker.sock ]] || fail "DOCKER_SOCKET_INVALID"
[[ "$(stat -c '%u:%G' /run/docker.sock)" == "0:docker" ]] || fail "DOCKER_SOCKET_OWNER_INVALID"
[[ -f /etc/telegram-userbot/compose.env && ! -L /etc/telegram-userbot/compose.env ]] || \
  fail "COMPOSE_ENV_REQUIRED"
[[ "$(stat -c '%u:%g:%a' /etc/telegram-userbot/compose.env)" == "0:0:600" ]] || \
  fail "COMPOSE_ENV_PERMISSION_INVALID"
[[ -d /etc/telegram-userbot/config && ! -L /etc/telegram-userbot/config ]] || \
  fail "DEPLOYMENT_CONFIG_DIRECTORY_REQUIRED"
[[ "$(stat -c '%u:%g:%a' /etc/telegram-userbot/config)" == "0:10001:750" ]] || \
  fail "DEPLOYMENT_CONFIG_DIRECTORY_PERMISSION_INVALID"
[[ -f /etc/telegram-userbot/config/deployment.json && \
   ! -L /etc/telegram-userbot/config/deployment.json ]] || fail "DEPLOYMENT_CONFIG_REQUIRED"
deployment_mode="$(stat -c '%u:%g:%a' /etc/telegram-userbot/config/deployment.json)"
[[ "$deployment_mode" == "0:10001:640" ]] || \
  fail "DEPLOYMENT_CONFIG_PERMISSION_INVALID"
[[ -f /etc/telegram-userbot/config/secret-files.json && \
   ! -L /etc/telegram-userbot/config/secret-files.json ]] || fail "SECRET_MANIFEST_REQUIRED"
[[ "$(stat -c '%u:%g:%a' /etc/telegram-userbot/config/secret-files.json)" == "0:10001:640" ]] || \
  fail "SECRET_MANIFEST_PERMISSION_INVALID"

for unit in \
  tudt-ops@.service \
  tudt-ops-alert@.service \
  tudt-postgres-full.timer \
  tudt-postgres-diff.timer \
  tudt-wal-check.timer \
  tudt-session-maintenance.timer \
  tudt-data-export.timer \
  tudt-erasure-ledger.timer; do
  install --owner=root --group=root --mode=0644 "$source_root/$unit" "/etc/systemd/system/$unit"
done
install --owner=root --group=root --mode=0644 \
  "$source_root/telegram-userbot.conf" /etc/tmpfiles.d/telegram-userbot.conf
systemd-tmpfiles --create /etc/tmpfiles.d/telegram-userbot.conf
systemctl daemon-reload
systemd-analyze verify \
  /etc/systemd/system/tudt-ops@.service \
  /etc/systemd/system/tudt-ops-alert@.service \
  /etc/systemd/system/tudt-*.timer >/dev/null
systemd-analyze calendar 'Sun *-*-* 02:00:00' >/dev/null
systemd-analyze calendar 'Mon..Sat *-*-* 02:00:00' >/dev/null
systemd-analyze calendar '*-*-* 03:30:00' >/dev/null
systemd-analyze calendar '*-*-* 04:30:00' >/dev/null
for instance in postgres-full postgres-diff wal-check session data-export erasure-ledger; do
  systemd-analyze verify "tudt-ops@${instance}.service" >/dev/null
  [[ "$(systemctl show --property=OnFailure --value "tudt-ops@${instance}.service")" == \
     "tudt-ops-alert@${instance}.service" ]] || fail "ON_FAILURE_EXPANSION_INVALID"
done

if [[ "${1:-}" == --enable ]]; then
  [[ "${2:-}" == I_ACCEPT_SCHEDULED_OPERATIONS ]] || fail "ENABLE_CONFIRMATION_REQUIRED"
  systemctl enable --now \
    tudt-postgres-full.timer \
    tudt-postgres-diff.timer \
    tudt-wal-check.timer \
    tudt-session-maintenance.timer \
    tudt-data-export.timer \
    tudt-erasure-ledger.timer
elif [[ $# -ne 0 ]]; then
  fail "ARGUMENT_INVALID"
fi
printf '%s\n' 'SYSTEMD_INSTALL_COMPLETE'
