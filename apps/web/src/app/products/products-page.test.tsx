// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthSession } from "@/lib/auth";

const pageMocks = vi.hoisted(() => ({
  getAuthSession: vi.fn(),
  productManager: vi.fn(),
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

vi.mock("@/app/products/product-manager", () => ({
  ProductManager: (props: { canManage: boolean }) => {
    pageMocks.productManager(props);
    return <section>Product manager mounted</section>;
  },
}));

import ProductsPage from "@/app/products/page";

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

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ProductsPage permissions", () => {
  it("shows a clean Russian 403 for Cashier without mounting product management", async () => {
    pageMocks.getAuthSession.mockResolvedValue(cashierSession);

    render(await ProductsPage());

    expect(screen.getByRole("heading", { name: "Доступ к товарам запрещён" })).toBeTruthy();
    expect(screen.getByText(/закупочные цены и архивный каталог доступны только ролям Owner/i)).toBeTruthy();
    expect(screen.getByRole<HTMLAnchorElement>("link", { name: "Открыть кассу" }).href).toContain(
      "/pos",
    );
    expect(pageMocks.productManager).not.toHaveBeenCalled();
  });

  it("mounts product management for Owner/Admin roles", async () => {
    pageMocks.getAuthSession.mockResolvedValue(ownerSession);

    render(await ProductsPage());

    expect(pageMocks.productManager).toHaveBeenCalledWith({ canManage: true });
    expect(screen.getByText("Product manager mounted")).toBeTruthy();
  });
});
