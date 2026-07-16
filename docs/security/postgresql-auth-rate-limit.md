# Phase 6A.5: общее состояние auth rate limit в PostgreSQL

Phase 6A.5 переводит защиту `POST /auth/login` на единый асинхронный
process-scoped repository. SQLite остаётся только адаптером для локальной разработки и
быстрых тестов. PostgreSQL adapter использует две общие для всех API replicas очереди:

- client bucket по IP клиента;
- login bucket по tenant, IP клиента и нормализованному login.

Оба ключа — domain-separated HMAC-SHA-256. Raw tenant, login и IP в state database не
записываются. Для PostgreSQL обязателен отдельный секрет
`MYRETAIL_AUTH_RATE_LIMIT_SECRET` длиной не менее 32 байт; использовать значение JWT
секрета повторно запрещено. Значения секретов и database URL не должны попадать в логи.

## Транзакционные инварианты

- Время окна и `Retry-After` определяет PostgreSQL `clock_timestamp()`.
- Оба bucket блокируются в детерминированном порядке внутри короткой транзакции.
- Заблокированный запрос не добавляет timestamp в очередь.
- Singleton row `auth_rate_limit_meta` блокируется через `SELECT ... FOR UPDATE` и
  атомарно защищает hard capacity от гонок между replicas.
- `clear` удаляет login history после успешного входа и снимает только client reservation
  текущего запроса.
- `discard` снимает только client/login reservations текущего запроса после сбоя
  инфраструктуры или конфигурации.
- Ошибка state database приводит к `503`; аутентификация не продолжается без limiter.

Pre-auth таблицы являются утверждённым исключением из tenant RLS: global client bucket
нужен до проверки tenant. Исключение ограничено двумя таблицами, keyed pseudonyms и
отдельными DML grants. Business-state таблицы продолжают использовать FORCE RLS.

## Политика client IP и trusted proxy

По умолчанию `MYRETAIL_AUTH_CLIENT_IP_MODE=direct`: приложение использует только
непосредственный ASGI peer и игнорирует `X-Forwarded-For`.

Trusted proxy включается только одновременно с:

1. `MYRETAIL_AUTH_CLIENT_IP_MODE=trusted_proxy`;
2. непустым JSON-массивом CIDR в `MYRETAIL_AUTH_TRUSTED_PROXY_CIDRS`;
3. Uvicorn, запущенным с отключённой собственной подменой peer через
   `--no-proxy-headers`, чтобы цепочку разрешал ровно один компонент;
4. сетевой политикой, запрещающей прямой обход доверенного reverse proxy.

Resolver принимает `X-Forwarded-For` только от peer из allowlist, ограничивает цепочку
32 адресами и идёт справа налево до первого недоверенного hop. Для недоверенного peer,
пустой или некорректной цепочки используется direct peer. Пустой allowlist, неверный CIDR
или CIDR при direct mode блокируют startup.

## Граница фазы

Публичные HTTP schemas, status/error contracts и OpenAPI не меняются. Автоматическая
миграция при startup не выполняется. Schema для bucket/meta создана baseline migration
6A.1; additive migration `20260716_02` сужает grants singleton meta до `SELECT/UPDATE`.
Production остаётся fail closed до контролируемого Phase 6B cutover.
