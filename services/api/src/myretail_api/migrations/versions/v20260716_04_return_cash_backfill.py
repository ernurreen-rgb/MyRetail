"""Backfill the return cash ledger before durable create-return cutover.

Revision ID: 20260716_04
Revises: 20260716_03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_04"
down_revision: str | None = "20260716_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKFILL_TABLES = (
    "pos_returns",
    "pos_shifts",
    "pos_shift_cash_events",
)


def upgrade() -> None:
    # The migration runs as the table owner. FORCE RLS without a tenant context
    # intentionally exposes zero rows, so the owner temporarily uses its normal
    # RLS bypass for this all-tenant, transactional reconciliation. PostgreSQL
    # rolls these DDL changes back together with the data changes on any error.
    for table_name in _BACKFILL_TABLES:
        op.execute(
            sa.text(
                f"ALTER TABLE myretail_state.{table_name} "
                "NO FORCE ROW LEVEL SECURITY"
            )
        )
    op.execute(
        sa.text(
            """
            INSERT INTO myretail_state.pos_shift_cash_events (
                tenant_id, event_id, shift_id, source_type, source_id,
                effect_kind, amount_delta, created_at
            )
            SELECT
                returned.tenant_id,
                (
                    substr(returned.digest, 1, 8) || '-' ||
                    substr(returned.digest, 9, 4) || '-' ||
                    substr(returned.digest, 13, 4) || '-' ||
                    substr(returned.digest, 17, 4) || '-' ||
                    substr(returned.digest, 21, 12)
                )::uuid,
                returned.shift_id,
                'return',
                returned.return_id,
                'return',
                -returned.refund_total,
                returned.created_at
            FROM (
                SELECT item.*, md5(
                    'myretail:return-cash:' || item.tenant_id || ':' || item.return_id
                ) AS digest
                FROM myretail_state.pos_returns AS item
                WHERE item.state IN ('submitted', 'cancel_pending', 'cancelled')
            ) AS returned
            ON CONFLICT DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO myretail_state.pos_shift_cash_events (
                tenant_id, event_id, shift_id, source_type, source_id,
                effect_kind, amount_delta, created_at
            )
            SELECT
                cancelled.tenant_id,
                (
                    substr(cancelled.digest, 1, 8) || '-' ||
                    substr(cancelled.digest, 9, 4) || '-' ||
                    substr(cancelled.digest, 13, 4) || '-' ||
                    substr(cancelled.digest, 17, 4) || '-' ||
                    substr(cancelled.digest, 21, 12)
                )::uuid,
                cancelled.shift_id,
                'return',
                cancelled.return_id,
                'return_cancel',
                cancelled.refund_total,
                COALESCE(cancelled.cancelled_at, cancelled.updated_at)
            FROM (
                SELECT item.*, md5(
                    'myretail:return-cancel-cash:' || item.tenant_id || ':' ||
                    item.return_id
                ) AS digest
                FROM myretail_state.pos_returns AS item
                WHERE item.state = 'cancelled'
            ) AS cancelled
            ON CONFLICT DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM myretail_state.pos_returns AS returned
                    LEFT JOIN myretail_state.pos_shift_cash_events AS event
                      ON event.tenant_id = returned.tenant_id
                     AND event.source_type = 'return'
                     AND event.source_id = returned.return_id
                     AND event.effect_kind = 'return'
                    WHERE returned.state IN (
                        'submitted', 'cancel_pending', 'cancelled'
                    )
                      AND (
                          event.event_id IS NULL
                          OR event.shift_id <> returned.shift_id
                          OR event.amount_delta <> -returned.refund_total
                      )
                ) THEN
                    RAISE EXCEPTION
                        'return cash-event backfill reconciliation failed';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM myretail_state.pos_returns AS returned
                    LEFT JOIN myretail_state.pos_shift_cash_events AS event
                      ON event.tenant_id = returned.tenant_id
                     AND event.source_type = 'return'
                     AND event.source_id = returned.return_id
                     AND event.effect_kind = 'return_cancel'
                    WHERE returned.state = 'cancelled'
                      AND (
                          event.event_id IS NULL
                          OR event.shift_id <> returned.shift_id
                          OR event.amount_delta <> returned.refund_total
                      )
                ) THEN
                    RAISE EXCEPTION
                        'return-cancel cash-event backfill reconciliation failed';
                END IF;
            END
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            WITH outstanding AS (
                SELECT
                    shift.tenant_id,
                    shift.shift_id,
                    COALESCE(-SUM(event.amount_delta), 0) AS cash_returns_total
                FROM myretail_state.pos_shifts AS shift
                LEFT JOIN myretail_state.pos_shift_cash_events AS event
                  ON event.tenant_id = shift.tenant_id
                 AND event.shift_id = shift.shift_id
                 AND event.source_type = 'return'
                WHERE shift.status = 'open'
                GROUP BY shift.tenant_id, shift.shift_id
            )
            UPDATE myretail_state.pos_shifts AS shift
            SET cash_returns_total = outstanding.cash_returns_total,
                expected_cash = shift.opening_cash + shift.sales_total
                    - outstanding.cash_returns_total,
                updated_at = clock_timestamp()
            FROM outstanding
            WHERE shift.tenant_id = outstanding.tenant_id
              AND shift.shift_id = outstanding.shift_id
              AND outstanding.cash_returns_total >= 0
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT shift.tenant_id, shift.shift_id
                    FROM myretail_state.pos_shifts AS shift
                    LEFT JOIN myretail_state.pos_shift_cash_events AS event
                      ON event.tenant_id = shift.tenant_id
                     AND event.shift_id = shift.shift_id
                     AND event.source_type = 'return'
                    WHERE shift.status = 'open'
                    GROUP BY shift.tenant_id, shift.shift_id
                    HAVING COALESCE(-SUM(event.amount_delta), 0) < 0
                ) THEN
                    RAISE EXCEPTION 'return cash ledger has a negative outstanding total';
                END IF;
            END
            $$
            """
        )
    )
    for table_name in _BACKFILL_TABLES:
        op.execute(
            sa.text(
                f"ALTER TABLE myretail_state.{table_name} "
                "FORCE ROW LEVEL SECURITY"
            )
        )


def downgrade() -> None:
    # Operational cash events are immutable. Downgrade intentionally keeps the
    # reconciled ledger and shift totals instead of deleting auditable data.
    pass
