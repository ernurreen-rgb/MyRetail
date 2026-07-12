// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProductManager } from "@/app/products/product-manager";
import type { Product, ProductOptions } from "@/lib/products";

const productApi = vi.hoisted(() => ({
  archiveProduct: vi.fn(),
  createProduct: vi.fn(),
  getProductOptions: vi.fn(),
  listProducts: vi.fn(),
  updateProduct: vi.fn(),
}));

vi.mock("@/lib/products", async () => {
  const actual = await vi.importActual<typeof import("@/lib/products")>("@/lib/products");

  return {
    ...actual,
    ...productApi,
  };
});

const milkProduct: Product = {
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
};

const productOptions: ProductOptions = {
  categories: [{ id: "Молочные", name: "Молочные" }],
  brands: [],
  units: [{ id: "шт", name: "шт" }],
};

beforeEach(() => {
  productApi.archiveProduct.mockReset();
  productApi.createProduct.mockReset();
  productApi.updateProduct.mockReset();
  productApi.getProductOptions.mockReset().mockResolvedValue({
    status: "success",
    data: productOptions,
  });
  productApi.listProducts.mockReset().mockResolvedValue({
    status: "success",
    data: {
      items: [milkProduct],
      count: 1,
      limit: 50,
      offset: 0,
    },
  });
});

afterEach(() => {
  cleanup();
});

describe("ProductManager permissions", () => {
  it("blocks Cashier UI before product catalog, archive and purchase prices can load", () => {
    render(<ProductManager canManage={false} />);

    expect(screen.getByRole("heading", { name: "Доступ к товарам запрещён" })).toBeTruthy();
    expect(productApi.listProducts).not.toHaveBeenCalled();
    expect(productApi.getProductOptions).not.toHaveBeenCalled();
    expect(screen.queryByText("Показывать архивные товары")).toBeNull();
    expect(screen.queryByText(/Закуп:/)).toBeNull();
    expect(screen.queryByText("600.00")).toBeNull();
  });

  it("keeps product management and purchase prices visible for Owner/Admin", async () => {
    render(<ProductManager canManage />);

    expect(await screen.findByText("Молоко 3,2%")).toBeTruthy();
    expect(screen.getByText("Закуп: 600.00")).toBeTruthy();
    expect(screen.getByText("Показывать архивные товары")).toBeTruthy();
    expect(productApi.listProducts).toHaveBeenCalledWith({ limit: 50, offset: 0 });
  });
});
