from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from myretail_api.config import Settings
from myretail_api.idempotency import (
    IdempotencyCompletedScopeConflictError,
    IdempotencyConflictError,
    IdempotencyStore,
)
from myretail_api.state.idempotency import (
    PostgresIdempotencyRepository,
    SQLiteIdempotencyRepository,
)
from myretail_api.state.postgres import PostgresStateRuntime
from myretail_api.state.protocols import IdempotencyRepository

APP_DATABASE_URL = os.environ.get("MYRETAIL_TEST_POSTGRES_APP_URL", "")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def postgres_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        state_backend="postgresql",
        auth_rate_limit_secret=SecretStr("test-rate-limit-secret-32-bytes-minimum"),
        state_database_url=SecretStr(APP_DATABASE_URL),
        state_pool_min_size=1,
        state_pool_max_size=2,
        state_pool_acquire_timeout_seconds=1,
        state_statement_timeout_ms=5_000,
        state_lock_timeout_ms=2_000,
        state_postgres_ssl_mode="disable",
    )


async def assert_fencing_and_replay_contract(
    repository: IdempotencyRepository,
    *,
    tenant: str,
) -> None:
    key = str(uuid4())
    request_hash = f"hash-{uuid4()}"
    first = await repository.begin(
        tenant=tenant,
        key=key,
        request_hash=request_hash,
        lease_seconds=0,
    )
    assert first.acquired
    assert not first.recovery_only
    assert first.fencing_token == 1

    takeover = await repository.begin(
        tenant=tenant,
        key=key,
        request_hash=request_hash,
    )
    assert takeover.acquired
    assert takeover.recovery_only
    assert takeover.fencing_token == 2
    assert takeover.storage_key == key

    assert not await repository.complete(
        tenant=tenant,
        key=key,
        request_hash=request_hash,
        fencing_token=first.fencing_token,
        status_code=201,
        response_body={"id": "stale"},
    )
    assert await repository.complete(
        tenant=tenant,
        key=key,
        request_hash=request_hash,
        fencing_token=takeover.fencing_token,
        status_code=201,
        response_body={"id": "canonical"},
    )

    replay = await repository.begin(
        tenant=tenant,
        key=key,
        request_hash=request_hash,
    )
    assert not replay.acquired
    assert replay.record is not None
    assert replay.record.status_code == 201
    assert replay.record.response_body == {"id": "canonical"}


async def assert_scope_alias_contract(
    repository: IdempotencyRepository,
    *,
    tenant: str,
) -> None:
    canonical_key = str(uuid4())
    alias_key = str(uuid4())
    request_hash = f"hash-{uuid4()}"
    scope_key = f"cancel:{uuid4()}"

    canonical = await repository.begin(
        tenant=tenant,
        key=canonical_key,
        request_hash=request_hash,
        scope_key=scope_key,
    )
    alias = await repository.begin(
        tenant=tenant,
        key=alias_key,
        request_hash=request_hash,
        scope_key=scope_key,
    )
    assert canonical.acquired
    assert not alias.acquired
    assert alias.storage_key == canonical_key

    assert await repository.complete(
        tenant=tenant,
        key=canonical_key,
        request_hash=request_hash,
        fencing_token=canonical.fencing_token,
        status_code=200,
        response_body={"status": "cancelled"},
    )
    replay = await repository.begin(
        tenant=tenant,
        key=alias_key,
        request_hash=request_hash,
        scope_key=scope_key,
    )
    assert replay.storage_key == canonical_key
    assert replay.record is not None
    assert replay.record.response_body == {"status": "cancelled"}

    with pytest.raises(IdempotencyCompletedScopeConflictError):
        await repository.begin(
            tenant=tenant,
            key=str(uuid4()),
            request_hash=f"different-{uuid4()}",
            scope_key=scope_key,
        )
    with pytest.raises(IdempotencyConflictError):
        await repository.begin(
            tenant=tenant,
            key=canonical_key,
            request_hash=request_hash,
        )


async def assert_release_cascades_alias_contract(
    repository: IdempotencyRepository,
    *,
    tenant: str,
) -> None:
    first_key = str(uuid4())
    second_key = str(uuid4())
    request_hash = f"hash-{uuid4()}"
    scope_key = f"scope-{uuid4()}"
    first = await repository.begin(
        tenant=tenant,
        key=first_key,
        request_hash=request_hash,
        scope_key=scope_key,
    )
    assert not await repository.release(
        tenant=tenant,
        key=first_key,
        request_hash=request_hash,
        fencing_token=first.fencing_token + 1,
    )
    assert await repository.release(
        tenant=tenant,
        key=first_key,
        request_hash=request_hash,
        fencing_token=first.fencing_token,
    )

    second = await repository.begin(
        tenant=tenant,
        key=second_key,
        request_hash=request_hash,
        scope_key=scope_key,
    )
    assert second.acquired
    assert second.storage_key == second_key


def sqlite_repository(database_path: Path) -> SQLiteIdempotencyRepository:
    return SQLiteIdempotencyRepository(IdempotencyStore(database_path))


@pytest.mark.anyio
async def test_sqlite_idempotency_fencing_and_replay_contract(tmp_path: Path) -> None:
    await assert_fencing_and_replay_contract(
        sqlite_repository(tmp_path / "fencing.sqlite3"),
        tenant="tenant-a",
    )


@pytest.mark.anyio
async def test_sqlite_idempotency_scope_alias_contract(tmp_path: Path) -> None:
    await assert_scope_alias_contract(
        sqlite_repository(tmp_path / "aliases.sqlite3"),
        tenant="tenant-a",
    )


@pytest.mark.anyio
async def test_sqlite_idempotency_release_cascades_aliases(tmp_path: Path) -> None:
    await assert_release_cascades_alias_contract(
        sqlite_repository(tmp_path / "release.sqlite3"),
        tenant="tenant-a",
    )


@pytest.mark.anyio
async def test_sqlite_waiters_cannot_starve_completion(tmp_path: Path) -> None:
    repository = SQLiteIdempotencyRepository(
        IdempotencyStore(tmp_path / "wait-capacity.sqlite3"),
        worker_limit=2,
    )
    tenant = "tenant-a"
    key = str(uuid4())
    request_hash = f"hash-{uuid4()}"
    owner = await repository.begin(
        tenant=tenant,
        key=key,
        request_hash=request_hash,
    )
    waiters = [
        asyncio.create_task(
            repository.wait_for_completed(
                tenant=tenant,
                key=key,
                request_hash=request_hash,
                timeout_seconds=1,
                poll_seconds=0.01,
            )
        )
        for _ in range(2)
    ]
    await asyncio.sleep(0.05)

    completed = await asyncio.wait_for(
        repository.complete(
            tenant=tenant,
            key=key,
            request_hash=request_hash,
            fencing_token=owner.fencing_token,
            status_code=200,
            response_body={"id": "completed"},
        ),
        timeout=0.5,
    )
    records = await asyncio.gather(*waiters)

    assert completed
    assert all(record is not None for record in records)
    assert all(record.response_body == {"id": "completed"} for record in records if record)


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_idempotency_matches_sqlite_contract() -> None:
    runtime = await PostgresStateRuntime.start(postgres_settings())
    repository = PostgresIdempotencyRepository(runtime.engine)
    tenant = f"contract-{uuid4()}"
    try:
        await assert_fencing_and_replay_contract(repository, tenant=tenant)
        await assert_scope_alias_contract(repository, tenant=tenant)
        await assert_release_cascades_alias_contract(repository, tenant=tenant)
    finally:
        await runtime.close()


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_two_postgresql_pools_serialize_different_keys_for_one_scope() -> None:
    first_runtime, second_runtime = await asyncio.gather(
        PostgresStateRuntime.start(postgres_settings()),
        PostgresStateRuntime.start(postgres_settings()),
    )
    first_repository = PostgresIdempotencyRepository(first_runtime.engine)
    second_repository = PostgresIdempotencyRepository(second_runtime.engine)
    tenant = f"concurrent-{uuid4()}"
    first_key = str(uuid4())
    second_key = str(uuid4())
    request_hash = f"hash-{uuid4()}"
    scope_key = f"scope-{uuid4()}"
    try:
        first, second = await asyncio.gather(
            first_repository.begin(
                tenant=tenant,
                key=first_key,
                request_hash=request_hash,
                scope_key=scope_key,
            ),
            second_repository.begin(
                tenant=tenant,
                key=second_key,
                request_hash=request_hash,
                scope_key=scope_key,
            ),
        )
        assert sum(result.acquired for result in (first, second)) == 1
        assert first.storage_key == second.storage_key

        owner_repository, owner_key, owner = (
            (first_repository, first_key, first)
            if first.acquired
            else (second_repository, second_key, second)
        )
        retry_repository, retry_key = (
            (second_repository, second_key)
            if first.acquired
            else (first_repository, first_key)
        )
        canonical_key = owner.storage_key or owner_key
        assert await owner_repository.mark_recovery_required(
            tenant=tenant,
            key=canonical_key,
            request_hash=request_hash,
            fencing_token=owner.fencing_token,
            lease_seconds=0,
        )
        takeover = await retry_repository.begin(
            tenant=tenant,
            key=retry_key,
            request_hash=request_hash,
            scope_key=scope_key,
        )
        assert takeover.acquired
        assert takeover.recovery_only
        assert takeover.fencing_token == owner.fencing_token + 1
        assert not await owner_repository.complete(
            tenant=tenant,
            key=canonical_key,
            request_hash=request_hash,
            fencing_token=owner.fencing_token,
            status_code=201,
            response_body={"id": "stale"},
        )
        assert await retry_repository.complete(
            tenant=tenant,
            key=canonical_key,
            request_hash=request_hash,
            fencing_token=takeover.fencing_token,
            status_code=201,
            response_body={"id": "only-one"},
        )
        async with first_runtime.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                    {"tenant": tenant},
                )
                record_count = (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*)
                            FROM myretail_state.idempotency_records
                            WHERE tenant_id = :tenant
                              AND namespace = 'stock_purchases'
                              AND scope_key = :scope_key
                            """
                        ),
                        {"tenant": tenant, "scope_key": scope_key},
                    )
                ).scalar_one()
                alias_count = (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*)
                            FROM myretail_state.idempotency_aliases
                            WHERE tenant_id = :tenant
                              AND namespace = 'stock_purchases'
                              AND scope_key = :scope_key
                            """
                        ),
                        {"tenant": tenant, "scope_key": scope_key},
                    )
                ).scalar_one()
            finally:
                await transaction.rollback()
        assert record_count == 1
        assert alias_count == 2
    finally:
        await asyncio.gather(first_runtime.close(), second_runtime.close())


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_wait_observes_completion_from_another_pool() -> None:
    first_runtime, second_runtime = await asyncio.gather(
        PostgresStateRuntime.start(postgres_settings()),
        PostgresStateRuntime.start(postgres_settings()),
    )
    first_repository = PostgresIdempotencyRepository(first_runtime.engine)
    second_repository = PostgresIdempotencyRepository(second_runtime.engine)
    tenant = f"wait-{uuid4()}"
    key = str(uuid4())
    request_hash = f"hash-{uuid4()}"
    try:
        owner = await first_repository.begin(
            tenant=tenant,
            key=key,
            request_hash=request_hash,
        )
        waiter = asyncio.create_task(
            second_repository.wait_for_completed(
                tenant=tenant,
                key=key,
                request_hash=request_hash,
                timeout_seconds=2,
                poll_seconds=0.01,
            )
        )
        await asyncio.sleep(0.05)
        assert await first_repository.complete(
            tenant=tenant,
            key=key,
            request_hash=request_hash,
            fencing_token=owner.fencing_token,
            status_code=200,
            response_body={"id": "visible"},
        )
        completed = await waiter
        assert completed is not None
        assert completed.response_body == {"id": "visible"}
    finally:
        await asyncio.gather(first_runtime.close(), second_runtime.close())


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_idempotency_does_not_expose_cross_tenant_identity() -> None:
    runtime = await PostgresStateRuntime.start(postgres_settings())
    repository = PostgresIdempotencyRepository(runtime.engine)
    key = str(uuid4())
    scope_key = f"scope-{uuid4()}"
    try:
        first = await repository.begin(
            tenant=f"tenant-a-{uuid4()}",
            key=key,
            request_hash=f"hash-a-{uuid4()}",
            scope_key=scope_key,
        )
        second = await repository.begin(
            tenant=f"tenant-b-{uuid4()}",
            key=key,
            request_hash=f"hash-b-{uuid4()}",
            scope_key=scope_key,
        )
        assert first.acquired
        assert second.acquired
        assert first.storage_key == key
        assert second.storage_key == key
    finally:
        await runtime.close()
