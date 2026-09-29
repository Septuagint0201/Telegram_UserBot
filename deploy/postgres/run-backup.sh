#!/usr/bin/env bash
set -Eeuo pipefail
set +x

backup_type="${1:-}"
case "$backup_type" in
  full|diff|incr) ;;
  *) printf '%s\n' 'PGBACKREST_BACKUP_FAILED:TYPE_INVALID' >&2; exit 2 ;;
esac

readonly config=/run/pgbackrest/pgbackrest.conf
readonly ops_state=/ops-state
[[ -f "$config" && ! -L "$config" && -r "$config" ]] || {
  printf '%s\n' 'PGBACKREST_BACKUP_FAILED:CONFIG_INVALID' >&2
  exit 2
}
[[ -d "$ops_state" && ! -L "$ops_state" && "$(stat -c '%u:%g:%a' "$ops_state")" == "0:21016:2770" ]] || {
  printf '%s\n' 'PGBACKREST_BACKUP_FAILED:OPS_STATE_INVALID' >&2
  exit 2
}

pgbackrest --config="$config" --stanza=telegram-userbot check >/dev/null
pgbackrest --config="$config" --stanza=telegram-userbot --type="$backup_type" \
  --no-expire-auto backup >/dev/null
pgbackrest --config="$config" --stanza=telegram-userbot check >/dev/null
pgbackrest --config="$config" --stanza=telegram-userbot expire >/dev/null
umask 027
marker_tmp="$(mktemp "$ops_state/.postgres-backup.XXXXXX")"
trap 'rm -f -- "$marker_tmp"' EXIT
printf '{"schema_version":1,"kind":"postgres-backup","completed_at":"%s","result":"PASS"}\n' \
  "$(date --utc +'%Y-%m-%dT%H:%M:%SZ')" > "$marker_tmp"
chmod 0640 "$marker_tmp"
mv -fT "$marker_tmp" "$ops_state/postgres-backup.json"
trap - EXIT
printf '%s\n' "PGBACKREST_BACKUP_COMPLETE:${backup_type}"
