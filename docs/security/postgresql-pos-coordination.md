# PostgreSQL POS coordination — adapter contract

Актуальные архитектурные решения и отчёты хранятся в Notion. Этот файл фиксирует
исполняемый контракт Phase 6A.3 рядом с кодом.

## Граница этапа

- Phase 6A.3 добавляет shared POS idempotency и workflow-intent coordination для
  development/test и CI.
- Public HTTP API, schemas, status codes и error codes не меняются.
- FastAPI lifespan создаёт один process-scoped coordination adapter на процесс.
- POS request path пока продолжает использовать атомарный SQLite `POSStore`.
  Переключение только intents на PostgreSQL создало бы split-brain с SQLite
  projections, поэтому оно запрещено до Phase 6A.4.
- Production остаётся fail closed до controlled cutover Phase 6B.

## POS idempotency

PostgreSQL adapter использует общий `idempotency_records` с внутренним
`namespace=pos`. Полная identity записи состоит из tenant, POS operation,
principal и `Idempotency-Key`; повтор с другим request hash отклоняется.

- Lease исчисляется по `clock_timestamp()` PostgreSQL.
- Begin сериализован transaction-scoped advisory lock и unique constraint.
- Повтор, не получивший ownership, не продлевает lease активного владельца.
- Takeover увеличивает fencing token.
- Complete и release требуют совпадения tenant, identity, request hash, owner,
  fencing token и processing state.
- Stale owner не может записать ответ или удалить запись нового владельца.

SQLite adapter сохраняет тот же внешний async-контракт. Синхронные операции
выполняются через ограниченный `asyncio.to_thread`, поэтому event loop не
блокируется. Неуспешный повтор также больше не продлевает чужой lease.

## Workflow intents и recovery

Активный scope защищён advisory lock и partial unique index. Для `open_shift`
дополнительно блокируется tenant/cashier scope. Одинаковая операция получает
существующий intent; конфликтующая операция получает существующий внутренний
`SHIFT_CHANGED` без создания второго intent.

Все переходы после ERP-вызова условны по tenant, owner UUID, fencing token и
допустимому state. Recovery worker выбирает только due `recovery_required`
intents через `FOR UPDATE SKIP LOCKED`; разные pools не забирают одну запись.
Повтор после рестарта runtime получает новый owner и увеличенный fencing token.
`last_error_code` принимает только ограниченный ASCII machine-code; произвольный
exception text, credentials и upstream response в recovery metadata отклоняются.

## Tenant isolation и транзакции

Каждая PostgreSQL transaction сначала устанавливает transaction-local tenant
context через `set_config(..., true)`. RLS остаётся обязательной границей даже
при наличии tenant predicates в SQL. Lock keys включают tenant и сортируются,
чтобы одинаковый набор locks имел стабильный порядок.

## Условия следующего переключения

Phase 6A.4 должна реализовать PostgreSQL projection repositories и атомарную
materialization intent + projection в одной DB transaction. До этого:

- POS routers/services не получают PostgreSQL coordination dependency;
- local dev/test workflow остаётся на SQLite;
- production PostgreSQL state backend не разрешает старт API;
- dual-write или скрытая миграция данных не выполняются.

Acceptance покрывает SQLite/PostgreSQL parity, fencing stale owner, отсутствие
lease extension чужим retry, tenant isolation, два независимых pools,
конкурентный `SKIP LOCKED` claim, process-scoped lifespan и recovery после
пересоздания runtime.
