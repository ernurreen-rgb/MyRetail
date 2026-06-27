import type { NextResponse } from "next/server";
import { cookies } from "next/headers";

import {
  AUTH_COOKIE_NAMES,
  type AuthSession,
  type LoginResponse,
} from "@/lib/auth";

const MIN_SESSION_SECONDS = 60;
const MAX_SESSION_SECONDS = 60 * 60 * 8;

function clampSessionMaxAge(expiresIn: number) {
  if (!Number.isFinite(expiresIn)) {
    return MIN_SESSION_SECONDS;
  }

  return Math.max(MIN_SESSION_SECONDS, Math.min(Math.floor(expiresIn), MAX_SESSION_SECONDS));
}

function authCookieOptions(maxAge: number) {
  return {
    httpOnly: true,
    maxAge,
    path: "/",
    sameSite: "lax" as const,
    secure: process.env.NODE_ENV === "production",
  };
}

export async function getAuthSession(): Promise<AuthSession | null> {
  const cookieStore = await cookies();
  const accessToken = cookieStore.get(AUTH_COOKIE_NAMES.accessToken)?.value;
  const tenant = cookieStore.get(AUTH_COOKIE_NAMES.tenant)?.value;

  if (!accessToken || !tenant) {
    return null;
  }

  return {
    accessToken,
    tenant,
  };
}

export function setAuthCookies(response: NextResponse, login: LoginResponse) {
  const maxAge = clampSessionMaxAge(login.expires_in);

  response.cookies.set(
    AUTH_COOKIE_NAMES.accessToken,
    login.access_token,
    authCookieOptions(maxAge),
  );
  response.cookies.set(AUTH_COOKIE_NAMES.tenant, login.tenant, authCookieOptions(maxAge));
}

export function clearAuthCookies(response: NextResponse) {
  response.cookies.set(AUTH_COOKIE_NAMES.accessToken, "", authCookieOptions(0));
  response.cookies.set(AUTH_COOKIE_NAMES.tenant, "", authCookieOptions(0));
}