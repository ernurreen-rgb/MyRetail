from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess  # nosec B404
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Docker/Git are fixed executables and are always invoked without a shell.

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPOSITORY_ROOT / "services" / "api" / "Dockerfile"
BOOTSTRAP_SQL = (
    REPOSITORY_ROOT / "services" / "api" / "tests" / "postgresql" / "bootstrap_roles.sql"
)
POSTGRES_IMAGE = (
    "postgres:18.4@sha256:"
    "bb9940981eeb86356ec91bda430f8e0ba8729cc0b319615a2c2d7ac932fdf7bb"
)
EXPECTED_REVISION = "20260716_03"
DATABASE_NAME = "myretail_state_test"
SENTINEL_ID = "00000000-0000-4000-8000-000000006b02"
SENTINEL_TENANT = "phase6b2-restore-drill"
OTHER_TENANT = "phase6b2-other-tenant"
PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
OWNERSHIP_LABEL = "com.myretail.production-state-drill.owner"


class DrillError(RuntimeError):
    """Safe drill failure that never includes command output or connection details."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DatabaseSnapshot:
    revision: str
    row_counts: dict[str, int]


@dataclass(frozen=True)
class DrillNames:
    prefix: str
    network: str
    tls_volume: str
    data_volume: str
    database: str
    wrong_database_alias: str
    api_one: str
    api_two: str
    backup_file: str


def validate_prefix(prefix: str) -> str:
    if not PREFIX_PATTERN.fullmatch(prefix):
        raise DrillError("Resource prefix must use lowercase letters, digits, and hyphens")
    return prefix


def names_for_prefix(prefix: str) -> DrillNames:
    safe_prefix = validate_prefix(prefix)
    return DrillNames(
        prefix=safe_prefix,
        network=f"{safe_prefix}-network",
        tls_volume=f"{safe_prefix}-tls",
        data_volume=f"{safe_prefix}-data",
        database=f"{safe_prefix}-db",
        wrong_database_alias=f"{safe_prefix}-wrong-db",
        api_one=f"{safe_prefix}-api-1",
        api_two=f"{safe_prefix}-api-2",
        backup_file=f"/var/lib/postgresql/{safe_prefix}-state.dump",
    )


def run_command(
    command: Sequence[str],
    *,
    stage: str,
    input_text: str | None = None,
    check: bool = True,
) -> CommandResult:
    try:
        # All arguments are lists; resource prefixes are validated before use.
        completed = subprocess.run(  # nosec B603
            list(command),
            cwd=REPOSITORY_ROOT,
            check=False,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        raise DrillError(f"{stage}: required executable is unavailable") from None
    if check and completed.returncode != 0:
        raise DrillError(f"{stage}: command failed")
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def docker(
    *arguments: str,
    stage: str,
    input_text: str | None = None,
    check: bool = True,
) -> CommandResult:
    return run_command(
        ("docker", *arguments),
        stage=stage,
        input_text=input_text,
        check=check,
    )


def environment_arguments(values: dict[str, str]) -> list[str]:
    arguments: list[str] = []
    for name in sorted(values):
        arguments.extend(("--env", f"{name}={values[name]}"))
    return arguments


class ProductionStateDrill:
    def __init__(
        self,
        *,
        names: DrillNames,
        api_image: str,
        migration_image: str,
        build_images: bool,
    ) -> None:
        self.names = names
        self.api_image = api_image
        self.migration_image = migration_image
        self.build_images_enabled = build_images
        self._active_api_containers: set[str] = set()
        self._database_container_id: str | None = None
        self._network_owned = False
        self._tls_volume_owned = False
        self._data_volume_owned = False
        self._ownership_token = secrets.token_hex(16)
        self._rate_limit_key = secrets.token_urlsafe(32)

    def run(self) -> DatabaseSnapshot:
        if self.build_images_enabled:
            self._build_images()
        self._verify_artifact_contracts()
        self._create_resources()
        self._generate_tls_material()
        self._start_database()
        self._wait_for_database()
        self._verify_server_tls()
        self._bootstrap_roles()
        self._run_migration()
        self._run_preflight()
        self._verify_hostname_mismatch_fails_closed()
        self._insert_sentinel()
        self._verify_sentinel_isolation()
        self._start_api_pair()
        self._verify_api_pair()
        self._stop_api_pair()

        before_restore = self._snapshot_database()
        self._backup_database()
        self._destroy_database()
        self._restore_database()
        after_restore = self._snapshot_database()
        if after_restore != before_restore:
            raise DrillError("restore reconciliation: database snapshot mismatch")

        self._verify_sentinel_isolation()
        self._run_preflight()
        self._start_api_pair()
        self._verify_api_pair()
        return after_restore

    def cleanup(self) -> None:
        owned_containers = set(self._active_api_containers)
        if self._database_container_id is not None:
            owned_containers.add(self._database_container_id)
        owned_resources = (
            bool(owned_containers)
            or self._network_owned
            or self._tls_volume_owned
            or self._data_volume_owned
        )
        if not owned_resources:
            return
        for container in owned_containers:
            docker(
                "rm",
                "--force",
                "--volumes",
                container,
                stage="cleanup container",
                check=False,
            )
        if self._network_owned and self._resource_is_still_owned(
            "network", self.names.network
        ):
            docker(
                "network",
                "rm",
                self.names.network,
                stage="cleanup network",
                check=False,
            )
        if self._tls_volume_owned and self._resource_is_still_owned(
            "volume", self.names.tls_volume
        ):
            docker(
                "volume",
                "rm",
                "--force",
                self.names.tls_volume,
                stage="cleanup TLS volume",
                check=False,
            )
        if self._data_volume_owned and self._resource_is_still_owned(
            "volume", self.names.data_volume
        ):
            docker(
                "volume",
                "rm",
                "--force",
                self.names.data_volume,
                stage="cleanup data volume",
                check=False,
            )
        if not self._owned_resources_are_absent(owned_containers):
            raise DrillError("cleanup verification: disposable resources remain")
        self._active_api_containers.clear()
        self._database_container_id = None
        self._network_owned = False
        self._tls_volume_owned = False
        self._data_volume_owned = False

    def _build_images(self) -> None:
        revision = run_command(
            ("git", "rev-parse", "HEAD"),
            stage="resolve artifact revision",
        ).stdout
        common = (
            "build",
            "--file",
            str(DOCKERFILE),
            "--build-arg",
            f"OCI_REVISION={revision}",
        )
        docker(
            *common,
            "--target",
            "api",
            "--tag",
            self.api_image,
            ".",
            stage="build API image",
        )
        docker(
            *common,
            "--target",
            "migration",
            "--tag",
            self.migration_image,
            ".",
            stage="build migration image",
        )

    def _verify_artifact_contracts(self) -> None:
        for image in (self.api_image, self.migration_image):
            user = docker(
                "image",
                "inspect",
                image,
                "--format",
                "{{.Config.User}}",
                stage="inspect image runtime user",
            ).stdout
            if user != "10001:10001":
                raise DrillError("inspect image runtime user: non-root contract failed")

            environment = docker(
                "image",
                "inspect",
                image,
                "--format",
                "{{json .Config.Env}}",
                stage="inspect image environment",
            ).stdout
            if "MYRETAIL_ENVIRONMENT=production" not in environment:
                raise DrillError("inspect image environment: production default is missing")

        api_modules = docker(
            "run",
            "--rm",
            "--entrypoint",
            "python",
            self.api_image,
            "-c",
            "import importlib.util; "
            "names=('pip','setuptools','wheel','alembic','hatchling','editables',"
            "'pathspec','pluggy','trove_classifiers'); "
            "print(int(all(importlib.util.find_spec(name) is None for name in names)))",
            stage="inspect API image modules",
        ).stdout
        if api_modules != "1":
            raise DrillError("inspect API image modules: minimized toolchain contract failed")

        migration_modules = docker(
            "run",
            "--rm",
            "--entrypoint",
            "python",
            self.migration_image,
            "-c",
            "import importlib.util; "
            "build=('pip','setuptools','wheel','hatchling','editables','pathspec',"
            "'pluggy','trove_classifiers'); "
            "print(int(all(importlib.util.find_spec(name) is None for name in build) "
            "and importlib.util.find_spec('alembic') is not None))",
            stage="inspect migration image modules",
        ).stdout
        if migration_modules != "1":
            raise DrillError(
                "inspect migration image modules: migration toolchain contract failed"
            )

        api_command = docker(
            "image",
            "inspect",
            self.api_image,
            "--format",
            "{{json .Config.Cmd}}",
            stage="inspect API image command",
        ).stdout
        if "--no-proxy-headers" not in api_command:
            raise DrillError("inspect API image command: proxy boundary is ambiguous")

        self._verify_image_default_fails_closed(self.api_image, "API")
        self._verify_image_default_fails_closed(self.migration_image, "migration")

    def _verify_image_default_fails_closed(self, image: str, label: str) -> None:
        result = docker(
            "run",
            "--rm",
            image,
            stage=f"verify {label} image default",
            check=False,
        )
        combined_output = f"{result.stdout}\n{result.stderr}"
        if result.returncode == 0:
            raise DrillError(f"verify {label} image default: image unexpectedly started")
        if "postgresql+asyncpg" in combined_output:
            raise DrillError(f"verify {label} image default: diagnostic exposed a URL")

    def _create_resources(self) -> None:
        if not self._resources_are_absent():
            raise DrillError("resource creation: prefix collides with existing Docker resources")
        docker(
            "network",
            "create",
            "--label",
            f"{OWNERSHIP_LABEL}={self._ownership_token}",
            self.names.network,
            stage="create internal network",
        )
        self._network_owned = True
        docker(
            "volume",
            "create",
            "--label",
            f"{OWNERSHIP_LABEL}={self._ownership_token}",
            self.names.tls_volume,
            stage="create TLS volume",
        )
        if not self._resource_is_still_owned("volume", self.names.tls_volume):
            raise DrillError("create TLS volume: ownership collision")
        self._tls_volume_owned = True
        docker(
            "volume",
            "create",
            "--label",
            f"{OWNERSHIP_LABEL}={self._ownership_token}",
            self.names.data_volume,
            stage="create data volume",
        )
        if not self._resource_is_still_owned("volume", self.names.data_volume):
            raise DrillError("create data volume: ownership collision")
        self._data_volume_owned = True

    def _resources_are_absent(self) -> bool:
        checks = (
            ("container", "inspect", self.names.database),
            ("container", "inspect", self.names.api_one),
            ("container", "inspect", self.names.api_two),
            ("network", "inspect", self.names.network),
            ("volume", "inspect", self.names.tls_volume),
            ("volume", "inspect", self.names.data_volume),
        )
        return all(
            docker(*arguments, stage="inspect disposable resource", check=False).returncode
            != 0
            for arguments in checks
        )

    def _owned_resources_are_absent(self, container_ids: set[str]) -> bool:
        checks = [
            ("container", "inspect", container_id) for container_id in container_ids
        ]
        if self._network_owned:
            checks.append(("network", "inspect", self.names.network))
        if self._tls_volume_owned:
            checks.append(("volume", "inspect", self.names.tls_volume))
        if self._data_volume_owned:
            checks.append(("volume", "inspect", self.names.data_volume))
        return all(
            docker(*arguments, stage="verify cleaned resource", check=False).returncode
            != 0
            for arguments in checks
        )

    def _resource_is_still_owned(self, resource_type: str, name: str) -> bool:
        result = docker(
            resource_type,
            "inspect",
            name,
            "--format",
            f'{{{{index .Labels "{OWNERSHIP_LABEL}"}}}}',
            stage="verify disposable resource ownership",
            check=False,
        )
        return result.returncode == 0 and result.stdout == self._ownership_token

    def _generate_tls_material(self) -> None:
        certificate_script = f"""
set -eu
umask 077
openssl genrsa -out /tls/ca.key 3072
openssl req -x509 -new -sha256 -days 1 \
  -key /tls/ca.key \
  -subj '/CN=MyRetail Phase 6B.2 Disposable CA' \
  -out /tls/ca.crt
openssl genrsa -out /tls/server.key 3072
cat >/tmp/server.cnf <<'EOF'
[req]
distinguished_name = subject
req_extensions = extensions
prompt = no
[subject]
CN = {self.names.database}
[extensions]
subjectAltName = @alt_names
[alt_names]
DNS.1 = {self.names.database}
EOF
openssl req -new -sha256 \
  -key /tls/server.key \
  -config /tmp/server.cnf \
  -out /tls/server.csr
openssl x509 -req -sha256 -days 1 \
  -in /tls/server.csr \
  -CA /tls/ca.crt \
  -CAkey /tls/ca.key \
  -CAcreateserial \
  -extensions extensions \
  -extfile /tmp/server.cnf \
  -out /tls/server.crt
chown postgres:postgres /tls/server.key /tls/server.crt /tls/ca.crt
chmod 0600 /tls/server.key
chmod 0644 /tls/server.crt /tls/ca.crt
rm -f /tls/ca.key /tls/ca.srl /tls/server.csr /tmp/server.cnf
"""
        docker(
            "run",
            "--rm",
            "--volume",
            f"{self.names.tls_volume}:/tls",
            POSTGRES_IMAGE,
            "bash",
            "-euc",
            certificate_script,
            stage="generate disposable TLS material",
        )

    def _start_database(self) -> None:
        result = docker(
            "run",
            "--detach",
            "--name",
            self.names.database,
            "--network",
            self.names.network,
            "--network-alias",
            self.names.database,
            "--network-alias",
            self.names.wrong_database_alias,
            "--volume",
            f"{self.names.tls_volume}:/tls:ro",
            "--volume",
            f"{self.names.data_volume}:/var/lib/postgresql",
            "--env",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            POSTGRES_IMAGE,
            "-c",
            "ssl=on",
            "-c",
            "ssl_cert_file=/tls/server.crt",
            "-c",
            "ssl_key_file=/tls/server.key",
            "-c",
            "ssl_ca_file=/tls/ca.crt",
            stage="start TLS PostgreSQL",
        )
        if not result.stdout:
            raise DrillError("start TLS PostgreSQL: container id is unavailable")
        self._database_container_id = result.stdout

    def _wait_for_database(self) -> None:
        for _ in range(60):
            result = docker(
                "exec",
                self.names.database,
                "pg_isready",
                "-U",
                "postgres",
                "-d",
                "postgres",
                stage="wait for PostgreSQL",
                check=False,
            )
            if result.returncode == 0:
                return
            time.sleep(0.5)
        raise DrillError("wait for PostgreSQL: readiness timeout")

    def _verify_server_tls(self) -> None:
        for _ in range(20):
            result = docker(
                "exec",
                "--env",
                "PGSSLMODE=verify-full",
                "--env",
                "PGSSLROOTCERT=/tls/ca.crt",
                self.names.database,
                "psql",
                "-h",
                self.names.database,
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-qAt",
                "-c",
                "SELECT ssl::text || '|' || version FROM pg_stat_ssl "
                "WHERE pid = pg_backend_pid()",
                stage="verify PostgreSQL TLS session",
                check=False,
            )
            if result.returncode == 0 and result.stdout.startswith("true|TLSv"):
                return
            time.sleep(0.25)
        raise DrillError("verify PostgreSQL TLS session: TLS readiness timeout")

    def _bootstrap_roles(self) -> None:
        bootstrap_sql = BOOTSTRAP_SQL.read_text(encoding="utf-8")
        docker(
            "exec",
            "--interactive",
            self.names.database,
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            stage="bootstrap disposable PostgreSQL roles",
            input_text=bootstrap_sql,
        )

    def _migration_environment(self) -> dict[str, str]:
        return {
            "MYRETAIL_ENVIRONMENT": "production",
            "MYRETAIL_STATE_MIGRATION_DATABASE_URL": self._database_url(
                "myretail_state_migrator"
            ),
            "MYRETAIL_STATE_MIGRATION_SSL_MODE": "verify-full",
            "MYRETAIL_STATE_MIGRATION_SSL_ROOT_CERT_PATH": "/tls/ca.crt",
        }

    def _application_environment(self, *, host: str | None = None) -> dict[str, str]:
        return {
            "MYRETAIL_AUTH_RATE_LIMIT_SECRET": self._rate_limit_key,
            "MYRETAIL_ENVIRONMENT": "production",
            "MYRETAIL_STATE_BACKEND": "postgresql",
            "MYRETAIL_STATE_DATABASE_URL": self._database_url(
                "myretail_api", host=host
            ),
            "MYRETAIL_STATE_POSTGRES_SSL_MODE": "verify-full",
            "MYRETAIL_STATE_POSTGRES_SSL_ROOT_CERT_PATH": "/tls/ca.crt",
            "MYRETAIL_STATE_PRODUCTION_ENABLEMENT": "controlled",
        }

    def _database_url(self, role: str, *, host: str | None = None) -> str:
        database_host = host or self.names.database
        return f"postgresql+asyncpg://{role}@{database_host}:5432/{DATABASE_NAME}"

    def _run_migration(self) -> None:
        docker(
            "run",
            "--rm",
            "--network",
            self.names.network,
            "--volume",
            f"{self.names.tls_volume}:/tls:ro",
            *environment_arguments(self._migration_environment()),
            self.migration_image,
            "upgrade",
            "head",
            stage="run migration image",
        )

    def _run_preflight(self) -> None:
        docker(
            "run",
            "--rm",
            "--network",
            self.names.network,
            "--volume",
            f"{self.names.tls_volume}:/tls:ro",
            *environment_arguments(self._application_environment()),
            "--entrypoint",
            "myretail-state-preflight",
            self.api_image,
            stage="run application preflight",
        )

    def _verify_hostname_mismatch_fails_closed(self) -> None:
        result = docker(
            "run",
            "--rm",
            "--network",
            self.names.network,
            "--volume",
            f"{self.names.tls_volume}:/tls:ro",
            *environment_arguments(
                self._application_environment(host=self.names.wrong_database_alias)
            ),
            "--entrypoint",
            "myretail-state-preflight",
            self.api_image,
            stage="verify TLS hostname mismatch",
            check=False,
        )
        combined_output = f"{result.stdout}\n{result.stderr}"
        if result.returncode == 0:
            raise DrillError("verify TLS hostname mismatch: preflight unexpectedly passed")
        if (
            "postgresql+asyncpg" in combined_output
            or self._rate_limit_key in combined_output
        ):
            raise DrillError("verify TLS hostname mismatch: diagnostic exposed configuration")

    def _psql_as_application(
        self,
        sql: str,
        *,
        stage: str,
        variables: dict[str, str] | None = None,
    ) -> CommandResult:
        variable_arguments: list[str] = []
        for name, value in sorted((variables or {}).items()):
            variable_arguments.extend(("-v", f"{name}={value}"))
        return docker(
            "exec",
            "--interactive",
            "--env",
            "PGSSLMODE=verify-full",
            "--env",
            "PGSSLROOTCERT=/tls/ca.crt",
            self.names.database,
            "psql",
            "-h",
            self.names.database,
            "-U",
            "myretail_api",
            "-d",
            DATABASE_NAME,
            "-v",
            "ON_ERROR_STOP=1",
            *variable_arguments,
            "-qAt",
            stage=stage,
            input_text=sql,
        )

    def _insert_sentinel(self) -> None:
        sql = (
            "BEGIN; "
            "SET LOCAL myretail.tenant_id = :'tenant'; "
            "INSERT INTO myretail_state.rls_canary (canary_id, tenant_id) "
            "VALUES (:'sentinel_id'::uuid, :'tenant'); "
            "COMMIT;"
        )
        self._psql_as_application(
            sql,
            stage="insert RLS restore sentinel",
            variables={"sentinel_id": SENTINEL_ID, "tenant": SENTINEL_TENANT},
        )

    def _verify_sentinel_isolation(self) -> None:
        own_count = self._sentinel_count(SENTINEL_TENANT)
        other_count = self._sentinel_count(OTHER_TENANT)
        if own_count != 1 or other_count != 0:
            raise DrillError("verify RLS restore sentinel: tenant isolation mismatch")

    def _sentinel_count(self, tenant: str) -> int:
        sql = (
            "BEGIN; "
            "SET LOCAL myretail.tenant_id = :'tenant'; "
            "SELECT count(*) FROM myretail_state.rls_canary "
            "WHERE canary_id = :'sentinel_id'::uuid; "
            "ROLLBACK;"
        )
        result = self._psql_as_application(
            sql,
            stage="read RLS restore sentinel",
            variables={"sentinel_id": SENTINEL_ID, "tenant": tenant},
        )
        try:
            return int(result.stdout)
        except ValueError:
            raise DrillError("read RLS restore sentinel: invalid count") from None

    def _start_api_pair(self) -> None:
        for name in (self.names.api_one, self.names.api_two):
            result = docker(
                "run",
                "--detach",
                "--name",
                name,
                "--network",
                self.names.network,
                "--volume",
                f"{self.names.tls_volume}:/tls:ro",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",  # nosec B108
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "256",
                *environment_arguments(self._application_environment()),
                self.api_image,
                stage="start API container pair",
            )
            if not result.stdout:
                raise DrillError("start API container pair: container id is unavailable")
            self._active_api_containers.add(result.stdout)

    def _verify_api_pair(self) -> None:
        for _ in range(60):
            healthy = all(self._api_health(container) for container in self._active_api_containers)
            if healthy and len(self._active_api_containers) == 2:
                break
            time.sleep(0.5)
        else:
            raise DrillError("verify API container pair: health timeout")

        sessions = docker(
            "exec",
            self.names.database,
            "psql",
            "-U",
            "postgres",
            "-d",
            DATABASE_NAME,
            "-qAt",
            "-c",
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE usename = 'myretail_api' AND datname = current_database()",
            stage="verify API database sessions",
        )
        try:
            session_count = int(sessions.stdout)
        except ValueError:
            raise DrillError("verify API database sessions: invalid count") from None
        if session_count < 2:
            raise DrillError("verify API database sessions: shared database not observed")

    def _api_health(self, container: str) -> bool:
        result = docker(
            "exec",
            container,
            "python",
            "-c",
            "from urllib.request import urlopen; "
            "raise SystemExit(0 if b'\\\"status\\\":\\\"ok\\\"' in "
            "urlopen('http://127.0.0.1:8000/health', timeout=1).read().replace(b' ', b'') "
            "else 1)",
            stage="probe API container health",
            check=False,
        )
        return result.returncode == 0

    def _stop_api_pair(self) -> None:
        for container in tuple(self._active_api_containers):
            docker("stop", container, stage="stop API container")
            docker("rm", container, stage="remove API container")
            self._active_api_containers.remove(container)

    def _snapshot_database(self) -> DatabaseSnapshot:
        tables_result = docker(
            "exec",
            self.names.database,
            "psql",
            "-U",
            "postgres",
            "-d",
            DATABASE_NAME,
            "-qAt",
            "-c",
            "SELECT table_schema || '.' || table_name "
            "FROM information_schema.tables "
            "WHERE table_type = 'BASE TABLE' "
            "AND (table_schema = 'myretail_state' "
            "OR (table_schema = 'public' AND table_name = 'alembic_version')) "
            "ORDER BY table_schema, table_name",
            stage="snapshot table inventory",
        )
        table_names = [name for name in tables_result.stdout.splitlines() if name]
        if not table_names:
            raise DrillError("snapshot table inventory: no state tables found")

        row_counts: dict[str, int] = {}
        for table_name in table_names:
            schema_name, relation_name = table_name.split(".", maxsplit=1)
            quoted_schema = _quote_identifier(schema_name)
            quoted_relation = _quote_identifier(relation_name)
            result = docker(
                "exec",
                self.names.database,
                "psql",
                "-U",
                "postgres",
                "-d",
                DATABASE_NAME,
                "-qAt",
                "-c",
                # Names originate from information_schema and are identifier-quoted.
                f"SELECT count(*) FROM {quoted_schema}.{quoted_relation}",  # nosec B608
                stage="snapshot table row count",
            )
            try:
                row_counts[table_name] = int(result.stdout)
            except ValueError:
                raise DrillError("snapshot table row count: invalid count") from None

        revision = docker(
            "exec",
            self.names.database,
            "psql",
            "-U",
            "postgres",
            "-d",
            DATABASE_NAME,
            "-qAt",
            "-c",
            "SELECT version_num FROM public.alembic_version",
            stage="snapshot migration revision",
        ).stdout
        if revision != EXPECTED_REVISION:
            raise DrillError("snapshot migration revision: unexpected head")
        return DatabaseSnapshot(revision=revision, row_counts=row_counts)

    def _backup_database(self) -> None:
        docker(
            "exec",
            "--interactive",
            self.names.database,
            "pg_dump",
            "-U",
            "postgres",
            "-d",
            DATABASE_NAME,
            "--format=custom",
            "--create",
            f"--file={self.names.backup_file}",
            stage="create disposable PostgreSQL backup",
        )

    def _destroy_database(self) -> None:
        docker(
            "exec",
            self.names.database,
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            f"database_name={DATABASE_NAME}",
            "-q",
            stage="terminate disposable database sessions",
            input_text=(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :'database_name' AND pid <> pg_backend_pid();"
            ),
        )
        docker(
            "exec",
            self.names.database,
            "dropdb",
            "-U",
            "postgres",
            DATABASE_NAME,
            stage="destroy disposable database",
        )

    def _restore_database(self) -> None:
        docker(
            "exec",
            self.names.database,
            "pg_restore",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "--create",
            "--exit-on-error",
            self.names.backup_file,
            stage="restore disposable PostgreSQL backup",
        )


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build MyRetail production artifacts and run a disposable TLS backup/restore drill."
        )
    )
    parser.add_argument(
        "--prefix",
        default=f"myretail-phase6b2-{os.getpid()}",
        help="Unique lowercase Docker resource prefix.",
    )
    parser.add_argument("--api-image", default="myretail-api:phase6b2-drill")
    parser.add_argument("--migration-image", default="myretail-migration:phase6b2-drill")
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Use already-built image tags instead of building both targets.",
    )
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_arguments(arguments)
    try:
        names = names_for_prefix(args.prefix)
    except DrillError as exc:
        print(f"MyRetail production-state drill failed: {exc}.", file=sys.stderr)
        return 1

    drill = ProductionStateDrill(
        names=names,
        api_image=args.api_image,
        migration_image=args.migration_image,
        build_images=not args.skip_build,
    )
    snapshot: DatabaseSnapshot | None = None
    failure: DrillError | None = None
    try:
        snapshot = drill.run()
    except DrillError as exc:
        failure = exc
    except KeyboardInterrupt:
        failure = DrillError("interrupted")

    try:
        drill.cleanup()
    except DrillError as exc:
        if failure is None:
            failure = exc

    if failure is not None:
        print(f"MyRetail production-state drill failed: {failure}.", file=sys.stderr)
        return 1
    if snapshot is None:
        print("MyRetail production-state drill failed: result is unavailable.", file=sys.stderr)
        return 1

    print(
        "MyRetail production-state drill passed: "
        f"revision={snapshot.revision}, tables={len(snapshot.row_counts)}, "
        "TLS=verify-full, API replicas=2, restore=reconciled."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
