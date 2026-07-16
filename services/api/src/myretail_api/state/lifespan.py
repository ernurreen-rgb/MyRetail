from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from myretail_api.config import Settings
from myretail_api.idempotency import IdempotencyStore
from myretail_api.pos_store import POSStore
from myretail_api.rate_limit import (
    build_postgres_login_rate_limiter,
    build_sqlite_login_rate_limiter,
)
from myretail_api.state.idempotency import (
    PostgresIdempotencyRepository,
    SQLiteIdempotencyRepository,
)
from myretail_api.state.pos_repository import (
    PostgresPOSRepository,
    SQLitePOSRepository,
)
from myretail_api.state.postgres import PostgresStateRuntime
from myretail_api.state.rate_limit import PostgresLoginRateLimitRepository


def build_state_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def state_lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime: PostgresStateRuntime | None = None
        if settings.state_backend == "postgresql":
            runtime = await PostgresStateRuntime.start(settings)
            idempotency_repository = PostgresIdempotencyRepository(runtime.engine)
            pos_repository = PostgresPOSRepository(runtime.engine)
            rate_limit_repository = PostgresLoginRateLimitRepository(
                runtime.engine,
                max_attempts=settings.auth_rate_limit_attempts,
                max_client_attempts=settings.auth_rate_limit_client_attempts,
                window_seconds=settings.auth_rate_limit_window_seconds,
                capacity=settings.auth_rate_limit_capacity,
            )
            login_rate_limiter = build_postgres_login_rate_limiter(
                settings,
                rate_limit_repository,
            )
        else:
            idempotency_repository = SQLiteIdempotencyRepository(
                IdempotencyStore(settings.stock_idempotency_db_path)
            )
            pos_repository = SQLitePOSRepository(
                POSStore(settings.pos_db_path)
            )
            login_rate_limiter = getattr(app.state, "login_rate_limiter", None)
            if login_rate_limiter is None:
                login_rate_limiter = build_sqlite_login_rate_limiter(settings)
        app.state.postgres_state_runtime = runtime
        app.state.shared_idempotency_repository = idempotency_repository
        app.state.pos_state_repository = pos_repository
        app.state.pos_coordination_repository = (
            pos_repository.coordination_repository
        )
        app.state.login_rate_limiter = login_rate_limiter
        try:
            yield
        finally:
            if runtime is not None:
                await runtime.close()

    return state_lifespan
