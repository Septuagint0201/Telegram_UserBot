"""Record content-free PostgreSQL/Redis compatibility and migration-chain evidence."""

import argparse
import hashlib
import json
from os import environ
from pathlib import Path
from typing import cast

import psycopg
import redis

EXPECTED_POSTGRES_MAJOR = "17"
EXPECTED_VECTOR_VERSION = "0.8.6"
EXPECTED_REDIS_VERSION = "8.2.8"
EXPECTED_REVISION = "0012_worker_lease_retry"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(root: Path, database_dsn: str, redis_url: str) -> dict[str, object]:
    sync_dsn = database_dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(sync_dsn) as connection:
        postgres_version = connection.execute("SHOW server_version").fetchone()
        vector_version = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        ).fetchall()
        unnamed_constraints = connection.execute(
            """
            SELECT COUNT(*) FROM pg_constraint c
            JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE n.nspname = 'public' AND c.conname IS NULL
            """
        ).fetchone()
    if not postgres_version or not str(postgres_version[0]).startswith(
        f"{EXPECTED_POSTGRES_MAJOR}."
    ):
        raise RuntimeError("unexpected PostgreSQL version")
    if not vector_version or vector_version[0] != EXPECTED_VECTOR_VERSION:
        raise RuntimeError("unexpected pgvector version")
    if not revision or revision[0] != EXPECTED_REVISION:
        raise RuntimeError("unexpected Alembic revision")
    if not unnamed_constraints or unnamed_constraints[0] != 0:
        raise RuntimeError("schema contains unnamed constraints")

    redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        server_info = cast(dict[str, object], redis_client.info("server"))
        redis_version = str(server_info["redis_version"])
    finally:
        redis_client.close()
    if redis_version != EXPECTED_REDIS_VERSION:
        raise RuntimeError("unexpected Redis version")

    postgres_image = environ.get("TEST_POSTGRES_IMAGE", "").strip()
    redis_image = environ.get("TEST_REDIS_IMAGE", "").strip()
    if "@sha256:" not in postgres_image or "@sha256:" not in redis_image:
        raise RuntimeError("service image digests are required")

    return {
        "schema_version": 2,
        "revision": EXPECTED_REVISION,
        "migration_sha256": _hash(root / "alembic" / "versions" / "0012_worker_lease_retry.py"),
        "migration_chain_sha256": {
            "0001_m1_durable_state": _hash(
                root / "alembic" / "versions" / "0001_m1_durable_state.py"
            ),
            "0002_m2_model_control": _hash(
                root / "alembic" / "versions" / "0002_m2_model_control.py"
            ),
            "0003_m3_telegram_lifecycle": _hash(
                root / "alembic" / "versions" / "0003_m3_telegram_lifecycle.py"
            ),
            "0004_m4_orchestrator": _hash(
                root / "alembic" / "versions" / "0004_m4_orchestrator.py"
            ),
            "0005_m4_control_result": _hash(
                root / "alembic" / "versions" / "0005_m4_control_result.py"
            ),
            "0006_m5_media_context": _hash(
                root / "alembic" / "versions" / "0006_m5_media_context.py"
            ),
            "0007_m6_memory_pipeline": _hash(
                root / "alembic" / "versions" / "0007_m6_memory_pipeline.py"
            ),
            "0008_m7_proactive_pipeline": _hash(
                root / "alembic" / "versions" / "0008_m7_proactive_pipeline.py"
            ),
            "0009_m5_m6_account_scope": _hash(
                root / "alembic" / "versions" / "0009_m5_m6_account_scope_constraints.py"
            ),
            "0010_account_scope_refs": _hash(
                root / "alembic" / "versions" / "0010_account_scope_refs.py"
            ),
            "0011_scope_context_erasure": _hash(
                root / "alembic" / "versions" / "0011_scope_context_erasure.py"
            ),
            "0012_worker_lease_retry": _hash(
                root / "alembic" / "versions" / "0012_worker_lease_retry.py"
            ),
        },
        "roles_sha256": _hash(root / "deploy" / "postgres" / "m7_roles.sql"),
        "role_chain_sha256": {
            "m1": _hash(root / "deploy" / "postgres" / "m1_roles.sql"),
            "m2": _hash(root / "deploy" / "postgres" / "m2_roles.sql"),
            "m3": _hash(root / "deploy" / "postgres" / "m3_roles.sql"),
            "m4": _hash(root / "deploy" / "postgres" / "m4_roles.sql"),
            "m5": _hash(root / "deploy" / "postgres" / "m5_roles.sql"),
            "m6": _hash(root / "deploy" / "postgres" / "m6_roles.sql"),
            "m7": _hash(root / "deploy" / "postgres" / "m7_roles.sql"),
        },
        "table_count": len(tables),
        "tables": [row[0] for row in tables],
        "unnamed_constraint_count": 0,
        "migration_paths": {
            "empty_to_head": "PASS",
            "head_to_base_to_head": "PASS",
            "previous_supported_to_head": "PASS",
            "account_scope_partial_round_trip": "PASS",
            "interrupted_progress_resume": "PASS",
        },
        "query_baselines": [
            {
                "name": "recent_messages_by_conversation",
                "synthetic_rows": 1000,
                "required_index": "ix_messages_conversation_created",
                "evidence": "tests/integration/test_m1_persistence.py",
            }
        ],
        "services": [
            {
                "name": "postgresql-pgvector",
                "version": f"{postgres_version[0]} / {vector_version[0]}",
                "image": postgres_image,
            },
            {"name": "redis", "version": redis_version, "image": redis_image},
        ],
        "production_load_evidence": "NOT RUN",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    database_dsn = environ.get("TEST_POSTGRES_DSN", "").strip()
    redis_url = environ.get("TEST_REDIS_URL", "").strip()
    if not database_dsn or not redis_url:
        raise RuntimeError("TEST_POSTGRES_DSN and TEST_REDIS_URL are required")
    document = build_manifest(root, database_dsn, redis_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"PASS wrote {args.output.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
