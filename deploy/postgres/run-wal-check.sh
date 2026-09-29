#!/usr/bin/env bash
# Verify the continuous archive path and publish only a content-free freshness marker.
set -Eeuo pipefail
set +x

fail() {
  printf '%s\n' "PGBACKREST_WAL_CHECK_FAILED:$1" >&2
  exit 2
}

readonly config=/run/pgbackrest/pgbackrest.conf
readonly ops_state=/ops-state
[[ -f "$config" && ! -L "$config" && -r "$config" ]] || fail "CONFIG_INVALID"
[[ -d "$ops_state" && ! -L "$ops_state" && "$(stat -c '%u:%g:%a' "$ops_state")" == "0:21016:2770" ]] || \
  fail "OPS_STATE_INVALID"

pgbackrest --config="$config" --stanza=telegram-userbot check >/dev/null
archive_state="$(psql -X -Atq -U postgres -d telegram_userbot -v ON_ERROR_STOP=1 <<'SQL'
SELECT CASE
  WHEN last_archived_time IS NULL THEN 'ARCHIVE_MISSING'
  WHEN last_failed_time IS NOT NULL AND last_failed_time > last_archived_time THEN 'ARCHIVE_FAILED'
  WHEN now() - last_archived_time > interval '15 minutes' THEN 'ARCHIVE_STALE'
  ELSE 'ARCHIVE_OK'
END
FROM pg_stat_archiver;
SQL
)" || fail "ARCHIVE_QUERY_FAILED"
[[ "$archive_state" == "ARCHIVE_OK" ]] || fail "$archive_state"

umask 027
marker_tmp="$(mktemp "$ops_state/.wal-archive.XXXXXX")"
trap 'rm -f -- "$marker_tmp"' EXIT
printf '{"schema_version":1,"kind":"wal-archive","completed_at":"%s","result":"PASS"}\n' \
  "$(date --utc +'%Y-%m-%dT%H:%M:%SZ')" > "$marker_tmp"
chmod 0640 "$marker_tmp"
mv -fT "$marker_tmp" "$ops_state/wal-archive.json"
trap - EXIT
printf '%s\n' 'PGBACKREST_WAL_CHECK_COMPLETE'
