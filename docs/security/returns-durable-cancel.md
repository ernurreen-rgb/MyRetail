# Security Phase 7C — durable cancel return

Дата: 16.07.2026.

## Граница изменения

Phase 7C переводит `POST /pos/returns/{return_id}/cancel` на durable workflow с
одинаковыми гарантиями SQLite/PostgreSQL. HTTP endpoint, request/response schemas,
публичные состояния и утверждённые error codes не меняются. Новая migration не нужна:
packaged PostgreSQL head теперь `20260716_05`; return backfill остаётся revision `20260716_04`.

Отмена cash return допустима только пока исходная смена открыта. Для закрытой смены
сохраняется fail-closed `409 POS_OPENING_OUTDATED` до отдельного утверждённого settlement
contract; ERPNext при этом не вызывается.

## Каноническая идентичность и сериализация

- Канонический principal не зависит от администратора:
  `return:<return_id>:cancel`.
- Stable external marker вычисляется детерминированно как SHA-256 нормализованного
  `tenant + return_id`. Разные `Idempotency-Key` и разные Owner/Admin присоединяются к
  одному совместимому intent через immutable aliases.
- ERPNext cancel адресует уже существующий Sales Invoice, поэтому внешний side effect
  имеет естественную стабильную identity — `erpnext_return_invoice_id`. Перед вызовом и
  при recovery сервер проверяет `docstatus`: `2` означает подтверждённую отмену, `1` —
  допустимый первый cancel, остальные/недоступность дают safe recovery response.
- Intent резервируется в общем `shift:<shift_id>` scope. Активные close/sale/return и
  несовместимый cancel получают safe `409` до второго ERP side effect.

Новый key после terminal cancelled по-прежнему получает существующий
`409 RETURN_ALREADY_CANCELLED`. Replay того же key возвращает сохранённый успешный
результат. Это сохраняет публичную семантику, но защищает параллельные совместимые
повторы с разными ключами.

## Атомарная материализация

После подтверждённого ERP `docstatus=2` одна fenced database transaction:

1. проверяет владельца lease, fencing token, immutable cancel snapshot и исходный
   отрицательный cash event;
2. блокирует return projection и открытую shift projection;
3. append-only добавляет ровно один положительный `return_cancel` cash event;
4. переводит return из внутреннего `cancel_pending` в `cancelled` с audit-полями;
5. пересчитывает `cash_returns_total` и `expected_cash` из ledger;
6. переводит workflow intent в terminal `completed`.

Уникальность `(tenant, source_type, source_id, effect_kind)` исключает второй денежный
эффект. Stale fencing token не может записать projection, cash event, totals или terminal
intent. PostgreSQL app role не получил `UPDATE` на immutable ledger: чтение event для
проверки выполняется без `FOR UPDATE`, а сериализацию обеспечивают locks return/shift и
unique effect identity.

## Recovery и legacy compatibility

Timeout/lost response переводит intent в `recovery_required`; terminal `5xx` для новых
cancel операций не кэшируется. Retry сначала восстанавливает canonical intent и проверяет
ERP `docstatus`. Уже отменённый invoice материализуется без повторного ERP cancel.

Для записанного старыми версиями terminal `5xx` idempotency response включён специальный
recovery: состояния `submitted` и `cancel_pending` присоединяются к новому durable intent,
а подтверждённый ERP cancel завершается ровно одним compensating event. Старые
`cancelled` rows и их компенсации уже reconciled migration `20260716_04`.

Внутренний `cancel_pending` публично отображается как `pending_recovery`, а не как
`cancelled`. Пока отмена не подтверждена, return продолжает учитываться в
`return-options`, поэтому товар нельзя повторно вернуть через legacy pending window.

## Проверяемые инварианты

- два одновременных cancel одного return с разными keys и разными администраторами:
  один ERP cancel, один intent, два aliases и один `return_cancel` event;
- lost response и same-key replay восстанавливают результат без второго side effect;
- старый cached `503` для `submitted`/`cancel_pending` восстанавливается через stable
  durable intent;
- несовместимый cancel, active close и active cancel взаимно блокируются до ERP;
- pending публично не выглядит cancelled и резервирует возвращённое количество;
- SQLite/PostgreSQL stale fencing не создаёт компенсацию, takeover создаёт её один раз;
- cash totals после cancel восстановлены ровно один раз.

## Закрытие границы в Phase 7D

Phase 7D завершила frozen shift/cash snapshot и сериализацию всех комбинаций
sale/create-return/cancel-return/close для SQLite и PostgreSQL. Подтверждённый ERP Closing
с local projection conflict теперь переводится в recovery и не создаётся повторно;
подробные инварианты зафиксированы в `returns-frozen-shift-close.md`. Storage/API contract
и migration head не изменены.
