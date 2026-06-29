from pathlib import Path


def test_bootstrap_api_user_grants_minimal_stock_permissions() -> None:
    root = Path(__file__).resolve().parents[3]
    script = (root / "infra/erpnext/scripts/bootstrap-api-user.ps1").read_text(
        encoding="utf-8"
    )

    for doctype in [
        "Warehouse",
        "Bin",
        "Stock Entry",
        "Stock Entry Detail",
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
    assert '"submit"' in script


def test_stock_qa_data_script_seeds_item_bin_and_stock_entry() -> None:
    root = Path(__file__).resolve().parents[3]
    script = (root / "infra/erpnext/scripts/setup-stock-qa-data.ps1").read_text(
        encoding="utf-8"
    )

    for item_code in ["QA-MILK-001", "QA-BREAD-001", "QA-CHEESE-001", "QA-ZERO-001"]:
        assert item_code in script
    assert "Bin" in script
    assert "Stock%20Entry" in script
    assert "Sales%20Order" in script
    assert "MYRETAIL-QA-RESERVATION" in script
    assert "Material Receipt" in script
