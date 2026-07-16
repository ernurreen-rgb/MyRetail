"""Create the PostgreSQL state foundation with forced tenant RLS.

Revision ID: 20260716_01
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_TABLES = (
    "rls_canary",
    "idempotency_records",
    "idempotency_aliases",
    "workflow_intents",
    "pos_shifts",
    "pos_held_receipts",
    "pos_sales",
    "pos_returns",
)

_UPGRADE_STATEMENTS = (
    """
    DO $$
    BEGIN
        IF session_user <> 'myretail_state_migrator'
           OR current_user <> 'myretail_state_owner' THEN
            RAISE EXCEPTION 'migration role contract failed';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_roles
            WHERE rolname = 'myretail_api'
              AND rolcanlogin
              AND NOT rolsuper
              AND NOT rolbypassrls
        ) THEN
            RAISE EXCEPTION 'application role contract failed';
        END IF;
    END
    $$
    """,
    "CREATE SCHEMA myretail_state AUTHORIZATION myretail_state_owner",
    "REVOKE ALL ON SCHEMA myretail_state FROM PUBLIC",
    """
    CREATE TABLE myretail_state.rls_canary (
        tenant_id text NOT NULL CHECK (btrim(tenant_id) <> ''),
        canary_id uuid NOT NULL,
        probed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (tenant_id, canary_id)
    )
    """,
    """
    CREATE TABLE myretail_state.idempotency_records (
        record_id uuid PRIMARY KEY,
        tenant_id text NOT NULL CHECK (btrim(tenant_id) <> ''),
        namespace text NOT NULL CHECK (btrim(namespace) <> ''),
        operation_key text NOT NULL CHECK (btrim(operation_key) <> ''),
        principal_key text NOT NULL DEFAULT '',
        idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
        request_hash text NOT NULL CHECK (btrim(request_hash) <> ''),
        scope_key text,
        state text NOT NULL CHECK (state IN ('processing', 'recovery_required', 'completed')),
        status_code integer,
        response_body jsonb,
        lease_owner uuid,
        lease_until timestamptz,
        fencing_token bigint NOT NULL DEFAULT 1 CHECK (fencing_token > 0),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        completed_at timestamptz,
        CONSTRAINT idempotency_records_tenant_record_unique UNIQUE (tenant_id, record_id),
        CONSTRAINT idempotency_records_identity_unique UNIQUE (
            tenant_id, namespace, operation_key, principal_key, idempotency_key
        ),
        CONSTRAINT idempotency_records_scope_nonempty CHECK (
            scope_key IS NULL OR btrim(scope_key) <> ''
        ),
        CONSTRAINT idempotency_records_completed_response CHECK (
            state <> 'completed'
            OR (status_code IS NOT NULL AND response_body IS NOT NULL AND completed_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE UNIQUE INDEX idempotency_records_scope_unique
    ON myretail_state.idempotency_records (
        tenant_id, namespace, operation_key, principal_key, scope_key
    )
    WHERE scope_key IS NOT NULL
    """,
    """
    CREATE INDEX idempotency_records_recovery_due
    ON myretail_state.idempotency_records (tenant_id, lease_until)
    WHERE state IN ('processing', 'recovery_required')
    """,
    """
    CREATE TABLE myretail_state.idempotency_aliases (
        tenant_id text NOT NULL CHECK (btrim(tenant_id) <> ''),
        namespace text NOT NULL CHECK (btrim(namespace) <> ''),
        operation_key text NOT NULL CHECK (btrim(operation_key) <> ''),
        principal_key text NOT NULL DEFAULT '',
        idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
        record_id uuid NOT NULL,
        request_hash text NOT NULL CHECK (btrim(request_hash) <> ''),
        scope_key text NOT NULL CHECK (btrim(scope_key) <> ''),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (tenant_id, namespace, operation_key, principal_key, idempotency_key),
        CONSTRAINT idempotency_aliases_record_fk FOREIGN KEY (tenant_id, record_id)
            REFERENCES myretail_state.idempotency_records (tenant_id, record_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE myretail_state.workflow_intents (
        intent_id uuid PRIMARY KEY,
        tenant_id text NOT NULL CHECK (btrim(tenant_id) <> ''),
        operation text NOT NULL CHECK (btrim(operation) <> ''),
        scope_key text NOT NULL CHECK (btrim(scope_key) <> ''),
        principal_key text NOT NULL,
        business_hash text NOT NULL CHECK (btrim(business_hash) <> ''),
        external_marker text NOT NULL CHECK (btrim(external_marker) <> ''),
        payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        state text NOT NULL CHECK (
            state IN (
                'reserved', 'erp_pending', 'recovery_required',
                'materialized', 'completed', 'failed'
            )
        ),
        lease_owner uuid,
        lease_until timestamptz,
        fencing_token bigint NOT NULL DEFAULT 1 CHECK (fencing_token > 0),
        erp_doc_type text,
        erp_document_id text,
        result_id text,
        attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        next_attempt_at timestamptz,
        last_error_code varchar(128),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        materialized_at timestamptz,
        completed_at timestamptz,
        CONSTRAINT workflow_intents_tenant_intent_unique UNIQUE (tenant_id, intent_id),
        CONSTRAINT workflow_intents_marker_unique UNIQUE (
            tenant_id, operation, external_marker
        )
    )
    """,
    """
    CREATE UNIQUE INDEX workflow_intents_active_scope_unique
    ON myretail_state.workflow_intents (tenant_id, scope_key)
    WHERE state IN ('reserved', 'erp_pending', 'recovery_required', 'materialized')
    """,
    """
    CREATE INDEX workflow_intents_active_business
    ON myretail_state.workflow_intents (
        tenant_id, operation, principal_key, business_hash
    )
    WHERE state IN ('reserved', 'erp_pending', 'recovery_required', 'materialized')
    """,
    """
    CREATE UNIQUE INDEX workflow_intents_active_open_cashier_unique
    ON myretail_state.workflow_intents (tenant_id, principal_key)
    WHERE operation = 'open_shift'
      AND state IN ('reserved', 'erp_pending', 'recovery_required', 'materialized')
    """,
    """
    CREATE INDEX workflow_intents_recovery_due
    ON myretail_state.workflow_intents (tenant_id, next_attempt_at, lease_until)
    WHERE state = 'recovery_required'
    """,
    """
    CREATE TABLE myretail_state.pos_shifts (
        tenant_id text NOT NULL CHECK (btrim(tenant_id) <> ''),
        shift_id text NOT NULL CHECK (btrim(shift_id) <> ''),
        register_id text NOT NULL,
        register_name text NOT NULL,
        warehouse_id text NOT NULL,
        warehouse_name text NOT NULL,
        cashier_email text NOT NULL,
        cashier_full_name text,
        status text NOT NULL CHECK (status IN ('open', 'closed')),
        opening_cash numeric(20, 6) NOT NULL CHECK (opening_cash >= 0),
        sales_total numeric(20, 6) NOT NULL DEFAULT 0 CHECK (sales_total >= 0),
        cash_returns_total numeric(20, 6) NOT NULL DEFAULT 0 CHECK (cash_returns_total >= 0),
        expected_cash numeric(20, 6) NOT NULL CHECK (expected_cash >= 0),
        actual_cash numeric(20, 6),
        difference numeric(20, 6),
        erpnext_opening_id text,
        erpnext_closing_id text,
        opened_at timestamptz NOT NULL,
        closed_at timestamptz,
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (tenant_id, shift_id)
    )
    """,
    """
    CREATE UNIQUE INDEX pos_shifts_open_register_unique
    ON myretail_state.pos_shifts (tenant_id, register_id)
    WHERE status = 'open'
    """,
    """
    CREATE UNIQUE INDEX pos_shifts_open_cashier_unique
    ON myretail_state.pos_shifts (tenant_id, cashier_email)
    WHERE status = 'open'
    """,
    """
    CREATE TABLE myretail_state.pos_held_receipts (
        tenant_id text NOT NULL CHECK (btrim(tenant_id) <> ''),
        held_receipt_id text NOT NULL CHECK (btrim(held_receipt_id) <> ''),
        shift_id text NOT NULL,
        label text,
        lines jsonb NOT NULL CHECK (jsonb_typeof(lines) = 'array'),
        subtotal numeric(20, 6) NOT NULL CHECK (subtotal >= 0),
        discount_total numeric(20, 6) NOT NULL CHECK (discount_total >= 0),
        grand_total numeric(20, 6) NOT NULL CHECK (grand_total >= 0),
        created_by_email text NOT NULL,
        created_by_full_name text,
        status text NOT NULL CHECK (status IN ('open', 'completed')),
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        PRIMARY KEY (tenant_id, held_receipt_id),
        CONSTRAINT pos_held_receipts_shift_fk FOREIGN KEY (tenant_id, shift_id)
            REFERENCES myretail_state.pos_shifts (tenant_id, shift_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX pos_held_receipts_shift_status
    ON myretail_state.pos_held_receipts (tenant_id, shift_id, status)
    """,
    """
    CREATE TABLE myretail_state.pos_sales (
        tenant_id text NOT NULL CHECK (btrim(tenant_id) <> ''),
        sale_id text NOT NULL CHECK (btrim(sale_id) <> ''),
        receipt_number text NOT NULL,
        shift_id text NOT NULL,
        register_id text NOT NULL,
        register_name text NOT NULL,
        warehouse_id text NOT NULL,
        warehouse_name text NOT NULL,
        cashier_email text NOT NULL,
        cashier_full_name text,
        lines jsonb NOT NULL CHECK (jsonb_typeof(lines) = 'array'),
        subtotal numeric(20, 6) NOT NULL CHECK (subtotal >= 0),
        discount_total numeric(20, 6) NOT NULL CHECK (discount_total >= 0),
        grand_total numeric(20, 6) NOT NULL CHECK (grand_total >= 0),
        cash_received numeric(20, 6) NOT NULL CHECK (cash_received >= 0),
        change_amount numeric(20, 6) NOT NULL CHECK (change_amount >= 0),
        erpnext_sales_invoice_id text NOT NULL,
        created_at timestamptz NOT NULL,
        PRIMARY KEY (tenant_id, sale_id),
        CONSTRAINT pos_sales_shift_fk FOREIGN KEY (tenant_id, shift_id)
            REFERENCES myretail_state.pos_shifts (tenant_id, shift_id)
            ON DELETE RESTRICT,
        CONSTRAINT pos_sales_erp_invoice_unique UNIQUE (
            tenant_id, erpnext_sales_invoice_id
        )
    )
    """,
    """
    CREATE INDEX pos_sales_created_at
    ON myretail_state.pos_sales (tenant_id, created_at DESC)
    """,
    """
    CREATE TABLE myretail_state.pos_returns (
        tenant_id text NOT NULL CHECK (btrim(tenant_id) <> ''),
        return_id text NOT NULL CHECK (btrim(return_id) <> ''),
        sale_id text NOT NULL,
        receipt_number text NOT NULL,
        return_receipt_number text NOT NULL,
        state text NOT NULL CHECK (
            state IN ('pending_recovery', 'submitted', 'cancel_pending', 'cancelled')
        ),
        refund_method text NOT NULL,
        reason text NOT NULL,
        comment text,
        register_id text NOT NULL,
        shift_id text NOT NULL,
        cashier_email text NOT NULL,
        currency text NOT NULL,
        refund_total numeric(20, 6) NOT NULL CHECK (refund_total >= 0),
        lines jsonb NOT NULL CHECK (jsonb_typeof(lines) = 'array'),
        erpnext_return_invoice_id text,
        idempotency_key text NOT NULL,
        created_by_email text NOT NULL,
        created_at timestamptz NOT NULL,
        cancelled_by text,
        cancelled_at timestamptz,
        cancel_reason text,
        cancel_comment text,
        updated_at timestamptz NOT NULL,
        PRIMARY KEY (tenant_id, return_id),
        CONSTRAINT pos_returns_sale_fk FOREIGN KEY (tenant_id, sale_id)
            REFERENCES myretail_state.pos_sales (tenant_id, sale_id)
            ON DELETE RESTRICT,
        CONSTRAINT pos_returns_shift_fk FOREIGN KEY (tenant_id, shift_id)
            REFERENCES myretail_state.pos_shifts (tenant_id, shift_id)
            ON DELETE RESTRICT,
        CONSTRAINT pos_returns_idempotency_unique UNIQUE (
            tenant_id, created_by_email, idempotency_key
        )
    )
    """,
    """
    CREATE UNIQUE INDEX pos_returns_erp_invoice_unique
    ON myretail_state.pos_returns (tenant_id, erpnext_return_invoice_id)
    WHERE erpnext_return_invoice_id IS NOT NULL
    """,
    """
    CREATE INDEX pos_returns_sale_state
    ON myretail_state.pos_returns (tenant_id, sale_id, state)
    """,
    """
    CREATE INDEX pos_returns_created_at
    ON myretail_state.pos_returns (tenant_id, created_at DESC)
    """,
    """
    CREATE TABLE myretail_state.auth_rate_limit_buckets (
        bucket_key text PRIMARY KEY,
        bucket_type text NOT NULL CHECK (bucket_type IN ('client', 'login')),
        attempts_at timestamptz[] NOT NULL DEFAULT '{}',
        window_expires_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX auth_rate_limit_buckets_expiry
    ON myretail_state.auth_rate_limit_buckets (window_expires_at)
    """,
    """
    CREATE TABLE myretail_state.auth_rate_limit_meta (
        singleton_id smallint PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
        bucket_count bigint NOT NULL DEFAULT 0 CHECK (bucket_count >= 0),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )
    """,
    "INSERT INTO myretail_state.auth_rate_limit_meta (singleton_id) VALUES (1)",
)


def upgrade() -> None:
    for statement in _UPGRADE_STATEMENTS:
        op.execute(sa.text(statement))

    tenant_expression = (
        "tenant_id = NULLIF(current_setting('myretail.tenant_id', true), '')"
    )
    for table_name in _TENANT_TABLES:
        op.execute(sa.text(f"ALTER TABLE myretail_state.{table_name} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE myretail_state.{table_name} FORCE ROW LEVEL SECURITY"))
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

    table_list = ", ".join(
        f"myretail_state.{table_name}"
        for table_name in (
            *_TENANT_TABLES,
            "auth_rate_limit_buckets",
            "auth_rate_limit_meta",
        )
    )
    op.execute(sa.text("GRANT USAGE ON SCHEMA myretail_state TO myretail_api"))
    op.execute(
        sa.text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table_list} TO myretail_api"
        )
    )
    op.execute(sa.text("REVOKE ALL ON TABLE public.alembic_version FROM PUBLIC"))
    op.execute(sa.text("GRANT SELECT ON TABLE public.alembic_version TO myretail_api"))
    op.execute(sa.text("REVOKE CREATE ON SCHEMA public FROM myretail_state_owner"))


def downgrade() -> None:
    op.execute(sa.text("REVOKE SELECT ON TABLE public.alembic_version FROM myretail_api"))
    op.execute(sa.text("DROP SCHEMA myretail_state CASCADE"))
