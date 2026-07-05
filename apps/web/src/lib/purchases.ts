export type SupplierStatusFilter = "active" | "archived" | "all";

export type Supplier = {
  id: string;
  name: string;
  tax_id: string | null;
  contact_name: string | null;
  phone: string | null;
  email: string | null;
  address: string | null;
  is_active: boolean;
  updated_at: string;
};

export type SupplierList = {
  items: Supplier[];
  count: number;
  limit: number;
  offset: number;
};

export type SupplierFormValues = {
  name: string;
  tax_id: string;
  contact_name: string;
  phone: string;
  email: string;
  address: string;
};

export type SupplierCreatePayload = {
  name: string;
  tax_id: string | null;
  contact_name: string | null;
  phone: string | null;
  email: string | null;
  address: string | null;
};

export type SupplierUpdatePayload = SupplierCreatePayload & {
  expected_updated_at: string;
};

export type PurchaseStatus = "draft" | "posted" | "cancelled";

export type Warehouse = {
  id: string;
  name: string;
  is_default: boolean;
  is_active: boolean;
};

export type WarehouseRef = {
  id: string;
  name: string;
};

export type AuditUser = {
  email: string;
  full_name: string | null;
};

export type PurchaseSupplierRef = {
  id: string;
  name: string;
};

export type PurchaseLine = {
  product_id: string;
  sku: string;
  name: string;
  unit: string;
  quantity: string;
  unit_price: string;
  line_total: string;
};

export type Purchase = {
  id: string;
  status: PurchaseStatus;
  supplier: PurchaseSupplierRef;
  warehouse: WarehouseRef;
  posting_date: string;
  supplier_invoice_number: string | null;
  supplier_invoice_date: string | null;
  currency: string;
  comment: string | null;
  subtotal: string;
  total: string;
  created_by: AuditUser;
  created_at: string;
  submitted_by: AuditUser | null;
  submitted_at: string | null;
  cancelled_by: AuditUser | null;
  cancelled_at: string | null;
  updated_at: string;
  lines: PurchaseLine[];
};

export type PurchaseSummary = {
  id: string;
  status: PurchaseStatus;
  supplier: PurchaseSupplierRef;
  warehouse: WarehouseRef;
  posting_date: string;
  supplier_invoice_number: string | null;
  supplier_invoice_date: string | null;
  currency: string;
  subtotal: string;
  total: string;
  updated_at: string;
};

export type PurchaseList = {
  items: PurchaseSummary[];
  count: number;
  limit: number;
  offset: number;
};

export type PurchaseOptions = {
  warehouses: Warehouse[];
  currency: string;
  quantity_precision: number;
  money_precision: number;
};

export type PurchaseLineFormValues = {
  product_id: string;
  quantity: string;
  unit_price: string;
};

export type PurchaseFormValues = {
  supplier_id: string;
  warehouse_id: string;
  posting_date: string;
  supplier_invoice_number: string;
  supplier_invoice_date: string;
  comment: string;
  lines: PurchaseLineFormValues[];
};

export type PurchaseCreatePayload = {
  supplier_id: string;
  warehouse_id: string;
  posting_date: string;
  supplier_invoice_number: string | null;
  supplier_invoice_date: string | null;
  comment: string | null;
  lines: PurchaseLineFormValues[];
};

export type PurchaseUpdatePayload = PurchaseCreatePayload & {
  expected_updated_at: string;
};

export type PurchaseSubmitPayload = {
  expected_updated_at: string;
};

export type PurchaseCancelPayload = {
  reason: string;
};

export type PurchasesApiError = {
  code: string;
  message: string;
  fields: Record<string, string>;
};

type ClientErrorResult = {
  status: "error";
  statusCode: number;
  error: PurchasesApiError;
};

type ClientSuccessResult<T> = {
  status: "success";
  data: T;
};

export type SupplierListClientResult = ClientSuccessResult<SupplierList> | ClientErrorResult;
export type SupplierClientResult = ClientSuccessResult<Supplier> | ClientErrorResult;
export type SupplierArchiveResult = ClientSuccessResult<null> | ClientErrorResult;
export type PurchaseOptionsClientResult =
  | ClientSuccessResult<PurchaseOptions>
  | ClientErrorResult;
export type PurchaseListClientResult = ClientSuccessResult<PurchaseList> | ClientErrorResult;
export type PurchaseClientResult = ClientSuccessResult<Purchase> | ClientErrorResult;

const DEFAULT_PURCHASES_ERROR: PurchasesApiError = {
  code: "REQUEST_ERROR",
  message: "Запрос закупок не может быть обработан. Попробуйте ещё раз.",
  fields: {},
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isNullableString(value: unknown): value is string | null {
  return typeof value === "string" || value === null;
}

function isAuditUser(value: unknown): value is AuditUser {
  return isRecord(value) && typeof value.email === "string" && isNullableString(value.full_name);
}

function isWarehouse(value: unknown): value is Warehouse {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.name === "string" &&
    typeof value.is_default === "boolean" &&
    typeof value.is_active === "boolean"
  );
}

function isWarehouseRef(value: unknown): value is WarehouseRef {
  return isRecord(value) && typeof value.id === "string" && typeof value.name === "string";
}

function isPurchaseStatus(value: unknown): value is PurchaseStatus {
  return value === "draft" || value === "posted" || value === "cancelled";
}

function isPurchaseSupplierRef(value: unknown): value is PurchaseSupplierRef {
  return isRecord(value) && typeof value.id === "string" && typeof value.name === "string";
}

export function isSupplier(value: unknown): value is Supplier {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.name === "string" &&
    isNullableString(value.tax_id) &&
    isNullableString(value.contact_name) &&
    isNullableString(value.phone) &&
    isNullableString(value.email) &&
    isNullableString(value.address) &&
    typeof value.is_active === "boolean" &&
    typeof value.updated_at === "string"
  );
}

export function isSupplierList(value: unknown): value is SupplierList {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isSupplier) &&
    typeof value.count === "number" &&
    typeof value.limit === "number" &&
    typeof value.offset === "number"
  );
}

export function isPurchaseLine(value: unknown): value is PurchaseLine {
  return (
    isRecord(value) &&
    typeof value.product_id === "string" &&
    typeof value.sku === "string" &&
    typeof value.name === "string" &&
    typeof value.unit === "string" &&
    typeof value.quantity === "string" &&
    typeof value.unit_price === "string" &&
    typeof value.line_total === "string"
  );
}

export function isPurchase(value: unknown): value is Purchase {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    isPurchaseStatus(value.status) &&
    isPurchaseSupplierRef(value.supplier) &&
    isWarehouseRef(value.warehouse) &&
    typeof value.posting_date === "string" &&
    isNullableString(value.supplier_invoice_number) &&
    isNullableString(value.supplier_invoice_date) &&
    typeof value.currency === "string" &&
    isNullableString(value.comment) &&
    typeof value.subtotal === "string" &&
    typeof value.total === "string" &&
    isAuditUser(value.created_by) &&
    typeof value.created_at === "string" &&
    (value.submitted_by === null || isAuditUser(value.submitted_by)) &&
    isNullableString(value.submitted_at) &&
    (value.cancelled_by === null || isAuditUser(value.cancelled_by)) &&
    isNullableString(value.cancelled_at) &&
    typeof value.updated_at === "string" &&
    Array.isArray(value.lines) &&
    value.lines.every(isPurchaseLine)
  );
}

export function isPurchaseSummary(value: unknown): value is PurchaseSummary {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    isPurchaseStatus(value.status) &&
    isPurchaseSupplierRef(value.supplier) &&
    isWarehouseRef(value.warehouse) &&
    typeof value.posting_date === "string" &&
    isNullableString(value.supplier_invoice_number) &&
    isNullableString(value.supplier_invoice_date) &&
    typeof value.currency === "string" &&
    typeof value.subtotal === "string" &&
    typeof value.total === "string" &&
    typeof value.updated_at === "string"
  );
}

export function isPurchaseList(value: unknown): value is PurchaseList {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isPurchaseSummary) &&
    typeof value.count === "number" &&
    typeof value.limit === "number" &&
    typeof value.offset === "number"
  );
}

export function isPurchaseOptions(value: unknown): value is PurchaseOptions {
  return (
    isRecord(value) &&
    Array.isArray(value.warehouses) &&
    value.warehouses.every(isWarehouse) &&
    typeof value.currency === "string" &&
    typeof value.quantity_precision === "number" &&
    typeof value.money_precision === "number"
  );
}

export function isPurchasesApiError(value: unknown): value is PurchasesApiError {
  return (
    isRecord(value) &&
    typeof value.code === "string" &&
    typeof value.message === "string" &&
    isRecord(value.fields) &&
    Object.values(value.fields).every((fieldError) => typeof fieldError === "string")
  );
}

export function parsePurchasesApiError(value: unknown): PurchasesApiError {
  if (!isRecord(value) || !isPurchasesApiError(value.error)) {
    return DEFAULT_PURCHASES_ERROR;
  }

  return value.error;
}

export function createIdempotencyKey() {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }

  const bytes = new Uint8Array(16);

  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }

  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));

  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join(""),
  ].join("-");
}

export function emptySupplierFormValues(
  values: Partial<SupplierFormValues> = {},
): SupplierFormValues {
  return {
    name: values.name ?? "",
    tax_id: values.tax_id ?? "",
    contact_name: values.contact_name ?? "",
    phone: values.phone ?? "",
    email: values.email ?? "",
    address: values.address ?? "",
  };
}

export function supplierToFormValues(supplier: Supplier): SupplierFormValues {
  return {
    name: supplier.name,
    tax_id: supplier.tax_id ?? "",
    contact_name: supplier.contact_name ?? "",
    phone: supplier.phone ?? "",
    email: supplier.email ?? "",
    address: supplier.address ?? "",
  };
}

function todayIsoDate() {
  return new Date().toISOString().slice(0, 10);
}

export function emptyPurchaseFormValues(
  values: Partial<PurchaseFormValues> = {},
): PurchaseFormValues {
  return {
    supplier_id: values.supplier_id ?? "",
    warehouse_id: values.warehouse_id ?? "",
    posting_date: values.posting_date ?? todayIsoDate(),
    supplier_invoice_number: values.supplier_invoice_number ?? "",
    supplier_invoice_date: values.supplier_invoice_date ?? "",
    comment: values.comment ?? "",
    lines: values.lines ?? [{ product_id: "", quantity: "", unit_price: "" }],
  };
}

export function purchaseToFormValues(purchase: Purchase): PurchaseFormValues {
  return {
    supplier_id: purchase.supplier.id,
    warehouse_id: purchase.warehouse.id,
    posting_date: purchase.posting_date,
    supplier_invoice_number: purchase.supplier_invoice_number ?? "",
    supplier_invoice_date: purchase.supplier_invoice_date ?? "",
    comment: purchase.comment ?? "",
    lines: purchase.lines.map((line) => ({
      product_id: line.product_id,
      quantity: line.quantity,
      unit_price: line.unit_price,
    })),
  };
}

function normalizeText(value: string) {
  return value.trim();
}

function optionalText(value: string) {
  const normalized = normalizeText(value);
  return normalized ? normalized : null;
}

function normalizeDecimalInput(value: string) {
  return normalizeText(value).replace(",", ".");
}

function normalizeLine(line: PurchaseLineFormValues): PurchaseLineFormValues {
  return {
    product_id: normalizeText(line.product_id),
    quantity: normalizeDecimalInput(line.quantity),
    unit_price: normalizeDecimalInput(line.unit_price),
  };
}

export function toSupplierCreatePayload(values: SupplierFormValues): SupplierCreatePayload {
  return {
    name: normalizeText(values.name),
    tax_id: optionalText(values.tax_id),
    contact_name: optionalText(values.contact_name),
    phone: optionalText(values.phone),
    email: optionalText(values.email),
    address: optionalText(values.address),
  };
}

export function toSupplierUpdatePayload(
  values: SupplierFormValues,
  expectedUpdatedAt: string,
): SupplierUpdatePayload {
  return {
    expected_updated_at: expectedUpdatedAt,
    ...toSupplierCreatePayload(values),
  };
}

export function toPurchaseCreatePayload(values: PurchaseFormValues): PurchaseCreatePayload {
  return {
    supplier_id: normalizeText(values.supplier_id),
    warehouse_id: normalizeText(values.warehouse_id),
    posting_date: normalizeText(values.posting_date),
    supplier_invoice_number: optionalText(values.supplier_invoice_number),
    supplier_invoice_date: optionalText(values.supplier_invoice_date),
    comment: optionalText(values.comment),
    lines: values.lines.map(normalizeLine),
  };
}

export function toPurchaseUpdatePayload(
  values: PurchaseFormValues,
  expectedUpdatedAt: string,
): PurchaseUpdatePayload {
  return {
    expected_updated_at: expectedUpdatedAt,
    ...toPurchaseCreatePayload(values),
  };
}

export function toPurchaseSubmitPayload(expectedUpdatedAt: string): PurchaseSubmitPayload {
  return {
    expected_updated_at: expectedUpdatedAt,
  };
}

export function toPurchaseCancelPayload(reason: string): PurchaseCancelPayload {
  return {
    reason: normalizeText(reason),
  };
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function toNetworkError(): PurchasesApiError {
  return {
    code: "NETWORK_ERROR",
    message:
      "Не удалось связаться с веб-приложением. Проверьте подключение и попробуйте ещё раз.",
    fields: {},
  };
}

function shouldRedirectToLogin(statusCode: number, error: PurchasesApiError) {
  return statusCode === 401 || error.code === "UNAUTHORIZED" || error.code === "AUTH_REQUIRED";
}

function redirectToLoginIfNeeded(statusCode: number, error: PurchasesApiError) {
  if (typeof window !== "undefined" && shouldRedirectToLogin(statusCode, error)) {
    window.setTimeout(() => window.location.assign("/login"), 900);
  }
}

function appendStringParam(params: URLSearchParams, key: string, value?: string) {
  const normalized = value?.trim();

  if (normalized) {
    params.set(key, normalized);
  }
}

function appendNumberParam(params: URLSearchParams, key: string, value?: number) {
  if (typeof value === "number") {
    params.set(key, String(value));
  }
}

export async function listSuppliers(
  params: {
    q?: string;
    status?: SupplierStatusFilter | "";
    limit?: number;
    offset?: number;
  } = {},
): Promise<SupplierListClientResult> {
  const searchParams = new URLSearchParams();

  appendStringParam(searchParams, "q", params.q);
  appendStringParam(searchParams, "status", params.status);
  appendNumberParam(searchParams, "limit", params.limit);
  appendNumberParam(searchParams, "offset", params.offset);

  const queryString = searchParams.toString();
  const url = queryString ? `/api/suppliers?${queryString}` : "/api/suppliers";

  return getPurchasesResource(url, isSupplierList, "API вернул неожиданный формат поставщиков.");
}

export async function getSupplier(supplierId: string): Promise<SupplierClientResult> {
  return getPurchasesResource(
    `/api/suppliers/${encodeURIComponent(supplierId)}`,
    isSupplier,
    "API вернул неожиданный формат поставщика.",
  );
}

export async function createSupplier(
  values: SupplierFormValues,
  idempotencyKey: string,
): Promise<SupplierClientResult> {
  return mutatePurchasesResource(
    "/api/suppliers",
    {
      method: "POST",
      body: JSON.stringify(toSupplierCreatePayload(values)),
      idempotencyKey,
    },
    isSupplier,
    "API вернул неожиданный формат поставщика.",
  );
}

export async function updateSupplier(
  supplierId: string,
  values: SupplierFormValues,
  expectedUpdatedAt: string,
): Promise<SupplierClientResult> {
  return mutatePurchasesResource(
    `/api/suppliers/${encodeURIComponent(supplierId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(toSupplierUpdatePayload(values, expectedUpdatedAt)),
    },
    isSupplier,
    "API вернул неожиданный формат поставщика.",
  );
}

export async function archiveSupplier(supplierId: string): Promise<SupplierArchiveResult> {
  return mutatePurchasesResource(
    `/api/suppliers/${encodeURIComponent(supplierId)}`,
    {
      method: "DELETE",
    },
    (value): value is null => value === null,
    "API вернул неожиданный ответ архивации поставщика.",
  );
}

export async function getPurchaseOptions(): Promise<PurchaseOptionsClientResult> {
  return getPurchasesResource(
    "/api/purchases/options",
    isPurchaseOptions,
    "API вернул неожиданный формат справочников закупок.",
  );
}

export async function listPurchases(
  params: {
    q?: string;
    supplierId?: string;
    warehouseId?: string;
    status?: PurchaseStatus | "";
    dateFrom?: string;
    dateTo?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<PurchaseListClientResult> {
  const searchParams = new URLSearchParams();

  appendStringParam(searchParams, "q", params.q);
  appendStringParam(searchParams, "supplier_id", params.supplierId);
  appendStringParam(searchParams, "warehouse_id", params.warehouseId);
  appendStringParam(searchParams, "status", params.status);
  appendStringParam(searchParams, "date_from", params.dateFrom);
  appendStringParam(searchParams, "date_to", params.dateTo);
  appendNumberParam(searchParams, "limit", params.limit);
  appendNumberParam(searchParams, "offset", params.offset);

  const queryString = searchParams.toString();
  const url = queryString ? `/api/purchases?${queryString}` : "/api/purchases";

  return getPurchasesResource(url, isPurchaseList, "API вернул неожиданный формат закупок.");
}

export async function getPurchase(purchaseId: string): Promise<PurchaseClientResult> {
  return getPurchasesResource(
    `/api/purchases/${encodeURIComponent(purchaseId)}`,
    isPurchase,
    "API вернул неожиданный формат закупки.",
  );
}

export async function createPurchase(
  values: PurchaseFormValues,
  idempotencyKey: string,
): Promise<PurchaseClientResult> {
  return mutatePurchasesResource(
    "/api/purchases",
    {
      method: "POST",
      body: JSON.stringify(toPurchaseCreatePayload(values)),
      idempotencyKey,
    },
    isPurchase,
    "API вернул неожиданный формат закупки.",
  );
}

export async function updatePurchase(
  purchaseId: string,
  values: PurchaseFormValues,
  expectedUpdatedAt: string,
): Promise<PurchaseClientResult> {
  return mutatePurchasesResource(
    `/api/purchases/${encodeURIComponent(purchaseId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(toPurchaseUpdatePayload(values, expectedUpdatedAt)),
    },
    isPurchase,
    "API вернул неожиданный формат закупки.",
  );
}

export async function submitPurchase(
  purchaseId: string,
  expectedUpdatedAt: string,
  idempotencyKey: string,
): Promise<PurchaseClientResult> {
  return mutatePurchasesResource(
    `/api/purchases/${encodeURIComponent(purchaseId)}/submit`,
    {
      method: "POST",
      body: JSON.stringify(toPurchaseSubmitPayload(expectedUpdatedAt)),
      idempotencyKey,
    },
    isPurchase,
    "API вернул неожиданный формат проведённой закупки.",
  );
}

export async function cancelPurchase(
  purchaseId: string,
  reason: string,
  idempotencyKey: string,
): Promise<PurchaseClientResult> {
  return mutatePurchasesResource(
    `/api/purchases/${encodeURIComponent(purchaseId)}/cancel`,
    {
      method: "POST",
      body: JSON.stringify(toPurchaseCancelPayload(reason)),
      idempotencyKey,
    },
    isPurchase,
    "API вернул неожиданный формат отменённой закупки.",
  );
}

async function getPurchasesResource<T>(
  url: string,
  validate: (value: unknown) => value is T,
  invalidResponseMessage: string,
): Promise<ClientSuccessResult<T> | ClientErrorResult> {
  try {
    const response = await fetch(url, { cache: "no-store" });
    const payload = await readJson(response);

    if (!response.ok) {
      const error = parsePurchasesApiError(payload);
      redirectToLoginIfNeeded(response.status, error);
      return { status: "error", statusCode: response.status, error };
    }

    if (!validate(payload)) {
      return {
        status: "error",
        statusCode: 502,
        error: {
          code: "INVALID_RESPONSE",
          message: invalidResponseMessage,
          fields: {},
        },
      };
    }

    return { status: "success", data: payload };
  } catch {
    return { status: "error", statusCode: 0, error: toNetworkError() };
  }
}

async function mutatePurchasesResource<T>(
  url: string,
  init: {
    method: "POST" | "PATCH" | "DELETE";
    body?: string;
    idempotencyKey?: string;
  },
  validate: (value: unknown) => value is T,
  invalidResponseMessage: string,
): Promise<ClientSuccessResult<T> | ClientErrorResult> {
  try {
    const headers: HeadersInit | undefined = init.body
      ? {
          "Content-Type": "application/json",
        }
      : undefined;

    if (headers && init.idempotencyKey) {
      headers["Idempotency-Key"] = init.idempotencyKey;
    }

    const response = await fetch(url, {
      method: init.method,
      cache: "no-store",
      headers,
      body: init.body,
    });

    if (response.status === 204) {
      return { status: "success", data: null as T };
    }

    const payload = await readJson(response);

    if (!response.ok) {
      const error = parsePurchasesApiError(payload);
      redirectToLoginIfNeeded(response.status, error);
      return { status: "error", statusCode: response.status, error };
    }

    if (!validate(payload)) {
      return {
        status: "error",
        statusCode: 502,
        error: {
          code: "INVALID_RESPONSE",
          message: invalidResponseMessage,
          fields: {},
        },
      };
    }

    return { status: "success", data: payload };
  } catch {
    return { status: "error", statusCode: 0, error: toNetworkError() };
  }
}
