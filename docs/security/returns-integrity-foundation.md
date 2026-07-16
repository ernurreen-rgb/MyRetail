# Phase 7A — durable foundation целостности возвратов

Дата: 16.07.2026.

## Назначение

Phase 7A добавляет внутренний durable foundation для последующего исправления
MR-SEC-018…021. Публичные endpoints, поля ответов, HTTP-коды и пользовательская
семантика кассовой смены в этой фазе не меняются.

## Модель данных

Migration `20260716_03` добавляет две tenant-scoped append-only таблицы:

- `workflow_intent_aliases` неизменно связывает дополнительный Idempotency-Key с
  canonical workflow intent. Это позволит recovery операции возврата при retry с
  другим ключом без создания второго ERPNext документа;
- `pos_shift_cash_events` хранит exact-once события opening/sale/return/return_cancel.
  Уникальность `(tenant_id, source_type, source_id, effect_kind)` не допускает
  повторного денежного эффекта одного business source.

Обе таблицы имеют `ENABLE RLS` и `FORCE RLS`, tenant policy и только
`SELECT/INSERT` для `myretail_api`. `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES` и
`TRIGGER` запрещены. Foreign keys с `ON DELETE RESTRICT` защищают canonical intent
и shift от удаления при наличии immutable history.

SQLite сохраняет dev/test parity: alias и cash-event identities имеют те же
unique/conflict правила, а существующие локальные базы получают
`cash_returns_total = 0.00` без изменения текущего поведения. Production guard
по-прежнему запрещает local SQLite.

## Транзакционная граница

Phase 7A намеренно не подключает standalone adapters к return service. В Phase 7B
создание возврата обязано одной database transaction:

1. проверить fencing token и canonical durable intent;
2. materialize return projection;
3. append exact-once отрицательный cash event для cash refund;
4. завершить intent.

Подключать `append_cash_event` отдельным коммитом до такой materialization нельзя:
это создало бы окно между projection, ledger и intent. Аналогичная единая
transaction требуется для cancel в Phase 7C.

## Совместимость

- публичный `sales_total` остаётся gross sales;
- `cash_returns_total` пока равен нулю на рабочих service paths;
- `expected_cash` вычисляется как `opening_cash + sales_total - cash_returns_total`,
  поэтому до Phase 7B результат идентичен прежнему;
- новых API-полей и error codes нет; OpenAPI fingerprint должен остаться прежним.

## Acceptance

Обязательные проверки Phase 7A:

- migration `upgrade head → downgrade base → upgrade head`;
- packaged head и startup preflight равны `20260716_03`;
- точный table inventory, RLS и least-privilege grants;
- alias attach/find/replay/conflict на SQLite и PostgreSQL;
- конкурентный append одного cash event через два независимых repository/pool:
  один `created`, один replay, одна строка;
- cross-tenant чтение возвращает пустой результат;
- изменённый amount и неверный sign завершаются conflict;
- wheel и production-like TLS backup/restore artifact drill включают migration 03;
- Ruff, Bandit, полный API/web regression и `git diff --check`.

Phase 7A не закрывает MR-SEC-018…021 самостоятельно: она создаёт проверенный
foundation. Service-path fixes и live QA выполняются в Phase 7B–7E.
