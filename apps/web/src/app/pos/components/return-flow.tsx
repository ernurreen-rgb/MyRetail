"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { type FormEvent, useEffect, useMemo, useState } from "react";

import { ReturnSlipView } from "@/app/pos/components/return-slip-view";
import {
  EmptyState,
  ErrorState,
  FieldError,
  dangerButtonClass,
  formatDateTime,
  getFieldError,
  inputClass,
  parseDecimal,
  secondaryButtonClass,
} from "@/app/pos/components/shared";
import {
  createIdempotencyKey,
  createReturn,
  getReturnOptions,
  type POSReturn,
  type ReturnOptions,
  type ReturnReason,
  type Sale,
} from "@/lib/pos";

const reasonOptions: { value: ReturnReason; label: string }[] = [
  { value: "customer_request", label: "Запрос клиента" },
  { value: "cashier_error", label: "Ошибка кассира" },
  { value: "damaged", label: "Повреждение" },
  { value: "other", label: "Другое" },
];

const returnStatusLabels: Record<Sale["return_status"], string> = {
  none: "возвратов нет",
  partial: "частично возвращено",
  full: "полностью возвращено",
};

const friendlyReturnErrors: Record<string, string> = {
  RETURN_QUANTITY_EXCEEDED: "Запрошено больше, чем доступно к возврату. Обновите варианты возврата.",
  SALE_ALREADY_FULLY_RETURNED: "По этой продаже уже нечего возвращать.",
  IDEMPOTENCY_KEY_REUSED:
    "Этот ключ идемпотентности уже использован с другим запросом. Я подготовил новый ключ — повторите отправку.",
  RETURN_RECOVERY_REQUIRED:
    "Результат возврата требует ручного восстановления. Не повторяйте вслепую, проверьте историю возвратов.",
  POS_OPENING_OUTDATED:
    "POS Opening Entry устарела для cash refund. Обновите/переоткройте актуальную смену.",
  ERPNEXT_UNAVAILABLE: "ERPNext временно недоступен. Повторите позже с тем же ключом, если запрос мог уйти.",
  API_TIMEOUT:
    "Backend не ответил вовремя. Для безопасного retry используется тот же Idempotency-Key.",
};

function errorMessage(code: string, fallback: string) {
  return friendlyReturnErrors[code] ?? fallback;
}

function availableLines(options: ReturnOptions) {
  return options.lines.filter(
    (line) => parseDecimal(line.available_to_return_quantity) > 0,
  );
}

function buildInitialQuantities(options: ReturnOptions) {
  return Object.fromEntries(options.lines.map((line) => [line.line_id, ""]));
}

function buildFullQuantities(options: ReturnOptions) {
  return Object.fromEntries(
    options.lines.map((line) => [
      line.line_id,
      parseDecimal(line.available_to_return_quantity) > 0
        ? line.available_to_return_quantity
        : "",
    ]),
  );
}

export function ReturnFlow({
  sale,
  onClose,
  onCompleted,
}: {
  sale: Sale | null;
  onClose: () => void;
  onCompleted: (posReturn: POSReturn) => void;
}) {
  const [options, setOptions] = useState<ReturnOptions | null>(null);
  const [quantities, setQuantities] = useState<Record<string, string>>({});
  const [reason, setReason] = useState<ReturnReason | "">("");
  const [comment, setComment] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState(createIdempotencyKey);
  const [isLoading, setIsLoading] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [createdReturn, setCreatedReturn] = useState<POSReturn | null>(null);

  const linesToReturn = useMemo(() => {
    if (!options) {
      return [];
    }

    return options.lines
      .map((line) => ({
        line,
        quantity: (quantities[line.line_id] ?? "").replace(",", ".").trim(),
      }))
      .filter(({ quantity }) => parseDecimal(quantity) > 0);
  }, [options, quantities]);

  const estimatedRefund = useMemo(() => {
    return linesToReturn.reduce((sum, { line, quantity }) => {
      const available = parseDecimal(line.available_to_return_quantity);
      const safeQuantity = Math.min(parseDecimal(quantity), available);
      return sum + safeQuantity * parseDecimal(line.unit_price);
    }, 0);
  }, [linesToReturn]);

  async function loadOptions() {
    if (!sale) {
      return;
    }

    setIsLoading(true);
    setError(null);
    setFieldErrors({});
    setCreatedReturn(null);

    const result = await getReturnOptions(sale.id);

    if (result.status === "success") {
      setOptions(result.data);
      setQuantities(buildInitialQuantities(result.data));
    } else {
      setError(errorMessage(result.error.code, result.error.message));
    }

    setIsLoading(false);
  }

  useEffect(() => {
    setOptions(null);
    setQuantities({});
    setReason("");
    setComment("");
    setIdempotencyKey(createIdempotencyKey());
    if (sale) {
      void loadOptions();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sale?.id]);

  if (!sale) {
    return null;
  }

  function validate() {
    const nextErrors: Record<string, string> = {};

    if (!reason) {
      nextErrors.reason = "Выберите причину возврата.";
    }

    if (!options) {
      nextErrors.form = "Сначала загрузите доступные позиции возврата.";
    } else if (availableLines(options).length === 0) {
      nextErrors.form = "По этой продаже нет доступных к возврату позиций.";
    }

    for (const line of options?.lines ?? []) {
      const rawQuantity = (quantities[line.line_id] ?? "").replace(",", ".").trim();
      const quantity = parseDecimal(rawQuantity);
      const available = parseDecimal(line.available_to_return_quantity);

      if (!rawQuantity) {
        continue;
      }
      if (quantity <= 0) {
        nextErrors[`lines.${line.line_id}.quantity`] = "Количество должно быть больше 0.";
      }
      if (quantity > available) {
        nextErrors[`lines.${line.line_id}.quantity`] =
          `Доступно к возврату не больше ${line.available_to_return_quantity}.`;
      }
    }

    if (linesToReturn.length === 0) {
      nextErrors.lines = "Укажите количество хотя бы по одной строке.";
    }

    setFieldErrors(nextErrors);
    return Object.keys(nextErrors).length === 0;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!options || !validate() || !reason) {
      return;
    }

    setIsSubmitting(true);
    setError(null);

    const result = await createReturn(
      {
        sale_id: options.sale_id,
        register_id: options.register_id,
        shift_id: options.shift_id,
        refund_method: "cash",
        reason,
        comment,
        lines: linesToReturn.map(({ line, quantity }) => ({
          line_id: line.line_id,
          quantity,
        })),
      },
      idempotencyKey,
    );

    if (result.status === "success") {
      setIdempotencyKey(createIdempotencyKey());
      setFieldErrors({});
      setError(null);
      onCompleted(result.data);
      await loadOptions();
      setCreatedReturn(result.data);
    } else {
      setError(errorMessage(result.error.code, result.error.message));
      setFieldErrors(result.error.fields);
      if (result.error.code === "IDEMPOTENCY_KEY_REUSED") {
        setIdempotencyKey(createIdempotencyKey());
      }
    }

    setIsSubmitting(false);
  }

  return (
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)]">
      <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-sm text-[var(--muted)]">Возвраты POS</p>
          <h2 className="text-2xl font-semibold tracking-tight">
            Оформить возврат по продаже {sale.receipt_number}
          </h2>
          <p className="mt-1 text-sm text-[var(--muted)]">
            Статус: {returnStatusLabels[sale.return_status]} · возвращено {sale.returned_total}{" "}
            {sale.currency} · {formatDateTime(sale.created_at)}
          </p>
        </div>
        <button type="button" className={secondaryButtonClass} onClick={onClose}>
          Закрыть форму
        </button>
      </div>

      {isLoading ? <EmptyState>Загружаю доступные позиции возврата…</EmptyState> : null}
      {error ? <ErrorState message={error} onRetry={() => void loadOptions()} /> : null}

      {options && !isLoading ? (
        <form onSubmit={handleSubmit} className="grid gap-5">
          <div className="grid gap-3 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4 md:grid-cols-3">
            <div>
              <p className="text-sm text-[var(--muted)]">Доступно к возврату</p>
              <p className="text-xl font-semibold">
                {options.totals.available_to_return_total ?? options.totals.refund_total}{" "}
                {options.currency}
              </p>
            </div>
            <div>
              <p className="text-sm text-[var(--muted)]">Уже возвращено</p>
              <p className="text-xl font-semibold">
                {options.totals.already_returned_total ?? sale.returned_total} {options.currency}
              </p>
            </div>
            <div>
              <p className="text-sm text-[var(--muted)]">Текущий расчёт cash refund</p>
              <p className="text-xl font-semibold">{estimatedRefund.toFixed(2)} KZT</p>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-left text-sm">
              <thead>
                <tr className="border-b border-[var(--border)] text-[var(--muted)]">
                  <th className="py-2 pr-3">Позиция</th>
                  <th className="py-2 pr-3">Продано</th>
                  <th className="py-2 pr-3">Уже возвращено</th>
                  <th className="py-2 pr-3">Доступно</th>
                  <th className="py-2 pr-3">Вернуть</th>
                </tr>
              </thead>
              <tbody>
                {options.lines.map((line) => {
                  const disabled = parseDecimal(line.available_to_return_quantity) <= 0;

                  return (
                    <tr key={line.line_id} className="border-b border-[var(--border)]">
                      <td className="py-3 pr-3">
                        <span className="font-medium">{line.item_name}</span>
                        <span className="block font-mono text-xs text-[var(--muted)]">
                          {line.item_id} · {line.line_id}
                        </span>
                      </td>
                      <td className="py-3 pr-3">
                        {line.sold_quantity} {line.unit}
                      </td>
                      <td className="py-3 pr-3">
                        {line.already_returned_quantity} {line.unit}
                      </td>
                      <td className="py-3 pr-3">
                        {line.available_to_return_quantity} {line.unit}
                      </td>
                      <td className="py-3 pr-3">
                        <input
                          value={quantities[line.line_id] ?? ""}
                          onChange={(event) =>
                            setQuantities((current) => ({
                              ...current,
                              [line.line_id]: event.target.value,
                            }))
                          }
                          className={inputClass}
                          inputMode="decimal"
                          placeholder="0.000"
                          disabled={disabled || isSubmitting}
                        />
                        <FieldError
                          message={getFieldError(
                            fieldErrors,
                            `lines.${line.line_id}.quantity`,
                            `lines.${line.line_id}`,
                          )}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className={secondaryButtonClass}
              onClick={() => setQuantities(buildFullQuantities(options))}
              disabled={isSubmitting || availableLines(options).length === 0}
            >
              Полный возврат оставшихся позиций
            </button>
            <button
              type="button"
              className={secondaryButtonClass}
              onClick={() => setQuantities(buildInitialQuantities(options))}
              disabled={isSubmitting}
            >
              Очистить количества
            </button>
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <label className="block">
              <span className="text-sm font-semibold">Причина возврата</span>
              <select
                value={reason}
                onChange={(event) => setReason(event.target.value as ReturnReason | "")}
                className={inputClass}
                disabled={isSubmitting}
              >
                <option value="">Выберите причину</option>
                {reasonOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              <FieldError message={getFieldError(fieldErrors, "reason")} />
            </label>
            <label className="block">
              <span className="text-sm font-semibold">Комментарий</span>
              <input
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                className={inputClass}
                maxLength={500}
                placeholder="Необязательно"
                disabled={isSubmitting}
              />
            </label>
          </div>

          <FieldError message={getFieldError(fieldErrors, "form", "lines")} />

          <div className="flex flex-wrap items-center gap-3">
            <button
              type="submit"
              className={dangerButtonClass}
              disabled={isSubmitting || availableLines(options).length === 0}
            >
              {isSubmitting ? "Оформляю возврат…" : "Оформить cash refund KZT"}
            </button>
            <span className="font-mono text-xs text-[var(--muted)]">
              Idempotency-Key: {idempotencyKey}
            </span>
          </div>
        </form>
      ) : null}

      <div className="mt-5">
        <ReturnSlipView posReturn={createdReturn} />
      </div>
    </section>
  );
}
