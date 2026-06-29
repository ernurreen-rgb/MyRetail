export type MovementType = "receipt" | "write_off" | "transfer" | "adjustment";

export type MovementStatus = "posted" | "cancelled";

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

export type ReasonOption = {
  code: string;
  name: string;
};

export type StockOptions = {
  warehouses: Warehouse[];
  write_off_reasons: ReasonOption[];
  adjustment_reasons: ReasonOption[];
};

export type StockBalance = {
  product_id: string;
  sku: string;
  name: string;
  unit: string;
  warehouse: WarehouseRef;
  on_hand: string;
  reserved: string;
  available: string;
  updated_at: string;
};

export type StockBalanceList = {
  items: StockBalance[];
  count: number;
  limit: number;
  offset: number;
};

export type AuditUser = {
  email: string;
  full_name: string | null;
};

export type StockMovementLine = {
  product_id: string;
  quantity: string;
  before_quantity: string;
  after_quantity: string;
};

export type StockMovement = {
  id: string;
  type: MovementType;
  status: MovementStatus;
  warehouse_id: string;
  destination_warehouse_id: string | null;
  reason_code: string | null;
  comment: string | null;
  created_by: AuditUser;
  created_at: string;
  cancelled_by: AuditUser | null;
  cancelled_at: string | null;
  reversal_movement_id: string | null;
  lines: StockMovementLine[];
};

export type StockMovementList = {
  items: StockMovement[];
  count: number;
  limit: number;
  offset: number;
};

export type StockMovementCancelResponse = {
  movement: StockMovement;
  reversal: StockMovement;
};

export type StockMovementFormValues = {
  type: MovementType;
  product_id: string;
  warehouse_id: string;
  destination_warehouse_id: string;
  quantity: string;
  counted_quantity: string;
  expected_quantity: string;
  reason_code: string;
  comment: string;
};

export type StockMovementCreatePayload = {
  type: MovementType;
  warehouse_id: string;
  destination_warehouse_id: string | null;
  reason_code: string | null;
  comment: string | null;
  lines: Array<{
    product_id: string;
    quantity?: string;
    counted_quantity?: string;
    expected_quantity?: string;
  }>;
};

export type StockMovementCancelPayload = {
  reason: string;
};

export type StockApiError = {
  code: string;
  message: string;
  fields: Record<string, string>;
};

export type StockOptionsClientResult =
  | {
      status: "success";
      data: StockOptions;
    }
  | {
      status: "error";
      statusCode: number;
      error: StockApiError;
    };

export type StockBalancesClientResult =
  | {
      status: "success";
      data: StockBalanceList;
    }
  | {
      status: "error";
      statusCode: number;
      error: StockApiError;
    };

export type StockMovementsClientResult =
  | {
      status: "success";
      data: StockMovementList;
    }
  | {
      status: "error";
      statusCode: number;
      error: StockApiError;
    };

export type StockMovementMutationResult =
  | {
      status: "success";
      data: StockMovement;
    }
  | {
      status: "error";
      statusCode: number;
      error: StockApiError;
    };

export type StockMovementCancelResult =
  | {
      status: "success";
      data: StockMovementCancelResponse;
    }
  | {
      status: "error";
      statusCode: number;
      error: StockApiError;
    };

const DEFAULT_STOCK_ERROR: StockApiError = {
  code: "REQUEST_ERROR",
  message: "Запрос склада не может быть обработан. Попробуйте ещё раз.",
  fields: {},
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isNullableString(value: unknown): value is string | null {
  return typeof value === "string" || value === null;
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

function isReasonOption(value: unknown): value is ReasonOption {
  return isRecord(value) && typeof value.code === "string" && typeof value.name === "string";
}

function isMovementType(value: unknown): value is MovementType {
  return (
    value === "receipt" ||
    value === "write_off" ||
    value === "transfer" ||
    value === "adjustment"
  );
}

function isMovementStatus(value: unknown): value is MovementStatus {
  return value === "posted" || value === "cancelled";
}

function isAuditUser(value: unknown): value is AuditUser {
  return (
    isRecord(value) &&
    typeof value.email === "string" &&
    isNullableString(value.full_name)
  );
}

function isStockMovementLine(value: unknown): value is StockMovementLine {
  return (
    isRecord(value) &&
    typeof value.product_id === "string" &&
    typeof value.quantity === "string" &&
    typeof value.before_quantity === "string" &&
    typeof value.after_quantity === "string"
  );
}

export function isStockOptions(value: unknown): value is StockOptions {
  return (
    isRecord(value) &&
    Array.isArray(value.warehouses) &&
    value.warehouses.every(isWarehouse) &&
    Array.isArray(value.write_off_reasons) &&
    value.write_off_reasons.every(isReasonOption) &&
    Array.isArray(value.adjustment_reasons) &&
    value.adjustment_reasons.every(isReasonOption)
  );
}

export function isStockBalance(value: unknown): value is StockBalance {
  return (
    isRecord(value) &&
    typeof value.product_id === "string" &&
    typeof value.sku === "string" &&
    typeof value.name === "string" &&
    typeof value.unit === "string" &&
    isWarehouseRef(value.warehouse) &&
    typeof value.on_hand === "string" &&
    typeof value.reserved === "string" &&
    typeof value.available === "string" &&
    typeof value.updated_at === "string"
  );
}

export function isStockBalanceList(value: unknown): value is StockBalanceList {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isStockBalance) &&
    typeof value.count === "number" &&
    typeof value.limit === "number" &&
    typeof value.offset === "number"
  );
}

export function isStockMovement(value: unknown): value is StockMovement {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    isMovementType(value.type) &&
    isMovementStatus(value.status) &&
    typeof value.warehouse_id === "string" &&
    isNullableString(value.destination_warehouse_id) &&
    isNullableString(value.reason_code) &&
    isNullableString(value.comment) &&
    isAuditUser(value.created_by) &&
    typeof value.created_at === "string" &&
    (value.cancelled_by === null || isAuditUser(value.cancelled_by)) &&
    isNullableString(value.cancelled_at) &&
    isNullableString(value.reversal_movement_id) &&
    Array.isArray(value.lines) &&
    value.lines.every(isStockMovementLine)
  );
}

export function isStockMovementList(value: unknown): value is StockMovementList {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isStockMovement) &&
    typeof value.count === "number" &&
    typeof value.limit === "number" &&
    typeof value.offset === "number"
  );
}

export function isStockMovementCancelResponse(
  value: unknown,
): value is StockMovementCancelResponse {
  return (
    isRecord(value) &&
    isStockMovement(value.movement) &&
    isStockMovement(value.reversal)
  );
}

export function isStockApiError(value: unknown): value is StockApiError {
  return (
    isRecord(value) &&
    typeof value.code === "string" &&
    typeof value.message === "string" &&
    isRecord(value.fields) &&
    Object.values(value.fields).every((fieldError) => typeof fieldError === "string")
  );
}

export function parseStockApiError(value: unknown): StockApiError {
  if (!isRecord(value) || !isStockApiError(value.error)) {
    return DEFAULT_STOCK_ERROR;
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

export function emptyStockMovementFormValues(
  values: Partial<StockMovementFormValues> = {},
): StockMovementFormValues {
  return {
    type: values.type ?? "receipt",
    product_id: values.product_id ?? "",
    warehouse_id: values.warehouse_id ?? "",
    destination_warehouse_id: values.destination_warehouse_id ?? "",
    quantity: values.quantity ?? "",
    counted_quantity: values.counted_quantity ?? "",
    expected_quantity: values.expected_quantity ?? "",
    reason_code: values.reason_code ?? "",
    comment: values.comment ?? "",
  };
}

function normalizeText(value: string) {
  return value.trim();
}

function optionalText(value: string) {
  const normalized = normalizeText(value);
  return normalized ? normalized : null;
}

function normalizeQuantityInput(value: string) {
  return normalizeText(value).replace(",", ".");
}

export function toStockMovementPayload(
  values: StockMovementFormValues,
): StockMovementCreatePayload {
  const line =
    values.type === "adjustment"
      ? {
          product_id: normalizeText(values.product_id),
          counted_quantity: normalizeQuantityInput(values.counted_quantity),
          expected_quantity: normalizeQuantityInput(values.expected_quantity),
        }
      : {
          product_id: normalizeText(values.product_id),
          quantity: normalizeQuantityInput(values.quantity),
        };

  return {
    type: values.type,
    warehouse_id: normalizeText(values.warehouse_id),
    destination_warehouse_id:
      values.type === "transfer" ? optionalText(values.destination_warehouse_id) : null,
    reason_code:
      values.type === "write_off" || values.type === "adjustment"
        ? optionalText(values.reason_code)
        : null,
    comment: optionalText(values.comment),
    lines: [line],
  };
}

export function toStockMovementCancelPayload(
  reason: string,
): StockMovementCancelPayload {
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

function toNetworkError(): StockApiError {
  return {
    code: "NETWORK_ERROR",
    message:
      "Не удалось связаться с веб-приложением. Проверьте подключение и попробуйте ещё раз.",
    fields: {},
  };
}

function shouldRedirectToLogin(statusCode: number, error: StockApiError) {
  return statusCode === 401 || error.code === "AUTH_REQUIRED";
}

function redirectToLoginIfNeeded(statusCode: number, error: StockApiError) {
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

export async function getStockOptions(): Promise<StockOptionsClientResult> {
  try {
    const response = await fetch("/api/stock/options", { cache: "no-store" });
    const payload = await readJson(response);

    if (!response.ok) {
      const error = parseStockApiError(payload);
      redirectToLoginIfNeeded(response.status, error);
      return { status: "error", statusCode: response.status, error };
    }

    if (!isStockOptions(payload)) {
      return {
        status: "error",
        statusCode: 502,
        error: {
          code: "INVALID_RESPONSE",
          message: "API вернул неожиданный формат справочников склада.",
          fields: {},
        },
      };
    }

    return { status: "success", data: payload };
  } catch {
    return { status: "error", statusCode: 0, error: toNetworkError() };
  }
}

export async function listStockBalances(
  params: {
    q?: string;
    warehouseId?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<StockBalancesClientResult> {
  const searchParams = new URLSearchParams();

  appendStringParam(searchParams, "q", params.q);
  appendStringParam(searchParams, "warehouse_id", params.warehouseId);
  appendNumberParam(searchParams, "limit", params.limit);
  appendNumberParam(searchParams, "offset", params.offset);

  const queryString = searchParams.toString();
  const url = queryString ? `/api/stock/balances?${queryString}` : "/api/stock/balances";

  try {
    const response = await fetch(url, { cache: "no-store" });
    const payload = await readJson(response);

    if (!response.ok) {
      const error = parseStockApiError(payload);
      redirectToLoginIfNeeded(response.status, error);
      return { status: "error", statusCode: response.status, error };
    }

    if (!isStockBalanceList(payload)) {
      return {
        status: "error",
        statusCode: 502,
        error: {
          code: "INVALID_RESPONSE",
          message: "API вернул неожиданный формат остатков склада.",
          fields: {},
        },
      };
    }

    return { status: "success", data: payload };
  } catch {
    return { status: "error", statusCode: 0, error: toNetworkError() };
  }
}

export async function listStockMovements(
  params: {
    productId?: string;
    warehouseId?: string;
    type?: MovementType | "";
    status?: MovementStatus | "";
    dateFrom?: string;
    dateTo?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<StockMovementsClientResult> {
  const searchParams = new URLSearchParams();

  appendStringParam(searchParams, "product_id", params.productId);
  appendStringParam(searchParams, "warehouse_id", params.warehouseId);
  appendStringParam(searchParams, "type", params.type);
  appendStringParam(searchParams, "status", params.status);
  appendStringParam(searchParams, "date_from", params.dateFrom);
  appendStringParam(searchParams, "date_to", params.dateTo);
  appendNumberParam(searchParams, "limit", params.limit);
  appendNumberParam(searchParams, "offset", params.offset);

  const queryString = searchParams.toString();
  const url = queryString ? `/api/stock/movements?${queryString}` : "/api/stock/movements";

  try {
    const response = await fetch(url, { cache: "no-store" });
    const payload = await readJson(response);

    if (!response.ok) {
      const error = parseStockApiError(payload);
      redirectToLoginIfNeeded(response.status, error);
      return { status: "error", statusCode: response.status, error };
    }

    if (!isStockMovementList(payload)) {
      return {
        status: "error",
        statusCode: 502,
        error: {
          code: "INVALID_RESPONSE",
          message: "API вернул неожиданный формат истории склада.",
          fields: {},
        },
      };
    }

    return { status: "success", data: payload };
  } catch {
    return { status: "error", statusCode: 0, error: toNetworkError() };
  }
}

export async function createStockMovement(
  values: StockMovementFormValues,
  idempotencyKey: string,
): Promise<StockMovementMutationResult> {
  return mutateStockMovement("/api/stock/movements", {
    body: JSON.stringify(toStockMovementPayload(values)),
    idempotencyKey,
    validate: isStockMovement,
    invalidResponseMessage: "API вернул неожиданный формат движения склада.",
  });
}

export async function cancelStockMovement(
  movementId: string,
  reason: string,
  idempotencyKey: string,
): Promise<StockMovementCancelResult> {
  return mutateStockMovement(`/api/stock/movements/${encodeURIComponent(movementId)}/cancel`, {
    body: JSON.stringify(toStockMovementCancelPayload(reason)),
    idempotencyKey,
    validate: isStockMovementCancelResponse,
    invalidResponseMessage: "API вернул неожиданный формат отмены движения склада.",
  });
}

async function mutateStockMovement<T>(url: string, init: {
  body: string;
  idempotencyKey: string;
  validate: (value: unknown) => value is T;
  invalidResponseMessage: string;
}): Promise<
  | {
      status: "success";
      data: T;
    }
  | {
      status: "error";
      statusCode: number;
      error: StockApiError;
    }
> {
  try {
    const response = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": init.idempotencyKey,
      },
      body: init.body,
    });
    const payload = await readJson(response);

    if (!response.ok) {
      const error = parseStockApiError(payload);
      redirectToLoginIfNeeded(response.status, error);
      return { status: "error", statusCode: response.status, error };
    }

    if (!init.validate(payload)) {
      return {
        status: "error",
        statusCode: 502,
        error: {
          code: "INVALID_RESPONSE",
          message: init.invalidResponseMessage,
          fields: {},
        },
      };
    }

    return { status: "success", data: payload };
  } catch {
    return { status: "error", statusCode: 0, error: toNetworkError() };
  }
}
