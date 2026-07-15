import { NextResponse } from "next/server";

import { buildApiUrl } from "@/lib/config";
import { isSameOriginMutation } from "@/lib/request-security";
import { getAuthSession } from "@/lib/session";

const PURCHASES_API_TIMEOUT_MS = 12_000;
const MUTATION_METHODS = new Set(["POST", "PATCH", "DELETE"]);
const IDEMPOTENT_POST_METHODS = new Set(["POST"]);

type ProxyOptions = {
  endpoint: string;
  request: Request;
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
};

function purchasesError(code: string, message: string, fields: Record<string, string> = {}) {
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

export async function proxyPurchasesRequest({
  endpoint,
  request,
  method = "GET",
  body,
}: ProxyOptions) {
  if (MUTATION_METHODS.has(method) && !isSameOriginMutation(request)) {
    return NextResponse.json(
      purchasesError("FORBIDDEN", "Запрос отклонён проверкой безопасности."),
      { status: 403 },
    );
  }

  const session = await getAuthSession();

  if (!session) {
    return NextResponse.json(
      purchasesError("UNAUTHORIZED", "Нужно войти в систему."),
      { status: 401 },
    );
  }

  const hasJsonBody = method === "POST" || method === "PATCH";
  const idempotencyKey = IDEMPOTENT_POST_METHODS.has(method)
    ? request.headers.get("Idempotency-Key")?.trim()
    : null;

  if (IDEMPOTENT_POST_METHODS.has(method) && !idempotencyKey) {
    return NextResponse.json(
      purchasesError("INVALID_REQUEST", "Для операции закупок нужен Idempotency-Key."),
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
  }
  if (idempotencyKey) {
    headers["Idempotency-Key"] = idempotencyKey;
  }

  let apiResponse: Response;

  try {
    apiResponse = await fetch(buildApiUrl(endpoint, new URL(request.url).searchParams), {
      method,
      headers,
      body: hasJsonBody ? JSON.stringify(requestBody) : undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(PURCHASES_API_TIMEOUT_MS),
    });
  } catch (error) {
    const timedOut = isTimeoutError(error);
    return NextResponse.json(
      purchasesError(
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
      purchasesError("INVALID_RESPONSE", "MyRetail API вернул пустой или нечитаемый ответ."),
      { status: apiResponse.ok ? 502 : apiResponse.status },
    );
  }

  return NextResponse.json(payload, { status: apiResponse.status });
}
