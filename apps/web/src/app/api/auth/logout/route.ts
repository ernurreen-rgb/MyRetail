import { NextResponse } from "next/server";

import { clearAuthCookies } from "@/lib/session";

export const dynamic = "force-dynamic";

function wantsHtmlRedirect(request: Request) {
  const accept = request.headers.get("accept") ?? "";

  return accept.includes("text/html") && !accept.includes("application/json");
}

export async function POST(request: Request) {
  const response = wantsHtmlRedirect(request)
    ? NextResponse.redirect(new URL("/login", request.url), { status: 303 })
    : NextResponse.json({ ok: true });

  clearAuthCookies(response);

  return response;
}