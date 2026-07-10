# Sprint 6 Phase 0: ERPNext Sales Return spike

Дата проверки: 2026-07-10
Ветка: `codex/damir-pos-returns-phase0`
База: `main` на `6d9659eb6456e298df17d92691cc6f43746fda04`

## Вывод

Sales Return подходит для Sprint 6 только с защитным MyRetail-слоем. ERPNext корректно связывает документ с `return_against`, восстанавливает stock и создаёт обратные GL-проводки. Но ERPNext v16.23.1 в проверенном сценарии сам не запретил over-return: после возврата `2 + 2` из исходных `5` был принят ещё один возврат `2`. POS-возврат также зависит от актуальной POS Opening Entry; stale opening блокирует submit.

## Live сценарии

| Сценарий | Фактический результат |
| --- | --- |
| Full return | Existing `Sales Invoice`, исходный qty 5: stock `5 -> 10`; `is_return=1`, `return_against` заполнен. |
| Cancel full return | `docstatus 2`; stock `10 -> 5`; GL reversals созданы. |
| Partial return | qty 2: stock `5 -> 7`; qty 2 повторно: `7 -> 9`. |
| Repeated partial | Повторный partial against same original принят. |
| Over-return | Следующий qty 2 после total returned 4 из 5 принят ERPNext; server-side guard не доказан. QA-документы после проверки отменены. |
| Stock reservation | Проверено через `tabBin`: `reserved_qty` оставался `0`; основной эффект был в `actual_qty`. |
| POS Closing visibility | `get_invoices` с теми же `pos_profile` и mapped POS user вернул исходный invoice и 1 payment. |
| POS return / shift | Submit POS return был отклонён из-за stale `POS Opening Entry` (`Outdated POS Opening Entry`); требуется актуальная открытая смена. |
| Cleanup | Все созданные Phase 0 return-документы отменены; QA stock восстановлен до `5.000`. |

## ERPNext документы и поля

- `Sales Invoice`: `name`, `docstatus`, `is_return`, `return_against`, `update_stock`, `owner`, `modified`, `posting_date`, `grand_total`, `outstanding_amount`, `pos_profile`, `is_pos`.
- `Sales Invoice Item`: `item_code`, `qty`, `rate`, `warehouse`, `stock_uom`, `income_account`, `expense_account`, `cost_center`.
- `Sales Invoice Payment`: `mode_of_payment`, `amount`, `account`.
- `POS Opening Entry` и `POS Opening Entry Detail`: открытая смена, `pos_profile`, `user`, `balance_details`.
- `POS Closing Entry` и child tables: `pos_opening_entry`, `sales_invoices`, `payment_reconciliation`.
- `Bin`: `item_code`, `warehouse`, `actual_qty`, `reserved_qty`.
- `GL Entry` и `Stock Ledger Entry`: фактическая accounting/stock-аудит трасса.

Использованные ERPNext endpoints/methods: `GET /api/resource/Sales Invoice/{name}`, `POST /api/resource/Sales Invoice`, `POST /api/method/frappe.client.submit`, `POST /api/method/frappe.client.cancel`, `POST /api/method/erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry.get_invoices`.

## Cash/accounting и shift

Non-POS return сформировал обратные записи по `Sales - MRD`, `Debtors - MRD`, `Stock In Hand - MRD` и COGS. Наличный refund через POS Payment не утверждаю: POS submit был остановлен stale opening, поэтому cash reversal и Closing Entry с return invoice требуют отдельного QA после создания свежей смены.

## Proposal для будущего API v1.0

Предлагаемый контракт: `POST /pos/returns` с обязательным `Idempotency-Key`, `sale_id`, `lines[{sale_line_id, quantity}]`, `refund_method=cash`, `reason`, `register_id`, `shift_id`; `GET /pos/returns/{return_id}` и история возвратов. Это proposal, не изменение утверждённого POS API.

Перед ERPNext create MyRetail должен транзакционно проверить `sold_qty - returned_qty`, tenant/register/cashier scope и закрытую/открытую смену. ERPNext draft должен содержать recovery marker, original invoice id и line snapshot. После timeout нельзя blind retry: recovery ищет exact marker + `return_against`; при неопределённости нужен terminal `pending_recovery`/manual review. Повторная отмена должна быть conflict после первого успешного cancel. Cash POS return разрешать только при актуальном Opening Entry; closed-shift возврат вынести в отдельный approval/manual flow.

## Ограничения и безопасность

- Sprint 6 backend API и утверждённый POS API не менялись.
- Feature-код, ветки реализации endpoint-ов и PR не создавались.
- Секреты, API tokens, passwords и cookies не выводились в отчёт и не коммитились.
- Результат: **подходит с условиями**; до API freeze обязательны MyRetail over-return guard, recovery/idempotency и отдельный live POS cash refund QA на свежей смене.
