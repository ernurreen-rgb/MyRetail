import { formatDateTime, secondaryButtonClass } from "@/app/pos/components/shared";
import type { Sale } from "@/lib/pos";

export function ReceiptView({ sale }: { sale: Sale | null }) {
  if (!sale) {
    return (
      <section className="rounded-2xl border border-[var(--border)] bg-[var(--surface-muted)] p-5 text-sm leading-6 text-[var(--muted)]">
        Последний чек появится здесь после продажи.
      </section>
    );
  }

  return (
    <section
      aria-label="HTML-чек"
      className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)]"
    >
      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-sm text-[var(--muted)]">HTML-чек</p>
          <h2 className="text-2xl font-semibold tracking-tight">Чек {sale.receipt_number}</h2>
        </div>
        <button
          type="button"
          className={secondaryButtonClass}
          onClick={() => window.print()}
        >
          Печать чека
        </button>
      </div>

      <div className="rounded-xl border border-[var(--border)] bg-white p-5 font-mono text-sm text-slate-950">
        <div className="text-center">
          <p className="font-bold">MyRetail POS</p>
          <p>{sale.register.name}</p>
          <p>{formatDateTime(sale.created_at)}</p>
          <p>Кассир: {sale.cashier.full_name || sale.cashier.email}</p>
        </div>

        <div className="my-4 border-t border-dashed border-slate-400" />

        <div className="grid gap-3">
          {sale.lines.map((line) => (
            <div key={line.product_id}>
              <div className="flex justify-between gap-4">
                <span>{line.name}</span>
                <span>{line.total}</span>
              </div>
              <p className="text-xs text-slate-600">
                {line.quantity} {line.unit} × {line.unit_price}, скидка {line.discount_percent}%
              </p>
            </div>
          ))}
        </div>

        <div className="my-4 border-t border-dashed border-slate-400" />

        <div className="grid gap-1">
          <div className="flex justify-between">
            <span>Сумма</span>
            <span>{sale.subtotal}</span>
          </div>
          <div className="flex justify-between">
            <span>Скидка</span>
            <span>{sale.discount_total}</span>
          </div>
          <div className="flex justify-between font-bold">
            <span>Итого</span>
            <span>{sale.grand_total}</span>
          </div>
          <div className="flex justify-between">
            <span>Получено</span>
            <span>{sale.cash_received}</span>
          </div>
          <div className="flex justify-between">
            <span>Сдача</span>
            <span>{sale.change}</span>
          </div>
        </div>
      </div>
    </section>
  );
}
