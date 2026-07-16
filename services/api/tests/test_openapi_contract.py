import hashlib
import json
from pathlib import Path

from myretail_api.config import Settings
from myretail_api.main import create_app


def test_openapi_contract_matches_approved_fingerprint() -> None:
    schema = create_app(Settings(_env_file=None, environment="test")).openapi()
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    actual = hashlib.sha256(canonical).hexdigest()
    expected = (Path(__file__).parents[1] / "openapi.sha256").read_text().strip()

    assert actual == expected
