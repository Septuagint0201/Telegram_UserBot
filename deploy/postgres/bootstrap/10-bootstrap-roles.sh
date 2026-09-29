#!/usr/bin/env bash
# Fresh-cluster bootstrap only. The Docker Official Image sources this file after initdb.

# Keep shell options, readonly names, functions, and secret-bearing variables out of the
# Docker Official Image entrypoint shell. A failure in this subshell is still returned to
# the sourcing entrypoint, whose fail-fast contract aborts fresh-cluster initialization.
(
set -Eeuo pipefail
set +x

readonly EXPECTED_DATABASE="telegram_userbot"
readonly EXPECTED_VECTOR_VERSION="0.8.6"
readonly POSTGRES_BOOTSTRAP_PASSWORD_FILE="/run/secrets/postgres_database_password"
readonly APP_PASSWORD_FILE="/run/secrets/app_database_password"
readonly CONTROL_PASSWORD_FILE="/run/secrets/control_database_password"
readonly WORKER_PASSWORD_FILE="/run/secrets/worker_database_password"
readonly MIGRATOR_PASSWORD_FILE="/run/secrets/migrator_database_password"
readonly EXPORT_PASSWORD_FILE="/run/secrets/export_database_password"
readonly MONITOR_PASSWORD_FILE="/run/secrets/monitor_database_password"

bootstrap_fail() {
  printf '%s\n' "POSTGRES_BOOTSTRAP_FAILED:$1" >&2
  exit 1
}

read_bootstrap_password() {
  local secret_file="$1"
  local output_name="$2"
  local byte_count
  local secret_value

  [[ -f "$secret_file" && ! -L "$secret_file" && -r "$secret_file" ]] || \
    bootstrap_fail "SECRET_FILE_INVALID"
  byte_count="$(LC_ALL=C wc -c < "$secret_file")" || bootstrap_fail "SECRET_FILE_INVALID"
  [[ "$byte_count" =~ ^[0-9]+$ ]] || bootstrap_fail "SECRET_FILE_INVALID"
  ((byte_count >= 32 && byte_count <= 128)) || bootstrap_fail "SECRET_FORMAT_INVALID"

  secret_value="$(<"$secret_file")" || bootstrap_fail "SECRET_FILE_INVALID"
  ((${#secret_value} == byte_count)) || bootstrap_fail "SECRET_FORMAT_INVALID"
  [[ "$secret_value" =~ ^[A-Za-z0-9._~-]{32,128}$ ]] || \
    bootstrap_fail "SECRET_FORMAT_INVALID"
  printf -v "$output_name" '%s' "$secret_value"
}

apply_role_password() {
  local role_name="$1"
  local role_password="$2"

  # psql's \password path derives the SCRAM verifier client-side. The cleartext is sent
  # only over stdin, never as an argument, Compose environment value, or SQL literal.
  if ! printf '%s\n%s\n' "$role_password" "$role_password" |
    psql \
      --username postgres \
      --dbname "$EXPECTED_DATABASE" \
      --no-password \
      --no-psqlrc \
      --command "SET password_encryption = 'scram-sha-256'" \
      --command "\\password ${role_name}" \
      >/dev/null 2>&1; then
    bootstrap_fail "PASSWORD_APPLY_FAILED"
  fi
}

[[ "${POSTGRES_USER:-}" == "postgres" ]] || bootstrap_fail "POSTGRES_USER_INVALID"
[[ "${POSTGRES_DB:-}" == "$EXPECTED_DATABASE" ]] || bootstrap_fail "POSTGRES_DB_INVALID"
[[ "${POSTGRES_INITDB_ARGS:-}" == "--auth-local=peer --auth-host=scram-sha-256" ]] || \
  bootstrap_fail "INITDB_AUTH_INVALID"
[[ "${POSTGRES_HOST_AUTH_METHOD:-}" == "scram-sha-256" ]] || \
  bootstrap_fail "HOST_AUTH_INVALID"

read_bootstrap_password "$POSTGRES_BOOTSTRAP_PASSWORD_FILE" postgres_bootstrap_password
read_bootstrap_password "$APP_PASSWORD_FILE" app_password
read_bootstrap_password "$CONTROL_PASSWORD_FILE" control_password
read_bootstrap_password "$WORKER_PASSWORD_FILE" worker_password
read_bootstrap_password "$MIGRATOR_PASSWORD_FILE" migrator_password
read_bootstrap_password "$EXPORT_PASSWORD_FILE" export_password
read_bootstrap_password "$MONITOR_PASSWORD_FILE" monitor_password

[[ "$postgres_bootstrap_password" != "$app_password" && \
  "$postgres_bootstrap_password" != "$control_password" && \
  "$postgres_bootstrap_password" != "$worker_password" && \
  "$postgres_bootstrap_password" != "$migrator_password" && \
  "$app_password" != "$control_password" && \
  "$app_password" != "$worker_password" && \
  "$app_password" != "$migrator_password" && \
  "$control_password" != "$worker_password" && \
  "$control_password" != "$migrator_password" && \
  "$worker_password" != "$migrator_password" && \
  "$export_password" != "$postgres_bootstrap_password" && \
  "$export_password" != "$app_password" && \
  "$export_password" != "$control_password" && \
  "$export_password" != "$worker_password" && \
  "$export_password" != "$migrator_password" && \
  "$monitor_password" != "$postgres_bootstrap_password" && \
  "$monitor_password" != "$app_password" && \
  "$monitor_password" != "$control_password" && \
  "$monitor_password" != "$worker_password" && \
  "$monitor_password" != "$migrator_password" && \
  "$monitor_password" != "$export_password" ]] || bootstrap_fail "SECRET_REUSE_FORBIDDEN"

psql \
  --username postgres \
  --dbname "$EXPECTED_DATABASE" \
  --no-password \
  --no-psqlrc \
  --set=database_name="$EXPECTED_DATABASE" \
  --set=vector_version="$EXPECTED_VECTOR_VERSION" \
  --set=ON_ERROR_STOP=1 <<'SQL'
BEGIN;

CREATE ROLE telegram_userbot_migrator
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;
CREATE ROLE telegram_userbot_app_runtime
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;
CREATE ROLE telegram_userbot_control_runtime
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;
CREATE ROLE telegram_userbot_worker_runtime
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;
CREATE ROLE telegram_userbot_backup
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;
CREATE ROLE telegram_userbot_maintenance
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;
CREATE ROLE telegram_userbot_export_runtime
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;
CREATE ROLE telegram_userbot_monitor_runtime
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;

CREATE ROLE telegram_userbot_migrator_login
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;
CREATE ROLE telegram_userbot_app_login
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;
CREATE ROLE telegram_userbot_control_login
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;
CREATE ROLE telegram_userbot_worker_login
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;
CREATE ROLE telegram_userbot_exporter_login
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;
CREATE ROLE telegram_userbot_monitor_login
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS PASSWORD NULL;

GRANT telegram_userbot_migrator TO telegram_userbot_migrator_login
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
GRANT telegram_userbot_app_runtime TO telegram_userbot_app_login
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
GRANT telegram_userbot_control_runtime TO telegram_userbot_control_login
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
GRANT telegram_userbot_worker_runtime TO telegram_userbot_worker_login
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
GRANT telegram_userbot_export_runtime TO telegram_userbot_exporter_login
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
GRANT telegram_userbot_monitor_runtime TO telegram_userbot_monitor_login
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;

CREATE EXTENSION vector WITH SCHEMA public VERSION :'vector_version';
ALTER DATABASE :"database_name" OWNER TO telegram_userbot_migrator;
ALTER SCHEMA public OWNER TO telegram_userbot_migrator;

REVOKE ALL ON DATABASE :"database_name" FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE template1 FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name" TO
  telegram_userbot_migrator_login,
  telegram_userbot_app_login,
  telegram_userbot_control_login,
  telegram_userbot_worker_login,
  telegram_userbot_exporter_login,
  telegram_userbot_monitor_login;
GRANT USAGE, CREATE ON SCHEMA public TO telegram_userbot_migrator;

ALTER DEFAULT PRIVILEGES FOR ROLE telegram_userbot_migrator
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

DO $bootstrap$
DECLARE
  role_count integer;
  invalid_roles integer;
  unexpected_roles integer;
  membership_count integer;
  invalid_memberships integer;
BEGIN
  SELECT count(*) INTO role_count
  FROM pg_roles
  WHERE rolname IN (
    'telegram_userbot_migrator',
    'telegram_userbot_app_runtime',
    'telegram_userbot_control_runtime',
    'telegram_userbot_worker_runtime',
    'telegram_userbot_backup',
    'telegram_userbot_maintenance',
    'telegram_userbot_export_runtime',
    'telegram_userbot_monitor_runtime',
    'telegram_userbot_migrator_login',
    'telegram_userbot_app_login',
    'telegram_userbot_control_login',
    'telegram_userbot_worker_login',
    'telegram_userbot_exporter_login',
    'telegram_userbot_monitor_login'
  );
  IF role_count <> 14 THEN
    RAISE EXCEPTION 'POSTGRES_BOOTSTRAP_FAILED:ROLE_INVENTORY_INVALID';
  END IF;

  SELECT count(*) INTO invalid_roles
  FROM pg_roles
  WHERE rolname IN (
    'telegram_userbot_migrator',
    'telegram_userbot_app_runtime',
    'telegram_userbot_control_runtime',
    'telegram_userbot_worker_runtime',
    'telegram_userbot_backup',
    'telegram_userbot_maintenance',
    'telegram_userbot_export_runtime',
    'telegram_userbot_monitor_runtime',
    'telegram_userbot_migrator_login',
    'telegram_userbot_app_login',
    'telegram_userbot_control_login',
    'telegram_userbot_worker_login',
    'telegram_userbot_exporter_login',
    'telegram_userbot_monitor_login'
  )
  AND (
    rolsuper OR rolcreatedb OR rolcreaterole OR rolinherit OR rolreplication OR rolbypassrls
    OR rolcanlogin <> (right(rolname, 6) = '_login')
  );
  IF invalid_roles <> 0 THEN
    RAISE EXCEPTION 'POSTGRES_BOOTSTRAP_FAILED:ROLE_ATTRIBUTES_INVALID';
  END IF;

  SELECT count(*) INTO unexpected_roles
  FROM pg_roles
  WHERE rolname LIKE 'telegram_userbot_%'
  AND rolname NOT IN (
    'telegram_userbot_migrator',
    'telegram_userbot_app_runtime',
    'telegram_userbot_control_runtime',
    'telegram_userbot_worker_runtime',
    'telegram_userbot_backup',
    'telegram_userbot_maintenance',
    'telegram_userbot_export_runtime',
    'telegram_userbot_monitor_runtime',
    'telegram_userbot_migrator_login',
    'telegram_userbot_app_login',
    'telegram_userbot_control_login',
    'telegram_userbot_worker_login',
    'telegram_userbot_exporter_login',
    'telegram_userbot_monitor_login'
  );
  IF unexpected_roles <> 0 THEN
    RAISE EXCEPTION 'POSTGRES_BOOTSTRAP_FAILED:UNEXPECTED_ROLE_RETAINED';
  END IF;

  SELECT count(*), count(*) FILTER (
    WHERE membership.admin_option OR membership.inherit_option OR NOT membership.set_option
  ) INTO membership_count, invalid_memberships
  FROM pg_auth_members membership
  JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
  JOIN pg_roles login_role ON login_role.oid = membership.member
  WHERE (granted_role.rolname, login_role.rolname) IN (
    ('telegram_userbot_migrator', 'telegram_userbot_migrator_login'),
    ('telegram_userbot_app_runtime', 'telegram_userbot_app_login'),
    ('telegram_userbot_control_runtime', 'telegram_userbot_control_login'),
    ('telegram_userbot_worker_runtime', 'telegram_userbot_worker_login'),
    ('telegram_userbot_export_runtime', 'telegram_userbot_exporter_login'),
    ('telegram_userbot_monitor_runtime', 'telegram_userbot_monitor_login')
  );
  IF membership_count <> 6 OR invalid_memberships <> 0 THEN
    RAISE EXCEPTION 'POSTGRES_BOOTSTRAP_FAILED:ROLE_MEMBERSHIP_INVALID';
  END IF;

  IF (SELECT extversion FROM pg_extension WHERE extname = 'vector')
    IS DISTINCT FROM '0.8.6' THEN
    RAISE EXCEPTION 'POSTGRES_BOOTSTRAP_FAILED:VECTOR_VERSION_INVALID';
  END IF;
END;
$bootstrap$;

COMMIT;
SQL

apply_role_password telegram_userbot_app_login "$app_password"
apply_role_password telegram_userbot_control_login "$control_password"
apply_role_password telegram_userbot_worker_login "$worker_password"
apply_role_password telegram_userbot_migrator_login "$migrator_password"
apply_role_password telegram_userbot_exporter_login "$export_password"
apply_role_password telegram_userbot_monitor_login "$monitor_password"

# The immutable initdb superuser cannot lose SUPERUSER, so retain LOGIN only for local peer
# maintenance and remove all password authentication. No application bootstrap role is kept.
psql \
  --username postgres \
  --dbname "$EXPECTED_DATABASE" \
  --no-password \
  --no-psqlrc \
  --set=ON_ERROR_STOP=1 <<'SQL'
ALTER ROLE postgres WITH
  LOGIN NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD NULL;
DO $bootstrap$
DECLARE
  invalid_login_passwords integer;
  invalid_group_passwords integer;
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'telegram_userbot_bootstrap') THEN
    RAISE EXCEPTION 'POSTGRES_BOOTSTRAP_FAILED:APPLICATION_BOOTSTRAP_ROLE_RETAINED';
  END IF;
  IF NOT EXISTS (
    SELECT FROM pg_authid
    WHERE rolname = 'postgres' AND rolcanlogin AND rolpassword IS NULL
  ) THEN
    RAISE EXCEPTION 'POSTGRES_BOOTSTRAP_FAILED:POSTGRES_PEER_ROLE_INVALID';
  END IF;
  SELECT count(*) INTO invalid_login_passwords
  FROM pg_authid
  WHERE rolname IN (
    'telegram_userbot_migrator_login',
    'telegram_userbot_app_login',
    'telegram_userbot_control_login',
    'telegram_userbot_worker_login',
    'telegram_userbot_exporter_login',
    'telegram_userbot_monitor_login'
  )
  AND (rolpassword IS NULL OR rolpassword NOT LIKE 'SCRAM-SHA-256$%');
  IF invalid_login_passwords <> 0 THEN
    RAISE EXCEPTION 'POSTGRES_BOOTSTRAP_FAILED:LOGIN_PASSWORD_INVALID';
  END IF;
  SELECT count(*) INTO invalid_group_passwords
  FROM pg_authid
  WHERE rolname IN (
    'telegram_userbot_migrator',
    'telegram_userbot_app_runtime',
    'telegram_userbot_control_runtime',
    'telegram_userbot_worker_runtime',
    'telegram_userbot_export_runtime',
    'telegram_userbot_monitor_runtime',
    'telegram_userbot_backup',
    'telegram_userbot_maintenance'
  )
  AND rolpassword IS NOT NULL;
  IF invalid_group_passwords <> 0 THEN
    RAISE EXCEPTION 'POSTGRES_BOOTSTRAP_FAILED:GROUP_PASSWORD_RETAINED';
  END IF;
END;
$bootstrap$;
SQL

unset \
  postgres_bootstrap_password \
  app_password \
  control_password \
  worker_password \
  migrator_password \
  export_password \
  monitor_password \
  POSTGRES_PASSWORD \
  PGPASSWORD
printf '%s\n' 'POSTGRES_BOOTSTRAP_COMPLETE'
)
