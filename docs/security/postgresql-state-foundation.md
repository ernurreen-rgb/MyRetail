# PostgreSQL state foundation — роли и миграции

Актуальное продуктовое и архитектурное решение хранится в Notion. Этот файл описывает только
исполняемый контракт Phase 6A.1 рядом с кодом.

## Граница этапа

- PostgreSQL foundation доступна для development/test и CI.
- Public HTTP API не меняется.
- Текущие SQLite adapters продолжают обслуживать local dev/test requests.
- Production остаётся fail closed до controlled Phase 6B. Наличие schema и pool само по себе не
  разрешает production traffic.
- API startup не применяет migrations.

## Предварительно созданные роли

Роли создаёт platform/database administrator до запуска migration job:

- `myretail_state_owner`: `NOLOGIN`, владелец schema и tables;
- `myretail_state_migrator`: `LOGIN`, не superuser и не `BYPASSRLS`; имеет право только явно
  выполнить `SET ROLE myretail_state_owner`;
- `myretail_api`: `LOGIN`, без DDL, ownership, `SUPERUSER`, `CREATEDB`, `CREATEROLE`, replication
  и `BYPASSRLS`.

Migration job подключается как `myretail_state_migrator`, проверяет `session_user`, выполняет
`SET ROLE myretail_state_owner` и только затем запускает Alembic. Для первичного создания
`public.alembic_version` owner временно получает `CREATE` на schema `public`; baseline revision
сразу отзывает это право. Роли не создаются и не повышаются самой migration.

`myretail_api` получает только `USAGE` state schema, DML на явно перечисленные state tables и
`SELECT` на Alembic revision. Все tenant business tables используют `ENABLE` + `FORCE ROW LEVEL
SECURITY`. Pre-auth rate-limit tables — документированное исключение без tenant RLS; они хранят
только keyed pseudonyms.

## Credentials и запуск

API config содержит только `MYRETAIL_STATE_DATABASE_URL` как `SecretStr`. Migration credentials
не входят в `Settings` и передаются только one-shot job через
`MYRETAIL_STATE_MIGRATION_DATABASE_URL`. URL, SQL parameters и credentials не выводятся в логи.

Migration artifact устанавливается из `requirements-migrations.lock` с `--require-hashes`, затем
из заранее собранного API wheel без dependency resolution. Migrations находятся внутри package,
поэтому job не зависит от source checkout.

Команды job:

```text
myretail-state-migrate upgrade head
myretail-state-migrate current
```

Expected revision задаётся package constant и не имеет environment override.

## Startup probe

При opt-in `state_backend=postgresql` один async pool создаётся в FastAPI lifespan и закрывается
при shutdown. Startup fail closed проверяет:

- exact Alembic revision;
- read/write connection;
- exact app role и отсутствие elevated attributes;
- отсутствие schema `CREATE` и table ownership у app role;
- `FORCE RLS` на всех tenant tables;
- rollback-only canary: запись/чтение своего tenant и невидимость строки после смены tenant context.

Tenant context задаётся только transaction-local через `set_config(..., true)`. Unset context не
видит tenant rows. Canary transaction всегда откатывается.

CI использует disposable PostgreSQL container с host `trust` только внутри изолированного runner;
это не production authentication model.

## Phase 6A.6 acceptance

Текущий package-owned head — `20260716_02`. Startup дополнительно проверяет отсутствие
role memberships у `myretail_api`, точный owner schema/tables, точный table inventory и
отсутствие elevated table grants. CI выполняет полный `upgrade → downgrade base → upgrade`
round-trip и автоматическую сверку утверждённого OpenAPI fingerprint.

Полная матрица evidence и граница Phase 6B описаны в
`docs/security/postgresql-foundation-acceptance.md`. Production остаётся fail closed.
