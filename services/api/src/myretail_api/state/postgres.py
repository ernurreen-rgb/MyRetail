from __future__ import annotations

import ssl
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from myretail_api.config import InvalidStateFoundationSettingsError, Settings
from myretail_api.state.schema import (
    EXPECTED_STATE_SCHEMA_REVISION,
    PREAUTH_STATE_TABLES,
    STATE_APP_ROLE,
    STATE_SCHEMA,
    TENANT_STATE_TABLES,
)


class StateStartupError(RuntimeError):
    """Safe startup failure that never includes connection credentials or SQL parameters."""


class PostgresStateRuntime:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @classmethod
    async def start(cls, settings: Settings) -> PostgresStateRuntime:
        try:
            engine = create_postgres_state_engine(settings)
        except InvalidStateFoundationSettingsError:
            raise
        except Exception:
            raise StateStartupError("PostgreSQL state pool could not be created") from None

        runtime = cls(engine)
        try:
            await runtime.verify()
        except StateStartupError:
            await engine.dispose()
            raise
        except Exception:
            await engine.dispose()
            raise StateStartupError("PostgreSQL state startup checks failed") from None
        return runtime

    async def close(self) -> None:
        await self.engine.dispose()

    async def verify(self) -> None:
        try:
            async with self.engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    await _verify_connection_contract(connection)
                    await _verify_preauth_rate_limit_contract(connection)
                    await _verify_rls_canary(connection)
                finally:
                    await transaction.rollback()
        except StateStartupError:
            raise
        except Exception:
            raise StateStartupError("PostgreSQL state storage is unavailable") from None


def create_postgres_state_engine(settings: Settings) -> AsyncEngine:
    if settings.state_backend != "postgresql" or settings.state_database_url is None:
        raise InvalidStateFoundationSettingsError(
            "PostgreSQL state engine requires the postgresql backend and database URL"
        )
    if settings.state_pool_min_size > settings.state_pool_max_size:
        raise InvalidStateFoundationSettingsError(
            "PostgreSQL state pool minimum size cannot exceed maximum size"
        )

    return create_async_engine(
        settings.state_database_url.get_secret_value(),
        echo=False,
        hide_parameters=True,
        pool_pre_ping=True,
        pool_size=settings.state_pool_min_size,
        max_overflow=settings.state_pool_max_size - settings.state_pool_min_size,
        pool_timeout=settings.state_pool_acquire_timeout_seconds,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.state_statement_timeout_ms),
                "lock_timeout": str(settings.state_lock_timeout_ms),
            },
            "ssl": _ssl_argument(settings),
        },
    )


def _ssl_argument(settings: Settings) -> bool | ssl.SSLContext:
    mode = settings.state_postgres_ssl_mode
    if mode == "disable":
        return False

    root_cert = settings.state_postgres_ssl_root_cert_path
    if root_cert is not None and not root_cert.is_file():
        raise InvalidStateFoundationSettingsError(
            "PostgreSQL state TLS root certificate file is unavailable"
        )
    context = ssl.create_default_context(cafile=_cert_path(root_cert))
    if mode == "require":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif mode == "verify-ca":
        context.check_hostname = False
    return context


def _cert_path(path: Path | None) -> str | None:
    return str(path) if path is not None else None


async def _verify_connection_contract(connection: AsyncConnection) -> None:
    result = await connection.execute(
        text(
            """
            SELECT current_user AS role_name,
                   rolcanlogin,
                   rolsuper,
                   rolcreatedb,
                   rolcreaterole,
                   rolreplication,
                   rolbypassrls
            FROM pg_roles
            WHERE rolname = current_user
            """
        )
    )
    role = result.mappings().one_or_none()
    if role is None or role["role_name"] != STATE_APP_ROLE or not role["rolcanlogin"]:
        raise StateStartupError("PostgreSQL state application role contract is invalid")
    if any(
        role[name]
        for name in (
            "rolsuper",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise StateStartupError("PostgreSQL state application role is overprivileged")

    read_only = (await connection.execute(text("SHOW transaction_read_only"))).scalar_one()
    if str(read_only).lower() != "off":
        raise StateStartupError("PostgreSQL state connection is read-only")

    revision = (
        await connection.execute(text("SELECT version_num FROM public.alembic_version"))
    ).scalar_one_or_none()
    if revision != EXPECTED_STATE_SCHEMA_REVISION:
        raise StateStartupError("PostgreSQL state schema revision mismatch")

    privilege_result = await connection.execute(
        text(
            """
            SELECT has_schema_privilege(current_user, :schema_name, 'USAGE') AS has_usage,
                   has_schema_privilege(current_user, :schema_name, 'CREATE') AS has_create,
                   (
                       SELECT count(*)
                       FROM pg_tables
                       WHERE schemaname = :schema_name AND tableowner = current_user
                   ) AS owned_tables
            """
        ),
        {"schema_name": STATE_SCHEMA},
    )
    privileges = privilege_result.mappings().one()
    if not privileges["has_usage"] or privileges["has_create"] or privileges["owned_tables"]:
        raise StateStartupError("PostgreSQL state application grants are invalid")

    rls_result = await connection.execute(
        text(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema_name AND c.relkind = 'r'
            """
        ),
        {"schema_name": STATE_SCHEMA},
    )
    rls_tables = {
        row["relname"]
        for row in rls_result.mappings()
        if row["relrowsecurity"] and row["relforcerowsecurity"]
    }
    if not set(TENANT_STATE_TABLES).issubset(rls_tables):
        raise StateStartupError("PostgreSQL state tenant RLS contract is invalid")

    policy_result = await connection.execute(
        text(
            """
            SELECT c.relname,
                   p.polname,
                   p.polpermissive,
                   p.polcmd::text AS policy_command,
                   ARRAY(
                       SELECT r.rolname
                       FROM unnest(p.polroles) AS policy_role(role_oid)
                       JOIN pg_roles AS r ON r.oid = policy_role.role_oid
                       ORDER BY r.rolname
                   ) AS role_names,
                   pg_get_expr(p.polqual, p.polrelid) AS using_expression,
                   pg_get_expr(p.polwithcheck, p.polrelid) AS check_expression
            FROM pg_policy AS p
            JOIN pg_class AS c ON c.oid = p.polrelid
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema_name
              AND c.relname = ANY(CAST(:table_names AS text[]))
            ORDER BY c.relname, p.polname
            """
        ),
        {
            "schema_name": STATE_SCHEMA,
            "table_names": list(TENANT_STATE_TABLES),
        },
    )
    policies = list(policy_result.mappings())
    policies_by_table = {row["relname"]: row for row in policies}
    canary_policy = policies_by_table.get("rls_canary")
    if (
        len(policies) != len(TENANT_STATE_TABLES)
        or set(policies_by_table) != set(TENANT_STATE_TABLES)
        or canary_policy is None
        or canary_policy["using_expression"] is None
        or canary_policy["using_expression"] != canary_policy["check_expression"]
    ):
        raise StateStartupError("PostgreSQL state tenant policy contract is invalid")

    expected_expression = canary_policy["using_expression"]
    for table_name, policy in policies_by_table.items():
        if (
            policy["polname"] != f"{table_name}_tenant_isolation"
            or not policy["polpermissive"]
            or policy["policy_command"] != "*"
            or list(policy["role_names"]) != [STATE_APP_ROLE]
            or policy["using_expression"] != expected_expression
            or policy["check_expression"] != expected_expression
        ):
            raise StateStartupError("PostgreSQL state tenant policy contract is invalid")


async def _verify_rls_canary(connection: AsyncConnection) -> None:
    canary_id = uuid4()
    tenant_a = f"startup-canary-a-{canary_id}"
    tenant_b = f"startup-canary-b-{canary_id}"
    await connection.execute(
        text("SELECT set_config('myretail.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_a},
    )
    await connection.execute(
        text(
            """
            INSERT INTO myretail_state.rls_canary (canary_id, tenant_id)
            VALUES (CAST(:canary_id AS uuid), :tenant_id)
            """
        ),
        {"canary_id": str(canary_id), "tenant_id": tenant_a},
    )
    own_rows = (
        await connection.execute(
            text(
                """
                SELECT count(*) FROM myretail_state.rls_canary
                WHERE canary_id = CAST(:canary_id AS uuid)
                """
            ),
            {"canary_id": str(canary_id)},
        )
    ).scalar_one()
    if own_rows != 1:
        raise StateStartupError("PostgreSQL state RLS canary write/read failed")

    await connection.execute(
        text("SELECT set_config('myretail.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_b},
    )
    cross_tenant_rows = (
        await connection.execute(
            text(
                """
                SELECT count(*) FROM myretail_state.rls_canary
                WHERE canary_id = CAST(:canary_id AS uuid)
                """
            ),
            {"canary_id": str(canary_id)},
        )
    ).scalar_one()
    if cross_tenant_rows != 0:
        raise StateStartupError("PostgreSQL state RLS canary isolation failed")


async def _verify_preauth_rate_limit_contract(connection: AsyncConnection) -> None:
    table_result = await connection.execute(
        text(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   has_table_privilege(
                       current_user, quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                       'SELECT'
                   ) AS can_select,
                   has_table_privilege(
                       current_user, quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                       'INSERT'
                   ) AS can_insert,
                   has_table_privilege(
                       current_user, quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                       'UPDATE'
                   ) AS can_update,
                   has_table_privilege(
                       current_user, quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                       'DELETE'
                   ) AS can_delete,
                   has_table_privilege(
                       current_user, quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                       'TRUNCATE, REFERENCES, TRIGGER'
                   ) AS has_extra_privileges
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema_name
              AND c.relname = ANY(CAST(:table_names AS text[]))
              AND c.relkind = 'r'
            """
        ),
        {
            "schema_name": STATE_SCHEMA,
            "table_names": list(PREAUTH_STATE_TABLES),
        },
    )
    tables = {row["relname"]: row for row in table_result.mappings()}
    if set(tables) != set(PREAUTH_STATE_TABLES):
        raise StateStartupError("PostgreSQL pre-auth state table contract is invalid")
    buckets = tables["auth_rate_limit_buckets"]
    meta = tables["auth_rate_limit_meta"]
    if any(row["relrowsecurity"] or row["relforcerowsecurity"] for row in tables.values()):
        raise StateStartupError("PostgreSQL pre-auth RLS exception contract is invalid")
    if not all(
        buckets[name]
        for name in ("can_select", "can_insert", "can_update", "can_delete")
    ) or buckets["has_extra_privileges"]:
        raise StateStartupError("PostgreSQL pre-auth bucket grants are invalid")
    if (
        not meta["can_select"]
        or not meta["can_update"]
        or meta["can_insert"]
        or meta["can_delete"]
        or meta["has_extra_privileges"]
    ):
        raise StateStartupError("PostgreSQL pre-auth meta grants are invalid")

    meta_state = (
        await connection.execute(
            text(
                """
                SELECT singleton_id,
                       bucket_count,
                       (SELECT count(*)
                        FROM myretail_state.auth_rate_limit_buckets) AS actual_count
                FROM myretail_state.auth_rate_limit_meta
                """
            )
        )
    ).mappings().one_or_none()
    if (
        meta_state is None
        or meta_state["singleton_id"] != 1
        or int(meta_state["bucket_count"]) != int(meta_state["actual_count"])
    ):
        raise StateStartupError("PostgreSQL pre-auth capacity state is invalid")
