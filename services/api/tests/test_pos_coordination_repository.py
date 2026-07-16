from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from myretail_api.config import Settings
from myretail_api.main import create_app
from myretail_api.pos_store import (
    POSIdempotencyConflictError,
    POSStore,
    POSStoreConflictError,
)
from myretail_api.state.pos_coordination import (
    PostgresPOSCoordinationRepository,
    SQLitePOSCoordinationRepository,
)
from myretail_api.state.pos_repository import PostgresPOSRepository
from myretail_api.state.postgres import PostgresStateRuntime
from myretail_api.state.protocols import (
    POSIdempotencyRepository,
    WorkflowIntentRepository,
)

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


def sqlite_repository(database_path: Path) -> SQLitePOSCoordinationRepository:
    return SQLitePOSCoordinationRepository(POSStore(database_path))


async def assert_pos_idempotency_contract(
    repository: POSIdempotencyRepository,
    *,
    tenant_id: str,
) -> None:
    operation = "create_sale"
    principal_key = "cashier@example.test"
    key = str(uuid4())
    request_hash = f"hash-{uuid4()}"
    first = await repository.begin(
        tenant_id=tenant_id,
        operation=operation,
        principal_key=principal_key,
        idempotency_key=key,
        request_hash=request_hash,
        lease_seconds=0,
    )
    assert first.acquired
    assert first.fencing_token == 1

    takeover = await repository.begin(
        tenant_id=tenant_id,
        operation=operation,
        principal_key=principal_key,
        idempotency_key=key,
        request_hash=request_hash,
    )
    assert takeover.acquired
    assert takeover.expired
    assert takeover.fencing_token == 2
    assert not await repository.complete(
        tenant_id=tenant_id,
        operation=operation,
        principal_key=principal_key,
        idempotency_key=key,
        request_hash=request_hash,
        fencing_token=first.fencing_token,
        status_code=201,
        response_body={"id": "stale"},
    )
    assert await repository.complete(
        tenant_id=tenant_id,
        operation=operation,
        principal_key=principal_key,
        idempotency_key=key,
        request_hash=request_hash,
        fencing_token=takeover.fencing_token,
        status_code=201,
        response_body={"id": "canonical"},
    )

    replay = await repository.begin(
        tenant_id=tenant_id,
        operation=operation,
        principal_key=principal_key,
        idempotency_key=key,
        request_hash=request_hash,
    )
    assert not replay.acquired
    assert replay.record is not None
    assert replay.record.status_code == 201
    assert replay.record.response_body == {"id": "canonical"}
    with pytest.raises(POSIdempotencyConflictError):
        await repository.begin(
            tenant_id=tenant_id,
            operation=operation,
            principal_key=principal_key,
            idempotency_key=key,
            request_hash=f"different-{uuid4()}",
        )


async def assert_workflow_contract(
    repository: WorkflowIntentRepository,
    *,
    tenant_id: str,
) -> None:
    scope_key = f"shift:{uuid4()}"
    business_hash = f"hash-{uuid4()}"
    marker = f"marker-{uuid4()}"
    reserved = await repository.reserve(
        tenant_id=tenant_id,
        operation="create_sale",
        scope_key=scope_key,
        principal_key="cashier@example.test",
        business_hash=business_hash,
        external_marker=marker,
        payload={"sale": {"id": "SALE-1"}},
    )
    assert reserved.acquired
    assert not reserved.recovery_only
    assert reserved.intent.lease is not None

    duplicate = await repository.reserve(
        tenant_id=tenant_id,
        operation="create_sale",
        scope_key=scope_key,
        principal_key="cashier@example.test",
        business_hash=business_hash,
        external_marker=marker,
        payload={"sale": {"id": "SALE-1"}},
    )
    assert not duplicate.acquired
    assert duplicate.intent.intent_id == reserved.intent.intent_id
    with pytest.raises(POSStoreConflictError) as conflict:
        await repository.reserve(
            tenant_id=tenant_id,
            operation="close_shift",
            scope_key=scope_key,
            principal_key="cashier@example.test",
            business_hash=f"different-{uuid4()}",
            external_marker=f"marker-{uuid4()}",
            payload={"close": {"id": "SHIFT-1"}},
        )
    assert conflict.value.code == "SHIFT_CHANGED"

    lease = reserved.intent.lease
    assert await repository.mark_erp_pending(
        tenant_id=tenant_id,
        intent_id=reserved.intent.intent_id,
        lease=lease,
    )
    assert await repository.mark_recovery_required(
        tenant_id=tenant_id,
        intent_id=reserved.intent.intent_id,
        lease=lease,
        last_error_code="ERPNEXT_TIMEOUT",
    )
    claimed = await repository.claim(
        tenant_id=tenant_id,
        intent_id=reserved.intent.intent_id,
    )
    assert claimed.acquired
    assert claimed.recovery_only
    assert claimed.intent.lease is not None
    assert claimed.intent.lease.fencing_token == lease.fencing_token + 1
    with pytest.raises(ValueError, match="bounded ASCII machine code"):
        await repository.mark_recovery_required(
            tenant_id=tenant_id,
            intent_id=reserved.intent.intent_id,
            lease=claimed.intent.lease,
            last_error_code="ERP timeout: upstream detail must not be persisted",
        )
    assert not await repository.fail(
        tenant_id=tenant_id,
        intent_id=reserved.intent.intent_id,
        lease=lease,
    )
    assert await repository.fail(
        tenant_id=tenant_id,
        intent_id=reserved.intent.intent_id,
        lease=claimed.intent.lease,
        last_error_code="VALIDATION_ERROR",
    )
    assert await repository.find_active(
        tenant_id=tenant_id,
        operation="create_sale",
        principal_key="cashier@example.test",
        business_hash=business_hash,
    ) is None


@pytest.mark.anyio
async def test_sqlite_pos_idempotency_contract(tmp_path: Path) -> None:
    await assert_pos_idempotency_contract(
        sqlite_repository(tmp_path / "pos-idempotency.sqlite3"),
        tenant_id="tenant-a",
    )


@pytest.mark.anyio
async def test_sqlite_workflow_intent_contract(tmp_path: Path) -> None:
    await assert_workflow_contract(
        sqlite_repository(tmp_path / "workflow.sqlite3"),
        tenant_id="tenant-a",
    )


def test_sqlite_retry_does_not_extend_an_active_idempotency_lease(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "lease.sqlite3"
    store = POSStore(database_path)
    key = str(uuid4())
    request_hash = f"hash-{uuid4()}"
    store.begin_idempotency(
        tenant="tenant-a",
        operation="create_sale",
        user_email="cashier@example.test",
        key=key,
        request_hash=request_hash,
        lease_seconds=60,
    )
    with sqlite3.connect(database_path) as connection:
        before = connection.execute(
            "SELECT lease_until FROM pos_idempotency WHERE idempotency_key = ?",
            (key,),
        ).fetchone()[0]
    retry = store.begin_idempotency(
        tenant="tenant-a",
        operation="create_sale",
        user_email="cashier@example.test",
        key=key,
        request_hash=request_hash,
        lease_seconds=3_600,
    )
    with sqlite3.connect(database_path) as connection:
        after = connection.execute(
            "SELECT lease_until FROM pos_idempotency WHERE idempotency_key = ?",
            (key,),
        ).fetchone()[0]
    assert not retry.acquired
    assert after == before


@pytest.mark.anyio
async def test_sqlite_claim_due_returns_recovery_intents_once(tmp_path: Path) -> None:
    repository = sqlite_repository(tmp_path / "claim-due.sqlite3")
    tenant_id = "tenant-a"
    claims = []
    for index in range(2):
        reserved = await repository.reserve(
            tenant_id=tenant_id,
            operation="create_sale",
            scope_key=f"shift:{index}",
            principal_key=f"cashier-{index}@example.test",
            business_hash=f"hash-{index}",
            external_marker=f"marker-{index}",
            payload={"sale": {"id": f"SALE-{index}"}},
        )
        assert reserved.intent.lease is not None
        assert await repository.mark_recovery_required(
            tenant_id=tenant_id,
            intent_id=reserved.intent.intent_id,
            lease=reserved.intent.lease,
        )
        claims.append(reserved)
    due = await repository.claim_due(tenant_id=tenant_id, limit=10)
    assert {intent.intent_id for intent in due} == {
        claim.intent.intent_id for claim in claims
    }
    assert await repository.claim_due(tenant_id=tenant_id, limit=10) == []


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_pos_coordination_matches_sqlite_contract() -> None:
    runtime = await PostgresStateRuntime.start(postgres_settings())
    repository = PostgresPOSCoordinationRepository(runtime.engine)
    tenant_id = f"pos-contract-{uuid4()}"
    try:
        await assert_pos_idempotency_contract(repository, tenant_id=tenant_id)
        await assert_workflow_contract(repository, tenant_id=tenant_id)
    finally:
        await runtime.close()


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_lifespan_exposes_one_process_scoped_repository() -> None:
    app = create_app(postgres_settings())

    async with app.router.lifespan_context(app):
        repository = app.state.pos_coordination_repository
        assert isinstance(repository, PostgresPOSCoordinationRepository)
        assert repository._engine is app.state.postgres_state_runtime.engine
        assert isinstance(app.state.pos_state_repository, PostgresPOSRepository)
        assert app.state.pos_state_repository.coordination_repository is repository


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_retry_does_not_extend_an_active_idempotency_lease() -> None:
    runtime = await PostgresStateRuntime.start(postgres_settings())
    repository = PostgresPOSCoordinationRepository(runtime.engine)
    tenant_id = f"pos-lease-{uuid4()}"
    operation = "create_sale"
    principal_key = "cashier@example.test"
    key = str(uuid4())
    request_hash = f"hash-{uuid4()}"
    try:
        first = await repository.begin(
            tenant_id=tenant_id,
            operation=operation,
            principal_key=principal_key,
            idempotency_key=key,
            request_hash=request_hash,
            lease_seconds=60,
        )
        assert first.acquired
        async with runtime.engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', :tenant_id, true)"),
                {"tenant_id": tenant_id},
            )
            before = await connection.scalar(
                text(
                    """
                    SELECT lease_until
                    FROM myretail_state.idempotency_records
                    WHERE tenant_id = :tenant_id
                      AND namespace = 'pos'
                      AND operation_key = :operation
                      AND principal_key = :principal_key
                      AND idempotency_key = :idempotency_key
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "operation": operation,
                    "principal_key": principal_key,
                    "idempotency_key": key,
                },
            )
        retry = await repository.begin(
            tenant_id=tenant_id,
            operation=operation,
            principal_key=principal_key,
            idempotency_key=key,
            request_hash=request_hash,
            lease_seconds=3_600,
        )
        async with runtime.engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', :tenant_id, true)"),
                {"tenant_id": tenant_id},
            )
            after = await connection.scalar(
                text(
                    """
                    SELECT lease_until
                    FROM myretail_state.idempotency_records
                    WHERE tenant_id = :tenant_id
                      AND namespace = 'pos'
                      AND operation_key = :operation
                      AND principal_key = :principal_key
                      AND idempotency_key = :idempotency_key
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "operation": operation,
                    "principal_key": principal_key,
                    "idempotency_key": key,
                },
            )
        assert not retry.acquired
        assert after == before
    finally:
        await runtime.close()


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_two_postgresql_workers_claim_distinct_due_intents() -> None:
    first_runtime, second_runtime = await asyncio.gather(
        PostgresStateRuntime.start(postgres_settings()),
        PostgresStateRuntime.start(postgres_settings()),
    )
    first = PostgresPOSCoordinationRepository(first_runtime.engine)
    second = PostgresPOSCoordinationRepository(second_runtime.engine)
    tenant_id = f"pos-workers-{uuid4()}"
    reserved_ids: set[str] = set()
    try:
        for index in range(2):
            reserved = await first.reserve(
                tenant_id=tenant_id,
                operation="create_sale",
                scope_key=f"shift:{uuid4()}",
                principal_key=f"cashier-{index}@example.test",
                business_hash=f"hash-{uuid4()}",
                external_marker=f"marker-{uuid4()}",
                payload={"sale": {"id": f"SALE-{index}"}},
            )
            assert reserved.intent.lease is not None
            assert await first.mark_recovery_required(
                tenant_id=tenant_id,
                intent_id=reserved.intent.intent_id,
                lease=reserved.intent.lease,
            )
            reserved_ids.add(reserved.intent.intent_id)
        first_due, second_due = await asyncio.gather(
            first.claim_due(tenant_id=tenant_id, limit=1),
            second.claim_due(tenant_id=tenant_id, limit=1),
        )
        claimed = [*first_due, *second_due]
        assert {intent.intent_id for intent in claimed} == reserved_ids
        assert len({intent.lease.owner_id for intent in claimed if intent.lease}) == 2
    finally:
        await asyncio.gather(first_runtime.close(), second_runtime.close())


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_scope_fencing_and_tenant_isolation_between_pools() -> None:
    first_runtime, second_runtime = await asyncio.gather(
        PostgresStateRuntime.start(postgres_settings()),
        PostgresStateRuntime.start(postgres_settings()),
    )
    first = PostgresPOSCoordinationRepository(first_runtime.engine)
    second = PostgresPOSCoordinationRepository(second_runtime.engine)
    tenant_id = f"pos-scope-{uuid4()}"
    other_tenant = f"pos-scope-other-{uuid4()}"
    scope_key = f"shift:{uuid4()}"
    business_hash = f"hash-{uuid4()}"
    marker = f"marker-{uuid4()}"
    try:
        owner, duplicate = await asyncio.gather(
            first.reserve(
                tenant_id=tenant_id,
                operation="create_sale",
                scope_key=scope_key,
                principal_key="cashier@example.test",
                business_hash=business_hash,
                external_marker=marker,
                payload={"sale": {"id": "SALE-1"}},
            ),
            second.reserve(
                tenant_id=tenant_id,
                operation="create_sale",
                scope_key=scope_key,
                principal_key="cashier@example.test",
                business_hash=business_hash,
                external_marker=marker,
                payload={"sale": {"id": "SALE-1"}},
            ),
        )
        acquired, waiting = (owner, duplicate) if owner.acquired else (duplicate, owner)
        assert acquired.acquired
        assert not waiting.acquired
        assert acquired.intent.intent_id == waiting.intent.intent_id
        assert acquired.intent.lease is not None
        acquired_repository = first if owner.acquired else second
        takeover_repository = second if owner.acquired else first
        assert await acquired_repository.mark_recovery_required(
            tenant_id=tenant_id,
            intent_id=acquired.intent.intent_id,
            lease=acquired.intent.lease,
        )
        takeover = await takeover_repository.claim(
            tenant_id=tenant_id,
            intent_id=acquired.intent.intent_id,
        )
        assert takeover.acquired
        assert takeover.intent.lease is not None
        assert takeover.intent.lease.fencing_token == acquired.intent.lease.fencing_token + 1
        assert not await acquired_repository.fail(
            tenant_id=tenant_id,
            intent_id=acquired.intent.intent_id,
            lease=acquired.intent.lease,
        )
        isolated = await first.reserve(
            tenant_id=other_tenant,
            operation="create_sale",
            scope_key=scope_key,
            principal_key="cashier@example.test",
            business_hash=f"other-{business_hash}",
            external_marker=marker,
            payload={"sale": {"id": "SALE-OTHER"}},
        )
        assert isolated.acquired
        assert isolated.intent.intent_id != acquired.intent.intent_id
    finally:
        await asyncio.gather(first_runtime.close(), second_runtime.close())


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_recovery_claim_survives_runtime_restart() -> None:
    tenant_id = f"pos-restart-{uuid4()}"
    first_runtime = await PostgresStateRuntime.start(postgres_settings())
    first = PostgresPOSCoordinationRepository(first_runtime.engine)
    reserved = await first.reserve(
        tenant_id=tenant_id,
        operation="open_shift",
        scope_key=f"register:{uuid4()}",
        principal_key="cashier@example.test",
        business_hash=f"hash-{uuid4()}",
        external_marker=f"marker-{uuid4()}",
        payload={"shift": {"id": "SHIFT-1"}},
    )
    assert reserved.intent.lease is not None
    assert await first.mark_recovery_required(
        tenant_id=tenant_id,
        intent_id=reserved.intent.intent_id,
        lease=reserved.intent.lease,
        last_error_code="ERPNEXT_TIMEOUT",
    )
    await first_runtime.close()

    second_runtime = await PostgresStateRuntime.start(postgres_settings())
    second = PostgresPOSCoordinationRepository(second_runtime.engine)
    try:
        due = await second.claim_due(tenant_id=tenant_id, limit=10)
        assert [intent.intent_id for intent in due] == [reserved.intent.intent_id]
        assert due[0].lease is not None
        assert due[0].lease.fencing_token == reserved.intent.lease.fencing_token + 1
    finally:
        await second_runtime.close()
