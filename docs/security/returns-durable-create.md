# Security Phase 7B — durable create return

## Граница изменения

Phase 7B переводит `POST /pos/returns` на durable workflow PostgreSQL/SQLite parity без
изменения HTTP endpoint, request/response schemas, публичных состояний и error codes.
Утверждённый `sales_total` остаётся gross submitted sales. Внутренний
`cash_returns_total` и append-only cash ledger не публикуются в API.

Cash return для закрытой исходной смены fail closed до отдельного additive settlement
contract: Owner/Admin получает существующий `409 POS_OPENING_OUTDATED`, ERPNext при этом
не вызывается.

## Durable create workflow

1. Сервер проверяет sale/shift/access и строит нормализованный immutable snapshot:
   `return_id`, строки/цены/refund total, actor, shift/register, cash-event UUID и время.
2. `workflow_intent` резервируется в общем scope `shift:<shift_id>`. Его
   `external_marker` является единственным стабильным ключом ERP side effect.
3. Первый `Idempotency-Key` и совместимые ключи активных повторов связываются с canonical
   intent через immutable aliases. Несовместимый активный запрос получает safe `409`.
4. После ERP commit одна fenced-транзакция записывает submitted return projection,
   отрицательный cash event, пересчитывает `cash_returns_total`/`expected_cash` из ledger
   и переводит intent сразу в terminal `completed`.
5. `completed` освобождает coordination scope. Новый key после terminal может создать
   следующий самостоятельный partial return; alias уже выполненного key возвращает
   canonical result.

Timeout/lost response не кэшируется как terminal `5xx`. Intent переходит в
`recovery_required`; retry ищет ERP Sales Invoice только по stable canonical marker и
материализует тот же snapshot. Stale fencing token не может записать projection, cash
event или shift totals.

## Совместимость и backfill

Migration `20260716_04` перед cutover восстанавливает append-only cash events для старых
`submitted`/`cancel_pending`/`cancelled` returns и пересчитывает только открытые смены.
Closed shift snapshots задним числом не меняются. Legacy `pending_recovery` после
подтверждения старого ERP marker материализуется вместе с cash effect; для закрытой смены
он остаётся fail closed и требует утверждённого settlement flow.

Все tenant tables используют `FORCE RLS`. Cross-tenant data migration от table owner без
tenant context иначе видит ноль строк. Поэтому `20260716_04` в одной PostgreSQL
транзакции временно снимает только `FORCE` с `pos_returns`, `pos_shifts` и
`pos_shift_cash_events`, выполняет owner-only backfill/reconciliation и восстанавливает
`FORCE RLS`. Любая missing/mismatched cash identity или отрицательный outstanding total
прерывает migration; PostgreSQL откатывает и данные, и DDL. Runtime app role и его
`SELECT/INSERT` grants для immutable ledger не расширяются.

Downgrade `20260716_04 → 20260716_03` намеренно не удаляет operational cash events:
append-only audit data не уничтожается. Полный downgrade до base удаляет таблицу в
предыдущей schema migration.

## Открытая граница после 7B

Phase 7B закрывает durable create/recovery и exact-once create cash effect. Durable cancel,
компенсирующий `return_cancel` cash event и полная сериализация cancel с create/sale/close
остаются в Phase 7C/7D. До их merge MR-SEC-019…021 нельзя считать полностью закрытыми.

## Обязательная проверка

- SQLite и PostgreSQL two-pool concurrent create с разными keys: один ERP return, одна
  projection, один cash event, два aliases;
- lost response и повторный recovery без второго ERP create;
- legacy pending recovery и backfill;
- stale fencing atomic rollback;
- tenant isolation/RLS и fail-closed migration collision;
- OpenAPI fingerprint без изменений, полный `ruff`, `bandit`, API/web tests, build,
  migration round-trip и live QA через два API-процесса + локальный ERPNext.
