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

import { proxyPOSRequest } from "@/lib/pos-proxy";

const authSession = {
  accessToken: "test-access-token",
  tenant: "myretail",
  user: {
    email: "cashier@example.test",
    full_name: "Cashier",
    roles: ["Cashier"],
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

describe("POS proxy", () => {
  it("rejects unsafe mutations before reading the session", async () => {
    proxyDependencies.isSameOriginMutation.mockReturnValue(false);

    const response = await proxyPOSRequest({
      endpoint: "/pos/sales",
      request: jsonRequest("http://localhost:3000/api/pos/sales", { shift_id: "SHIFT-1" }),
      method: "POST",
    });

    expect(response.status).toBe(403);
    expect(await response.json()).toMatchObject({
      error: {
        code: "FORBIDDEN",
      },
    });
    expect(proxyDependencies.getAuthSession).not.toHaveBeenCalled();
  });

  it("requires an authenticated HttpOnly session", async () => {
    proxyDependencies.getAuthSession.mockResolvedValue(null);

    const response = await proxyPOSRequest({
      endpoint: "/pos/options",
      request: new Request("http://localhost:3000/api/pos/options"),
    });

    expect(response.status).toBe(401);
    expect(await response.json()).toMatchObject({
      error: {
        code: "UNAUTHORIZED",
      },
    });
  });

  it("requires Idempotency-Key for POS POST operations", async () => {
    const response = await proxyPOSRequest({
      endpoint: "/pos/sales",
      request: jsonRequest("http://localhost:3000/api/pos/sales", { shift_id: "SHIFT-1" }, ""),
      method: "POST",
    });

    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({
      error: {
        code: "INVALID_REQUEST",
      },
    });
  });

  it("forwards filters, session context, JSON body and idempotency key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ id: "SALE-1" }, { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    const body = {
      shift_id: "SHIFT-1",
      lines: [{ product_id: "QA-MILK-001", quantity: "1.000", discount_percent: "0.00" }],
      cash_received: "1000.00",
    };

    const response = await proxyPOSRequest({
      endpoint: "/pos/sales",
      request: jsonRequest("http://localhost:3000/api/pos/sales?q=milk", body),
      method: "POST",
    });

    expect(response.status).toBe(201);
    const [forwardedUrl, forwardedInit] = fetchMock.mock.calls[0];
    expect(String(forwardedUrl)).toBe("http://api.test/pos/sales?q=milk");
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

  it("does not require idempotency for held receipt PATCH and DELETE", async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ id: "HELD-1" }));
    vi.stubGlobal("fetch", fetchMock);

    const response = await proxyPOSRequest({
      endpoint: "/pos/held-receipts/HELD-1",
      request: jsonRequest(
        "http://localhost:3000/api/pos/held-receipts/HELD-1",
        { expected_updated_at: "2026-07-08T00:00:00Z", lines: [] },
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

  it("maps backend timeout to a stable POS error", async () => {
    const timeout = new Error("timeout");
    timeout.name = "TimeoutError";
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(timeout));

    const response = await proxyPOSRequest({
      endpoint: "/pos/sales",
      request: new Request("http://localhost:3000/api/pos/sales"),
    });

    expect(response.status).toBe(504);
    expect(await response.json()).toMatchObject({
      error: {
        code: "API_TIMEOUT",
      },
    });
  });
});
