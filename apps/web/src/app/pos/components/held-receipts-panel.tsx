/* eslint-disable react-hooks/set-state-in-effect */

import { type FormEvent, useEffect, useState } from "react";

import {
  EmptyState,
  FieldError,
  formatDateTime,
  inputClass,
  primaryButtonClass,
  secondaryButtonClass,
  type POSActionResult,
} from "@/app/pos/components/shared";
import { createIdempotencyKey, type HeldReceipt } from "@/lib/pos";

export function HeldReceiptsPanel({
  items,
  count,
  offset,
  pageSize,
  selectedHeld,
  canOperate,
  canHoldCart,
  isLoading,
  error,
  onRetry,
  onPrevious,
  onNext,
  onCreateHeld,
  onUpdateSelectedHeld,
  onReloadHeld,
  onDeleteHeld,
}: {
  items: HeldReceipt[];
  count: number;
  offset: number;
  pageSize: number;
  selectedHeld: HeldReceipt | null;
  canOperate: boolean;
  canHoldCart: boolean;
  isLoading: boolean;
  error: string | null;
  onRetry: () => void;
  onPrevious: () => void;
  onNext: () => void;
  onCreateHeld: (label: string, idempotencyKey: string) => Promise<POSActionResult>;
  onUpdateSelectedHeld: (label: string) => Promise<POSActionResult>;
  onReloadHeld: (held: HeldReceipt) => void;
  onDeleteHeld: (held: HeldReceipt) => Promise<POSActionResult>;
}) {
  const [label, setLabel] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState(createIdempotencyKey);
  const [formError, setFormError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);

  const currentPage = Math.floor(offset / pageSize) + 1;
  const totalPages = Math.max(1, Math.ceil(count / pageSize));
  const hasPreviousPage = offset > 0;
  const hasNextPage = offset + items.length < count;

  useEffect(() => {
    setLabel(selectedHeld?.label ?? "");
    setFormError(null);
  }, [selectedHeld]);

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsSaving(true);
    setFormError(null);

    const result = await onCreateHeld(label, idempotencyKey);

    if (result.ok) {
      setIdempotencyKey(createIdempotencyKey());
      setLabel("");
    } else {
      setFormError(result.message);
      if (result.code === "IDEMPOTENCY_CONFLICT") {
        setIdempotencyKey(createIdempotencyKey());
      }
    }

    setIsSaving(false);
  }

  async function handleUpdate() {
    setIsSaving(true);
    setFormError(null);

    const result = await onUpdateSelectedHeld(label);

    if (!result.ok) {
      setFormError(result.message);
    }

    setIsSaving(false);
  }

  async function handleDelete(held: HeldReceipt) {
    setIsSaving(true);
    setFormError(null);

    const result = await onDeleteHeld(held);

    if (!result.ok) {
      setFormError(result.message);
    }

    setIsSaving(false);
  }

  return (
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)]">
      <div className="mb-5">
        <p className="text-sm text-[var(--muted)]">Отложенные чеки</p>
        <h2 className="text-2xl font-semibold tracking-tight">Hold / reload</h2>
      </div>

      <form onSubmit={handleCreate} className="rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4">
        <label className="block">
          <span className="text-sm font-semibold">Метка чека</span>
          <input
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            className={inputClass}
            placeholder="Например: клиент вернётся через 5 минут"
            disabled={!canOperate || isSaving}
          />
        </label>

        <FieldError message={formError ?? undefined} />

        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="submit"
            className={primaryButtonClass}
            disabled={!canOperate || !canHoldCart || isSaving}
          >
            {isSaving ? "Сохраняю…" : "Отложить чек"}
          </button>
          <button
            type="button"
            className={secondaryButtonClass}
            disabled={!canOperate || !selectedHeld || !canHoldCart || isSaving}
            onClick={() => void handleUpdate()}
          >
            Обновить отложенный чек
          </button>
        </div>
      </form>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-sm text-[var(--muted)]">
        <span>
          Страница {currentPage} из {totalPages}. Чеков: {count}
        </span>
        <div className="flex gap-2">
          <button
            type="button"
            className={secondaryButtonClass}
            disabled={!hasPreviousPage || isLoading}
            onClick={onPrevious}
          >
            Назад
          </button>
          <button
            type="button"
            className={secondaryButtonClass}
            disabled={!hasNextPage || isLoading}
            onClick={onNext}
          >
            Вперёд
          </button>
        </div>
      </div>

      {isLoading ? <p className="mt-4 text-sm text-[var(--muted)]">Загружаю отложенные чеки…</p> : null}
      {error ? (
        <div className="mt-4 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
          <p>{error}</p>
          <button type="button" onClick={onRetry} className={`${secondaryButtonClass} mt-3`}>
            Повторить
          </button>
        </div>
      ) : null}
      {!isLoading && !error && items.length === 0 ? (
        <div className="mt-4">
          <EmptyState>Отложенных чеков по текущей смене пока нет.</EmptyState>
        </div>
      ) : null}

      {!isLoading && !error && items.length > 0 ? (
        <div className="mt-4 grid gap-3">
          {items.map((held) => (
            <article
              key={held.id}
              className={`rounded-xl border p-4 ${
                selectedHeld?.id === held.id
                  ? "border-[var(--accent)] bg-[var(--accent-soft)]"
                  : "border-[var(--border)] bg-[var(--surface-muted)]"
              }`}
            >
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <p className="font-mono text-xs text-[var(--muted)]">{held.id}</p>
                  <h3 className="font-semibold">{held.label || "Без метки"}</h3>
                  <p className="mt-1 text-sm text-[var(--muted)]">
                    Позиций: {held.lines.length}. Итого: {held.grand_total}. Обновлён:
                    {" "}
                    {formatDateTime(held.updated_at)}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2 sm:justify-end">
                  <button
                    type="button"
                    className={secondaryButtonClass}
                    onClick={() => onReloadHeld(held)}
                    disabled={!canOperate || isSaving}
                  >
                    Загрузить
                  </button>
                  <button
                    type="button"
                    className={secondaryButtonClass}
                    onClick={() => void handleDelete(held)}
                    disabled={!canOperate || isSaving}
                  >
                    Удалить
                  </button>
                </div>
              </div>
            </article>
          ))}
        </div>
      ) : null}
    </section>
  );
}
