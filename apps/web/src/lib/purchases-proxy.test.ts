import { beforeEach, describe, expect, it, vi } from "vitest";

const proxyDependencies = vi.hoisted(() => ({
  getApiBaseUrl: vi.fn(),
  isSameOriginMutation: vi.fn(),
  getAuthSession: vi.fn(),
}));

vi.mock("@/lib/config", () => ({
  getApiBaseUrl: proxyDependencies.getApiBaseUrl,
}));

vi.mock("@/lib/request-security", () => ({
  isSameOriginMutation: proxyDependencies.isSameOriginMutation,
}));

vi.mock("@/lib/session", () => ({
  getAuthSession: proxyDependencies.getAuthSession,
}));

import { proxyPurchasesRequest } from "@/lib/purchases-proxy";

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
  method = "POST",
) {
  return new Request(url, {
    method,
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
      Origin: "http://localhost:3000",
    },
    body: JSON.stringify(body),
  });
}

beforeEach(() => {
  proxyDependencies.getApiBaseUrl.mockReset().mockReturnValue("http://api.test");
  proxyDependencies.isSameOriginMutation.mockReset().mockReturnValue(true);
  proxyDependencies.getAuthSession.mockReset().mockResolvedValue(authSession);
});

describe("purchases proxy", () => {
  it("rejects cross-origin mutations before reading the session", async () => {
    proxyDependencies.isSameOriginMutation.mockReturnValue(false);

    const response = await proxyPurchasesRequest({
      endpoint: "/purchases",
      request: jsonRequest("http://localhost:3000/api/purchases", { supplier_id: "SUP-1" }),
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

    const response = await proxyPurchasesRequest({
      endpoint: "/suppliers",
      request: new Request("http://localhost:3000/api/suppliers"),
    });

    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({
      error: {
        code: "UNAUTHORIZED",
        message: "Нужно войти в систему.",
        fields: {},
      },
    });
  });

  it("requires an idempotency key for purchase POST mutations", async () => {
    const response = await proxyPurchasesRequest({
      endpoint: "/purchases",
      request: jsonRequest("http://localhost:3000/api/purchases", { supplier_id: "SUP-1" }, ""),
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
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ id: "PUR-00001" }, { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    const body = {
      supplier_id: "SUP-1",
      warehouse_id: "Stores - MR",
      posting_date: "2026-07-04",
      lines: [{ product_id: "QA-MILK-001", quantity: "1.000", unit_price: "600.00" }],
    };

    const response = await proxyPurchasesRequest({
      endpoint: "/purchases",
      request: jsonRequest("http://localhost:3000/api/purchases?source=ui", body),
      method: "POST",
    });

    expect(response.status).toBe(201);
    expect(fetchMock).toHaveBeenCalledOnce();
    const [forwardedUrl, forwardedInit] = fetchMock.mock.calls[0];
    expect(String(forwardedUrl)).toBe("http://api.test/purchases?source=ui");
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

  it("does not require an idempotency key for PATCH or DELETE", async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ id: "SUP-1" }));
    vi.stubGlobal("fetch", fetchMock);

    const response = await proxyPurchasesRequest({
      endpoint: "/suppliers/SUP-1",
      request: jsonRequest(
        "http://localhost:3000/api/suppliers/SUP-1",
        { expected_updated_at: "v1", phone: "+7" },
        "",
        "PATCH",
      ),
      method: "PATCH",
    });

    expect(response.status).toBe(200);
    const [, forwardedInit] = fetchMock.mock.calls[0];
    expect(forwardedInit.headers).toEqual({
      Authorization: "Bearer test-access-token",
      "X-MyRetail-Tenant": "myretail",
      "Content-Type": "application/json",
    });
  });

  it("maps a backend timeout to a stable Russian error", async () => {
    const timeout = new Error("timeout");
    timeout.name = "TimeoutError";
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(timeout));

    const response = await proxyPurchasesRequest({
      endpoint: "/purchases",
      request: new Request("http://localhost:3000/api/purchases"),
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
