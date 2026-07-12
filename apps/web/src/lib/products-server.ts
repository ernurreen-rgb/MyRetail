import type { AuthSession } from "@/lib/auth";
import { getApiBaseUrl } from "@/lib/config";
import {
  type ProductsState,
  isProductsResponse,
  parseProductApiError,
} from "@/lib/products";

function toErrorMessage(error: unknown) {
  if (error instanceof Error && error.message) {
    return error.message;
  }

  return "неизвестная ошибка";
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function toProductsErrorMessage(status: number, payload: unknown) {
  const productError = parseProductApiError(payload);

  if (status === 403) {
    return "Нет доступа к управлению товарами. Раздел доступен только ролям Owner/Admin.";
  }

  if (productError.message) {
    return productError.message;
  }

  if (status === 401 || status === 403) {
    return "Сессия истекла или у пользователя нет доступа к товарам. Выйдите и войдите снова.";
  }

  return `API вернул HTTP ${status}`;
}

export async function getProducts(session: AuthSession): Promise<ProductsState> {
  const apiBaseUrl = getApiBaseUrl().replace(/\/+$/, "");

  try {
    const response = await fetch(`${apiBaseUrl}/products`, {
      cache: "no-store",
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        "X-MyRetail-Tenant": session.tenant,
      },
    });

    const payload: unknown = await readJson(response);

    if (!response.ok) {
      return {
        status: "error",
        message: toProductsErrorMessage(response.status, payload),
      };
    }

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
