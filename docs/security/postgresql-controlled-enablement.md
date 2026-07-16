# PostgreSQL controlled enablement — Phase 6B.1

Статус: runtime/preflight gate. Этот документ не подтверждает готовность production
инфраструктуры и не закрывает MR-SEC-010 без внешних evidence gates Phase 6B.2–6B.3.

Production state preflight также проверяет fixed isolated tenant → ERPNext boundary из
[tenant-isolated-site-boundary.md](tenant-isolated-site-boundary.md). Controlled PostgreSQL
не разрешает shared tenant routing и не заменяет отдельную ERPNext site/database boundary.

## Граница 6B.1

Production API может перейти от безусловного запрета к startup-проверке PostgreSQL только
при явном внутреннем параметре:

```text
MYRETAIL_ENVIRONMENT=production
MYRETAIL_STATE_BACKEND=postgresql
MYRETAIL_STATE_PRODUCTION_ENABLEMENT=controlled
MYRETAIL_STATE_POSTGRES_SSL_MODE=verify-full
```

`controlled` — authorization latch, а не attest-флаг. Он не доказывает наличие managed
PostgreSQL, secret manager, backup/PITR, restore drill, monitoring или решения о cutover.
Production SQLite запрещён независимо от значения latch.

API startup дополнительно проверяет наличие URL без его логирования, dedicated HMAC secret,
точную Alembic revision, application role и membership, отсутствие DDL/owner/BYPASSRLS,
точный inventory/ownership/grants, tenant RLS policies и read/write tenant canary. Migration
автоматически на startup не запускается. Отдельный HTTP readiness endpoint не добавляется.

## Разделение ролей и TLS migration job

Migration job получает отдельный URL только через
`MYRETAIL_STATE_MIGRATION_DATABASE_URL`; этого поля нет в API Settings. Для каждого запуска
обязательны явные:

```text
MYRETAIL_ENVIRONMENT=development|test|production
MYRETAIL_STATE_MIGRATION_SSL_MODE=disable|require|verify-ca|verify-full
```

В production разрешён только `verify-full`. При частном CA путь задаётся через
`MYRETAIL_STATE_MIGRATION_SSL_ROOT_CERT_PATH`. Отсутствующий/невалидный trust file приводит
к fail-closed завершению. URL, password и SQL parameters не печатаются.

Migration job выполняется до API rollout:

```text
myretail-state-migrate upgrade head
myretail-state-migrate current
```

Ожидаемый head принадлежит package и не задаётся environment-переменной.

## Preflight

После migration и до открытия traffic запускается тот же artifact и тот же environment,
которые предназначены API:

```text
myretail-state-preflight
```

Успешный preflight создаёт application pool, выполняет полный startup contract и закрывает
pool. Ошибки выводятся только в нормализованной безопасной форме. Preflight не выполняет
migration и не заменяет smoke/live QA.

Platform readiness в 6B.1 определяется успешным startup процесса. Изменение HTTP API для
отдельного readiness endpoint остаётся вне scope.

## Порядок rollout и rollback

1. Проверить managed endpoint, TLS trust, secret injection и pre-provisioned roles.
2. Выполнить one-shot migration job отдельной migration role.
3. Запустить `myretail-state-preflight` application role.
4. Остановить старые API instances, развернуть одну версию на всех replicas.
5. Выполнить multi-process smoke/concurrency QA и reconciliation до открытия traffic.

До первого PostgreSQL write допустим rollback binary. После первого write fallback на
SQLite запрещён: при DB incident сервис остаётся fail closed, восстанавливается PostgreSQL.

## Что остаётся открытым

- production API image/entrypoint и one-shot wiring;
- TLS deployment drill и backup/restore rehearsal на disposable environment;
- реальный managed PostgreSQL endpoint и secret manager;
- provider backup/PITR и зафиксированный restore evidence;
- monitoring/alerts и владельцы cutover/rollback;
- production-like multi-process QA и формальное решение об открытии traffic.

Эти пункты закрываются только Phase 6B.2–6B.3 и не могут быть подменены latch или локальным
тестом.
