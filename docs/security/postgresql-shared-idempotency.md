# PostgreSQL shared idempotency — adapter contract

Актуальное архитектурное решение и отчёты хранятся в Notion. Этот файл фиксирует исполняемый
контракт Phase 6A.2 рядом с кодом.

## Граница этапа

- Stock и purchases продолжают использовать один общий idempotency key-space.
- SQLite остаётся default adapter для local development и unit tests.
- PostgreSQL adapter включается только через opt-in `state_backend=postgresql` в development/test.
- Production остаётся fail closed до controlled Phase 6B.
- Public HTTP schemas, status codes и error codes не меняются.

## Canonical record и aliases

Первый запрос создаёт canonical `idempotency_records` row. Resource-scoped операция также создаёт
alias для исходного key. Повтор с другим `Idempotency-Key`, но тем же tenant, request hash и scope,
создаёт только новый alias и возвращает canonical storage key.

Для общего stock/purchases key-space PostgreSQL adapter использует package-owned внутренние
идентификаторы `namespace=stock_purchases`, `operation_key=shared`, `principal_key=''`. Они не
приходят из HTTP request и не позволяют развести один общий key-space на независимые операции.

## Конкуренция и recovery

- Begin выполняется в короткой транзакции после transaction-local tenant context.
- DB clock задаёт `lease_until`.
- Transaction-scoped advisory locks сериализуют canonical key и protected scope между pools;
  unique constraints остаются окончательным DB invariant.
- Lease takeover переводит запись в `recovery_required` и атомарно увеличивает fencing token.
- Complete, recovery mark и release являются conditional mutation по tenant, canonical key,
  request hash, fencing token и допустимому state.
- Stale owner не может complete, release или изменить recovery lease.
- Release canonical pending row каскадно удаляет aliases; completed replay state не удаляется.

## Tenant isolation

Каждая repository transaction сначала выполняет transaction-local `set_config(..., true)`. RLS
скрывает rows и aliases другого tenant; одинаковые key/scope у двух tenant являются независимыми и
не раскрывают наличие операции соседнего tenant.

SQLite adapter выполняет существующие синхронные операции через ограниченную process-scoped
worker capacity, поэтому blocking SQLite calls больше не выполняются прямо в async router path.
Долгие wait-операции используют не более `worker_limit - 1` слотов: минимум один слот всегда
остаётся для `complete`, recovery mark или release и waiters не могут заблокировать владельца.
