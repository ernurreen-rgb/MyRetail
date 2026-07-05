// @vitest-environment jsdom

import { act } from "react";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
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

const secondSupplier: Supplier = {
  ...supplier,
  id: "SUP-00002",
  name: "ТОО Сыр",
  tax_id: "987654321098",
  updated_at: "2026-07-04T00:20:00Z",
};

const milkProduct = {
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
};

const cheeseProduct = {
  ...milkProduct,
  id: "QA-CHEESE-001",
  sku: "QA-CHEESE-001",
  name: "Сыр гауда",
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

function getLookupControl(label: string) {
  const input = screen.getByLabelText(label);
  const control = input.closest("div")?.parentElement;
  expect(control).toBeTruthy();
  return { input, control: control as HTMLElement };
}

async function searchLookup(user: ReturnType<typeof userEvent.setup>, label: string, value: string) {
  const { input, control } = getLookupControl(label);
  await user.clear(input);
  await user.type(input, value);
  await user.click(within(control).getByRole("button", { name: "Найти" }));
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
    async ({
      status,
      limit = 10,
      offset = 0,
    }: {
      status?: string;
      limit?: number;
      offset?: number;
    } = {}) => {
      const items =
        status === "all"
          ? [supplier, archivedSupplier]
          : status === "archived"
            ? [archivedSupplier]
            : [supplier];

      return {
        status: "success" as const,
        data: {
          items,
          count: items.length,
          limit,
          offset,
        },
      };
    },
  );
  purchasesApi.submitPurchase.mockReset().mockResolvedValue({ status: "success", data: postedPurchase });
  purchasesApi.updatePurchase.mockReset().mockResolvedValue({ status: "success", data: draftPurchase });
  purchasesApi.updateSupplier.mockReset().mockResolvedValue({ status: "success", data: supplier });
  productsApi.listProducts.mockReset().mockResolvedValue({
    status: "success",
    data: {
      items: [milkProduct],
      count: 1,
      limit: 20,
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
  it("searches, paginates and edits suppliers on the server", async () => {
    purchasesApi.listSuppliers.mockImplementation(
      async ({
        q = "",
        status,
        limit = 10,
        offset = 0,
      }: {
        q?: string;
        status?: string;
        limit?: number;
        offset?: number;
      } = {}) => {
        if (status === "all") {
          return {
            status: "success" as const,
            data: { items: [supplier, archivedSupplier], count: 2, limit, offset },
          };
        }
        if (q === "сыр" || offset > 0) {
          return {
            status: "success" as const,
            data: { items: [secondSupplier], count: 21, limit, offset },
          };
        }

        return {
          status: "success" as const,
          data: { items: [supplier], count: 21, limit, offset },
        };
      },
    );
    purchasesApi.updateSupplier.mockResolvedValueOnce({
      status: "success",
      data: { ...secondSupplier, phone: "+7 777 000 11 22" },
    });
    const user = userEvent.setup();

    renderManager();
    await screen.findAllByText(supplier.name);

    const supplierSection = screen.getByRole("heading", { name: "Поставщики" }).closest("section");
    expect(supplierSection).toBeTruthy();

    await user.click(within(supplierSection as HTMLElement).getByRole("button", { name: "Вперёд" }));
    expect(purchasesApi.listSuppliers).toHaveBeenCalledWith(
      expect.objectContaining({ status: "active", limit: 10, offset: 10 }),
    );

    await user.clear(within(supplierSection as HTMLElement).getByLabelText("Поиск поставщика"));
    await user.type(within(supplierSection as HTMLElement).getByLabelText("Поиск поставщика"), "сыр");
    await user.click(within(supplierSection as HTMLElement).getByRole("button", { name: "Найти" }));
    expect(purchasesApi.listSuppliers).toHaveBeenCalledWith(
      expect.objectContaining({ q: "сыр", status: "active", limit: 10, offset: 0 }),
    );

    await screen.findByText(secondSupplier.name);
    await user.click(within(supplierSection as HTMLElement).getByRole("button", { name: "Редактировать" }));

    const form = screen.getByRole("heading", { name: "Редактирование поставщика" }).closest("form");
    expect(form).toBeTruthy();
    await user.clear(within(form as HTMLElement).getByLabelText("Телефон"));
    await user.type(within(form as HTMLElement).getByLabelText("Телефон"), "+7 777 000 11 22");
    await user.click(within(form as HTMLElement).getByRole("button", { name: "Сохранить поставщика" }));

    expect(purchasesApi.updateSupplier).toHaveBeenCalledWith(
      secondSupplier.id,
      expect.objectContaining({ phone: "+7 777 000 11 22" }),
      secondSupplier.updated_at,
    );
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

describe("PurchaseManager purchase review regressions", () => {
  it("searches and paginates purchase supplier and product lookups server-side", async () => {
    purchasesApi.listSuppliers.mockImplementation(
      async ({
        q = "",
        status,
        limit = 10,
        offset = 0,
      }: {
        q?: string;
        status?: string;
        limit?: number;
        offset?: number;
      } = {}) => {
        const activeItems = q === "сыр" || offset > 0 ? [secondSupplier] : [supplier];
        const items =
          status === "all"
            ? q === "сыр" || offset > 0
              ? [secondSupplier]
              : [supplier, archivedSupplier]
            : status === "archived"
              ? [archivedSupplier]
              : activeItems;

        return {
          status: "success" as const,
          data: { items, count: 41, limit, offset },
        };
      },
    );
    productsApi.listProducts.mockImplementation(
      async ({
        q = "",
        limit = 20,
        offset = 0,
      }: {
        q?: string;
        limit?: number;
        offset?: number;
      } = {}) => ({
        status: "success" as const,
        data: {
          items: q === "сыр" || offset > 0 ? [cheeseProduct] : [milkProduct],
          count: 41,
          limit,
          offset,
        },
      }),
    );
    const user = userEvent.setup();

    renderManager();
    await screen.findAllByText(supplier.name);
    expect(purchasesApi.listSuppliers).toHaveBeenCalledWith(
      expect.objectContaining({ status: "active", limit: 20, offset: 0 }),
    );
    expect(purchasesApi.listSuppliers).toHaveBeenCalledWith(
      expect.objectContaining({ status: "all", limit: 20, offset: 0 }),
    );
    expect(productsApi.listProducts).toHaveBeenCalledWith(
      expect.objectContaining({ limit: 20, offset: 0 }),
    );

    const newDraftButton = screen.getByRole("button", { name: "Новый черновик" });
    await waitFor(() => expect(newDraftButton).not.toHaveProperty("disabled", true));
    await user.click(newDraftButton);
    await screen.findByRole("heading", { name: "Новый черновик закупки" });

    const activeSupplierLookup = getLookupControl("Поиск активного поставщика для закупки");
    await user.click(within(activeSupplierLookup.control).getByRole("button", { name: "Вперёд" }));
    expect(purchasesApi.listSuppliers).toHaveBeenCalledWith(
      expect.objectContaining({ status: "active", limit: 20, offset: 20 }),
    );

    await searchLookup(user, "Поиск активного поставщика для закупки", "сыр");
    expect(purchasesApi.listSuppliers).toHaveBeenCalledWith(
      expect.objectContaining({ q: "сыр", status: "active", limit: 20, offset: 0 }),
    );

    const productLookup = getLookupControl("Поиск товара для строк");
    await user.click(within(productLookup.control).getByRole("button", { name: "Вперёд" }));
    expect(productsApi.listProducts).toHaveBeenCalledWith(
      expect.objectContaining({ limit: 20, offset: 20 }),
    );

    await searchLookup(user, "Поиск товара для строк", "сыр");
    expect(productsApi.listProducts).toHaveBeenCalledWith(
      expect.objectContaining({ q: "сыр", limit: 20, offset: 0 }),
    );
  });

  it("warns about duplicate supplier invoice numbers without blocking the draft", async () => {
    const user = userEvent.setup();

    renderManager();
    await screen.findAllByText(supplier.name);
    const newDraftButton = screen.getByRole("button", { name: "Новый черновик" });
    await waitFor(() => expect(newDraftButton).not.toHaveProperty("disabled", true));
    await user.click(newDraftButton);

    const form = screen.getByRole("heading", { name: "Новый черновик закупки" }).closest("form");
    expect(form).toBeTruthy();
    await user.type(within(form as HTMLElement).getByLabelText("Номер накладной"), "INV-42");
    await user.click(within(form as HTMLElement).getByRole("button", { name: "Проверить номер" }));

    expect(
      await screen.findByText(/уже есть закупка PUR-00001 с номером накладной INV-42/),
    ).toBeTruthy();
  });

  it("refreshes a changed purchase form and saves with the new updated_at", async () => {
    const refreshedDraft: Purchase = {
      ...draftPurchase,
      comment: "Серверная версия",
      updated_at: "2026-07-04T00:30:00Z",
    };
    purchasesApi.getPurchase
      .mockResolvedValueOnce({ status: "success", data: draftPurchase })
      .mockResolvedValueOnce({ status: "success", data: refreshedDraft });
    purchasesApi.updatePurchase
      .mockResolvedValueOnce({
        status: "error",
        statusCode: 409,
        error: {
          code: "PURCHASE_CHANGED",
          message: "Документ изменён другим пользователем.",
          fields: { expected_updated_at: "Обновите документ" },
        },
      })
      .mockResolvedValueOnce({ status: "success", data: refreshedDraft });
    const user = userEvent.setup();

    renderManager();
    await screen.findAllByText(supplier.name);
    await user.click(screen.getByRole("button", { name: "Открыть" }));
    await screen.findByText("Молоко 3,2%");
    await user.click(screen.getByRole("button", { name: "Редактировать черновик" }));

    let form = screen.getByRole("heading", { name: "Редактирование черновика" }).closest("form");
    expect(form).toBeTruthy();
    await user.type(within(form as HTMLElement).getByLabelText("Комментарий"), "локально");
    await user.click(within(form as HTMLElement).getByRole("button", { name: "Сохранить черновик" }));

    expect(await screen.findByText("Документ изменён другим пользователем.")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Обновить документ" }));
    expect(await screen.findByText(/форма пересобрана/)).toBeTruthy();

    form = screen.getByRole("heading", { name: "Редактирование черновика" }).closest("form");
    expect(form).toBeTruthy();
    await user.type(within(form as HTMLElement).getByLabelText("Комментарий"), " после refresh");
    await user.click(within(form as HTMLElement).getByRole("button", { name: "Сохранить черновик" }));

    expect(purchasesApi.updatePurchase).toHaveBeenNthCalledWith(
      1,
      "PUR-00001",
      expect.any(Object),
      "2026-07-04T00:00:00Z",
    );
    expect(purchasesApi.updatePurchase).toHaveBeenNthCalledWith(
      2,
      "PUR-00001",
      expect.any(Object),
      "2026-07-04T00:30:00Z",
    );
  });

  it("filters purchases, opens detail and cancels a posted purchase", async () => {
    const cancelledPurchase: Purchase = {
      ...postedPurchase,
      status: "cancelled",
      cancelled_by: postedPurchase.created_by,
      cancelled_at: "2026-07-04T00:20:00Z",
    };
    purchasesApi.getPurchase.mockResolvedValue({ status: "success", data: postedPurchase });
    purchasesApi.cancelPurchase.mockResolvedValue({ status: "success", data: cancelledPurchase });
    const user = userEvent.setup();

    renderManager();
    await screen.findAllByText(supplier.name);

    const purchaseSection = screen.getByRole("heading", { name: "Закупки" }).closest("section");
    expect(purchaseSection).toBeTruthy();
    await user.selectOptions(within(purchaseSection as HTMLElement).getByLabelText("Поставщик"), supplier.id);
    await user.selectOptions(within(purchaseSection as HTMLElement).getByLabelText("Статус"), "posted");
    await user.type(within(purchaseSection as HTMLElement).getByLabelText("Дата от"), "2026-07-01");
    await user.type(within(purchaseSection as HTMLElement).getByLabelText("Дата до"), "2026-07-31");

    expect(purchasesApi.listPurchases).toHaveBeenCalledWith(
      expect.objectContaining({
        supplierId: supplier.id,
        status: "posted",
        dateFrom: "2026-07-01",
        dateTo: "2026-07-31",
        limit: 10,
        offset: 0,
      }),
    );

    await user.click(within(purchaseSection as HTMLElement).getByRole("button", { name: "Открыть" }));
    expect(await screen.findByText("Молоко 3,2%")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Отменить" }));

    const cancelFormElement = screen.getByRole("heading", { name: /Отмена закупки PUR-00001/ }).closest("form");
    expect(cancelFormElement).toBeTruthy();
    await user.type(within(cancelFormElement as HTMLElement).getByLabelText("Причина отмены *"), "Ошибка поставки");
    await user.click(
      within(cancelFormElement as HTMLElement).getByRole("button", { name: "Подтвердить отмену" }),
    );

    expect(purchasesApi.cancelPurchase).toHaveBeenCalledWith(
      "PUR-00001",
      "Ошибка поставки",
      "123e4567-e89b-42d3-a456-426614174000",
    );
  });

  it("shows purchase loading, empty, error and retry states", async () => {
    let resolvePurchases:
      | ((value: ReturnType<typeof purchasesSuccess>) => void)
      | undefined;
    purchasesApi.listPurchases.mockReturnValueOnce(
      new Promise((resolve) => {
        resolvePurchases = resolve;
      }),
    );
    const user = userEvent.setup();

    renderManager();
    expect(await screen.findByText("Загружаем закупки…")).toBeTruthy();

    await act(async () => {
      resolvePurchases?.(purchasesSuccess([]));
    });
    expect(await screen.findByText("Закупок по текущим фильтрам пока нет.")).toBeTruthy();

    purchasesApi.listPurchases
      .mockResolvedValueOnce({
        status: "error",
        statusCode: 503,
        error: { code: "ERP_UNAVAILABLE", message: "История недоступна", fields: {} },
      })
      .mockResolvedValueOnce(purchasesSuccess());

    const purchaseSection = screen.getByRole("heading", { name: "Закупки" }).closest("section");
    expect(purchaseSection).toBeTruthy();
    const purchaseFilterForm = within(purchaseSection as HTMLElement)
      .getByLabelText("Поиск")
      .closest("form");
    expect(purchaseFilterForm).toBeTruthy();
    await user.type(within(purchaseFilterForm as HTMLElement).getByLabelText("Поиск"), "INV");
    await user.click(within(purchaseFilterForm as HTMLElement).getByRole("button", { name: "Найти" }));

    const errorMessage = await screen.findByText("История недоступна");
    const errorBox = errorMessage.closest("div");
    expect(errorBox).toBeTruthy();
    await user.click(within(errorBox as HTMLElement).getByRole("button", { name: "Повторить запрос" }));

    expect(await screen.findByText(/Итого 1200\.00 KZT/)).toBeTruthy();
    expect(purchasesApi.listPurchases).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: "INV", limit: 10, offset: 0 }),
    );
  });

  it("retries failed draft creation with the same idempotency key", async () => {
    let keyIndex = 0;
    purchasesApi.createIdempotencyKey.mockImplementation(() => `retry-key-${++keyIndex}`);
    purchasesApi.createPurchase
      .mockResolvedValueOnce({
        status: "error",
        statusCode: 503,
        error: { code: "ERP_UNAVAILABLE", message: "ERPNext недоступен", fields: {} },
      })
      .mockResolvedValueOnce({ status: "success", data: draftPurchase });
    const user = userEvent.setup();

    renderManager();
    await screen.findAllByText(supplier.name);
    const newDraftButton = screen.getByRole("button", { name: "Новый черновик" });
    await waitFor(() => expect(newDraftButton).not.toHaveProperty("disabled", true));
    await user.click(newDraftButton);

    const form = screen.getByRole("heading", { name: "Новый черновик закупки" }).closest("form");
    expect(form).toBeTruthy();
    await user.selectOptions(within(form as HTMLElement).getByLabelText("Товар 1"), "QA-MILK-001");
    await user.type(within(form as HTMLElement).getByLabelText("Количество"), "2.000");
    await user.type(within(form as HTMLElement).getByLabelText("Цена закупки"), "600.00");
    await user.click(within(form as HTMLElement).getByRole("button", { name: "Сохранить черновик" }));

    expect(await screen.findByText("ERPNext недоступен")).toBeTruthy();
    await user.click(within(form as HTMLElement).getByRole("button", { name: "Сохранить черновик" }));

    expect(purchasesApi.createPurchase).toHaveBeenCalledTimes(2);
    expect(purchasesApi.createPurchase.mock.calls[1][1]).toBe(
      purchasesApi.createPurchase.mock.calls[0][1],
    );
  });
});
