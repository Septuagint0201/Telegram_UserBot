#!/usr/bin/env bash
# Restore one exact pgBackRest backup into a new, empty PostgreSQL data volume.
set -Eeuo pipefail
set +x

fail() {
  printf '%s\n' "PGBACKREST_RESTORE_FAILED:$1" >&2
  exit 2
}

readonly config=/run/pgbackrest/pgbackrest.conf
readonly pgdata=/var/lib/postgresql/data/pgdata
readonly ops_state=/ops-state
readonly backup_set="${PGBACKREST_RESTORE_SET:-}"

[[ "$backup_set" =~ ^[0-9]{8}-[0-9]{6}F(_[0-9]{8}-[0-9]{6}[DI])?$ ]] || \
  fail "BACKUP_SET_INVALID"
[[ -f "$config" && ! -L "$config" && -r "$config" ]] || fail "CONFIG_INVALID"
[[ -d /var/lib/postgresql/data && ! -L /var/lib/postgresql/data ]] || \
  fail "TARGET_INVALID"
[[ -d "$ops_state" && ! -L "$ops_state" && "$(stat -c '%u:%g:%a' "$ops_state")" == "0:21016:2770" ]] || \
  fail "OPS_STATE_INVALID"

mkdir -p "$pgdata"
[[ -d "$pgdata" && ! -L "$pgdata" ]] || fail "TARGET_INVALID"
if find "$pgdata" -mindepth 1 -print -quit | grep -q .; then
  fail "TARGET_NOT_EMPTY"
fi

# An exact set is mandatory; restore never follows a moving "latest" pointer.  pgBackRest
# verifies the repository metadata and every restored file checksum as part of restore.
# Stop at the first consistent state of that backup. target-action requires an explicit
# recovery target type; default recovery would also consume newer available WAL.
pgbackrest --config="$config" --stanza=telegram-userbot \
  --set="$backup_set" --type=immediate --target-action=promote restore >/dev/null || \
  fail "RESTORE_EXECUTION_FAILED"
[[ "$(<"$pgdata/PG_VERSION")" == "17" ]] || fail "POSTGRES_VERSION_INVALID"
pg_controldata "$pgdata" >/dev/null || fail "CONTROL_DATA_INVALID"

umask 027
marker_tmp="$(mktemp "$ops_state/.postgres-restore.XXXXXX")"
trap 'rm -f -- "$marker_tmp"' EXIT
printf '{"schema_version":1,"kind":"postgres-restore","completed_at":"%s","result":"PASS"}\n' \
  "$(date --utc +'%Y-%m-%dT%H:%M:%SZ')" > "$marker_tmp"
chmod 0640 "$marker_tmp"
mv -fT "$marker_tmp" "$ops_state/postgres-restore.json"
trap - EXIT
printf '%s\n' 'PGBACKREST_RESTORE_COMPLETE'
