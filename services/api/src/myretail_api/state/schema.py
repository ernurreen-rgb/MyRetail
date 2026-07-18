STATE_SCHEMA = "myretail_state"
STATE_APP_ROLE = "myretail_api"
STATE_MIGRATOR_ROLE = "myretail_state_migrator"
STATE_OWNER_ROLE = "myretail_state_owner"
EXPECTED_STATE_SCHEMA_REVISION = "20260718_06"

MUTABLE_TENANT_STATE_TABLES = (
    "rls_canary",
    "idempotency_records",
    "idempotency_aliases",
    "workflow_intents",
    "pos_shifts",
    "pos_held_receipts",
    "pos_sales",
    "pos_returns",
    "auth_principals",
    "auth_sessions",
)

APPEND_ONLY_TENANT_STATE_TABLES = (
    "workflow_intent_aliases",
    "pos_shift_cash_events",
)

TENANT_STATE_TABLES = (
    *MUTABLE_TENANT_STATE_TABLES,
    *APPEND_ONLY_TENANT_STATE_TABLES,
)

PREAUTH_STATE_TABLES = (
    "auth_rate_limit_buckets",
    "auth_rate_limit_meta",
)
