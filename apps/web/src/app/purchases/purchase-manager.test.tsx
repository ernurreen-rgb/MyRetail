// @vitest-environment jsdom

import { act } from "react";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PurchaseManager } from "@/app/purchases/purchase-manager";
import type {
  Purchase,
  PurchaseOptions,
  PurchaseSummary,
  Supplier,
} from "@/lib/purchases";

const purchasesApi = vi.hoisted(() => ({
  archiveSupplier: vi.fn(),
  cancelPurchase: vi.fn(),
  createIdempotencyKey: vi.fn(),
  createPurchase: vi.fn(),
  createSupplier: vi.fn(),
  getPurchase: vi.fn(),
  getPurchaseOptions: vi.fn(),
  listPurchases: vi.fn(),
  listSuppliers: vi.fn(),
  submitPurchase: vi.fn(),
  updatePurchase: vi.fn(),
  updateSupplier: vi.fn(),
}));

const productsApi = vi.hoisted(() => ({
  listProducts: vi.fn(),
}));

vi.mock("@/lib/purchases", async () => {
  const actual = await vi.importActual<typeof import("@/lib/purchases")>("@/lib/purchases");

  return {
    ...actual,
    ...purchasesApi,
  };
});

vi.mock("@/lib/products", async () => {
  const actual = await vi.importActual<typeof import("@/lib/products")>("@/lib/products");

  return {
    ...actual,
    listProducts: productsApi.listProducts,
  };
});

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

const archivedSupplier: Supplier = {
  ...supplier,
  id: "SUP-ARCHIVE",
  name: "Архивный поставщик",
  is_active: false,
};

const options: PurchaseOptions = {
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

const purchaseSummary: PurchaseSummary = {
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
  subtotal: "1200.00",
  total: "1200.00",
  updated_at: "2026-07-04T00:00:00Z",
};

const draftPurchase: Purchase = {
  ...purchaseSummary,
  comment: null,
  created_by: {
    email: "owner@example.test",
    full_name: "Owner",
  },
  created_at: "2026-07-04T00:00:00Z",
  submitted_by: null,
  submitted_at: null,
  cancelled_by: null,
  cancelled_at: null,
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

const postedPurchase: Purchase = {
  ...draftPurchase,
  status: "posted",
  submitted_by: draftPurchase.created_by,
  submitted_at: "2026-07-04T00:10:00Z",
};

function suppliersSuccess(items: Supplier[] = [supplier]) {
  return {
    status: "success" as const,
    data: {
      items,
      count: items.length,
      limit: 10,
      offset: 0,
    },
  };
}

function purchasesSuccess(items: PurchaseSummary[] = [purchaseSummary]) {
  return {
    status: "success" as const,
    data: {
      items,
      count: items.length,
      limit: 10,
      offset: 0,
    },
  };
}

function renderManager(canManage = true) {
  return render(<PurchaseManager canManage={canManage} userRoles={canManage ? ["Owner"] : ["Cashier"]} />);
}

beforeEach(() => {
  purchasesApi.archiveSupplier.mockReset().mockResolvedValue({ status: "success", data: null });
  purchasesApi.cancelPurchase.mockReset();
  purchasesApi.createIdempotencyKey.mockReset().mockReturnValue("123e4567-e89b-42d3-a456-426614174000");
  purchasesApi.createPurchase.mockReset().mockResolvedValue({ status: "success", data: draftPurchase });
  purchasesApi.createSupplier.mockReset().mockResolvedValue({ status: "success", data: supplier });
  purchasesApi.getPurchase.mockReset().mockResolvedValue({ status: "success", data: draftPurchase });
  purchasesApi.getPurchaseOptions.mockReset().mockResolvedValue({ status: "success", data: options });
  purchasesApi.listPurchases.mockReset().mockResolvedValue(purchasesSuccess());
  purchasesApi.listSuppliers.mockReset().mockImplementation(
    async ({ status }: { status?: string } = {}) =>
      status === "all"
        ? suppliersSuccess([supplier, archivedSupplier])
        : status === "archived"
          ? suppliersSuccess([archivedSupplier])
          : suppliersSuccess([supplier]),
  );
  purchasesApi.submitPurchase.mockReset().mockResolvedValue({ status: "success", data: postedPurchase });
  purchasesApi.updatePurchase.mockReset().mockResolvedValue({ status: "success", data: draftPurchase });
  purchasesApi.updateSupplier.mockReset().mockResolvedValue({ status: "success", data: supplier });
  productsApi.listProducts.mockReset().mockResolvedValue({
    status: "success",
    data: {
      items: [
        {
          id: "QA-MILK-001",
          sku: "QA-MILK-001",
          name: "Молоко 3,2%",
          barcode: null,
          category: "Молочные",
          brand: null,
          unit: "Nos",
          sale_price: "800.00",
          purchase_price: "600.00",
          currency: "KZT",
          description: null,
          image_url: null,
          is_active: true,
        },
      ],
      count: 1,
      limit: 100,
      offset: 0,
    },
  });
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(() => {
  cleanup();
});

describe("PurchaseManager permissions and loading", () => {
  it("shows a Russian 403 screen without calling purchases APIs", () => {
    renderManager(false);

    expect(screen.getByRole("heading", { name: "Доступ к закупкам запрещён" })).toBeTruthy();
    expect(purchasesApi.listPurchases).not.toHaveBeenCalled();
    expect(purchasesApi.listSuppliers).not.toHaveBeenCalled();
  });

  it("renders suppliers and purchase history with server totals", async () => {
    renderManager();

    expect((await screen.findAllByText("ТОО Молоко")).length).toBeGreaterThan(0);
    expect(screen.getByText(/Итого 1200\.00 KZT/)).toBeTruthy();
    expect(screen.getAllByText("Черновик").length).toBeGreaterThan(0);
  });
});

describe("PurchaseManager supplier operations", () => {
  it("creates a supplier with an idempotency key and archives with confirmation", async () => {
    const user = userEvent.setup();

    renderManager();
    await screen.findAllByText("ТОО Молоко");

    await user.click(screen.getByRole("button", { name: "Новый поставщик" }));
    await user.type(screen.getByLabelText("Название *"), "Новый поставщик");
    await user.click(screen.getByRole("button", { name: "Сохранить поставщика" }));

    expect(purchasesApi.createSupplier).toHaveBeenCalledWith(
      expect.objectContaining({ name: "Новый поставщик" }),
      "123e4567-e89b-42d3-a456-426614174000",
    );

    await user.click(screen.getByRole("button", { name: "Архивировать" }));

    expect(window.confirm).toHaveBeenCalledWith("Архивировать поставщика «ТОО Молоко»?");
    expect(purchasesApi.archiveSupplier).toHaveBeenCalledWith("SUP-00001");
  });

  it("keeps archived suppliers out of the new purchase supplier selector", async () => {
    const user = userEvent.setup();

    renderManager();
    await screen.findAllByText("ТОО Молоко");
    await user.click(screen.getByRole("button", { name: "Новый черновик" }));

    const form = screen.getByRole("heading", { name: "Новый черновик закупки" }).closest("form");
    expect(form).toBeTruthy();
    const supplierSelect = within(form as HTMLElement).getByLabelText("Поставщик *");

    expect(within(supplierSelect).getByRole("option", { name: "ТОО Молоко" })).toBeTruthy();
    expect(within(supplierSelect).queryByRole("option", { name: /Архивный поставщик/ })).toBeNull();
  });
});

describe("PurchaseManager purchase operations", () => {
  it("creates a draft with string quantity and money", async () => {
    const user = userEvent.setup();

    renderManager();
    await screen.findAllByText("ТОО Молоко");
    await user.click(screen.getByRole("button", { name: "Новый черновик" }));

    const form = screen.getByRole("heading", { name: "Новый черновик закупки" }).closest("form");
    expect(form).toBeTruthy();

    await user.selectOptions(
      within(form as HTMLElement).getByLabelText("Товар 1"),
      "QA-MILK-001",
    );
    await user.type(within(form as HTMLElement).getByLabelText("Количество"), "2,000");
    await user.type(within(form as HTMLElement).getByLabelText("Цена закупки"), "600,00");
    await user.click(within(form as HTMLElement).getByRole("button", { name: "Сохранить черновик" }));

    expect(purchasesApi.createPurchase).toHaveBeenCalledWith(
      expect.objectContaining({
        lines: [
          {
            product_id: "QA-MILK-001",
            quantity: "2,000",
            unit_price: "600,00",
          },
        ],
      }),
      "123e4567-e89b-42d3-a456-426614174000",
    );
  });

  it("blocks repeated submit while the idempotent submit is pending", async () => {
    let resolveSubmit:
      | ((value: { status: "success"; data: Purchase }) => void)
      | undefined;
    purchasesApi.submitPurchase.mockReturnValue(
      new Promise((resolve) => {
        resolveSubmit = resolve;
      }),
    );
    const user = userEvent.setup();

    renderManager();
    await screen.findAllByText("ТОО Молоко");
    await user.click(screen.getByRole("button", { name: "Открыть" }));
    expect(await screen.findByText("Молоко 3,2%")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Провести" }));
    const pendingButton = screen.getByRole("button", { name: "Проводим…" });
    expect(pendingButton).toHaveProperty("disabled", true);
    await user.click(pendingButton);

    expect(purchasesApi.submitPurchase).toHaveBeenCalledTimes(1);
    expect(purchasesApi.submitPurchase).toHaveBeenCalledWith(
      "PUR-00001",
      "2026-07-04T00:00:00Z",
      "123e4567-e89b-42d3-a456-426614174000",
    );

    await act(async () => {
      resolveSubmit?.({ status: "success", data: postedPurchase });
    });

    expect(await screen.findByText("Закупка PUR-00001 проведена.")).toBeTruthy();
  });

  it("surfaces version conflicts during draft edit", async () => {
    purchasesApi.updatePurchase.mockResolvedValue({
      status: "error",
      statusCode: 409,
      error: {
        code: "PURCHASE_CHANGED",
        message: "Документ изменён другим пользователем.",
        fields: { expected_updated_at: "Обновите документ" },
      },
    });
    const user = userEvent.setup();

    renderManager();
    await screen.findAllByText("ТОО Молоко");
    await user.click(screen.getByRole("button", { name: "Открыть" }));
    expect(await screen.findByText("Молоко 3,2%")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Редактировать черновик" }));

    const form = screen.getByRole("heading", { name: "Редактирование черновика" }).closest("form");
    expect(form).toBeTruthy();
    await user.type(within(form as HTMLElement).getByLabelText("Комментарий"), "новый комментарий");
    await user.click(within(form as HTMLElement).getByRole("button", { name: "Сохранить черновик" }));

    expect(await screen.findByText("Документ изменён другим пользователем.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Обновить документ" })).toBeTruthy();
  });
});
