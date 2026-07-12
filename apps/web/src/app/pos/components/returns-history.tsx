"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { type FormEvent, useEffect, useRef, useState } from "react";

import { ReturnSlipView } from "@/app/pos/components/return-slip-view";
import {
  EmptyState,
  ErrorState,
  FieldError,
  dangerButtonClass,
  formatDateTime,
  getFieldError,
  inputClass,
  secondaryButtonClass,
} from "@/app/pos/components/shared";
import {
  cancelReturn,
  createIdempotencyKey,
  getReturn,
  listReturns,
  type POSReturn,
  type Register,
  type ReturnHistoryItem,
  type ReturnReason,
  type ReturnState,
} from "@/lib/pos";

const RETURNS_PAGE_SIZE = 10;

const stateLabels: Record<ReturnState, string> = {
  submitted: "Проведён",
  cancelled: "Отменён",
  pending_recovery: "Требует восстановления",
};

const reasonOptions: { value: ReturnReason; label: string }[] = [
  { value: "customer_request", label: "Запрос клиента" },
  { value: "cashier_error", label: "Ошибка кассира" },
  { value: "damaged", label: "Повреждение" },
  { value: "other", label: "Другое" },
];

const friendlyCancelErrors: Record<string, string> = {
  RETURN_CANCEL_NOT_ALLOWED: "Этот возврат нельзя отменить в текущем состоянии или вашей роли.",
  RETURN_ALREADY_CANCELLED: "Возврат уже отменён.",
  IDEMPOTENCY_KEY_REUSED:
    "Ключ идемпотентности уже использован с другим запросом. Я подготовил новый ключ.",
  RETURN_RECOVERY_REQUIRED: "Отмена требует ручного восстановления. Не повторяйте операцию вслепую.",
  ERPNEXT_UNAVAILABLE: "ERPNext временно недоступен. Повторите позже с тем же ключом.",
  API_TIMEOUT: "Backend не ответил вовремя. Безопасный retry использует тот же Idempotency-Key.",
};

function friendlyCancelMessage(code: string, fallback: string) {
  return friendlyCancelErrors[code] ?? fallback;
}

export function ReturnsHistory({
  registers,
  currentRegisterId,
  canCancelReturns,
  refreshToken,
  onChanged,
}: {
  registers: Register[];
  currentRegisterId: string;
  canCancelReturns: boolean;
  refreshToken: number;
  onChanged: (posReturn: POSReturn) => void;
}) {
  const [query, setQuery] = useState("");
  const [saleId, setSaleId] = useState("");
  const [registerId, setRegisterId] = useState(currentRegisterId);
  const [cashierEmail, setCashierEmail] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [state, setState] = useState<ReturnState | "">("");
  const [appliedFilters, setAppliedFilters] = useState({
    q: "",
    saleId: "",
    registerId: currentRegisterId,
    cashierEmail: "",
    dateFrom: "",
    dateTo: "",
    state: "" as ReturnState | "",
  });
  const [items, setItems] = useState<ReturnHistoryItem[]>([]);
  const [count, setCount] = useState(0);
  const [offset, setOffset] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedReturn, setSelectedReturn] = useState<POSReturn | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [cancelReason, setCancelReason] = useState<ReturnReason | "">("");
  const [cancelComment, setCancelComment] = useState("");
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [cancelFieldErrors, setCancelFieldErrors] = useState<Record<string, string>>({});
  const [cancelKey, setCancelKey] = useState(createIdempotencyKey);
  const [isCancelling, setIsCancelling] = useState(false);
  const requestIdRef = useRef(0);

  const currentPage = Math.floor(offset / RETURNS_PAGE_SIZE) + 1;
  const totalPages = Math.max(1, Math.ceil(count / RETURNS_PAGE_SIZE));
  const hasPreviousPage = offset > 0;
  const hasNextPage = offset + items.length < count;

  async function refreshReturns(next?: {
    filters?: typeof appliedFilters;
    offset?: number;
  }) {
    const filters = next?.filters ?? appliedFilters;
    const nextOffset = next?.offset ?? offset;
    const requestId = ++requestIdRef.current;

    setIsLoading(true);
    setError(null);

    const result = await listReturns({
      q: filters.q,
      saleId: filters.saleId,
      registerId: filters.registerId,
      cashierEmail: filters.cashierEmail,
      dateFrom: filters.dateFrom,
      dateTo: filters.dateTo,
      state: filters.state,
      limit: RETURNS_PAGE_SIZE,
      offset: nextOffset,
    });

    if (requestId !== requestIdRef.current) {
      return;
    }

    if (result.status === "success") {
      setItems(result.data.items);
      setCount(result.data.count);
      setOffset(result.data.offset);
    } else {
      setError(result.error.message);
    }

    setIsLoading(false);
  }

  useEffect(() => {
    setRegisterId(currentRegisterId);
    setAppliedFilters((filters) => ({ ...filters, registerId: currentRegisterId }));
  }, [currentRegisterId]);

  useEffect(() => {
    void refreshReturns({ offset: 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshToken]);

  async function handleApplyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const filters = {
      q: query.trim(),
      saleId: saleId.trim(),
      registerId,
      cashierEmail: cashierEmail.trim(),
      dateFrom,
      dateTo,
      state,
    };
    setAppliedFilters(filters);
    await refreshReturns({ filters, offset: 0 });
  }

  async function handleResetFilters() {
    const filters = {
      q: "",
      saleId: "",
      registerId: currentRegisterId,
      cashierEmail: "",
      dateFrom: "",
      dateTo: "",
      state: "" as ReturnState | "",
    };
    setQuery("");
    setSaleId("");
    setRegisterId(currentRegisterId);
    setCashierEmail("");
    setDateFrom("");
    setDateTo("");
    setState("");
    setAppliedFilters(filters);
    await refreshReturns({ filters, offset: 0 });
  }

  async function loadReturnDetail(returnId: string) {
    setDetailError(null);
    setCancelError(null);
    setCancelFieldErrors({});
    setCancelReason("");
    setCancelComment("");
    setCancelKey(createIdempotencyKey());

    const result = await getReturn(returnId);

    if (result.status === "success") {
      setSelectedReturn(result.data);
    } else {
      setDetailError(result.error.message);
    }
  }

  async function handleCancelReturn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!selectedReturn || !canCancelReturns) {
      return;
    }

    if (!cancelReason) {
      setCancelFieldErrors({ reason: "Выберите причину отмены возврата." });
      return;
    }

    setIsCancelling(true);
    setCancelError(null);
    setCancelFieldErrors({});

    const result = await cancelReturn(
      selectedReturn.return_id,
      {
        reason: cancelReason,
        comment: cancelComment,
      },
      cancelKey,
    );

    if (result.status === "success") {
      setSelectedReturn(result.data);
      setCancelKey(createIdempotencyKey());
      setCancelReason("");
      setCancelComment("");
      onChanged(result.data);
      await refreshReturns();
    } else {
      setCancelError(friendlyCancelMessage(result.error.code, result.error.message));
      setCancelFieldErrors(result.error.fields);
      if (result.error.code === "IDEMPOTENCY_KEY_REUSED") {
        setCancelKey(createIdempotencyKey());
      }
    }

    setIsCancelling(false);
  }

  return (
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)]">
      <div className="mb-5">
        <p className="text-sm text-[var(--muted)]">Возвраты</p>
        <h2 className="text-2xl font-semibold tracking-tight">История возвратов</h2>
      </div>

      <form
        onSubmit={handleApplyFilters}
        className="grid gap-3 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4 lg:grid-cols-7"
      >
        <label className="block">
          <span className="text-sm font-semibold">Поиск</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className={inputClass}
            type="search"
            placeholder="Return, sale, чек"
          />
        </label>
        <label className="block">
          <span className="text-sm font-semibold">Sale ID</span>
          <input
            value={saleId}
            onChange={(event) => setSaleId(event.target.value)}
            className={inputClass}
            placeholder="SALE-..."
          />
        </label>
        <label className="block">
          <span className="text-sm font-semibold">Касса</span>
          <select
            value={registerId}
            onChange={(event) => setRegisterId(event.target.value)}
            className={inputClass}
          >
            <option value="">Все кассы</option>
            {registers.map((register) => (
              <option key={register.id} value={register.id}>
                {register.name}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="text-sm font-semibold">Кассир email</span>
          <input
            value={cashierEmail}
            onChange={(event) => setCashierEmail(event.target.value)}
            className={inputClass}
            type="email"
            placeholder="cashier@example.kz"
          />
        </label>
        <label className="block">
          <span className="text-sm font-semibold">Статус</span>
          <select
            value={state}
            onChange={(event) => setState(event.target.value as ReturnState | "")}
            className={inputClass}
          >
            <option value="">Все</option>
            <option value="submitted">Проведён</option>
            <option value="cancelled">Отменён</option>
            <option value="pending_recovery">Recovery</option>
          </select>
        </label>
        <label className="block">
          <span className="text-sm font-semibold">Дата от</span>
          <input
            value={dateFrom}
            onChange={(event) => setDateFrom(event.target.value)}
            className={inputClass}
            type="date"
          />
        </label>
        <label className="block">
          <span className="text-sm font-semibold">Дата до</span>
          <input
            value={dateTo}
            onChange={(event) => setDateTo(event.target.value)}
            className={inputClass}
            type="date"
          />
        </label>
        <div className="flex flex-wrap gap-2 lg:col-span-7">
          <button type="submit" className={secondaryButtonClass} disabled={isLoading}>
            Применить фильтры возвратов
          </button>
          <button
            type="button"
            className={secondaryButtonClass}
            onClick={() => void handleResetFilters()}
            disabled={isLoading}
          >
            Сбросить
          </button>
        </div>
      </form>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-sm text-[var(--muted)]">
        <span>
          Страница {currentPage} из {totalPages}. Возвратов: {count}
        </span>
        <div className="flex gap-2">
          <button
            type="button"
            className={secondaryButtonClass}
            disabled={!hasPreviousPage || isLoading}
            onClick={() =>
              void refreshReturns({ offset: Math.max(0, offset - RETURNS_PAGE_SIZE) })
            }
          >
            Назад
          </button>
          <button
            type="button"
            className={secondaryButtonClass}
            disabled={!hasNextPage || isLoading}
            onClick={() => void refreshReturns({ offset: offset + RETURNS_PAGE_SIZE })}
          >
            Вперёд
          </button>
        </div>
      </div>

      <div className="mt-4">
        {isLoading ? (
          <p className="text-sm text-[var(--muted)]">Загружаю историю возвратов…</p>
        ) : null}
        {error ? <ErrorState message={error} onRetry={() => void refreshReturns()} /> : null}
        {!isLoading && !error && items.length === 0 ? (
          <EmptyState>Возвратов по выбранным фильтрам пока нет.</EmptyState>
        ) : null}
        {!isLoading && !error && items.length > 0 ? (
          <div className="grid gap-3">
            {items.map((item) => (
              <article
                key={item.return_id}
                className="rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4"
              >
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div>
                    <p className="font-mono text-xs text-[var(--muted)]">
                      {item.return_receipt_number}
                    </p>
                    <h3 className="font-semibold">
                      {item.refund_total} {item.currency} · {stateLabels[item.state]}
                    </h3>
                    <p className="mt-1 text-sm text-[var(--muted)]">
                      Sale {item.receipt_number} · касса {item.register_id} · {item.cashier_email} ·{" "}
                      {formatDateTime(item.created_at)}
                    </p>
                  </div>
                  <button
                    type="button"
                    className={secondaryButtonClass}
                    onClick={() => void loadReturnDetail(item.return_id)}
                  >
                    Детали
                  </button>
                </div>
              </article>
            ))}
          </div>
        ) : null}
      </div>

      {detailError ? (
        <p className="mt-4 text-sm text-red-700 dark:text-red-300" role="alert">
          {detailError}
        </p>
      ) : null}

      {selectedReturn ? (
        <div className="mt-5 grid gap-5">
          <ReturnSlipView posReturn={selectedReturn} />

          {canCancelReturns && selectedReturn.state === "submitted" ? (
            <form
              onSubmit={handleCancelReturn}
              className="rounded-xl border border-red-200 bg-red-50 p-4 dark:border-red-900 dark:bg-red-950"
            >
              <h3 className="font-semibold text-red-800 dark:text-red-200">
                Отмена возврата Owner/Admin
              </h3>
              <p className="mt-1 text-sm text-red-700 dark:text-red-300">
                Cashier не видит это действие. Отмена отправляется с обязательным Idempotency-Key.
              </p>
              <div className="mt-4 grid gap-4 md:grid-cols-2">
                <label className="block">
                  <span className="text-sm font-semibold">Причина отмены</span>
                  <select
                    value={cancelReason}
                    onChange={(event) => setCancelReason(event.target.value as ReturnReason | "")}
                    className={inputClass}
                    disabled={isCancelling}
                  >
                    <option value="">Выберите причину</option>
                    {reasonOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                  <FieldError message={getFieldError(cancelFieldErrors, "reason")} />
                </label>
                <label className="block">
                  <span className="text-sm font-semibold">Комментарий</span>
                  <input
                    value={cancelComment}
                    onChange={(event) => setCancelComment(event.target.value)}
                    className={inputClass}
                    maxLength={500}
                    disabled={isCancelling}
                  />
                </label>
              </div>
              {cancelError ? (
                <p className="mt-3 text-sm text-red-700 dark:text-red-300" role="alert">
                  {cancelError}
                </p>
              ) : null}
              <div className="mt-4 flex flex-wrap items-center gap-3">
                <button type="submit" className={dangerButtonClass} disabled={isCancelling}>
                  {isCancelling ? "Отменяю возврат…" : "Отменить возврат"}
                </button>
                <span className="font-mono text-xs text-red-700 dark:text-red-300">
                  Idempotency-Key: {cancelKey}
                </span>
              </div>
            </form>
          ) : null}

          {!canCancelReturns && selectedReturn.state === "submitted" ? (
            <EmptyState>Отмена возврата доступна только Owner/Admin. Для Cashier действие скрыто.</EmptyState>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
