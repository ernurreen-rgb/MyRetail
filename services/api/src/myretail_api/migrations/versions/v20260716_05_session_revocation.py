"""Add durable principals and revocable authentication sessions.

Revision ID: 20260716_05
Revises: 20260716_04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_05"
down_revision: str | None = "20260716_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SESSION_TABLES = (
    "auth_principals",
    "auth_sessions",
)


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE myretail_state.auth_principals (
                tenant_id text NOT NULL CHECK (btrim(tenant_id) <> ''),
                principal_id uuid NOT NULL,
                normalized_email text NOT NULL CHECK (
                    normalized_email = lower(btrim(normalized_email))
                    AND btrim(normalized_email) <> ''
                ),
                auth_epoch bigint NOT NULL DEFAULT 1 CHECK (auth_epoch > 0),
                disabled_at timestamptz,
                revoked_before timestamptz,
                created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                PRIMARY KEY (tenant_id, principal_id),
                CONSTRAINT auth_principals_email_unique UNIQUE (
                    tenant_id, normalized_email
                )
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TABLE myretail_state.auth_sessions (
                tenant_id text NOT NULL CHECK (btrim(tenant_id) <> ''),
                session_id uuid NOT NULL,
                principal_id uuid NOT NULL,
                auth_epoch bigint NOT NULL CHECK (auth_epoch > 0),
                route_version bigint NOT NULL CHECK (route_version > 0),
                issued_at timestamptz NOT NULL,
                expires_at timestamptz NOT NULL,
                revoked_at timestamptz,
                revocation_reason text CHECK (
                    revocation_reason IS NULL OR revocation_reason IN (
                        'logout', 'admin_revoke', 'role_change',
                        'route_change', 'security_incident'
                    )
                ),
                revoked_by_principal_id uuid,
                created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                PRIMARY KEY (tenant_id, session_id),
                CONSTRAINT auth_sessions_principal_fk
                    FOREIGN KEY (tenant_id, principal_id)
                    REFERENCES myretail_state.auth_principals (
                        tenant_id, principal_id
                    )
                    ON DELETE RESTRICT,
                CONSTRAINT auth_sessions_revoker_fk
                    FOREIGN KEY (tenant_id, revoked_by_principal_id)
                    REFERENCES myretail_state.auth_principals (
                        tenant_id, principal_id
                    )
                    ON DELETE RESTRICT,
                CONSTRAINT auth_sessions_lifetime CHECK (expires_at > issued_at),
                CONSTRAINT auth_sessions_revocation_shape CHECK (
                    (revoked_at IS NULL AND revocation_reason IS NULL)
                    OR (revoked_at IS NOT NULL AND revocation_reason IS NOT NULL)
                )
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX auth_sessions_active_principal
            ON myretail_state.auth_sessions (
                tenant_id, principal_id, expires_at
            )
            WHERE revoked_at IS NULL
            """
        )
    )

    tenant_expression = (
        "tenant_id = NULLIF(current_setting('myretail.tenant_id', true), '')"
    )
    for table_name in _SESSION_TABLES:
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
                f"GRANT SELECT, INSERT, UPDATE, DELETE "
                f"ON TABLE myretail_state.{table_name} TO myretail_api"
            )
        )


def downgrade() -> None:
    for table_name in reversed(_SESSION_TABLES):
        op.execute(
            sa.text(
                f"REVOKE SELECT, INSERT, UPDATE, DELETE "
                f"ON TABLE myretail_state.{table_name} FROM myretail_api"
            )
        )
        op.execute(sa.text(f"DROP TABLE myretail_state.{table_name}"))
