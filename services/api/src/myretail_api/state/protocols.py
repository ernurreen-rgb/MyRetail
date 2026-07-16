from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, TypeAlias
from uuid import UUID

from myretail_api.idempotency import IdempotencyBeginResult, IdempotencyRecord

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
class WorkflowIntent:
    intent_id: str
    tenant_id: str
    operation: str
    scope_key: str
    principal_key: str
    business_hash: str
    external_marker: str
    state: IntentState
    payload: JsonObject
    lease: FencedLease | None = None
    erp_document_id: str | None = None
    result_id: str | None = None


@dataclass(frozen=True)
class WorkflowIntentClaim:
    acquired: bool
    intent: WorkflowIntent
    recovery_only: bool = False


@dataclass(frozen=True)
class POSIdempotencyClaim:
    acquired: bool
    record: IdempotencyRecord | None = None
    expired: bool = False
    fencing_token: int = 0


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class IdempotencyRepository(Protocol):
    async def begin(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        scope_key: str | None = None,
        lease_seconds: float = 60.0,
    ) -> IdempotencyBeginResult: ...

    async def wait_for_completed(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.05,
    ) -> IdempotencyRecord | None: ...

    async def get_completed(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
    ) -> IdempotencyRecord | None: ...

    async def complete(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        fencing_token: int,
        status_code: int,
        response_body: dict[str, object],
    ) -> bool: ...

    async def mark_recovery_required(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        fencing_token: int,
        lease_seconds: float = 60.0,
    ) -> bool: ...

    async def release(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        fencing_token: int,
    ) -> bool: ...


class POSIdempotencyRepository(Protocol):
    async def begin(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
        lease_seconds: float = 60.0,
    ) -> POSIdempotencyClaim: ...

    async def get_completed(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
    ) -> IdempotencyRecord | None: ...

    async def complete(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
        fencing_token: int,
        status_code: int,
        response_body: dict[str, object],
    ) -> bool: ...

    async def release(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
        fencing_token: int,
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
        expected_shift_updated_at: str | None = None,
        require_no_held_receipts: bool = False,
        lease_seconds: float = 60.0,
    ) -> WorkflowIntentClaim: ...

    async def find_active(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        business_hash: str,
    ) -> WorkflowIntent | None: ...

    async def claim(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        lease_seconds: float = 60.0,
    ) -> WorkflowIntentClaim: ...

    async def claim_due(
        self,
        *,
        tenant_id: str,
        limit: int,
        lease_seconds: float = 60.0,
    ) -> Sequence[WorkflowIntent]: ...

    async def get(
        self,
        *,
        tenant_id: str,
        intent_id: str,
    ) -> WorkflowIntent | None: ...

    async def mark_erp_pending(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        lease: FencedLease,
    ) -> bool: ...

    async def mark_recovery_required(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        lease: FencedLease,
        last_error_code: str | None = None,
    ) -> bool: ...

    async def fail(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        lease: FencedLease,
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
