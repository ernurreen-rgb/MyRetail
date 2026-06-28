export type Product = {
  id: string;
  sku: string;
  name: string;
  barcode: string | null;
  category: string;
  brand: string | null;
  unit: string;
  sale_price: string;
  purchase_price: string | null;
  currency: string;
  description: string | null;
  image_url: string | null;
  is_active: boolean;
};

export type ProductsResponse = {
  items: Product[];
  count: number;
  limit: number;
  offset: number;
};

export type ProductOption = {
  id: string;
  name: string;
};

export type ProductOptions = {
  categories: ProductOption[];
  brands: ProductOption[];
  units: ProductOption[];
};

export type ProductFormValues = {
  sku: string;
  name: string;
  barcode: string;
  category: string;
  brand: string;
  unit: string;
  sale_price: string;
  purchase_price: string;
  description: string;
};

export type ProductCreatePayload = {
  sku: string;
  name: string;
  barcode: string | null;
  category: string;
  brand: string | null;
  unit: string;
  sale_price: string;
  purchase_price: string | null;
  description: string | null;
};

export type ProductUpdatePayload = Omit<ProductCreatePayload, "sku">;

export type ProductApiError = {
  code: string;
  message: string;
  fields: Record<string, string>;
};

export type ProductsState =
  | {
      status: "ready";
      data: ProductsResponse;
    }
  | {
      status: "error";
      message: string;
    };

export type ProductsClientResult =
  | {
      status: "success";
      data: ProductsResponse;
    }
  | {
      status: "error";
      statusCode: number;
      error: ProductApiError;
    };

export type ProductOptionsClientResult =
  | {
      status: "success";
      data: ProductOptions;
    }
  | {
      status: "error";
      statusCode: number;
      error: ProductApiError;
    };

export type ProductMutationResult =
  | {
      status: "success";
      data: Product | null;
    }
  | {
      status: "error";
      statusCode: number;
      error: ProductApiError;
    };

const DEFAULT_PRODUCT_ERROR: ProductApiError = {
  code: "REQUEST_ERROR",
  message: "Запрос не может быть обработан. Попробуйте ещё раз.",
  fields: {},
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isNullableString(value: unknown): value is string | null {
  return typeof value === "string" || value === null;
}

function isProductOption(value: unknown): value is ProductOption {
  return isRecord(value) && typeof value.id === "string" && typeof value.name === "string";
}

export function isProduct(value: unknown): value is Product {
  if (!isRecord(value)) {
    return false;
  }

  return (
    typeof value.id === "string" &&
    typeof value.sku === "string" &&
    typeof value.name === "string" &&
    isNullableString(value.barcode) &&
    typeof value.category === "string" &&
    isNullableString(value.brand) &&
    typeof value.unit === "string" &&
    typeof value.sale_price === "string" &&
    isNullableString(value.purchase_price) &&
    typeof value.currency === "string" &&
    isNullableString(value.description) &&
    isNullableString(value.image_url) &&
    typeof value.is_active === "boolean"
  );
}

export function isProductsResponse(value: unknown): value is ProductsResponse {
  if (!isRecord(value)) {
    return false;
  }

  return (
    Array.isArray(value.items) &&
    value.items.every(isProduct) &&
    typeof value.count === "number" &&
    typeof value.limit === "number" &&
    typeof value.offset === "number"
  );
}

export function isProductOptions(value: unknown): value is ProductOptions {
  if (!isRecord(value)) {
    return false;
  }

  return (
    Array.isArray(value.categories) &&
    value.categories.every(isProductOption) &&
    Array.isArray(value.brands) &&
    value.brands.every(isProductOption) &&
    Array.isArray(value.units) &&
    value.units.every(isProductOption)
  );
}

export function isProductApiError(value: unknown): value is ProductApiError {
  if (!isRecord(value)) {
    return false;
  }

  return (
    typeof value.code === "string" &&
    typeof value.message === "string" &&
    isRecord(value.fields) &&
    Object.values(value.fields).every((fieldError) => typeof fieldError === "string")
  );
}

export function parseProductApiError(value: unknown): ProductApiError {
  if (!isRecord(value) || !isProductApiError(value.error)) {
    return DEFAULT_PRODUCT_ERROR;
  }

  return value.error;
}

export function productToFormValues(product: Product): ProductFormValues {
  return {
    sku: product.sku,
    name: product.name,
    barcode: product.barcode ?? "",
    category: product.category,
    brand: product.brand ?? "",
    unit: product.unit,
    sale_price: product.sale_price,
    purchase_price: product.purchase_price ?? "",
    description: product.description ?? "",
  };
}

export function emptyProductFormValues(options?: ProductOptions): ProductFormValues {
  return {
    sku: "",
    name: "",
    barcode: "",
    category: options?.categories[0]?.id ?? "",
    brand: "",
    unit: options?.units[0]?.id ?? "",
    sale_price: "",
    purchase_price: "",
    description: "",
  };
}

function normalizeText(value: string) {
  return value.trim();
}

function optionalText(value: string) {
  const normalized = normalizeText(value);
  return normalized ? normalized : null;
}

function normalizeMoneyInput(value: string) {
  const normalized = normalizeText(value).replace(",", ".");
  if (!normalized) {
    return normalized;
  }

  const amount = Number(normalized);
  return Number.isFinite(amount) ? amount.toFixed(2) : normalized;
}

export function toProductCreatePayload(values: ProductFormValues): ProductCreatePayload {
  return {
    sku: normalizeText(values.sku),
    name: normalizeText(values.name),
    barcode: optionalText(values.barcode),
    category: normalizeText(values.category),
    brand: optionalText(values.brand),
    unit: normalizeText(values.unit),
    sale_price: normalizeMoneyInput(values.sale_price),
    purchase_price: optionalText(normalizeMoneyInput(values.purchase_price)),
    description: optionalText(values.description),
  };
}

export function toProductUpdatePayload(values: ProductFormValues): ProductUpdatePayload {
  return {
    name: normalizeText(values.name),
    barcode: optionalText(values.barcode),
    category: normalizeText(values.category),
    brand: optionalText(values.brand),
    unit: normalizeText(values.unit),
    sale_price: normalizeMoneyInput(values.sale_price),
    purchase_price: optionalText(normalizeMoneyInput(values.purchase_price)),
    description: optionalText(values.description),
  };
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function toNetworkError(): ProductApiError {
  return {
    code: "NETWORK_ERROR",
    message: "Не удалось связаться с веб-приложением. Проверьте подключение и попробуйте ещё раз.",
    fields: {},
  };
}

function shouldRedirectToLogin(statusCode: number, error: ProductApiError) {
  return statusCode === 401 || error.code === "AUTH_REQUIRED";
}

function redirectToLoginIfNeeded(statusCode: number, error: ProductApiError) {
  if (typeof window !== "undefined" && shouldRedirectToLogin(statusCode, error)) {
    window.setTimeout(() => window.location.assign("/login"), 900);
  }
}

export async function listProducts(params: {
  q?: string;
  includeArchived?: boolean;
  limit?: number;
  offset?: number;
} = {}): Promise<ProductsClientResult> {
  const searchParams = new URLSearchParams();
  const query = params.q?.trim();

  if (query) {
    searchParams.set("q", query);
  }
  if (params.includeArchived) {
    searchParams.set("include_archived", "true");
  }
  if (typeof params.limit === "number") {
    searchParams.set("limit", String(params.limit));
  }
  if (typeof params.offset === "number") {
    searchParams.set("offset", String(params.offset));
  }

  const queryString = searchParams.toString();
  const url = queryString ? `/api/products?${queryString}` : "/api/products";

  try {
    const response = await fetch(url, { cache: "no-store" });
    const payload = await readJson(response);

    if (!response.ok) {
      const error = parseProductApiError(payload);
      redirectToLoginIfNeeded(response.status, error);
      return { status: "error", statusCode: response.status, error };
    }

    if (!isProductsResponse(payload)) {
      return {
        status: "error",
        statusCode: 502,
        error: {
          code: "INVALID_RESPONSE",
          message: "API вернул неожиданный формат списка товаров.",
          fields: {},
        },
      };
    }

    return { status: "success", data: payload };
  } catch {
    return { status: "error", statusCode: 0, error: toNetworkError() };
  }
}

export async function getProductOptions(): Promise<ProductOptionsClientResult> {
  try {
    const response = await fetch("/api/products/options", { cache: "no-store" });
    const payload = await readJson(response);

    if (!response.ok) {
      const error = parseProductApiError(payload);
      redirectToLoginIfNeeded(response.status, error);
      return { status: "error", statusCode: response.status, error };
    }

    if (!isProductOptions(payload)) {
      return {
        status: "error",
        statusCode: 502,
        error: {
          code: "INVALID_RESPONSE",
          message: "API вернул неожиданный формат справочников товаров.",
          fields: {},
        },
      };
    }

    return { status: "success", data: payload };
  } catch {
    return { status: "error", statusCode: 0, error: toNetworkError() };
  }
}

export async function createProduct(
  values: ProductFormValues,
): Promise<ProductMutationResult> {
  return mutateProduct("/api/products", {
    method: "POST",
    body: JSON.stringify(toProductCreatePayload(values)),
  });
}

export async function updateProduct(
  productId: string,
  values: ProductFormValues,
): Promise<ProductMutationResult> {
  return mutateProduct(`/api/products/${encodeURIComponent(productId)}`, {
    method: "PATCH",
    body: JSON.stringify(toProductUpdatePayload(values)),
  });
}

export async function archiveProduct(productId: string): Promise<ProductMutationResult> {
  return mutateProduct(`/api/products/${encodeURIComponent(productId)}`, {
    method: "DELETE",
  });
}

async function mutateProduct(
  url: string,
  init: RequestInit,
): Promise<ProductMutationResult> {
  try {
    const response = await fetch(url, {
      ...init,
      cache: "no-store",
      headers: init.body
        ? {
            "Content-Type": "application/json",
          }
        : undefined,
    });

    if (response.status === 204) {
      return { status: "success", data: null };
    }

    const payload = await readJson(response);

    if (!response.ok) {
      const error = parseProductApiError(payload);
      redirectToLoginIfNeeded(response.status, error);
      return { status: "error", statusCode: response.status, error };
    }

    if (!isProduct(payload)) {
      return {
        status: "error",
        statusCode: 502,
        error: {
          code: "INVALID_RESPONSE",
          message: "API вернул неожиданный формат товара.",
          fields: {},
        },
      };
    }

    return { status: "success", data: payload };
  } catch {
    return { status: "error", statusCode: 0, error: toNetworkError() };
  }
}
