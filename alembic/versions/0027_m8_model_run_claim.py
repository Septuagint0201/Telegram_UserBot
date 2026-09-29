"""Separate orchestration claims from canonical model-input fingerprints."""

from collections.abc import Sequence
from hashlib import sha256
from hmac import compare_digest
from uuid import UUID

from alembic import op
from sqlalchemy import LargeBinary, text

revision: str = "0027_m8_model_run_claim"
down_revision: str | Sequence[str] | None = "0026_m8_data_export"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _membership_claim(membership: Sequence[tuple[UUID, int]]) -> bytes:
    if not membership or any(
        not isinstance(message_id, UUID)
        or type(revision_no) is not int
        or not 1 <= revision_no <= 0xFFFFFFFF
        for message_id, revision_no in membership
    ):
        raise RuntimeError("M8_MODEL_RUN_CLAIM_BACKFILL_INVALID_MEMBERSHIP")
    return sha256(
        b"m4-turn-input-v1\0"
        + b"\0".join(
            message_id.bytes + revision_no.to_bytes(4, "big")
            for message_id, revision_no in membership
        )
    ).digest()


def _backfill_orchestration_claims() -> None:
    connection = op.get_bind()
    runs = tuple(
        connection.execute(
            text(
                "SELECT id, logical_role, turn_id, context_manifest_id, input_fingerprint "
                "FROM model_runs WHERE orchestration_claim_fingerprint IS NULL "
                "ORDER BY id"
            )
        ).mappings()
    )
    for run in runs:
        if run["logical_role"] != "main_ai" or not isinstance(run["turn_id"], UUID):
            # Pre-M8 production only creates turn-owned Main AI runs. A future
            # background role needs its own immutable job-manifest claim; copying
            # an unrelated input digest here would silently invent provenance.
            raise RuntimeError("M8_MODEL_RUN_CLAIM_BACKFILL_UNSUPPORTED_OWNER")
        membership = tuple(
            (
                item["message_id"],
                item["message_revision_no"],
            )
            for item in connection.execute(
                text(
                    "SELECT message_id, message_revision_no FROM turn_messages "
                    "WHERE turn_id = :turn_id ORDER BY ordinal"
                ),
                {"turn_id": run["turn_id"]},
            ).mappings()
        )
        claim = _membership_claim(membership)
        stored_input = run["input_fingerprint"]
        if run["context_manifest_id"] is None and (
            not isinstance(stored_input, bytes) or not compare_digest(stored_input, claim)
        ):
            # Before preparation input_fingerprint must still carry the exact M4
            # membership claim. Prepared legacy rows already contain the canonical
            # keyed model-input HMAC and are intentionally not compared to it.
            raise RuntimeError("M8_MODEL_RUN_CLAIM_BACKFILL_INPUT_MISMATCH")
        connection.execute(
            text(
                "UPDATE model_runs SET orchestration_claim_fingerprint = :claim "
                "WHERE id = :run_id AND orchestration_claim_fingerprint IS NULL"
            ),
            {"claim": claim, "run_id": run["id"]},
        )


def _replace_schema_revision(*, projection: str, event_revisions: Sequence[str]) -> None:
    connection = op.get_bind()
    op.execute(
        "ALTER TABLE service_instances DROP CONSTRAINT IF EXISTS schema_revision_current; "
        "ALTER TABLE service_instances DROP CONSTRAINT IF EXISTS "
        "ck_service_instances_schema_revision_current"
    )
    connection.execute(
        text(
            "UPDATE service_instances SET schema_revision = :projection "
            "WHERE schema_revision <> :projection"
        ),
        {"projection": projection},
    )
    op.create_check_constraint(
        "schema_revision_current",
        "service_instances",
        f"schema_revision = '{projection}'",
    )
    op.execute(
        "ALTER TABLE service_status_events DROP CONSTRAINT IF EXISTS schema_revision_current; "
        "ALTER TABLE service_status_events DROP CONSTRAINT IF EXISTS "
        "ck_service_status_events_schema_revision_current"
    )
    allowed = ",".join(f"'{item}'" for item in event_revisions)
    op.create_check_constraint(
        "schema_revision_current",
        "service_status_events",
        f"schema_revision IN ({allowed})",
    )


def upgrade() -> None:
    # Historical create migrations import current metadata, so a clean replay may
    # already contain the column and its check. Normalize both paths explicitly.
    op.execute(
        "ALTER TABLE model_runs ADD COLUMN IF NOT EXISTS orchestration_claim_fingerprint bytea"
    )
    _backfill_orchestration_claims()
    op.alter_column(
        "model_runs",
        "orchestration_claim_fingerprint",
        existing_type=LargeBinary(),
        nullable=False,
    )
    op.execute(
        "ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS "
        "orchestration_claim_fingerprint_32_bytes; "
        "ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS "
        "ck_model_runs_orchestration_claim_fingerprint_32_bytes"
    )
    op.create_check_constraint(
        "orchestration_claim_fingerprint_32_bytes",
        "model_runs",
        "octet_length(orchestration_claim_fingerprint) = 32",
    )
    _replace_schema_revision(
        projection=revision,
        event_revisions=(
            "0025_m8_service_status",
            "0026_m8_data_export",
            revision,
        ),
    )


def downgrade() -> None:
    _replace_schema_revision(
        projection="0026_m8_data_export",
        event_revisions=("0025_m8_service_status", "0026_m8_data_export"),
    )
    op.execute(
        "ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS "
        "ck_model_runs_orchestration_claim_fingerprint_32_bytes; "
        "ALTER TABLE model_runs DROP CONSTRAINT IF EXISTS "
        "orchestration_claim_fingerprint_32_bytes"
    )
    op.drop_column("model_runs", "orchestration_claim_fingerprint")
