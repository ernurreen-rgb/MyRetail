import { describe, expect, it } from "vitest";

import { getAlbForwardedClientIp } from "@/lib/client-ip";

function requestWithForwardedFor(value?: string) {
  return new Request("https://retail.example.test/api/auth/login", {
    headers: value === undefined ? {} : { "X-Forwarded-For": value },
  });
}

describe("getAlbForwardedClientIp", () => {
  it.each([
    ["198.51.100.24", "198.51.100.24"],
    ["203.0.113.66, 198.51.100.24", "198.51.100.24"],
    ["2001:db8:ffff::66, 2001:db8::24", "2001:db8::24"],
  ])("returns only the ALB-appended address from %s", (header, expected) => {
    expect(getAlbForwardedClientIp(requestWithForwardedFor(header))).toBe(expected);
  });

  it.each([
    undefined,
    "",
    "198.51.100.24, ",
    "198.51.100.24, not-an-ip",
    `[${"1".repeat(46)}]`,
  ])("fails safe for missing or malformed address %s", (header) => {
    expect(getAlbForwardedClientIp(requestWithForwardedFor(header))).toBeNull();
  });
});
