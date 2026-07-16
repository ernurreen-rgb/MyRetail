from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from myretail_api.config import Settings
from myretail_api.state.postgres import PostgresStateRuntime


def build_state_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def state_lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime: PostgresStateRuntime | None = None
        if settings.state_backend == "postgresql":
            runtime = await PostgresStateRuntime.start(settings)
        app.state.postgres_state_runtime = runtime
        try:
            yield
        finally:
            if runtime is not None:
                await runtime.close()

    return state_lifespan
