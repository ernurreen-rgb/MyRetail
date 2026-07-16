import { NextResponse } from "next/server";

import { AUTH_COOKIE_NAMES } from "@/lib/auth";
import { buildApiUrl } from "@/lib/config";
import {
  getExpectedOrigin,
  getVerifiedRequestOrigin,
  isSameOriginMutation,
} from "@/lib/request-security";
import { clearAuthCookies } from "@/lib/session";

export const dynamic = "force-dynamic";
const LOGOUT_TIMEOUT_MS = 10_000;

function wantsHtmlRedirect(request: Request) {
  const accept = request.headers.get("accept") ?? "";

  return accept.includes("text/html") && !accept.includes("application/json");
}

function readCookie(request: Request, name: string) {
  const cookieHeader = request.headers.get("cookie") ?? "";

  for (const item of cookieHeader.split(";")) {
    const [rawName, ...rawValue] = item.trim().split("=");
    if (rawName === name) {
      try {
        return decodeURIComponent(rawValue.join("="));
      } catch {
        return null;
      }
    }
  }

  return null;
}

function completedLogoutResponse(request: Request) {
  const redirectOrigin =
    getVerifiedRequestOrigin(request) ?? getExpectedOrigin(request) ?? request.url;
  const response = wantsHtmlRedirect(request)
    ? NextResponse.redirect(new URL("/login", redirectOrigin), {
        status: 303,
      })
    : NextResponse.json({ ok: true });

  clearAuthCookies(response);
  return response;
}

export async function POST(request: Request) {
  if (!isSameOriginMutation(request)) {
    return NextResponse.json(
      { message: "Запрос выхода отклонён проверкой безопасности." },
      { status: 403 },
    );
  }

  const accessToken = readCookie(request, AUTH_COOKIE_NAMES.accessToken);
  const tenant = readCookie(request, AUTH_COOKIE_NAMES.tenant);
  if (!accessToken || !tenant) {
    return completedLogoutResponse(request);
  }

  let apiResponse: Response;
  try {
    apiResponse = await fetch(buildApiUrl("/auth/logout"), {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "X-MyRetail-Tenant": tenant,
      },
      cache: "no-store",
      signal: AbortSignal.timeout(LOGOUT_TIMEOUT_MS),
    });
  } catch {
    return NextResponse.json(
      { message: "Backend временно недоступен. Сессия сохранена; повторите выход." },
      { status: 503 },
    );
  }

  if (apiResponse.status === 204 || apiResponse.status === 401) {
    return completedLogoutResponse(request);
  }

  return NextResponse.json(
    { message: "Не удалось подтвердить отзыв сессии. Повторите выход." },
    { status: 503 },
  );
}
