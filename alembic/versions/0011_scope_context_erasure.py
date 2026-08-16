"""Bind context embedding and erasure targets to their owning account."""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_scope_context_erasure"
down_revision: str | Sequence[str] | None = "0010_account_scope_refs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ensure_scope_data() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM context_manifests m
            LEFT JOIN embedding_spaces s ON s.id = m.embedding_space_id
            WHERE m.embedding_space_id IS NOT NULL
              AND (s.id IS NULL OR s.account_id IS DISTINCT FROM m.account_id)
          ) THEN
            RAISE EXCEPTION 'context manifest embedding space scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM data_erasure_requests r
            LEFT JOIN memories m ON m.id = r.memory_id
            WHERE r.memory_id IS NOT NULL
              AND (m.id IS NULL OR m.account_id IS DISTINCT FROM r.account_id)
          ) THEN
            RAISE EXCEPTION 'erasure memory target scope is inconsistent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM data_erasure_requests r
            LEFT JOIN contacts c ON c.id = r.contact_id
            WHERE r.contact_id IS NOT NULL
              AND (c.id IS NULL OR c.account_id IS DISTINCT FROM r.account_id)
          ) THEN
            RAISE EXCEPTION 'erasure contact target scope is inconsistent';
          END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    _ensure_scope_data()
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'embedding_spaces'::regclass
              AND conname = 'uq_embedding_spaces_id_account'
          ) THEN
            ALTER TABLE embedding_spaces ADD CONSTRAINT uq_embedding_spaces_id_account
              UNIQUE (id, account_id);
          END IF;
        END
        $$;
        """
    )
    op.create_foreign_key(
        "fk_context_manifests_embedding_scope",
        "context_manifests",
        "embedding_spaces",
        ["embedding_space_id", "account_id"],
        ["id", "account_id"],
    )
    op.create_foreign_key(
        "fk_erasure_requests_memory_scope",
        "data_erasure_requests",
        "memories",
        ["memory_id", "account_id"],
        ["id", "account_id"],
    )
    op.create_foreign_key(
        "fk_erasure_requests_contact_scope",
        "data_erasure_requests",
        "contacts",
        ["contact_id", "account_id"],
        ["id", "account_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_erasure_requests_contact_scope",
        "data_erasure_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_erasure_requests_memory_scope",
        "data_erasure_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_context_manifests_embedding_scope",
        "context_manifests",
        type_="foreignkey",
    )

    # The redundant candidate key remains so current metadata and this
    # migration can re-upgrade from 0010 without a dependency-order hazard.
