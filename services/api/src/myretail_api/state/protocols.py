from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, TypeAlias
from uuid import UUID

JsonObject: TypeAlias = Mapping[str, Any]
IntentState: TypeAlias = Literal[
    "reserved",
    "erp_pending",
    "recovery_required",
    "materialized",
    "completed",
    "failed",
]


@dataclass(frozen=True)
class FencedLease:
    owner_id: UUID
    fencing_token: int
    lease_until: datetime


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: JsonObject


@dataclass(frozen=True)
class IdempotencyDecision:
    action: Literal["execute", "wait", "recover", "replay"]
    record_id: UUID
    lease: FencedLease | None = None
    response: StoredResponse | None = None


@dataclass(frozen=True)
class WorkflowIntent:
    intent_id: UUID
    tenant_id: str
    operation: str
    scope_key: str
    external_marker: str
    state: IntentState
    payload: JsonObject
    lease: FencedLease | None = None
    erp_document_id: str | None = None
    result_id: str | None = None


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class IdempotencyRepository(Protocol):
    async def begin(
        self,
        *,
        tenant_id: str,
        namespace: str,
        operation_key: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
        scope_key: str | None,
        lease_owner: UUID,
    ) -> IdempotencyDecision: ...

    async def get_completed(self, *, tenant_id: str, record_id: UUID) -> StoredResponse | None: ...

    async def complete(
        self,
        *,
        tenant_id: str,
        record_id: UUID,
        lease: FencedLease,
        response: StoredResponse,
    ) -> bool: ...

    async def mark_recovery_required(
        self,
        *,
        tenant_id: str,
        record_id: UUID,
        lease: FencedLease,
    ) -> bool: ...

    async def release(
        self,
        *,
        tenant_id: str,
        record_id: UUID,
        lease: FencedLease,
    ) -> bool: ...


class WorkflowIntentRepository(Protocol):
    async def reserve(
        self,
        *,
        tenant_id: str,
        operation: str,
        scope_key: str,
        principal_key: str,
        business_hash: str,
        external_marker: str,
        payload: JsonObject,
        lease_owner: UUID,
    ) -> WorkflowIntent: ...

    async def claim_due(
        self,
        *,
        tenant_id: str,
        lease_owner: UUID,
        limit: int,
    ) -> Sequence[WorkflowIntent]: ...

    async def transition(
        self,
        *,
        tenant_id: str,
        intent_id: UUID,
        lease: FencedLease,
        expected_states: frozenset[IntentState],
        target_state: IntentState,
        erp_document_id: str | None = None,
        result_id: str | None = None,
        last_error_code: str | None = None,
    ) -> bool: ...


class POSProjectionRepository(Protocol):
    async def get(
        self,
        *,
        tenant_id: str,
        projection: Literal["shift", "held_receipt", "sale", "return"],
        projection_id: str,
    ) -> JsonObject | None: ...

    async def materialize(
        self,
        *,
        tenant_id: str,
        intent_id: UUID,
        lease: FencedLease,
        projection: Literal["shift", "held_receipt", "sale", "return"],
        projection_id: str,
        row: JsonObject,
    ) -> bool: ...


class LoginRateLimitRepository(Protocol):
    async def check_and_record(
        self,
        *,
        client_bucket_key: str,
        login_bucket_key: str,
        now: datetime,
    ) -> RateLimitDecision: ...

    async def clear(self, *, client_bucket_key: str, login_bucket_key: str) -> None: ...

    async def discard(self, *, client_bucket_key: str, login_bucket_key: str) -> None: ...


class StateHealthRepository(Protocol):
    async def verify(self) -> None: ...
