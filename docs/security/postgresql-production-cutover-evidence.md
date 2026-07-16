# PostgreSQL production cutover evidence — Phase 6B.3

Статус: внешний production gate. Пока default validator не принял манифест и решение о
traffic не стало `approved`, production traffic остаётся закрытым. Этот документ не заменяет
реальный managed PostgreSQL, provider backup/PITR, restore drill, monitoring или smoke.

## Evidence manifest

Шаблон находится в
[`production-evidence.example.json`](production-evidence.example.json). Он намеренно содержит
только placeholder URL/digests и `traffic_decision: blocked`. CI проверяет структуру шаблона:

```text
python services/api/scripts/validate_production_evidence.py \
  --mode template docs/security/production-evidence.example.json
```

Для cutover создаётся отдельный некоммитимый JSON из секретобезопасных идентификаторов и
стабильных HTTPS-ссылок на authoritative evidence. Production-проверка всегда запускается без
`--mode template`:

```text
python services/api/scripts/validate_production_evidence.py /secure/path/phase-6b3.json
```

Успешная проверка подтверждает полноту и внутреннюю согласованность манифеста, но не истинность
внешних attestations. Reviewer обязан открыть каждую evidence-ссылку. Ссылки с query/fragment,
credential-bearing URL, database DSN, token, password и private key отклоняются. Сам манифест не
должен содержать секреты и не коммитится в репозиторий.

## Порядок выполнения

1. Зафиксировать release commit и immutable digest API/migration images. Оба artifact должны
   относиться к одной версии, migration head — `20260716_05`.
2. Создать managed PostgreSQL в выбранных provider/project/region. Включить HA, encryption at
   rest и TLS hostname verification. Не записывать endpoint или credentials в evidence manifest.
3. Создать application/migration roles вне startup API, сохранить credentials в production secret
   manager, выполнить rotation test и приложить безопасную ссылку на audit event/version metadata.
4. Настроить provider backup retention и PITR. Восстановить provider backup в отдельный cluster,
   проверить migration revision, table inventory и reconciliation; production cluster не использовать
   как restore target.
5. Включить dashboard и проверить alerts: database unavailable, pool saturation, statement/lock
   timeout, migration mismatch, backup failure, replication lag и recovery age.
6. Выполнить migration job и `myretail-state-preflight`, затем развернуть минимум две API replicas
   в production-like окружении с отдельным ERPNext site/database.
7. Выполнить smoke для logout/session revocation, exact-once stock/purchases, полного POS lifecycle
   и recovery после restart. Зафиксировать reconciliation.
8. Назначить change, database и rollback owners, окно cutover и evidence решения. До первого
   PostgreSQL write допустим binary rollback. После первого write запрещены dual-write и SQLite
   fallback; применяется только PostgreSQL recovery/forward-fix.
9. Заполнить манифест стабильными ссылками без signed query, проверить default validator и провести
   ручной review всех ссылок. Только после review изменить `traffic_decision` на `approved` и повторить
   default validation.

## Fail-closed правила

- Отсутствие provider/project/cluster/region, HA, encryption, TLS или pre-provisioned roles блокирует
  traffic.
- Отсутствие rotation evidence, успешного provider backup, isolated restore/PITR evidence или любого
  обязательного alert блокирует traffic.
- Менее двух API replicas, production ERPNext вместо production-like QA site, неполный smoke или
  reconciliation failure блокируют traffic.
- Placeholder release SHA/digests, `example.invalid`, событие после `captured_at`, неизвестное поле,
  duplicate JSON key и неверный тип блокируют traffic.
- `--mode template` не является production approval и не должен использоваться в deployment gate.

## Текущая граница

На 16.07.2026 в доступном workspace/GitHub отсутствуют production environment, cloud/IaC context,
repository secrets/variables, deployments и подключение к managed PostgreSQL provider. Поэтому
внешний evidence manifest пока не может быть правдиво заполнен, а traffic остаётся закрытым.
