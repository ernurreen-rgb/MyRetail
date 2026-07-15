import { afterEach, describe, expect, it } from "vitest";

import { buildApiUrl, getApiBaseUrl } from "@/lib/config";

afterEach(() => {
  delete process.env.MYRETAIL_API_URL;
});

describe("MyRetail API URL boundary", () => {
  it("uses the local default and preserves an explicitly configured base path", () => {
    expect(getApiBaseUrl()).toBe("http://localhost:8000");
    expect(buildApiUrl("/auth/me")).toBe("http://localhost:8000/auth/me");

    process.env.MYRETAIL_API_URL = "https://api.example.test/internal///";

    expect(getApiBaseUrl()).toBe("https://api.example.test/internal");
    expect(buildApiUrl("/products/A%2FB")).toBe(
      "https://api.example.test/internal/products/A%2FB",
    );
  });

  it("copies query values without allowing them to change the configured origin", () => {
    process.env.MYRETAIL_API_URL = "https://api.example.test";
    const searchParams = new URLSearchParams([
      ["next", "https://attacker.example/collect"],
      ["next", "//attacker.example/collect"],
    ]);

    const target = new URL(buildApiUrl("/products", searchParams));

    expect(target.origin).toBe("https://api.example.test");
    expect(target.pathname).toBe("/products");
    expect(target.searchParams.getAll("next")).toEqual([
      "https://attacker.example/collect",
      "//attacker.example/collect",
    ]);
  });

  it.each([
    "api.example.test",
    "ftp://api.example.test",
    "https://user:password@api.example.test",
    "https://api.example.test?target=other",
    "https://api.example.test#fragment",
  ])("rejects unsafe configured base URL %s", (configuredUrl) => {
    process.env.MYRETAIL_API_URL = configuredUrl;

    expect(() => getApiBaseUrl()).toThrow();
    expect(() => buildApiUrl("/products")).toThrow();
  });

  it.each([
    "products",
    "https://attacker.example/collect",
    "//attacker.example/collect",
    "/products\\redirect",
    "/products?target=https://attacker.example",
    "/products#target",
    "/products/..",
    "/products/%2e%2e",
    "/products/%ZZ",
  ])("rejects endpoint escape attempt %s", (endpoint) => {
    process.env.MYRETAIL_API_URL = "https://api.example.test/internal";

    expect(() => buildApiUrl(endpoint)).toThrow();
  });
});
