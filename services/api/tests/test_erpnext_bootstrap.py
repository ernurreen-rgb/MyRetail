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
    assert "cancel = 1" in stock_entry_block
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
