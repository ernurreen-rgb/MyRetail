from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


COMPANY = "MyRetail Demo"
MAIN_WAREHOUSE_NAME = "Основной склад QA"
RESERVE_WAREHOUSE_NAME = "Резервный склад QA"
RESERVATION_MARKER = "MYRETAIL-QA-RESERVATION"

ITEMS = (
    {
        "code": "QA-MILK-001",
        "name": "Молоко 3,2%",
        "uom": "Nos",
        "barcode": "4870000000011",
    },
    {
        "code": "QA-BREAD-001",
        "name": "Хлеб пшеничный",
        "uom": "Nos",
        "barcode": "4870000000028",
    },
    {
        "code": "QA-CHEESE-001",
        "name": "Сыр весовой",
        "uom": "Kg",
        "barcode": "4870000000035",
    },
    {
        "code": "QA-ZERO-001",
        "name": "Товар без остатка",
        "uom": "Nos",
        "barcode": "4870000000042",
    },
)


def read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise RuntimeError(f"Environment file not found: {path}")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


class ERPNextClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(
                json_body,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        elif form_body is not None:
            body = urlencode(form_body).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                payload = response.read()
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"ERPNext request failed: {method} {path} -> {exc.code}: {details}"
            ) from exc

        if not payload:
            return {}
        decoded = json.loads(payload.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise RuntimeError(f"ERPNext returned an invalid response for {method} {path}")
        return decoded

    def login(self, username: str, password: str) -> None:
        self.request(
            "POST",
            "/api/method/login",
            form_body={"usr": username, "pwd": password},
        )

    def get_document(self, doctype: str, name: str) -> dict[str, Any] | None:
        path = f"/api/resource/{quote(doctype, safe='')}/{quote(name, safe='')}"
        try:
            data = self.request("GET", path).get("data")
        except RuntimeError as exc:
            if "-> 404:" in str(exc):
                return None
            raise
        return data if isinstance(data, dict) else None

    def list_documents(
        self,
        doctype: str,
        *,
        fields: list[str],
        filters: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = urlencode(
            {
                "fields": json.dumps(fields, separators=(",", ":")),
                "filters": json.dumps(filters or {}, separators=(",", ":")),
                "limit_page_length": str(limit),
            }
        )
        payload = self.request(
            "GET",
            f"/api/resource/{quote(doctype, safe='')}?{query}",
        ).get("data")
        if not isinstance(payload, list):
            raise RuntimeError(f"ERPNext returned an invalid {doctype} list")
        return [row for row in payload if isinstance(row, dict)]

    def create_document(self, doctype: str, document: dict[str, Any]) -> dict[str, Any]:
        payload = self.request(
            "POST",
            f"/api/resource/{quote(doctype, safe='')}",
            json_body=document,
        ).get("data")
        if not isinstance(payload, dict):
            raise RuntimeError(f"ERPNext did not return the created {doctype}")
        return payload

    def submit_document(self, document: dict[str, Any]) -> dict[str, Any]:
        payload = self.request(
            "POST",
            "/api/method/frappe.client.submit",
            form_body={
                "doc": json.dumps(
                    document,
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
            },
        ).get("message")
        if not isinstance(payload, dict):
            raise RuntimeError("ERPNext did not return the submitted document")
        return payload

def ensure_warehouse(
    client: ERPNextClient,
    *,
    name: str,
    warehouse_id: str,
    company: str,
    parent_warehouse: str,
) -> None:
    if client.get_document("Warehouse", warehouse_id) is not None:
        return
    client.create_document(
        "Warehouse",
        {
            "warehouse_name": name,
            "company": company,
            "parent_warehouse": parent_warehouse,
            "is_group": 0,
            "disabled": 0,
        },
    )


def ensure_item(
    client: ERPNextClient,
    *,
    definition: dict[str, str],
    item_group: str,
) -> None:
    if client.get_document("Item", definition["code"]) is not None:
        return
    client.create_document(
        "Item",
        {
            "item_code": definition["code"],
            "item_name": definition["name"],
            "description": "Тестовые данные MyRetail Sprint 3",
            "item_group": item_group,
            "stock_uom": definition["uom"],
            "is_stock_item": 1,
            "disabled": 0,
            "barcodes": [{"barcode": definition["barcode"]}],
        },
    )


def actual_quantity(client: ERPNextClient, item_code: str, warehouse: str) -> Decimal:
    rows = client.list_documents(
        "Bin",
        fields=["actual_qty"],
        filters={"item_code": item_code, "warehouse": warehouse},
        limit=1,
    )
    if not rows:
        return Decimal("0")
    return Decimal(str(rows[0].get("actual_qty") or "0"))


def ensure_stock_baseline(
    client: ERPNextClient,
    *,
    company: str,
    expense_account: str,
    targets: list[dict[str, str]],
) -> None:
    items: list[dict[str, str]] = []
    for target in targets:
        current = actual_quantity(client, target["item_code"], target["warehouse"])
        desired = Decimal(target["quantity"])
        if current != desired:
            items.append(
                {
                    "item_code": target["item_code"],
                    "warehouse": target["warehouse"],
                    "qty": target["quantity"],
                    "valuation_rate": target["valuation_rate"],
                }
            )

    if not items:
        return

    now = datetime.now()
    draft = client.create_document(
        "Stock Reconciliation",
        {
            "company": company,
            "purpose": "Stock Reconciliation",
            "expense_account": expense_account,
            "posting_date": now.date().isoformat(),
            "posting_time": now.strftime("%H:%M:%S"),
            "set_posting_time": 1,
            "items": items,
        },
    )
    client.submit_document(draft)


def ensure_customer(client: ERPNextClient, name: str) -> None:
    if client.get_document("Customer", name) is not None:
        return
    client.create_document(
        "Customer",
        {
            "customer_name": name,
            "customer_type": "Individual",
        },
    )


def ensure_reservation(
    client: ERPNextClient,
    *,
    company: str,
    warehouse: str,
    customer: str,
) -> str:
    existing = client.list_documents(
        "Sales Order",
        fields=["name", "docstatus"],
        filters={"po_no": RESERVATION_MARKER, "docstatus": 1},
        limit=1,
    )
    if existing:
        return str(existing[0]["name"])

    today = date.today()
    delivery_date = today + timedelta(days=30)
    draft = client.create_document(
        "Sales Order",
        {
            "customer": customer,
            "company": company,
            "transaction_date": today.isoformat(),
            "delivery_date": delivery_date.isoformat(),
            "po_no": RESERVATION_MARKER,
            "currency": "KZT",
            "selling_price_list": "Standard Selling",
            "items": [
                {
                    "item_code": "QA-MILK-001",
                    "delivery_date": delivery_date.isoformat(),
                    "qty": "2.000",
                    "rate": "550.00",
                    "warehouse": warehouse,
                }
            ],
        },
    )
    submitted = client.submit_document(draft)
    return str(submitted["name"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    arguments = parser.parse_args()

    environment = read_dotenv(arguments.env_file)
    for required in ("SITE_NAME", "HTTP_PORT", "ADMIN_PASSWORD"):
        if not environment.get(required):
            raise RuntimeError(f"Required variable {required} is missing")

    base_url = f"http://{environment['SITE_NAME']}:{environment['HTTP_PORT']}"
    client = ERPNextClient(base_url)
    client.login("Administrator", environment["ADMIN_PASSWORD"])

    company = client.get_document("Company", COMPANY)
    if company is None:
        raise RuntimeError(
            f"Company '{COMPANY}' is missing. Run setup-local-demo.ps1 first."
        )

    company_abbr = str(company["abbr"])
    parent_warehouse = f"All Warehouses - {company_abbr}"
    main_warehouse = f"{MAIN_WAREHOUSE_NAME} - {company_abbr}"
    reserve_warehouse = f"{RESERVE_WAREHOUSE_NAME} - {company_abbr}"

    ensure_warehouse(
        client,
        name=MAIN_WAREHOUSE_NAME,
        warehouse_id=main_warehouse,
        company=COMPANY,
        parent_warehouse=parent_warehouse,
    )
    ensure_warehouse(
        client,
        name=RESERVE_WAREHOUSE_NAME,
        warehouse_id=reserve_warehouse,
        company=COMPANY,
        parent_warehouse=parent_warehouse,
    )

    groups = client.list_documents(
        "Item Group",
        fields=["name"],
        filters={"is_group": 0},
        limit=1,
    )
    if not groups:
        raise RuntimeError("ERPNext has no leaf Item Group")
    item_group = str(groups[0]["name"])
    for definition in ITEMS:
        ensure_item(client, definition=definition, item_group=item_group)

    targets = [
        {
            "item_code": "QA-MILK-001",
            "warehouse": main_warehouse,
            "quantity": "10.000",
            "valuation_rate": "400.00",
        },
        {
            "item_code": "QA-MILK-001",
            "warehouse": reserve_warehouse,
            "quantity": "2.000",
            "valuation_rate": "400.00",
        },
        {
            "item_code": "QA-BREAD-001",
            "warehouse": main_warehouse,
            "quantity": "5.000",
            "valuation_rate": "180.00",
        },
        {
            "item_code": "QA-BREAD-001",
            "warehouse": reserve_warehouse,
            "quantity": "0.000",
            "valuation_rate": "180.00",
        },
        {
            "item_code": "QA-CHEESE-001",
            "warehouse": main_warehouse,
            "quantity": "12.500",
            "valuation_rate": "2500.00",
        },
        {
            "item_code": "QA-CHEESE-001",
            "warehouse": reserve_warehouse,
            "quantity": "1.250",
            "valuation_rate": "2500.00",
        },
        {
            "item_code": "QA-ZERO-001",
            "warehouse": main_warehouse,
            "quantity": "0.000",
            "valuation_rate": "100.00",
        },
    ]
    has_stock_ledger = bool(
        client.list_documents(
            "Stock Ledger Entry",
            fields=["name"],
            limit=1,
        )
    )
    account_filters: dict[str, Any] = {
        "company": COMPANY,
        "is_group": 0,
        "disabled": 0,
    }
    account_filters["report_type"] = (
        "Profit and Loss" if has_stock_ledger else "Balance Sheet"
    )
    difference_accounts = client.list_documents(
        "Account",
        fields=["name", "account_type", "report_type", "root_type"],
        filters=account_filters,
        limit=500,
    )
    if not difference_accounts:
        raise RuntimeError("ERPNext has no difference account for stock reconciliation")
    difference_accounts.sort(
        key=lambda account: (
            str(account.get("account_type") or "")
            not in (("Stock Adjustment",) if has_stock_ledger else ("Temporary",)),
            str(account.get("name") or ""),
        )
    )
    expense_account = str(difference_accounts[0]["name"])
    ensure_stock_baseline(
        client,
        company=COMPANY,
        expense_account=expense_account,
        targets=targets,
    )

    customer = "Покупатель QA MyRetail"
    ensure_customer(client, customer)
    sales_order = ensure_reservation(
        client,
        company=COMPANY,
        warehouse=main_warehouse,
        customer=customer,
    )

    rows: list[dict[str, Any]] = []
    for target in targets:
        bins = client.list_documents(
            "Bin",
            fields=["item_code", "warehouse", "actual_qty", "reserved_qty"],
            filters={
                "item_code": target["item_code"],
                "warehouse": target["warehouse"],
            },
            limit=1,
        )
        rows.extend(bins)

    print("MyRetail Sprint 3 QA data are ready.")
    print(f"Reservation sales order: {sales_order}")
    for row in sorted(
        rows,
        key=lambda value: (str(value["warehouse"]), str(value["item_code"])),
    ):
        print(
            f"{row['item_code']} | {row['warehouse']} | "
            f"actual={Decimal(str(row.get('actual_qty') or 0)):.3f} | "
            f"reserved={Decimal(str(row.get('reserved_qty') or 0)):.3f}"
        )


if __name__ == "__main__":
    main()
