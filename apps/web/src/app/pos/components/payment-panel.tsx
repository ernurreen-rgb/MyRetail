import { type FormEvent } from "react";

import type { CartLine } from "@/app/pos/pos-types";
import {
  calculateCartTotals,
  FieldError,
  formatMoney,
  getFieldError,
  inputClass,
  parseDecimal,
  primaryButtonClass,
} from "@/app/pos/components/shared";
import type { HeldReceipt, Shift } from "@/lib/pos";

export function PaymentPanel({
  shift,
  cartLines,
  selectedHeld,
  cashReceived,
  isSelling,
  saleError,
  saleFieldErrors,
  hasCartErrors,
  onCashReceivedChange,
  onSubmitSale,
}: {
  shift: Shift | null;
  cartLines: CartLine[];
  selectedHeld: HeldReceipt | null;
  cashReceived: string;
  isSelling: boolean;
  saleError: string | null;
  saleFieldErrors?: Record<string, string>;
  hasCartErrors: boolean;
  onCashReceivedChange: (value: string) => void;
  onSubmitSale: () => Promise<void>;
}) {
  const totals = calculateCartTotals(cartLines);
  const change = Math.max(0, parseDecimal(cashReceived) - totals.total);
  const canSell = Boolean(shift) && cartLines.length > 0 && !hasCartErrors && !isSelling;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await onSubmitSale();
  }

  return (
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)]">
      <div className="mb-5">
        <p className="text-sm text-[var(--muted)]">Оплата</p>
        <h2 className="text-2xl font-semibold tracking-tight">Наличный расчёт</h2>
      </div>

      <form onSubmit={handleSubmit} className="grid gap-4">
        <label className="block">
          <span className="text-sm font-semibold">Получено наличными</span>
          <input
            value={cashReceived}
            onChange={(event) => onCashReceivedChange(event.target.value)}
            className={inputClass}
            inputMode="decimal"
          />
          <FieldError message={getFieldError(saleFieldErrors, "cash_received")} />
        </label>

        <div className="grid gap-2 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4 text-sm">
          <div className="flex items-center justify-between gap-4">
            <span className="text-[var(--muted)]">К оплате</span>
            <span className="font-semibold">{formatMoney(totals.total)}</span>
          </div>
          <div className="flex items-center justify-between gap-4">
            <span className="text-[var(--muted)]">Сдача preview</span>
            <span className="font-semibold">{formatMoney(change)}</span>
          </div>
          {selectedHeld ? (
            <p className="text-xs leading-5 text-[var(--muted)]">
              Продажа будет создана из отложенного чека {selectedHeld.id}; backend удалит его после
              успешной продажи.
            </p>
          ) : null}
        </div>

        {saleError ? (
          <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm leading-6 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300" role="alert">
            <p>{saleError}</p>
            <p className="mt-2 text-xs">
              Корзина не очищена. Повтор без изменения корзины или оплаты использует тот же
              Idempotency-Key.
            </p>
          </div>
        ) : null}

        <button type="submit" className={primaryButtonClass} disabled={!canSell}>
          {isSelling ? "Пробиваю…" : saleError ? "Повторить продажу безопасно" : "Пробить продажу"}
        </button>
      </form>
    </section>
  );
}
