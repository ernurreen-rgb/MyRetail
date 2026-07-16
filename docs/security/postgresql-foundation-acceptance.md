# Phase 6A.6: PostgreSQL foundation acceptance и reconciliation

Этот документ является исполняемой матрицей приёмки Phase 6A. Репозиторные документы
по отдельным adapter остаются источником их внутренних инвариантов:

- `postgresql-state-foundation.md` — роли, pool, migrations и startup probe;
- `postgresql-shared-idempotency.md` — stock/purchases idempotency;
- `postgresql-pos-coordination.md` — POS leases, fencing и recovery claims;
- `postgresql-pos-projections.md` — POS materialization и totals;
- `postgresql-auth-rate-limit.md` — pre-auth rolling limiter и client-IP policy.

Продуктовое и архитектурное решение хранится в Notion. Этот файл не разрешает production
cutover и не заменяет Phase 6B runbook.

## Обязательные автоматические gates

### Role и ownership boundary

Startup и PostgreSQL acceptance обязаны доказать:

- API подключается ровно как `myretail_api`;
- app role — `LOGIN`, но не `SUPERUSER`, `CREATEDB`, `CREATEROLE`, replication или
  `BYPASSRLS`;
- app role не состоит ни в одной другой роли и не может получить owner/migrator через
  `SET ROLE`;
- schema и все state tables принадлежат `myretail_state_owner`;
- app role не имеет schema `CREATE`, ownership или elevated table privileges
  `TRUNCATE/REFERENCES/TRIGGER`;
- migrator и API credentials разделены; migration credentials отсутствуют в API Settings.

### Schema, RLS и pre-auth exception

- Alembic revision точно равна package constant `20260716_02`;
- table inventory точно равен утверждённым tenant + pre-auth tables;
- любая extra table блокирует startup до review и новой versioned migration;
- каждая tenant table имеет ровно одну ожидаемую permissive policy для `myretail_api`,
  `ENABLE RLS` и `FORCE RLS`;
- unset tenant context не видит строки; tenant A не видит tenant B;
- только `auth_rate_limit_buckets` и `auth_rate_limit_meta` являются pre-auth исключением
  без RLS;
- pre-auth exception хранит только HMAC pseudonyms, bucket table имеет DML, meta table —
  только `SELECT/UPDATE`, а `bucket_count` совпадает с фактическим числом bucket.

### Migration artifact

В disposable database CI выполняет:

1. clean `upgrade head`;
2. `current == 20260716_02 (head)`;
3. `downgrade base`;
4. отсутствие active revision;
5. повторный `upgrade head` и сверку current;
6. проверку обеих migration внутри собранного wheel.

API startup никогда не запускает migration и fail closed работает на empty/unmigrated или
revision-mismatched database.

### Adapter и concurrency evidence

PostgreSQL CI запускает один acceptance набор минимум с двумя независимыми pools:

- stock/purchases: same key и different key/same scope, fencing takeover, ambiguous
  recovery и одна reversal/materialization;
- POS coordination: active lease не продлевается чужим retry, stale owner не завершает
  intent, `FOR UPDATE SKIP LOCKED` recovery переживает restart;
- POS projections: open/close/sale materialization и shift totals применяются ровно один
  раз, tenant identity входит во все FK/unique boundaries;
- auth limiter: два rolling bucket, DB clock, hard capacity, queue bounds, exact
  clear/discard reservation, restart persistence и HMAC privacy.

### Contract и failure injection

- canonical OpenAPI JSON сравнивается с `services/api/openapi.sha256`; изменение hash
  требует отдельного утверждённого API decision;
- startup tests инъецируют revision mismatch, read-only connection, missing grants,
  permissive policy, role membership, owner drift, extra table, pre-auth meta drift и
  unavailable database;
- безопасные startup errors не содержат database URL, credentials или SQL parameters;
- Ruff, API tests, Bandit, supply-chain drift/audit, web lint/typecheck/tests/build,
  Gitleaks и CodeQL обязательны.

## Live QA Phase 6A

В 6A.2–6A.5 отдельно подтверждены два API процесса на одном PostgreSQL и локальном
ERPNext для concurrent stock cancel, POS sale/materialization и shared login threshold.
Phase 6A.6 повторно проверяет два process-scoped pool и startup/health. Эти доказательства
не являются production readiness: окружение disposable, TLS и managed backup не моделируются.

## Что остаётся до Phase 6B

Production по-прежнему обязан не стартовать. Phase 6B требует отдельной контролируемой
работы и evidence:

- утверждённый managed PostgreSQL endpoint с `verify-full` TLS и secret manager;
- pre-provisioned production roles без ручного privilege drift;
- backup/PITR policy и успешный restore drill;
- one-shot migration job из проверенного wheel;
- monitoring/alerts для availability, pool saturation, lock/statement timeouts, recovery
  age, migration mismatch, backup failure и replication lag;
- maintenance/cutover plan: остановка старых writers, одна версия на всех replicas, запрет
  dual-write/fallback-read и запрет rollback к SQLite после первого PostgreSQL write;
- production-like multi-process live QA и reconciliation с ERPNext;
- формальная фиксация rollback/forward-fix criteria и операционного владельца.

Отдельный readiness HTTP endpoint, session revocation, tenant-to-ERPNext routing и Returns
integrity не входят в 6A.6. Они остаются отдельными утверждёнными решениями/фазами.
