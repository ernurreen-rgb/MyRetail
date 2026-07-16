# PostgreSQL POS projections — Phase 6A.4

Актуальные решения и отчёты проекта хранятся в Notion. Этот документ фиксирует
исполняемый контракт Phase 6A.4 рядом с кодом.

## Граница этапа

- POS request path для development/test использует один process-scoped repository,
  выбранный в FastAPI lifespan: SQLite либо PostgreSQL.
- Переключение coordination отдельно от projections запрещено: idempotency,
  workflow intent и его POS projection используют один backend.
- Public HTTP API, OpenAPI schemas, status codes и error codes не меняются.
- Скрытого dual-write и автоматического переноса данных SQLite → PostgreSQL нет.
- Production по-прежнему fail closed до утверждённого controlled cutover Phase 6B.

## Repository contract

`POSService` работает только с асинхронным state repository. Для локального SQLite
используется compatibility adapter с ограниченным `asyncio.to_thread`, поэтому
синхронные SQLite-вызовы не блокируют event loop. PostgreSQL adapter обслуживает:

- shifts и их денежные totals;
- held receipts;
- sales и связь с ERPNext Sales Invoice;
- текущую projection-семантику Returns;
- POS idempotency и workflow intents через coordination adapter того же process/pool.

SQLite остаётся допустимым только для local development/test. Создание repository
на каждый HTTP request запрещено; lifespan создаёт один экземпляр на процесс.

## Атомарная materialization

Для `open_shift`, `close_shift` и `create_sale` PostgreSQL выполняет в одной DB
transaction:

1. проверку tenant, owner UUID, fencing token, operation и допустимого intent state;
2. блокировку workflow intent и затрагиваемой POS projection через `FOR UPDATE`;
3. insert/update projection;
4. изменение shift totals и completion held receipt, если применимо;
5. перевод workflow intent в `materialized` с ERP document/result id.

Если transaction откатывается, ни projection, ни totals, ни intent transition не
сохраняются. Stale owner не может материализовать результат после lease takeover.
Повтор найденного ERP invoice не увеличивает totals второй раз.

После формирования HTTP response fenced completion idempotency record переводит
соответствующий `materialized` workflow intent в `completed` в той же PostgreSQL
transaction. Это освобождает shift scope для следующей операции.

## Tenant isolation и запросы

Каждая PostgreSQL transaction устанавливает transaction-local tenant context через
`set_config(..., true)`. RLS остаётся обязательной границей вместе с явными tenant
predicates. Optional filters имеют фиксированные SQL clauses и typed bind parameters;
request values не интерполируются в SQL.

## Held receipts и Returns

Схема foundation разрешает held status только `open/completed`. Поэтому публичный
DELETE удаляет open held row физически; публичное поведение совпадает с SQLite —
последующий GET возвращает 404. Продажа переводит использованный held receipt в
`completed` атомарно с sale projection.

Returns в этом этапе сохраняют существующий Sprint 6 контракт и поведение SQLite:
pending recovery, submit, idempotent replay, cancel claim/release/cancelled и history
filters. Phase 6A.4 не вводит новые правила Returns и не реализует будущую Phase 7
cash-return accounting. Поле `cash_returns_total` не меняется до отдельного решения.

## Проверяемые инварианты

- два API процесса с разными PostgreSQL pools видят общий POS state;
- параллельный одинаковый sale с одним Idempotency-Key вызывает ERP один раз;
- создаётся одна sale projection, а `sales_total/expected_cash` увеличиваются один раз;
- после completion следующий sale по той же смене разрешён;
- consumed held receipt больше не доступен как open;
- stale fencing owner не оставляет частичную projection;
- recovery после пересоздания runtime материализует существующий ERP result один раз;
- cross-tenant чтение не возвращает projection;
- SQLite POS regression остаётся совместимой.

## Условия Phase 6B

Production cutover требует отдельного утверждённого runbook: инвентаризация и перенос
существующего SQLite state либо явное решение о пустом старте, pre-cutover backup,
maintenance window, reconciliation с ERPNext, rollback criteria и наблюдаемость.
До этого production startup с PostgreSQL state backend остаётся заблокированным.
