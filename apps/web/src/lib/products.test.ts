import { describe, expect, it } from "vitest";

import { canManageProducts } from "@/lib/auth";
import {
  isProduct,
  isProductsResponse,
  emptyProductFormValues,
  parseProductApiError,
  toProductCreatePayload,
  type ProductFormValues,
} from "@/lib/products";

const formValues: ProductFormValues = {
  sku: " DEMO-001 ",
  name: " Молоко ",
  barcode: " ",
  category: "Products",
  brand: "",
  unit: "Nos",
  sale_price: " 9999999999999999,99 ",
  purchase_price: "",
  description: " ",
};

describe("product permissions", () => {
  it.each(["Owner", "Admin"])("allows %s to manage products", (role) => {
    expect(canManageProducts([role])).toBe(true);
  });

  it("keeps Cashier in read-only mode", () => {
    expect(canManageProducts(["Cashier"])).toBe(false);
  });
});

describe("product payloads", () => {
  it("requires an explicit category and unit selection for a new product", () => {
    expect(emptyProductFormValues()).toMatchObject({
      category: "",
      unit: "",
    });
  });

  it("normalizes text without converting exact money strings through Number", () => {
    expect(toProductCreatePayload(formValues)).toEqual({
      sku: "DEMO-001",
      name: "Молоко",
      barcode: null,
      category: "Products",
      brand: null,
      unit: "Nos",
      sale_price: "9999999999999999.99",
      purchase_price: null,
      description: null,
    });
  });

  it("leaves excessive precision for backend validation instead of rounding it", () => {
    expect(
      toProductCreatePayload({
        ...formValues,
        sale_price: "1.999",
      }).sale_price,
    ).toBe("1.999");
  });
});

describe("product API guards", () => {
  const product = {
    id: "DEMO-001",
    sku: "DEMO-001",
    name: "Молоко",
    barcode: null,
    category: "Products",
    brand: null,
    unit: "Nos",
    sale_price: "650.00",
    purchase_price: null,
    currency: "KZT",
    description: null,
    image_url: null,
    is_active: true,
  };

  it("accepts a complete product response", () => {
    expect(isProduct(product)).toBe(true);
    expect(
      isProductsResponse({
        items: [product],
        count: 1,
        limit: 50,
        offset: 0,
      }),
    ).toBe(true);
  });

  it("rejects an incomplete product response", () => {
    expect(isProduct({ ...product, currency: undefined })).toBe(false);
  });

  it("parses structured API errors and falls back for malformed errors", () => {
    expect(
      parseProductApiError({
        error: {
          code: "VALIDATION_ERROR",
          message: "Проверьте поля",
          fields: { sale_price: "Некорректная цена" },
        },
      }),
    ).toEqual({
      code: "VALIDATION_ERROR",
      message: "Проверьте поля",
      fields: { sale_price: "Некорректная цена" },
    });
    expect(parseProductApiError(null).code).toBe("REQUEST_ERROR");
  });
});
