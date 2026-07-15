import { NextResponse } from "next/server";

import { DEFAULT_TENANT, isLoginResponse, type LoginFormValues } from "@/lib/auth";
import { buildApiUrl } from "@/lib/config";
import {
  getExpectedOrigin,
  getVerifiedRequestOrigin,
  isSameOriginMutation,
} from "@/lib/request-security";
import { setAuthCookies } from "@/lib/session";

export const dynamic = "force-dynamic";
const LOGIN_TIMEOUT_MS = 10_000;

type ValidatedLoginValues = LoginFormValues;
type LoginRequestFormat = "json" | "form" | "unsupported";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function normalizeLoginValues(value: unknown): ValidatedLoginValues | null {
  if (!isRecord(value)) {
    return null;
  }

  const tenant = typeof value.tenant === "string" ? value.tenant.trim() : "";
  const email = typeof value.email === "string" ? value.email.trim() : "";
  const password = typeof value.password === "string" ? value.password : "";

  if (!email || !password) {
    return null;
  }

  return {
    tenant: tenant || DEFAULT_TENANT,
    email,
    password,
  };
}

function getLoginRequestFormat(request: Request): LoginRequestFormat {
  const contentType = request.headers.get("content-type") ?? "";

  if (contentType.includes("application/json")) {
    return "json";
  }

  if (contentType.includes("application/x-www-form-urlencoded")) {
    return "form";
  }

  return "unsupported";
}

async function readLoginValues(
  request: Request,
  format: Exclude<LoginRequestFormat, "unsupported">,
): Promise<ValidatedLoginValues | null> {
  try {
    if (format === "json") {
      return normalizeLoginValues(await request.json());
    }

    const formData = await request.formData();
    return normalizeLoginValues({
      tenant: formData.get("tenant"),
      email: formData.get("email"),
      password: formData.get("password"),
    });
  } catch {
    return null;
  }
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function loginErrorMessage(status: number) {
  if (status === 401 || status === 403) {
    return "Неверный email или пароль.";
  }

  if (status === 404) {
    return "Тенант не найден. Проверьте код компании.";
  }

  if (status === 400 || status === 422) {
    return "Проверьте tenant, email и пароль.";
  }

  if (status === 503 || status === 504) {
    return "Backend временно недоступен. Попробуйте позже.";
  }
  if (status === 429) {
    return "Слишком много попыток входа. Подождите несколько минут и попробуйте снова.";
  }

  return "Не удалось войти. Попробуйте ещё раз.";
}

function isTimeoutError(error: unknown) {
  return error instanceof Error && ["AbortError", "TimeoutError"].includes(error.name);
}

function loginErrorCode(status: number) {
  if (status === 401 || status === 403) {
    return "invalid_credentials";
  }

  if (status === 404) {
    return "tenant_not_found";
  }

  if (status === 400 || status === 422) {
    return "invalid_request";
  }

  if (status === 429) {
    return "rate_limited";
  }

  return "unavailable";
}

function formRedirect(request: Request, error?: string) {
  const redirectOrigin =
    getVerifiedRequestOrigin(request) ?? getExpectedOrigin(request) ?? request.url;
  const url = new URL(error ? "/login" : "/", redirectOrigin);
  if (error) {
    url.searchParams.set("error", error);
  }

  return NextResponse.redirect(url, 303);
}

function loginFailureResponse(
  request: Request,
  format: Exclude<LoginRequestFormat, "unsupported">,
  status: number,
  message: string,
) {
  if (format === "form") {
    return formRedirect(request, loginErrorCode(status));
  }

  return NextResponse.json({ message }, { status });
}

export async function POST(request: Request) {
  if (!isSameOriginMutation(request)) {
    return NextResponse.json(
      { message: "Запрос входа отклонён проверкой безопасности." },
      { status: 403 },
    );
  }

  const format = getLoginRequestFormat(request);
  if (format === "unsupported") {
    return NextResponse.json(
      { message: "Поддерживаются JSON и стандартная HTML-форма." },
      { status: 415 },
    );
  }

  const values = await readLoginValues(request, format);

  if (!values) {
    return loginFailureResponse(
      request,
      format,
      400,
      "Укажите tenant, email и пароль.",
    );
  }

  let apiResponse: Response;

  try {
    apiResponse = await fetch(buildApiUrl("/auth/login"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(values),
      cache: "no-store",
      signal: AbortSignal.timeout(LOGIN_TIMEOUT_MS),
    });
  } catch (error) {
    const timedOut = isTimeoutError(error);
    return loginFailureResponse(
      request,
      format,
      timedOut ? 504 : 503,
      timedOut
        ? "Backend не ответил вовремя. Попробуйте ещё раз."
        : "Backend недоступен. Проверьте, что MyRetail API запущен.",
    );
  }

  const payload = await readJson(apiResponse);

  if (!apiResponse.ok) {
    const responseStatus =
      apiResponse.status >= 400 && apiResponse.status <= 599 ? apiResponse.status : 502;
    return loginFailureResponse(
      request,
      format,
      responseStatus,
      loginErrorMessage(apiResponse.status),
    );
  }

  if (!isLoginResponse(payload)) {
    return loginFailureResponse(
      request,
      format,
      502,
      "Backend вернул неожиданный формат ответа входа.",
    );
  }

  const response =
    format === "form" ? formRedirect(request) : NextResponse.json({ ok: true });
  setAuthCookies(response, payload);

  return response;
}
