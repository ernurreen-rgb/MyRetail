// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthSession } from "@/lib/auth";
import type { ProductsResponse } from "@/lib/products";

const pageMocks = vi.hoisted(() => ({
  getAuthSession: vi.fn(),
  getProducts: vi.fn(),
  redirect: vi.fn(),
}));

vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...props
  }: {
    href: string;
    children: ReactNode;
  }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

vi.mock("next/navigation", () => ({
  redirect: pageMocks.redirect,
}));

vi.mock("@/lib/session", () => ({
  getAuthSession: pageMocks.getAuthSession,
}));

vi.mock("@/lib/products-server", () => ({
  getProducts: pageMocks.getProducts,
}));

import Home from "@/app/page";

const ownerSession: AuthSession = {
  accessToken: "owner-token",
  tenant: "myretail",
  user: {
    email: "owner@example.test",
    full_name: "Owner",
    roles: ["Owner"],
  },
};

const cashierSession: AuthSession = {
  accessToken: "cashier-token",
  tenant: "myretail",
  user: {
    email: "cashier@example.test",
    full_name: "Cashier",
    roles: ["Cashier"],
  },
};

const productsResponse: ProductsResponse = {
  items: [
    {
      id: "QA-MILK-001",
      sku: "QA-MILK-001",
      name: "Молоко 3,2%",
      barcode: "460123",
      category: "Молочные",
      brand: null,
      unit: "шт",
      sale_price: "800.00",
      purchase_price: "600.00",
      currency: "KZT",
      description: null,
      image_url: null,
      is_active: true,
    },
  ],
  count: 1,
  limit: 50,
  offset: 0,
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Home role navigation", () => {
  it("hides product management navigation from Cashier without calling /products", async () => {
    pageMocks.getAuthSession.mockResolvedValue(cashierSession);

    render(await Home());

    expect(pageMocks.getProducts).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Касса" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Товары" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Открыть управление товарами" })).toBeNull();
    expect(screen.queryByText("Товары")).toBeNull();
    expect(screen.getByText(/закупочные цены и архив скрыты/i)).toBeTruthy();
  });

  it("keeps Products visible for Owner/Admin roles", async () => {
    pageMocks.getAuthSession.mockResolvedValue(ownerSession);
    pageMocks.getProducts.mockResolvedValue({ status: "ready", data: productsResponse });

    render(await Home());

    expect(pageMocks.getProducts).toHaveBeenCalledWith(ownerSession);
    expect(screen.getByRole("link", { name: "Товары" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Открыть управление товарами" })).toBeTruthy();
    expect(screen.getByText("Молоко 3,2%")).toBeTruthy();
  });
});
