import { describe, expect, it } from "vitest";

import nextConfig, {
  BFF_NO_STORE_HEADERS,
  WEB_SECURITY_HEADERS,
} from "../../next.config";

describe("web response security headers", () => {
  it("applies browser hardening globally and no-store to BFF routes", async () => {
    expect(nextConfig.headers).toBeTypeOf("function");

    const rules = await nextConfig.headers!();

    expect(rules).toContainEqual({
      source: "/:path*",
      headers: [...WEB_SECURITY_HEADERS],
    });
    expect(rules).toContainEqual({
      source: "/api/:path*",
      headers: [...BFF_NO_STORE_HEADERS],
    });
  });
});
