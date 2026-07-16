STATE_SCHEMA = "myretail_state"
STATE_APP_ROLE = "myretail_api"
STATE_MIGRATOR_ROLE = "myretail_state_migrator"
STATE_OWNER_ROLE = "myretail_state_owner"
EXPECTED_STATE_SCHEMA_REVISION = "20260716_02"

TENANT_STATE_TABLES = (
    "rls_canary",
    "idempotency_records",
    "idempotency_aliases",
    "workflow_intents",
    "pos_shifts",
    "pos_held_receipts",
    "pos_sales",
    "pos_returns",
)

PREAUTH_STATE_TABLES = (
    "auth_rate_limit_buckets",
    "auth_rate_limit_meta",
)
