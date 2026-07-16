from __future__ import annotations

import asyncio
import sys

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

SAFE_PREFLIGHT_ERRORS = (
    InvalidAuthRateLimitSettingsError,
    InvalidStateFoundationSettingsError,
    StateStartupError,
    UnsafeProductionStateError,
)


async def run_preflight(settings: Settings) -> None:
    validate_production_state_storage(settings)
    validate_state_foundation_settings(settings)
    validate_auth_rate_limit_settings(settings)
    runtime = await PostgresStateRuntime.start(settings)
    await runtime.close()


def main() -> int:
    try:
        settings = Settings()
        asyncio.run(run_preflight(settings))
    except SAFE_PREFLIGHT_ERRORS as exc:
        print(
            f"MyRetail PostgreSQL state preflight failed ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            f"MyRetail PostgreSQL state preflight failed ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 1
    print("MyRetail PostgreSQL state preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
