#!/usr/bin/env bash
set -Eeuo pipefail
set +x

fail() {
  printf '%s\n' "POSTGRES_RUNTIME_FAILED:$1" >&2
  exit 1
}

read_secret() {
  local path="$1"
  local output_name="$2"
  local byte_count
  local value
  [[ -f "$path" && ! -L "$path" && -r "$path" ]] || fail "BACKUP_SECRET_INVALID"
  byte_count="$(LC_ALL=C wc -c < "$path")" || fail "BACKUP_SECRET_INVALID"
  [[ "$byte_count" =~ ^[0-9]+$ ]] || fail "BACKUP_SECRET_INVALID"
  ((byte_count >= 16 && byte_count <= 256)) || fail "BACKUP_SECRET_INVALID"
  value="$(<"$path")" || fail "BACKUP_SECRET_INVALID"
  ((${#value} == byte_count)) || fail "BACKUP_SECRET_INVALID"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || fail "BACKUP_SECRET_INVALID"
  printf -v "$output_name" '%s' "$value"
}

required_token() {
  local value="$1"
  [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,252}$ ]] || fail "BACKUP_CONFIG_INVALID"
}

required_token "${DEPLOYMENT_ID:-}"
read_secret /run/secrets/pgbackrest_repo_cipher_pass repo_cipher_pass

umask 077
mkdir -p /run/pgbackrest
{
  printf '%s\n' '[global]'
  if [[ "${PGBACKREST_REPO_TYPE:-s3}" == "posix" ]]; then
    [[ "${TUDT_LOCAL_BACKUP_VALIDATION:-}" == "explicit-local-synthetic-only" ]] || \
      fail "BACKUP_CONFIG_INVALID"
    [[ -d /backup-repository && ! -L /backup-repository ]] || fail "BACKUP_CONFIG_INVALID"
    printf '%s\n' 'repo1-type=posix'
    printf '%s\n' 'repo1-path=/backup-repository'
  else
    [[ "${PGBACKREST_REPO_TYPE:-s3}" == "s3" ]] || fail "BACKUP_CONFIG_INVALID"
    required_token "${PGBACKREST_S3_ENDPOINT:-}"
    required_token "${PGBACKREST_S3_BUCKET:-}"
    required_token "${PGBACKREST_S3_REGION:-}"
    [[ "${PGBACKREST_S3_URI_STYLE:-host}" =~ ^(host|path)$ ]] || fail "BACKUP_CONFIG_INVALID"
    read_secret /run/secrets/pgbackrest_s3_access_key repo_access_key
    read_secret /run/secrets/pgbackrest_s3_secret_key repo_secret_key
    printf '%s\n' 'repo1-type=s3'
    printf 'repo1-s3-endpoint=%s\n' "$PGBACKREST_S3_ENDPOINT"
    printf 'repo1-s3-bucket=%s\n' "$PGBACKREST_S3_BUCKET"
    printf 'repo1-s3-region=%s\n' "$PGBACKREST_S3_REGION"
    printf 'repo1-s3-uri-style=%s\n' "${PGBACKREST_S3_URI_STYLE:-host}"
    printf 'repo1-s3-key=%s\n' "$repo_access_key"
    printf 'repo1-s3-key-secret=%s\n' "$repo_secret_key"
    printf 'repo1-path=/telegram-userbot/%s\n' "$DEPLOYMENT_ID"
  fi
  printf 'repo1-cipher-type=aes-256-cbc\n'
  printf 'repo1-cipher-pass=%s\n' "$repo_cipher_pass"
  printf '%s\n' 'repo1-retention-full=4'
  printf '%s\n' 'repo1-retention-diff=6'
  printf '%s\n' 'archive-async=y'
  printf '%s\n' 'spool-path=/var/spool/pgbackrest'
  printf '%s\n' 'process-max=1'
  printf '%s\n' 'start-fast=y'
  printf '%s\n' 'log-level-console=info'
  printf '%s\n' '[telegram-userbot]'
  printf '%s\n' 'pg1-path=/var/lib/postgresql/data/pgdata'
  printf '%s\n' 'pg1-port=5432'
} > /run/pgbackrest/pgbackrest.conf
chmod 0600 /run/pgbackrest/pgbackrest.conf

unset repo_access_key repo_secret_key repo_cipher_pass

# A restore target is deliberately a brand-new empty volume.  The official
# PostgreSQL entrypoint would run ``initdb`` before handing control to the
# restore command, destroying the empty-target invariant.  The restore
# overlay opts into this exact, command-scoped path after this wrapper has
# generated the pgBackRest configuration.
if [[ "${TUDT_RESTORE_ONLY:-}" == "explicit-restore-only" ]]; then
  [[ "$#" -eq 1 && "$1" == "/usr/local/bin/tudt-pgbackrest-restore" ]] ||
    fail "RESTORE_COMMAND_INVALID"
  exec "$1"
fi

exec /usr/local/bin/docker-entrypoint.sh "$@"
