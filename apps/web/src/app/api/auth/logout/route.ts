import { NextResponse } from "next/server";

import {
  getExpectedOrigin,
  getVerifiedRequestOrigin,
  isSameOriginMutation,
} from "@/lib/request-security";
import { clearAuthCookies } from "@/lib/session";

export const dynamic = "force-dynamic";

function wantsHtmlRedirect(request: Request) {
  const accept = request.headers.get("accept") ?? "";

  return accept.includes("text/html") && !accept.includes("application/json");
}

export async function POST(request: Request) {
  if (!isSameOriginMutation(request)) {
    return NextResponse.json(
      { message: "Запрос выхода отклонён проверкой безопасности." },
      { status: 403 },
    );
  }

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
