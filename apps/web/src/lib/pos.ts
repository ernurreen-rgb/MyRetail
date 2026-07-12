export type WarehouseRef = {
  id: string;
  name: string;
};

export type CashierRef = {
  email: string;
  full_name: string | null;
};

export type Register = {
  id: string;
  name: string;
  warehouse: WarehouseRef;
  currency: string;
  payment_methods: string[];
  is_active: boolean;
};

export type POSOptions = {
  registers: Register[];
  payment_methods: Record<string, string>[];
  discount_limit_percent: string;
};

export type ShiftRegisterRef = {
  id: string;
  name: string;
};

export type ShiftStatus = "open" | "closed";

export type Shift = {
  id: string;
  register: ShiftRegisterRef;
  warehouse: WarehouseRef;
  cashier: CashierRef;
  status: ShiftStatus;
  opening_cash: string;
  sales_total: string;
  expected_cash: string;
  actual_cash: string | null;
  difference: string | null;
  opened_at: string;
  closed_at: string | null;
  updated_at: string;
};

export type POSProduct = {
  id: string;
  sku: string;
  name: string;
  barcode: string | null;
  unit: string;
  sale_price: string;
  currency: string;
  available: string;
  is_active: boolean;
  allows_fractional_quantity: boolean;
};

export type POSProductList = {
  items: POSProduct[];
  count: number;
  limit: number;
  offset: number;
};

export type POSLineInput = {
  product_id: string;
  quantity: string;
  discount_percent: string;
};

export type ReturnStatus = "none" | "partial" | "full";
export type ReturnState = "submitted" | "cancelled" | "pending_recovery";
export type ReturnReason = "customer_request" | "cashier_error" | "damaged" | "other";

export type SaleLine = {
  line_id: string | null;
  product_id: string;
  sku: string;
  name: string;
  unit: string;
  quantity: string;
  unit_price: string;
  subtotal: string;
  discount_percent: string;
  discount_amount: string;
  total: string;
  returned_quantity: string;
  available_to_return_quantity: string;
};

export type Sale = {
  id: string;
  receipt_number: string;
  status: "completed";
  shift_id: string;
  register: ShiftRegisterRef;
  warehouse: WarehouseRef;
  cashier: CashierRef;
  currency: string;
  lines: SaleLine[];
  subtotal: string;
  discount_total: string;
  grand_total: string;
  cash_received: string;
  change: string;
  created_at: string;
  return_status: ReturnStatus;
  returned_total: string;
};

export type SaleList = {
  items: Sale[];
  count: number;
  limit: number;
  offset: number;
};

export type ReturnTotals = {
  refund_total: string;
  sold_total: string | null;
  already_returned_total: string | null;
  available_to_return_total: string | null;
};

export type ReturnOptionsLine = {
  line_id: string;
  item_id: string;
  item_name: string;
  sold_quantity: string;
  already_returned_quantity: string;
  available_to_return_quantity: string;
  unit: string;
  unit_price: string;
  line_total: string;
};

export type ReturnOptions = {
  sale_id: string;
  receipt_number: string;
  status: "submitted";
  return_status: ReturnStatus;
  register_id: string;
  shift_id: string;
  cashier_email: string;
  created_at: string;
  currency: string;
  lines: ReturnOptionsLine[];
  totals: ReturnTotals;
};

export type ReturnLineInput = {
  line_id: string;
  quantity: string;
};

export type ReturnCreatePayload = {
  sale_id: string;
  register_id: string;
  shift_id: string;
  refund_method: "cash";
  reason: ReturnReason;
  comment: string | null;
  lines: ReturnLineInput[];
};

export type ReturnCancelPayload = {
  reason: ReturnReason;
  comment: string | null;
};

export type ReturnLine = {
  line_id: string;
  item_id: string;
  item_name: string;
  quantity: string;
  unit: string;
  unit_price: string;
  line_total: string;
};

export type POSReturn = {
  return_id: string;
  sale_id: string;
  receipt_number: string;
  return_receipt_number: string;
  state: ReturnState;
  return_status_after: ReturnStatus;
  refund_method: "cash";
  reason: ReturnReason;
  comment: string | null;
  currency: string;
  register_id: string;
  shift_id: string;
  lines: ReturnLine[];
  totals: ReturnTotals;
  created_by: string;
  created_at: string;
  cancelled_by: string | null;
  cancelled_at: string | null;
};

export type ReturnHistoryItem = {
  return_id: string;
  sale_id: string;
  receipt_number: string;
  return_receipt_number: string;
  state: ReturnState;
  refund_total: string;
  currency: string;
  register_id: string;
  shift_id: string;
  cashier_email: string;
  created_at: string;
};

export type ReturnList = {
  items: ReturnHistoryItem[];
  count: number;
  limit: number;
  offset: number;
};

export type HeldReceipt = {
  id: string;
  shift_id: string;
  label: string | null;
  lines: SaleLine[];
  subtotal: string;
  discount_total: string;
  grand_total: string;
  created_by: CashierRef;
  created_at: string;
  updated_at: string;
};

export type HeldReceiptList = {
  items: HeldReceipt[];
  count: number;
  limit: number;
  offset: number;
};

export type ShiftOpenPayload = {
  register_id: string;
  opening_cash: string;
};

export type ShiftClosePayload = {
  actual_cash: string;
  expected_updated_at: string;
  reason: string | null;
};

export type HeldReceiptCreatePayload = {
  shift_id: string;
  label: string | null;
  lines: POSLineInput[];
};

export type HeldReceiptUpdatePayload = {
  expected_updated_at: string;
  label: string | null;
  lines?: POSLineInput[];
};

export type SaleCreatePayload = {
  shift_id: string;
  held_receipt_id: string | null;
  lines: POSLineInput[];
  cash_received: string;
};

export type POSApiError = {
  code: string;
  message: string;
  fields: Record<string, string>;
};

type ClientErrorResult = {
  status: "error";
  statusCode: number;
  error: POSApiError;
};

type ClientSuccessResult<T> = {
  status: "success";
  data: T;
};

export type POSOptionsClientResult = ClientSuccessResult<POSOptions> | ClientErrorResult;
export type POSProductListClientResult = ClientSuccessResult<POSProductList> | ClientErrorResult;
export type ShiftClientResult = ClientSuccessResult<Shift> | ClientErrorResult;
export type HeldReceiptListClientResult =
  | ClientSuccessResult<HeldReceiptList>
  | ClientErrorResult;
export type HeldReceiptClientResult = ClientSuccessResult<HeldReceipt> | ClientErrorResult;
export type HeldReceiptDeleteResult = ClientSuccessResult<null> | ClientErrorResult;
export type SaleClientResult = ClientSuccessResult<Sale> | ClientErrorResult;
export type SaleListClientResult = ClientSuccessResult<SaleList> | ClientErrorResult;
export type ReturnOptionsClientResult = ClientSuccessResult<ReturnOptions> | ClientErrorResult;
export type ReturnClientResult = ClientSuccessResult<POSReturn> | ClientErrorResult;
export type ReturnListClientResult = ClientSuccessResult<ReturnList> | ClientErrorResult;

const DEFAULT_POS_ERROR: POSApiError = {
  code: "REQUEST_ERROR",
  message: "Запрос кассы не может быть обработан. Попробуйте ещё раз.",
  fields: {},
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isNullableString(value: unknown): value is string | null {
  return typeof value === "string" || value === null;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isStringRecord(value: unknown): value is Record<string, string> {
  return isRecord(value) && Object.values(value).every((item) => typeof item === "string");
}

function isCashierRef(value: unknown): value is CashierRef {
  return isRecord(value) && typeof value.email === "string" && isNullableString(value.full_name);
}

function isWarehouseRef(value: unknown): value is WarehouseRef {
  return isRecord(value) && typeof value.id === "string" && typeof value.name === "string";
}

function isShiftRegisterRef(value: unknown): value is ShiftRegisterRef {
  return isRecord(value) && typeof value.id === "string" && typeof value.name === "string";
}

function isShiftStatus(value: unknown): value is ShiftStatus {
  return value === "open" || value === "closed";
}

function isReturnStatus(value: unknown): value is ReturnStatus {
  return value === "none" || value === "partial" || value === "full";
}

function isReturnState(value: unknown): value is ReturnState {
  return value === "submitted" || value === "cancelled" || value === "pending_recovery";
}

function isReturnReason(value: unknown): value is ReturnReason {
  return (
    value === "customer_request" ||
    value === "cashier_error" ||
    value === "damaged" ||
    value === "other"
  );
}

function isRegister(value: unknown): value is Register {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.name === "string" &&
    isWarehouseRef(value.warehouse) &&
    typeof value.currency === "string" &&
    isStringArray(value.payment_methods) &&
    typeof value.is_active === "boolean"
  );
}

export function isPOSOptions(value: unknown): value is POSOptions {
  return (
    isRecord(value) &&
    Array.isArray(value.registers) &&
    value.registers.every(isRegister) &&
    Array.isArray(value.payment_methods) &&
    value.payment_methods.every(isStringRecord) &&
    typeof value.discount_limit_percent === "string"
  );
}

export function isShift(value: unknown): value is Shift {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    isShiftRegisterRef(value.register) &&
    isWarehouseRef(value.warehouse) &&
    isCashierRef(value.cashier) &&
    isShiftStatus(value.status) &&
    typeof value.opening_cash === "string" &&
    typeof value.sales_total === "string" &&
    typeof value.expected_cash === "string" &&
    isNullableString(value.actual_cash) &&
    isNullableString(value.difference) &&
    typeof value.opened_at === "string" &&
    isNullableString(value.closed_at) &&
    typeof value.updated_at === "string"
  );
}

export function isPOSProduct(value: unknown): value is POSProduct {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.sku === "string" &&
    typeof value.name === "string" &&
    isNullableString(value.barcode) &&
    typeof value.unit === "string" &&
    typeof value.sale_price === "string" &&
    typeof value.currency === "string" &&
    typeof value.available === "string" &&
    typeof value.is_active === "boolean" &&
    typeof value.allows_fractional_quantity === "boolean"
  );
}

export function isPOSProductList(value: unknown): value is POSProductList {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isPOSProduct) &&
    typeof value.count === "number" &&
    typeof value.limit === "number" &&
    typeof value.offset === "number"
  );
}

export function isSaleLine(value: unknown): value is SaleLine {
  return (
    isRecord(value) &&
    isNullableString(value.line_id) &&
    typeof value.product_id === "string" &&
    typeof value.sku === "string" &&
    typeof value.name === "string" &&
    typeof value.unit === "string" &&
    typeof value.quantity === "string" &&
    typeof value.unit_price === "string" &&
    typeof value.subtotal === "string" &&
    typeof value.discount_percent === "string" &&
    typeof value.discount_amount === "string" &&
    typeof value.total === "string" &&
    typeof value.returned_quantity === "string" &&
    typeof value.available_to_return_quantity === "string"
  );
}

export function isSale(value: unknown): value is Sale {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.receipt_number === "string" &&
    value.status === "completed" &&
    typeof value.shift_id === "string" &&
    isShiftRegisterRef(value.register) &&
    isWarehouseRef(value.warehouse) &&
    isCashierRef(value.cashier) &&
    typeof value.currency === "string" &&
    Array.isArray(value.lines) &&
    value.lines.every(isSaleLine) &&
    typeof value.subtotal === "string" &&
    typeof value.discount_total === "string" &&
    typeof value.grand_total === "string" &&
    typeof value.cash_received === "string" &&
    typeof value.change === "string" &&
    typeof value.created_at === "string" &&
    isReturnStatus(value.return_status) &&
    typeof value.returned_total === "string"
  );
}

export function isSaleList(value: unknown): value is SaleList {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isSale) &&
    typeof value.count === "number" &&
    typeof value.limit === "number" &&
    typeof value.offset === "number"
  );
}

export function isHeldReceipt(value: unknown): value is HeldReceipt {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.shift_id === "string" &&
    isNullableString(value.label) &&
    Array.isArray(value.lines) &&
    value.lines.every(isSaleLine) &&
    typeof value.subtotal === "string" &&
    typeof value.discount_total === "string" &&
    typeof value.grand_total === "string" &&
    isCashierRef(value.created_by) &&
    typeof value.created_at === "string" &&
    typeof value.updated_at === "string"
  );
}

export function isHeldReceiptList(value: unknown): value is HeldReceiptList {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isHeldReceipt) &&
    typeof value.count === "number" &&
    typeof value.limit === "number" &&
    typeof value.offset === "number"
  );
}

function isReturnTotals(value: unknown): value is ReturnTotals {
  return (
    isRecord(value) &&
    typeof value.refund_total === "string" &&
    isNullableString(value.sold_total) &&
    isNullableString(value.already_returned_total) &&
    isNullableString(value.available_to_return_total)
  );
}

function isReturnOptionsLine(value: unknown): value is ReturnOptionsLine {
  return (
    isRecord(value) &&
    typeof value.line_id === "string" &&
    typeof value.item_id === "string" &&
    typeof value.item_name === "string" &&
    typeof value.sold_quantity === "string" &&
    typeof value.already_returned_quantity === "string" &&
    typeof value.available_to_return_quantity === "string" &&
    typeof value.unit === "string" &&
    typeof value.unit_price === "string" &&
    typeof value.line_total === "string"
  );
}

export function isReturnOptions(value: unknown): value is ReturnOptions {
  return (
    isRecord(value) &&
    typeof value.sale_id === "string" &&
    typeof value.receipt_number === "string" &&
    value.status === "submitted" &&
    isReturnStatus(value.return_status) &&
    typeof value.register_id === "string" &&
    typeof value.shift_id === "string" &&
    typeof value.cashier_email === "string" &&
    typeof value.created_at === "string" &&
    typeof value.currency === "string" &&
    Array.isArray(value.lines) &&
    value.lines.every(isReturnOptionsLine) &&
    isReturnTotals(value.totals)
  );
}

function isReturnLine(value: unknown): value is ReturnLine {
  return (
    isRecord(value) &&
    typeof value.line_id === "string" &&
    typeof value.item_id === "string" &&
    typeof value.item_name === "string" &&
    typeof value.quantity === "string" &&
    typeof value.unit === "string" &&
    typeof value.unit_price === "string" &&
    typeof value.line_total === "string"
  );
}

export function isPOSReturn(value: unknown): value is POSReturn {
  return (
    isRecord(value) &&
    typeof value.return_id === "string" &&
    typeof value.sale_id === "string" &&
    typeof value.receipt_number === "string" &&
    typeof value.return_receipt_number === "string" &&
    isReturnState(value.state) &&
    isReturnStatus(value.return_status_after) &&
    value.refund_method === "cash" &&
    isReturnReason(value.reason) &&
    isNullableString(value.comment) &&
    typeof value.currency === "string" &&
    typeof value.register_id === "string" &&
    typeof value.shift_id === "string" &&
    Array.isArray(value.lines) &&
    value.lines.every(isReturnLine) &&
    isReturnTotals(value.totals) &&
    typeof value.created_by === "string" &&
    typeof value.created_at === "string" &&
    isNullableString(value.cancelled_by) &&
    isNullableString(value.cancelled_at)
  );
}

function isReturnHistoryItem(value: unknown): value is ReturnHistoryItem {
  return (
    isRecord(value) &&
    typeof value.return_id === "string" &&
    typeof value.sale_id === "string" &&
    typeof value.receipt_number === "string" &&
    typeof value.return_receipt_number === "string" &&
    isReturnState(value.state) &&
    typeof value.refund_total === "string" &&
    typeof value.currency === "string" &&
    typeof value.register_id === "string" &&
    typeof value.shift_id === "string" &&
    typeof value.cashier_email === "string" &&
    typeof value.created_at === "string"
  );
}

export function isReturnList(value: unknown): value is ReturnList {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isReturnHistoryItem) &&
    typeof value.count === "number" &&
    typeof value.limit === "number" &&
    typeof value.offset === "number"
  );
}

export function isPOSApiError(value: unknown): value is POSApiError {
  return (
    isRecord(value) &&
    typeof value.code === "string" &&
    typeof value.message === "string" &&
    isRecord(value.fields) &&
    Object.values(value.fields).every((fieldError) => typeof fieldError === "string")
  );
}

export function parsePOSApiError(value: unknown): POSApiError {
  if (!isRecord(value) || !isPOSApiError(value.error)) {
    return DEFAULT_POS_ERROR;
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

function normalizeText(value: string) {
  return value.trim();
}

function optionalText(value: string | null | undefined) {
  const normalized = normalizeText(value ?? "");
  return normalized ? normalized : null;
}

export function normalizeDecimalInput(value: string) {
  return normalizeText(value).replace(",", ".");
}

export function normalizePOSLine(line: POSLineInput): POSLineInput {
  return {
    product_id: normalizeText(line.product_id),
    quantity: normalizeDecimalInput(line.quantity),
    discount_percent: normalizeDecimalInput(line.discount_percent || "0"),
  };
}

export function toShiftOpenPayload(values: ShiftOpenPayload): ShiftOpenPayload {
  return {
    register_id: normalizeText(values.register_id),
    opening_cash: normalizeDecimalInput(values.opening_cash),
  };
}

export function toShiftClosePayload(values: ShiftClosePayload): ShiftClosePayload {
  return {
    actual_cash: normalizeDecimalInput(values.actual_cash),
    expected_updated_at: values.expected_updated_at,
    reason: optionalText(values.reason),
  };
}

export function toHeldReceiptCreatePayload(
  values: HeldReceiptCreatePayload,
): HeldReceiptCreatePayload {
  return {
    shift_id: normalizeText(values.shift_id),
    label: optionalText(values.label),
    lines: values.lines.map(normalizePOSLine),
  };
}

export function toHeldReceiptUpdatePayload(
  values: HeldReceiptUpdatePayload,
): HeldReceiptUpdatePayload {
  return {
    expected_updated_at: values.expected_updated_at,
    label: optionalText(values.label),
    lines: values.lines?.map(normalizePOSLine),
  };
}

export function toSaleCreatePayload(values: SaleCreatePayload): SaleCreatePayload {
  return {
    shift_id: normalizeText(values.shift_id),
    held_receipt_id: optionalText(values.held_receipt_id),
    lines: values.lines.map(normalizePOSLine),
    cash_received: normalizeDecimalInput(values.cash_received),
  };
}

function normalizeReturnLine(line: ReturnLineInput): ReturnLineInput {
  return {
    line_id: normalizeText(line.line_id),
    quantity: normalizeDecimalInput(line.quantity),
  };
}

export function toReturnCreatePayload(values: ReturnCreatePayload): ReturnCreatePayload {
  return {
    sale_id: normalizeText(values.sale_id),
    register_id: normalizeText(values.register_id),
    shift_id: normalizeText(values.shift_id),
    refund_method: "cash",
    reason: values.reason,
    comment: optionalText(values.comment),
    lines: values.lines.map(normalizeReturnLine),
  };
}

export function toReturnCancelPayload(values: ReturnCancelPayload): ReturnCancelPayload {
  return {
    reason: values.reason,
    comment: optionalText(values.comment),
  };
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function toNetworkError(): POSApiError {
  return {
    code: "NETWORK_ERROR",
    message:
      "Не удалось связаться с веб-приложением. Проверьте подключение и попробуйте ещё раз.",
    fields: {},
  };
}

function shouldRedirectToLogin(statusCode: number, error: POSApiError) {
  return statusCode === 401 || error.code === "UNAUTHORIZED" || error.code === "AUTH_REQUIRED";
}

function redirectToLoginIfNeeded(statusCode: number, error: POSApiError) {
  if (typeof window !== "undefined" && shouldRedirectToLogin(statusCode, error)) {
    window.setTimeout(() => window.location.assign("/login"), 900);
  }
}

function appendStringParam(params: URLSearchParams, key: string, value?: string | null) {
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

export async function getPOSOptions(): Promise<POSOptionsClientResult> {
  return getPOSResource(
    "/api/pos/options",
    isPOSOptions,
    "API вернул неожиданный формат справочников кассы.",
  );
}

export async function listPOSProducts(
  params: {
    registerId: string;
    q?: string;
    barcode?: string;
    limit?: number;
    offset?: number;
  },
): Promise<POSProductListClientResult> {
  const searchParams = new URLSearchParams();

  appendStringParam(searchParams, "register_id", params.registerId);
  appendStringParam(searchParams, "q", params.q);
  appendStringParam(searchParams, "barcode", params.barcode);
  appendNumberParam(searchParams, "limit", params.limit);
  appendNumberParam(searchParams, "offset", params.offset);

  return getPOSResource(
    `/api/pos/products?${searchParams.toString()}`,
    isPOSProductList,
    "API вернул неожиданный формат товаров кассы.",
  );
}

export async function getCurrentShift(registerId: string): Promise<ShiftClientResult> {
  const params = new URLSearchParams();
  appendStringParam(params, "register_id", registerId);

  return getPOSResource(
    `/api/pos/shifts/current?${params.toString()}`,
    isShift,
    "API вернул неожиданный формат текущей смены.",
  );
}

export async function openShift(
  values: ShiftOpenPayload,
  idempotencyKey: string,
): Promise<ShiftClientResult> {
  return mutatePOSResource(
    "/api/pos/shifts",
    {
      method: "POST",
      body: JSON.stringify(toShiftOpenPayload(values)),
      idempotencyKey,
    },
    isShift,
    "API вернул неожиданный формат открытой смены.",
  );
}

export async function closeShift(
  shiftId: string,
  values: ShiftClosePayload,
  idempotencyKey: string,
): Promise<ShiftClientResult> {
  return mutatePOSResource(
    `/api/pos/shifts/${encodeURIComponent(shiftId)}/close`,
    {
      method: "POST",
      body: JSON.stringify(toShiftClosePayload(values)),
      idempotencyKey,
    },
    isShift,
    "API вернул неожиданный формат закрытой смены.",
  );
}

export async function listHeldReceipts(
  params: {
    shiftId: string;
    limit?: number;
    offset?: number;
  },
): Promise<HeldReceiptListClientResult> {
  const searchParams = new URLSearchParams();

  appendStringParam(searchParams, "shift_id", params.shiftId);
  appendNumberParam(searchParams, "limit", params.limit);
  appendNumberParam(searchParams, "offset", params.offset);

  return getPOSResource(
    `/api/pos/held-receipts?${searchParams.toString()}`,
    isHeldReceiptList,
    "API вернул неожиданный формат отложенных чеков.",
  );
}

export async function createHeldReceipt(
  values: HeldReceiptCreatePayload,
  idempotencyKey: string,
): Promise<HeldReceiptClientResult> {
  return mutatePOSResource(
    "/api/pos/held-receipts",
    {
      method: "POST",
      body: JSON.stringify(toHeldReceiptCreatePayload(values)),
      idempotencyKey,
    },
    isHeldReceipt,
    "API вернул неожиданный формат отложенного чека.",
  );
}

export async function getHeldReceipt(heldId: string): Promise<HeldReceiptClientResult> {
  return getPOSResource(
    `/api/pos/held-receipts/${encodeURIComponent(heldId)}`,
    isHeldReceipt,
    "API вернул неожиданный формат отложенного чека.",
  );
}

export async function updateHeldReceipt(
  heldId: string,
  values: HeldReceiptUpdatePayload,
): Promise<HeldReceiptClientResult> {
  return mutatePOSResource(
    `/api/pos/held-receipts/${encodeURIComponent(heldId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(toHeldReceiptUpdatePayload(values)),
    },
    isHeldReceipt,
    "API вернул неожиданный формат обновлённого отложенного чека.",
  );
}

export async function deleteHeldReceipt(heldId: string): Promise<HeldReceiptDeleteResult> {
  return mutatePOSResource(
    `/api/pos/held-receipts/${encodeURIComponent(heldId)}`,
    {
      method: "DELETE",
    },
    (value): value is null => value === null,
    "API вернул неожиданный ответ удаления отложенного чека.",
  );
}

export async function createSale(
  values: SaleCreatePayload,
  idempotencyKey: string,
): Promise<SaleClientResult> {
  return mutatePOSResource(
    "/api/pos/sales",
    {
      method: "POST",
      body: JSON.stringify(toSaleCreatePayload(values)),
      idempotencyKey,
    },
    isSale,
    "API вернул неожиданный формат продажи.",
  );
}

export async function listSales(
  params: {
    q?: string;
    registerId?: string;
    cashierEmail?: string;
    dateFrom?: string;
    dateTo?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<SaleListClientResult> {
  const searchParams = new URLSearchParams();

  appendStringParam(searchParams, "q", params.q);
  appendStringParam(searchParams, "register_id", params.registerId);
  appendStringParam(searchParams, "cashier_email", params.cashierEmail);
  appendStringParam(searchParams, "date_from", params.dateFrom);
  appendStringParam(searchParams, "date_to", params.dateTo);
  appendNumberParam(searchParams, "limit", params.limit);
  appendNumberParam(searchParams, "offset", params.offset);

  const queryString = searchParams.toString();
  const url = queryString ? `/api/pos/sales?${queryString}` : "/api/pos/sales";

  return getPOSResource(url, isSaleList, "API вернул неожиданный формат истории продаж.");
}

export async function getSale(saleId: string): Promise<SaleClientResult> {
  return getPOSResource(
    `/api/pos/sales/${encodeURIComponent(saleId)}`,
    isSale,
    "API вернул неожиданный формат продажи.",
  );
}

export async function getReturnOptions(saleId: string): Promise<ReturnOptionsClientResult> {
  return getPOSResource(
    `/api/pos/sales/${encodeURIComponent(saleId)}/return-options`,
    isReturnOptions,
    "API вернул неожиданный формат доступных позиций возврата.",
  );
}

export async function createReturn(
  values: ReturnCreatePayload,
  idempotencyKey: string,
): Promise<ReturnClientResult> {
  return mutatePOSResource(
    "/api/pos/returns",
    {
      method: "POST",
      body: JSON.stringify(toReturnCreatePayload(values)),
      idempotencyKey,
    },
    isPOSReturn,
    "API вернул неожиданный формат возврата.",
  );
}

export async function listReturns(
  params: {
    q?: string;
    saleId?: string;
    registerId?: string;
    cashierEmail?: string;
    dateFrom?: string;
    dateTo?: string;
    state?: ReturnState | "";
    limit?: number;
    offset?: number;
  } = {},
): Promise<ReturnListClientResult> {
  const searchParams = new URLSearchParams();

  appendStringParam(searchParams, "q", params.q);
  appendStringParam(searchParams, "sale_id", params.saleId);
  appendStringParam(searchParams, "register_id", params.registerId);
  appendStringParam(searchParams, "cashier_email", params.cashierEmail);
  appendStringParam(searchParams, "date_from", params.dateFrom);
  appendStringParam(searchParams, "date_to", params.dateTo);
  appendStringParam(searchParams, "state", params.state);
  appendNumberParam(searchParams, "limit", params.limit);
  appendNumberParam(searchParams, "offset", params.offset);

  const queryString = searchParams.toString();
  const url = queryString ? `/api/pos/returns?${queryString}` : "/api/pos/returns";

  return getPOSResource(url, isReturnList, "API вернул неожиданный формат истории возвратов.");
}

export async function getReturn(returnId: string): Promise<ReturnClientResult> {
  return getPOSResource(
    `/api/pos/returns/${encodeURIComponent(returnId)}`,
    isPOSReturn,
    "API вернул неожиданный формат возврата.",
  );
}

export async function cancelReturn(
  returnId: string,
  values: ReturnCancelPayload,
  idempotencyKey: string,
): Promise<ReturnClientResult> {
  return mutatePOSResource(
    `/api/pos/returns/${encodeURIComponent(returnId)}/cancel`,
    {
      method: "POST",
      body: JSON.stringify(toReturnCancelPayload(values)),
      idempotencyKey,
    },
    isPOSReturn,
    "API вернул неожиданный формат отмены возврата.",
  );
}

async function getPOSResource<T>(
  url: string,
  validate: (value: unknown) => value is T,
  invalidResponseMessage: string,
): Promise<ClientSuccessResult<T> | ClientErrorResult> {
  try {
    const response = await fetch(url, { cache: "no-store" });
    const payload = await readJson(response);

    if (!response.ok) {
      const error = parsePOSApiError(payload);
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

async function mutatePOSResource<T>(
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
    const headers: HeadersInit = {};

    if (init.body) {
      headers["Content-Type"] = "application/json";
    }
    if (init.idempotencyKey) {
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
      const error = parsePOSApiError(payload);
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
