import type { CartLine } from "@/app/pos/pos-types";
import {
  calculateCartTotals,
  EmptyState,
  FieldError,
  formatMoney,
  getFieldError,
  inputClass,
  parseDecimal,
  secondaryButtonClass,
} from "@/app/pos/components/shared";

export function hasCartValidationErrors(lines: CartLine[], discountLimitPercent: string) {
  const discountLimit = parseDecimal(discountLimitPercent);

  return lines.some(
    (line) =>
      parseDecimal(line.quantity) <= 0 ||
      parseDecimal(line.discount_percent || "0") < 0 ||
      parseDecimal(line.discount_percent || "0") > discountLimit,
  );
}

export function CartPanel({
  lines,
  discountLimitPercent,
  backendFieldErrors,
  onUpdateLine,
  onRemoveLine,
  onClearCart,
}: {
  lines: CartLine[];
  discountLimitPercent: string;
  backendFieldErrors?: Record<string, string>;
  onUpdateLine: (index: number, patch: Partial<Pick<CartLine, "quantity" | "discount_percent">>) => void;
  onRemoveLine: (index: number) => void;
  onClearCart: () => void;
}) {
  const totals = calculateCartTotals(lines);
  const discountLimit = parseDecimal(discountLimitPercent);

  return (
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)]">
      <div className="mb-5 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-sm text-[var(--muted)]">Корзина</p>
          <h2 className="text-2xl font-semibold tracking-tight">Чек покупателя</h2>
        </div>
        <button
          type="button"
          className={secondaryButtonClass}
          onClick={onClearCart}
          disabled={lines.length === 0}
        >
          Очистить
        </button>
      </div>

      {lines.length === 0 ? (
        <EmptyState>Добавьте товар поиском или сканером. Корзина сохранится при ошибке продажи.</EmptyState>
      ) : (
        <div className="grid gap-3">
          {lines.map((line, index) => {
            const quantityError =
              parseDecimal(line.quantity) <= 0
                ? "Количество должно быть больше нуля."
                : getFieldError(backendFieldErrors, `lines.${index}.quantity`, "quantity");
            const discount = parseDecimal(line.discount_percent || "0");
            const discountError =
              discount < 0 || discount > discountLimit
                ? `Скидка должна быть от 0 до ${discountLimitPercent}%.`
                : getFieldError(
                    backendFieldErrors,
                    `lines.${index}.discount_percent`,
                    "discount_percent",
                  );
            const lineSubtotal = parseDecimal(line.product.sale_price) * parseDecimal(line.quantity);
            const lineTotal = Math.max(0, lineSubtotal - (lineSubtotal * discount) / 100);

            return (
              <article
                key={line.product.id}
                className="rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4"
              >
                <div className="grid gap-4 lg:grid-cols-[1.1fr_0.65fr_0.65fr_auto] lg:items-start">
                  <div>
                    <p className="font-mono text-xs text-[var(--muted)]">{line.product.sku}</p>
                    <h3 className="font-semibold">{line.product.name}</h3>
                    <p className="mt-1 text-sm text-[var(--muted)]">
                      Цена: {line.product.sale_price} {line.product.currency} · Остаток:
                      {" "}
                      {line.product.available} {line.product.unit}
                    </p>
                  </div>

                  <label className="block">
                    <span className="text-sm font-semibold">Количество {line.product.name}</span>
                    <input
                      value={line.quantity}
                      onChange={(event) => onUpdateLine(index, { quantity: event.target.value })}
                      className={inputClass}
                      inputMode="decimal"
                    />
                    <FieldError message={quantityError} />
                  </label>

                  <label className="block">
                    <span className="text-sm font-semibold">Скидка {line.product.name}, %</span>
                    <input
                      value={line.discount_percent}
                      onChange={(event) =>
                        onUpdateLine(index, { discount_percent: event.target.value })
                      }
                      className={inputClass}
                      inputMode="decimal"
                    />
                    <FieldError message={discountError} />
                  </label>

                  <div className="flex flex-col gap-3 lg:items-end">
                    <span className="rounded-full bg-[var(--accent-soft)] px-3 py-1.5 text-sm font-semibold text-[var(--accent)]">
                      {formatMoney(lineTotal)}
                    </span>
                    <button
                      type="button"
                      className={secondaryButtonClass}
                      onClick={() => onRemoveLine(index)}
                    >
                      Удалить
                    </button>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      )}

      <div className="mt-5 grid gap-2 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4 text-sm sm:grid-cols-3">
        <div>
          <span className="text-[var(--muted)]">Сумма</span>
          <p className="text-lg font-semibold">{formatMoney(totals.subtotal)}</p>
        </div>
        <div>
          <span className="text-[var(--muted)]">Скидка</span>
          <p className="text-lg font-semibold">{formatMoney(totals.discountTotal)}</p>
        </div>
        <div>
          <span className="text-[var(--muted)]">Итого к оплате</span>
          <p className="text-lg font-semibold">{formatMoney(totals.total)}</p>
        </div>
      </div>
      <p className="mt-2 text-xs leading-5 text-[var(--muted)]">
        Итоги здесь предварительные; финальный чек рассчитывает backend по POS API v1.0.
      </p>
    </section>
  );
}
