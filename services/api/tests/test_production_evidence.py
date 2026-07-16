from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from myretail_api.state.schema import EXPECTED_STATE_SCHEMA_REVISION

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPOSITORY_ROOT / "services" / "api" / "scripts" / "validate_production_evidence.py"
EXAMPLE_PATH = REPOSITORY_ROOT / "docs" / "security" / "production-evidence.example.json"

SPEC = importlib.util.spec_from_file_location("validate_production_evidence", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
evidence = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evidence
SPEC.loader.exec_module(evidence)


def example_manifest() -> dict[str, Any]:
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def replace_example_urls(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(nested, str) and nested.startswith("https://"):
                value[key] = nested.replace("example.invalid", "evidence.myretail.app")
            else:
                replace_example_urls(nested)
    elif isinstance(value, list):
        for nested in value:
            replace_example_urls(nested)


def production_manifest() -> dict[str, Any]:
    manifest = copy.deepcopy(example_manifest())
    replace_example_urls(manifest)
    manifest["release"]["commit_sha"] = "1" * 40
    manifest["release"]["api_image_digest"] = f"sha256:{'2' * 64}"
    manifest["release"]["migration_image_digest"] = f"sha256:{'3' * 64}"
    manifest["cutover"]["traffic_decision"] = "approved"
    return manifest


def test_validator_revision_matches_package_head() -> None:
    assert evidence.EXPECTED_MIGRATION_REVISION == EXPECTED_STATE_SCHEMA_REVISION


def test_committed_example_is_valid_only_as_blocked_template() -> None:
    evidence.validate_manifest(example_manifest(), mode="template")
    with pytest.raises(evidence.EvidenceError) as caught:
        evidence.validate_manifest(example_manifest())
    assert "cutover.traffic_decision.invalid" in caught.value.paths
    assert any(path.endswith(".placeholder_forbidden") for path in caught.value.paths)


def test_complete_production_manifest_passes() -> None:
    evidence.validate_manifest(production_manifest())


def test_schema_version_rejects_boolean_substitution() -> None:
    manifest = production_manifest()
    manifest["schema_version"] = True
    with pytest.raises(evidence.EvidenceError) as caught:
        evidence.validate_manifest(manifest)
    assert "schema_version.invalid" in caught.value.paths


def test_missing_and_unknown_fields_fail_closed_without_reporting_unknown_key() -> None:
    manifest = production_manifest()
    del manifest["managed_postgresql"]["high_availability"]
    manifest["managed_postgresql"]["unexpected-provider-payload"] = "opaque"
    with pytest.raises(evidence.EvidenceError) as caught:
        evidence.validate_manifest(manifest)
    assert "managed_postgresql.high_availability.missing" in caught.value.paths
    assert "managed_postgresql.unknown_field" in caught.value.paths
    assert all("unexpected-provider-payload" not in path for path in caught.value.paths)


def test_secret_material_is_rejected_and_never_reflected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = production_manifest()
    secret = "postgresql://operator:actual-password@db.internal/myretail"
    manifest["secret_manager"]["reference"] = secret
    with pytest.raises(evidence.EvidenceError) as caught:
        evidence.validate_manifest(manifest)
    assert "manifest.secret_material_forbidden" in caught.value.paths
    assert secret not in str(caught.value)
    assert all(secret not in path for path in caught.value.paths)

    result = evidence.main([str(EXAMPLE_PATH)])
    output = capsys.readouterr()
    assert result == 1
    assert "0000000000000000000000000000000000000000" not in output.err


def test_signed_or_fragmented_evidence_url_is_rejected() -> None:
    manifest = production_manifest()
    manifest["backup"]["evidence_url"] = "https://evidence.myretail.app/backup?token=secret"
    with pytest.raises(evidence.EvidenceError) as caught:
        evidence.validate_manifest(manifest)
    assert "backup.evidence_url.invalid_https_evidence_url" in caught.value.paths


def test_malformed_evidence_url_fails_without_crashing() -> None:
    manifest = production_manifest()
    manifest["backup"]["evidence_url"] = "https://[invalid"
    with pytest.raises(evidence.EvidenceError) as caught:
        evidence.validate_manifest(manifest)
    assert "backup.evidence_url.invalid_https_evidence_url" in caught.value.paths


def test_opaque_secret_cannot_be_used_as_secret_manager_reference() -> None:
    manifest = production_manifest()
    manifest["secret_manager"]["reference"] = "thiscouldbearawcredentialvalue"
    with pytest.raises(evidence.EvidenceError) as caught:
        evidence.validate_manifest(manifest)
    assert "secret_manager.reference.invalid" in caught.value.paths


def test_required_alerts_and_smoke_scenarios_cannot_be_attested_partially() -> None:
    manifest = production_manifest()
    manifest["monitoring"]["alerts"].pop()
    manifest["smoke"]["scenarios"].pop()
    with pytest.raises(evidence.EvidenceError) as caught:
        evidence.validate_manifest(manifest)
    assert "monitoring.alerts.required_kinds_missing" in caught.value.paths
    assert "smoke.scenarios.required_kinds_missing" in caught.value.paths


def test_events_after_capture_and_non_isolated_restore_are_rejected() -> None:
    manifest = production_manifest()
    manifest["backup"]["restore"]["completed_at"] = "2026-07-16T13:06:00Z"
    manifest["backup"]["restore"]["target_cluster_id"] = manifest["managed_postgresql"][
        "cluster_id"
    ]
    with pytest.raises(evidence.EvidenceError) as caught:
        evidence.validate_manifest(manifest)
    assert "backup.restore.completed_at.after_capture" in caught.value.paths
    assert "backup.restore.target_cluster_id.not_isolated" in caught.value.paths


def test_boolean_attestations_reject_integer_substitution() -> None:
    manifest = production_manifest()
    manifest["managed_postgresql"]["high_availability"] = 1
    with pytest.raises(evidence.EvidenceError) as caught:
        evidence.validate_manifest(manifest)
    assert "managed_postgresql.high_availability.invalid" in caught.value.paths


def test_duplicate_json_keys_are_rejected_without_echo(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
    with pytest.raises(evidence.EvidenceError) as caught:
        evidence.load_manifest(path)
    assert caught.value.paths == ("document.duplicate_key",)


def test_non_standard_json_constant_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "nan.json"
    path.write_text('{"schema_version": NaN}', encoding="utf-8")
    with pytest.raises(evidence.EvidenceError) as caught:
        evidence.load_manifest(path)
    assert caught.value.paths == ("document.invalid_json",)


def test_invalid_validation_mode_is_programming_error() -> None:
    with pytest.raises(ValueError, match="Unsupported validation mode"):
        evidence.validate_manifest(production_manifest(), mode="permissive")
