import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4


class POSStoreConflictError(RuntimeError):
    def __init__(self, code: str, message: str, fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields or {}


class POSIdempotencyConflictError(RuntimeError):
    """Raised when an idempotency key is reused with another body."""


class POSStoreMigrationError(RuntimeError):
    """Raised when existing financial data prevents a safe schema migration."""


@dataclass(frozen=True)
class POSIdempotencyRecord:
    status_code: int
    response_body: dict[str, object]


@dataclass(frozen=True)
class POSIdempotencyBeginResult:
    acquired: bool
    record: POSIdempotencyRecord | None = None
    expired: bool = False
    fencing_token: int = 0


@dataclass(frozen=True)
class POSIntentBeginResult:
    acquired: bool
    intent: dict[str, Any]
    recovery_only: bool = False


class POSStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._ensure_schema()

    def begin_idempotency(
        self,
        *,
        tenant: str,
        operation: str,
        user_email: str,
        key: str,
        request_hash: str,
        lease_seconds: int = 60,
    ) -> POSIdempotencyBeginResult:
        now = _now()
        lease_until = _timestamp(time.time() + lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT request_hash, status, status_code, response_body, lease_until,
                       fencing_token
                FROM pos_idempotency
                WHERE tenant = ? AND operation = ? AND user_email = ? AND idempotency_key = ?
                """,
                (tenant, operation, user_email, key),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO pos_idempotency (
                        tenant, operation, user_email, idempotency_key, request_hash,
                        status, status_code, response_body, lease_until, fencing_token,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'processing', 0, '{}', ?, 1, ?, ?)
                    """,
                    (tenant, operation, user_email, key, request_hash, lease_until, now, now),
                )
                connection.commit()
                return POSIdempotencyBeginResult(acquired=True, fencing_token=1)

            if row[0] != request_hash:
                connection.rollback()
                raise POSIdempotencyConflictError("Idempotency key reused with another body")
            if row[1] == "completed":
                connection.commit()
                return POSIdempotencyBeginResult(
                    acquired=False,
                    record=_record_from_row(row[2], row[3]),
                    fencing_token=int(row[5]),
                )
            if _parse_timestamp(str(row[4])) <= time.time():
                connection.execute(
                    """
                    UPDATE pos_idempotency
                    SET lease_until = ?, fencing_token = fencing_token + 1, updated_at = ?
                    WHERE tenant = ? AND operation = ? AND user_email = ? AND idempotency_key = ?
                    """,
                    (lease_until, now, tenant, operation, user_email, key),
                )
                fencing_token = int(row[5]) + 1
                connection.commit()
                return POSIdempotencyBeginResult(
                    acquired=True,
                    expired=True,
                    fencing_token=fencing_token,
                )
            connection.execute(
                """
                UPDATE pos_idempotency
                SET lease_until = ?, updated_at = ?
                WHERE tenant = ? AND operation = ? AND user_email = ? AND idempotency_key = ?
                """,
                (lease_until, now, tenant, operation, user_email, key),
            )
            connection.commit()
            return POSIdempotencyBeginResult(
                acquired=False,
                fencing_token=int(row[5]),
            )

    def get_completed_idempotency(
        self,
        *,
        tenant: str,
        operation: str,
        user_email: str,
        key: str,
        request_hash: str,
    ) -> POSIdempotencyRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_hash, status, status_code, response_body
                FROM pos_idempotency
                WHERE tenant = ? AND operation = ? AND user_email = ? AND idempotency_key = ?
                """,
                (tenant, operation, user_email, key),
            ).fetchone()
        if row is None:
            return None
        if row[0] != request_hash:
            raise POSIdempotencyConflictError("Idempotency key reused with another body")
        if row[1] != "completed":
            return None
        return _record_from_row(row[2], row[3])

    def complete_idempotency(
        self,
        *,
        tenant: str,
        operation: str,
        user_email: str,
        key: str,
        request_hash: str,
        fencing_token: int,
        status_code: int,
        response_body: dict[str, object],
    ) -> bool:
        encoded = json.dumps(
            response_body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE pos_idempotency
                SET status = 'completed', status_code = ?, response_body = ?, updated_at = ?
                WHERE tenant = ? AND operation = ? AND user_email = ? AND idempotency_key = ?
                  AND request_hash = ?
                  AND fencing_token = ? AND status = 'processing'
                """,
                (
                    status_code,
                    encoded,
                    _now(),
                    tenant,
                    operation,
                    user_email,
                    key,
                    request_hash,
                    fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise POSIdempotencyConflictError("Idempotency lease ownership was lost")
            connection.execute(
                """
                UPDATE pos_operation_intents
                SET state = 'completed', updated_at = ?
                WHERE tenant = ? AND operation = ? AND user_email = ?
                  AND business_hash = ? AND state = 'materialized'
                """,
                (_now(), tenant, operation, user_email, request_hash),
            )
        return True

    def release_idempotency(
        self,
        *,
        tenant: str,
        operation: str,
        user_email: str,
        key: str,
        request_hash: str,
        fencing_token: int,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM pos_idempotency
                WHERE tenant = ? AND operation = ? AND user_email = ? AND idempotency_key = ?
                  AND request_hash = ? AND status = 'processing'
                  AND fencing_token = ?
                """,
                (tenant, operation, user_email, key, request_hash, fencing_token),
            )
        return cursor.rowcount == 1

    def begin_operation_intent(
        self,
        *,
        tenant: str,
        operation: str,
        scope_id: str,
        user_email: str,
        business_hash: str,
        payload: dict[str, Any],
        external_key: str | None = None,
        expected_shift_updated_at: str | None = None,
        require_no_held_receipts: bool = False,
        lease_seconds: int = 60,
    ) -> POSIntentBeginResult:
        now = _now()
        lease_until = _timestamp(time.time() + lease_seconds)
        encoded_payload = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM pos_operation_intents
                WHERE tenant = ? AND scope_id = ?
                  AND state IN ('reserved', 'erp_pending', 'recovery_required', 'materialized')
                LIMIT 1
                """,
                (tenant, scope_id),
            ).fetchone()
            if existing is not None:
                intent = dict(existing)
                if not (
                    intent["operation"] == operation
                    and intent["user_email"] == user_email
                    and intent["business_hash"] == business_hash
                ):
                    connection.rollback()
                    raise POSStoreConflictError(
                        "SHIFT_CHANGED", "По смене уже выполняется другая операция"
                    )
                expired = _parse_timestamp(str(intent["lease_until"])) <= time.time()
                recovery_only = intent["state"] != "reserved" or expired
                if intent["state"] == "materialized":
                    connection.commit()
                    return POSIntentBeginResult(
                        acquired=True,
                        intent=intent,
                        recovery_only=True,
                    )
                if intent["state"] == "recovery_required" or expired:
                    fencing_token = int(intent["fencing_token"]) + 1
                    connection.execute(
                        """
                        UPDATE pos_operation_intents
                        SET fencing_token = ?, lease_until = ?, updated_at = ?
                        WHERE id = ? AND fencing_token = ?
                        """,
                        (
                            fencing_token,
                            lease_until,
                            now,
                            intent["id"],
                            intent["fencing_token"],
                        ),
                    )
                    connection.commit()
                    intent.update(
                        {
                            "fencing_token": fencing_token,
                            "lease_until": lease_until,
                            "updated_at": now,
                        }
                    )
                    return POSIntentBeginResult(
                        acquired=True,
                        intent=intent,
                        recovery_only=True,
                    )
                connection.commit()
                return POSIntentBeginResult(
                    acquired=False,
                    intent=intent,
                    recovery_only=recovery_only,
                )

            if expected_shift_updated_at is not None:
                shift_id = scope_id.removeprefix("shift:")
                shift = connection.execute(
                    "SELECT status, updated_at FROM pos_shifts WHERE tenant = ? AND id = ?",
                    (tenant, shift_id),
                ).fetchone()
                if shift is None:
                    connection.rollback()
                    raise POSStoreConflictError("SHIFT_NOT_FOUND", "Смена не найдена")
                if str(shift["status"]) != "open":
                    connection.rollback()
                    raise POSStoreConflictError("SHIFT_CLOSED", "Смена закрыта")
                if str(shift["updated_at"]) != expected_shift_updated_at:
                    connection.rollback()
                    raise POSStoreConflictError("SHIFT_CHANGED", "Смена изменилась")

            if require_no_held_receipts:
                shift_id = scope_id.removeprefix("shift:")
                held = connection.execute(
                    """
                    SELECT 1 FROM pos_held_receipts
                    WHERE tenant = ? AND shift_id = ? AND status = 'open'
                    LIMIT 1
                    """,
                    (tenant, shift_id),
                ).fetchone()
                if held is not None:
                    connection.rollback()
                    raise POSStoreConflictError(
                        "SHIFT_HAS_HELD_RECEIPTS",
                        "Open held receipts block shift closing",
                    )

            intent_id = f"POSOP-{uuid4().hex[:16].upper()}"
            operation_key = external_key or intent_id
            try:
                connection.execute(
                    """
                    INSERT INTO pos_operation_intents (
                        id, tenant, operation, scope_id, user_email, business_hash,
                        external_key, payload_json, state, lease_until, fencing_token,
                        erpnext_document_id, result_id, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, 1,
                        NULL, NULL, ?, ?
                    )
                    """,
                    (
                        intent_id,
                        tenant,
                        operation,
                        scope_id,
                        user_email,
                        business_hash,
                        operation_key,
                        encoded_payload,
                        lease_until,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise POSStoreConflictError(
                    "SHIFT_CHANGED", "Для кассы или кассира уже выполняется операция"
                ) from exc
            connection.commit()
        intent = self.get_operation_intent(intent_id)
        if intent is None:
            raise POSStoreConflictError("SHIFT_CHANGED", "Operation intent was not persisted")
        return POSIntentBeginResult(acquired=True, intent=intent)

    def find_active_operation_intent(
        self,
        *,
        tenant: str,
        operation: str,
        user_email: str,
        business_hash: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM pos_operation_intents
                WHERE tenant = ? AND operation = ? AND user_email = ? AND business_hash = ?
                  AND state IN ('reserved', 'erp_pending', 'recovery_required', 'materialized')
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (tenant, operation, user_email, business_hash),
            ).fetchone()
        return _dict(row)

    def claim_operation_intent(
        self, intent_id: str, *, lease_seconds: int = 60
    ) -> POSIntentBeginResult:
        now = _now()
        lease_until = _timestamp(time.time() + lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pos_operation_intents WHERE id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise POSStoreConflictError("SHIFT_CHANGED", "Operation intent not found")
            intent = dict(row)
            if intent["state"] == "materialized":
                connection.commit()
                return POSIntentBeginResult(
                    acquired=True,
                    intent=intent,
                    recovery_only=True,
                )
            if intent["state"] not in {
                "reserved",
                "erp_pending",
                "recovery_required",
            }:
                connection.commit()
                return POSIntentBeginResult(acquired=False, intent=intent)
            expired = _parse_timestamp(str(intent["lease_until"])) <= time.time()
            if intent["state"] != "recovery_required" and not expired:
                connection.commit()
                return POSIntentBeginResult(
                    acquired=False,
                    intent=intent,
                    recovery_only=intent["state"] != "reserved",
                )
            fencing_token = int(intent["fencing_token"]) + 1
            cursor = connection.execute(
                """
                UPDATE pos_operation_intents
                SET fencing_token = ?, lease_until = ?, updated_at = ?
                WHERE id = ? AND fencing_token = ?
                  AND state IN ('reserved', 'erp_pending', 'recovery_required')
                """,
                (
                    fencing_token,
                    lease_until,
                    now,
                    intent_id,
                    intent["fencing_token"],
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise POSStoreConflictError("SHIFT_CHANGED", "Operation intent changed")
            connection.commit()
        intent.update(
            {
                "fencing_token": fencing_token,
                "lease_until": lease_until,
                "updated_at": now,
            }
        )
        return POSIntentBeginResult(acquired=True, intent=intent, recovery_only=True)

    def get_operation_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pos_operation_intents WHERE id = ?",
                (intent_id,),
            ).fetchone()
        return _dict(row)

    def mark_operation_erp_pending(self, intent_id: str, fencing_token: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE pos_operation_intents
                SET state = 'erp_pending', updated_at = ?
                WHERE id = ? AND fencing_token = ? AND state = 'reserved'
                """,
                (_now(), intent_id, fencing_token),
            )
        return cursor.rowcount == 1

    def mark_operation_recovery_required(self, intent_id: str, fencing_token: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE pos_operation_intents
                SET state = 'recovery_required', lease_until = ?, updated_at = ?
                WHERE id = ? AND fencing_token = ?
                  AND state IN ('reserved', 'erp_pending', 'recovery_required')
                """,
                (_timestamp(time.time() - 1), _now(), intent_id, fencing_token),
            )
        return cursor.rowcount == 1

    def fail_operation_intent(self, intent_id: str, fencing_token: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE pos_operation_intents
                SET state = 'failed', updated_at = ?
                WHERE id = ? AND fencing_token = ?
                  AND state IN ('reserved', 'erp_pending', 'recovery_required')
                """,
                (_now(), intent_id, fencing_token),
            )
        return cursor.rowcount == 1

    def materialize_open_shift_intent(
        self, intent_id: str, fencing_token: int, erpnext_opening_id: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intent = self._owned_intent(
                connection, intent_id, fencing_token, operation="open_shift"
            )
            payload = _intent_payload(intent)
            shift_row = dict(payload["shift"])
            shift_row["erpnext_opening_id"] = erpnext_opening_id
            existing = connection.execute(
                "SELECT * FROM pos_shifts WHERE tenant = ? AND id = ?",
                (shift_row["tenant"], shift_row["id"]),
            ).fetchone()
            if existing is None:
                active = connection.execute(
                    """
                    SELECT id FROM pos_shifts
                    WHERE tenant = ? AND status = 'open'
                      AND (register_id = ? OR cashier_email = ?)
                    LIMIT 1
                    """,
                    (
                        shift_row["tenant"],
                        shift_row["register_id"],
                        shift_row["cashier_email"],
                    ),
                ).fetchone()
                if active is not None:
                    connection.rollback()
                    raise POSStoreConflictError("SHIFT_ALREADY_OPEN", "Смена уже открыта")
                connection.execute(
                    """
                    INSERT INTO pos_shifts (
                        id, tenant, register_id, register_name, warehouse_id, warehouse_name,
                        cashier_email, cashier_full_name, status, opening_cash, sales_total,
                        expected_cash, actual_cash, difference, erpnext_opening_id,
                        erpnext_closing_id, opened_at, closed_at, updated_at
                    ) VALUES (
                        :id, :tenant, :register_id, :register_name, :warehouse_id,
                        :warehouse_name, :cashier_email, :cashier_full_name, 'open',
                        :opening_cash, '0.00', :opening_cash, NULL, NULL,
                        :erpnext_opening_id, NULL, :opened_at, NULL, :updated_at
                    )
                    """,
                    shift_row,
                )
            self._complete_intent(
                connection,
                intent_id,
                fencing_token,
                erpnext_document_id=erpnext_opening_id,
                result_id=str(shift_row["id"]),
            )
            connection.commit()
        return self.get_shift(str(shift_row["tenant"]), str(shift_row["id"])) or shift_row

    def materialize_close_shift_intent(
        self, intent_id: str, fencing_token: int, erpnext_closing_id: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intent = self._owned_intent(
                connection, intent_id, fencing_token, operation="close_shift"
            )
            payload = _intent_payload(intent)
            close = dict(payload["close"])
            shift = connection.execute(
                "SELECT * FROM pos_shifts WHERE tenant = ? AND id = ?",
                (close["tenant"], close["shift_id"]),
            ).fetchone()
            if shift is None:
                connection.rollback()
                raise POSStoreConflictError("SHIFT_NOT_FOUND", "Смена не найдена")
            current = dict(shift)
            if current["status"] == "open":
                if current["updated_at"] != close["expected_updated_at"]:
                    connection.rollback()
                    raise POSStoreConflictError("SHIFT_CHANGED", "Смена изменилась")
                connection.execute(
                    """
                    UPDATE pos_shifts
                    SET status = 'closed', actual_cash = ?, difference = ?,
                        erpnext_closing_id = ?, closed_at = ?, updated_at = ?
                    WHERE tenant = ? AND id = ? AND status = 'open'
                    """,
                    (
                        close["actual_cash"],
                        close["difference"],
                        erpnext_closing_id,
                        close["closed_at"],
                        close["closed_at"],
                        close["tenant"],
                        close["shift_id"],
                    ),
                )
            elif current.get("erpnext_closing_id") != erpnext_closing_id:
                connection.rollback()
                raise POSStoreConflictError("SHIFT_CLOSED", "Смена уже закрыта")
            self._complete_intent(
                connection,
                intent_id,
                fencing_token,
                erpnext_document_id=erpnext_closing_id,
                result_id=str(close["shift_id"]),
            )
            connection.commit()
        return self.get_shift(str(close["tenant"]), str(close["shift_id"])) or current

    def materialize_sale_intent(
        self, intent_id: str, fencing_token: int, erpnext_invoice_id: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intent = self._owned_intent(
                connection, intent_id, fencing_token, operation="create_sale"
            )
            payload = _intent_payload(intent)
            sale_row = dict(payload["sale"])
            sale_row["receipt_number"] = erpnext_invoice_id
            sale_row["erpnext_sales_invoice_id"] = erpnext_invoice_id
            existing = connection.execute(
                """
                SELECT * FROM pos_sales
                WHERE tenant = ? AND erpnext_sales_invoice_id = ?
                """,
                (sale_row["tenant"], erpnext_invoice_id),
            ).fetchone()
            if existing is None:
                shift = connection.execute(
                    "SELECT * FROM pos_shifts WHERE tenant = ? AND id = ?",
                    (sale_row["tenant"], sale_row["shift_id"]),
                ).fetchone()
                if shift is None:
                    connection.rollback()
                    raise POSStoreConflictError("SHIFT_NOT_FOUND", "Смена не найдена")
                shift_data = dict(shift)
                if shift_data["status"] != "open":
                    connection.rollback()
                    raise POSStoreConflictError("SHIFT_CLOSED", "Смена закрыта")
                try:
                    connection.execute(
                        """
                        INSERT INTO pos_sales (
                            id, tenant, receipt_number, shift_id, register_id, register_name,
                            warehouse_id, warehouse_name, cashier_email, cashier_full_name,
                            lines_json, subtotal, discount_total, grand_total, cash_received,
                            change, erpnext_sales_invoice_id, created_at
                        ) VALUES (
                            :id, :tenant, :receipt_number, :shift_id, :register_id,
                            :register_name, :warehouse_id, :warehouse_name, :cashier_email,
                            :cashier_full_name, :lines_json, :subtotal, :discount_total,
                            :grand_total, :cash_received, :change,
                            :erpnext_sales_invoice_id, :created_at
                        )
                        """,
                        sale_row,
                    )
                except sqlite3.IntegrityError as exc:
                    connection.rollback()
                    raise POSStoreConflictError(
                        "IDEMPOTENCY_CONFLICT", "ERPNext invoice уже materialized"
                    ) from exc
                sales_total = _money_add(shift_data["sales_total"], sale_row["grand_total"])
                expected_cash = _money_add(shift_data["opening_cash"], sales_total)
                connection.execute(
                    """
                    UPDATE pos_shifts
                    SET sales_total = ?, expected_cash = ?, updated_at = ?
                    WHERE tenant = ? AND id = ? AND status = 'open'
                    """,
                    (
                        sales_total,
                        expected_cash,
                        sale_row["created_at"],
                        sale_row["tenant"],
                        sale_row["shift_id"],
                    ),
                )
                held_receipt_id = payload.get("held_receipt_id")
                if held_receipt_id:
                    connection.execute(
                        """
                        UPDATE pos_held_receipts
                        SET status = 'completed', updated_at = ?
                        WHERE tenant = ? AND id = ? AND status = 'open'
                        """,
                        (sale_row["created_at"], sale_row["tenant"], held_receipt_id),
                    )
                result_id = str(sale_row["id"])
            else:
                result_id = str(existing["id"])
            self._complete_intent(
                connection,
                intent_id,
                fencing_token,
                erpnext_document_id=erpnext_invoice_id,
                result_id=result_id,
            )
            connection.commit()
        return self.get_sale(str(sale_row["tenant"]), result_id) or sale_row

    @staticmethod
    def _owned_intent(
        connection: sqlite3.Connection,
        intent_id: str,
        fencing_token: int,
        *,
        operation: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT * FROM pos_operation_intents
            WHERE id = ? AND operation = ? AND fencing_token = ?
              AND state IN ('reserved', 'erp_pending', 'recovery_required')
            """,
            (intent_id, operation, fencing_token),
        ).fetchone()
        if row is None:
            connection.rollback()
            raise POSStoreConflictError(
                "IDEMPOTENCY_CONFLICT", "Operation lease больше не принадлежит запросу"
            )
        return dict(row)

    @staticmethod
    def _complete_intent(
        connection: sqlite3.Connection,
        intent_id: str,
        fencing_token: int,
        *,
        erpnext_document_id: str,
        result_id: str,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE pos_operation_intents
            SET state = 'materialized', erpnext_document_id = ?, result_id = ?, updated_at = ?
            WHERE id = ? AND fencing_token = ?
              AND state IN ('reserved', 'erp_pending', 'recovery_required')
            """,
            (erpnext_document_id, result_id, _now(), intent_id, fencing_token),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise POSStoreConflictError(
                "IDEMPOTENCY_CONFLICT", "Operation lease больше не принадлежит запросу"
            )

    def create_shift(self, row: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active_register = connection.execute(
                """
                SELECT id FROM pos_shifts
                WHERE tenant = ? AND register_id = ? AND status = 'open'
                """,
                (row["tenant"], row["register_id"]),
            ).fetchone()
            if active_register is not None:
                connection.rollback()
                raise POSStoreConflictError("SHIFT_ALREADY_OPEN", "Смена на кассе уже открыта")
            active_cashier = connection.execute(
                """
                SELECT id FROM pos_shifts
                WHERE tenant = ? AND cashier_email = ? AND status = 'open'
                """,
                (row["tenant"], row["cashier_email"]),
            ).fetchone()
            if active_cashier is not None:
                connection.rollback()
                raise POSStoreConflictError(
                    "SHIFT_ALREADY_OPEN", "У кассира уже есть открытая смена"
                )
            connection.execute(
                """
                INSERT INTO pos_shifts (
                    id, tenant, register_id, register_name, warehouse_id, warehouse_name,
                    cashier_email, cashier_full_name, status, opening_cash, sales_total,
                    expected_cash, actual_cash, difference, erpnext_opening_id,
                    erpnext_closing_id, opened_at, closed_at, updated_at
                )
                VALUES (
                    :id, :tenant, :register_id, :register_name, :warehouse_id, :warehouse_name,
                    :cashier_email, :cashier_full_name, 'open', :opening_cash, '0.00',
                    :opening_cash, NULL, NULL, :erpnext_opening_id, NULL,
                    :opened_at, NULL, :updated_at
                )
                """,
                row,
            )
            connection.commit()
        return self.get_shift(row["tenant"], row["id"]) or row

    def get_shift(self, tenant: str, shift_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pos_shifts WHERE tenant = ? AND id = ?",
                (tenant, shift_id),
            ).fetchone()
        return _dict(row)

    def get_current_shift(
        self, tenant: str, register_id: str, cashier_email: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM pos_shifts
                WHERE tenant = ? AND register_id = ? AND cashier_email = ? AND status = 'open'
                ORDER BY opened_at DESC
                LIMIT 1
                """,
                (tenant, register_id, cashier_email),
            ).fetchone()
        return _dict(row)

    def close_shift(
        self,
        *,
        tenant: str,
        shift_id: str,
        expected_updated_at: str,
        actual_cash: str,
        difference: str,
        erpnext_closing_id: str,
        closed_at: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pos_shifts WHERE tenant = ? AND id = ?",
                (tenant, shift_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise POSStoreConflictError("SHIFT_NOT_FOUND", "Смена не найдена")
            current = _dict(row)
            if current["status"] == "closed":
                connection.rollback()
                raise POSStoreConflictError("SHIFT_CLOSED", "Смена уже закрыта")
            if current["updated_at"] != expected_updated_at:
                connection.rollback()
                raise POSStoreConflictError("SHIFT_CHANGED", "Смена изменилась")
            connection.execute(
                """
                UPDATE pos_shifts
                SET status = 'closed', actual_cash = ?, difference = ?,
                    erpnext_closing_id = ?, closed_at = ?, updated_at = ?
                WHERE tenant = ? AND id = ?
                """,
                (
                    actual_cash,
                    difference,
                    erpnext_closing_id,
                    closed_at,
                    closed_at,
                    tenant,
                    shift_id,
                ),
            )
            connection.commit()
        return self.get_shift(tenant, shift_id) or current

    def list_open_held_receipts(self, tenant: str, shift_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pos_held_receipts
                WHERE tenant = ? AND shift_id = ? AND status = 'open'
                ORDER BY updated_at DESC
                """,
                (tenant, shift_id),
            ).fetchall()
        return [_dict(row) for row in rows if row is not None]

    def upsert_held_receipt(self, row: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_held_mutation_allowed(
                connection,
                tenant=str(row["tenant"]),
                shift_id=str(row["shift_id"]),
            )
            connection.execute(
                """
                INSERT INTO pos_held_receipts (
                    id, tenant, shift_id, label, lines_json, subtotal, discount_total,
                    grand_total, created_by_email, created_by_full_name, status,
                    created_at, updated_at
                )
                VALUES (
                    :id, :tenant, :shift_id, :label, :lines_json, :subtotal, :discount_total,
                    :grand_total, :created_by_email, :created_by_full_name, 'open',
                    :created_at, :updated_at
                )
                """,
                row,
            )
            connection.commit()
        return self.get_held_receipt(row["tenant"], row["id"]) or row

    def get_held_receipt(self, tenant: str, held_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pos_held_receipts WHERE tenant = ? AND id = ?",
                (tenant, held_id),
            ).fetchone()
        data = _dict(row)
        if data is None or data.get("status") != "open":
            return None
        return data

    def update_held_receipt(
        self, row: dict[str, Any], *, expected_updated_at: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM pos_held_receipts WHERE tenant = ? AND id = ? AND status = 'open'",
                (row["tenant"], row["id"]),
            ).fetchone()
            if existing is None:
                connection.rollback()
                raise POSStoreConflictError("HELD_RECEIPT_NOT_FOUND", "Отложенный чек не найден")
            current = _dict(existing)
            self._assert_held_mutation_allowed(
                connection,
                tenant=str(current["tenant"]),
                shift_id=str(current["shift_id"]),
            )
            if current["updated_at"] != expected_updated_at:
                connection.rollback()
                raise POSStoreConflictError("HELD_RECEIPT_CHANGED", "Отложенный чек изменился")
            connection.execute(
                """
                UPDATE pos_held_receipts
                SET label = :label, lines_json = :lines_json, subtotal = :subtotal,
                    discount_total = :discount_total, grand_total = :grand_total,
                    updated_at = :updated_at
                WHERE tenant = :tenant AND id = :id
                """,
                row,
            )
            connection.commit()
        return self.get_held_receipt(row["tenant"], row["id"]) or row

    def delete_held_receipt(self, tenant: str, held_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM pos_held_receipts "
                "WHERE tenant = ? AND id = ? AND status = 'open'",
                (tenant, held_id),
            ).fetchone()
            if existing is None:
                connection.commit()
                return
            current = _dict(existing)
            self._assert_held_mutation_allowed(
                connection,
                tenant=tenant,
                shift_id=str(current["shift_id"]),
            )
            connection.execute(
                """
                UPDATE pos_held_receipts
                SET status = 'deleted', updated_at = ?
                WHERE tenant = ? AND id = ? AND status = 'open'
                """,
                (_now(), tenant, held_id),
            )
            connection.commit()

    @staticmethod
    def _assert_held_mutation_allowed(
        connection: sqlite3.Connection,
        *,
        tenant: str,
        shift_id: str,
    ) -> None:
        shift = connection.execute(
            "SELECT status FROM pos_shifts WHERE tenant = ? AND id = ?",
            (tenant, shift_id),
        ).fetchone()
        if shift is None:
            raise POSStoreConflictError("SHIFT_NOT_FOUND", "Shift not found")
        if str(shift["status"]) != "open":
            raise POSStoreConflictError("SHIFT_CLOSED", "Shift is closed")
        active = connection.execute(
            """
            SELECT 1 FROM pos_operation_intents
            WHERE tenant = ? AND scope_id = ?
              AND state IN ('reserved', 'erp_pending', 'recovery_required', 'materialized')
            LIMIT 1
            """,
            (tenant, f"shift:{shift_id}"),
        ).fetchone()
        if active is not None:
            raise POSStoreConflictError(
                "SHIFT_CHANGED", "Another shift operation is in progress"
            )

    def complete_held_receipt(self, tenant: str, held_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE pos_held_receipts
                SET status = 'completed', updated_at = ?
                WHERE tenant = ? AND id = ? AND status = 'open'
                """,
                (_now(), tenant, held_id),
            )

    def create_sale(self, row: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            shift = connection.execute(
                "SELECT * FROM pos_shifts WHERE tenant = ? AND id = ?",
                (row["tenant"], row["shift_id"]),
            ).fetchone()
            if shift is None:
                connection.rollback()
                raise POSStoreConflictError("SHIFT_NOT_FOUND", "Смена не найдена")
            shift_data = _dict(shift)
            if shift_data["status"] != "open":
                connection.rollback()
                raise POSStoreConflictError("SHIFT_CLOSED", "Смена закрыта")
            connection.execute(
                """
                INSERT INTO pos_sales (
                    id, tenant, receipt_number, shift_id, register_id, register_name,
                    warehouse_id, warehouse_name, cashier_email, cashier_full_name,
                    lines_json, subtotal, discount_total, grand_total, cash_received,
                    change, erpnext_sales_invoice_id, created_at
                )
                VALUES (
                    :id, :tenant, :receipt_number, :shift_id, :register_id, :register_name,
                    :warehouse_id, :warehouse_name, :cashier_email, :cashier_full_name,
                    :lines_json, :subtotal, :discount_total, :grand_total, :cash_received,
                    :change, :erpnext_sales_invoice_id, :created_at
                )
                """,
                row,
            )
            sales_total = _money_add(shift_data["sales_total"], row["grand_total"])
            expected_cash = _money_add(shift_data["opening_cash"], sales_total)
            connection.execute(
                """
                UPDATE pos_shifts
                SET sales_total = ?, expected_cash = ?, updated_at = ?
                WHERE tenant = ? AND id = ? AND status = 'open'
                """,
                (sales_total, expected_cash, row["created_at"], row["tenant"], row["shift_id"]),
            )
            connection.commit()
        return self.get_sale(row["tenant"], row["id"]) or row

    def get_sale(self, tenant: str, sale_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pos_sales WHERE tenant = ? AND id = ?",
                (tenant, sale_id),
            ).fetchone()
        return _dict(row)

    def get_return(self, tenant: str, return_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pos_returns WHERE tenant = ? AND id = ?",
                (tenant, return_id),
            ).fetchone()
        return _dict(row)

    def get_return_by_idempotency(
        self, tenant: str, operation: str, user_email: str, key: str
    ) -> dict[str, Any] | None:
        if operation != "create_return":
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM pos_returns
                WHERE tenant = ? AND created_by_email = ? AND idempotency_key = ?
                """,
                (tenant, user_email, key),
            ).fetchone()
        return _dict(row)

    def create_pending_return(
        self,
        *,
        row: dict[str, Any],
        requested_lines: list[dict[str, str]],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sale = connection.execute(
                "SELECT * FROM pos_sales WHERE tenant = ? AND id = ?",
                (row["tenant"], row["sale_id"]),
            ).fetchone()
            if sale is None:
                connection.rollback()
                raise POSStoreConflictError("SALE_NOT_FOUND", "Продажа не найдена")

            pending = connection.execute(
                """
                SELECT id FROM pos_returns
                WHERE tenant = ? AND sale_id = ? AND state = 'pending_recovery'
                LIMIT 1
                """,
                (row["tenant"], row["sale_id"]),
            ).fetchone()
            if pending is not None:
                connection.rollback()
                raise POSStoreConflictError(
                    "RETURN_RECOVERY_REQUIRED",
                    "По продаже уже есть возврат, ожидающий recovery",
                    {"return_id": str(pending[0])},
                )

            sale_lines = json.loads(str(sale["lines_json"]))
            returned_by_line: dict[str, Decimal] = {}
            existing = connection.execute(
                """
                SELECT lines_json FROM pos_returns
                WHERE tenant = ? AND sale_id = ? AND state = 'submitted'
                """,
                (row["tenant"], row["sale_id"]),
            ).fetchall()
            for existing_row in existing:
                for line in json.loads(str(existing_row[0])):
                    line_id = str(line["line_id"])
                    returned_by_line[line_id] = returned_by_line.get(line_id, Decimal("0")) + (
                        Decimal(str(line["quantity"]))
                    )

            if sale_lines and all(
                Decimal(str(source["quantity"])) - returned_by_line.get(
                    _sale_line_id(str(row["sale_id"]), index), Decimal("0")
                )
                <= Decimal("0")
                for index, source in enumerate(sale_lines)
            ):
                connection.rollback()
                raise POSStoreConflictError(
                    "SALE_ALREADY_FULLY_RETURNED", "Продажа уже возвращена полностью"
                )

            snapshot: list[dict[str, str]] = []
            for requested in requested_lines:
                line_id = requested["line_id"]
                index = _sale_line_index(str(row["sale_id"]), line_id)
                if index is None or index >= len(sale_lines):
                    connection.rollback()
                    raise POSStoreConflictError(
                        "RETURN_LINE_NOT_FOUND", "Строка продажи не найдена", {"line_id": line_id}
                    )
                source = sale_lines[index]
                if line_id != _sale_line_id(str(row["sale_id"]), index):
                    connection.rollback()
                    raise POSStoreConflictError(
                        "RETURN_LINE_NOT_FOUND", "Строка продажи не найдена", {"line_id": line_id}
                    )
                requested_quantity = Decimal(requested["quantity"])
                sold_quantity = Decimal(str(source["quantity"]))
                net_unit_price = _net_unit_price(source)
                already_returned = returned_by_line.get(line_id, Decimal("0"))
                available = sold_quantity - already_returned
                if requested_quantity > available:
                    connection.rollback()
                    raise POSStoreConflictError(
                        "RETURN_QUANTITY_EXCEEDED",
                        "Количество возврата больше доступного",
                        {
                            "line_id": line_id,
                            "available_to_return_quantity": _format_quantity(available),
                        },
                    )
                snapshot.append(
                    {
                        "line_id": line_id,
                        "item_id": str(source["product_id"]),
                        "item_name": str(source["name"]),
                        "quantity": _format_quantity(requested_quantity),
                        "unit": str(source["unit"]),
                        "unit_price": _format_money(net_unit_price),
                        "line_total": _format_money(
                            requested_quantity * net_unit_price
                        ),
                    }
                )

            insert_row = {**row, "lines_json": json.dumps(snapshot, ensure_ascii=False)}
            connection.execute(
                """
                INSERT INTO pos_returns (
                    id, tenant, sale_id, receipt_number, return_receipt_number, state,
                    refund_method, reason, comment, register_id, shift_id, cashier_email,
                    currency, refund_total, lines_json, erpnext_return_invoice_id,
                    idempotency_key, created_by_email, created_at, cancelled_by,
                    cancelled_at, cancel_reason, cancel_comment, updated_at
                ) VALUES (
                    :id, :tenant, :sale_id, :receipt_number, :return_receipt_number, :state,
                    :refund_method, :reason, :comment, :register_id, :shift_id, :cashier_email,
                    :currency, :refund_total, :lines_json, :erpnext_return_invoice_id,
                    :idempotency_key, :created_by_email, :created_at,
                    NULL, NULL, NULL, NULL, :updated_at
                )
                """,
                insert_row,
            )
            connection.commit()
        return self.get_return(str(row["tenant"]), str(row["id"])) or insert_row

    def delete_pending_return(self, tenant: str, return_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM pos_returns "
                "WHERE tenant = ? AND id = ? AND state = 'pending_recovery'",
                (tenant, return_id),
            )

    def claim_return_cancel(self, tenant: str, return_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pos_returns WHERE tenant = ? AND id = ?",
                (tenant, return_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                return {}
            state = str(row["state"])
            if state == "cancel_pending":
                connection.rollback()
                raise POSStoreConflictError(
                    "RETURN_CANCEL_NOT_ALLOWED", "Отмена возврата уже выполняется"
                )
            if state != "submitted":
                connection.rollback()
                return dict(row)
            connection.execute(
                "UPDATE pos_returns SET state = 'cancel_pending', updated_at = ? "
                "WHERE tenant = ? AND id = ? AND state = 'submitted'",
                (_now(), tenant, return_id),
            )
            connection.commit()
        return self.get_return(tenant, return_id) or {}

    def mark_return_submitted(
        self, tenant: str, return_id: str, erpnext_invoice_id: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE pos_returns
                SET state = 'submitted', erpnext_return_invoice_id = ?,
                    return_receipt_number = ?, updated_at = ?
                WHERE tenant = ? AND id = ? AND state = 'pending_recovery'
                """,
                (erpnext_invoice_id, erpnext_invoice_id, _now(), tenant, return_id),
            )
        return self.get_return(tenant, return_id) or {}

    def mark_return_cancelled(
        self,
        *,
        tenant: str,
        return_id: str,
        cancelled_by: str,
        reason: str,
        comment: str | None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE pos_returns
                SET state = 'cancelled', cancelled_by = ?, cancelled_at = ?,
                    cancel_reason = ?, cancel_comment = ?, updated_at = ?
                WHERE tenant = ? AND id = ? AND state = 'cancel_pending'
                """,
                (
                    cancelled_by,
                    _now(),
                    reason,
                    comment,
                    _now(),
                    tenant,
                    return_id,
                ),
            )
        return self.get_return(tenant, return_id) or {}

    def release_return_cancel(self, tenant: str, return_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE pos_returns SET state = 'submitted', updated_at = ? "
                "WHERE tenant = ? AND id = ? AND state = 'cancel_pending'",
                (_now(), tenant, return_id),
            )

    def return_options(
        self, tenant: str, sale_id: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        sale = self.get_sale(tenant, sale_id)
        if sale is None:
            return None, []
        returned_by_line: dict[str, Decimal] = {}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT lines_json FROM pos_returns
                WHERE tenant = ? AND sale_id = ? AND state = 'submitted'
                """,
                (tenant, sale_id),
            ).fetchall()
        for row in rows:
            for line in json.loads(str(row[0])):
                line_id = str(line["line_id"])
                returned_by_line[line_id] = returned_by_line.get(line_id, Decimal("0")) + Decimal(
                    str(line["quantity"])
                )
        result: list[dict[str, Any]] = []
        for index, source in enumerate(json.loads(str(sale["lines_json"]))):
            line_id = _sale_line_id(sale_id, index)
            sold = Decimal(str(source["quantity"]))
            returned = returned_by_line.get(line_id, Decimal("0"))
            available = max(Decimal("0"), sold - returned)
            net_unit_price = _net_unit_price(source)
            result.append(
                {
                    "line_id": line_id,
                    "item_id": str(source["product_id"]),
                    "item_name": str(source["name"]),
                    "sold_quantity": _format_quantity(sold),
                    "already_returned_quantity": _format_quantity(returned),
                    "available_to_return_quantity": _format_quantity(available),
                    "unit": str(source["unit"]),
                    "unit_price": _format_money(net_unit_price),
                    "net_unit_price": _format_money(net_unit_price),
                    "line_total": _format_money(sold * net_unit_price),
                }
            )
        return sale, result

    def list_returns(
        self,
        *,
        tenant: str,
        cashier_email: str | None,
        q: str | None,
        sale_id: str | None,
        register_id: str | None,
        date_from: date | None,
        date_to: date | None,
        state: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        filters = ["tenant = ?"]
        params: list[Any] = [tenant]
        if cashier_email:
            filters.append("cashier_email = ?")
            params.append(cashier_email)
        if q and q.strip():
            pattern = f"%{q.strip()}%"
            filters.append(
                "(id LIKE ? OR sale_id LIKE ? OR receipt_number LIKE ? "
                "OR return_receipt_number LIKE ?)"
            )
            params.extend([pattern] * 4)
        if sale_id:
            filters.append("sale_id = ?")
            params.append(sale_id)
        if register_id:
            filters.append("register_id = ?")
            params.append(register_id)
        if date_from:
            filters.append("created_at >= ?")
            params.append(_date_start(date_from))
        if date_to:
            filters.append("created_at < ?")
            params.append(_date_start(date_to + timedelta(days=1)))
        if state:
            filters.append("state = ?")
            params.append(state)
        where = " AND ".join(filters)
        with self._connect() as connection:
            count = connection.execute(
                f"SELECT COUNT(*) FROM pos_returns WHERE {where}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM pos_returns WHERE {where} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [_dict(row) for row in rows if row is not None], int(count)

    def list_sales(
        self,
        *,
        tenant: str,
        cashier_email: str | None,
        register_id: str | None,
        q: str | None,
        date_from: date | None,
        date_to: date | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        filters = ["tenant = ?"]
        params: list[Any] = [tenant]
        if cashier_email:
            filters.append("cashier_email = ?")
            params.append(cashier_email)
        if register_id:
            filters.append("register_id = ?")
            params.append(register_id)
        if q:
            pattern = f"%{q.strip()}%"
            if pattern != "%%":
                filters.append(
                    "("
                    "id LIKE ? OR "
                    "receipt_number LIKE ? OR "
                    "cashier_email LIKE ? OR "
                    "register_id LIKE ? OR "
                    "register_name LIKE ?"
                    ")"
                )
                params.extend([pattern, pattern, pattern, pattern, pattern])
        if date_from:
            filters.append("created_at >= ?")
            params.append(_date_start(date_from))
        if date_to:
            filters.append("created_at < ?")
            params.append(_date_start(date_to + timedelta(days=1)))
        where = " AND ".join(filters)
        with self._connect() as connection:
            count = connection.execute(
                f"SELECT COUNT(*) FROM pos_sales WHERE {where}",
                params,
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM pos_sales WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [_dict(row) for row in rows if row is not None], int(count)

    def _ensure_schema(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pos_idempotency (
                    tenant TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    user_email TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    response_body TEXT NOT NULL,
                    lease_until TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant, operation, user_email, idempotency_key)
                )
                """
            )
            if "fencing_token" not in _table_columns(connection, "pos_idempotency"):
                connection.execute(
                    "ALTER TABLE pos_idempotency "
                    "ADD COLUMN fencing_token INTEGER NOT NULL DEFAULT 1"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pos_operation_intents (
                    id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    user_email TEXT NOT NULL,
                    business_hash TEXT NOT NULL,
                    external_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    lease_until TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    erpnext_document_id TEXT,
                    result_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS pos_operation_active_scope
                ON pos_operation_intents(tenant, scope_id)
                WHERE state IN ('reserved', 'erp_pending', 'recovery_required', 'materialized')
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS pos_operation_active_business
                ON pos_operation_intents(tenant, operation, user_email, business_hash)
                WHERE state IN ('reserved', 'erp_pending', 'recovery_required', 'materialized')
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS pos_operation_active_open_cashier
                ON pos_operation_intents(tenant, user_email)
                WHERE operation = 'open_shift'
                  AND state IN ('reserved', 'erp_pending', 'recovery_required', 'materialized')
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pos_shifts (
                    id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    register_id TEXT NOT NULL,
                    register_name TEXT NOT NULL,
                    warehouse_id TEXT NOT NULL,
                    warehouse_name TEXT NOT NULL,
                    cashier_email TEXT NOT NULL,
                    cashier_full_name TEXT,
                    status TEXT NOT NULL,
                    opening_cash TEXT NOT NULL,
                    sales_total TEXT NOT NULL,
                    expected_cash TEXT NOT NULL,
                    actual_cash TEXT,
                    difference TEXT,
                    erpnext_opening_id TEXT,
                    erpnext_closing_id TEXT,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS pos_open_shift_register "
                "ON pos_shifts(tenant, register_id) WHERE status = 'open'"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS pos_open_shift_cashier "
                "ON pos_shifts(tenant, cashier_email) WHERE status = 'open'"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pos_held_receipts (
                    id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    shift_id TEXT NOT NULL,
                    label TEXT,
                    lines_json TEXT NOT NULL,
                    subtotal TEXT NOT NULL,
                    discount_total TEXT NOT NULL,
                    grand_total TEXT NOT NULL,
                    created_by_email TEXT NOT NULL,
                    created_by_full_name TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pos_sales (
                    id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    receipt_number TEXT NOT NULL,
                    shift_id TEXT NOT NULL,
                    register_id TEXT NOT NULL,
                    register_name TEXT NOT NULL,
                    warehouse_id TEXT NOT NULL,
                    warehouse_name TEXT NOT NULL,
                    cashier_email TEXT NOT NULL,
                    cashier_full_name TEXT,
                    lines_json TEXT NOT NULL,
                    subtotal TEXT NOT NULL,
                    discount_total TEXT NOT NULL,
                    grand_total TEXT NOT NULL,
                    cash_received TEXT NOT NULL,
                    change TEXT NOT NULL,
                    erpnext_sales_invoice_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pos_returns (
                    id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    sale_id TEXT NOT NULL,
                    receipt_number TEXT NOT NULL,
                    return_receipt_number TEXT NOT NULL,
                    state TEXT NOT NULL,
                    refund_method TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    comment TEXT,
                    register_id TEXT NOT NULL,
                    shift_id TEXT NOT NULL,
                    cashier_email TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    refund_total TEXT NOT NULL,
                    lines_json TEXT NOT NULL,
                    erpnext_return_invoice_id TEXT,
                    idempotency_key TEXT NOT NULL,
                    created_by_email TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    cancelled_by TEXT,
                    cancelled_at TEXT,
                    cancel_reason TEXT,
                    cancel_comment TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant, created_by_email, idempotency_key)
                )
                """
            )
            duplicate_invoice = connection.execute(
                """
                SELECT 1 FROM pos_sales
                GROUP BY tenant, erpnext_sales_invoice_id
                HAVING COUNT(*) > 1
                LIMIT 1
                """
            ).fetchone()
            if duplicate_invoice is not None:
                raise POSStoreMigrationError(
                    "pos_sales contains duplicate ERPNext invoice ids; "
                    "manual reconciliation required"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS pos_sales_erpnext_invoice_unique
                ON pos_sales(tenant, erpnext_sales_invoice_id)
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS pos_returns_sale_state "
                "ON pos_returns(tenant, sale_id, state)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS pos_returns_created_at "
                "ON pos_returns(tenant, created_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _intent_payload(intent: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(str(intent["payload_json"]))
    if not isinstance(payload, dict):
        raise POSStoreConflictError("IDEMPOTENCY_CONFLICT", "Operation snapshot is invalid")
    return payload


def _record_from_row(status_code: object, response_body: object) -> POSIdempotencyRecord:
    body = json.loads(str(response_body))
    if not isinstance(body, dict):
        raise POSIdempotencyConflictError("Stored response is invalid")
    return POSIdempotencyRecord(status_code=int(status_code), response_body=body)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def _sale_line_id(sale_id: str, index: int) -> str:
    return f"{sale_id}:line:{index + 1}"


def _sale_line_index(sale_id: str, line_id: str) -> int | None:
    prefix = f"{sale_id}:line:"
    if not line_id.startswith(prefix):
        return None
    try:
        index = int(line_id[len(prefix) :]) - 1
    except ValueError:
        return None
    return index if index >= 0 else None


def _format_quantity(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.001')):.3f}"


def _format_money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def _net_unit_price(source: dict[str, Any]) -> Decimal:
    quantity = Decimal(str(source["quantity"]))
    total = Decimal(str(source.get("total") or source["unit_price"]))
    if not source.get("total"):
        total *= quantity
    if quantity <= Decimal("0"):
        return Decimal("0")
    return total / quantity


def _date_start(value: date) -> str:
    return datetime.combine(value, datetime_time.min, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _money_add(left: str, right: str) -> str:
    return (
        f"{(Decimal(left) + Decimal(right)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
    )
