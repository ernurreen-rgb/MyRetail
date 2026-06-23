import { getApiBaseUrl } from "@/lib/config";

export type Product = {
  id: string;
  name: string;
  description: string | null;
  unit: string;
  image_url: string | null;
};

export type ProductsResponse = {
  items: Product[];
  count: number;
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isNullableString(value: unknown): value is string | null {
  return typeof value === "string" || value === null;
}

function isProduct(value: unknown): value is Product {
  if (!isRecord(value)) {
    return false;
  }

  return (
    typeof value.id === "string" &&
    typeof value.name === "string" &&
    isNullableString(value.description) &&
    typeof value.unit === "string" &&
    isNullableString(value.image_url)
  );
}

function isProductsResponse(value: unknown): value is ProductsResponse {
  if (!isRecord(value)) {
    return false;
  }

  return (
    Array.isArray(value.items) &&
    value.items.every(isProduct) &&
    typeof value.count === "number"
  );
}

function toErrorMessage(error: unknown) {
  if (error instanceof Error && error.message) {
    return error.message;
  }

  return "неизвестная ошибка";
}

export async function getProducts(): Promise<ProductsState> {
  const apiBaseUrl = getApiBaseUrl().replace(/\/+$/, "");

  try {
    const response = await fetch(`${apiBaseUrl}/products`, {
      cache: "no-store",
    });

    if (!response.ok) {
      return {
        status: "error",
        message: `API вернул HTTP ${response.status}`,
      };
    }

    const payload: unknown = await response.json();

    if (!isProductsResponse(payload)) {
      return {
        status: "error",
        message: "API вернул неожиданный формат ответа",
      };
    }

    return {
      status: "ready",
      data: payload,
    };
  } catch (error) {
    return {
      status: "error",
      message: `API недоступен: ${toErrorMessage(error)}`,
    };
  }
}
