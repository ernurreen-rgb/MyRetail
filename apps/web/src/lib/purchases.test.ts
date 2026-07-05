import { describe, expect, it, vi } from "vitest";

import { canManagePurchases } from "@/lib/auth";
import {
  createIdempotencyKey,
  emptyPurchaseFormValues,
  emptySupplierFormValues,
  isPurchase,
  isPurchaseList,
  isPurchaseOptions,
  isSupplier,
  isSupplierList,
  parsePurchasesApiError,
  toPurchaseCancelPayload,
  toPurchaseCreatePayload,
  toPurchaseSubmitPayload,
  toPurchaseUpdatePayload,
  toSupplierCreatePayload,
  toSupplierUpdatePayload,
  type Purchase,
  type PurchaseOptions,
  type Supplier,
} from "@/lib/purchases";

const supplier: Supplier = {
  id: "SUP-00001",
  name: "ТОО Молоко",
  tax_id: "123456789012",
  contact_name: "Алия",
  phone: "+7 701 111 22 33",
  email: "supplier@example.kz",
  address: "Астана",
  is_active: true,
  updated_at: "2026-07-04T00:00:00Z",
};

const purchaseOptions: PurchaseOptions = {
  warehouses: [
    {
      id: "Stores - MR",
      name: "Основной склад",
      is_default: true,
      is_active: true,
    },
  ],
  currency: "KZT",
  quantity_precision: 3,
  money_precision: 2,
};

const purchase: Purchase = {
  id: "PUR-00001",
  status: "draft",
  supplier: {
    id: supplier.id,
    name: supplier.name,
  },
  warehouse: {
    id: "Stores - MR",
    name: "Основной склад",
  },
  posting_date: "2026-07-04",
  supplier_invoice_number: "INV-42",
  supplier_invoice_date: "2026-07-03",
  currency: "KZT",
  comment: null,
  subtotal: "1200.00",
  total: "1200.00",
  created_by: {
    email: "owner@example.test",
    full_name: "Owner",
  },
  created_at: "2026-07-04T00:00:00Z",
  submitted_by: null,
  submitted_at: null,
  cancelled_by: null,
  cancelled_at: null,
  updated_at: "2026-07-04T00:00:00Z",
  lines: [
    {
      product_id: "QA-MILK-001",
      sku: "QA-MILK-001",
      name: "Молоко 3,2%",
      unit: "Nos",
      quantity: "2.000",
      unit_price: "600.00",
      line_total: "1200.00",
    },
  ],
};

describe("purchase permissions", () => {
  it.each(["Owner", "Admin"])("allows %s to manage purchases", (role) => {
    expect(canManagePurchases([role])).toBe(true);
  });

  it("blocks Cashier from the purchases module", () => {
    expect(canManagePurchases(["Cashier"])).toBe(false);
  });
});

describe("purchase payloads", () => {
  it("creates a valid UUID when randomUUID is unavailable", () => {
    vi.stubGlobal("crypto", {
      getRandomValues(bytes: Uint8Array) {
        bytes.fill(0xcd);
        return bytes;
      },
    });

    expect(createIdempotencyKey()).toBe("cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd");
  });

  it("normalizes supplier payload text and nullable fields", () => {
    expect(
      toSupplierCreatePayload(
        emptySupplierFormValues({
          name: " ТОО Молоко ",
          tax_id: " ",
          contact_name: " Алия ",
        }),
      ),
    ).toEqual({
      name: "ТОО Молоко",
      tax_id: null,
      contact_name: "Алия",
      phone: null,
      email: null,
      address: null,
    });
  });

  it("adds expected_updated_at to supplier updates", () => {
    expect(toSupplierUpdatePayload(emptySupplierFormValues({ name: "A" }), "v1")).toMatchObject({
      expected_updated_at: "v1",
      name: "A",
    });
  });

  it("keeps quantity and money as strings without float conversion", () => {
    const payload = toPurchaseCreatePayload(
      emptyPurchaseFormValues({
        supplier_id: " SUP-00001 ",
        warehouse_id: " Stores - MR ",
        posting_date: "2026-07-04",
        supplier_invoice_number: " ",
        supplier_invoice_date: "",
        comment: " тест ",
        lines: [
          {
            product_id: " QA-MILK-001 ",
            quantity: " 9999999999999999,999 ",
            unit_price: " 1200,50 ",
          },
        ],
      }),
    );

    expect(payload).toEqual({
      supplier_id: "SUP-00001",
      warehouse_id: "Stores - MR",
      posting_date: "2026-07-04",
      supplier_invoice_number: null,
      supplier_invoice_date: null,
      comment: "тест",
      lines: [
        {
          product_id: "QA-MILK-001",
          quantity: "9999999999999999.999",
          unit_price: "1200.50",
        },
      ],
    });
    expect(typeof payload.lines[0].quantity).toBe("string");
    expect(typeof payload.lines[0].unit_price).toBe("string");
  });

  it("uses optimistic version fields for draft update and submit", () => {
    expect(toPurchaseUpdatePayload(emptyPurchaseFormValues({ posting_date: "2026-07-04" }), "v2"))
      .toMatchObject({
        expected_updated_at: "v2",
        posting_date: "2026-07-04",
      });
    expect(toPurchaseSubmitPayload("v3")).toEqual({ expected_updated_at: "v3" });
    expect(toPurchaseCancelPayload(" ошибка ")).toEqual({ reason: "ошибка" });
  });
});

describe("purchase API guards", () => {
  it("accepts complete supplier, options, purchase and list responses", () => {
    expect(isSupplier(supplier)).toBe(true);
    expect(isSupplierList({ items: [supplier], count: 1, limit: 10, offset: 0 })).toBe(true);
    expect(isPurchaseOptions(purchaseOptions)).toBe(true);
    expect(isPurchase(purchase)).toBe(true);
    expect(
      isPurchaseList({
        items: [
          {
            id: purchase.id,
            status: purchase.status,
            supplier: purchase.supplier,
            warehouse: purchase.warehouse,
            posting_date: purchase.posting_date,
            supplier_invoice_number: purchase.supplier_invoice_number,
            supplier_invoice_date: purchase.supplier_invoice_date,
            currency: purchase.currency,
            subtotal: purchase.subtotal,
            total: purchase.total,
            updated_at: purchase.updated_at,
          },
        ],
        count: 1,
        limit: 10,
        offset: 0,
      }),
    ).toBe(true);
  });

  it("rejects incomplete responses and parses structured API errors", () => {
    expect(isPurchase({ ...purchase, lines: [{ product_id: "QA-MILK-001" }] })).toBe(false);
    expect(
      parsePurchasesApiError({
        error: {
          code: "PURCHASE_CHANGED",
          message: "Документ изменён",
          fields: { expected_updated_at: "Обновите документ" },
        },
      }),
    ).toEqual({
      code: "PURCHASE_CHANGED",
      message: "Документ изменён",
      fields: { expected_updated_at: "Обновите документ" },
    });
    expect(parsePurchasesApiError(null).code).toBe("REQUEST_ERROR");
  });
});
