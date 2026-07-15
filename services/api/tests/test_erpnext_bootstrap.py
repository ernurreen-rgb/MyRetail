from pathlib import Path


def test_bootstrap_api_user_grants_minimal_stock_permissions() -> None:
    root = Path(__file__).resolve().parents[3]
    script = (root / "infra/erpnext/scripts/bootstrap-api-user.ps1").read_text(
        encoding="utf-8"
    )

    for doctype in [
        "Supplier",
        "Warehouse",
        "Bin",
        "Stock Entry",
        "Stock Entry Detail",
        "Purchase Receipt",
        "Purchase Receipt Item",
        "Comment",
        "Item",
        "Item Barcode",
        "Item Price",
        "POS Profile",
        "POS Opening Entry",
        "POS Opening Entry Detail",
        "POS Closing Entry",
        "POS Closing Entry Detail",
        "POS Closing Entry Taxes",
        "Sales Invoice",
        "Sales Invoice Item",
        "Sales Invoice Payment",
    ]:
        assert f'parent = "{doctype}"' in script

    stock_entry_block = script[
        script.index('parent = "Stock Entry"') : script.index(
            'parent = "Stock Entry Detail"'
        )
    ]
    assert "create = 1" in stock_entry_block
    assert "write = 1" in stock_entry_block
    assert "submit = 1" in stock_entry_block
    assert "cancel = 1" not in stock_entry_block
    assert '"submit"' in script
    assert '"cancel"' in script

    purchase_receipt_block = script[
        script.index('parent = "Purchase Receipt"') : script.index(
            'parent = "Purchase Receipt Item"'
        )
    ]
    assert "create = 1" in purchase_receipt_block
    assert "write = 1" in purchase_receipt_block
    assert "submit = 1" in purchase_receipt_block
    assert "cancel = 1" in purchase_receipt_block

    for fieldname in [
        "myretail_stock_idempotency_key",
        "myretail_purchase_idempotency_key",
        "myretail_sale_idempotency_key",
        "myretail_open_idempotency_key",
        "myretail_close_idempotency_key",
        "myretail_shift_id",
        "myretail_register_id",
        "myretail_cashier_email",
    ]:
        assert fieldname in script
    assert '"MYRETAIL_ERPNEXT_API_USER=$serviceUser"' in script
    assert "MYRETAIL_ERPNEXT_POS_USER" in script
    assert "MYRETAIL_ERPNEXT_POS_USER_MAP" in script
    assert "MYRETAIL_ERPNEXT_POS_CREDENTIALS_MAP" in script
    assert "MYRETAIL_POS_CASHIER_ASSIGNMENTS" in script
    assert "Ensure-ErpUser" in script
    assert "Get-ProfileUserEmail" in script
    assert "myretail-pos-$hash@local.test" in script
    assert "POS%20Profile?fields=" in script
    assert "applicable_for_users" in script
    assert "generate_keys" in script
    assert "$posCredentialMap" in script


def test_bootstrap_creates_explicit_myretail_admin_marker_role() -> None:
    root = Path(__file__).resolve().parents[3]
    script = (root / "infra/erpnext/scripts/bootstrap-api-user.ps1").read_text(
        encoding="utf-8"
    )

    assert '$myRetailAdminRole = "MyRetail Admin"' in script
    assert "function Ensure-ErpRole" in script
    assert "Ensure-ErpRole -RoleName $serviceRole -DeskAccess 0" in script
    assert "Ensure-ErpRole -RoleName $myRetailAdminRole -DeskAccess 0" in script


def test_stock_qa_data_scripts_seed_balances_and_reservation() -> None:
    root = Path(__file__).resolve().parents[3]
    scripts_dir = root / "infra/erpnext/scripts"
    wrapper = (scripts_dir / "setup-stock-qa-data.ps1").read_text(
        encoding="utf-8"
    )
    implementation = (scripts_dir / "setup-stock-qa-data.py").read_text(
        encoding="utf-8"
    )

    assert "setup-stock-qa-data.py" in wrapper
    assert "--env-file" in wrapper

    for item_code in ["QA-MILK-001", "QA-BREAD-001", "QA-CHEESE-001", "QA-ZERO-001"]:
        assert item_code in implementation
    assert '"Bin"' in implementation
    assert '"Stock Reconciliation"' in implementation
    assert "time.sleep" in implementation
    assert '"Sales Order"' in implementation
    assert "MYRETAIL-QA-RESERVATION" in implementation
    assert '"DELETE"' not in implementation
