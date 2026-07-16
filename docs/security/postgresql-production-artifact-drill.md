# PostgreSQL production artifact и restore drill — Phase 6B.2

Статус: воспроизводимый container/deployment mechanism. Этот документ и успешный drill не
подтверждают готовность production инфраструктуры и не заменяют external evidence Phase 6B.3.

## Артефакты

`services/api/Dockerfile` использует pinned multi-arch official Python 3.11 image и pinned
Dockerfile frontend. Wheel собирается один раз с hash-locked bootstrap toolchain.

Два runtime target устанавливают один и тот же wheel:

- `api` — только `requirements.lock`; Alembic и pip отсутствуют;
- `migration` — `requirements-migrations.lock`; Alembic присутствует, pip удалён.

Оба target:

- запускаются как UID/GID `10001:10001`;
- имеют `MYRETAIL_ENVIRONMENT=production` как безопасный default;
- без explicit PostgreSQL configuration завершаются fail closed;
- не содержат source checkout, tests, local state или `.env`.

API запускает один Uvicorn worker. Horizontal scaling выполняется replicas/container instances.
Uvicorn proxy-header rewriting отключён: direct/trusted-proxy client-IP policy принадлежит
MyRetail application config и не должна неявно переопределяться process server.

Root `.dockerignore` — whitelist. В build context разрешены только Dockerfile, package source,
pyproject и три lockfile. Git, `.env*`, cookies, SQLite/state, logs, caches, frontend и tests не
отправляются BuildKit.

## Запуск drill

Требования: Docker с Linux containers и Python 3.11+ на host. Реальные credentials и `.env` не
нужны и не читаются.

```text
python services/api/scripts/production_state_drill.py
```

Для повторного прогона уже собранных local tags:

```text
python services/api/scripts/production_state_drill.py --skip-build --api-image myretail-api:phase6b2-drill --migration-image myretail-migration:phase6b2-drill
```

Resource prefix принимает только lowercase letters, digits и hyphens. Existing resource с тем же
prefix не удаляется: drill откажется до mutation. Default prefix содержит PID.

## Что проверяет drill

1. Сборку API/migration targets из hash-locked graphs.
2. Non-root UID/GID, production default, отсутствие pip, разделение Alembic toolchain и
   `--no-proxy-headers`.
3. Fail-closed запуск обоих images без configuration без раскрытия URL.
4. Уникальный internal Docker network без published PostgreSQL port.
5. Однодневный disposable CA и server certificate только с SAN PostgreSQL service name; CA key
   удаляется сразу после подписи.
6. PostgreSQL TLS session через `verify-full`; wrong network alias обязан дать hostname mismatch и
   безопасный preflight failure.
7. Pre-provisioned owner/migrator/app roles, one-shot migration image и application preflight.
8. Committed RLS sentinel: видим своему tenant и невидим другому tenant.
9. Два read-only API containers с dropped capabilities, no-new-privileges, bounded PIDs и общей
   PostgreSQL; оба должны пройти startup и health.
10. Custom-format backup с database metadata, остановку API writers, разрушение disposable DB и
    restore cluster-admin role.
11. Exact reconciliation migration revision, полного table inventory/row counts и RLS sentinel.
12. Повторный preflight и два API containers после restore.

Команда наружу выводит только безопасный stage failure. Docker command, subprocess stdout/stderr,
URL и ephemeral HMAC material не печатаются. Cleanup удаляет только validated unique containers,
network, TLS volume и PostgreSQL data volume, которыми владеет текущий запуск, и проверяет их
отсутствие.

## Почему restore выполняется platform role

Database roles являются cluster-level objects и provisioned до migration. Provider/PITR restore
также является platform operation. Поэтому disposable `pg_restore` выполняется cluster admin, а
после restore application preflight повторно доказывает owners, memberships, grants, RLS policies
и canary. API role никогда не получает restore/DDL privileges.

## CI

Отдельный `production-artifact` job выполняет тот же script на clean Linux runner. Existing API,
PostgreSQL foundation, OpenAPI, dependency audit, Gitleaks и CodeQL gates продолжают выполняться
отдельно.

## Граница evidence

Drill подтверждает, что versioned package можно собрать, мигрировать, запустить с `verify-full`,
восстановить из PostgreSQL backup и сверить. Он не подтверждает:

- реальный managed PostgreSQL endpoint;
- production secret manager и rotation;
- provider backup schedule/retention/PITR;
- успешный restore из provider backup;
- replication/failover, monitoring и alerts;
- утверждённых владельцев cutover/rollback и решение открыть traffic.

Эти пункты остаются Phase 6B.3. До их закрытия MR-SEC-010 не считается полностью закрытой.
