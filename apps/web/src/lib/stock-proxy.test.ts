import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const proxyDependencies = vi.hoisted(() => ({
  isSameOriginMutation: vi.fn(),
  getAuthSession: vi.fn(),
}));

vi.mock("@/lib/request-security", () => ({
  isSameOriginMutation: proxyDependencies.isSameOriginMutation,
}));

vi.mock("@/lib/session", () => ({
  getAuthSession: proxyDependencies.getAuthSession,
}));

import { proxyStockRequest } from "@/lib/stock-proxy";

const authSession = {
  accessToken: "test-access-token",
  tenant: "myretail",
  user: {
    email: "owner@example.test",
    full_name: "Owner",
    roles: ["Owner"],
  },
};

function jsonRequest(
  url: string,
  body: unknown,
  idempotencyKey = "123e4567-e89b-42d3-a456-426614174000",
) {
  return new Request(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
      Origin: "http://localhost:3000",
    },
    body: JSON.stringify(body),
  });
}

beforeEach(() => {
  process.env.MYRETAIL_API_URL = "http://api.test";
  proxyDependencies.isSameOriginMutation.mockReset().mockReturnValue(true);
  proxyDependencies.getAuthSession.mockReset().mockResolvedValue(authSession);
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.MYRETAIL_API_URL;
});

describe("stock proxy", () => {
  it("rejects a cross-origin mutation before reading the session", async () => {
    proxyDependencies.isSameOriginMutation.mockReturnValue(false);

    const response = await proxyStockRequest({
      endpoint: "/stock/movements",
      request: jsonRequest("http://localhost:3000/api/stock/movements", {
        type: "receipt",
      }),
      method: "POST",
    });

    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({
      error: {
        code: "FORBIDDEN",
        message: "Запрос отклонён проверкой безопасности.",
        fields: {},
      },
    });
    expect(proxyDependencies.getAuthSession).not.toHaveBeenCalled();
  });

  it("requires an authenticated session", async () => {
    proxyDependencies.getAuthSession.mockResolvedValue(null);

    const response = await proxyStockRequest({
      endpoint: "/stock/balances",
      request: new Request("http://localhost:3000/api/stock/balances"),
    });

    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({
      error: {
        code: "AUTH_REQUIRED",
        message: "Нужно войти в систему.",
        fields: {},
      },
    });
  });

  it("requires an idempotency key for stock mutations", async () => {
    const response = await proxyStockRequest({
      endpoint: "/stock/movements",
      request: jsonRequest(
        "http://localhost:3000/api/stock/movements",
        { type: "receipt" },
        "",
      ),
      method: "POST",
    });

    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({
      error: {
        code: "INVALID_REQUEST",
      },
    });
  });

  it("forwards query, session context, JSON body and idempotency key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json(
        {
          id: "MAT-STE-2026-00001",
        },
        { status: 201 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = {
      type: "receipt",
      warehouse_id: "Основной склад QA - MRD",
      lines: [{ product_id: "QA-MILK-001", quantity: "1.000" }],
    };

    const response = await proxyStockRequest({
      endpoint: "/stock/movements",
      request: jsonRequest(
        "http://localhost:3000/api/stock/movements?source=ui",
        body,
      ),
      method: "POST",
    });

    expect(response.status).toBe(201);
    expect(fetchMock).toHaveBeenCalledOnce();
    const [forwardedUrl, forwardedInit] = fetchMock.mock.calls[0];
    expect(String(forwardedUrl)).toBe("http://api.test/stock/movements?source=ui");
    expect(forwardedInit.method).toBe("POST");
    expect(forwardedInit.cache).toBe("no-store");
    expect(forwardedInit.headers).toEqual({
      Authorization: "Bearer test-access-token",
      "X-MyRetail-Tenant": "myretail",
      "Content-Type": "application/json",
      "Idempotency-Key": "123e4567-e89b-42d3-a456-426614174000",
    });
    expect(forwardedInit.body).toBe(JSON.stringify(body));
  });

  it("maps a backend timeout to a stable Russian error", async () => {
    const timeout = new Error("timeout");
    timeout.name = "TimeoutError";
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(timeout));

    const response = await proxyStockRequest({
      endpoint: "/stock/balances",
      request: new Request("http://localhost:3000/api/stock/balances"),
    });

    expect(response.status).toBe(504);
    expect(await response.json()).toEqual({
      error: {
        code: "API_TIMEOUT",
        message: "MyRetail API не ответил вовремя. Попробуйте ещё раз.",
        fields: {},
      },
    });
  });
});
