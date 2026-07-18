from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, TypeAlias
from uuid import UUID

from myretail_api.idempotency import IdempotencyBeginResult, IdempotencyRecord

JsonObject: TypeAlias = Mapping[str, Any]
CashEventSourceType: TypeAlias = Literal["shift", "sale", "return"]
CashEventEffectKind: TypeAlias = Literal[
    "opening",
    "sale",
    "return",
    "return_cancel",
]
IntentState: TypeAlias = Literal[
    "reserved",
    "erp_pending",
    "recovery_required",
    "materialized",
    "completed",
    "failed",
]
SessionRevocationReason: TypeAlias = Literal[
    "logout",
    "admin_revoke",
    "role_change",
    "route_change",
    "security_incident",
    "session_limit",
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
class POSCashEvent:
    event_id: UUID
    tenant_id: str
    shift_id: str
    source_type: CashEventSourceType
    source_id: str
    effect_kind: CashEventEffectKind
    amount_delta: str
    created_at: datetime


@dataclass(frozen=True)
class POSCashEventAppendResult:
    created: bool
    event: POSCashEvent


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
    reservation_at: datetime | None = None


@dataclass(frozen=True)
class AuthSession:
    tenant_id: str
    session_id: UUID
    principal_id: UUID
    normalized_email: str
    auth_epoch: int
    route_version: int
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


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

    async def attach_alias(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
        intent_id: str,
        business_hash: str,
    ) -> WorkflowIntent: ...

    async def find_by_alias(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
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


class POSCashEventRepository(Protocol):
    async def append_cash_event(
        self,
        *,
        event_id: UUID,
        tenant_id: str,
        shift_id: str,
        source_type: CashEventSourceType,
        source_id: str,
        effect_kind: CashEventEffectKind,
        amount_delta: str,
        created_at: datetime,
    ) -> POSCashEventAppendResult: ...

    async def list_cash_events(
        self,
        *,
        tenant_id: str,
        shift_id: str,
    ) -> Sequence[POSCashEvent]: ...


class LoginRateLimitRepository(Protocol):
    async def check_and_record(
        self,
        *,
        client_bucket_key: str,
        login_bucket_key: str,
    ) -> RateLimitDecision: ...

    async def clear(
        self,
        *,
        client_bucket_key: str,
        login_bucket_key: str,
        reservation_at: datetime,
    ) -> None: ...

    async def discard(
        self,
        *,
        client_bucket_key: str,
        login_bucket_key: str,
        reservation_at: datetime,
    ) -> None: ...


class SessionRepository(Protocol):
    async def issue_session(
        self,
        *,
        tenant_id: str,
        email: str,
        route_version: int,
        ttl_seconds: int,
    ) -> AuthSession: ...

    async def validate_session(
        self,
        *,
        tenant_id: str,
        session_id: UUID,
        principal_id: UUID,
        auth_epoch: int,
        route_version: int,
    ) -> AuthSession | None: ...

    async def revoke_session(
        self,
        *,
        tenant_id: str,
        session_id: UUID,
        reason: SessionRevocationReason,
        revoked_by_principal_id: UUID | None = None,
    ) -> None: ...

    async def revoke_principal_sessions(
        self,
        *,
        tenant_id: str,
        email: str,
        revoked_by_principal_id: UUID,
    ) -> None: ...

class StateHealthRepository(Protocol):
    async def verify(self) -> None: ...
