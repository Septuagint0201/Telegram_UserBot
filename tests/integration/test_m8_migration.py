import asyncio
import os
import time
from pathlib import Path
from uuid import uuid7

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection
from testcontainers.core.container import DockerContainer, ExecConfig
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from telegram_userbot.adapters.persistence.engine import (
    DatabaseReadinessPolicy,
    PostgresConnectionSettings,
    create_postgres_engine,
    create_sync_postgres_engine,
    schema_is_ready,
)
from telegram_userbot.adapters.persistence.migration import (
    MIGRATION_LOCK_KEY,
    MigrationStatus,
    migrate_to_head,
)
from telegram_userbot.adapters.persistence.role_closure import load_role_closure_plan
from telegram_userbot.domain.shared.redaction import SensitiveValue
from telegram_userbot.platform.compatibility import (
    EXPECTED_PGVECTOR_VERSION,
    EXPECTED_SCHEMA_REVISION,
)

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_REVISION = EXPECTED_SCHEMA_REVISION
EXPECTED_VECTOR_VERSION = EXPECTED_PGVECTOR_VERSION
POSTGRES_IMAGE = (
    "pgvector/pgvector:0.8.6-pg17-bookworm@"
    "sha256:f193b9c848deb27b0e892f85ed07b53fd83ccb1feb634ba415b1cdc44411bedc"
)
pytestmark = pytest.mark.asyncio(loop_scope="session")
ROLE_CLOSURE = load_role_closure_plan(ROOT / "deploy/postgres")
IDENTITY_SEQUENCES = frozenset(
    {
        "message_events_id_seq",
        "conversation_mode_history_id_seq",
        "account_control_history_id_seq",
        "transactional_outbox_id_seq",
        "audit_log_id_seq",
        "erasure_progress_id_seq",
        "erasure_ledger_id_seq",
        "outbound_attempts_id_seq",
        "model_run_attempts_id_seq",
        "context_manifest_items_id_seq",
        "context_manifest_omissions_id_seq",
        "context_preview_deliveries_id_seq",
        "memory_input_manifest_items_id_seq",
        "service_status_events_id_seq",
    }
)
SEQUENCE_USAGE_BY_ROLE = {
    "telegram_userbot_app_runtime": frozenset(
        {
            "message_events_id_seq",
            "conversation_mode_history_id_seq",
            "account_control_history_id_seq",
            "transactional_outbox_id_seq",
            "audit_log_id_seq",
            "outbound_attempts_id_seq",
            "model_run_attempts_id_seq",
            "context_manifest_items_id_seq",
            "context_manifest_omissions_id_seq",
            "service_status_events_id_seq",
        }
    ),
    "telegram_userbot_control_runtime": frozenset(
        {
            "transactional_outbox_id_seq",
            "audit_log_id_seq",
            "context_preview_deliveries_id_seq",
            "service_status_events_id_seq",
        }
    ),
    "telegram_userbot_worker_runtime": frozenset(
        {
            "transactional_outbox_id_seq",
            "audit_log_id_seq",
            "erasure_progress_id_seq",
            "memory_input_manifest_items_id_seq",
            "model_run_attempts_id_seq",
            "service_status_events_id_seq",
        }
    ),
    "telegram_userbot_backup": frozenset(),
    "telegram_userbot_maintenance": IDENTITY_SEQUENCES,
}


def _bootstrap_passwords() -> dict[str, str]:
    return {
        "postgres_database_password": "Bootstrap.Postgres_0123456789abcdef-ABCD",
        "app_database_password": "Bootstrap.App_0123456789abcdef-ABCDEFGH",
        "control_database_password": "Bootstrap.Control_0123456789abcdef-ABCD",
        "worker_database_password": "Bootstrap.Worker_0123456789abcdef-ABCDE",
        "migrator_database_password": "Bootstrap.Migrator_0123456789abcdef-AB",
        "export_database_password": "Bootstrap.Export_0123456789abcdef-ABCDE",
        "monitor_database_password": "Bootstrap.Monitor_0123456789abcdef-ABCD",
    }


def _write_bootstrap_secrets(directory: Path, values: dict[str, str]) -> None:
    # The disposable PostgreSQL container runs init scripts as its non-root postgres
    # user, so the bind-mounted parent must be traversable. These are synthetic test-only
    # values; production host secrets retain the strict root-owned 0700/0440 contract.
    directory.mkdir(mode=0o755)
    for name, value in values.items():
        path = directory / name
        path.write_text(value, encoding="ascii")
        if os.name == "posix":
            path.chmod(0o444)


def _docker_is_unavailable(error: Exception) -> bool:
    error_type = type(error)
    return error_type.__name__ == "DockerException" and error_type.__module__.startswith("docker.")


def _verify_application_bootstrap(host: str, port: int, app_password: str) -> None:
    with psycopg.connect(
        host=host,
        port=port,
        dbname="telegram_userbot",
        user="telegram_userbot_app_login",
        password=app_password,
        connect_timeout=5,
    ) as connection:
        identities = connection.execute("SELECT session_user, current_user").fetchone()
        assert identities == (
            "telegram_userbot_app_login",
            "telegram_userbot_app_login",
        )
        assert connection.execute(
            "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
        ).fetchone() == (False,)
        connection.execute("SET ROLE telegram_userbot_app_runtime")
        assert connection.execute("SELECT current_user").fetchone() == (
            "telegram_userbot_app_runtime",
        )

        roles = connection.execute(
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
            "rolcanlogin, rolreplication, rolbypassrls FROM pg_roles "
            "WHERE rolname LIKE 'telegram_userbot_%' ORDER BY rolname"
        ).fetchall()
        assert len(roles) == 14
        for role in roles:
            is_login = str(role[0]).endswith("_login")
            assert role[1:] == (False, False, False, False, is_login, False, False)

        memberships = connection.execute(
            "SELECT granted.rolname, member.rolname, membership.admin_option, "
            "membership.inherit_option, membership.set_option "
            "FROM pg_auth_members membership "
            "JOIN pg_roles granted ON granted.oid = membership.roleid "
            "JOIN pg_roles member ON member.oid = membership.member "
            "WHERE granted.rolname LIKE 'telegram_userbot_%' "
            "ORDER BY granted.rolname"
        ).fetchall()
        assert len(memberships) == 6
        assert all(row[2:] == (False, False, True) for row in memberships)

        inventory = connection.execute(
            "SELECT (SELECT extversion FROM pg_extension WHERE extname = 'vector'), "
            "(SELECT pg_get_userbyid(datdba) FROM pg_database "
            " WHERE datname = 'telegram_userbot'), "
            "(SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
            " WHERE nspname = 'public')"
        ).fetchone()
        assert inventory == (
            EXPECTED_VECTOR_VERSION,
            "telegram_userbot_migrator",
            "telegram_userbot_migrator",
        )


def _wait_for_final_postgres(host: str, port: int, app_password: str) -> None:
    """Wait past the official image's temporary-init-server shutdown/start race."""

    deadline = time.monotonic() + 30
    while True:
        try:
            with psycopg.connect(
                host=host,
                port=port,
                dbname="telegram_userbot",
                user="telegram_userbot_app_login",
                password=app_password,
                connect_timeout=2,
            ) as connection:
                if connection.execute("SELECT 1").fetchone() == (1,):
                    return
        except psycopg.OperationalError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("final PostgreSQL server did not become ready")
        time.sleep(0.2)


def _verify_postgres_peer_only(
    container: DockerContainer,
    *,
    host: str,
    port: int,
    bootstrap_password: str,
) -> None:
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(
            host=host,
            port=port,
            dbname="telegram_userbot",
            user="postgres",
            password=bootstrap_password,
            connect_timeout=2,
        )

    peer_check = container.exec(
        ExecConfig(
            [
                "psql",
                "--no-password",
                "--no-psqlrc",
                "-U",
                "postgres",
                "-d",
                "telegram_userbot",
                "-Atc",
                "SELECT rolcanlogin::int || '|' || (rolpassword IS NULL)::int "
                "FROM pg_authid WHERE rolname = 'postgres'",
            ],
            user="postgres",
        )
    )
    assert peer_check.exit_code == 0
    assert peer_check.output.strip() == b"1|1"


def _verify_service_status_role_isolation(host: str, port: int, passwords: dict[str, str]) -> None:
    instance_ids = {service: uuid7() for service in ("app", "control", "worker")}

    def connect(service: str) -> psycopg.Connection[tuple[object, ...]]:
        connection = psycopg.connect(
            host=host,
            port=port,
            dbname="telegram_userbot",
            user=f"telegram_userbot_{service}_login",
            password=passwords[f"{service}_database_password"],
            connect_timeout=5,
        )
        connection.execute(f"SET ROLE telegram_userbot_{service}_runtime")
        return connection

    insert_instance = (
        "INSERT INTO service_instances (instance_id, service_name, started_at, "
        "last_heartbeat_at, readiness, status_code, schema_revision, metadata) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'starting', 'STARTING', "
        '%s, \'{"deployment_id":"integration-primary"}\'::jsonb)'
    )
    insert_event = (
        "INSERT INTO service_status_events (instance_id, service_name, event_kind, "
        "readiness, status_code, schema_revision, metadata) VALUES "
        "(%s, %s, 'started', 'starting', 'STARTING', %s, "
        '\'{"deployment_id":"integration-primary"}\'::jsonb)'
    )

    for service in ("app", "worker"):
        with connect(service) as connection:
            connection.execute(insert_instance, (instance_ids[service], service, EXPECTED_REVISION))
            connection.execute(insert_event, (instance_ids[service], service, EXPECTED_REVISION))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    insert_instance,
                    (uuid7(), "worker" if service == "app" else "app", EXPECTED_REVISION),
                )
            connection.rollback()

    # A failed transaction above rolls back its own-service seed as well. Recreate app so
    # control's cross-service SELECT and write isolation are both exercised.
    with connect("app") as connection:
        connection.execute(insert_instance, (instance_ids["app"], "app", EXPECTED_REVISION))
        connection.execute(insert_event, (instance_ids["app"], "app", EXPECTED_REVISION))
        connection.commit()

    with connect("control") as connection:
        connection.execute(insert_instance, (instance_ids["control"], "control", EXPECTED_REVISION))
        connection.execute(insert_event, (instance_ids["control"], "control", EXPECTED_REVISION))
        connection.commit()
        assert connection.execute(
            "SELECT count(*) FROM service_instances WHERE service_name = 'app'"
        ).fetchone() == (1,)
        assert (
            connection.execute(
                "UPDATE service_instances SET status_code = 'PROCESS_LOOP_FAILED', "
                "readiness = 'not_ready' WHERE service_name = 'app'"
            ).rowcount
            == 0
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                insert_instance, (instance_ids["worker"], "worker", EXPECTED_REVISION)
            )
        connection.rollback()

    with connect("app") as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(insert_event, (instance_ids["app"], "worker", EXPECTED_REVISION))
        connection.rollback()

    account_id = uuid7()
    with connect("app") as connection:
        connection.execute(
            "INSERT INTO accounts (id, telegram_user_id, display_label, status) "
            "VALUES (%s, 7000000001, 'M8 integration', 'active')",
            (account_id,),
        )
        connection.execute(
            "INSERT INTO telegram_ingest_watermarks "
            "(account_id, scope, pts, pts_count, update_identity, durable_ingested_at) "
            "VALUES (%s, 'account', 10, 1, 'UpdateNewMessage:10', CURRENT_TIMESTAMP)",
            (account_id,),
        )
        connection.commit()

    with connect("control") as connection:
        connection.execute(
            "INSERT INTO control_bot_cursors (deployment_id, bot_user_id, next_offset) "
            "VALUES ('integration-primary', 7000000002, 0)"
        )
        connection.execute(
            "INSERT INTO control_bot_update_receipts "
            "(deployment_id, bot_user_id, update_id, state, owner_instance_id, "
            "claimed_at, lease_expires_at) VALUES "
            "('integration-primary', 7000000002, 42, 'claimed', %s, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '1 minute')",
            (instance_ids["control"],),
        )
        connection.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT * FROM telegram_ingest_watermarks")
        connection.rollback()

    with connect("app") as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT * FROM control_bot_update_receipts")
        connection.rollback()


def _verify_credential_accessor_role_isolation(
    container: DockerContainer,
    *,
    host: str,
    port: int,
    passwords: dict[str, str],
) -> None:
    """Exercise SECURITY DEFINER identity scope with disposable, inert run records."""

    account_id = uuid7()
    conversation_id = uuid7()
    app_profile_id = uuid7()
    memory_profile_id = uuid7()
    app_credential_id = uuid7()
    memory_credential_id = uuid7()
    app_credential_version_id = uuid7()
    memory_credential_version_id = uuid7()
    app_config_id = uuid7()
    memory_config_id = uuid7()
    app_run_id = uuid7()
    memory_run_id = uuid7()
    proactive_final_run_id = uuid7()
    psql_variables = (
        ("account_id", account_id),
        ("conversation_id", conversation_id),
        ("app_profile_id", app_profile_id),
        ("memory_profile_id", memory_profile_id),
        ("app_credential_id", app_credential_id),
        ("memory_credential_id", memory_credential_id),
        ("app_credential_version_id", app_credential_version_id),
        ("memory_credential_version_id", memory_credential_version_id),
        ("app_config_id", app_config_id),
        ("memory_config_id", memory_config_id),
        ("app_run_id", app_run_id),
        ("memory_run_id", memory_run_id),
        ("proactive_final_run_id", proactive_final_run_id),
        ("app_endpoint_id", uuid7()),
        ("app_capability_id", uuid7()),
        ("memory_endpoint_id", uuid7()),
        ("memory_capability_id", uuid7()),
        ("app_turn_id", uuid7()),
        ("memory_job_id", uuid7()),
        ("memory_manifest_id", uuid7()),
        ("proactive_job_id", uuid7()),
        ("proactive_manifest_id", uuid7()),
    )
    # These rows only provide the exact joins used by the accessor. FK trigger
    # enforcement is disabled in this disposable fixture because creating a full
    # conversation/memory/proactive graph would mask the caller-identity contract.
    seed_sql = """
    SET session_replication_role = replica;
    INSERT INTO model_profiles (id, logical_role, profile_kind, state) VALUES
      (:'app_profile_id', 'main_ai', 'generation', 'disabled'),
      (:'memory_profile_id', 'memory_agent', 'generation', 'disabled');
    INSERT INTO model_credentials (id, profile_id, status, latest_version_no) VALUES
      (:'app_credential_id', :'app_profile_id', 'missing', 0),
      (:'memory_credential_id', :'memory_profile_id', 'missing', 0);
    INSERT INTO model_credential_versions
      (id, credential_id, profile_id, version_no, algorithm, key_version,
       aad_schema_version, nonce, ciphertext, secret_fingerprint) VALUES
      (:'app_credential_version_id', :'app_credential_id', :'app_profile_id',
       1, 'aes_256_gcm', 1, 1, decode(repeat('00', 12), 'hex'), decode('00', 'hex'),
       decode(repeat('00', 32), 'hex')),
      (:'memory_credential_version_id', :'memory_credential_id', :'memory_profile_id',
       1, 'aes_256_gcm', 1, 1, decode(repeat('00', 12), 'hex'), decode('00', 'hex'),
       decode(repeat('00', 32), 'hex'));
    INSERT INTO model_config_versions
      (id, profile_id, profile_kind, version_no, endpoint_id, credential_id,
       capability_snapshot_id, protocol, model_name, max_output_tokens,
       timeout_seconds, enabled, config_sha256, created_by_admin_id, validated_at) VALUES
      (:'app_config_id', :'app_profile_id', 'generation', 1, :'app_endpoint_id',
       :'app_credential_id', :'app_capability_id', 'openai_responses', 'integration-app',
       32, 30, false, decode(repeat('00', 32), 'hex'), 1, CURRENT_TIMESTAMP),
      (:'memory_config_id', :'memory_profile_id', 'generation', 1, :'memory_endpoint_id',
       :'memory_credential_id', :'memory_capability_id', 'openai_responses',
       'integration-memory', 32, 30, false, decode(repeat('00', 32), 'hex'),
       1, CURRENT_TIMESTAMP);
    INSERT INTO model_runs
      (id, account_id, conversation_id, turn_id, memory_job_id, proactive_job_id,
       logical_role, model_profile_id, purpose, generation_no, state,
       config_version_id, credential_version_id, memory_input_manifest_id,
       proactive_input_manifest_id, prompt_version, prompt_bundle_sha256,
       capability_snapshot_sha256, orchestration_claim_fingerprint, input_fingerprint,
       adapter_version, request_schema_version, output_schema_version, normalizer_version)
    VALUES
      (:'app_run_id', :'account_id', :'conversation_id', :'app_turn_id', NULL, NULL,
       'main_ai', :'app_profile_id', 'conversation_reply', 1, 'running',
       :'app_config_id', :'app_credential_version_id', NULL, NULL, 'integration',
       decode(repeat('00', 32), 'hex'), decode(repeat('00', 32), 'hex'),
       decode(repeat('00', 32), 'hex'), decode(repeat('00', 32), 'hex'), 'integration', 1, 1,
       'integration'),
      (:'memory_run_id', :'account_id', :'conversation_id', NULL, :'memory_job_id', NULL,
       'memory_agent', :'memory_profile_id', 'memory_episode', 1, 'running',
       :'memory_config_id', :'memory_credential_version_id', :'memory_manifest_id', NULL,
       'integration', decode(repeat('00', 32), 'hex'), decode(repeat('00', 32), 'hex'),
       decode(repeat('00', 32), 'hex'), decode(repeat('00', 32), 'hex'), 'integration', 1, 1,
       'integration'),
      (:'proactive_final_run_id', :'account_id', :'conversation_id', NULL, NULL,
       :'proactive_job_id', 'main_ai', :'app_profile_id', 'proactive_final', 1, 'running',
       :'app_config_id', :'app_credential_version_id', NULL, :'proactive_manifest_id',
       'integration', decode(repeat('00', 32), 'hex'), decode(repeat('00', 32), 'hex'),
       decode(repeat('00', 32), 'hex'), decode(repeat('00', 32), 'hex'), 'integration', 1, 1,
       'integration');
    SET session_replication_role = origin;
    """
    # psql does not expand colon variables in a command provided through -c.
    # Every value here is a UUID generated by this test, so quoted replacement
    # remains deterministic and does not turn the fixture into an SQL-input path.
    for name, value in psql_variables:
        seed_sql = seed_sql.replace(f":'{name}'", f"'{value}'")
    seeded = container.exec(
        ExecConfig(
            [
                "psql",
                "--no-password",
                "--no-psqlrc",
                "-U",
                "postgres",
                "-d",
                "telegram_userbot",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                seed_sql,
            ],
            user="postgres",
        )
    )
    assert seeded.exit_code == 0

    def can_read(
        service: str,
        *,
        run_id: object,
        profile_id: object,
        credential_version_id: object,
    ) -> bool:
        with psycopg.connect(
            host=host,
            port=port,
            dbname="telegram_userbot",
            user=f"telegram_userbot_{service}_login",
            password=passwords[f"{service}_database_password"],
            connect_timeout=5,
        ) as connection:
            connection.execute(f"SET ROLE telegram_userbot_{service}_runtime")
            count = connection.execute(
                "SELECT count(*) FROM get_model_credential_version_by_id(%s, %s, %s)",
                (run_id, profile_id, credential_version_id),
            ).fetchone()
            return count == (1,)

    assert can_read(
        "app",
        run_id=app_run_id,
        profile_id=app_profile_id,
        credential_version_id=app_credential_version_id,
    )
    assert not can_read(
        "app",
        run_id=memory_run_id,
        profile_id=memory_profile_id,
        credential_version_id=memory_credential_version_id,
    )
    assert not can_read(
        "app",
        run_id=proactive_final_run_id,
        profile_id=app_profile_id,
        credential_version_id=app_credential_version_id,
    )
    assert not can_read(
        "worker",
        run_id=app_run_id,
        profile_id=app_profile_id,
        credential_version_id=app_credential_version_id,
    )
    assert can_read(
        "worker",
        run_id=memory_run_id,
        profile_id=memory_profile_id,
        credential_version_id=memory_credential_version_id,
    )
    assert can_read(
        "worker",
        run_id=proactive_final_run_id,
        profile_id=app_profile_id,
        credential_version_id=app_credential_version_id,
    )


def _verify_sequence_privilege_matrix(connection: Connection) -> None:
    inventory = frozenset(
        connection.execute(
            text(
                "SELECT relname FROM pg_class JOIN pg_namespace ON "
                "pg_namespace.oid = pg_class.relnamespace "
                "WHERE nspname = 'public' AND relkind = 'S'"
            )
        ).scalars()
    )
    assert inventory == IDENTITY_SEQUENCES
    for role, expected_usage in SEQUENCE_USAGE_BY_ROLE.items():
        for sequence in sorted(IDENTITY_SEQUENCES):
            privileges = connection.execute(
                text(
                    "SELECT has_sequence_privilege(:role, :sequence, 'USAGE'), "
                    "has_sequence_privilege(:role, :sequence, 'SELECT')"
                ),
                {"role": role, "sequence": f"public.{sequence}"},
            ).one()
            assert privileges == (sequence in expected_usage, False), (role, sequence)


async def _verify_runtime_readiness(
    host: str,
    port: int,
    passwords: dict[str, str],
) -> None:
    for process in ("app", "control", "worker"):
        settings = PostgresConnectionSettings(
            host=host,
            port=port,
            database="telegram_userbot",
            login_role=f"telegram_userbot_{process}_login",
            password=SensitiveValue(passwords[f"{process}_database_password"]),
            runtime_role=f"telegram_userbot_{process}_runtime",
            sslmode="disable",
            application_name=f"m8_{process}_readiness_integration",
        )
        engine = create_postgres_engine(settings)
        try:
            assert await schema_is_ready(
                engine,
                EXPECTED_REVISION,
                policy=DatabaseReadinessPolicy.for_production_process(process),
            )
        finally:
            await engine.dispose()


def _migrate_fresh_database(host: str, port: int, passwords: dict[str, str]) -> None:
    settings = PostgresConnectionSettings(
        host=host,
        port=port,
        database="telegram_userbot",
        login_role="telegram_userbot_migrator_login",
        password=SensitiveValue(passwords["migrator_database_password"]),
        runtime_role="telegram_userbot_migrator",
        sslmode="disable",
        application_name="m8_bootstrap_integration",
    )
    engine = create_sync_postgres_engine(settings)
    try:
        result = migrate_to_head(
            engine,
            Config(str(ROOT / "alembic.ini")),
            expected_revision=EXPECTED_REVISION,
            expected_vector_version=EXPECTED_VECTOR_VERSION,
            role_closure=ROLE_CLOSURE,
        )
        assert result.status is MigrationStatus.APPLIED
        assert result.from_revision is None
        assert result.to_revision == EXPECTED_REVISION
        with engine.connect() as connection:
            assert connection.execute(text("SELECT session_user, current_user")).one() == (
                "telegram_userbot_migrator_login",
                "telegram_userbot_migrator",
            )
            owners = connection.execute(
                text("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
            assert set(owners) == {"telegram_userbot_migrator"}
            grants = connection.execute(
                text(
                    "SELECT "
                    "has_table_privilege('telegram_userbot_app_runtime', 'messages', 'SELECT'), "
                    "has_table_privilege("
                    "'telegram_userbot_control_runtime', 'messages', 'SELECT'), "
                    "has_table_privilege("
                    "'telegram_userbot_worker_runtime', 'memory_jobs', 'SELECT'), "
                    "has_table_privilege('telegram_userbot_backup', 'audit_log', 'SELECT'), "
                    "has_table_privilege('telegram_userbot_maintenance', 'audit_log', 'DELETE'), "
                    "has_table_privilege('telegram_userbot_app_runtime', 'audit_log', 'DELETE')"
                )
            ).one()
            assert grants == (True, False, True, True, True, False)
            export_and_monitor_grants = connection.execute(
                text(
                    "SELECT "
                    "has_table_privilege('telegram_userbot_export_runtime', "
                    "'account_peers', 'SELECT'), "
                    "has_column_privilege('telegram_userbot_export_runtime', "
                    "'account_peers', 'access_hash', 'SELECT'), "
                    "has_table_privilege('telegram_userbot_export_runtime', "
                    "'export_account_peers_v1', 'SELECT'), "
                    "has_table_privilege('telegram_userbot_export_runtime', "
                    "'export_message_revisions_v1', 'SELECT'), "
                    "has_table_privilege('telegram_userbot_monitor_runtime', "
                    "'outbound_delivery_groups', 'SELECT'), "
                    "has_table_privilege('telegram_userbot_monitor_runtime', "
                    "'outbound_delivery_groups', 'UPDATE'), "
                    "has_table_privilege('telegram_userbot_monitor_runtime', "
                    "'model_credential_versions', 'SELECT'), "
                    "has_table_privilege('telegram_userbot_monitor_runtime', "
                    "'deployment_restore_state', 'SELECT'), "
                    "has_table_privilege('telegram_userbot_monitor_runtime', "
                    "'deployment_restore_state', 'UPDATE')"
                )
            ).one()
            assert export_and_monitor_grants == (
                False,
                False,
                True,
                True,
                True,
                False,
                False,
                True,
                False,
            )
            export_views = connection.execute(
                text(
                    "SELECT c.relname, c.reloptions, pg_get_userbyid(c.relowner) "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname LIKE 'export_%_v1' "
                    "ORDER BY c.relname"
                )
            ).all()
            assert len(export_views) == 6
            assert all("security_barrier=true" in (row[1] or []) for row in export_views)
            assert {row[2] for row in export_views} == {"telegram_userbot_migrator"}
            _verify_sequence_privilege_matrix(connection)

            connection.execute(text("GRANT DELETE ON messages TO telegram_userbot_app_runtime"))
            connection.commit()
            assert (
                connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'telegram_userbot_app_runtime', 'messages', 'DELETE')"
                    )
                ).scalar_one()
                is True
            )

        repeated = migrate_to_head(
            engine,
            Config(str(ROOT / "alembic.ini")),
            expected_revision=EXPECTED_REVISION,
            expected_vector_version=EXPECTED_VECTOR_VERSION,
            role_closure=ROLE_CLOSURE,
        )
        assert repeated.status is MigrationStatus.APPLIED
        assert repeated.from_revision == EXPECTED_REVISION
        assert repeated.to_revision == EXPECTED_REVISION

        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'telegram_userbot_app_runtime', 'messages', 'DELETE')"
                    )
                ).scalar_one()
                is False
            )
            _verify_sequence_privilege_matrix(connection)

        asyncio.run(_verify_runtime_readiness(host, port, passwords))

        with engine.connect() as holder:
            holder.scalar(
                text("SELECT pg_advisory_lock(:lock_key)"),
                {"lock_key": MIGRATION_LOCK_KEY},
            )
            holder.commit()
            try:
                busy = migrate_to_head(
                    engine,
                    Config(str(ROOT / "alembic.ini")),
                    expected_revision=EXPECTED_REVISION,
                    expected_vector_version=EXPECTED_VECTOR_VERSION,
                    role_closure=ROLE_CLOSURE,
                )
                assert busy.status is MigrationStatus.BUSY
            finally:
                released = holder.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": MIGRATION_LOCK_KEY},
                )
                holder.commit()
                assert released is True
    finally:
        engine.dispose()


def _verify_0027_legacy_model_run_round_trip(
    container: DockerContainer,
    *,
    host: str,
    port: int,
    passwords: dict[str, str],
) -> None:
    """Keep a content-free M7 Main AI run intact across the M8 ownership migration."""

    settings = PostgresConnectionSettings(
        host=host,
        port=port,
        database="telegram_userbot",
        login_role="telegram_userbot_migrator_login",
        password=SensitiveValue(passwords["migrator_database_password"]),
        runtime_role="telegram_userbot_migrator",
        sslmode="disable",
        application_name="m8_legacy_model_run_integration",
    )
    engine = create_sync_postgres_engine(settings)
    legacy_run_id = "00000000-0000-7000-8000-000000000101"
    legacy_account_id = "00000000-0000-7000-8000-000000000102"
    legacy_conversation_id = "00000000-0000-7000-8000-000000000103"
    legacy_turn_id = "00000000-0000-7000-8000-000000000104"
    try:
        config = Config(str(ROOT / "alembic.ini"))
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0027_m8_model_run_claim")

        # The generated historical row is deliberately content-free.  Its missing
        # other foreign-key parents are irrelevant to this structural migration test.
        # Keep its turn because 0036 validates the new delivery-turn reference over
        # existing rows. Trigger enforcement is disabled only for this synthetic seed.
        seed_sql = """
        SET session_replication_role = replica;
        INSERT INTO conversation_turns (
          id, account_id, conversation_id, state, trigger_kind, collection_sequence
        ) VALUES (
          '00000000-0000-7000-8000-000000000104',
          '00000000-0000-7000-8000-000000000102',
          '00000000-0000-7000-8000-000000000103', 'completed', 'incoming', 1
        );
        INSERT INTO model_runs (
          id, account_id, conversation_id, turn_id, logical_role, model_profile_id,
          purpose, generation_no, state, config_version_id, credential_version_id,
          prompt_version, prompt_bundle_sha256, capability_snapshot_sha256,
          orchestration_claim_fingerprint, input_fingerprint, adapter_version,
          request_schema_version, output_schema_version, normalizer_version
        ) VALUES (
          '00000000-0000-7000-8000-000000000101',
          '00000000-0000-7000-8000-000000000102',
          '00000000-0000-7000-8000-000000000103',
          '00000000-0000-7000-8000-000000000104', 'main_ai',
          '00000000-0000-7000-8000-000000000105',
          'conversation_reply', 1, 'succeeded',
          '00000000-0000-7000-8000-000000000106',
          '00000000-0000-7000-8000-000000000107', 'm7-legacy',
          decode(repeat('00', 32), 'hex'), decode(repeat('00', 32), 'hex'),
          decode(repeat('00', 32), 'hex'), decode(repeat('00', 32), 'hex'),
          'm7-legacy', 1, 1, 'm7-legacy'
        );
        SET session_replication_role = origin;
        """
        seeded = container.exec(
            ExecConfig(
                [
                    "psql",
                    "--no-password",
                    "--no-psqlrc",
                    "-U",
                    "postgres",
                    "-d",
                    "telegram_userbot",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    seed_sql,
                ],
                user="postgres",
            )
        )
        assert seeded.exit_code == 0

        def assert_m8_row() -> None:
            with engine.connect() as connection:
                assert connection.execute(
                    text(
                        "SELECT id::text, account_id::text, conversation_id::text, turn_id::text, "
                        "memory_job_id, proactive_job_id, logical_role, purpose, "
                        "delivery_turn_id::text "
                        "FROM model_runs WHERE id = :run_id"
                    ),
                    {"run_id": legacy_run_id},
                ).one() == (
                    legacy_run_id,
                    legacy_account_id,
                    legacy_conversation_id,
                    legacy_turn_id,
                    None,
                    None,
                    "main_ai",
                    "conversation_reply",
                    legacy_turn_id,
                )

        first_upgrade = migrate_to_head(
            engine,
            Config(str(ROOT / "alembic.ini")),
            expected_revision=EXPECTED_REVISION,
            expected_vector_version=EXPECTED_VECTOR_VERSION,
            role_closure=ROLE_CLOSURE,
        )
        assert first_upgrade.status is MigrationStatus.APPLIED
        assert first_upgrade.from_revision == "0027_m8_model_run_claim"
        assert first_upgrade.to_revision == EXPECTED_REVISION
        assert_m8_row()

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0027_m8_model_run_claim")
            assert connection.execute(
                text(
                    "SELECT id::text, account_id::text, conversation_id::text, turn_id::text, "
                    "logical_role, purpose FROM model_runs WHERE id = :run_id"
                ),
                {"run_id": legacy_run_id},
            ).one() == (
                legacy_run_id,
                legacy_account_id,
                legacy_conversation_id,
                legacy_turn_id,
                "main_ai",
                "conversation_reply",
            )

        repeated_upgrade = migrate_to_head(
            engine,
            Config(str(ROOT / "alembic.ini")),
            expected_revision=EXPECTED_REVISION,
            expected_vector_version=EXPECTED_VECTOR_VERSION,
            role_closure=ROLE_CLOSURE,
        )
        assert repeated_upgrade.status is MigrationStatus.APPLIED
        assert repeated_upgrade.from_revision == "0027_m8_model_run_claim"
        assert repeated_upgrade.to_revision == EXPECTED_REVISION
        assert_m8_row()
    finally:
        engine.dispose()


def _exercise_fresh_bootstrap(temp_root: Path) -> None:
    values = _bootstrap_passwords()
    secret_directory = temp_root / "secrets"
    _write_bootstrap_secrets(secret_directory, values)
    bootstrap_directory = ROOT / "deploy/postgres/bootstrap"
    try:
        container = (
            DockerContainer(POSTGRES_IMAGE)
            .with_env("POSTGRES_DB", "telegram_userbot")
            .with_env("POSTGRES_USER", "postgres")
            .with_env(
                "POSTGRES_PASSWORD_FILE",
                "/run/secrets/postgres_database_password",
            )
            .with_env(
                "POSTGRES_INITDB_ARGS",
                "--auth-local=peer --auth-host=scram-sha-256",
            )
            .with_env("POSTGRES_HOST_AUTH_METHOD", "scram-sha-256")
            .with_env("PGDATA", "/var/lib/postgresql/data/pgdata")
            .with_volume_mapping(secret_directory.resolve(), "/run/secrets", "ro")
            .with_volume_mapping(
                bootstrap_directory.resolve(),
                "/docker-entrypoint-initdb.d",
                "ro",
            )
            .with_exposed_ports(5432)
            .waiting_for(
                LogMessageWaitStrategy("POSTGRES_BOOTSTRAP_COMPLETE").with_startup_timeout(120)
            )
        )
    except Exception as error:
        if _docker_is_unavailable(error):
            pytest.skip(f"disposable PostgreSQL unavailable: {type(error).__name__}")
        raise
    started = False
    try:
        try:
            container.start()
            started = True
        except Exception as error:
            if _docker_is_unavailable(error):
                pytest.skip(f"disposable PostgreSQL unavailable: {type(error).__name__}")
            raise

        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(5432))
        _wait_for_final_postgres(host, port, values["app_database_password"])
        _verify_application_bootstrap(host, port, values["app_database_password"])
        _verify_postgres_peer_only(
            container,
            host=host,
            port=port,
            bootstrap_password=values["postgres_database_password"],
        )
        _migrate_fresh_database(host, port, values)
        _verify_0027_legacy_model_run_round_trip(
            container,
            host=host,
            port=port,
            passwords=values,
        )
        _verify_credential_accessor_role_isolation(
            container,
            host=host,
            port=port,
            passwords=values,
        )
        _verify_service_status_role_isolation(host, port, values)

        stdout, stderr = container.get_logs()
        combined_logs = stdout + stderr
        assert b"POSTGRES_BOOTSTRAP_COMPLETE" in combined_logs
        for value in values.values():
            assert value.encode("ascii") not in combined_logs
    finally:
        if started:
            container.stop()


@pytest.mark.integration
async def test_fresh_bootstrap_precreates_roles_extension_and_migrator_path(
    tmp_path: Path,
) -> None:
    await asyncio.to_thread(_exercise_fresh_bootstrap, tmp_path)
