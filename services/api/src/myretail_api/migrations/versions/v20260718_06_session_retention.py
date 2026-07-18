"""Add bounded authentication session retention policy.

Revision ID: 20260718_06
Revises: 20260716_05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_06"
down_revision: str | None = "20260716_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            ALTER TABLE myretail_state.auth_sessions
            DROP CONSTRAINT auth_sessions_revocation_reason_check
            """
        )
    )
    op.execute(
        sa.text(
            """
            ALTER TABLE myretail_state.auth_sessions
            ADD CONSTRAINT auth_sessions_revocation_reason_check CHECK (
                revocation_reason IS NULL OR revocation_reason IN (
                    'logout', 'admin_revoke', 'role_change', 'route_change',
                    'security_incident', 'session_limit'
                )
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX auth_sessions_terminal_history
            ON myretail_state.auth_sessions (
                tenant_id,
                principal_id,
                (COALESCE(revoked_at, expires_at)) DESC,
                issued_at DESC,
                session_id DESC
            )
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            ALTER TABLE myretail_state.auth_sessions
            NO FORCE ROW LEVEL SECURITY
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
                    FROM myretail_state.auth_sessions
                    WHERE revocation_reason = 'session_limit'
                ) THEN
                    RAISE EXCEPTION
                        'Cannot downgrade while session_limit audit rows exist'
                        USING ERRCODE = 'object_not_in_prerequisite_state';
                END IF;
            END
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            DROP INDEX myretail_state.auth_sessions_terminal_history
            """
        )
    )
    op.execute(
        sa.text(
            """
            ALTER TABLE myretail_state.auth_sessions
            DROP CONSTRAINT auth_sessions_revocation_reason_check
            """
        )
    )
    op.execute(
        sa.text(
            """
            ALTER TABLE myretail_state.auth_sessions
            ADD CONSTRAINT auth_sessions_revocation_reason_check CHECK (
                revocation_reason IS NULL OR revocation_reason IN (
                    'logout', 'admin_revoke', 'role_change', 'route_change',
                    'security_incident'
                )
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            ALTER TABLE myretail_state.auth_sessions
            FORCE ROW LEVEL SECURITY
            """
        )
    )
