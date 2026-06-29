import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { POST as login } from "@/app/api/auth/login/route";
import { POST as logout } from "@/app/api/auth/logout/route";
import { LoginForm } from "@/app/login/login-form";

const LOGIN_URL = "http://localhost:3000/api/auth/login";
const LOGOUT_URL = "http://localhost:3000/api/auth/logout";

function loginRequest({
  contentType = "application/json",
  fetchSite,
  origin = "http://localhost:3000",
}: {
  contentType?: string;
  fetchSite?: "same-origin" | "cross-site";
  origin?: string;
} = {}) {
  return new Request(LOGIN_URL, {
    method: "POST",
    headers: {
      "Content-Type": contentType,
      Origin: origin,
      "Sec-Fetch-Site":
        fetchSite ?? (origin === "http://localhost:3000" ? "same-origin" : "cross-site"),
    },
    body:
      contentType === "application/json"
        ? JSON.stringify({
            tenant: "myretail",
            email: "owner@example.com",
            password: "secret",
          })
        : "tenant=myretail&email=owner%40example.com&password=secret",
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.MYRETAIL_API_URL;
  delete process.env.MYRETAIL_WEB_ORIGIN;
});

describe("POST /api/auth/login", () => {
  it("rejects cross-origin login before forwarding credentials", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const response = await login(loginRequest({ origin: "https://attacker.example" }));

    expect(response.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a cross-origin HTML form fallback before forwarding credentials", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const response = await login(
      loginRequest({
        contentType: "application/x-www-form-urlencoded",
        origin: "https://attacker.example",
      }),
    );

    expect(response.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("accepts a same-origin HTML form fallback and redirects after login", async () => {
    process.env.MYRETAIL_API_URL = "http://api.test";
    process.env.MYRETAIL_WEB_ORIGIN = "http://127.0.0.1:3000";
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        access_token: "signed-token",
        token_type: "bearer",
        expires_in: 3_600,
        tenant: "myretail",
        user: {
          email: "owner@example.com",
          full_name: "Owner",
          roles: ["Owner"],
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await login(
      loginRequest({
        contentType: "application/x-www-form-urlencoded",
        fetchSite: "same-origin",
        origin: "http://127.0.0.1:3000",
      }),
    );
    const setCookie = response.headers.get("set-cookie") ?? "";

    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("http://127.0.0.1:3000/");
    expect(setCookie).toContain("myretail_access_token=signed-token");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/auth/login",
      expect.objectContaining({
        method: "POST",
      }),
    );
  });

  it("returns an HTML form fallback to the login page after invalid credentials", async () => {
    process.env.MYRETAIL_API_URL = "http://api.test";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({ detail: "Invalid email or password" }, { status: 401 }),
      ),
    );

    const response = await login(
      loginRequest({ contentType: "application/x-www-form-urlencoded" }),
    );

    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe(
      "http://localhost:3000/login?error=invalid_credentials",
    );
  });

  it("accepts localhost as a development alias for a configured 127.0.0.1 origin", async () => {
    process.env.MYRETAIL_API_URL = "http://api.test";
    process.env.MYRETAIL_WEB_ORIGIN = "http://127.0.0.1:3000";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          access_token: "signed-token",
          token_type: "bearer",
          expires_in: 3_600,
          tenant: "myretail",
          user: {
            email: "owner@example.com",
            full_name: "Owner",
            roles: ["Owner"],
          },
        }),
      ),
    );

    const response = await login(loginRequest());

    expect(response.status).toBe(200);
    expect(response.headers.get("set-cookie")).toContain(
      "myretail_access_token=signed-token",
    );
  });

  it("sets hardened cookies after a same-origin JSON login", async () => {
    process.env.MYRETAIL_API_URL = "http://api.test";
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        access_token: "signed-token",
        token_type: "bearer",
        expires_in: 3_600,
        tenant: "myretail",
        user: {
          email: "owner@example.com",
          full_name: "Owner",
          roles: ["Owner"],
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await login(loginRequest());
    const setCookie = response.headers.get("set-cookie") ?? "";

    expect(response.status).toBe(200);
    expect(setCookie).toContain("myretail_access_token=signed-token");
    expect(setCookie).toContain("HttpOnly");
    expect(setCookie).toContain("SameSite=lax");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/auth/login",
      expect.objectContaining({
        method: "POST",
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("returns 504 when the backend login times out", async () => {
    const timeoutError = Object.assign(new Error("timed out"), { name: "TimeoutError" });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(timeoutError));

    const response = await login(loginRequest());

    expect(response.status).toBe(504);
    await expect(response.json()).resolves.toEqual({
      message: "Backend не ответил вовремя. Попробуйте ещё раз.",
    });
  });

  it("returns a clear message when login attempts are rate limited", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          { detail: "Too many login attempts" },
          { status: 429, headers: { "Retry-After": "120" } },
        ),
      ),
    );

    const response = await login(loginRequest());

    expect(response.status).toBe(429);
    await expect(response.json()).resolves.toEqual({
      message: "Слишком много попыток входа. Подождите несколько минут и попробуйте снова.",
    });
  });
});

describe("POST /api/auth/logout", () => {
  it("rejects cross-origin logout", async () => {
    const response = await logout(
      new Request(LOGOUT_URL, {
        method: "POST",
        headers: {
          Accept: "text/html",
          Origin: "https://attacker.example",
          "Sec-Fetch-Site": "cross-site",
        },
      }),
    );

    expect(response.status).toBe(403);
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("clears cookies for a same-origin logout", async () => {
    const response = await logout(
      new Request(LOGOUT_URL, {
        method: "POST",
        headers: {
          Accept: "text/html",
          Origin: "http://localhost:3000",
          "Sec-Fetch-Site": "same-origin",
        },
      }),
    );
    const setCookie = response.headers.get("set-cookie") ?? "";

    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("http://localhost:3000/login");
    expect(setCookie).toContain("myretail_access_token=");
    expect(setCookie).toContain("Max-Age=0");
  });

  it("keeps the browser host when localhost is used as a development alias", async () => {
    process.env.MYRETAIL_WEB_ORIGIN = "http://127.0.0.1:3000";

    const response = await logout(
      new Request(LOGOUT_URL, {
        method: "POST",
        headers: {
          Accept: "text/html",
          Origin: "http://localhost:3000",
          "Sec-Fetch-Site": "same-origin",
        },
      }),
    );

    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("http://localhost:3000/login");
  });
});

describe("LoginForm", () => {
  it("uses POST as the safe fallback when client JavaScript is unavailable", () => {
    const html = renderToStaticMarkup(<LoginForm />);

    expect(html).toContain('action="/api/auth/login"');
    expect(html).toContain('method="post"');
  });
});
