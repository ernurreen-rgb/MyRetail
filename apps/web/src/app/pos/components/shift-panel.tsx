/* eslint-disable react-hooks/set-state-in-effect */

import { type FormEvent, useEffect, useState } from "react";

import {
  dangerButtonClass,
  FieldError,
  formatDateTime,
  inputClass,
  primaryButtonClass,
  secondaryButtonClass,
  type POSActionResult,
} from "@/app/pos/components/shared";
import { createIdempotencyKey, type POSOptions, type Shift } from "@/lib/pos";

export function ShiftPanel({
  options,
  registerId,
  shift,
  isLoading,
  error,
  canOperate,
  onRegisterChange,
  onRetry,
  onOpenShift,
  onCloseShift,
}: {
  options: POSOptions | null;
  registerId: string;
  shift: Shift | null;
  isLoading: boolean;
  error: string | null;
  canOperate: boolean;
  onRegisterChange: (registerId: string) => void;
  onRetry: () => void;
  onOpenShift: (openingCash: string, idempotencyKey: string) => Promise<POSActionResult>;
  onCloseShift: (
    actualCash: string,
    reason: string,
    idempotencyKey: string,
  ) => Promise<POSActionResult>;
}) {
  const [openingCash, setOpeningCash] = useState("0.00");
  const [actualCash, setActualCash] = useState("");
  const [reason, setReason] = useState("");
  const [openKey, setOpenKey] = useState(createIdempotencyKey);
  const [closeKey, setCloseKey] = useState(createIdempotencyKey);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    if (shift) {
      setActualCash(shift.expected_cash);
      setReason("");
      setFieldErrors({});
      setFormError(null);
      setCloseKey(createIdempotencyKey());
    }
  }, [shift?.expected_cash, shift?.id, shift]);

  const activeRegisters = options?.registers.filter((register) => register.is_active) ?? [];
  const selectedRegister =
    options?.registers.find((register) => register.id === registerId) ?? activeRegisters[0] ?? null;

  async function handleOpen(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsSaving(true);
    setFormError(null);
    setFieldErrors({});

    const result = await onOpenShift(openingCash, openKey);

    if (result.ok) {
      setOpeningCash("0.00");
      setOpenKey(createIdempotencyKey());
    } else {
      setFormError(result.message);
      setFieldErrors(result.fields ?? {});
      if (result.code === "IDEMPOTENCY_CONFLICT" || result.code === "SHIFT_ALREADY_OPEN") {
        setOpenKey(createIdempotencyKey());
      }
    }

    setIsSaving(false);
  }

  async function handleClose(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsSaving(true);
    setFormError(null);
    setFieldErrors({});

    const result = await onCloseShift(actualCash, reason, closeKey);

    if (result.ok) {
      setReason("");
      setCloseKey(createIdempotencyKey());
    } else {
      setFormError(result.message);
      setFieldErrors(result.fields ?? {});
      if (result.code === "SHIFT_CHANGED" || result.code === "IDEMPOTENCY_CONFLICT") {
        setCloseKey(createIdempotencyKey());
      }
    }

    setIsSaving(false);
  }

  return (
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)]">
      <div className="mb-5 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-sm text-[var(--muted)]">Касса и смена</p>
          <h2 className="text-2xl font-semibold tracking-tight">Операционный контур POS</h2>
        </div>
        {shift ? (
          <span className="rounded-full bg-[var(--accent-soft)] px-3 py-1.5 text-xs font-semibold text-[var(--accent)]">
            Смена открыта
          </span>
        ) : (
          <span className="rounded-full bg-[var(--warning-soft)] px-3 py-1.5 text-xs font-semibold text-[var(--warning)]">
            Смена не открыта
          </span>
        )}
      </div>

      <label className="block">
        <span className="text-sm font-semibold">Касса</span>
        <select
          value={registerId}
          onChange={(event) => onRegisterChange(event.target.value)}
          className={inputClass}
          disabled={!canOperate || isSaving || isLoading}
        >
          {activeRegisters.length === 0 ? <option value="">Нет активных касс</option> : null}
          {activeRegisters.map((register) => (
            <option key={register.id} value={register.id}>
              {register.name} · {register.warehouse.name}
            </option>
          ))}
        </select>
      </label>

      {selectedRegister ? (
        <p className="mt-3 text-sm leading-6 text-[var(--muted)]">
          Склад: {selectedRegister.warehouse.name}. Валюта: {selectedRegister.currency}. Оплата:
          {" "}
          {selectedRegister.payment_methods.join(", ") || "cash"}.
        </p>
      ) : null}

      {isLoading ? (
        <p className="mt-4 text-sm text-[var(--muted)]">Загружаю текущую смену…</p>
      ) : null}

      {error ? (
        <div className="mt-4 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
          <p>{error}</p>
          <button type="button" onClick={onRetry} className={`${secondaryButtonClass} mt-3`}>
            Повторить
          </button>
        </div>
      ) : null}

      {shift ? (
        <form onSubmit={handleClose} className="mt-5 grid gap-4 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4">
          <div className="grid gap-3 text-sm sm:grid-cols-2">
            <div>
              <span className="text-[var(--muted)]">Смена</span>
              <p className="font-mono">{shift.id}</p>
            </div>
            <div>
              <span className="text-[var(--muted)]">Кассир</span>
              <p>{shift.cashier.full_name || shift.cashier.email}</p>
            </div>
            <div>
              <span className="text-[var(--muted)]">Открыта</span>
              <p>{formatDateTime(shift.opened_at)}</p>
            </div>
            <div>
              <span className="text-[var(--muted)]">Ожидаемая наличность</span>
              <p className="font-semibold">
                {shift.expected_cash} {selectedRegister?.currency ?? "KZT"}
              </p>
            </div>
          </div>

          <label className="block">
            <span className="text-sm font-semibold">Фактическая наличность</span>
            <input
              value={actualCash}
              onChange={(event) => setActualCash(event.target.value)}
              className={inputClass}
              inputMode="decimal"
              disabled={!canOperate || isSaving}
            />
            <FieldError message={fieldErrors.actual_cash} />
          </label>

          <label className="block">
            <span className="text-sm font-semibold">Причина расхождения / закрытия чужой смены</span>
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              className={`${inputClass} min-h-24`}
              disabled={!canOperate || isSaving}
            />
            <FieldError message={fieldErrors.reason} />
          </label>

          <FieldError message={formError ?? undefined} />

          <div className="flex flex-wrap gap-2">
            <button
              type="submit"
              className={dangerButtonClass}
              disabled={!canOperate || isSaving}
            >
              {isSaving ? "Закрываю…" : "Закрыть смену"}
            </button>
            <button type="button" onClick={onRetry} className={secondaryButtonClass} disabled={isSaving}>
              Обновить смену
            </button>
          </div>
        </form>
      ) : (
        <form onSubmit={handleOpen} className="mt-5 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4">
          <label className="block">
            <span className="text-sm font-semibold">Разменная наличность на старте</span>
            <input
              value={openingCash}
              onChange={(event) => setOpeningCash(event.target.value)}
              className={inputClass}
              inputMode="decimal"
              disabled={!canOperate || isSaving || !registerId}
            />
            <FieldError message={fieldErrors.opening_cash} />
          </label>

          <FieldError message={formError ?? undefined} />

          <button
            type="submit"
            className={`${primaryButtonClass} mt-4`}
            disabled={!canOperate || isSaving || !registerId}
          >
            {isSaving ? "Открываю…" : "Открыть смену"}
          </button>
        </form>
      )}
    </section>
  );
}
