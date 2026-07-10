import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any


class POSStoreConflictError(RuntimeError):
    def __init__(self, code: str, message: str, fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields or {}


class POSIdempotencyConflictError(RuntimeError):
    """Raised when an idempotency key is reused with another body."""


@dataclass(frozen=True)
class POSIdempotencyRecord:
    status_code: int
    response_body: dict[str, object]


@dataclass(frozen=True)
class POSIdempotencyBeginResult:
    acquired: bool
    record: POSIdempotencyRecord | None = None
    expired: bool = False


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
                SELECT request_hash, status, status_code, response_body, lease_until
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
                        status, status_code, response_body, lease_until, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'processing', 0, '{}', ?, ?, ?)
                    """,
                    (tenant, operation, user_email, key, request_hash, lease_until, now, now),
                )
                connection.commit()
                return POSIdempotencyBeginResult(acquired=True)

            if row[0] != request_hash:
                connection.rollback()
                raise POSIdempotencyConflictError("Idempotency key reused with another body")
            if row[1] == "completed":
                connection.commit()
                return POSIdempotencyBeginResult(
                    acquired=False,
                    record=_record_from_row(row[2], row[3]),
                )
            if _parse_timestamp(str(row[4])) <= time.time():
                connection.execute(
                    """
                    UPDATE pos_idempotency
                    SET lease_until = ?, updated_at = ?
                    WHERE tenant = ? AND operation = ? AND user_email = ? AND idempotency_key = ?
                    """,
                    (lease_until, now, tenant, operation, user_email, key),
                )
                connection.commit()
                return POSIdempotencyBeginResult(acquired=True, expired=True)
            connection.execute(
                """
                UPDATE pos_idempotency
                SET lease_until = ?, updated_at = ?
                WHERE tenant = ? AND operation = ? AND user_email = ? AND idempotency_key = ?
                """,
                (lease_until, now, tenant, operation, user_email, key),
            )
            connection.commit()
            return POSIdempotencyBeginResult(acquired=False)

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
        status_code: int,
        response_body: dict[str, object],
    ) -> None:
        encoded = json.dumps(
            response_body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE pos_idempotency
                SET status = 'completed', status_code = ?, response_body = ?, updated_at = ?
                WHERE tenant = ? AND operation = ? AND user_email = ? AND idempotency_key = ?
                  AND request_hash = ?
                """,
                (status_code, encoded, _now(), tenant, operation, user_email, key, request_hash),
            )

    def release_idempotency(
        self,
        *,
        tenant: str,
        operation: str,
        user_email: str,
        key: str,
        request_hash: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM pos_idempotency
                WHERE tenant = ? AND operation = ? AND user_email = ? AND idempotency_key = ?
                  AND request_hash = ? AND status = 'processing'
                """,
                (tenant, operation, user_email, key, request_hash),
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
            connection.execute(
                """
                UPDATE pos_held_receipts
                SET status = 'deleted', updated_at = ?
                WHERE tenant = ? AND id = ? AND status = 'open'
                """,
                (_now(), tenant, held_id),
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
                        "unit_price": str(source["unit_price"]),
                        "line_total": _format_money(
                            requested_quantity * Decimal(str(source["unit_price"]))
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
                WHERE tenant = ? AND id = ? AND state = 'submitted'
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
            result.append(
                {
                    "line_id": line_id,
                    "item_id": str(source["product_id"]),
                    "item_name": str(source["name"]),
                    "sold_quantity": _format_quantity(sold),
                    "already_returned_quantity": _format_quantity(returned),
                    "available_to_return_quantity": _format_quantity(available),
                    "unit": str(source["unit"]),
                    "unit_price": str(source["unit_price"]),
                    "line_total": _format_money(sold * Decimal(str(source["unit_price"]))),
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant, operation, user_email, idempotency_key)
                )
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


def _date_start(value: date) -> str:
    return datetime.combine(value, datetime_time.min, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _money_add(left: str, right: str) -> str:
    return (
        f"{(Decimal(left) + Decimal(right)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
    )
