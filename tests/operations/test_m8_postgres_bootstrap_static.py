import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = json.loads((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))
BOOTSTRAP_COMPOSE = json.loads((ROOT / "deploy/compose.bootstrap.yaml").read_text(encoding="utf-8"))
BOOTSTRAP_SCRIPT_PATH = ROOT / "deploy/postgres/bootstrap/10-bootstrap-roles.sh"
BOOTSTRAP_SCRIPT = BOOTSTRAP_SCRIPT_PATH.read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
GIT_ATTRIBUTES = (ROOT / ".gitattributes").read_text(encoding="utf-8")
MIGRATE_PROCESS = (ROOT / "src/telegram_userbot/processes/migrate.py").read_text(encoding="utf-8")


@pytest.mark.unit
def test_steady_postgres_has_no_database_password_mount() -> None:
    postgres = COMPOSE["services"]["postgres"]

    assert postgres["secrets"] == [
        "pgbackrest_s3_access_key",
        "pgbackrest_s3_secret_key",
        "pgbackrest_repo_cipher_pass",
    ]
    assert postgres["group_add"] == ["21013", "21016"]
    assert postgres["environment"]["POSTGRES_DB"] == "telegram_userbot"
    assert postgres["environment"]["POSTGRES_USER"] == "postgres"
    assert postgres["environment"]["PGDATA"] == "/var/lib/postgresql/data/pgdata"
    assert postgres["environment"]["PGBACKREST_REPO_TYPE"] == "s3"
    assert postgres["healthcheck"]["test"] == [
        "CMD",
        "pg_isready",
        "-h",
        "127.0.0.1",
        "-U",
        "postgres",
        "-d",
        "telegram_userbot",
    ]
    assert "/docker-entrypoint-initdb.d" not in json.dumps(postgres)


@pytest.mark.unit
def test_bootstrap_override_is_explicit_and_has_exact_secret_scope() -> None:
    assert set(BOOTSTRAP_COMPOSE) == {"services"}
    assert set(BOOTSTRAP_COMPOSE["services"]) == {"postgres"}
    postgres = BOOTSTRAP_COMPOSE["services"]["postgres"]

    assert postgres["secrets"] == [
        "postgres_database_password",
        "app_database_password",
        "control_database_password",
        "worker_database_password",
        "migrator_database_password",
        "export_database_password",
        "monitor_database_password",
    ]
    assert postgres["group_add"] == [
        "21008",
        "21004",
        "21005",
        "21006",
        "21007",
        "21011",
        "21017",
    ]
    assert postgres["environment"] == {
        "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_database_password",
        "POSTGRES_INITDB_ARGS": "--auth-local=peer --auth-host=scram-sha-256",
        "POSTGRES_HOST_AUTH_METHOD": "scram-sha-256",
    }
    assert postgres["volumes"] == [
        {
            "type": "bind",
            "source": "./postgres/bootstrap",
            "target": "/docker-entrypoint-initdb.d",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
    rendered = json.dumps(BOOTSTRAP_COMPOSE, sort_keys=True)
    assert '"POSTGRES_PASSWORD"' not in rendered
    assert "postgresql://" not in rendered


@pytest.mark.unit
def test_bootstrap_script_has_fixed_roles_scram_and_no_password_arguments() -> None:
    assert BOOTSTRAP_SCRIPT.startswith("#!/usr/bin/env bash\n# Fresh-cluster bootstrap only. ")
    assert "entrypoint shell. A failure in this subshell" in BOOTSTRAP_SCRIPT
    assert BOOTSTRAP_SCRIPT.rstrip().endswith(")")
    assert "set -Eeuo pipefail" in BOOTSTRAP_SCRIPT
    assert "set +x" in BOOTSTRAP_SCRIPT
    assert "^[A-Za-z0-9._~-]{32,128}$" in BOOTSTRAP_SCRIPT
    assert "SECRET_REUSE_FORBIDDEN" in BOOTSTRAP_SCRIPT
    for secret_name in (
        "postgres_database_password",
        "app_database_password",
        "control_database_password",
        "worker_database_password",
        "migrator_database_password",
        "export_database_password",
        "monitor_database_password",
    ):
        assert f'"/run/secrets/{secret_name}"' in BOOTSTRAP_SCRIPT

    for role in (
        "telegram_userbot_migrator",
        "telegram_userbot_app_runtime",
        "telegram_userbot_control_runtime",
        "telegram_userbot_worker_runtime",
        "telegram_userbot_backup",
        "telegram_userbot_maintenance",
        "telegram_userbot_export_runtime",
        "telegram_userbot_monitor_runtime",
    ):
        assert f"CREATE ROLE {role}\n  NOLOGIN NOSUPERUSER" in BOOTSTRAP_SCRIPT
    for role in (
        "telegram_userbot_migrator_login",
        "telegram_userbot_app_login",
        "telegram_userbot_control_login",
        "telegram_userbot_worker_login",
        "telegram_userbot_exporter_login",
        "telegram_userbot_monitor_login",
    ):
        assert f"CREATE ROLE {role}\n  LOGIN NOSUPERUSER" in BOOTSTRAP_SCRIPT

    assert BOOTSTRAP_SCRIPT.count("WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;") == 6
    assert "CREATE EXTENSION vector WITH SCHEMA public VERSION :'vector_version';" in (
        BOOTSTRAP_SCRIPT
    )
    assert 'ALTER DATABASE :"database_name" OWNER TO telegram_userbot_migrator;' in (
        BOOTSTRAP_SCRIPT
    )
    assert "ALTER SCHEMA public OWNER TO telegram_userbot_migrator;" in BOOTSTRAP_SCRIPT
    assert "REVOKE ALL ON DATABASE" in BOOTSTRAP_SCRIPT
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC;" in BOOTSTRAP_SCRIPT
    assert "\\password ${role_name}" in BOOTSTRAP_SCRIPT
    assert "PASSWORD_APPLY_FAILED" in BOOTSTRAP_SCRIPT
    assert "PASSWORD :'" not in BOOTSTRAP_SCRIPT
    assert "--set=password" not in BOOTSTRAP_SCRIPT.lower()
    assert "CREATE ROLE telegram_userbot_bootstrap" not in BOOTSTRAP_SCRIPT
    assert "LOGIN NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD NULL;" in (
        BOOTSTRAP_SCRIPT
    )
    assert "rolpassword NOT LIKE 'SCRAM-SHA-256$%'" in BOOTSTRAP_SCRIPT
    assert "LOGIN_PASSWORD_INVALID" in BOOTSTRAP_SCRIPT
    assert "GROUP_PASSWORD_RETAINED" in BOOTSTRAP_SCRIPT
    assert "UNEXPECTED_ROLE_RETAINED" in BOOTSTRAP_SCRIPT


@pytest.mark.unit
def test_role_closure_starts_with_privilege_reset_and_restores_readiness_grant() -> None:
    m1_roles = (ROOT / "deploy/postgres/m1_roles.sql").read_text(encoding="utf-8")

    reset = "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM"
    first_business_grant = "GRANT SELECT, INSERT, UPDATE ON"
    assert reset in m1_roles
    assert m1_roles.index(reset) < m1_roles.index(first_business_grant)
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM" in m1_roles
    assert "REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA public FROM" in m1_roles
    assert "GRANT SELECT ON alembic_version TO" in m1_roles


@pytest.mark.unit
def test_bootstrap_script_has_valid_bash_syntax_on_posix() -> None:
    if os.name != "posix":
        pytest.skip("bash syntax execution is a Linux/Ubuntu gate")
    bash = Path("/usr/bin/bash")
    assert bash.is_file()
    subprocess.run(  # noqa: S603 - fixed Ubuntu shell and reviewed repository script
        [str(bash), "-n", str(BOOTSTRAP_SCRIPT_PATH)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
def test_role_closure_assets_are_fixed_in_runtime_image_and_migrate_entrypoint() -> None:
    for stage in range(1, 9):
        assert f"deploy/postgres/m{stage}_roles.sql" in DOCKERFILE
    assert "deploy/postgres/bootstrap" not in DOCKERFILE
    assert "*.sh text eol=lf" in GIT_ATTRIBUTES

    assert "ProductionSettings.load(ProductionProcess.MIGRATE" in MIGRATE_PROCESS
    assert "PostgresConnectionSettings.from_test_dsn" not in MIGRATE_PROCESS
    assert 'ROLE_CLOSURE_DIRECTORY = Path("/opt/app/deploy/postgres")' in MIGRATE_PROCESS
    assert 'ALEMBIC_CONFIG_PATH = Path("/opt/app/alembic.ini")' in MIGRATE_PROCESS
    assert 'tuple(argv) != ("upgrade", "head")' in MIGRATE_PROCESS
    assert 'stdout.write("MIGRATION_BUSY\\n")' in MIGRATE_PROCESS


@pytest.mark.unit
def test_m8_service_status_roles_are_row_scoped_and_sequence_specific() -> None:
    m8_roles = (ROOT / "deploy/postgres/m8_roles.sql").read_text(encoding="utf-8")

    for role_script in sorted((ROOT / "deploy/postgres").glob("m*_roles.sql")):
        role_sql = role_script.read_text(encoding="utf-8")
        assert "GRANT USAGE, SELECT ON ALL SEQUENCES" not in role_sql
        assert "GRANT USAGE ON ALL SEQUENCES" not in role_sql
        assert "GRANT USAGE, SELECT ON SEQUENCE" not in role_sql
    assert "GRANT USAGE ON SEQUENCE service_status_events_id_seq" in m8_roles
    assert "service_instances_control_read" in m8_roles
    assert "service_instances_control_write" in m8_roles
    assert "WITH CHECK (service_name = 'control')" in m8_roles
    assert "WITH CHECK (service_name = 'app')" in m8_roles
    assert "WITH CHECK (service_name = 'worker')" in m8_roles
    assert "REVOKE UPDATE, DELETE ON service_status_events" in m8_roles
    assert "control_bot_cursors_control" in m8_roles
    assert "control_bot_receipts_control" in m8_roles
    assert "telegram_watermarks_app" in m8_roles


@pytest.mark.unit
def test_exporter_and_monitor_roles_are_fail_closed_allowlists() -> None:
    m8_roles = (ROOT / "deploy/postgres/m8_roles.sql").read_text(encoding="utf-8")
    migration = (ROOT / "alembic/versions/0026_m8_data_export.py").read_text(encoding="utf-8")

    protected_views = (
        "export_account_peers_v1",
        "export_message_revisions_v1",
        "export_message_media_v1",
        "export_memories_v1",
        "export_memory_versions_v1",
        "export_summary_versions_v1",
    )
    for view in protected_views:
        assert f'"{view}"' in migration
        assert f"ALTER VIEW public.{view} OWNER TO telegram_userbot_migrator" in m8_roles
    assert migration.count("WITH (security_barrier=true)") == 1
    assert "access_hash" not in m8_roles
    assert "telegram_file_ref" not in m8_roles
    assert "GRANT SELECT ON\n  export_account_peers_v1," in m8_roles
    assert "ALTER FUNCTION get_model_credential_version_by_id(uuid, uuid, uuid)" in m8_roles
    assert (
        "REVOKE ALL ON FUNCTION get_model_credential_version_by_id(uuid, uuid, uuid) FROM PUBLIC"
        in m8_roles
    )
    assert "GRANT EXECUTE ON FUNCTION get_model_credential_version_by_id(uuid, uuid, uuid) TO" in (
        m8_roles
    )
    assert "telegram_userbot_app_runtime,\n  telegram_userbot_worker_runtime;" in m8_roles
    assert "SECURITY DEFINER" in (
        ROOT / "alembic/versions/0028_m8_background_model_runtime.py"
    ).read_text(encoding="utf-8")
    assert "SET search_path = pg_catalog, public" in (
        ROOT / "alembic/versions/0028_m8_background_model_runtime.py"
    ).read_text(encoding="utf-8")
    assert "requested_run_id uuid" in (
        ROOT / "alembic/versions/0028_m8_background_model_runtime.py"
    ).read_text(encoding="utf-8")
    assert "run.state = 'running'" in (
        ROOT / "alembic/versions/0028_m8_background_model_runtime.py"
    ).read_text(encoding="utf-8")
    assert "GRANT SELECT ON model_credential_versions" not in m8_roles
    assert "GRANT SELECT ON\n  model_credential_versions" not in m8_roles
    assert "FROM telegram_userbot_monitor_runtime" in m8_roles
