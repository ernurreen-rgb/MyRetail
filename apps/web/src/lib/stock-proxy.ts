import { NextResponse } from "next/server";

import { getApiBaseUrl } from "@/lib/config";
import { isSameOriginMutation } from "@/lib/request-security";
import { getAuthSession } from "@/lib/session";

const STOCK_API_TIMEOUT_MS = 12_000;
const MUTATION_METHODS = new Set(["POST"]);

type ProxyOptions = {
  endpoint: string;
  request: Request;
  method?: "GET" | "POST";
  body?: unknown;
};

function stockError(code: string, message: string, fields: Record<string, string> = {}) {
  return { error: { code, message, fields } };
}

function isTimeoutError(error: unknown) {
  return error instanceof Error && ["AbortError", "TimeoutError"].includes(error.name);
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

async function readRequestJson(request: Request): Promise<unknown> {
  try {
    return await request.json();
  } catch {
    return null;
  }
}

function buildEndpointUrl(endpoint: string, request: Request) {
  const apiBaseUrl = getApiBaseUrl().replace(/\/+$/, "");
  const url = new URL(`${apiBaseUrl}${endpoint}`);

  for (const [key, value] of new URL(request.url).searchParams) {
    url.searchParams.append(key, value);
  }

  return url;
}

export async function proxyStockRequest({
  endpoint,
  request,
  method = "GET",
  body,
}: ProxyOptions) {
  if (MUTATION_METHODS.has(method) && !isSameOriginMutation(request)) {
    return NextResponse.json(
      stockError("FORBIDDEN", "Запрос отклонён проверкой безопасности."),
      { status: 403 },
    );
  }

  const session = await getAuthSession();

  if (!session) {
    return NextResponse.json(
      stockError("AUTH_REQUIRED", "Нужно войти в систему."),
      { status: 401 },
    );
  }

  const hasJsonBody = method === "POST";
  const idempotencyKey = hasJsonBody ? request.headers.get("Idempotency-Key")?.trim() : null;

  if (hasJsonBody && !idempotencyKey) {
    return NextResponse.json(
      stockError("INVALID_REQUEST", "Для операции склада нужен Idempotency-Key."),
      { status: 400 },
    );
  }

  const requestBody = hasJsonBody ? body ?? (await readRequestJson(request)) : undefined;
  const headers: HeadersInit = {
    Authorization: `Bearer ${session.accessToken}`,
    "X-MyRetail-Tenant": session.tenant,
  };

  if (hasJsonBody) {
    headers["Content-Type"] = "application/json";
    headers["Idempotency-Key"] = idempotencyKey ?? "";
  }

  let apiResponse: Response;

  try {
    apiResponse = await fetch(buildEndpointUrl(endpoint, request), {
      method,
      headers,
      body: hasJsonBody ? JSON.stringify(requestBody) : undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(STOCK_API_TIMEOUT_MS),
    });
  } catch (error) {
    const timedOut = isTimeoutError(error);
    return NextResponse.json(
      stockError(
        timedOut ? "API_TIMEOUT" : "API_UNAVAILABLE",
        timedOut
          ? "MyRetail API не ответил вовремя. Попробуйте ещё раз."
          : "MyRetail API недоступен. Проверьте, что backend запущен.",
      ),
      { status: timedOut ? 504 : 503 },
    );
  }

  if (apiResponse.status === 204) {
    return new Response(null, { status: 204 });
  }

  const payload = await readJson(apiResponse);

  if (payload === null) {
    return NextResponse.json(
      stockError("INVALID_RESPONSE", "MyRetail API вернул пустой или нечитаемый ответ."),
      { status: apiResponse.ok ? 502 : apiResponse.status },
    );
  }

  return NextResponse.json(payload, { status: apiResponse.status });
}
