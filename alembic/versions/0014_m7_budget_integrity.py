"""Make M7 budget buckets and reservations self-describing and fail closed."""

from collections.abc import Sequence

from alembic import op

revision: str = "0014_m7_budget_integrity"
down_revision: str | Sequence[str] | None = "0013_m5_m7_consistency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _constraint(name: str, ddl: str) -> None:
    op.execute(
        "DO $$ BEGIN "  # noqa: S608 - migration-owned constants only
        f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN "
        f"{ddl}; END IF; END $$"
    )


def upgrade() -> None:
    # Existing M7 rows used one UTC local_date and did not retain the bucket
    # snapshot.  Preserve those rows, but refuse to invent bindings for an
    # orphan reservation.
    op.execute(
        "ALTER TABLE proactive_budget_buckets ADD COLUMN IF NOT EXISTS timezone_name_snapshot text"
    )
    op.execute(
        "ALTER TABLE proactive_budget_buckets ADD COLUMN IF NOT EXISTS starts_at timestamptz"
    )
    op.execute("ALTER TABLE proactive_budget_buckets ADD COLUMN IF NOT EXISTS ends_at timestamptz")
    op.execute(
        "UPDATE proactive_budget_buckets SET timezone_name_snapshot = 'UTC', "
        "starts_at = local_date::timestamp AT TIME ZONE 'UTC', "
        "ends_at = (local_date + 1)::timestamp AT TIME ZONE 'UTC' "
        "WHERE timezone_name_snapshot IS NULL OR starts_at IS NULL OR ends_at IS NULL"
    )
    op.execute(
        "ALTER TABLE proactive_budget_buckets ALTER COLUMN timezone_name_snapshot SET NOT NULL"
    )
    op.execute("ALTER TABLE proactive_budget_buckets ALTER COLUMN starts_at SET NOT NULL")
    op.execute("ALTER TABLE proactive_budget_buckets ALTER COLUMN ends_at SET NOT NULL")
    _constraint(
        "ck_proactive_budget_buckets_scope_contact",
        "ALTER TABLE proactive_budget_buckets ADD CONSTRAINT "
        "ck_proactive_budget_buckets_scope_contact CHECK "
        "((scope = 'account_daily' AND contact_id IS NULL) OR "
        "(scope IN ('contact_daily','contact_bypass') AND contact_id IS NOT NULL))",
    )
    _constraint(
        "ck_proactive_budget_buckets_window",
        "ALTER TABLE proactive_budget_buckets ADD CONSTRAINT "
        "ck_proactive_budget_buckets_window CHECK (starts_at < ends_at)",
    )
    _constraint(
        "uq_proactive_budget_buckets_id_account",
        "ALTER TABLE proactive_budget_buckets ADD CONSTRAINT "
        "uq_proactive_budget_buckets_id_account UNIQUE (id, account_id)",
    )

    op.execute("ALTER TABLE proactive_candidates ADD COLUMN IF NOT EXISTS timezone_name text")
    op.execute(
        "UPDATE proactive_candidates AS candidate SET timezone_name = occurrence.timezone_name "
        "FROM proactive_candidate_memberships AS membership "
        "JOIN proactive_occurrences AS occurrence ON occurrence.id = membership.occurrence_id "
        "AND occurrence.account_id = membership.account_id "
        "WHERE candidate.id = membership.candidate_id "
        "AND candidate.account_id = membership.account_id "
        "AND membership.ordinal = 1 AND candidate.timezone_name IS NULL"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM proactive_candidates WHERE timezone_name IS NULL) "
        "THEN RAISE EXCEPTION 'cannot bind existing proactive candidate timezone'; END IF; END $$"
    )
    op.execute("ALTER TABLE proactive_candidates ALTER COLUMN timezone_name SET NOT NULL")

    for column in ("contact_id", "conversation_id", "policy_version_id", "timezone_name"):
        op.execute(
            f"ALTER TABLE proactive_decisions ADD COLUMN IF NOT EXISTS {column} "
            f"{'uuid' if column != 'timezone_name' else 'text'}"
        )
    op.execute(
        "UPDATE proactive_decisions AS decision SET "
        "contact_id = candidate.contact_id, conversation_id = candidate.conversation_id, "
        "policy_version_id = candidate.policy_version_id, timezone_name = candidate.timezone_name "
        "FROM proactive_candidates AS candidate "
        "WHERE decision.candidate_id = candidate.id AND decision.account_id = candidate.account_id "
        "AND (decision.contact_id IS NULL OR decision.conversation_id IS NULL "
        "OR decision.policy_version_id IS NULL OR decision.timezone_name IS NULL)"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM proactive_decisions WHERE contact_id IS NULL "
        "OR conversation_id IS NULL OR policy_version_id IS NULL OR timezone_name IS NULL) "
        "THEN RAISE EXCEPTION 'cannot bind existing proactive decision scope'; END IF; END $$"
    )
    for column in ("contact_id", "conversation_id", "policy_version_id", "timezone_name"):
        op.execute(f"ALTER TABLE proactive_decisions ALTER COLUMN {column} SET NOT NULL")
    _constraint(
        "fk_proactive_decisions_contact_scope",
        "ALTER TABLE proactive_decisions ADD CONSTRAINT fk_proactive_decisions_contact_scope "
        "FOREIGN KEY (contact_id, account_id) REFERENCES contacts (id, account_id)",
    )
    _constraint(
        "fk_proactive_decisions_conversation_scope",
        "ALTER TABLE proactive_decisions ADD CONSTRAINT fk_proactive_decisions_conversation_scope "
        "FOREIGN KEY (conversation_id, account_id) REFERENCES conversations (id, account_id)",
    )
    _constraint(
        "fk_proactive_decisions_policy_scope",
        "ALTER TABLE proactive_decisions ADD CONSTRAINT fk_proactive_decisions_policy_scope "
        "FOREIGN KEY (policy_version_id, account_id) "
        "REFERENCES proactive_policies (id, account_id)",
    )
    _constraint(
        "uq_proactive_decisions_candidate_scope",
        "ALTER TABLE proactive_decisions ADD CONSTRAINT uq_proactive_decisions_candidate_scope "
        "UNIQUE (id, account_id, candidate_id)",
    )

    for column, definition in (
        ("conversation_id", "uuid"),
        ("decision_id", "uuid"),
        ("policy_version_id", "uuid"),
        ("authorization_generation", "integer"),
        ("account_bucket_id", "uuid"),
        ("contact_bucket_id", "uuid"),
        ("bypass_bucket_id", "uuid"),
        ("account_local_date", "date"),
        ("contact_local_date", "date"),
        ("held_at", "timestamptz"),
        ("terminal_at", "timestamptz"),
        ("outbound_group_id", "uuid"),
        ("copilot_draft_id", "uuid"),
        ("reason_code", "text"),
    ):
        op.execute(
            f"ALTER TABLE proactive_budget_reservations "
            f"ADD COLUMN IF NOT EXISTS {column} {definition}"
        )

    # Keep the target table out of JOIN ... ON clauses.  PostgreSQL does not
    # permit a target-table alias to be referenced from those join predicates;
    # materialize the source bindings first and update by the stable row id.
    op.execute(
        "UPDATE proactive_budget_reservations AS reservation SET "
        "conversation_id = backfill.conversation_id, "
        "policy_version_id = backfill.policy_version_id, "
        "decision_id = backfill.decision_id, "
        "authorization_generation = backfill.authorization_generation, "
        "account_local_date = backfill.account_local_date, "
        "contact_local_date = backfill.contact_local_date, "
        "held_at = backfill.held_at, "
        "account_bucket_id = backfill.account_bucket_id, "
        "contact_bucket_id = backfill.contact_bucket_id, "
        "bypass_bucket_id = backfill.bypass_bucket_id "
        "FROM ("
        "SELECT reservation_src.id AS reservation_id, "
        "candidate.conversation_id AS conversation_id, "
        "candidate.policy_version_id AS policy_version_id, "
        "decision.id AS decision_id, "
        "candidate.generation AS authorization_generation, "
        "reservation_src.local_date AS account_local_date, "
        "reservation_src.local_date AS contact_local_date, "
        "COALESCE(reservation_src.held_at, reservation_src.created_at) AS held_at, "
        "account_bucket.id AS account_bucket_id, "
        "contact_bucket.id AS contact_bucket_id, "
        "CASE WHEN reservation_src.bypass THEN bypass_bucket.id ELSE NULL END "
        "AS bypass_bucket_id "
        "FROM proactive_budget_reservations AS reservation_src "
        "JOIN proactive_candidates AS candidate "
        "ON reservation_src.candidate_id = candidate.id "
        "AND reservation_src.account_id = candidate.account_id "
        "JOIN proactive_decisions AS decision "
        "ON decision.candidate_id = candidate.id "
        "AND decision.account_id = candidate.account_id "
        "JOIN proactive_budget_buckets AS account_bucket "
        "ON account_bucket.account_id = reservation_src.account_id "
        "AND account_bucket.contact_id IS NULL AND account_bucket.scope = 'account_daily' "
        "AND account_bucket.local_date = reservation_src.local_date "
        "JOIN proactive_budget_buckets AS contact_bucket "
        "ON contact_bucket.account_id = reservation_src.account_id "
        "AND contact_bucket.contact_id = reservation_src.contact_id "
        "AND contact_bucket.scope = 'contact_daily' "
        "AND contact_bucket.local_date = reservation_src.local_date "
        "LEFT JOIN proactive_budget_buckets AS bypass_bucket "
        "ON bypass_bucket.account_id = reservation_src.account_id "
        "AND bypass_bucket.contact_id = reservation_src.contact_id "
        "AND bypass_bucket.scope = 'contact_bypass' "
        "AND bypass_bucket.local_date = reservation_src.local_date "
        "WHERE reservation_src.conversation_id IS NULL OR reservation_src.decision_id IS NULL "
        "OR reservation_src.account_bucket_id IS NULL OR reservation_src.contact_bucket_id IS NULL"
        ") AS backfill "
        "WHERE reservation.id = backfill.reservation_id"
    )
    op.execute(
        "UPDATE proactive_budget_reservations SET terminal_at = COALESCE(committed_at, created_at) "
        "WHERE state IN ('committed','released','expired','send_unknown') AND terminal_at IS NULL"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM proactive_budget_reservations WHERE "
        "candidate_id IS NULL OR conversation_id IS NULL OR decision_id IS NULL OR "
        "policy_version_id IS NULL OR authorization_generation IS NULL OR "
        "account_bucket_id IS NULL OR contact_bucket_id IS NULL OR "
        "account_local_date IS NULL OR contact_local_date IS NULL OR held_at IS NULL) "
        "THEN RAISE EXCEPTION 'cannot bind existing proactive budget reservations'; END IF; "
        "END $$"
    )
    for column in (
        "candidate_id",
        "conversation_id",
        "decision_id",
        "policy_version_id",
        "authorization_generation",
        "account_bucket_id",
        "contact_bucket_id",
        "account_local_date",
        "contact_local_date",
        "held_at",
    ):
        op.execute(f"ALTER TABLE proactive_budget_reservations ALTER COLUMN {column} SET NOT NULL")

    _constraint(
        "fk_proactive_budget_reservations_decision_scope",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "fk_proactive_budget_reservations_decision_scope FOREIGN KEY (decision_id, account_id) "
        "REFERENCES proactive_decisions (id, account_id)",
    )
    _constraint(
        "fk_proactive_budget_reservations_conversation_scope",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "fk_proactive_budget_reservations_conversation_scope FOREIGN KEY "
        "(conversation_id, account_id) "
        "REFERENCES conversations (id, account_id)",
    )
    _constraint(
        "fk_proactive_budget_reservations_policy_scope",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "fk_proactive_budget_reservations_policy_scope FOREIGN KEY (policy_version_id, account_id) "
        "REFERENCES proactive_policies (id, account_id)",
    )
    for name, column in (
        ("account", "account_bucket_id"),
        ("contact", "contact_bucket_id"),
    ):
        _constraint(
            f"fk_proactive_budget_reservations_{name}_bucket",
            "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
            f"fk_proactive_budget_reservations_{name}_bucket FOREIGN KEY ({column}, account_id) "
            "REFERENCES proactive_budget_buckets (id, account_id)",
        )
    _constraint(
        "fk_proactive_budget_reservations_bypass_bucket",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "fk_proactive_budget_reservations_bypass_bucket FOREIGN KEY (bypass_bucket_id, account_id) "
        "REFERENCES proactive_budget_buckets (id, account_id)",
    )
    _constraint(
        "fk_proactive_budget_reservations_outbound_group_scope",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "fk_proactive_budget_reservations_outbound_group_scope FOREIGN KEY "
        "(outbound_group_id, account_id, conversation_id) REFERENCES outbound_delivery_groups "
        "(id, account_id, conversation_id)",
    )
    _constraint(
        "fk_proactive_budget_reservations_copilot_draft_scope",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "fk_proactive_budget_reservations_copilot_draft_scope FOREIGN KEY "
        "(copilot_draft_id, account_id, conversation_id) REFERENCES copilot_drafts "
        "(id, account_id, conversation_id)",
    )
    _constraint(
        "ck_proactive_budget_reservations_authorization_generation",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "ck_proactive_budget_reservations_authorization_generation CHECK "
        "(authorization_generation > 0)",
    )
    _constraint(
        "ck_proactive_budget_reservations_bypass_bucket",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "ck_proactive_budget_reservations_bypass_bucket CHECK "
        "((bypass AND bypass_bucket_id IS NOT NULL) OR "
        "(NOT bypass AND bypass_bucket_id IS NULL))",
    )
    _constraint(
        "ck_proactive_budget_reservations_local_date",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "ck_proactive_budget_reservations_local_date CHECK "
        "(local_date = contact_local_date)",
    )
    op.execute(
        "ALTER TABLE proactive_budget_reservations DROP CONSTRAINT IF EXISTS "
        "uq_proactive_budget_reservations_key"
    )
    _constraint(
        "uq_proactive_budget_reservations_account_key",
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "uq_proactive_budget_reservations_account_key UNIQUE (account_id, reservation_key)",
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_proactive_budget_reservations_active_decision "
        "ON proactive_budget_reservations (decision_id) "
        "WHERE state IN ('held','committed','send_unknown')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_proactive_budget_reservations_active_decision")
    op.execute(
        "ALTER TABLE proactive_budget_reservations DROP CONSTRAINT IF EXISTS "
        "uq_proactive_budget_reservations_account_key"
    )
    op.execute(
        "ALTER TABLE proactive_budget_reservations ADD CONSTRAINT "
        "uq_proactive_budget_reservations_key UNIQUE (reservation_key)"
    )
    for name in (
        "fk_proactive_budget_reservations_copilot_draft_scope",
        "fk_proactive_budget_reservations_outbound_group_scope",
        "fk_proactive_budget_reservations_bypass_bucket",
        "fk_proactive_budget_reservations_contact_bucket",
        "fk_proactive_budget_reservations_account_bucket",
        "fk_proactive_budget_reservations_policy_scope",
        "fk_proactive_budget_reservations_conversation_scope",
        "fk_proactive_budget_reservations_decision_scope",
        "ck_proactive_budget_reservations_authorization_generation",
        "ck_proactive_budget_reservations_bypass_bucket",
        "ck_proactive_budget_reservations_local_date",
        "uq_proactive_decisions_candidate_scope",
        "fk_proactive_decisions_policy_scope",
        "fk_proactive_decisions_conversation_scope",
        "fk_proactive_decisions_contact_scope",
    ):
        table = (
            "proactive_decisions"
            if name.startswith(("fk_proactive_decisions", "uq_proactive_decisions"))
            else "proactive_budget_reservations"
        )
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    for column in (
        "reason_code",
        "copilot_draft_id",
        "outbound_group_id",
        "terminal_at",
        "held_at",
        "contact_local_date",
        "account_local_date",
        "bypass_bucket_id",
        "contact_bucket_id",
        "account_bucket_id",
        "authorization_generation",
        "policy_version_id",
        "decision_id",
        "conversation_id",
    ):
        op.execute(f"ALTER TABLE proactive_budget_reservations DROP COLUMN IF EXISTS {column}")
    for column in ("timezone_name", "policy_version_id", "conversation_id", "contact_id"):
        op.execute(f"ALTER TABLE proactive_decisions DROP COLUMN IF EXISTS {column}")
    op.execute("ALTER TABLE proactive_candidates DROP COLUMN IF EXISTS timezone_name")
    op.execute(
        "ALTER TABLE proactive_budget_buckets DROP CONSTRAINT IF EXISTS "
        "uq_proactive_budget_buckets_id_account"
    )
    op.execute(
        "ALTER TABLE proactive_budget_buckets DROP CONSTRAINT IF EXISTS "
        "ck_proactive_budget_buckets_window"
    )
    op.execute(
        "ALTER TABLE proactive_budget_buckets DROP CONSTRAINT IF EXISTS "
        "ck_proactive_budget_buckets_scope_contact"
    )
    for column in ("ends_at", "starts_at", "timezone_name_snapshot"):
        op.execute(f"ALTER TABLE proactive_budget_buckets DROP COLUMN IF EXISTS {column}")
