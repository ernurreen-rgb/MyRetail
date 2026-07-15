import { afterEach, describe, expect, it, vi } from "vitest";

import { verifyAuthSession } from "@/lib/session";

const session = {
  accessToken: "signed-token",
  tenant: "myretail",
};

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.MYRETAIL_API_URL;
});

describe("verifyAuthSession", () => {
  it("accepts a matching context returned by MyRetail API", async () => {
    process.env.MYRETAIL_API_URL = "http://api.test";
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        tenant: "myretail",
        user: {
          email: "owner@example.com",
          full_name: "Owner",
          roles: ["Owner"],
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(verifyAuthSession(session)).resolves.toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/auth/me",
      expect.objectContaining({
        cache: "no-store",
        headers: {
          Authorization: "Bearer signed-token",
          "X-MyRetail-Tenant": "myretail",
        },
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("rejects an expired or invalid token response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 401 })));

    await expect(verifyAuthSession(session)).resolves.toBe(false);
  });

  it("rejects a response for another tenant", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          tenant: "other-tenant",
          user: {
            email: "owner@example.com",
            full_name: "Owner",
            roles: ["Owner"],
          },
        }),
      ),
    );

    await expect(verifyAuthSession(session)).resolves.toBe(false);
  });

  it("rejects an unavailable verification service", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network unavailable")));

    await expect(verifyAuthSession(session)).resolves.toBe(false);
  });

  it("fails closed without fetch for an unsafe configured API URL", async () => {
    process.env.MYRETAIL_API_URL = "file:///etc/passwd";
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(verifyAuthSession(session)).resolves.toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
