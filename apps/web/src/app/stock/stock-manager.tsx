"use client";

import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  cancelStockMovement,
  createIdempotencyKey,
  createStockMovement,
  emptyStockMovementFormValues,
  getStockOptions,
  listStockBalances,
  listStockMovements,
  type MovementStatus,
  type MovementType,
  type StockBalance,
  type StockMovement,
  type StockMovementFormValues,
  type StockOptions,
} from "@/lib/stock";

const inputClass =
  "mt-2 w-full rounded-xl border border-[var(--border)] bg-[var(--surface)] px-4 py-3 text-base outline-none transition focus:border-[var(--accent)] focus:ring-4 focus:ring-[var(--accent-soft)] disabled:cursor-not-allowed disabled:opacity-70";

const secondaryButtonClass =
  "rounded-xl border border-[var(--border)] bg-[var(--surface)] px-4 py-2.5 text-sm font-semibold transition hover:border-[var(--accent)] hover:text-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-60";

const primaryButtonClass =
  "rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-60";

const dangerButtonClass =
  "rounded-xl bg-red-700 px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-60";

const PAGE_SIZE = 20;
const HISTORY_PAGE_SIZE = 10;

const movementTypeLabels: Record<MovementType, string> = {
  receipt: "Оприходование",
  write_off: "Списание",
  transfer: "Перемещение",
  adjustment: "Корректировка",
};

const movementStatusLabels: Record<MovementStatus, string> = {
  posted: "Проведено",
  cancelled: "Отменено",
};

const movementTypeTone: Record<MovementType, string> = {
  receipt: "bg-[var(--accent-soft)] text-[var(--accent)]",
  write_off: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  transfer: "bg-[var(--warning-soft)] text-[var(--warning)]",
  adjustment: "bg-[var(--surface-muted)] text-[var(--muted)]",
};

type CancelFormState = {
  movement: StockMovement;
  reason: string;
  idempotencyKey: string;
};

function FieldError({ message }: { message?: string }) {
  if (!message) {
    return null;
  }

  return (
    <p className="mt-2 text-sm leading-5 text-red-700 dark:text-red-300" role="alert">
      {message}
    </p>
  );
}

function formatDate(value: string) {
  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(date);
}

function formatUser(movement: StockMovement) {
  return movement.created_by.full_name?.trim() || movement.created_by.email;
}

function findWarehouseName(options: StockOptions | null, warehouseId: string | null) {
  if (!warehouseId) {
    return "—";
  }

  return options?.warehouses.find((warehouse) => warehouse.id === warehouseId)?.name ?? warehouseId;
}

function findReasonName(options: StockOptions | null, movement: StockMovement) {
  if (!movement.reason_code) {
    return "—";
  }

  const source =
    movement.type === "write_off"
      ? options?.write_off_reasons
      : movement.type === "adjustment"
        ? options?.adjustment_reasons
        : [];

  return source?.find((reason) => reason.code === movement.reason_code)?.name ?? movement.reason_code;
}

function getFieldError(fields: Record<string, string>, ...keys: string[]) {
  for (const key of keys) {
    const message = fields[key];

    if (message) {
      return message;
    }
  }

  return undefined;
}

function getDefaultWarehouseId(options: StockOptions | null) {
  const activeWarehouses = options?.warehouses.filter((warehouse) => warehouse.is_active) ?? [];
  return (
    activeWarehouses.find((warehouse) => warehouse.is_default)?.id ??
    activeWarehouses[0]?.id ??
    ""
  );
}

function getFirstDifferentWarehouseId(
  options: StockOptions | null,
  warehouseId: string,
) {
  return (
    options?.warehouses.find(
      (warehouse) => warehouse.is_active && warehouse.id !== warehouseId,
    )?.id ?? ""
  );
}

function isDangerousMovement(type: MovementType) {
  return type === "write_off" || type === "adjustment" || type === "transfer";
}

export function StockManager({
  canManage,
  userRoles,
}: {
  canManage: boolean;
  userRoles: string[];
}) {
  const [balances, setBalances] = useState<StockBalance[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [offset, setOffset] = useState(0);
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [warehouseId, setWarehouseId] = useState("");
  const [isLoadingBalances, setIsLoadingBalances] = useState(true);
  const [balancesError, setBalancesError] = useState<string | null>(null);

  const [options, setOptions] = useState<StockOptions | null>(null);
  const [isLoadingOptions, setIsLoadingOptions] = useState(true);
  const [optionsError, setOptionsError] = useState<string | null>(null);

  const [selectedBalance, setSelectedBalance] = useState<StockBalance | null>(null);
  const [movements, setMovements] = useState<StockMovement[]>([]);
  const [movementsTotalCount, setMovementsTotalCount] = useState(0);
  const [movementsOffset, setMovementsOffset] = useState(0);
  const [movementTypeFilter, setMovementTypeFilter] = useState<MovementType | "">("");
  const [movementStatusFilter, setMovementStatusFilter] = useState<MovementStatus | "">("");
  const [isLoadingMovements, setIsLoadingMovements] = useState(false);
  const [movementsError, setMovementsError] = useState<string | null>(null);

  const [isFormOpen, setIsFormOpen] = useState(false);
  const [formValues, setFormValues] = useState<StockMovementFormValues>(
    emptyStockMovementFormValues(),
  );
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [formIdempotencyKey, setFormIdempotencyKey] = useState(createIdempotencyKey);

  const [cancelForm, setCancelForm] = useState<CancelFormState | null>(null);
  const [cancelFieldErrors, setCancelFieldErrors] = useState<Record<string, string>>({});
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [isCancelling, setIsCancelling] = useState(false);

  const [notice, setNotice] = useState<string | null>(null);

  const balancesRequestId = useRef(0);
  const movementsRequestId = useRef(0);

  const activeWarehouses = useMemo(
    () => options?.warehouses.filter((warehouse) => warehouse.is_active) ?? [],
    [options],
  );
  const canTransfer = activeWarehouses.length >= 2;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  const hasPreviousPage = offset > 0;
  const hasNextPage = offset + balances.length < totalCount;
  const movementsCurrentPage = Math.floor(movementsOffset / HISTORY_PAGE_SIZE) + 1;
  const movementsTotalPages = Math.max(1, Math.ceil(movementsTotalCount / HISTORY_PAGE_SIZE));
  const hasPreviousMovementsPage = movementsOffset > 0;
  const hasNextMovementsPage = movementsOffset + movements.length < movementsTotalCount;
  const roleLabel = userRoles.length > 0 ? userRoles.join(", ") : "без роли";

  async function refreshBalances(next?: {
    q?: string;
    warehouseId?: string;
    offset?: number;
  }) {
    const nextQuery = next?.q ?? appliedQuery;
    const nextWarehouseId = next?.warehouseId ?? warehouseId;
    const nextOffset = next?.offset ?? offset;
    const requestId = ++balancesRequestId.current;

    setIsLoadingBalances(true);
    setBalancesError(null);

    const result = await listStockBalances({
      q: nextQuery,
      warehouseId: nextWarehouseId,
      limit: PAGE_SIZE,
      offset: nextOffset,
    });

    if (requestId !== balancesRequestId.current) {
      return;
    }

    if (result.status === "success") {
      setBalances(result.data.items);
      setTotalCount(result.data.count);
      setOffset(result.data.offset);
      const nextSelectedBalance =
        result.data.items.find(
          (balance) =>
            selectedBalance?.product_id === balance.product_id &&
            selectedBalance.warehouse.id === balance.warehouse.id,
        ) ??
        result.data.items[0] ??
        null;

      setSelectedBalance(nextSelectedBalance);

      if (
        nextSelectedBalance &&
        (selectedBalance?.product_id !== nextSelectedBalance.product_id ||
          selectedBalance.warehouse.id !== nextSelectedBalance.warehouse.id)
      ) {
        setMovementTypeFilter("");
        setMovementStatusFilter("");
        setCancelForm(null);
        void refreshMovements({
          balance: nextSelectedBalance,
          type: "",
          status: "",
          offset: 0,
        });
      } else if (!nextSelectedBalance) {
        void refreshMovements({ balance: null });
      }
    } else {
      setBalancesError(result.error.message);
    }

    setIsLoadingBalances(false);
  }

  async function refreshMovements(next?: {
    balance?: StockBalance | null;
    type?: MovementType | "";
    status?: MovementStatus | "";
    offset?: number;
  }) {
    const balance = next?.balance === undefined ? selectedBalance : next.balance;

    if (!balance) {
      setMovements([]);
      setMovementsTotalCount(0);
      setMovementsOffset(0);
      setMovementsError(null);
      return;
    }

    const nextType = next?.type ?? movementTypeFilter;
    const nextStatus = next?.status ?? movementStatusFilter;
    const nextOffset = next?.offset ?? movementsOffset;
    const requestId = ++movementsRequestId.current;

    setIsLoadingMovements(true);
    setMovementsError(null);

    const result = await listStockMovements({
      productId: balance.product_id,
      warehouseId: balance.warehouse.id,
      type: nextType,
      status: nextStatus,
      limit: HISTORY_PAGE_SIZE,
      offset: nextOffset,
    });

    if (requestId !== movementsRequestId.current) {
      return;
    }

    if (result.status === "success") {
      setMovements(result.data.items);
      setMovementsTotalCount(result.data.count);
      setMovementsOffset(result.data.offset);
    } else {
      setMovementsError(result.error.message);
    }

    setIsLoadingMovements(false);
  }

  async function refreshOptions() {
    setIsLoadingOptions(true);
    setOptionsError(null);

    const result = await getStockOptions();

    if (result.status === "success") {
      setOptions(result.data);
    } else {
      setOptionsError(result.error.message);
    }

    setIsLoadingOptions(false);
  }

  useEffect(() => {
    let ignore = false;
    const requestId = ++balancesRequestId.current;

    async function loadInitialData() {
      const [balancesResult, optionsResult] = await Promise.all([
        listStockBalances({ limit: PAGE_SIZE, offset: 0 }),
        getStockOptions(),
      ]);

      if (ignore) {
        return;
      }

      if (requestId === balancesRequestId.current && balancesResult.status === "success") {
        setBalances(balancesResult.data.items);
        setTotalCount(balancesResult.data.count);
        setOffset(balancesResult.data.offset);
        const firstBalance = balancesResult.data.items[0] ?? null;

        if (firstBalance) {
          setSelectedBalance(firstBalance);
          const movementsRequest = ++movementsRequestId.current;
          setIsLoadingMovements(true);
          setMovementsError(null);

          const movementsResult = await listStockMovements({
            productId: firstBalance.product_id,
            warehouseId: firstBalance.warehouse.id,
            limit: HISTORY_PAGE_SIZE,
            offset: 0,
          });

          if (!ignore && movementsRequest === movementsRequestId.current) {
            if (movementsResult.status === "success") {
              setMovements(movementsResult.data.items);
              setMovementsTotalCount(movementsResult.data.count);
              setMovementsOffset(movementsResult.data.offset);
            } else {
              setMovementsError(movementsResult.error.message);
            }

            setIsLoadingMovements(false);
          }
        }
      } else if (
        requestId === balancesRequestId.current &&
        balancesResult.status === "error"
      ) {
        setBalancesError(balancesResult.error.message);
      }

      if (optionsResult.status === "success") {
        setOptions(optionsResult.data);
      } else {
        setOptionsError(optionsResult.error.message);
      }

      if (requestId === balancesRequestId.current) {
        setIsLoadingBalances(false);
      }
      setIsLoadingOptions(false);
    }

    void loadInitialData();

    return () => {
      ignore = true;
    };
  }, []);

  async function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedQuery = query.trim();
    setAppliedQuery(normalizedQuery);
    await refreshBalances({ q: normalizedQuery, offset: 0 });
  }

  async function handleWarehouseFilter(nextWarehouseId: string) {
    setWarehouseId(nextWarehouseId);
    await refreshBalances({ warehouseId: nextWarehouseId, offset: 0 });
  }

  async function handleSelectBalance(balance: StockBalance) {
    setSelectedBalance(balance);
    setMovementTypeFilter("");
    setMovementStatusFilter("");
    setCancelForm(null);
    await refreshMovements({
      balance,
      type: "",
      status: "",
      offset: 0,
    });
  }

  function openMovementForm(type: MovementType, balance = selectedBalance) {
    if (type === "transfer" && !canTransfer) {
      setNotice("Перемещение доступно только когда активно минимум два склада.");
      return;
    }

    const defaultWarehouseId =
      balance?.warehouse.id ?? formValues.warehouse_id ?? getDefaultWarehouseId(options);
    const destinationWarehouseId =
      type === "transfer" ? getFirstDifferentWarehouseId(options, defaultWarehouseId) : "";

    setIsFormOpen(true);
    setFormError(null);
    setFieldErrors({});
    setNotice(null);
    setFormIdempotencyKey(createIdempotencyKey());
    setFormValues(
      emptyStockMovementFormValues({
        type,
        product_id: balance?.product_id ?? "",
        warehouse_id: defaultWarehouseId,
        destination_warehouse_id: destinationWarehouseId,
      }),
    );
  }

  function closeMovementForm() {
    if (isSaving) {
      return;
    }

    setIsFormOpen(false);
    setFieldErrors({});
    setFormError(null);
  }

  function updateFormField(field: keyof StockMovementFormValues, value: string) {
    setFormValues((current) => {
      const next: StockMovementFormValues = {
        ...current,
        [field]: value,
      };

      if (field === "type") {
        const movementType = value as MovementType;
        next.reason_code = "";
        next.quantity = "";
        next.counted_quantity = "";
        next.expected_quantity = "";
        next.destination_warehouse_id =
          movementType === "transfer"
            ? getFirstDifferentWarehouseId(options, next.warehouse_id)
            : "";
      }

      if (field === "warehouse_id" && next.type === "transfer") {
        next.destination_warehouse_id = getFirstDifferentWarehouseId(options, value);
      }

      return next;
    });
    setFormIdempotencyKey(createIdempotencyKey());
    setFieldErrors((current) => {
      const next = { ...current };
      delete next[field];
      delete next[`lines.0.${field}`];
      return next;
    });
  }

  async function handleSaveMovement(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (isSaving) {
      return;
    }

    if (formValues.type === "transfer" && !canTransfer) {
      setFormError("Для перемещения нужны минимум два активных склада.");
      return;
    }

    if (
      isDangerousMovement(formValues.type) &&
      !window.confirm(`Подтвердить операцию «${movementTypeLabels[formValues.type]}»?`)
    ) {
      return;
    }

    setIsSaving(true);
    setFormError(null);
    setFieldErrors({});

    const result = await createStockMovement(formValues, formIdempotencyKey);

    if (result.status === "success") {
      setNotice(`Операция «${movementTypeLabels[result.data.type]}» проведена.`);
      setIsFormOpen(false);
      setFormIdempotencyKey(createIdempotencyKey());
      await refreshBalances();
      await refreshMovements({ offset: 0 });
    } else {
      setFormError(result.error.message);
      setFieldErrors(result.error.fields);
    }

    setIsSaving(false);
  }

  function openCancelForm(movement: StockMovement) {
    setCancelForm({
      movement,
      reason: "",
      idempotencyKey: createIdempotencyKey(),
    });
    setCancelError(null);
    setCancelFieldErrors({});
  }

  function updateCancelReason(reason: string) {
    setCancelForm((current) =>
      current
        ? {
            ...current,
            reason,
            idempotencyKey: createIdempotencyKey(),
          }
        : current,
    );
    setCancelFieldErrors((current) => {
      const next = { ...current };
      delete next.reason;
      return next;
    });
  }

  async function handleCancelMovement(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!cancelForm || isCancelling) {
      return;
    }

    if (!window.confirm("Отменить проведённое движение и создать обратную проводку?")) {
      return;
    }

    setIsCancelling(true);
    setCancelError(null);
    setCancelFieldErrors({});

    const result = await cancelStockMovement(
      cancelForm.movement.id,
      cancelForm.reason,
      cancelForm.idempotencyKey,
    );

    if (result.status === "success") {
      setNotice("Движение отменено, обратная проводка создана.");
      setCancelForm(null);
      await refreshBalances();
      await refreshMovements({ offset: 0 });
    } else {
      setCancelError(result.error.message);
      setCancelFieldErrors(result.error.fields);
    }

    setIsCancelling(false);
  }

  const movementReasonOptions =
    formValues.type === "write_off"
      ? options?.write_off_reasons ?? []
      : formValues.type === "adjustment"
        ? options?.adjustment_reasons ?? []
        : [];

  return (
    <div
      className={
        canManage
          ? "grid gap-6 xl:grid-cols-[minmax(0,1fr)_420px] xl:items-start"
          : "grid gap-6"
      }
    >
      <div className="grid gap-6">
        <section
          aria-labelledby="stock-balances-heading"
          className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)] sm:p-6"
        >
          <div className="mb-5 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <p className="text-sm text-[var(--muted)]">Остатки ERPNext через MyRetail API</p>
              <h2 id="stock-balances-heading" className="text-2xl font-semibold tracking-tight">
                Остатки по складам
              </h2>
              <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
                Показано {balances.length} из {totalCount}. Количества отображаются ровно
                строками, как вернул backend.
              </p>
              {!canManage ? (
                <p className="mt-2 text-sm font-semibold text-[var(--warning)]">
                  Режим просмотра: ваши роли ({roleLabel}) не разрешают операции склада.
                </p>
              ) : null}
            </div>
            {canManage ? (
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => openMovementForm("receipt")}
                  className={primaryButtonClass}
                  disabled={isLoadingOptions || activeWarehouses.length === 0}
                >
                  Новая операция
                </button>
              </div>
            ) : null}
          </div>

          <form onSubmit={handleSearch} className="mb-4 grid gap-3 lg:grid-cols-[1fr_260px_auto]">
            <label className="block">
              <span className="text-sm font-semibold">
                Поиск по названию, артикулу или штрихкоду
              </span>
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                className={inputClass}
                placeholder="Например: DEMO-001 или молоко"
              />
            </label>
            <label className="block">
              <span className="text-sm font-semibold">Склад</span>
              <select
                value={warehouseId}
                onChange={(event) => void handleWarehouseFilter(event.target.value)}
                className={inputClass}
                disabled={isLoadingOptions}
              >
                <option value="">Все склады</option>
                {options?.warehouses.map((warehouse) => (
                  <option key={warehouse.id} value={warehouse.id}>
                    {warehouse.name}
                    {warehouse.is_active ? "" : " (неактивен)"}
                  </option>
                ))}
              </select>
            </label>
            <div className="flex items-end gap-3">
              <button type="submit" className={secondaryButtonClass} disabled={isLoadingBalances}>
                Найти
              </button>
              <button
                type="button"
                className={secondaryButtonClass}
                disabled={isLoadingBalances}
                onClick={() => {
                  setQuery("");
                  setAppliedQuery("");
                  setWarehouseId("");
                  void refreshBalances({ q: "", warehouseId: "", offset: 0 });
                }}
              >
                Сбросить
              </button>
            </div>
          </form>

          {notice ? (
            <div className="mb-4 rounded-xl border border-[var(--border)] bg-[var(--accent-soft)] px-4 py-3 text-sm leading-6 text-[var(--accent)]">
              {notice}
            </div>
          ) : null}

          {optionsError ? (
            <div className="mb-4 rounded-xl border border-[var(--warning)] bg-[var(--warning-soft)] px-4 py-3 text-sm leading-6 text-[var(--warning)]">
              Не удалось получить справочники склада: {optionsError}
              <button
                type="button"
                onClick={() => void refreshOptions()}
                className="ml-3 font-semibold underline decoration-dotted underline-offset-4"
              >
                повторить
              </button>
            </div>
          ) : null}

          {isLoadingBalances ? (
            <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--surface-muted)] p-8 text-center text-sm text-[var(--muted)]">
              Загружаем остатки…
            </div>
          ) : balancesError ? (
            <div className="rounded-xl border border-red-200 bg-red-50 p-5 text-sm leading-6 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
              <p className="font-semibold">Не удалось получить остатки</p>
              <p className="mt-1">{balancesError}</p>
              <button
                type="button"
                onClick={() => void refreshBalances()}
                className="mt-4 rounded-xl bg-red-700 px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-95"
              >
                Повторить запрос
              </button>
            </div>
          ) : balances.length === 0 ? (
            <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--surface-muted)] p-8 text-center">
              <h3 className="text-lg font-semibold">Остатков пока нет</h3>
              <p className="mx-auto mt-2 max-w-xl text-sm leading-6 text-[var(--muted)]">
                Попробуйте другой поиск или склад. Если товар новый, создайте приход через
                форму операции.
              </p>
            </div>
          ) : (
            <>
              <div className="overflow-x-auto rounded-xl border border-[var(--border)]">
                <table className="min-w-[980px] w-full border-collapse text-left text-sm">
                  <thead className="bg-[var(--surface-muted)] text-xs uppercase tracking-[0.08em] text-[var(--muted)]">
                    <tr>
                      <th scope="col" className="px-4 py-3 font-semibold">
                        Товар
                      </th>
                      <th scope="col" className="px-4 py-3 font-semibold">
                        Склад
                      </th>
                      <th scope="col" className="px-4 py-3 font-semibold">
                        На руках
                      </th>
                      <th scope="col" className="px-4 py-3 font-semibold">
                        Резерв
                      </th>
                      <th scope="col" className="px-4 py-3 font-semibold">
                        Доступно
                      </th>
                      <th scope="col" className="px-4 py-3 font-semibold">
                        Обновлено
                      </th>
                      <th scope="col" className="px-4 py-3 font-semibold">
                        История
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {balances.map((balance) => {
                      const selected =
                        selectedBalance?.product_id === balance.product_id &&
                        selectedBalance.warehouse.id === balance.warehouse.id;

                      return (
                        <tr
                          key={`${balance.product_id}-${balance.warehouse.id}`}
                          className={`border-t border-[var(--border)] align-top transition hover:bg-[var(--surface-muted)] ${
                            selected ? "bg-[var(--accent-soft)]" : ""
                          }`}
                        >
                          <td className="px-4 py-4">
                            <p className="font-semibold">{balance.name}</p>
                            <p className="mt-1 font-mono text-xs text-[var(--muted)]">
                              {balance.sku}
                            </p>
                            <p className="mt-1 text-xs text-[var(--muted)]">
                              ID: {balance.product_id}. Ед.: {balance.unit}
                            </p>
                          </td>
                          <td className="px-4 py-4">{balance.warehouse.name}</td>
                          <td className="px-4 py-4 font-mono text-sm">{balance.on_hand}</td>
                          <td className="px-4 py-4 font-mono text-sm">{balance.reserved}</td>
                          <td className="px-4 py-4 font-mono text-sm font-semibold">
                            {balance.available}
                          </td>
                          <td className="px-4 py-4 text-sm text-[var(--muted)]">
                            {formatDate(balance.updated_at)}
                          </td>
                          <td className="px-4 py-4">
                            <div className="flex flex-wrap gap-2">
                              <button
                                type="button"
                                onMouseDown={(event) => {
                                  event.preventDefault();
                                  void handleSelectBalance(balance);
                                }}
                                onKeyDown={(event) => {
                                  if (event.key === "Enter" || event.key === " ") {
                                    event.preventDefault();
                                    void handleSelectBalance(balance);
                                  }
                                }}
                                className={secondaryButtonClass}
                              >
                                Открыть
                              </button>
                              {canManage ? (
                                <button
                                  type="button"
                                  onMouseDown={(event) => {
                                    event.preventDefault();
                                    openMovementForm("receipt", balance);
                                  }}
                                  onKeyDown={(event) => {
                                    if (event.key === "Enter" || event.key === " ") {
                                      event.preventDefault();
                                      openMovementForm("receipt", balance);
                                    }
                                  }}
                                  className={secondaryButtonClass}
                                  disabled={isLoadingOptions}
                                >
                                  Операция
                                </button>
                              ) : null}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <p className="text-sm text-[var(--muted)]">
                  Страница {currentPage} из {totalPages}
                </p>
                <div className="flex gap-2">
                  <button
                    type="button"
                    className={secondaryButtonClass}
                    disabled={!hasPreviousPage || isLoadingBalances}
                    onClick={() => void refreshBalances({ offset: Math.max(0, offset - PAGE_SIZE) })}
                  >
                    Назад
                  </button>
                  <button
                    type="button"
                    className={secondaryButtonClass}
                    disabled={!hasNextPage || isLoadingBalances}
                    onClick={() => void refreshBalances({ offset: offset + PAGE_SIZE })}
                  >
                    Вперёд
                  </button>
                </div>
              </div>
            </>
          )}
        </section>

        <section
          aria-labelledby="stock-history-heading"
          className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)] sm:p-6"
        >
          <div className="mb-5 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <p className="text-sm text-[var(--muted)]">История выбранного товара и склада</p>
              <h2 id="stock-history-heading" className="text-2xl font-semibold tracking-tight">
                Движения склада
              </h2>
              {selectedBalance ? (
                <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
                  {selectedBalance.name}, склад {selectedBalance.warehouse.name}
                </p>
              ) : (
                <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
                  Выберите строку остатков, чтобы открыть историю.
                </p>
              )}
            </div>
          </div>

          {selectedBalance ? (
            <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-[220px_220px_auto]">
              <label className="block">
                <span className="text-sm font-semibold">Тип</span>
                <select
                  value={movementTypeFilter}
                  onChange={(event) => {
                    const value = event.target.value as MovementType | "";
                    setMovementTypeFilter(value);
                    void refreshMovements({ type: value, offset: 0 });
                  }}
                  className={inputClass}
                >
                  <option value="">Все типы</option>
                  {Object.entries(movementTypeLabels).map(([type, label]) => (
                    <option key={type} value={type}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="text-sm font-semibold">Статус</span>
                <select
                  value={movementStatusFilter}
                  onChange={(event) => {
                    const value = event.target.value as MovementStatus | "";
                    setMovementStatusFilter(value);
                    void refreshMovements({ status: value, offset: 0 });
                  }}
                  className={inputClass}
                >
                  <option value="">Все статусы</option>
                  {Object.entries(movementStatusLabels).map(([status, label]) => (
                    <option key={status} value={status}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <div className="flex items-end">
                <button
                  type="button"
                  className={secondaryButtonClass}
                  onClick={() => void refreshMovements({ offset: 0 })}
                  disabled={isLoadingMovements}
                >
                  Обновить историю
                </button>
              </div>
            </div>
          ) : null}

          {!selectedBalance ? (
            <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--surface-muted)] p-8 text-center text-sm text-[var(--muted)]">
              История появится после выбора остатка.
            </div>
          ) : isLoadingMovements ? (
            <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--surface-muted)] p-8 text-center text-sm text-[var(--muted)]">
              Загружаем движения…
            </div>
          ) : movementsError ? (
            <div className="rounded-xl border border-red-200 bg-red-50 p-5 text-sm leading-6 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
              <p className="font-semibold">Не удалось получить историю</p>
              <p className="mt-1">{movementsError}</p>
              <button
                type="button"
                onClick={() => void refreshMovements()}
                className="mt-4 rounded-xl bg-red-700 px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-95"
              >
                Повторить запрос
              </button>
            </div>
          ) : movements.length === 0 ? (
            <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--surface-muted)] p-8 text-center text-sm text-[var(--muted)]">
              По выбранному товару движений пока нет.
            </div>
          ) : (
            <div className="grid gap-3">
              {movements.map((movement) => (
                <article
                  key={movement.id}
                  className="rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4"
                >
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span
                          className={`rounded-full px-2.5 py-1 text-xs font-semibold ${
                            movementTypeTone[movement.type]
                          }`}
                        >
                          {movementTypeLabels[movement.type]}
                        </span>
                        <span className="rounded-full border border-[var(--border)] bg-[var(--surface)] px-2.5 py-1 text-xs font-semibold">
                          {movementStatusLabels[movement.status]}
                        </span>
                      </div>
                      <p className="mt-3 font-semibold">{formatDate(movement.created_at)}</p>
                      <p className="mt-1 font-mono text-xs text-[var(--muted)]">
                        {movement.id}
                      </p>
                    </div>
                    {canManage && movement.status === "posted" ? (
                      <button
                        type="button"
                        onClick={() => openCancelForm(movement)}
                        className={dangerButtonClass}
                        disabled={isCancelling}
                      >
                        Отменить
                      </button>
                    ) : null}
                  </div>

                  <div className="mt-4 grid gap-3 text-sm leading-6 text-[var(--muted)] sm:grid-cols-2">
                    <p>
                      Склад:{" "}
                      <span className="font-semibold text-[var(--foreground)]">
                        {findWarehouseName(options, movement.warehouse_id)}
                      </span>
                    </p>
                    <p>
                      Склад назначения:{" "}
                      <span className="font-semibold text-[var(--foreground)]">
                        {findWarehouseName(options, movement.destination_warehouse_id)}
                      </span>
                    </p>
                    <p>
                      Причина:{" "}
                      <span className="font-semibold text-[var(--foreground)]">
                        {findReasonName(options, movement)}
                      </span>
                    </p>
                    <p>
                      Автор:{" "}
                      <span className="font-semibold text-[var(--foreground)]">
                        {formatUser(movement)}
                      </span>
                    </p>
                  </div>

                  {movement.comment ? (
                    <p className="mt-3 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm leading-6">
                      {movement.comment}
                    </p>
                  ) : null}

                  <div className="mt-4 overflow-x-auto rounded-lg border border-[var(--border)] bg-[var(--surface)]">
                    <table className="min-w-[620px] w-full border-collapse text-left text-xs">
                      <thead className="bg-[var(--surface-muted)] text-[var(--muted)]">
                        <tr>
                          <th scope="col" className="px-3 py-2 font-semibold">
                            Товар
                          </th>
                          <th scope="col" className="px-3 py-2 font-semibold">
                            Кол-во
                          </th>
                          <th scope="col" className="px-3 py-2 font-semibold">
                            Было
                          </th>
                          <th scope="col" className="px-3 py-2 font-semibold">
                            Стало
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {movement.lines.map((line) => (
                          <tr key={`${movement.id}-${line.product_id}`} className="border-t border-[var(--border)]">
                            <td className="px-3 py-2 font-mono">{line.product_id}</td>
                            <td className="px-3 py-2 font-mono">{line.quantity}</td>
                            <td className="px-3 py-2 font-mono">{line.before_quantity}</td>
                            <td className="px-3 py-2 font-mono">{line.after_quantity}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  {cancelForm?.movement.id === movement.id ? (
                    <form
                      onSubmit={handleCancelMovement}
                      className="mt-4 rounded-xl border border-red-200 bg-red-50 p-4 dark:border-red-900 dark:bg-red-950"
                    >
                      <label className="block">
                        <span className="text-sm font-semibold text-red-800 dark:text-red-200">
                          Причина отмены
                        </span>
                        <textarea
                          value={cancelForm.reason}
                          onChange={(event) => updateCancelReason(event.target.value)}
                          className={`${inputClass} min-h-24`}
                          placeholder="Например: ошибочное движение"
                          disabled={isCancelling}
                        />
                      </label>
                      <FieldError
                        message={getFieldError(cancelFieldErrors, "reason")}
                      />
                      {cancelError ? (
                        <div className="mt-3 rounded-lg bg-white px-3 py-2 text-sm leading-6 text-red-700 dark:bg-black/20 dark:text-red-200">
                          {cancelError}
                        </div>
                      ) : null}
                      <div className="mt-4 flex flex-wrap gap-2">
                        <button
                          type="submit"
                          className={dangerButtonClass}
                          disabled={isCancelling}
                        >
                          {isCancelling ? "Отменяем…" : "Подтвердить отмену"}
                        </button>
                        <button
                          type="button"
                          className={secondaryButtonClass}
                          disabled={isCancelling}
                          onClick={() => setCancelForm(null)}
                        >
                          Закрыть
                        </button>
                      </div>
                    </form>
                  ) : null}
                </article>
              ))}

              <div className="mt-1 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <p className="text-sm text-[var(--muted)]">
                  Страница {movementsCurrentPage} из {movementsTotalPages}
                </p>
                <div className="flex gap-2">
                  <button
                    type="button"
                    className={secondaryButtonClass}
                    disabled={!hasPreviousMovementsPage || isLoadingMovements}
                    onClick={() =>
                      void refreshMovements({
                        offset: Math.max(0, movementsOffset - HISTORY_PAGE_SIZE),
                      })
                    }
                  >
                    Назад
                  </button>
                  <button
                    type="button"
                    className={secondaryButtonClass}
                    disabled={!hasNextMovementsPage || isLoadingMovements}
                    onClick={() =>
                      void refreshMovements({ offset: movementsOffset + HISTORY_PAGE_SIZE })
                    }
                  >
                    Вперёд
                  </button>
                </div>
              </div>
            </div>
          )}
        </section>
      </div>

      {canManage ? (
        <aside className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)] sm:p-6 xl:sticky xl:top-6">
          <div className="mb-5">
            <p className="text-sm text-[var(--muted)]">Owner/Admin</p>
            <h2 className="text-2xl font-semibold tracking-tight">Операции склада</h2>
            <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
              Повторный клик во время запроса заблокирован. Если запрос упал без изменения
              формы, повтор использует тот же ключ идемпотентности.
            </p>
          </div>

          <div className="mb-4 flex flex-wrap gap-2">
            {(Object.keys(movementTypeLabels) as MovementType[]).map((type) => (
              <button
                key={type}
                type="button"
                onClick={() => openMovementForm(type)}
                className={
                  formValues.type === type && isFormOpen
                    ? primaryButtonClass
                    : secondaryButtonClass
                }
                disabled={type === "transfer" ? !canTransfer : activeWarehouses.length === 0}
              >
                {movementTypeLabels[type]}
              </button>
            ))}
          </div>

          {!canTransfer ? (
            <div className="mb-4 rounded-xl border border-[var(--warning)] bg-[var(--warning-soft)] px-4 py-3 text-sm leading-6 text-[var(--warning)]">
              Перемещение скрыто от отправки: нужен минимум второй активный склад.
            </div>
          ) : null}

          {isFormOpen ? (
            <form onSubmit={handleSaveMovement} className="grid gap-4">
              <label className="block">
                <span className="text-sm font-semibold">Тип операции</span>
                <select
                  value={formValues.type}
                  onChange={(event) =>
                    updateFormField("type", event.target.value as MovementType)
                  }
                  className={inputClass}
                  disabled={isSaving}
                >
                  {(Object.keys(movementTypeLabels) as MovementType[]).map((type) => (
                    <option key={type} value={type} disabled={type === "transfer" && !canTransfer}>
                      {movementTypeLabels[type]}
                    </option>
                  ))}
                </select>
              </label>

              <label className="block">
                <span className="text-sm font-semibold">ID товара</span>
                <input
                  value={formValues.product_id}
                  onChange={(event) => updateFormField("product_id", event.target.value)}
                  className={inputClass}
                  placeholder="Например: DEMO-001"
                  disabled={isSaving}
                />
                <FieldError
                  message={getFieldError(
                    fieldErrors,
                    "product_id",
                    "lines.0.product_id",
                  )}
                />
              </label>

              <label className="block">
                <span className="text-sm font-semibold">Склад</span>
                <select
                  value={formValues.warehouse_id}
                  onChange={(event) => updateFormField("warehouse_id", event.target.value)}
                  className={inputClass}
                  disabled={isSaving || isLoadingOptions}
                >
                  <option value="">Выберите склад</option>
                  {activeWarehouses.map((warehouse) => (
                    <option key={warehouse.id} value={warehouse.id}>
                      {warehouse.name}
                    </option>
                  ))}
                </select>
                <FieldError message={getFieldError(fieldErrors, "warehouse_id")} />
              </label>

              {formValues.type === "transfer" ? (
                <label className="block">
                  <span className="text-sm font-semibold">Склад назначения</span>
                  <select
                    value={formValues.destination_warehouse_id}
                    onChange={(event) =>
                      updateFormField("destination_warehouse_id", event.target.value)
                    }
                    className={inputClass}
                    disabled={isSaving || isLoadingOptions || !canTransfer}
                  >
                    <option value="">Выберите склад назначения</option>
                    {activeWarehouses
                      .filter((warehouse) => warehouse.id !== formValues.warehouse_id)
                      .map((warehouse) => (
                        <option key={warehouse.id} value={warehouse.id}>
                          {warehouse.name}
                        </option>
                      ))}
                  </select>
                  <FieldError
                    message={getFieldError(fieldErrors, "destination_warehouse_id")}
                  />
                </label>
              ) : null}

              {formValues.type === "adjustment" ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  <label className="block">
                    <span className="text-sm font-semibold">Фактическое количество</span>
                    <input
                      value={formValues.counted_quantity}
                      onChange={(event) =>
                        updateFormField("counted_quantity", event.target.value)
                      }
                      className={inputClass}
                      inputMode="decimal"
                      placeholder="10.000"
                      disabled={isSaving}
                    />
                    <FieldError
                      message={getFieldError(
                        fieldErrors,
                        "counted_quantity",
                        "lines.0.counted_quantity",
                      )}
                    />
                  </label>
                  <label className="block">
                    <span className="text-sm font-semibold">Ожидаемое количество</span>
                    <input
                      value={formValues.expected_quantity}
                      onChange={(event) =>
                        updateFormField("expected_quantity", event.target.value)
                      }
                      className={inputClass}
                      inputMode="decimal"
                      placeholder="8.000"
                      disabled={isSaving}
                    />
                    <FieldError
                      message={getFieldError(
                        fieldErrors,
                        "expected_quantity",
                        "lines.0.expected_quantity",
                      )}
                    />
                  </label>
                </div>
              ) : (
                <label className="block">
                  <span className="text-sm font-semibold">Количество</span>
                  <input
                    value={formValues.quantity}
                    onChange={(event) => updateFormField("quantity", event.target.value)}
                    className={inputClass}
                    inputMode="decimal"
                    placeholder="1.000"
                    disabled={isSaving}
                  />
                  <FieldError
                    message={getFieldError(fieldErrors, "quantity", "lines.0.quantity")}
                  />
                </label>
              )}

              {formValues.type === "write_off" || formValues.type === "adjustment" ? (
                <label className="block">
                  <span className="text-sm font-semibold">Причина</span>
                  <select
                    value={formValues.reason_code}
                    onChange={(event) => updateFormField("reason_code", event.target.value)}
                    className={inputClass}
                    disabled={isSaving || isLoadingOptions}
                  >
                    <option value="">Выберите причину</option>
                    {movementReasonOptions.map((reason) => (
                      <option key={reason.code} value={reason.code}>
                        {reason.name}
                      </option>
                    ))}
                  </select>
                  <FieldError message={getFieldError(fieldErrors, "reason_code")} />
                </label>
              ) : null}

              <label className="block">
                <span className="text-sm font-semibold">
                  Комментарий
                  {formValues.reason_code === "other" ? " (обязателен для «другое»)" : ""}
                </span>
                <textarea
                  value={formValues.comment}
                  onChange={(event) => updateFormField("comment", event.target.value)}
                  className={`${inputClass} min-h-24`}
                  placeholder="Короткое пояснение для аудита"
                  disabled={isSaving}
                />
                <FieldError message={getFieldError(fieldErrors, "comment")} />
              </label>

              {formError ? (
                <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm leading-6 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
                  {formError}
                </div>
              ) : null}

              <div className="flex flex-wrap gap-2">
                <button
                  type="submit"
                  className={primaryButtonClass}
                  disabled={isSaving || activeWarehouses.length === 0}
                >
                  {isSaving ? "Сохраняем…" : "Провести операцию"}
                </button>
                <button
                  type="button"
                  onClick={closeMovementForm}
                  className={secondaryButtonClass}
                  disabled={isSaving}
                >
                  Закрыть
                </button>
              </div>
            </form>
          ) : (
            <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--surface-muted)] p-5 text-sm leading-6 text-[var(--muted)]">
              Выберите тип операции. Если сначала открыть строку остатков, товар и склад
              подставятся автоматически.
            </div>
          )}
        </aside>
      ) : null}
    </div>
  );
}
