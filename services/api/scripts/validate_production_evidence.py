from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

EXPECTED_MIGRATION_REVISION = "20260716_05"
MAX_MANIFEST_BYTES = 256 * 1024
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SECRET_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9._@:/-]+$")
SECRET_PATTERNS = (
    re.compile(r"postgres(?:ql)?(?:\+asyncpg)?://", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:password|passwd|pwd|token|api[_-]?key|api[_-]?secret)\s*=", re.IGNORECASE),
)
ALERT_KINDS = {
    "backup_failure",
    "database_unavailable",
    "lock_timeout",
    "migration_mismatch",
    "pool_saturation",
    "recovery_age",
    "replication_lag",
    "statement_timeout",
}
SMOKE_SCENARIOS = {
    "pos_lifecycle",
    "restart_recovery",
    "session_logout_revoke",
    "stock_purchase_exact_once",
}
RESERVED_EVIDENCE_HOSTS = {"example.com", "example.net", "example.org"}


class EvidenceError(RuntimeError):
    """A validation failure containing only safe, schema-owned field paths."""

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = tuple(sorted(set(paths)))
        super().__init__("Production evidence manifest is invalid")


class DuplicateKeyError(ValueError):
    """Raised without retaining or reporting the duplicate input key."""


class InvalidConstantError(ValueError):
    """Raised for non-standard JSON constants such as NaN or Infinity."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError from None
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise InvalidConstantError from None


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw_bytes = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw_bytes) > MAX_MANIFEST_BYTES:
            raise EvidenceError(("document.unavailable_or_too_large",))
        raw = raw_bytes.decode("utf-8")
    except (OSError, UnicodeError):
        raise EvidenceError(("document.unavailable_or_invalid_encoding",)) from None

    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except DuplicateKeyError:
        raise EvidenceError(("document.duplicate_key",)) from None
    except (InvalidConstantError, json.JSONDecodeError, RecursionError):
        raise EvidenceError(("document.invalid_json",)) from None
    if not isinstance(value, dict):
        raise EvidenceError(("document.invalid_root",))
    return value


class Validator:
    def __init__(self, *, mode: str) -> None:
        self.mode = mode
        self.errors: set[str] = set()
        self.event_times: dict[str, datetime] = {}

    def validate(self, manifest: Mapping[str, Any]) -> None:
        self._scan_for_secret_material(manifest)
        self._keys(
            manifest,
            "manifest",
            {
                "schema_version",
                "release",
                "managed_postgresql",
                "secret_manager",
                "backup",
                "monitoring",
                "smoke",
                "cutover",
                "provenance",
            },
        )
        if type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1:
            self.errors.add("schema_version.invalid")

        release = self._object(manifest.get("release"), "release")
        database = self._object(manifest.get("managed_postgresql"), "managed_postgresql")
        secret_manager = self._object(manifest.get("secret_manager"), "secret_manager")
        backup = self._object(manifest.get("backup"), "backup")
        monitoring = self._object(manifest.get("monitoring"), "monitoring")
        smoke = self._object(manifest.get("smoke"), "smoke")
        cutover = self._object(manifest.get("cutover"), "cutover")
        provenance = self._object(manifest.get("provenance"), "provenance")

        if release is not None:
            self._release(release)
        cluster_id = None
        if database is not None:
            cluster_id = self._database(database)
        if secret_manager is not None:
            self._secret_manager(secret_manager)
        if backup is not None:
            self._backup(backup, cluster_id=cluster_id)
        if monitoring is not None:
            self._monitoring(monitoring)
        if smoke is not None:
            self._smoke(smoke)
        if cutover is not None:
            self._cutover(cutover)
        captured_at = None
        if provenance is not None:
            captured_at = self._provenance(provenance)
        if captured_at is not None:
            for path, event_time in self.event_times.items():
                if event_time > captured_at:
                    self.errors.add(f"{path}.after_capture")

        if self.errors:
            raise EvidenceError(tuple(self.errors))

    def _scan_for_secret_material(self, value: Any) -> None:
        if isinstance(value, str):
            if any(pattern.search(value) for pattern in SECRET_PATTERNS):
                self.errors.add("manifest.secret_material_forbidden")
            return
        if isinstance(value, Mapping):
            for nested in value.values():
                self._scan_for_secret_material(nested)
            return
        if isinstance(value, list):
            for nested in value:
                self._scan_for_secret_material(nested)

    def _keys(self, value: Mapping[str, Any], path: str, expected: set[str]) -> None:
        for key in expected - set(value):
            self.errors.add(f"{path}.{key}.missing")
        if set(value) - expected:
            self.errors.add(f"{path}.unknown_field")

    def _object(self, value: Any, path: str) -> Mapping[str, Any] | None:
        if not isinstance(value, dict):
            self.errors.add(f"{path}.invalid_object")
            return None
        return value

    def _string(self, value: Any, path: str, *, max_length: int = 256) -> str | None:
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or len(value) > max_length
            or any(ord(character) < 32 for character in value)
        ):
            self.errors.add(f"{path}.invalid")
            return None
        return value

    def _integer(self, value: Any, path: str, *, minimum: int) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            self.errors.add(f"{path}.invalid")
            return None
        return value

    def _equals(self, value: Any, expected: Any, path: str) -> None:
        if type(value) is not type(expected) or value != expected:
            self.errors.add(f"{path}.invalid")

    def _timestamp(self, value: Any, path: str, *, event: bool = True) -> datetime | None:
        text = self._string(value, path)
        if text is None:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (OverflowError, ValueError):
            self.errors.add(f"{path}.invalid")
            return None
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
            self.errors.add(f"{path}.not_utc")
            return None
        parsed = parsed.astimezone(UTC)
        if event:
            self.event_times[path] = parsed
        return parsed

    def _url(self, value: Any, path: str) -> str | None:
        text = self._string(value, path, max_length=2048)
        if text is None:
            return None
        try:
            parsed = urlsplit(text)
            hostname = parsed.hostname
            username = parsed.username
            password = parsed.password
        except ValueError:
            self.errors.add(f"{path}.invalid_https_evidence_url")
            return None
        if (
            parsed.scheme != "https"
            or not hostname
            or "\\" in text
            or username is not None
            or password is not None
            or parsed.query
            or parsed.fragment
        ):
            self.errors.add(f"{path}.invalid_https_evidence_url")
            return None
        hostname = hostname.lower()
        if self.mode == "production" and (
            hostname in RESERVED_EVIDENCE_HOSTS
            or hostname.endswith((".example", ".invalid", ".test"))
        ):
            self.errors.add(f"{path}.placeholder_forbidden")
        return text

    def _release(self, value: Mapping[str, Any]) -> None:
        self._keys(
            value,
            "release",
            {
                "commit_sha",
                "api_image_digest",
                "migration_image_digest",
                "migration_revision",
                "evidence_url",
            },
        )
        commit = self._string(value.get("commit_sha"), "release.commit_sha")
        if commit is not None and (
            not SHA_PATTERN.fullmatch(commit)
            or (self.mode == "production" and commit == "0" * 40)
        ):
            self.errors.add("release.commit_sha.invalid")
        for field in ("api_image_digest", "migration_image_digest"):
            digest = self._string(value.get(field), f"release.{field}")
            if digest is not None and (
                not DIGEST_PATTERN.fullmatch(digest)
                or (self.mode == "production" and digest == f"sha256:{'0' * 64}")
            ):
                self.errors.add(f"release.{field}.invalid")
        if value.get("migration_revision") != EXPECTED_MIGRATION_REVISION:
            self.errors.add("release.migration_revision.invalid")
        self._url(value.get("evidence_url"), "release.evidence_url")

    def _database(self, value: Mapping[str, Any]) -> str | None:
        self._keys(
            value,
            "managed_postgresql",
            {
                "provider",
                "project_id",
                "cluster_id",
                "region",
                "tls_mode",
                "encryption_at_rest",
                "high_availability",
                "roles_preprovisioned",
                "evidence_url",
            },
        )
        for field in ("provider", "project_id", "region"):
            self._string(value.get(field), f"managed_postgresql.{field}")
        cluster_id = self._string(value.get("cluster_id"), "managed_postgresql.cluster_id")
        self._equals(value.get("tls_mode"), "verify-full", "managed_postgresql.tls_mode")
        for field in ("encryption_at_rest", "high_availability", "roles_preprovisioned"):
            self._equals(value.get(field), True, f"managed_postgresql.{field}")
        self._url(value.get("evidence_url"), "managed_postgresql.evidence_url")
        return cluster_id

    def _secret_manager(self, value: Mapping[str, Any]) -> None:
        self._keys(
            value,
            "secret_manager",
            {"provider", "reference", "last_rotated_at", "rotation_tested", "evidence_url"},
        )
        self._string(value.get("provider"), "secret_manager.provider")
        reference = self._string(value.get("reference"), "secret_manager.reference", max_length=512)
        if reference is not None and (
            not SECRET_REFERENCE_PATTERN.fullmatch(reference)
            or "://" in reference
            or "=" in reference
            or any(character.isspace() for character in reference)
            or ("/" not in reference and not reference.startswith("arn:"))
        ):
            self.errors.add("secret_manager.reference.invalid")
        self._timestamp(value.get("last_rotated_at"), "secret_manager.last_rotated_at")
        self._equals(value.get("rotation_tested"), True, "secret_manager.rotation_tested")
        self._url(value.get("evidence_url"), "secret_manager.evidence_url")

    def _backup(self, value: Mapping[str, Any], *, cluster_id: str | None) -> None:
        self._keys(
            value,
            "backup",
            {
                "policy_id",
                "schedule",
                "retention_days",
                "pitr_enabled",
                "pitr_window_hours",
                "latest_success_at",
                "evidence_url",
                "restore",
            },
        )
        self._string(value.get("policy_id"), "backup.policy_id")
        self._string(value.get("schedule"), "backup.schedule")
        self._integer(value.get("retention_days"), "backup.retention_days", minimum=1)
        self._equals(value.get("pitr_enabled"), True, "backup.pitr_enabled")
        self._integer(value.get("pitr_window_hours"), "backup.pitr_window_hours", minimum=1)
        self._timestamp(value.get("latest_success_at"), "backup.latest_success_at")
        self._url(value.get("evidence_url"), "backup.evidence_url")

        restore = self._object(value.get("restore"), "backup.restore")
        if restore is None:
            return
        self._keys(
            restore,
            "backup.restore",
            {
                "restore_point",
                "target_cluster_id",
                "completed_at",
                "schema_revision",
                "table_inventory_match",
                "reconciliation_passed",
                "evidence_url",
            },
        )
        self._string(restore.get("restore_point"), "backup.restore.restore_point")
        target = self._string(restore.get("target_cluster_id"), "backup.restore.target_cluster_id")
        if target is not None and cluster_id is not None and target == cluster_id:
            self.errors.add("backup.restore.target_cluster_id.not_isolated")
        self._timestamp(restore.get("completed_at"), "backup.restore.completed_at")
        if restore.get("schema_revision") != EXPECTED_MIGRATION_REVISION:
            self.errors.add("backup.restore.schema_revision.invalid")
        self._equals(
            restore.get("table_inventory_match"), True, "backup.restore.table_inventory_match"
        )
        self._equals(
            restore.get("reconciliation_passed"), True, "backup.restore.reconciliation_passed"
        )
        self._url(restore.get("evidence_url"), "backup.restore.evidence_url")

    def _monitoring(self, value: Mapping[str, Any]) -> None:
        self._keys(value, "monitoring", {"dashboard_url", "alerts"})
        self._url(value.get("dashboard_url"), "monitoring.dashboard_url")
        alerts = value.get("alerts")
        if not isinstance(alerts, list):
            self.errors.add("monitoring.alerts.invalid_list")
            return
        seen: set[str] = set()
        for index, raw_alert in enumerate(alerts):
            path = f"monitoring.alerts[{index}]"
            alert = self._object(raw_alert, path)
            if alert is None:
                continue
            self._keys(alert, path, {"kind", "enabled", "tested_at", "evidence_url"})
            kind = self._string(alert.get("kind"), f"{path}.kind")
            if kind is not None:
                if kind not in ALERT_KINDS:
                    self.errors.add(f"{path}.kind.invalid")
                elif kind in seen:
                    self.errors.add("monitoring.alerts.duplicate_kind")
                seen.add(kind)
            self._equals(alert.get("enabled"), True, f"{path}.enabled")
            self._timestamp(alert.get("tested_at"), f"{path}.tested_at")
            self._url(alert.get("evidence_url"), f"{path}.evidence_url")
        if seen != ALERT_KINDS:
            self.errors.add("monitoring.alerts.required_kinds_missing")

    def _smoke(self, value: Mapping[str, Any]) -> None:
        self._keys(
            value,
            "smoke",
            {
                "completed_at",
                "api_replicas",
                "erpnext_environment",
                "scenarios",
                "reconciliation_passed",
                "evidence_url",
            },
        )
        self._timestamp(value.get("completed_at"), "smoke.completed_at")
        self._integer(value.get("api_replicas"), "smoke.api_replicas", minimum=2)
        environment = self._string(value.get("erpnext_environment"), "smoke.erpnext_environment")
        if environment is not None and environment.lower() in {"prod", "production"}:
            self.errors.add("smoke.erpnext_environment.must_be_production_like")
        scenarios = value.get("scenarios")
        if not isinstance(scenarios, list):
            self.errors.add("smoke.scenarios.invalid_list")
        else:
            seen: set[str] = set()
            for index, raw_scenario in enumerate(scenarios):
                path = f"smoke.scenarios[{index}]"
                scenario = self._object(raw_scenario, path)
                if scenario is None:
                    continue
                self._keys(scenario, path, {"kind", "passed", "evidence_url"})
                kind = self._string(scenario.get("kind"), f"{path}.kind")
                if kind is not None:
                    if kind not in SMOKE_SCENARIOS:
                        self.errors.add(f"{path}.kind.invalid")
                    elif kind in seen:
                        self.errors.add("smoke.scenarios.duplicate_kind")
                    seen.add(kind)
                self._equals(scenario.get("passed"), True, f"{path}.passed")
                self._url(scenario.get("evidence_url"), f"{path}.evidence_url")
            if seen != SMOKE_SCENARIOS:
                self.errors.add("smoke.scenarios.required_kinds_missing")
        self._equals(
            value.get("reconciliation_passed"), True, "smoke.reconciliation_passed"
        )
        self._url(value.get("evidence_url"), "smoke.evidence_url")

    def _cutover(self, value: Mapping[str, Any]) -> None:
        self._keys(
            value,
            "cutover",
            {
                "window_start",
                "window_end",
                "change_owner",
                "database_owner",
                "rollback_owner",
                "rollback_before_first_write_documented",
                "forward_fix_after_first_write_documented",
                "dual_write",
                "sqlite_fallback",
                "traffic_decision",
                "evidence_url",
            },
        )
        start = self._timestamp(value.get("window_start"), "cutover.window_start", event=False)
        end = self._timestamp(value.get("window_end"), "cutover.window_end", event=False)
        if start is not None and end is not None and end <= start:
            self.errors.add("cutover.window.invalid_order")
        for field in ("change_owner", "database_owner", "rollback_owner"):
            self._string(value.get(field), f"cutover.{field}")
        self._equals(
            value.get("rollback_before_first_write_documented"),
            True,
            "cutover.rollback_before_first_write_documented",
        )
        self._equals(
            value.get("forward_fix_after_first_write_documented"),
            True,
            "cutover.forward_fix_after_first_write_documented",
        )
        self._equals(value.get("dual_write"), False, "cutover.dual_write")
        self._equals(value.get("sqlite_fallback"), False, "cutover.sqlite_fallback")
        expected_decision = "approved" if self.mode == "production" else "blocked"
        self._equals(value.get("traffic_decision"), expected_decision, "cutover.traffic_decision")
        self._url(value.get("evidence_url"), "cutover.evidence_url")

    def _provenance(self, value: Mapping[str, Any]) -> datetime | None:
        self._keys(value, "provenance", {"captured_at", "captured_by", "notion_report_url"})
        captured_at = self._timestamp(
            value.get("captured_at"), "provenance.captured_at", event=False
        )
        self._string(value.get("captured_by"), "provenance.captured_by")
        self._url(value.get("notion_report_url"), "provenance.notion_report_url")
        return captured_at


def validate_manifest(manifest: Mapping[str, Any], *, mode: str = "production") -> None:
    if mode not in {"production", "template"}:
        raise ValueError("Unsupported validation mode")
    Validator(mode=mode).validate(manifest)


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the secret-safe Phase 6B.3 production evidence manifest."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--mode",
        choices=("production", "template"),
        default="production",
        help="Template mode validates the committed blocked example; production is fail-closed.",
    )
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    options = parse_arguments(arguments)
    try:
        manifest = load_manifest(options.manifest)
        validate_manifest(manifest, mode=options.mode)
    except EvidenceError as exc:
        print("Production evidence manifest is invalid:", file=sys.stderr)
        for path in exc.paths:
            print(f"- {path}", file=sys.stderr)
        return 1
    print("Production evidence manifest is complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
