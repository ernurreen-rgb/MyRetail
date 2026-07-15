import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuthSession } from "@/lib/auth";

const proxyMocks = vi.hoisted(() => ({
  getAuthSession: vi.fn(),
  isSameOriginMutation: vi.fn(),
}));

vi.mock("@/lib/request-security", () => ({
  isSameOriginMutation: proxyMocks.isSameOriginMutation,
}));

vi.mock("@/lib/session", () => ({
  getAuthSession: proxyMocks.getAuthSession,
}));

import { proxyProductRequest } from "@/lib/products-proxy";

const session: AuthSession = {
  accessToken: "secret-access-token",
  tenant: "myretail",
  user: {
    email: "owner@example.test",
    full_name: "Owner",
    roles: ["Owner"],
  },
};

beforeEach(() => {
  process.env.MYRETAIL_API_URL = "http://api.example.test";
  proxyMocks.getAuthSession.mockResolvedValue(session);
  proxyMocks.isSameOriginMutation.mockReturnValue(true);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  delete process.env.MYRETAIL_API_URL;
});

describe("product BFF proxy", () => {
  it("fails closed before fetch when an endpoint tries to escape the API origin", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const response = await proxyProductRequest({
      endpoint: "//attacker.example/collect",
      request: new Request("http://localhost:3000/api/products"),
    });

    expect(response.status).toBe(503);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sanitizes backend 403 and never exposes bearer tokens in the browser response", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "PRODUCTS_FORBIDDEN",
            message: "Forbidden with secret-access-token and cookie=myretail_access_token",
            fields: {},
          },
        }),
        { status: 403, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await proxyProductRequest({
      endpoint: "/products",
      request: new Request("http://localhost:3000/api/products?include_archived=true"),
    });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(String(url)).toBe("http://api.example.test/products?include_archived=true");
    expect(init.headers).toMatchObject({
      Authorization: "Bearer secret-access-token",
      "X-MyRetail-Tenant": "myretail",
    });

    expect(response.status).toBe(403);
    const body = await response.text();
    expect(body).toContain("Нет доступа к управлению товарами");
    expect(body).not.toContain("secret-access-token");
    expect(body).not.toContain("myretail_access_token");
  });
});
