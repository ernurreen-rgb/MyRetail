# Security Phase 7D — frozen shift close

Дата: 16.07.2026.

## Граница изменения

Phase 7D закрывает MR-SEC-021: закрытие POS-смены теперь выполняется только по
неизменяемому snapshot, зарезервированному до ERPNext side effect. Публичный endpoint
`POST /pos/shifts/{shift_id}/close`, request/response schema, утверждённые error codes и
storage schema не меняются. Новая migration не нужна; packaged PostgreSQL head остаётся
`20260716_04`.

Изменение одинаково действует для SQLite dev/test и PostgreSQL production backend. Оно
не расширяет ранее утверждённую границу: cash return и его cancel по-прежнему допустимы
только для открытой исходной смены.

## Frozen snapshot

Атомарная reservation `close_shift` сохраняет в durable intent:

- полный публичный snapshot открытой `Shift`, включая register, warehouse, cashier,
  opening/sales/expected totals и `updated_at`;
- отдельный cash snapshot: `opening_cash`, `sales_total`, `cash_returns_total` и
  `expected_cash`;
- close snapshot: tenant, shift, cashier, ожидаемый `updated_at`, actual cash,
  difference и серверный `closed_at`;
- stable external marker, scope `shift:<shift_id>`, business hash, lease и fencing token.

Перед reservation база под общим shift scope проверяет status, optimistic `updated_at`,
отсутствие held receipts и отсутствие другой активной sale/create-return/cancel-return/
close операции. ERPNext POS Closing создаётся только из сохранённого `Shift`, а не из
повторно прочитанной или оставшейся в памяти mutable projection.

Snapshot проходит fail-closed проверку целостности:

```text
expected_cash = opening_cash + sales_total - cash_returns_total
difference    = actual_cash - expected_cash
```

Все денежные значения обязаны быть конечными decimal-строками с точностью не более двух
знаков; отрицательная кассовая разница допустима, отрицательные totals — нет. Shift в
snapshot должна быть открытой, принадлежать тому же cashier/shift и не содержать close
полей.

## Материализация и recovery

После подтверждённого ERP Closing одна fenced transaction в SQLite или PostgreSQL:

1. проверяет owner/lease/fencing token и tenant intent;
2. блокирует текущую shift projection;
3. повторно сверяет `updated_at` и все четыре cash totals с frozen snapshot;
4. записывает status `closed`, actual/difference, ERP Closing id и timestamps;
5. переводит durable intent в terminal `completed`.

Если ERP Closing уже создан, но projection изменилась или временно повреждена,
materialize не возвращает terminal business conflict. Intent переводится в
`recovery_required`, API отвечает существующим `503 ERPNEXT_RECOVERY_PENDING`, а retry
ищет Closing по тому же external marker. После восстановления projection используется
существующий ERP документ; второй POS Closing не создаётся.

Новый takeover intent в состоянии `reserved` может выполнить ERP side effect только при
наличии валидного frozen snapshot. Legacy `reserved` без snapshot получает safe `503` и
не вызывает ERPNext. Для legacy `erp_pending`/`recovery_required`, где внешний документ
мог уже существовать, сохранена совместимость: recovery сначала ищет ERP marker и
материализует результат под прежним optimistic `updated_at`; новый side effect из
legacy payload не создаётся.

## Общая сериализация shift scope

Проверена полная значимая матрица обеих последовательностей:

- active sale блокирует close; active close блокирует sale;
- active create-return блокирует close; active close блокирует create-return;
- active cancel-return блокирует close; active close блокирует cancel-return;
- held receipt и его mutation не пересекают активный close;
- stale owner после lease takeover не может записать close projection или terminal
  intent.

Гонки create-return/close проверяются через два независимых API/runtime pool как для
SQLite, так и для PostgreSQL. Проигравший запрос получает safe `409 SHIFT_CHANGED` до
ERP side effect. Совместимые recovery/replay присоединяются к каноническому intent.

## Проверяемые инварианты

- ERP Closing получает именно frozen `Shift` и frozen денежные значения;
- drift `sales_total` без изменения `updated_at` обнаруживается обеими реализациями
  repository;
- подтверждённый `CLOSE-1` + local materialization failure даёт `503`, затем retry
  завершает ту же смену с `CLOSE-1`, число create-вызовов остаётся равным одному;
- SQLite и PostgreSQL сохраняют точные opening/sales/returns/expected/actual/difference;
- два PostgreSQL пула соблюдают общий advisory-lock scope и fencing;
- отрицательная difference не ошибочно трактуется как повреждённый total;
- API/OpenAPI и migration head не изменены.

## Результат

MR-SEC-021 закрыт. Phase 7B–7D теперь дают единый durable и fenced lifecycle для
create-return, cancel-return и close shift поверх утверждённого PostgreSQL source of
truth. Следующие security-фазы должны начинаться с нового актуального аудита findings;
storage/API contract не расширяется без отдельного решения Ернура.
