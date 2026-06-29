import { describe, expect, it } from "vitest";

import { canManageStock } from "@/lib/auth";
import {
  emptyStockMovementFormValues,
  isStockBalanceList,
  isStockMovement,
  isStockMovementCancelResponse,
  isStockMovementList,
  isStockOptions,
  parseStockApiError,
  toStockMovementCancelPayload,
  toStockMovementPayload,
  type StockBalance,
  type StockMovement,
  type StockOptions,
} from "@/lib/stock";

const stockOptions: StockOptions = {
  warehouses: [
    {
      id: "WH-1",
      name: "Главный склад",
      is_default: true,
      is_active: true,
    },
    {
      id: "WH-2",
      name: "Витрина",
      is_default: false,
      is_active: true,
    },
  ],
  write_off_reasons: [
    {
      code: "damage",
      name: "Порча",
    },
  ],
  adjustment_reasons: [
    {
      code: "manual_count",
      name: "Ручной пересчёт",
    },
  ],
};

const stockBalance: StockBalance = {
  product_id: "DEMO-001",
  sku: "DEMO-001",
  name: "Молоко",
  unit: "Nos",
  warehouse: {
    id: "WH-1",
    name: "Главный склад",
  },
  on_hand: "10.000",
  reserved: "2.000",
  available: "8.000",
  updated_at: "2026-06-29T12:00:00Z",
};

const stockMovement: StockMovement = {
  id: "MOV-1",
  type: "receipt",
  status: "posted",
  warehouse_id: "WH-1",
  destination_warehouse_id: null,
  reason_code: null,
  comment: null,
  created_by: {
    email: "owner@example.test",
    full_name: "Owner",
  },
  created_at: "2026-06-29T12:05:00Z",
  cancelled_by: null,
  cancelled_at: null,
  reversal_movement_id: null,
  lines: [
    {
      product_id: "DEMO-001",
      quantity: "5.000",
      before_quantity: "5.000",
      after_quantity: "10.000",
    },
  ],
};

describe("stock permissions", () => {
  it.each(["Owner", "Admin"])("allows %s to manage stock movements", (role) => {
    expect(canManageStock([role])).toBe(true);
  });

  it("keeps Cashier in read-only stock mode", () => {
    expect(canManageStock(["Cashier"])).toBe(false);
  });
});

describe("stock payloads", () => {
  it("normalizes quantity text without converting through Number", () => {
    const payload = toStockMovementPayload(
      emptyStockMovementFormValues({
        type: "write_off",
        product_id: " DEMO-001 ",
        warehouse_id: " WH-1 ",
        quantity: " 9999999999999999,999 ",
        reason_code: " damage ",
        comment: " ",
      }),
    );

    expect(payload).toEqual({
      type: "write_off",
      warehouse_id: "WH-1",
      destination_warehouse_id: null,
      reason_code: "damage",
      comment: null,
      lines: [
        {
          product_id: "DEMO-001",
          quantity: "9999999999999999.999",
        },
      ],
    });
  });

  it("keeps adjustment counted and expected quantities as strings", () => {
    const payload = toStockMovementPayload(
      emptyStockMovementFormValues({
        type: "adjustment",
        product_id: "DEMO-001",
        warehouse_id: "WH-1",
        counted_quantity: "12,5000",
        expected_quantity: "10.000",
        reason_code: "manual_count",
      }),
    );

    expect(payload.lines[0]).toEqual({
      product_id: "DEMO-001",
      counted_quantity: "12.5000",
      expected_quantity: "10.000",
    });
    expect("quantity" in payload.lines[0]).toBe(false);
  });

  it("trims cancel reasons without mutating their text", () => {
    expect(toStockMovementCancelPayload(" ошибочная операция ")).toEqual({
      reason: "ошибочная операция",
    });
  });
});

describe("stock API guards", () => {
  it("accepts complete stock option, balance, movement and cancel responses", () => {
    expect(isStockOptions(stockOptions)).toBe(true);
    expect(
      isStockBalanceList({
        items: [stockBalance],
        count: 1,
        limit: 20,
        offset: 0,
      }),
    ).toBe(true);
    expect(isStockMovement(stockMovement)).toBe(true);
    expect(
      isStockMovementList({
        items: [stockMovement],
        count: 1,
        limit: 10,
        offset: 0,
      }),
    ).toBe(true);
    expect(
      isStockMovementCancelResponse({
        movement: {
          ...stockMovement,
          status: "cancelled",
          cancelled_at: "2026-06-29T12:10:00Z",
          cancelled_by: stockMovement.created_by,
          reversal_movement_id: "MOV-2",
        },
        reversal: {
          ...stockMovement,
          id: "MOV-2",
          type: "write_off",
        },
      }),
    ).toBe(true);
  });

  it("rejects incomplete stock responses", () => {
    expect(isStockMovement({ ...stockMovement, lines: [{ product_id: "DEMO-001" }] })).toBe(
      false,
    );
  });

  it("parses structured API errors and falls back for malformed errors", () => {
    expect(
      parseStockApiError({
        error: {
          code: "INSUFFICIENT_STOCK",
          message: "Недостаточно остатка",
          fields: { "lines.0.quantity": "Проверьте количество" },
        },
      }),
    ).toEqual({
      code: "INSUFFICIENT_STOCK",
      message: "Недостаточно остатка",
      fields: { "lines.0.quantity": "Проверьте количество" },
    });
    expect(parseStockApiError(null).code).toBe("REQUEST_ERROR");
  });
});
