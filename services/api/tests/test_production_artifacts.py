from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DRILL_PATH = REPOSITORY_ROOT / "services" / "api" / "scripts" / "production_state_drill.py"


def load_drill_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("production_state_drill", DRILL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Production-state drill module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


drill = load_drill_module()


def test_docker_build_context_is_an_explicit_secret_safe_allowlist() -> None:
    lines = {
        line.strip()
        for line in (REPOSITORY_ROOT / ".dockerignore").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    }

    assert lines == {
        "**",
        "!.dockerignore",
        "!package.json",
        "!package-lock.json",
        "!apps/",
        "!apps/web/",
        "!apps/web/**",
        "apps/web/.next/",
        "apps/web/node_modules/",
        "apps/web/tsconfig.tsbuildinfo",
        "!services/",
        "!services/api/",
        "!services/api/Dockerfile",
        "!services/api/pyproject.toml",
        "!services/api/requirements-bootstrap.lock",
        "!services/api/requirements.lock",
        "!services/api/requirements-migrations.lock",
        "!services/api/src/",
        "!services/api/src/**",
        "services/api/src/**/__pycache__/",
        "services/api/src/**/*.py[cod]",
        "!infra/",
        "!infra/aws/",
        "!infra/aws/database-bootstrap/",
        "!infra/aws/database-bootstrap/**",
        "**/.env",
        "**/.env.*",
        "**/cookies.json",
        "**/cookies.txt",
        "**/*.db",
        "**/*.key",
        "**/*.p12",
        "**/*.pem",
        "**/*.pfx",
        "**/*.sqlite",
        "**/*.sqlite3",
    }


def test_dockerfile_pins_build_frontend_and_base_and_separates_targets() -> None:
    dockerfile = (
        REPOSITORY_ROOT / "services" / "api" / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert dockerfile.startswith(
        "# syntax=docker/dockerfile:1.20@sha256:"
        "26147acbda4f14c5add9946e2fd2ed543fc402884fd75146bd342a7f6271dc1d"
    )
    assert (
        "python:3.11-slim-bookworm@sha256:"
        "b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba"
        in dockerfile
    )
    assert "FROM runtime-base AS api" in dockerfile
    assert "FROM runtime-base AS migration" in dockerfile
    assert dockerfile.count("USER 10001:10001") == 2
    assert "MYRETAIL_ENVIRONMENT=production" in dockerfile
    assert "--require-hashes -r /tmp/requirements.lock" in dockerfile
    assert "--require-hashes -r /tmp/requirements-migrations.lock" in dockerfile
    assert "--no-proxy-headers" in dockerfile
    assert (
        "--checksum=sha256:"
        "e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3"
        in dockerfile
    )
    assert dockerfile.count(
        "editables hatchling pathspec pluggy trove-classifiers wheel setuptools pip"
    ) == 2
    assert "COPY ." not in dockerfile


def test_aws_deployment_role_cannot_create_or_mutate_iam_roles() -> None:
    bootstrap = (REPOSITORY_ROOT / "infra" / "aws" / "bootstrap" / "main.tf").read_text(
        encoding="utf-8"
    )
    production_iam = (
        REPOSITORY_ROOT / "infra" / "aws" / "production" / "iam.tf"
    ).read_text(encoding="utf-8")

    assert 'name                 = "myretail-deployment-github-oidc"' in bootstrap
    assert "role/myretail-production-*" not in bootstrap
    for forbidden_action in (
        '"iam:AttachRolePolicy"',
        '"iam:CreateRole"',
        '"iam:DeleteRolePermissionsBoundary"',
        '"iam:PutRolePermissionsBoundary"',
        '"iam:PutRolePolicy"',
        '"iam:UpdateAssumeRolePolicy"',
    ):
        assert forbidden_action not in bootstrap
    assert bootstrap.count('variable = "iam:PassedToService"') == 4
    assert 'resource "aws_iam_role"' not in production_iam
    assert production_iam.count('data "aws_iam_role"') == 6


def test_aws_deployment_role_cannot_read_or_write_runtime_secret_values() -> None:
    bootstrap = (REPOSITORY_ROOT / "infra" / "aws" / "bootstrap" / "main.tf").read_text(
        encoding="utf-8"
    )
    deployment_policy = bootstrap.split(
        'data "aws_iam_policy_document" "github_production" {', maxsplit=1
    )[1].split('resource "aws_iam_role_policy" "github_production" {', maxsplit=1)[0]

    assert '"secretsmanager:*"' not in deployment_policy
    assert '"secretsmanager:GetSecretValue"' not in deployment_policy
    assert '"secretsmanager:BatchGetSecretValue"' not in deployment_policy
    assert '"secretsmanager:PutSecretValue"' not in deployment_policy
    assert '"secretsmanager:UpdateSecretVersionStage"' not in deployment_policy
    assert '"kms:*"' not in deployment_policy
    assert deployment_policy.count('"kms:Decrypt"') == 1
    assert deployment_policy.count('"kms:Encrypt"') == 1


def test_aws_apply_consumes_a_prior_immutable_reviewed_plan() -> None:
    workflow = (
        REPOSITORY_ROOT / ".github" / "workflows" / "aws-production.yml"
    ).read_text(encoding="utf-8")

    assert "plan_run_id:" in workflow
    assert "actions: read" in workflow
    assert "Verify dedicated production account boundary" in workflow
    assert "actual_account_id" in workflow
    assert '"${actual_account_id}" != "${AWS_ACCOUNT_ID}"' in workflow
    assert "production-plan-${{ github.run_id }}" in workflow
    assert "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in workflow
    assert "run-id: ${{ inputs.plan_run_id }}" in workflow
    assert "Verify reviewed plan provenance and digest" in workflow
    assert "sha256sum --check --strict" in workflow
    assert "cmp --silent" in workflow
    assert "Apply the exact reviewed plan" in workflow
    assert workflow.count("terraform -chdir=infra/aws/production plan") == 1


@pytest.mark.parametrize(
    "prefix",
    ["../escape", "Uppercase", "contains_space", "a" * 41, "-leading"],
)
def test_drill_rejects_unsafe_docker_resource_prefixes(prefix: str) -> None:
    with pytest.raises(drill.DrillError):
        drill.names_for_prefix(prefix)


def test_drill_separates_application_and_migration_environments() -> None:
    instance = drill.ProductionStateDrill(
        names=drill.names_for_prefix("myretail-phase6b2-test"),
        api_image="api:test",
        migration_image="migration:test",
        build_images=False,
    )

    application = instance._application_environment()
    migration = instance._migration_environment()

    assert "MYRETAIL_STATE_MIGRATION_DATABASE_URL" not in application
    assert "MYRETAIL_STATE_DATABASE_URL" not in migration
    assert application["MYRETAIL_STATE_PRODUCTION_ENABLEMENT"] == "controlled"
    assert application["MYRETAIL_STATE_POSTGRES_SSL_MODE"] == "verify-full"
    assert application["MYRETAIL_TENANCY_MODE"] == "isolated_site"
    assert application["MYRETAIL_TENANT_ID"] == drill.ISOLATED_TENANT_ID
    assert application["MYRETAIL_TENANT_SLUG"] == drill.SENTINEL_TENANT
    assert application["MYRETAIL_TENANT_ROUTE_VERSION"] == "1"
    assert application["MYRETAIL_ERPNEXT_BASE_URL"].startswith("https://")
    assert migration["MYRETAIL_STATE_MIGRATION_SSL_MODE"] == "verify-full"
    assert len(application["MYRETAIL_AUTH_SECRET"].encode()) >= 32
    assert len(application["MYRETAIL_AUTH_RATE_LIMIT_SECRET"].encode()) >= 32


def test_drill_command_failure_redacts_subprocess_output(monkeypatch) -> None:
    def failed_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=1,
            stdout="postgresql+asyncpg://user:do-not-log@example/state",
            stderr="unexpected do-not-log credential",
        )

    monkeypatch.setattr(drill.subprocess, "run", failed_run)

    with pytest.raises(drill.DrillError) as exc_info:
        drill.run_command(("docker", "version"), stage="redaction regression")

    message = str(exc_info.value)
    assert message == "redaction regression: command failed"
    assert "postgresql" not in message
    assert "do-not-log" not in message


def test_invalid_prefix_fails_before_any_docker_command(monkeypatch, capsys) -> None:
    def unexpected_run(*args, **kwargs):
        del args, kwargs
        raise AssertionError("Docker must not run for an invalid prefix")

    monkeypatch.setattr(drill.subprocess, "run", unexpected_run)

    result = drill.main(["--prefix", "../invalid", "--skip-build"])

    captured = capsys.readouterr()
    assert result == 1
    assert "Resource prefix" in captured.err


def test_collision_cleanup_never_deletes_resources_not_owned_by_drill(monkeypatch) -> None:
    instance = drill.ProductionStateDrill(
        names=drill.names_for_prefix("myretail-phase6b2-collision"),
        api_image="api:test",
        migration_image="migration:test",
        build_images=False,
    )
    docker_calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(instance, "_resources_are_absent", lambda: False)
    monkeypatch.setattr(
        drill,
        "docker",
        lambda *args, **kwargs: docker_calls.append((*args, kwargs)),
    )

    with pytest.raises(drill.DrillError, match="prefix collides"):
        instance._create_resources()
    instance.cleanup()

    assert docker_calls == []


def test_partial_resource_creation_cleans_only_resources_already_owned(monkeypatch) -> None:
    instance = drill.ProductionStateDrill(
        names=drill.names_for_prefix("myretail-phase6b2-partial"),
        api_image="api:test",
        migration_image="migration:test",
        build_images=False,
    )
    docker_calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(instance, "_resources_are_absent", lambda: True)
    monkeypatch.setattr(
        instance,
        "_owned_resources_are_absent",
        lambda container_ids: not container_ids,
    )

    def fake_docker(*args, **kwargs):
        docker_calls.append((*args, kwargs))
        if args[:2] == ("network", "inspect"):
            return drill.CommandResult(
                returncode=0,
                stdout=instance._ownership_token,
                stderr="",
            )
        if args[:2] == ("volume", "create"):
            raise drill.DrillError("create TLS volume: command failed")
        return drill.CommandResult(returncode=0, stdout="owned", stderr="")

    monkeypatch.setattr(drill, "docker", fake_docker)

    with pytest.raises(drill.DrillError, match="create TLS volume"):
        instance._create_resources()
    instance.cleanup()

    assert ("network", "rm", instance.names.network) in {
        call[:-1] for call in docker_calls
    }
    assert not any(
        call[:3] == ("volume", "rm", "--force") for call in docker_calls
    )


def test_volume_creation_race_never_claims_or_removes_foreign_volume(monkeypatch) -> None:
    instance = drill.ProductionStateDrill(
        names=drill.names_for_prefix("myretail-phase6b2-race"),
        api_image="api:test",
        migration_image="migration:test",
        build_images=False,
    )
    docker_calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(instance, "_resources_are_absent", lambda: True)
    monkeypatch.setattr(
        instance,
        "_owned_resources_are_absent",
        lambda container_ids: not container_ids,
    )

    def fake_docker(*args, **kwargs):
        docker_calls.append((*args, kwargs))
        if args[:2] == ("network", "inspect"):
            return drill.CommandResult(
                returncode=0,
                stdout=instance._ownership_token,
                stderr="",
            )
        if args[:2] == ("volume", "inspect"):
            return drill.CommandResult(returncode=0, stdout="foreign-owner", stderr="")
        return drill.CommandResult(returncode=0, stdout="created", stderr="")

    monkeypatch.setattr(drill, "docker", fake_docker)

    with pytest.raises(drill.DrillError, match="ownership collision"):
        instance._create_resources()
    instance.cleanup()

    assert not any(
        call[:3] == ("volume", "rm", "--force") for call in docker_calls
    )
