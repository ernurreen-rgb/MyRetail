import { NextResponse } from "next/server";

import { DEFAULT_TENANT, isLoginResponse, type LoginFormValues } from "@/lib/auth";
import { getApiBaseUrl } from "@/lib/config";
import { setAuthCookies } from "@/lib/session";

export const dynamic = "force-dynamic";

type ValidatedLoginValues = LoginFormValues;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function getStringField(value: FormDataEntryValue | null) {
  return typeof value === "string" ? value : "";
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

async function readLoginValues(request: Request): Promise<ValidatedLoginValues | null> {
  const contentType = request.headers.get("content-type") ?? "";

  if (contentType.includes("application/json")) {
    try {
      return normalizeLoginValues(await request.json());
    } catch {
      return null;
    }
  }

  if (
    contentType.includes("application/x-www-form-urlencoded") ||
    contentType.includes("multipart/form-data")
  ) {
    const formData = await request.formData();

    return normalizeLoginValues({
      tenant: getStringField(formData.get("tenant")),
      email: getStringField(formData.get("email")),
      password: getStringField(formData.get("password")),
    });
  }

  return null;
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

  return "Не удалось войти. Попробуйте ещё раз.";
}

export async function POST(request: Request) {
  const values = await readLoginValues(request);

  if (!values) {
    return NextResponse.json(
      { message: "Укажите tenant, email и пароль." },
      { status: 400 },
    );
  }

  const apiBaseUrl = getApiBaseUrl().replace(/\/+$/, "");
  let apiResponse: Response;

  try {
    apiResponse = await fetch(`${apiBaseUrl}/auth/login`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(values),
      cache: "no-store",
    });
  } catch {
    return NextResponse.json(
      { message: "Backend недоступен. Проверьте, что MyRetail API запущен." },
      { status: 503 },
    );
  }

  const payload = await readJson(apiResponse);

  if (!apiResponse.ok) {
    return NextResponse.json(
      { message: loginErrorMessage(apiResponse.status) },
      { status: apiResponse.status >= 400 && apiResponse.status <= 599 ? apiResponse.status : 502 },
    );
  }

  if (!isLoginResponse(payload)) {
    return NextResponse.json(
      { message: "Backend вернул неожиданный формат ответа входа." },
      { status: 502 },
    );
  }

  const response = NextResponse.json({ ok: true });
  setAuthCookies(response, payload);

  return response;
}