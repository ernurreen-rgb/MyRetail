from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text

from myretail_api.config import (
    InvalidAuthRateLimitSettingsError,
    InvalidStateFoundationSettingsError,
    Settings,
    UnsafeProductionStateError,
    validate_auth_rate_limit_settings,
    validate_production_state_storage,
    validate_state_foundation_settings,
)
from myretail_api.state.postgres import PostgresStateRuntime, StateStartupError
from myretail_api.tenancy import (
    InvalidTenantRouteSettingsError,
    build_isolated_tenant_route,
)

DATABASE_UNAVAILABLE_EVENT = "MYRETAIL_MONITOR_DATABASE_UNAVAILABLE"
MIGRATION_MISMATCH_EVENT = "MYRETAIL_MONITOR_MIGRATION_MISMATCH"
RECOVERY_AGE_EVENT = "MYRETAIL_MONITOR_RECOVERY_AGE"


class RecoveryAgeExceededError(RuntimeError):
    """Raised without exposing tenant state when recovery work is too old."""


@dataclass(frozen=True)
class RecoveryHealth:
    pending_count: int
    oldest_age_seconds: int


def evaluate_recovery_health(
    *, pending_count: int, oldest_age_seconds: Decimal | float | int | None, maximum_age: int
) -> RecoveryHealth:
    normalized_age = max(0, int(oldest_age_seconds or 0))
    if pending_count < 0:
        raise ValueError("Recovery count cannot be negative")
    if pending_count > 0 and normalized_age > maximum_age:
        raise RecoveryAgeExceededError("PostgreSQL recovery age threshold exceeded")
    return RecoveryHealth(
        pending_count=pending_count,
        oldest_age_seconds=normalized_age,
    )


async def run_monitor(settings: Settings) -> RecoveryHealth:
    validate_production_state_storage(settings)
    validate_state_foundation_settings(settings)
    validate_auth_rate_limit_settings(settings)
    route = build_isolated_tenant_route(settings)
    runtime = await PostgresStateRuntime.start(settings)
    try:
        async with runtime.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text("SELECT set_config('myretail.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(route.tenant_id)},
                )
                row = (
                    await connection.execute(
                        text(
                            """
                            WITH recovery_items(updated_at) AS (
                                SELECT updated_at
                                FROM myretail_state.idempotency_records
                                WHERE state = 'recovery_required'
                                UNION ALL
                                SELECT updated_at
                                FROM myretail_state.workflow_intents
                                WHERE state = 'recovery_required'
                                UNION ALL
                                SELECT updated_at
                                FROM myretail_state.pos_returns
                                WHERE state IN ('pending_recovery', 'cancel_pending')
                            )
                            SELECT count(*)::integer AS pending_count,
                                   EXTRACT(
                                       EPOCH FROM clock_timestamp() - min(updated_at)
                                   ) AS oldest_age_seconds
                            FROM recovery_items
                            """
                        )
                    )
                ).mappings().one()
            finally:
                await transaction.rollback()
    finally:
        await runtime.close()

    return evaluate_recovery_health(
        pending_count=int(row["pending_count"]),
        oldest_age_seconds=row["oldest_age_seconds"],
        maximum_age=settings.state_recovery_max_age_seconds,
    )


def main() -> int:
    try:
        health = asyncio.run(run_monitor(Settings()))
    except RecoveryAgeExceededError:
        print(RECOVERY_AGE_EVENT, file=sys.stderr)
        return 1
    except StateStartupError as exc:
        event = (
            MIGRATION_MISMATCH_EVENT
            if str(exc) == "PostgreSQL state schema revision mismatch"
            else DATABASE_UNAVAILABLE_EVENT
        )
        print(event, file=sys.stderr)
        return 1
    except (
        InvalidAuthRateLimitSettingsError,
        InvalidStateFoundationSettingsError,
        InvalidTenantRouteSettingsError,
        UnsafeProductionStateError,
    ):
        print(DATABASE_UNAVAILABLE_EVENT, file=sys.stderr)
        return 1
    except Exception:
        print(DATABASE_UNAVAILABLE_EVENT, file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "event": "MYRETAIL_MONITOR_OK",
                "oldest_recovery_age_seconds": health.oldest_age_seconds,
                "pending_recovery_count": health.pending_count,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
