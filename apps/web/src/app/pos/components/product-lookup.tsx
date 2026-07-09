import { type FormEvent, type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  EmptyState,
  ErrorState,
  inputClass,
  primaryButtonClass,
  secondaryButtonClass,
} from "@/app/pos/components/shared";
import { listPOSProducts, type POSProduct } from "@/lib/pos";

const PRODUCT_PAGE_SIZE = 20;

export function ProductLookup({
  registerId,
  disabled,
  onAddProduct,
}: {
  registerId: string;
  disabled: boolean;
  onAddProduct: (product: POSProduct) => void;
}) {
  const [scannerValue, setScannerValue] = useState("");
  const [scannerError, setScannerError] = useState<string | null>(null);
  const [isScanning, setIsScanning] = useState(false);

  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [products, setProducts] = useState<POSProduct[]>([]);
  const [count, setCount] = useState(0);
  const [offset, setOffset] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const scannerRef = useRef<HTMLInputElement | null>(null);
  const searchRequestId = useRef(0);
  const scannerRequestId = useRef(0);

  const currentPage = Math.floor(offset / PRODUCT_PAGE_SIZE) + 1;
  const totalPages = Math.max(1, Math.ceil(count / PRODUCT_PAGE_SIZE));
  const hasPreviousPage = offset > 0;
  const hasNextPage = offset + products.length < count;

  const canSearch = useMemo(() => Boolean(registerId) && !disabled, [disabled, registerId]);

  useEffect(() => {
    if (!disabled) {
      scannerRef.current?.focus();
    }
  }, [disabled, registerId]);

  async function refreshProducts(next?: { q?: string; offset?: number }) {
    if (!registerId) {
      return;
    }

    const nextQuery = next?.q ?? appliedQuery;
    const nextOffset = next?.offset ?? offset;
    const requestId = ++searchRequestId.current;

    setIsLoading(true);
    setError(null);

    const result = await listPOSProducts({
      registerId,
      q: nextQuery,
      limit: PRODUCT_PAGE_SIZE,
      offset: nextOffset,
    });

    if (requestId !== searchRequestId.current) {
      return;
    }

    if (result.status === "success") {
      setProducts(result.data.items);
      setCount(result.data.count);
      setOffset(result.data.offset);
    } else {
      setError(result.error.message);
    }

    setIsLoading(false);
  }

  async function exactLookup(barcode: string) {
    const normalizedBarcode = barcode.trim();

    if (!normalizedBarcode || !registerId) {
      scannerRef.current?.focus();
      return;
    }

    const requestId = ++scannerRequestId.current;
    setIsScanning(true);
    setScannerError(null);

    const result = await listPOSProducts({
      registerId,
      barcode: normalizedBarcode,
      limit: 1,
      offset: 0,
    });

    if (requestId !== scannerRequestId.current) {
      return;
    }

    if (result.status === "success" && result.data.items.length > 0) {
      onAddProduct(result.data.items[0]);
      setScannerValue("");
    } else if (result.status === "success") {
      setScannerError("Товар по штрихкоду не найден.");
    } else {
      setScannerError(result.error.message);
    }

    setIsScanning(false);
    scannerRef.current?.focus();
  }

  async function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedQuery = query.trim();
    setAppliedQuery(normalizedQuery);
    await refreshProducts({ q: normalizedQuery, offset: 0 });
  }

  async function handleScannerKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Enter") {
      return;
    }

    event.preventDefault();
    await exactLookup(scannerValue);
  }

  function addProduct(product: POSProduct) {
    onAddProduct(product);
    scannerRef.current?.focus();
  }

  return (
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)]">
      <div className="mb-5">
        <p className="text-sm text-[var(--muted)]">Сканер и поиск</p>
        <h2 className="text-2xl font-semibold tracking-tight">Товары в чек</h2>
      </div>

      <label className="block">
        <span className="text-sm font-semibold">Сканер штрихкода</span>
        <input
          ref={scannerRef}
          value={scannerValue}
          onChange={(event) => setScannerValue(event.target.value)}
          onKeyDown={handleScannerKeyDown}
          className={inputClass}
          placeholder="Отсканируйте barcode и нажмите Enter"
          disabled={!canSearch || isScanning}
          autoComplete="off"
        />
      </label>
      <p className="mt-2 text-xs leading-5 text-[var(--muted)]">
        Поле возвращает фокус после добавления товара, ошибки или продажи — удобно для поточного
        сканирования.
      </p>
      {scannerError ? (
        <p className="mt-2 text-sm text-red-700 dark:text-red-300" role="alert">
          {scannerError}
        </p>
      ) : null}

      <form onSubmit={handleSearch} className="mt-5 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4">
        <div className="grid gap-3 md:grid-cols-[1fr_auto]">
          <label className="block">
            <span className="text-sm font-semibold">Поиск товаров</span>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              className={inputClass}
              type="search"
              placeholder="Название, SKU или barcode"
              disabled={!canSearch || isLoading}
            />
          </label>
          <div className="flex items-end">
            <button
              type="submit"
              className={secondaryButtonClass}
              disabled={!canSearch || isLoading}
            >
              Найти товары
            </button>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-sm text-[var(--muted)]">
          <span>
            Страница {currentPage} из {totalPages}. Найдено: {count}
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={!hasPreviousPage || isLoading}
              onClick={() => void refreshProducts({ offset: Math.max(0, offset - PRODUCT_PAGE_SIZE) })}
            >
              Назад
            </button>
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={!hasNextPage || isLoading}
              onClick={() => void refreshProducts({ offset: offset + PRODUCT_PAGE_SIZE })}
            >
              Вперёд
            </button>
          </div>
        </div>
      </form>

      <div className="mt-4">
        {isLoading ? <p className="text-sm text-[var(--muted)]">Ищу товары…</p> : null}
        {error ? <ErrorState message={error} onRetry={() => void refreshProducts()} /> : null}
        {!isLoading && !error && products.length === 0 ? (
          <EmptyState>Введите поисковую строку или используйте barcode scanner.</EmptyState>
        ) : null}
        {!isLoading && !error && products.length > 0 ? (
          <div className="grid gap-3">
            {products.map((product) => (
              <article
                key={product.id}
                className="rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4"
              >
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div>
                    <p className="font-mono text-xs text-[var(--muted)]">{product.sku}</p>
                    <h3 className="text-lg font-semibold">{product.name}</h3>
                    <p className="mt-1 text-sm text-[var(--muted)]">
                      Остаток: {product.available} {product.unit}. Barcode: {product.barcode ?? "—"}
                    </p>
                  </div>
                  <div className="flex flex-col gap-2 sm:items-end">
                    <span className="rounded-full bg-[var(--accent-soft)] px-3 py-1 text-xs font-semibold text-[var(--accent)]">
                      {product.sale_price} {product.currency}
                    </span>
                    <button
                      type="button"
                      className={primaryButtonClass}
                      onClick={() => addProduct(product)}
                      disabled={disabled}
                    >
                      Добавить
                    </button>
                  </div>
                </div>
              </article>
            ))}
          </div>
        ) : null}
      </div>
    </section>
  );
}
