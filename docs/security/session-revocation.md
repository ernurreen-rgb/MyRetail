# MR-SEC-002: отзыв серверных сессий

JWT остаётся короткоживущим подписанным access token, но больше не является
самодостаточным разрешением на запрос. Каждый production-запрос после проверки
подписи и claims подтверждает активную сессию и principal в PostgreSQL. Положительный
кэш и process-local allowlist не используются; недоступность state-хранилища даёт 503.

## Claims и состояние

Token содержит `jti` (UUID сессии), `sub` (нормализованный ERPNext email),
`principal_id`, `auth_epoch`, `route_version`, tenant identity, issuer/audience,
roles, `iat` и `exp`. Legacy tokens без полного набора claims отклоняются.

Migration `20260716_05` добавляет tenant-scoped таблицы:

- `auth_principals`: стабильный principal, нормализованный email, монотонный
  `auth_epoch`, `disabled_at` и `revoked_before`;
- `auth_sessions`: UUID сессии, snapshots epoch/route, DB timestamps и метаданные
  отзыва.

Обе таблицы используют `ENABLE` + `FORCE RLS`, тот же transaction-local tenant
context и минимальный DML grant приложения. JWT, подписи, cookies, пароли, API keys,
headers и raw IP в state не сохраняются.

## API и граница отзыва

- `POST /auth/logout` проверяет подпись token отдельным путём, не зависящим от
  активности сессии, и идемпотентно отзывает только текущий `jti`. Повторный вызов
  возвращает 204.
- `POST /auth/sessions/revoke` доступен только активным Owner/Admin. Тело содержит
  только `email`; ответ всегда 204 независимо от существования principal. Транзакция
  повышает `auth_epoch` и отзывает все его активные сессии.

Новый запрос, session-check которого начался после commit отзыва, получает 401.
Запрос, уже прошедший авторизацию до commit, может завершиться. Durable business
intent, принятый до отзыва, сохраняет существующие recovery-гарантии; новые intents
отозванная сессия создавать не может.

## Web BFF logout

Same-origin/CSRF-проверка сохраняется. BFF читает HttpOnly token и tenant cookies на
сервере, вызывает API logout и очищает cookies только после 204 или 401. При timeout,
503 или другом неподтверждённом результате cookies сохраняются, клиент получает 503.
Token не отражается в ответе и не логируется.

## Проверки

Acceptance покрывает отзыв одной сессии без влияния на вторую, массовый отзыв,
повторный logout, отсутствие existence leak, fail-closed state outage, два экземпляра
приложения на общем state, tenant A/B/unset RLS, migration round-trip, OpenAPI
fingerprint и BFF cookie ordering.
