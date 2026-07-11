import {
  formatDateTime,
  primaryButtonClass,
  secondaryButtonClass,
} from "@/app/pos/components/shared";
import type { POSReturn } from "@/lib/pos";

const reasonLabels: Record<POSReturn["reason"], string> = {
  customer_request: "Запрос клиента",
  cashier_error: "Ошибка кассира",
  damaged: "Повреждение",
  other: "Другое",
};

const stateLabels: Record<POSReturn["state"], string> = {
  submitted: "Проведён",
  cancelled: "Отменён",
  pending_recovery: "Требует восстановления",
};

export function ReturnSlipView({
  posReturn,
  onClose,
}: {
  posReturn: POSReturn | null;
  onClose?: () => void;
}) {
  if (!posReturn) {
    return null;
  }

  return (
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)]">
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-sm text-[var(--muted)]">Return slip</p>
          <h2 className="text-2xl font-semibold tracking-tight">
            Чек возврата {posReturn.return_receipt_number}
          </h2>
          <p className="mt-1 text-sm text-[var(--muted)]">
            Исходная продажа {posReturn.receipt_number} · {formatDateTime(posReturn.created_at)}
          </p>
        </div>
        <div className="flex flex-wrap gap-2 print:hidden">
          <button type="button" className={primaryButtonClass} onClick={() => window.print()}>
            Печать
          </button>
          {onClose ? (
            <button type="button" className={secondaryButtonClass} onClick={onClose}>
              Скрыть
            </button>
          ) : null}
        </div>
      </div>

      <article className="rounded-xl border border-dashed border-[var(--border)] bg-white p-5 text-black dark:bg-white">
        <div className="border-b border-slate-200 pb-3">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">MyRetail POS return</p>
          <h3 className="mt-1 text-xl font-semibold">Возврат наличными KZT</h3>
          <p className="mt-1 text-sm text-slate-600">
            Статус: {stateLabels[posReturn.state]} · Причина: {reasonLabels[posReturn.reason]}
          </p>
        </div>

        <dl className="mt-4 grid gap-2 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-slate-500">Return ID</dt>
            <dd className="font-mono">{posReturn.return_id}</dd>
          </div>
          <div>
            <dt className="text-slate-500">Sale ID</dt>
            <dd className="font-mono">{posReturn.sale_id}</dd>
          </div>
          <div>
            <dt className="text-slate-500">Касса / смена</dt>
            <dd>
              {posReturn.register_id} / {posReturn.shift_id}
            </dd>
          </div>
          <div>
            <dt className="text-slate-500">Оформил</dt>
            <dd>{posReturn.created_by}</dd>
          </div>
        </dl>

        <table className="mt-5 w-full border-collapse text-left text-sm">
          <thead>
            <tr className="border-b border-slate-200">
              <th className="py-2 pr-2">Товар</th>
              <th className="py-2 pr-2">Кол-во</th>
              <th className="py-2 pr-2">Цена</th>
              <th className="py-2 text-right">Сумма</th>
            </tr>
          </thead>
          <tbody>
            {posReturn.lines.map((line) => (
              <tr key={line.line_id} className="border-b border-slate-100">
                <td className="py-2 pr-2">
                  <span className="font-medium">{line.item_name}</span>
                  <span className="block font-mono text-xs text-slate-500">{line.item_id}</span>
                </td>
                <td className="py-2 pr-2">
                  {line.quantity} {line.unit}
                </td>
                <td className="py-2 pr-2">{line.unit_price}</td>
                <td className="py-2 text-right">{line.line_total}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="mt-5 border-t border-slate-200 pt-4 text-right">
          <p className="text-sm text-slate-500">К возврату</p>
          <p className="text-2xl font-semibold">
            {posReturn.totals.refund_total} {posReturn.currency}
          </p>
          {posReturn.comment ? (
            <p className="mt-3 text-left text-sm text-slate-600">Комментарий: {posReturn.comment}</p>
          ) : null}
          {posReturn.cancelled_at ? (
            <p className="mt-3 text-left text-sm text-slate-600">
              Отменён: {formatDateTime(posReturn.cancelled_at)} · {posReturn.cancelled_by}
            </p>
          ) : null}
        </div>
      </article>
    </section>
  );
}
