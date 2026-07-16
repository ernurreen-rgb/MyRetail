"""Restrict pre-auth rate-limit singleton privileges.

Revision ID: 20260716_02
Revises: 20260716_01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_02"
down_revision: str | None = "20260716_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "REVOKE INSERT, DELETE ON TABLE "
            "myretail_state.auth_rate_limit_meta FROM myretail_api"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "GRANT INSERT, DELETE ON TABLE "
            "myretail_state.auth_rate_limit_meta TO myretail_api"
        )
    )
