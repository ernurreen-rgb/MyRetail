"""Add immutable return-intent aliases and POS cash events.

Revision ID: 20260716_03
Revises: 20260716_02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_03"
down_revision: str | None = "20260716_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPEND_ONLY_TABLES = (
    "workflow_intent_aliases",
    "pos_shift_cash_events",
)


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE myretail_state.workflow_intent_aliases (
                tenant_id text NOT NULL CHECK (btrim(tenant_id) <> ''),
                operation text NOT NULL CHECK (btrim(operation) <> ''),
                principal_key text NOT NULL,
                idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
                intent_id uuid NOT NULL,
                business_hash text NOT NULL CHECK (btrim(business_hash) <> ''),
                created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                PRIMARY KEY (
                    tenant_id, operation, principal_key, idempotency_key
                ),
                CONSTRAINT workflow_intent_aliases_intent_fk
                    FOREIGN KEY (tenant_id, intent_id)
                    REFERENCES myretail_state.workflow_intents (tenant_id, intent_id)
                    ON DELETE RESTRICT
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX workflow_intent_aliases_intent
            ON myretail_state.workflow_intent_aliases (tenant_id, intent_id)
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TABLE myretail_state.pos_shift_cash_events (
                tenant_id text NOT NULL CHECK (btrim(tenant_id) <> ''),
                event_id uuid NOT NULL,
                shift_id text NOT NULL,
                source_type text NOT NULL CHECK (
                    source_type IN ('shift', 'sale', 'return')
                ),
                source_id text NOT NULL CHECK (btrim(source_id) <> ''),
                effect_kind text NOT NULL CHECK (
                    effect_kind IN ('opening', 'sale', 'return', 'return_cancel')
                ),
                amount_delta numeric(20, 6) NOT NULL,
                created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                PRIMARY KEY (tenant_id, event_id),
                CONSTRAINT pos_shift_cash_events_shift_fk
                    FOREIGN KEY (tenant_id, shift_id)
                    REFERENCES myretail_state.pos_shifts (tenant_id, shift_id)
                    ON DELETE RESTRICT,
                CONSTRAINT pos_shift_cash_events_source_effect_unique UNIQUE (
                    tenant_id, source_type, source_id, effect_kind
                ),
                CONSTRAINT pos_shift_cash_events_effect_shape CHECK (
                    (source_type = 'shift' AND effect_kind = 'opening'
                        AND amount_delta >= 0)
                    OR (source_type = 'sale' AND effect_kind = 'sale'
                        AND amount_delta >= 0)
                    OR (source_type = 'return' AND effect_kind = 'return'
                        AND amount_delta <= 0)
                    OR (source_type = 'return' AND effect_kind = 'return_cancel'
                        AND amount_delta >= 0)
                )
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX pos_shift_cash_events_shift_created
            ON myretail_state.pos_shift_cash_events (
                tenant_id, shift_id, created_at, event_id
            )
            """
        )
    )

    tenant_expression = (
        "tenant_id = NULLIF(current_setting('myretail.tenant_id', true), '')"
    )
    for table_name in _APPEND_ONLY_TABLES:
        op.execute(
            sa.text(
                f"ALTER TABLE myretail_state.{table_name} ENABLE ROW LEVEL SECURITY"
            )
        )
        op.execute(
            sa.text(
                f"ALTER TABLE myretail_state.{table_name} FORCE ROW LEVEL SECURITY"
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE POLICY {table_name}_tenant_isolation
                ON myretail_state.{table_name}
                FOR ALL TO myretail_api
                USING ({tenant_expression})
                WITH CHECK ({tenant_expression})
                """
            )
        )
        op.execute(
            sa.text(
                f"GRANT SELECT, INSERT ON TABLE myretail_state.{table_name} "
                "TO myretail_api"
            )
        )


def downgrade() -> None:
    for table_name in reversed(_APPEND_ONLY_TABLES):
        op.execute(
            sa.text(
                f"REVOKE SELECT, INSERT ON TABLE myretail_state.{table_name} "
                "FROM myretail_api"
            )
        )
        op.execute(sa.text(f"DROP TABLE myretail_state.{table_name}"))
