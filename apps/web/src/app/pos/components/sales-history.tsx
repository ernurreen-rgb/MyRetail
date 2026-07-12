/* eslint-disable react-hooks/set-state-in-effect */

import { type FormEvent, useEffect, useRef, useState } from "react";

import {
  EmptyState,
  ErrorState,
  formatDateTime,
  inputClass,
  primaryButtonClass,
  secondaryButtonClass,
} from "@/app/pos/components/shared";
import { getSale, listSales, type Register, type Sale } from "@/lib/pos";

const SALES_PAGE_SIZE = 10;
const returnStatusLabels: Record<Sale["return_status"], string> = {
  none: "возвратов нет",
  partial: "частичный возврат",
  full: "полный возврат",
};

export function SalesHistory({
  registers,
  currentRegisterId,
  refreshToken,
  onStartReturn,
}: {
  registers: Register[];
  currentRegisterId: string;
  refreshToken: number;
  onStartReturn: (sale: Sale) => void;
}) {
  const [query, setQuery] = useState("");
  const [registerId, setRegisterId] = useState(currentRegisterId);
  const [cashierEmail, setCashierEmail] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [appliedFilters, setAppliedFilters] = useState({
    q: "",
    registerId: currentRegisterId,
    cashierEmail: "",
    dateFrom: "",
    dateTo: "",
  });
  const [items, setItems] = useState<Sale[]>([]);
  const [count, setCount] = useState(0);
  const [offset, setOffset] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedSale, setSelectedSale] = useState<Sale | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const requestIdRef = useRef(0);

  const currentPage = Math.floor(offset / SALES_PAGE_SIZE) + 1;
  const totalPages = Math.max(1, Math.ceil(count / SALES_PAGE_SIZE));
  const hasPreviousPage = offset > 0;
  const hasNextPage = offset + items.length < count;

  async function refreshSales(next?: {
    filters?: typeof appliedFilters;
    offset?: number;
  }) {
    const filters = next?.filters ?? appliedFilters;
    const nextOffset = next?.offset ?? offset;
    const requestId = ++requestIdRef.current;

    setIsLoading(true);
    setError(null);

    const result = await listSales({
      q: filters.q,
      registerId: filters.registerId,
      cashierEmail: filters.cashierEmail,
      dateFrom: filters.dateFrom,
      dateTo: filters.dateTo,
      limit: SALES_PAGE_SIZE,
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
    void refreshSales({ offset: 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshToken]);

  async function handleApplyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const filters = {
      q: query.trim(),
      registerId,
      cashierEmail: cashierEmail.trim(),
      dateFrom,
      dateTo,
    };
    setAppliedFilters(filters);
    await refreshSales({ filters, offset: 0 });
  }

  async function handleResetFilters() {
    const filters = {
      q: "",
      registerId: currentRegisterId,
      cashierEmail: "",
      dateFrom: "",
      dateTo: "",
    };
    setQuery("");
    setRegisterId(currentRegisterId);
    setCashierEmail("");
    setDateFrom("");
    setDateTo("");
    setAppliedFilters(filters);
    await refreshSales({ filters, offset: 0 });
  }

  async function loadSaleDetail(saleId: string) {
    setDetailError(null);
    const result = await getSale(saleId);

    if (result.status === "success") {
      setSelectedSale(result.data);
    } else {
      setDetailError(result.error.message);
    }
  }

  return (
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)]">
      <div className="mb-5">
        <p className="text-sm text-[var(--muted)]">Продажи</p>
        <h2 className="text-2xl font-semibold tracking-tight">История продаж</h2>
      </div>

      <form onSubmit={handleApplyFilters} className="grid gap-3 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4 lg:grid-cols-5">
        <label className="block">
          <span className="text-sm font-semibold">Поиск</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className={inputClass}
            type="search"
            placeholder="Чек, товар, SKU"
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
        <div className="flex flex-wrap gap-2 lg:col-span-5">
          <button type="submit" className={secondaryButtonClass} disabled={isLoading}>
            Применить фильтры продаж
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
          Страница {currentPage} из {totalPages}. Продаж: {count}
        </span>
        <div className="flex gap-2">
          <button
            type="button"
            className={secondaryButtonClass}
            disabled={!hasPreviousPage || isLoading}
            onClick={() => void refreshSales({ offset: Math.max(0, offset - SALES_PAGE_SIZE) })}
          >
            Назад
          </button>
          <button
            type="button"
            className={secondaryButtonClass}
            disabled={!hasNextPage || isLoading}
            onClick={() => void refreshSales({ offset: offset + SALES_PAGE_SIZE })}
          >
            Вперёд
          </button>
        </div>
      </div>

      <div className="mt-4">
        {isLoading ? <p className="text-sm text-[var(--muted)]">Загружаю историю продаж…</p> : null}
        {error ? <ErrorState message={error} onRetry={() => void refreshSales()} /> : null}
        {!isLoading && !error && items.length === 0 ? (
          <EmptyState>Продаж по выбранным фильтрам пока нет.</EmptyState>
        ) : null}
        {!isLoading && !error && items.length > 0 ? (
          <div className="grid gap-3">
            {items.map((sale) => (
              <article
                key={sale.id}
                className="rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4"
              >
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div>
                    <p className="font-mono text-xs text-[var(--muted)]">{sale.receipt_number}</p>
                    <h3 className="font-semibold">
                      {sale.grand_total} {sale.currency}
                    </h3>
                    <p className="mt-1 text-sm text-[var(--muted)]">
                      {sale.register.name} · {sale.cashier.full_name || sale.cashier.email} ·
                      {" "}
                      {formatDateTime(sale.created_at)}
                    </p>
                    <p className="mt-1 text-sm text-[var(--muted)]">
                      Возвраты: {returnStatusLabels[sale.return_status]} · возвращено{" "}
                      {sale.returned_total} {sale.currency}
                    </p>
                  </div>
                  <button
                    type="button"
                    className={secondaryButtonClass}
                    onClick={() => void loadSaleDetail(sale.id)}
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
      {selectedSale ? (
        <div className="mt-4 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4">
          <h3 className="font-semibold">Детали продажи {selectedSale.receipt_number}</h3>
          <p className="mt-1 text-sm text-[var(--muted)]">
            Смена {selectedSale.shift_id}, чек {selectedSale.id}, сдача {selectedSale.change}
          </p>
          <div className="mt-3 grid gap-3 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-3 text-sm sm:grid-cols-3">
            <div>
              <p className="text-[var(--muted)]">Статус возврата</p>
              <p className="font-semibold">{returnStatusLabels[selectedSale.return_status]}</p>
            </div>
            <div>
              <p className="text-[var(--muted)]">Возвращено</p>
              <p className="font-semibold">
                {selectedSale.returned_total} {selectedSale.currency}
              </p>
            </div>
            <div>
              <p className="text-[var(--muted)]">Доступно действие</p>
              <button
                type="button"
                className={primaryButtonClass}
                onClick={() => onStartReturn(selectedSale)}
                disabled={selectedSale.return_status === "full"}
              >
                {selectedSale.return_status === "full" ? "Полностью возвращено" : "Оформить возврат"}
              </button>
            </div>
          </div>
          <ul className="mt-3 grid gap-2 text-sm">
            {selectedSale.lines.map((line) => (
              <li
                key={line.line_id ?? line.product_id}
                className="grid gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface)] p-3 sm:grid-cols-[1fr_auto]"
              >
                <span>
                  <span className="font-medium">{line.name}</span>
                  <span className="block text-[var(--muted)]">
                    Продано {line.quantity} {line.unit} · возвращено {line.returned_quantity} ·
                    доступно {line.available_to_return_quantity}
                  </span>
                </span>
                <span className="font-semibold">{line.total}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
