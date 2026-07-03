"use client";

import {
  type FormEvent,
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { listProducts, type Product } from "@/lib/products";
import {
  archiveSupplier,
  cancelPurchase,
  createIdempotencyKey,
  createPurchase,
  createSupplier,
  emptyPurchaseFormValues,
  emptySupplierFormValues,
  getPurchase,
  getPurchaseOptions,
  listPurchases,
  listSuppliers,
  purchaseToFormValues,
  submitPurchase,
  supplierToFormValues,
  updatePurchase,
  updateSupplier,
  type Purchase,
  type PurchaseFormValues,
  type PurchaseLineFormValues,
  type PurchaseOptions,
  type PurchaseStatus,
  type PurchaseSummary,
  type Supplier,
  type SupplierFormValues,
  type SupplierStatusFilter,
} from "@/lib/purchases";

const inputClass =
  "mt-2 w-full rounded-xl border border-[var(--border)] bg-[var(--surface)] px-4 py-3 text-base outline-none transition focus:border-[var(--accent)] focus:ring-4 focus:ring-[var(--accent-soft)] disabled:cursor-not-allowed disabled:opacity-70";

const secondaryButtonClass =
  "rounded-xl border border-[var(--border)] bg-[var(--surface)] px-4 py-2.5 text-sm font-semibold transition hover:border-[var(--accent)] hover:text-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-60";

const primaryButtonClass =
  "rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-60";

const dangerButtonClass =
  "rounded-xl bg-red-700 px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-60";

const SUPPLIER_PAGE_SIZE = 10;
const PURCHASE_PAGE_SIZE = 10;

const purchaseStatusLabels: Record<PurchaseStatus, string> = {
  draft: "Черновик",
  posted: "Проведено",
  cancelled: "Отменено",
};

const purchaseStatusTone: Record<PurchaseStatus, string> = {
  draft: "bg-[var(--warning-soft)] text-[var(--warning)]",
  posted: "bg-[var(--accent-soft)] text-[var(--accent)]",
  cancelled: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
};

type SupplierFormState = {
  mode: "create" | "edit";
  supplier: Supplier | null;
  values: SupplierFormValues;
  idempotencyKey: string;
  fieldErrors: Record<string, string>;
  error: string | null;
  isSaving: boolean;
};

type PurchaseFormState = {
  mode: "create" | "edit";
  purchase: Purchase | null;
  values: PurchaseFormValues;
  idempotencyKey: string;
  fieldErrors: Record<string, string>;
  error: string | null;
  isSaving: boolean;
  isDirty: boolean;
};

type CancelFormState = {
  purchase: Purchase;
  reason: string;
  idempotencyKey: string;
  fieldErrors: Record<string, string>;
  error: string | null;
  isSaving: boolean;
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

function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--surface-muted)] p-5 text-sm leading-6 text-[var(--muted)]">
      {children}
    </div>
  );
}

function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="rounded-xl border border-red-200 bg-red-50 p-5 text-sm leading-6 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
      <p>{message}</p>
      <button type="button" onClick={onRetry} className={`${secondaryButtonClass} mt-4`}>
        Повторить запрос
      </button>
    </div>
  );
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

function formatDate(value: string | null) {
  if (!value) {
    return "—";
  }

  const date = new Date(`${value}T00:00:00`);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium" }).format(date);
}

function formatDateTime(value: string | null) {
  if (!value) {
    return "—";
  }

  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(date);
}

function formatUser(user: Purchase["created_by"] | null) {
  if (!user) {
    return "—";
  }

  return user.full_name?.trim() || user.email;
}

function getDefaultWarehouseId(options: PurchaseOptions | null) {
  const activeWarehouses = options?.warehouses.filter((warehouse) => warehouse.is_active) ?? [];
  return (
    activeWarehouses.find((warehouse) => warehouse.is_default)?.id ??
    activeWarehouses[0]?.id ??
    ""
  );
}

function normalizeDecimalForPreview(value: string) {
  return value.trim().replace(",", ".");
}

function stripLeadingZeros(value: string) {
  const stripped = value.replace(/^0+(?=\d)/, "");
  return stripped || "0";
}

function parseScaledDecimal(value: string, scale: number) {
  const normalized = normalizeDecimalForPreview(value);
  const match = /^(\d+)(?:\.(\d+))?$/.exec(normalized);

  if (!match) {
    return null;
  }

  const integerPart = match[1];
  const fractionalPart = (match[2] ?? "").padEnd(scale, "0").slice(0, scale);
  return stripLeadingZeros(`${integerPart}${fractionalPart}`);
}

function multiplyIntegerStrings(left: string, right: string) {
  const leftDigits = stripLeadingZeros(left);
  const rightDigits = stripLeadingZeros(right);

  if (leftDigits === "0" || rightDigits === "0") {
    return "0";
  }

  const result = Array.from({ length: leftDigits.length + rightDigits.length }, () => 0);

  for (let leftIndex = leftDigits.length - 1; leftIndex >= 0; leftIndex -= 1) {
    for (let rightIndex = rightDigits.length - 1; rightIndex >= 0; rightIndex -= 1) {
      const position = leftIndex + rightIndex + 1;
      const product =
        Number(leftDigits[leftIndex]) * Number(rightDigits[rightIndex]) + result[position];
      result[position] = product % 10;
      result[position - 1] += Math.floor(product / 10);
    }
  }

  return stripLeadingZeros(result.join(""));
}

function addIntegerStrings(left: string, right: string) {
  let carry = 0;
  let leftIndex = left.length - 1;
  let rightIndex = right.length - 1;
  let result = "";

  while (leftIndex >= 0 || rightIndex >= 0 || carry > 0) {
    const sum =
      (leftIndex >= 0 ? Number(left[leftIndex]) : 0) +
      (rightIndex >= 0 ? Number(right[rightIndex]) : 0) +
      carry;
    result = String(sum % 10) + result;
    carry = Math.floor(sum / 10);
    leftIndex -= 1;
    rightIndex -= 1;
  }

  return stripLeadingZeros(result);
}

function divideIntegerStringBySmall(value: string, divisor: number) {
  let remainder = 0;
  let quotient = "";

  for (const digit of value) {
    const current = remainder * 10 + Number(digit);
    quotient += String(Math.floor(current / divisor));
    remainder = current % divisor;
  }

  return stripLeadingZeros(quotient);
}

function formatCents(value: string) {
  const normalized = stripLeadingZeros(value).padStart(3, "0");
  return `${stripLeadingZeros(normalized.slice(0, -2))}.${normalized.slice(-2)}`;
}

function previewLineTotal(line: PurchaseLineFormValues) {
  const quantityMilli = parseScaledDecimal(line.quantity, 3);
  const priceCents = parseScaledDecimal(line.unit_price, 2);

  if (quantityMilli === null || priceCents === null) {
    return "—";
  }

  const raw = multiplyIntegerStrings(quantityMilli, priceCents);
  const roundedCents = divideIntegerStringBySmall(addIntegerStrings(raw, "500"), 1000);
  return formatCents(roundedCents);
}

function supplierStatusText(supplier: Supplier) {
  return supplier.is_active ? "Активен" : "В архиве";
}

export function PurchaseManager({
  canManage,
  userRoles,
}: {
  canManage: boolean;
  userRoles: string[];
}) {
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [supplierCount, setSupplierCount] = useState(0);
  const [supplierOffset, setSupplierOffset] = useState(0);
  const [supplierQuery, setSupplierQuery] = useState("");
  const [appliedSupplierQuery, setAppliedSupplierQuery] = useState("");
  const [supplierStatus, setSupplierStatus] = useState<SupplierStatusFilter>("active");
  const [isLoadingSuppliers, setIsLoadingSuppliers] = useState(canManage);
  const [suppliersError, setSuppliersError] = useState<string | null>(null);

  const [activeSuppliers, setActiveSuppliers] = useState<Supplier[]>([]);
  const [allSuppliers, setAllSuppliers] = useState<Supplier[]>([]);
  const [options, setOptions] = useState<PurchaseOptions | null>(null);
  const [products, setProducts] = useState<Product[]>([]);
  const [lookupsError, setLookupsError] = useState<string | null>(null);

  const [purchases, setPurchases] = useState<PurchaseSummary[]>([]);
  const [purchaseCount, setPurchaseCount] = useState(0);
  const [purchaseOffset, setPurchaseOffset] = useState(0);
  const [purchaseQuery, setPurchaseQuery] = useState("");
  const [appliedPurchaseQuery, setAppliedPurchaseQuery] = useState("");
  const [purchaseSupplierId, setPurchaseSupplierId] = useState("");
  const [purchaseWarehouseId, setPurchaseWarehouseId] = useState("");
  const [purchaseStatus, setPurchaseStatus] = useState<PurchaseStatus | "">("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [isLoadingPurchases, setIsLoadingPurchases] = useState(canManage);
  const [purchasesError, setPurchasesError] = useState<string | null>(null);

  const [selectedPurchase, setSelectedPurchase] = useState<Purchase | null>(null);
  const [isLoadingDetail, setIsLoadingDetail] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [supplierForm, setSupplierForm] = useState<SupplierFormState | null>(null);
  const [purchaseForm, setPurchaseForm] = useState<PurchaseFormState | null>(null);
  const [cancelForm, setCancelForm] = useState<CancelFormState | null>(null);
  const [isSubmittingPurchase, setIsSubmittingPurchase] = useState(false);
  const [submitIdempotencyKey, setSubmitIdempotencyKey] = useState(createIdempotencyKey);
  const [notice, setNotice] = useState<string | null>(null);

  const supplierRequestId = useRef(0);
  const purchaseRequestId = useRef(0);
  const detailRequestId = useRef(0);

  const activeWarehouses = useMemo(
    () => options?.warehouses.filter((warehouse) => warehouse.is_active) ?? [],
    [options],
  );
  const supplierCurrentPage = Math.floor(supplierOffset / SUPPLIER_PAGE_SIZE) + 1;
  const supplierTotalPages = Math.max(1, Math.ceil(supplierCount / SUPPLIER_PAGE_SIZE));
  const hasPreviousSupplierPage = supplierOffset > 0;
  const hasNextSupplierPage = supplierOffset + suppliers.length < supplierCount;
  const purchaseCurrentPage = Math.floor(purchaseOffset / PURCHASE_PAGE_SIZE) + 1;
  const purchaseTotalPages = Math.max(1, Math.ceil(purchaseCount / PURCHASE_PAGE_SIZE));
  const hasPreviousPurchasePage = purchaseOffset > 0;
  const hasNextPurchasePage = purchaseOffset + purchases.length < purchaseCount;
  const roleLabel = userRoles.length > 0 ? userRoles.join(", ") : "без роли";

  async function refreshSuppliers(next?: {
    q?: string;
    status?: SupplierStatusFilter;
    offset?: number;
  }) {
    const nextQuery = next?.q ?? appliedSupplierQuery;
    const nextStatus = next?.status ?? supplierStatus;
    const nextOffset = next?.offset ?? supplierOffset;
    const requestId = ++supplierRequestId.current;

    setIsLoadingSuppliers(true);
    setSuppliersError(null);

    const result = await listSuppliers({
      q: nextQuery,
      status: nextStatus,
      limit: SUPPLIER_PAGE_SIZE,
      offset: nextOffset,
    });

    if (requestId !== supplierRequestId.current) {
      return;
    }

    if (result.status === "success") {
      setSuppliers(result.data.items);
      setSupplierCount(result.data.count);
      setSupplierOffset(result.data.offset);
    } else {
      setSuppliersError(result.error.message);
    }

    setIsLoadingSuppliers(false);
  }

  async function refreshLookups() {
    setLookupsError(null);

    const [optionsResult, activeSuppliersResult, allSuppliersResult, productsResult] =
      await Promise.all([
        getPurchaseOptions(),
        listSuppliers({ status: "active", limit: 100, offset: 0 }),
        listSuppliers({ status: "all", limit: 100, offset: 0 }),
        listProducts({ limit: 100, offset: 0 }),
      ]);

    const lookupErrors: string[] = [];

    if (optionsResult.status === "success") {
      setOptions(optionsResult.data);
    } else {
      lookupErrors.push(optionsResult.error.message);
    }

    if (activeSuppliersResult.status === "success") {
      setActiveSuppliers(activeSuppliersResult.data.items);
    } else {
      lookupErrors.push(activeSuppliersResult.error.message);
    }

    if (allSuppliersResult.status === "success") {
      setAllSuppliers(allSuppliersResult.data.items);
    } else {
      lookupErrors.push(allSuppliersResult.error.message);
    }

    if (productsResult.status === "success") {
      setProducts(productsResult.data.items);
    } else {
      lookupErrors.push(productsResult.error.message);
    }

    setLookupsError(lookupErrors[0] ?? null);
  }

  async function refreshPurchases(next?: {
    q?: string;
    supplierId?: string;
    warehouseId?: string;
    status?: PurchaseStatus | "";
    dateFrom?: string;
    dateTo?: string;
    offset?: number;
  }) {
    const nextQuery = next?.q ?? appliedPurchaseQuery;
    const nextSupplierId = next?.supplierId ?? purchaseSupplierId;
    const nextWarehouseId = next?.warehouseId ?? purchaseWarehouseId;
    const nextStatus = next?.status ?? purchaseStatus;
    const nextDateFrom = next?.dateFrom ?? dateFrom;
    const nextDateTo = next?.dateTo ?? dateTo;
    const nextOffset = next?.offset ?? purchaseOffset;
    const requestId = ++purchaseRequestId.current;

    setIsLoadingPurchases(true);
    setPurchasesError(null);

    const result = await listPurchases({
      q: nextQuery,
      supplierId: nextSupplierId,
      warehouseId: nextWarehouseId,
      status: nextStatus,
      dateFrom: nextDateFrom,
      dateTo: nextDateTo,
      limit: PURCHASE_PAGE_SIZE,
      offset: nextOffset,
    });

    if (requestId !== purchaseRequestId.current) {
      return;
    }

    if (result.status === "success") {
      setPurchases(result.data.items);
      setPurchaseCount(result.data.count);
      setPurchaseOffset(result.data.offset);
    } else {
      setPurchasesError(result.error.message);
    }

    setIsLoadingPurchases(false);
  }

  async function loadPurchaseDetail(purchaseId: string) {
    if (!confirmDiscardPurchaseForm()) {
      return;
    }

    const requestId = ++detailRequestId.current;
    setIsLoadingDetail(true);
    setDetailError(null);

    const result = await getPurchase(purchaseId);

    if (requestId !== detailRequestId.current) {
      return;
    }

    if (result.status === "success") {
      setSelectedPurchase(result.data);
      setCancelForm(null);
      setSubmitIdempotencyKey(createIdempotencyKey());
    } else {
      setDetailError(result.error.message);
    }

    setIsLoadingDetail(false);
  }

  useEffect(() => {
    if (!canManage) {
      return;
    }

    async function loadInitialData() {
      await Promise.all([
        refreshLookups(),
        refreshSuppliers({ offset: 0 }),
        refreshPurchases({ offset: 0 }),
      ]);
    }

    void loadInitialData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canManage]);

  function confirmDiscardPurchaseForm() {
    if (!purchaseForm?.isDirty || purchaseForm.isSaving) {
      return true;
    }

    return window.confirm("Закрыть черновик закупки и потерять несохранённые изменения?");
  }

  async function handleSupplierSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedQuery = supplierQuery.trim();
    setAppliedSupplierQuery(normalizedQuery);
    await refreshSuppliers({ q: normalizedQuery, offset: 0 });
  }

  async function handleSupplierStatus(nextStatus: SupplierStatusFilter) {
    setSupplierStatus(nextStatus);
    await refreshSuppliers({ status: nextStatus, offset: 0 });
  }

  function openSupplierCreateForm() {
    setSupplierForm({
      mode: "create",
      supplier: null,
      values: emptySupplierFormValues(),
      idempotencyKey: createIdempotencyKey(),
      fieldErrors: {},
      error: null,
      isSaving: false,
    });
  }

  function openSupplierEditForm(supplier: Supplier) {
    setSupplierForm({
      mode: "edit",
      supplier,
      values: supplierToFormValues(supplier),
      idempotencyKey: createIdempotencyKey(),
      fieldErrors: {},
      error: null,
      isSaving: false,
    });
  }

  function updateSupplierField(field: keyof SupplierFormValues, value: string) {
    setSupplierForm((current) => {
      if (!current) {
        return current;
      }

      const nextFields = { ...current.fieldErrors };
      delete nextFields[field];

      return {
        ...current,
        values: {
          ...current.values,
          [field]: value,
        },
        fieldErrors: nextFields,
        idempotencyKey: current.mode === "create" ? createIdempotencyKey() : current.idempotencyKey,
      };
    });
  }

  async function handleSaveSupplier(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!supplierForm || supplierForm.isSaving) {
      return;
    }

    setSupplierForm((current) =>
      current ? { ...current, isSaving: true, error: null, fieldErrors: {} } : current,
    );

    const result =
      supplierForm.mode === "create"
        ? await createSupplier(supplierForm.values, supplierForm.idempotencyKey)
        : supplierForm.supplier
          ? await updateSupplier(
              supplierForm.supplier.id,
              supplierForm.values,
              supplierForm.supplier.updated_at,
            )
          : null;

    if (!result) {
      setSupplierForm((current) =>
        current ? { ...current, isSaving: false, error: "Поставщик не выбран." } : current,
      );
      return;
    }

    if (result.status === "success") {
      setNotice(
        supplierForm.mode === "create"
          ? `Поставщик «${result.data.name}» создан.`
          : `Поставщик «${result.data.name}» обновлён.`,
      );
      setSupplierForm(null);
      await Promise.all([refreshSuppliers(), refreshLookups()]);
    } else {
      setSupplierForm((current) =>
        current
          ? {
              ...current,
              isSaving: false,
              error: result.error.message,
              fieldErrors: result.error.fields,
            }
          : current,
      );
    }
  }

  async function handleArchiveSupplier(supplier: Supplier) {
    if (!window.confirm(`Архивировать поставщика «${supplier.name}»?`)) {
      return;
    }

    const result = await archiveSupplier(supplier.id);

    if (result.status === "success") {
      setNotice(`Поставщик «${supplier.name}» перенесён в архив.`);
      await Promise.all([refreshSuppliers(), refreshLookups()]);
    } else {
      setNotice(result.error.message);
    }
  }

  async function handlePurchaseSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedQuery = purchaseQuery.trim();
    setAppliedPurchaseQuery(normalizedQuery);
    await refreshPurchases({ q: normalizedQuery, offset: 0 });
  }

  async function updatePurchaseFilter(next: {
    supplierId?: string;
    warehouseId?: string;
    status?: PurchaseStatus | "";
    dateFrom?: string;
    dateTo?: string;
  }) {
    if (next.supplierId !== undefined) {
      setPurchaseSupplierId(next.supplierId);
    }
    if (next.warehouseId !== undefined) {
      setPurchaseWarehouseId(next.warehouseId);
    }
    if (next.status !== undefined) {
      setPurchaseStatus(next.status);
    }
    if (next.dateFrom !== undefined) {
      setDateFrom(next.dateFrom);
    }
    if (next.dateTo !== undefined) {
      setDateTo(next.dateTo);
    }

    await refreshPurchases({
      supplierId: next.supplierId,
      warehouseId: next.warehouseId,
      status: next.status,
      dateFrom: next.dateFrom,
      dateTo: next.dateTo,
      offset: 0,
    });
  }

  function openPurchaseCreateForm() {
    if (!confirmDiscardPurchaseForm()) {
      return;
    }

    setPurchaseForm({
      mode: "create",
      purchase: null,
      values: emptyPurchaseFormValues({
        supplier_id: activeSuppliers[0]?.id ?? "",
        warehouse_id: getDefaultWarehouseId(options),
      }),
      idempotencyKey: createIdempotencyKey(),
      fieldErrors: {},
      error: null,
      isSaving: false,
      isDirty: false,
    });
  }

  function openPurchaseEditForm(purchase: Purchase) {
    if (purchase.status !== "draft") {
      setNotice("Редактировать можно только черновик закупки.");
      return;
    }

    if (!confirmDiscardPurchaseForm()) {
      return;
    }

    setPurchaseForm({
      mode: "edit",
      purchase,
      values: purchaseToFormValues(purchase),
      idempotencyKey: createIdempotencyKey(),
      fieldErrors: {},
      error: null,
      isSaving: false,
      isDirty: false,
    });
  }

  function closePurchaseForm() {
    if (!confirmDiscardPurchaseForm()) {
      return;
    }

    setPurchaseForm(null);
  }

  function updatePurchaseField(field: keyof Omit<PurchaseFormValues, "lines">, value: string) {
    setPurchaseForm((current) => {
      if (!current) {
        return current;
      }

      const nextFields = { ...current.fieldErrors };
      delete nextFields[field];

      return {
        ...current,
        values: {
          ...current.values,
          [field]: value,
        },
        fieldErrors: nextFields,
        idempotencyKey: current.mode === "create" ? createIdempotencyKey() : current.idempotencyKey,
        isDirty: true,
      };
    });
  }

  function updatePurchaseLine(
    index: number,
    field: keyof PurchaseLineFormValues,
    value: string,
  ) {
    setPurchaseForm((current) => {
      if (!current) {
        return current;
      }

      const nextLines = current.values.lines.map((line, lineIndex) =>
        lineIndex === index ? { ...line, [field]: value } : line,
      );
      const nextFields = { ...current.fieldErrors };
      delete nextFields[`lines.${index}.${field}`];
      delete nextFields[field];

      return {
        ...current,
        values: {
          ...current.values,
          lines: nextLines,
        },
        fieldErrors: nextFields,
        idempotencyKey: current.mode === "create" ? createIdempotencyKey() : current.idempotencyKey,
        isDirty: true,
      };
    });
  }

  function addPurchaseLine() {
    setPurchaseForm((current) =>
      current
        ? {
            ...current,
            values: {
              ...current.values,
              lines: [...current.values.lines, { product_id: "", quantity: "", unit_price: "" }],
            },
            idempotencyKey:
              current.mode === "create" ? createIdempotencyKey() : current.idempotencyKey,
            isDirty: true,
          }
        : current,
    );
  }

  function removePurchaseLine(index: number) {
    setPurchaseForm((current) => {
      if (!current || current.values.lines.length <= 1) {
        return current;
      }

      return {
        ...current,
        values: {
          ...current.values,
          lines: current.values.lines.filter((_, lineIndex) => lineIndex !== index),
        },
        idempotencyKey: current.mode === "create" ? createIdempotencyKey() : current.idempotencyKey,
        isDirty: true,
      };
    });
  }

  async function handleSavePurchase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!purchaseForm || purchaseForm.isSaving) {
      return;
    }

    if (
      purchaseForm.mode === "create" &&
      !window.confirm("Сохранить черновик закупки? Итоги рассчитает MyRetail API.")
    ) {
      return;
    }

    setPurchaseForm((current) =>
      current ? { ...current, isSaving: true, error: null, fieldErrors: {} } : current,
    );

    const result =
      purchaseForm.mode === "create"
        ? await createPurchase(purchaseForm.values, purchaseForm.idempotencyKey)
        : purchaseForm.purchase
          ? await updatePurchase(
              purchaseForm.purchase.id,
              purchaseForm.values,
              purchaseForm.purchase.updated_at,
            )
          : null;

    if (!result) {
      setPurchaseForm((current) =>
        current ? { ...current, isSaving: false, error: "Закупка не выбрана." } : current,
      );
      return;
    }

    if (result.status === "success") {
      setNotice(
        purchaseForm.mode === "create"
          ? `Черновик закупки ${result.data.id} создан.`
          : `Черновик закупки ${result.data.id} обновлён.`,
      );
      setSelectedPurchase(result.data);
      setSubmitIdempotencyKey(createIdempotencyKey());
      setPurchaseForm(null);
      await refreshPurchases();
    } else {
      setPurchaseForm((current) =>
        current
          ? {
              ...current,
              isSaving: false,
              error: result.error.message,
              fieldErrors: result.error.fields,
            }
          : current,
      );
    }
  }

  async function handleSubmitPurchase() {
    if (!selectedPurchase || selectedPurchase.status !== "draft" || isSubmittingPurchase) {
      return;
    }

    if (
      !window.confirm(
        `Провести закупку ${selectedPurchase.id}? Остатки и последняя закупочная цена будут обновлены в ERPNext.`,
      )
    ) {
      return;
    }

    setIsSubmittingPurchase(true);
    setDetailError(null);

    const result = await submitPurchase(
      selectedPurchase.id,
      selectedPurchase.updated_at,
      submitIdempotencyKey,
    );

    if (result.status === "success") {
      setSelectedPurchase(result.data);
      setNotice(`Закупка ${result.data.id} проведена.`);
      setSubmitIdempotencyKey(createIdempotencyKey());
      await Promise.all([refreshPurchases(), refreshLookups()]);
    } else {
      setDetailError(result.error.message);
    }

    setIsSubmittingPurchase(false);
  }

  function openCancelForm(purchase: Purchase) {
    if (purchase.status !== "posted") {
      setNotice("Отменить можно только проведённую закупку.");
      return;
    }

    setCancelForm({
      purchase,
      reason: "",
      idempotencyKey: createIdempotencyKey(),
      fieldErrors: {},
      error: null,
      isSaving: false,
    });
  }

  function updateCancelReason(reason: string) {
    setCancelForm((current) =>
      current
        ? {
            ...current,
            reason,
            idempotencyKey: createIdempotencyKey(),
            fieldErrors: {
              ...current.fieldErrors,
              reason: "",
            },
          }
        : current,
    );
  }

  async function handleCancelPurchase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!cancelForm || cancelForm.isSaving) {
      return;
    }

    if (
      !window.confirm(
        `Отменить закупку ${cancelForm.purchase.id}? ERPNext сделает обратное движение остатков и восстановит закупочные цены.`,
      )
    ) {
      return;
    }

    setCancelForm((current) =>
      current ? { ...current, isSaving: true, error: null, fieldErrors: {} } : current,
    );

    const result = await cancelPurchase(
      cancelForm.purchase.id,
      cancelForm.reason,
      cancelForm.idempotencyKey,
    );

    if (result.status === "success") {
      setSelectedPurchase(result.data);
      setNotice(`Закупка ${result.data.id} отменена.`);
      setCancelForm(null);
      await Promise.all([refreshPurchases(), refreshLookups()]);
    } else {
      setCancelForm((current) =>
        current
          ? {
              ...current,
              isSaving: false,
              error: result.error.message,
              fieldErrors: result.error.fields,
            }
          : current,
      );
    }
  }

  if (!canManage) {
    return (
      <section
        aria-labelledby="purchases-forbidden-heading"
        className="rounded-2xl border border-red-200 bg-red-50 p-6 text-red-800 shadow-[0_12px_36px_rgba(20,32,24,0.04)] dark:border-red-900 dark:bg-red-950 dark:text-red-200"
      >
        <p className="text-sm font-semibold uppercase tracking-[0.16em]">403</p>
        <h2 id="purchases-forbidden-heading" className="mt-2 text-2xl font-semibold">
          Доступ к закупкам запрещён
        </h2>
        <p className="mt-3 max-w-2xl text-sm leading-6">
          Модуль поставщиков и закупок доступен только ролям Owner и Admin. Текущие роли:{" "}
          {roleLabel}. Браузер не делает прямых запросов к ERPNext; доступ проверяет MyRetail API.
        </p>
      </section>
    );
  }

  return (
    <div className="grid min-w-0 gap-6">
      {notice ? (
        <div
          className="rounded-2xl border border-[var(--border)] bg-[var(--accent-soft)] p-4 text-sm font-semibold text-[var(--accent)]"
          role="status"
        >
          {notice}
        </div>
      ) : null}

      {lookupsError ? (
        <ErrorState message={lookupsError} onRetry={() => void refreshLookups()} />
      ) : null}

      <div className="grid min-w-0 gap-6 xl:grid-cols-[minmax(0,0.95fr)_minmax(0,1.2fr)] xl:items-start">
        <section
          aria-labelledby="suppliers-heading"
          className="min-w-0 rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)] sm:p-6"
        >
          <div className="mb-5 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <p className="text-sm text-[var(--muted)]">Справочник ERPNext через MyRetail API</p>
              <h2 id="suppliers-heading" className="text-2xl font-semibold tracking-tight">
                Поставщики
              </h2>
            </div>
            <button type="button" onClick={openSupplierCreateForm} className={primaryButtonClass}>
              Новый поставщик
            </button>
          </div>

          <form onSubmit={handleSupplierSearch} className="grid gap-3 md:grid-cols-[1fr_180px_auto]">
            <label className="block">
              <span className="text-sm font-semibold">Поиск поставщика</span>
              <input
                value={supplierQuery}
                onChange={(event) => setSupplierQuery(event.target.value)}
                className={inputClass}
                type="search"
                placeholder="Название, БИН, контакт, телефон"
              />
            </label>
            <label className="block">
              <span className="text-sm font-semibold">Статус</span>
              <select
                value={supplierStatus}
                onChange={(event) =>
                  void handleSupplierStatus(event.target.value as SupplierStatusFilter)
                }
                className={inputClass}
              >
                <option value="active">Активные</option>
                <option value="archived">Архив</option>
                <option value="all">Все</option>
              </select>
            </label>
            <div className="flex items-end">
              <button type="submit" className={secondaryButtonClass}>
                Найти
              </button>
            </div>
          </form>

          <div className="mt-5 grid gap-3">
            {isLoadingSuppliers ? (
              <EmptyState>Загружаем поставщиков…</EmptyState>
            ) : suppliersError ? (
              <ErrorState message={suppliersError} onRetry={() => void refreshSuppliers()} />
            ) : suppliers.length === 0 ? (
              <EmptyState>Поставщиков по текущему фильтру пока нет.</EmptyState>
            ) : (
              suppliers.map((supplier) => (
                <article
                  key={supplier.id}
                  className="rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4"
                >
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div>
                      <p className="font-mono text-xs text-[var(--muted)]">{supplier.id}</p>
                      <h3 className="mt-1 text-lg font-semibold">{supplier.name}</h3>
                      <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
                        БИН/ИИН: {supplier.tax_id ?? "—"}. Контакт:{" "}
                        {supplier.contact_name ?? "—"}. Телефон: {supplier.phone ?? "—"}.
                      </p>
                    </div>
                    <span
                      className={`w-fit rounded-full px-2.5 py-1 text-xs font-semibold ${
                        supplier.is_active
                          ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                          : "bg-[var(--surface)] text-[var(--muted)]"
                      }`}
                    >
                      {supplierStatusText(supplier)}
                    </span>
                  </div>
                  <div className="mt-4 flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => openSupplierEditForm(supplier)}
                      className={secondaryButtonClass}
                    >
                      Редактировать
                    </button>
                    {supplier.is_active ? (
                      <button
                        type="button"
                        onClick={() => void handleArchiveSupplier(supplier)}
                        className={dangerButtonClass}
                      >
                        Архивировать
                      </button>
                    ) : null}
                  </div>
                </article>
              ))
            )}
          </div>

          <div className="mt-5 flex flex-wrap items-center justify-between gap-3 text-sm text-[var(--muted)]">
            <span>
              Страница {supplierCurrentPage} из {supplierTotalPages}. Всего: {supplierCount}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                className={secondaryButtonClass}
                disabled={!hasPreviousSupplierPage || isLoadingSuppliers}
                onClick={() =>
                  void refreshSuppliers({
                    offset: Math.max(0, supplierOffset - SUPPLIER_PAGE_SIZE),
                  })
                }
              >
                Назад
              </button>
              <button
                type="button"
                className={secondaryButtonClass}
                disabled={!hasNextSupplierPage || isLoadingSuppliers}
                onClick={() => void refreshSuppliers({ offset: supplierOffset + SUPPLIER_PAGE_SIZE })}
              >
                Вперёд
              </button>
            </div>
          </div>

          {supplierForm ? (
            <form
              onSubmit={handleSaveSupplier}
              className="mt-6 rounded-2xl border border-[var(--border)] bg-[var(--surface-muted)] p-5"
            >
              <div className="mb-4">
                <p className="text-sm text-[var(--muted)]">
                  {supplierForm.mode === "create"
                    ? "POST /suppliers с Idempotency-Key"
                    : "PATCH /suppliers/{id} с expected_updated_at"}
                </p>
                <h3 className="text-xl font-semibold">
                  {supplierForm.mode === "create" ? "Новый поставщик" : "Редактирование поставщика"}
                </h3>
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <label className="block md:col-span-2">
                  <span className="text-sm font-semibold">Название *</span>
                  <input
                    value={supplierForm.values.name}
                    onChange={(event) => updateSupplierField("name", event.target.value)}
                    className={inputClass}
                    disabled={supplierForm.isSaving}
                  />
                  <FieldError message={getFieldError(supplierForm.fieldErrors, "name")} />
                </label>
                <label className="block">
                  <span className="text-sm font-semibold">БИН/ИИН</span>
                  <input
                    value={supplierForm.values.tax_id}
                    onChange={(event) => updateSupplierField("tax_id", event.target.value)}
                    className={inputClass}
                    disabled={supplierForm.isSaving}
                  />
                  <FieldError message={getFieldError(supplierForm.fieldErrors, "tax_id")} />
                </label>
                <label className="block">
                  <span className="text-sm font-semibold">Контакт</span>
                  <input
                    value={supplierForm.values.contact_name}
                    onChange={(event) => updateSupplierField("contact_name", event.target.value)}
                    className={inputClass}
                    disabled={supplierForm.isSaving}
                  />
                  <FieldError message={getFieldError(supplierForm.fieldErrors, "contact_name")} />
                </label>
                <label className="block">
                  <span className="text-sm font-semibold">Телефон</span>
                  <input
                    value={supplierForm.values.phone}
                    onChange={(event) => updateSupplierField("phone", event.target.value)}
                    className={inputClass}
                    disabled={supplierForm.isSaving}
                  />
                  <FieldError message={getFieldError(supplierForm.fieldErrors, "phone")} />
                </label>
                <label className="block">
                  <span className="text-sm font-semibold">Email</span>
                  <input
                    value={supplierForm.values.email}
                    onChange={(event) => updateSupplierField("email", event.target.value)}
                    className={inputClass}
                    disabled={supplierForm.isSaving}
                  />
                  <FieldError message={getFieldError(supplierForm.fieldErrors, "email")} />
                </label>
                <label className="block md:col-span-2">
                  <span className="text-sm font-semibold">Адрес</span>
                  <textarea
                    value={supplierForm.values.address}
                    onChange={(event) => updateSupplierField("address", event.target.value)}
                    className={`${inputClass} min-h-24`}
                    disabled={supplierForm.isSaving}
                  />
                  <FieldError message={getFieldError(supplierForm.fieldErrors, "address")} />
                </label>
              </div>

              {supplierForm.error ? (
                <div className="mt-4 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
                  {supplierForm.error}
                </div>
              ) : null}

              <div className="mt-5 flex flex-wrap gap-2">
                <button type="submit" className={primaryButtonClass} disabled={supplierForm.isSaving}>
                  {supplierForm.isSaving ? "Сохраняем…" : "Сохранить поставщика"}
                </button>
                <button
                  type="button"
                  onClick={() => setSupplierForm(null)}
                  className={secondaryButtonClass}
                  disabled={supplierForm.isSaving}
                >
                  Закрыть
                </button>
              </div>
            </form>
          ) : null}
        </section>

        <section
          aria-labelledby="purchases-heading"
          className="min-w-0 rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)] sm:p-6"
        >
          <div className="mb-5 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <p className="text-sm text-[var(--muted)]">Черновики, проведение и история закупок</p>
              <h2 id="purchases-heading" className="text-2xl font-semibold tracking-tight">
                Закупки
              </h2>
            </div>
            <button
              type="button"
              onClick={openPurchaseCreateForm}
              className={primaryButtonClass}
              disabled={activeSuppliers.length === 0 || activeWarehouses.length === 0}
            >
              Новый черновик
            </button>
          </div>

          <form onSubmit={handlePurchaseSearch} className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            <label className="block">
              <span className="text-sm font-semibold">Поиск</span>
              <input
                value={purchaseQuery}
                onChange={(event) => setPurchaseQuery(event.target.value)}
                className={inputClass}
                type="search"
                placeholder="Поставщик или номер накладной"
              />
            </label>
            <label className="block">
              <span className="text-sm font-semibold">Поставщик</span>
              <select
                value={purchaseSupplierId}
                onChange={(event) => void updatePurchaseFilter({ supplierId: event.target.value })}
                className={inputClass}
              >
                <option value="">Все поставщики</option>
                {allSuppliers.map((supplier) => (
                  <option key={supplier.id} value={supplier.id}>
                    {supplier.name}
                    {supplier.is_active ? "" : " (архив)"}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-sm font-semibold">Склад</span>
              <select
                value={purchaseWarehouseId}
                onChange={(event) => void updatePurchaseFilter({ warehouseId: event.target.value })}
                className={inputClass}
              >
                <option value="">Все склады</option>
                {options?.warehouses.map((warehouse) => (
                  <option key={warehouse.id} value={warehouse.id}>
                    {warehouse.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-sm font-semibold">Статус</span>
              <select
                value={purchaseStatus}
                onChange={(event) =>
                  void updatePurchaseFilter({ status: event.target.value as PurchaseStatus | "" })
                }
                className={inputClass}
              >
                <option value="">Все статусы</option>
                <option value="draft">Черновик</option>
                <option value="posted">Проведено</option>
                <option value="cancelled">Отменено</option>
              </select>
            </label>
            <label className="block">
              <span className="text-sm font-semibold">Дата от</span>
              <input
                value={dateFrom}
                onChange={(event) => void updatePurchaseFilter({ dateFrom: event.target.value })}
                className={inputClass}
                type="date"
              />
            </label>
            <label className="block">
              <span className="text-sm font-semibold">Дата до</span>
              <input
                value={dateTo}
                onChange={(event) => void updatePurchaseFilter({ dateTo: event.target.value })}
                className={inputClass}
                type="date"
              />
            </label>
            <div className="flex items-end">
              <button type="submit" className={secondaryButtonClass}>
                Найти
              </button>
            </div>
          </form>

          <div className="mt-5 grid gap-3">
            {isLoadingPurchases ? (
              <EmptyState>Загружаем закупки…</EmptyState>
            ) : purchasesError ? (
              <ErrorState message={purchasesError} onRetry={() => void refreshPurchases()} />
            ) : purchases.length === 0 ? (
              <EmptyState>Закупок по текущим фильтрам пока нет.</EmptyState>
            ) : (
              purchases.map((purchase) => (
                <article
                  key={purchase.id}
                  className="rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4"
                >
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div>
                      <p className="font-mono text-xs text-[var(--muted)]">{purchase.id}</p>
                      <h3 className="mt-1 text-lg font-semibold">{purchase.supplier.name}</h3>
                      <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
                        {formatDate(purchase.posting_date)} · {purchase.warehouse.name} · Итого{" "}
                        {purchase.total} {purchase.currency}
                      </p>
                    </div>
                    <span
                      className={`w-fit rounded-full px-2.5 py-1 text-xs font-semibold ${purchaseStatusTone[purchase.status]}`}
                    >
                      {purchaseStatusLabels[purchase.status]}
                    </span>
                  </div>
                  <div className="mt-4 flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => void loadPurchaseDetail(purchase.id)}
                      className={secondaryButtonClass}
                    >
                      Открыть
                    </button>
                  </div>
                </article>
              ))
            )}
          </div>

          <div className="mt-5 flex flex-wrap items-center justify-between gap-3 text-sm text-[var(--muted)]">
            <span>
              Страница {purchaseCurrentPage} из {purchaseTotalPages}. Всего: {purchaseCount}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                className={secondaryButtonClass}
                disabled={!hasPreviousPurchasePage || isLoadingPurchases}
                onClick={() =>
                  void refreshPurchases({
                    offset: Math.max(0, purchaseOffset - PURCHASE_PAGE_SIZE),
                  })
                }
              >
                Назад
              </button>
              <button
                type="button"
                className={secondaryButtonClass}
                disabled={!hasNextPurchasePage || isLoadingPurchases}
                onClick={() => void refreshPurchases({ offset: purchaseOffset + PURCHASE_PAGE_SIZE })}
              >
                Вперёд
              </button>
            </div>
          </div>
        </section>
      </div>

      <section
        aria-labelledby="purchase-detail-heading"
        className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)] sm:p-6"
      >
        <div className="mb-5 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <p className="text-sm text-[var(--muted)]">Детали, аудит и действия</p>
            <h2 id="purchase-detail-heading" className="text-2xl font-semibold tracking-tight">
              Документ закупки
            </h2>
          </div>
          <div className="flex flex-wrap gap-2">
            {selectedPurchase?.status === "draft" ? (
              <>
                <button
                  type="button"
                  onClick={() => openPurchaseEditForm(selectedPurchase)}
                  className={secondaryButtonClass}
                >
                  Редактировать черновик
                </button>
                <button
                  type="button"
                  onClick={() => void handleSubmitPurchase()}
                  className={primaryButtonClass}
                  disabled={isSubmittingPurchase}
                >
                  {isSubmittingPurchase ? "Проводим…" : "Провести"}
                </button>
              </>
            ) : null}
            {selectedPurchase?.status === "posted" ? (
              <button
                type="button"
                onClick={() => openCancelForm(selectedPurchase)}
                className={dangerButtonClass}
              >
                Отменить
              </button>
            ) : null}
          </div>
        </div>

        {isLoadingDetail ? (
          <EmptyState>Загружаем документ закупки…</EmptyState>
        ) : detailError ? (
          <ErrorState
            message={detailError}
            onRetry={() => selectedPurchase && void loadPurchaseDetail(selectedPurchase.id)}
          />
        ) : selectedPurchase ? (
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1.2fr)_minmax(280px,0.8fr)]">
            <div className="min-w-0 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <p className="font-mono text-xs text-[var(--muted)]">{selectedPurchase.id}</p>
                  <h3 className="mt-1 text-xl font-semibold">{selectedPurchase.supplier.name}</h3>
                  <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
                    {formatDate(selectedPurchase.posting_date)} · {selectedPurchase.warehouse.name}
                  </p>
                </div>
                <span
                  className={`w-fit rounded-full px-2.5 py-1 text-xs font-semibold ${purchaseStatusTone[selectedPurchase.status]}`}
                >
                  {purchaseStatusLabels[selectedPurchase.status]}
                </span>
              </div>

              <div className="mt-5 overflow-x-auto">
                <table className="w-full min-w-[720px] text-left text-sm">
                  <thead className="text-xs uppercase tracking-[0.12em] text-[var(--muted)]">
                    <tr>
                      <th className="py-2 pr-3">Товар</th>
                      <th className="py-2 pr-3">Количество</th>
                      <th className="py-2 pr-3">Цена</th>
                      <th className="py-2 pr-3 text-right">Сумма</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selectedPurchase.lines.map((line, index) => (
                      <tr key={`${line.product_id}-${index}`} className="border-t border-[var(--border)]">
                        <td className="py-3 pr-3">
                          <p className="font-semibold">{line.name}</p>
                          <p className="font-mono text-xs text-[var(--muted)]">{line.sku}</p>
                        </td>
                        <td className="py-3 pr-3">
                          {line.quantity} {line.unit}
                        </td>
                        <td className="py-3 pr-3">
                          {line.unit_price} {selectedPurchase.currency}
                        </td>
                        <td className="py-3 pr-3 text-right font-semibold">
                          {line.line_total} {selectedPurchase.currency}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="mt-5 flex justify-end">
                <div className="w-full max-w-xs rounded-xl border border-[var(--border)] bg-[var(--surface)] p-4 text-sm">
                  <div className="flex justify-between">
                    <span>Подытог</span>
                    <span>
                      {selectedPurchase.subtotal} {selectedPurchase.currency}
                    </span>
                  </div>
                  <div className="mt-2 flex justify-between text-lg font-semibold">
                    <span>Итого</span>
                    <span>
                      {selectedPurchase.total} {selectedPurchase.currency}
                    </span>
                  </div>
                </div>
              </div>
            </div>

            <aside className="rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4 text-sm leading-6">
              <h3 className="text-lg font-semibold">Аудит</h3>
              <dl className="mt-3 grid gap-2">
                <div>
                  <dt className="font-semibold">Создал</dt>
                  <dd className="text-[var(--muted)]">
                    {formatUser(selectedPurchase.created_by)} ·{" "}
                    {formatDateTime(selectedPurchase.created_at)}
                  </dd>
                </div>
                <div>
                  <dt className="font-semibold">Провёл</dt>
                  <dd className="text-[var(--muted)]">
                    {formatUser(selectedPurchase.submitted_by)} ·{" "}
                    {formatDateTime(selectedPurchase.submitted_at)}
                  </dd>
                </div>
                <div>
                  <dt className="font-semibold">Отменил</dt>
                  <dd className="text-[var(--muted)]">
                    {formatUser(selectedPurchase.cancelled_by)} ·{" "}
                    {formatDateTime(selectedPurchase.cancelled_at)}
                  </dd>
                </div>
                <div>
                  <dt className="font-semibold">Версия</dt>
                  <dd className="font-mono text-xs text-[var(--muted)]">{selectedPurchase.updated_at}</dd>
                </div>
                <div>
                  <dt className="font-semibold">Накладная поставщика</dt>
                  <dd className="text-[var(--muted)]">
                    {selectedPurchase.supplier_invoice_number ?? "—"} ·{" "}
                    {formatDate(selectedPurchase.supplier_invoice_date)}
                  </dd>
                </div>
                <div>
                  <dt className="font-semibold">Комментарий</dt>
                  <dd className="text-[var(--muted)]">{selectedPurchase.comment ?? "—"}</dd>
                </div>
              </dl>
            </aside>
          </div>
        ) : (
          <EmptyState>Откройте закупку из истории или создайте новый черновик.</EmptyState>
        )}

        {purchaseForm ? (
          <form
            onSubmit={handleSavePurchase}
            className="mt-6 rounded-2xl border border-[var(--border)] bg-[var(--surface-muted)] p-5"
          >
            <div className="mb-5 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <p className="text-sm text-[var(--muted)]">
                  {purchaseForm.mode === "create"
                    ? "POST /purchases с Idempotency-Key"
                    : "PATCH /purchases/{id} с expected_updated_at"}
                </p>
                <h3 className="text-xl font-semibold">
                  {purchaseForm.mode === "create" ? "Новый черновик закупки" : "Редактирование черновика"}
                </h3>
              </div>
              {purchaseForm.isDirty ? (
                <span className="w-fit rounded-full bg-[var(--warning-soft)] px-2.5 py-1 text-xs font-semibold text-[var(--warning)]">
                  Есть несохранённые изменения
                </span>
              ) : null}
            </div>

            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              <label className="block">
                <span className="text-sm font-semibold">Поставщик *</span>
                <select
                  value={purchaseForm.values.supplier_id}
                  onChange={(event) => updatePurchaseField("supplier_id", event.target.value)}
                  className={inputClass}
                  disabled={purchaseForm.isSaving}
                >
                  <option value="">Выберите активного поставщика</option>
                  {activeSuppliers.map((supplier) => (
                    <option key={supplier.id} value={supplier.id}>
                      {supplier.name}
                    </option>
                  ))}
                </select>
                <FieldError message={getFieldError(purchaseForm.fieldErrors, "supplier_id")} />
              </label>
              <label className="block">
                <span className="text-sm font-semibold">Склад *</span>
                <select
                  value={purchaseForm.values.warehouse_id}
                  onChange={(event) => updatePurchaseField("warehouse_id", event.target.value)}
                  className={inputClass}
                  disabled={purchaseForm.isSaving}
                >
                  <option value="">Выберите склад</option>
                  {activeWarehouses.map((warehouse) => (
                    <option key={warehouse.id} value={warehouse.id}>
                      {warehouse.name}
                    </option>
                  ))}
                </select>
                <FieldError message={getFieldError(purchaseForm.fieldErrors, "warehouse_id")} />
              </label>
              <label className="block">
                <span className="text-sm font-semibold">Дата поступления *</span>
                <input
                  value={purchaseForm.values.posting_date}
                  onChange={(event) => updatePurchaseField("posting_date", event.target.value)}
                  className={inputClass}
                  type="date"
                  disabled={purchaseForm.isSaving}
                />
                <FieldError message={getFieldError(purchaseForm.fieldErrors, "posting_date")} />
              </label>
              <label className="block">
                <span className="text-sm font-semibold">Номер накладной</span>
                <input
                  value={purchaseForm.values.supplier_invoice_number}
                  onChange={(event) =>
                    updatePurchaseField("supplier_invoice_number", event.target.value)
                  }
                  className={inputClass}
                  disabled={purchaseForm.isSaving}
                />
                <FieldError
                  message={getFieldError(purchaseForm.fieldErrors, "supplier_invoice_number")}
                />
              </label>
              <label className="block">
                <span className="text-sm font-semibold">Дата накладной</span>
                <input
                  value={purchaseForm.values.supplier_invoice_date}
                  onChange={(event) =>
                    updatePurchaseField("supplier_invoice_date", event.target.value)
                  }
                  className={inputClass}
                  type="date"
                  disabled={purchaseForm.isSaving}
                />
                <FieldError
                  message={getFieldError(purchaseForm.fieldErrors, "supplier_invoice_date")}
                />
              </label>
              <label className="block md:col-span-2 xl:col-span-3">
                <span className="text-sm font-semibold">Комментарий</span>
                <textarea
                  value={purchaseForm.values.comment}
                  onChange={(event) => updatePurchaseField("comment", event.target.value)}
                  className={`${inputClass} min-h-24`}
                  disabled={purchaseForm.isSaving}
                />
                <FieldError message={getFieldError(purchaseForm.fieldErrors, "comment")} />
              </label>
            </div>

            <div className="mt-6">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h4 className="text-lg font-semibold">Строки закупки</h4>
                  <p className="text-sm text-[var(--muted)]">
                    Количество и деньги отправляются строками. Итоги после сохранения берутся с сервера.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={addPurchaseLine}
                  className={secondaryButtonClass}
                  disabled={purchaseForm.isSaving || purchaseForm.values.lines.length >= 100}
                >
                  Добавить строку
                </button>
              </div>

              <div className="grid gap-3">
                {purchaseForm.values.lines.map((line, index) => (
                  <div
                    key={`${index}-${line.product_id}`}
                    className="grid gap-3 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-4 lg:grid-cols-[1.6fr_0.8fr_0.8fr_0.7fr_auto]"
                  >
                    <label className="block">
                      <span className="text-sm font-semibold">Товар {index + 1}</span>
                      <select
                        value={line.product_id}
                        onChange={(event) =>
                          updatePurchaseLine(index, "product_id", event.target.value)
                        }
                        className={inputClass}
                        disabled={purchaseForm.isSaving}
                      >
                        <option value="">Выберите товар</option>
                        {products.map((product) => (
                          <option key={product.id} value={product.id}>
                            {product.sku} — {product.name}
                          </option>
                        ))}
                      </select>
                      <FieldError
                        message={getFieldError(
                          purchaseForm.fieldErrors,
                          `lines.${index}.product_id`,
                          "product_id",
                        )}
                      />
                    </label>
                    <label className="block">
                      <span className="text-sm font-semibold">Количество</span>
                      <input
                        value={line.quantity}
                        onChange={(event) =>
                          updatePurchaseLine(index, "quantity", event.target.value)
                        }
                        className={inputClass}
                        inputMode="decimal"
                        placeholder="1.000"
                        disabled={purchaseForm.isSaving}
                      />
                      <FieldError
                        message={getFieldError(
                          purchaseForm.fieldErrors,
                          `lines.${index}.quantity`,
                          "quantity",
                        )}
                      />
                    </label>
                    <label className="block">
                      <span className="text-sm font-semibold">Цена закупки</span>
                      <input
                        value={line.unit_price}
                        onChange={(event) =>
                          updatePurchaseLine(index, "unit_price", event.target.value)
                        }
                        className={inputClass}
                        inputMode="decimal"
                        placeholder="1200.00"
                        disabled={purchaseForm.isSaving}
                      />
                      <FieldError
                        message={getFieldError(
                          purchaseForm.fieldErrors,
                          `lines.${index}.unit_price`,
                          "unit_price",
                        )}
                      />
                    </label>
                    <div className="rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] px-4 py-3 text-sm">
                      <span className="font-semibold">Предпросмотр</span>
                      <p className="mt-2">
                        {previewLineTotal(line)} {options?.currency ?? "KZT"}
                      </p>
                    </div>
                    <div className="flex items-end">
                      <button
                        type="button"
                        onClick={() => removePurchaseLine(index)}
                        className={secondaryButtonClass}
                        disabled={purchaseForm.isSaving || purchaseForm.values.lines.length <= 1}
                        aria-label={`Удалить строку ${index + 1}`}
                      >
                        Удалить
                      </button>
                    </div>
                  </div>
                ))}
              </div>
              <FieldError message={getFieldError(purchaseForm.fieldErrors, "lines")} />
            </div>

            {purchaseForm.error ? (
              <div className="mt-4 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
                {purchaseForm.error}
                {purchaseForm.error.includes("измен") ? (
                  <button
                    type="button"
                    onClick={() =>
                      purchaseForm.purchase && void loadPurchaseDetail(purchaseForm.purchase.id)
                    }
                    className={`${secondaryButtonClass} mt-3 block`}
                  >
                    Обновить документ
                  </button>
                ) : null}
              </div>
            ) : null}

            <div className="mt-5 flex flex-wrap gap-2">
              <button
                type="submit"
                className={primaryButtonClass}
                disabled={purchaseForm.isSaving}
              >
                {purchaseForm.isSaving ? "Сохраняем…" : "Сохранить черновик"}
              </button>
              <button
                type="button"
                onClick={closePurchaseForm}
                className={secondaryButtonClass}
                disabled={purchaseForm.isSaving}
              >
                Закрыть
              </button>
            </div>
          </form>
        ) : null}

        {cancelForm ? (
          <form
            onSubmit={handleCancelPurchase}
            className="mt-6 rounded-2xl border border-red-200 bg-red-50 p-5 dark:border-red-900 dark:bg-red-950"
          >
            <h3 className="text-xl font-semibold text-red-800 dark:text-red-200">
              Отмена закупки {cancelForm.purchase.id}
            </h3>
            <label className="mt-4 block text-red-800 dark:text-red-200">
              <span className="text-sm font-semibold">Причина отмены *</span>
              <textarea
                value={cancelForm.reason}
                onChange={(event) => updateCancelReason(event.target.value)}
                className={`${inputClass} min-h-24 bg-white dark:bg-[var(--surface)]`}
                disabled={cancelForm.isSaving}
              />
              <FieldError message={getFieldError(cancelForm.fieldErrors, "reason")} />
            </label>
            {cancelForm.error ? (
              <p className="mt-4 text-sm text-red-800 dark:text-red-200" role="alert">
                {cancelForm.error}
              </p>
            ) : null}
            <div className="mt-5 flex flex-wrap gap-2">
              <button type="submit" className={dangerButtonClass} disabled={cancelForm.isSaving}>
                {cancelForm.isSaving ? "Отменяем…" : "Подтвердить отмену"}
              </button>
              <button
                type="button"
                onClick={() => setCancelForm(null)}
                className={secondaryButtonClass}
                disabled={cancelForm.isSaving}
              >
                Закрыть
              </button>
            </div>
          </form>
        ) : null}
      </section>

      <section className="rounded-2xl border border-[var(--border)] bg-[var(--surface-muted)] p-5 text-sm leading-6 text-[var(--muted)]">
        <h2 className="text-lg font-semibold text-[var(--foreground)]">Гарантии Sprint 4</h2>
        <ul className="mt-3 grid gap-2 md:grid-cols-2">
          <li>Браузер ходит только в same-origin `/api/*`, прямого ERPNext URL нет.</li>
          <li>POST-мутации используют UUID `Idempotency-Key`; повторные клики блокируются.</li>
          <li>Активные поставщики доступны для новых закупок; архивные остаются в фильтрах истории.</li>
          <li>403 показывается ролям вне Owner/Admin, текущие роли: {roleLabel}.</li>
        </ul>
      </section>
    </div>
  );
}
