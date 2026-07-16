# Security Phase 8A — isolated tenant → ERPNext site boundary

## Назначение

MR-SEC-001A разрешает production только в изолированной topology:

- один MyRetail API replica group обслуживает ровно один tenant;
- этот deployment имеет один фиксированный ERPNext site/database;
- все replicas одного deployment используют одну tenant identity, один route version и один
  набор site-local service credentials;
- HTTP header, path, query и body никогда не выбирают ERPNext origin или credentials;
- shared MyRetail API routing остаётся запрещён до MR-SEC-001B/C.

Public HTTP/OpenAPI и PostgreSQL schema этой фазой не меняются.

## Server-side contract

Production обязан явно передать следующие settings через утверждённый secret/config
delivery mechanism:

- `MYRETAIL_TENANCY_MODE=isolated_site`;
- `MYRETAIL_TENANT_ID` — immutable UUID конкретного tenant; nil UUID и local development
  UUID в production запрещены;
- `MYRETAIL_TENANT_SLUG` — lowercase ASCII label длиной до 63 символов;
- `MYRETAIL_TENANT_ROUTE_VERSION` — monotonic positive integer;
- `MYRETAIL_AUTH_ISSUER` и `MYRETAIL_AUTH_AUDIENCE`;
- `MYRETAIL_AUTH_SECRET` длиной не меньше 32 bytes и отдельный от ERPNext credentials;
- `MYRETAIL_ERPNEXT_BASE_URL` — только fixed HTTPS origin без userinfo, path, query и
  fragment;
- `MYRETAIL_ERPNEXT_API_KEY` и `MYRETAIL_ERPNEXT_API_SECRET` отдельного site-local
  service user;
- PostgreSQL controlled-enablement settings из
  [postgresql-controlled-enablement.md](postgresql-controlled-enablement.md).

Значения secrets, URL с credentials, bearer tokens и cookies запрещено помещать в Git,
Notion, логи и отчёты. В документации фиксируются только имена параметров и безопасные
sanitized labels.

Local development/test сохраняет default `isolated_site`, локальный UUID и HTTP ERPNext
origin. Production defaults не считаются implicit approval: обязательные route/auth/ERP
поля должны быть заданы явно, иначе API и packaged preflight завершаются fail closed.

## Startup validation

До создания FastAPI application выполняются проверки:

1. Production state backend — только controlled PostgreSQL с `verify-full`.
2. Tenancy mode — только `isolated_site`; shared/default route отсутствует.
3. Tenant slug canonical, tenant UUID не nil и не local development default, route version
   положительный.
4. Issuer/audience — explicit printable ASCII identifiers без whitespace/control symbols.
5. ERPNext URL — однозначный HTTPS origin; embedded credentials, path/query/fragment и
   malformed host запрещены.
6. Auth и ERPNext credentials присутствуют; auth secret имеет минимум 32 bytes и не
   совпадает с ERPNext key/secret.
7. Создаётся immutable process-scoped `IsolatedTenantRoute`. Копии mutable settings/maps
   после startup не могут изменить его origin или credentials.

Route snapshot и ERP profile имеют redacted `repr`; raw endpoint и credentials не должны
попасть в diagnostic exception.

## Request и token boundary

Login сначала сравнивает request tenant с fixed route slug и только затем вызывает
ERPNext authentication. Unknown/SSRF-style tenant не инициирует ERP request.

Access JWT сохраняет прежний public transport/response, но authorization policy version 3
добавляет обязательную внутреннюю привязку:

- issuer и audience;
- immutable tenant UUID и public slug;
- route version.

Каждый authenticated request проверяет signature/expiry/global policy и все route claims.
Token другого deployment, старого route version или legacy token без claims получает safe
`401`. Signed tenant/header mismatch внутри текущего deployment сохраняет существующий safe
`403`. `TenantContext` и `LoginResponse` не расширяются.

## Deployment

1. Создать отдельный ERPNext site/database и отдельного least-privilege service user.
2. Зафиксировать безопасные tenant/site labels, tenant UUID и начальный route version в
   контролируемом deployment inventory. Не фиксировать secret values.
3. Доставить API/state/ERP credentials через secret mechanism; не передавать их в image.
4. Выполнить one-shot migration job и `myretail-state-preflight`.
5. Запустить минимум две одинаково настроенные API replicas на одном PostgreSQL.
6. Проверить health, login, products, POS, stock и purchases через fixed site.
7. Отправить token/header другого tenant и убедиться, что ERP request не выполняется.
8. Проверить sanitized logs: нет endpoint credentials, raw bearer/cookie и secret values.
9. Только после reconciliation открыть traffic.

Replica с другим tenant UUID/slug/origin/route version не должна входить в тот же replica
group. Shared load balancer без tenant-dedicated routing запрещён.

## Rotation и suspension

Любое изменение ERP route, service credentials или tenant suspension выполняется
controlled deployment:

1. закрыть новый traffic или вывести tenant deployment из балансировщика;
2. заменить secret/profile server-side;
3. увеличить `MYRETAIL_TENANT_ROUTE_VERSION`;
4. при auth-secret incident также заменить auth secret;
5. одновременно развернуть все replicas и повторить preflight/smoke;
6. подтвердить, что старый JWT получает `401`, а новый ERP call идёт только в approved site;
7. вернуть traffic после reconciliation.

Fallback на старый/default site отсутствует. При недоступном route/site сервис остаётся fail
closed. Production rollback на binary до MR-SEC-001A запрещён: такой binary не проверяет
tenant UUID/route claims. После первого production rollout используется forward-fix либо
восстановление текущего compatible artifact.

## Reconciliation evidence

Для каждого deployment фиксируются без секретов:

- release/commit и image digest;
- sanitized tenant/site labels, tenant UUID fingerprint и route version;
- количество API replicas и PostgreSQL revision;
- результаты preflight, CI/CodeQL и OpenAPI fingerprint;
- HTTP statuses negative tenant/token tests;
- ERP document IDs/markers для smoke и подтверждение, что recovery искал их только в
  approved site;
- timestamp и владельцы rollout/rollback.

## Граница MR-SEC-001A

MR-SEC-001A закрывает finding только для dedicated API group + separate ERPNext
site/database на tenant. Existing PostgreSQL business-state key остаётся canonical slug в
этой topology; request не может выбрать другой slug. Registry UUID/composite shared-routing
schema, per-route pools и secret-manager adapter относятся к MR-SEC-001B/C и не вводятся
скрыто.

До MR-SEC-001B/C запрещены:

- один API process/replica group для нескольких tenant routes;
- shared ERPNext site или Company-per-tenant как security boundary;
- route URL/secret reference из request data;
- fallback route;
- reuse service credentials между sites.
