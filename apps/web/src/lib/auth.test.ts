import { describe, expect, it } from "vitest";

import {
  canManageProducts,
  canManagePurchases,
  canManageStock,
  canUsePOS,
} from "@/lib/auth";

const domainManagerRoles = [
  "Sales Manager",
  "Stock Manager",
  "Accounts Manager",
  "Item Manager",
];

describe("MyRetail role boundary", () => {
  it.each(domainManagerRoles)("does not treat raw ERP role %s as product Admin", (role) => {
    expect(canManageProducts([role])).toBe(false);
    expect(canManageStock([role])).toBe(false);
    expect(canManagePurchases([role])).toBe(false);
    expect(canUsePOS([role])).toBe(false);
  });

  it.each(["Owner", "Admin"])("keeps mapped MyRetail role %s privileged", (role) => {
    expect(canManageProducts([role])).toBe(true);
    expect(canManageStock([role])).toBe(true);
    expect(canManagePurchases([role])).toBe(true);
    expect(canUsePOS([role])).toBe(true);
  });
});
