// @vitest-environment jsdom

import { act } from "react";
import {
  cleanup,
  render,
  screen,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { StockManager } from "@/app/stock/stock-manager";
import type {
  StockBalance,
  StockMovement,
  StockOptions,
} from "@/lib/stock";

const stockApi = vi.hoisted(() => ({
  getStockOptions: vi.fn(),
  listStockBalances: vi.fn(),
  listStockMovements: vi.fn(),
  createStockMovement: vi.fn(),
  cancelStockMovement: vi.fn(),
  createIdempotencyKey: vi.fn(),
}));

vi.mock("@/lib/stock", async () => {
  const actual = await vi.importActual<typeof import("@/lib/stock")>("@/lib/stock");

  return {
    ...actual,
    ...stockApi,
  };
});

const primaryWarehouse = {
  id: "Основной склад QA - MRD",
  name: "Основной склад QA",
  is_default: true,
  is_active: true,
};

const reserveWarehouse = {
  id: "Резервный склад QA - MRD",
  name: "Резервный склад QA",
  is_default: false,
  is_active: true,
};

const stockOptions: StockOptions = {
  warehouses: [primaryWarehouse, reserveWarehouse],
  write_off_reasons: [
    { code: "damage", name: "Порча" },
    { code: "other", name: "Другое" },
  ],
  adjustment_reasons: [
    { code: "manual_count", name: "Ручной пересчёт" },
  ],
};

const milkBalance: StockBalance = {
  product_id: "QA-MILK-001",
  sku: "QA-MILK-001",
  name: "Молоко 3,2%",
  unit: "Nos",
  warehouse: {
    id: primaryWarehouse.id,
    name: primaryWarehouse.name,
  },
  on_hand: "10.000",
  reserved: "2.000",
  available: "8.000",
  updated_at: "2026-06-29T12:00:00Z",
};

const cheeseBalance: StockBalance = {
  product_id: "QA-CHEESE-001",
  sku: "QA-CHEESE-001",
  name: "Сыр весовой",
  unit: "Kg",
  warehouse: {
    id: reserveWarehouse.id,
    name: reserveWarehouse.name,
  },
  on_hand: "1.250",
  reserved: "0.000",
  available: "1.250",
  updated_at: "2026-06-29T12:05:00Z",
};

const receiptMovement: StockMovement = {
  id: "MAT-STE-2026-00001",
  type: "receipt",
  status: "posted",
  warehouse_id: primaryWarehouse.id,
  destination_warehouse_id: null,
  reason_code: null,
  comment: null,
  created_by: {
    email: "owner@example.test",
    full_name: "Owner",
  },
  created_at: "2026-06-29T12:10:00Z",
  cancelled_by: null,
  cancelled_at: null,
  reversal_movement_id: null,
  lines: [
    {
      product_id: milkBalance.product_id,
      quantity: "1.000",
      before_quantity: "10.000",
      after_quantity: "11.000",
    },
  ],
};

function optionsSuccess(options = stockOptions) {
  return {
    status: "success" as const,
    data: options,
  };
}

function balancesSuccess(
  items: StockBalance[] = [milkBalance],
  count = items.length,
  offset = 0,
) {
  return {
    status: "success" as const,
    data: {
      items,
      count,
      limit: 20,
      offset,
    },
  };
}

function movementsSuccess(items: StockMovement[] = []) {
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

function apiError(
  message: string,
  fields: Record<string, string> = {},
  statusCode = 409,
) {
  return {
    status: "error" as const,
    statusCode,
    error: {
      code: statusCode === 403 ? "FORBIDDEN" : "INSUFFICIENT_STOCK",
      message,
      fields,
    },
  };
}

function renderManager({
  canManage = true,
  roles = ["Owner"],
}: {
  canManage?: boolean;
  roles?: string[];
} = {}) {
  return render(<StockManager canManage={canManage} userRoles={roles} />);
}

beforeEach(() => {
  stockApi.getStockOptions.mockReset().mockResolvedValue(optionsSuccess());
  stockApi.listStockBalances.mockReset().mockResolvedValue(balancesSuccess());
  stockApi.listStockMovements.mockReset().mockResolvedValue(movementsSuccess());
  stockApi.createStockMovement
    .mockReset()
    .mockResolvedValue({ status: "success", data: receiptMovement });
  stockApi.cancelStockMovement.mockReset();
  stockApi.createIdempotencyKey
    .mockReset()
    .mockReturnValue("123e4567-e89b-42d3-a456-426614174000");
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(() => {
  cleanup();
});

describe("StockManager loading and failures", () => {
  it("shows loading, then renders exact stock quantities", async () => {
    let resolveBalances: ((value: ReturnType<typeof balancesSuccess>) => void) | undefined;
    stockApi.listStockBalances.mockReturnValue(
      new Promise((resolve) => {
        resolveBalances = resolve;
      }),
    );

    renderManager();

    expect(screen.getByText("Загружаем остатки…")).toBeTruthy();

    await act(async () => {
      resolveBalances?.(balancesSuccess());
    });

    expect(await screen.findByText("Молоко 3,2%")).toBeTruthy();
    expect(screen.getByText("10.000")).toBeTruthy();
    expect(screen.getByText("2.000")).toBeTruthy();
    expect(screen.getByText("8.000")).toBeTruthy();
  });

  it("shows an empty state", async () => {
    stockApi.listStockBalances.mockResolvedValue(balancesSuccess([]));

    renderManager();

    expect(await screen.findByText("Остатков пока нет")).toBeTruthy();
  });

  it("retries a failed balances request", async () => {
    stockApi.listStockBalances
      .mockResolvedValueOnce(apiError("ERPNext временно недоступен", {}, 503))
      .mockResolvedValueOnce(balancesSuccess());
    const user = userEvent.setup();

    renderManager();

    expect(await screen.findByText("ERPNext временно недоступен")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Повторить запрос" }));

    expect(await screen.findByText("Молоко 3,2%")).toBeTruthy();
    expect(stockApi.listStockBalances).toHaveBeenCalledTimes(2);
  });
});

describe("StockManager filters and pagination", () => {
  it("sends search and warehouse filters to the API", async () => {
    const user = userEvent.setup();

    renderManager();
    await screen.findByText("Молоко 3,2%");

    await user.type(
      screen.getByRole("searchbox", {
        name: "Поиск по названию, артикулу или штрихкоду",
      }),
      "QA-MILK-001",
    );
    await user.click(screen.getByRole("button", { name: "Найти" }));

    expect(stockApi.listStockBalances).toHaveBeenLastCalledWith({
      q: "QA-MILK-001",
      warehouseId: "",
      limit: 20,
      offset: 0,
    });

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Склад" }),
      primaryWarehouse.id,
    );

    expect(stockApi.listStockBalances).toHaveBeenLastCalledWith({
      q: "QA-MILK-001",
      warehouseId: primaryWarehouse.id,
      limit: 20,
      offset: 0,
    });
  });

  it("loads the next server page", async () => {
    stockApi.listStockBalances.mockImplementation(
      async ({ offset = 0 }: { offset?: number }) =>
        offset === 20
          ? balancesSuccess([cheeseBalance], 21, 20)
          : balancesSuccess([milkBalance], 21, 0),
    );
    const user = userEvent.setup();

    renderManager();
    await screen.findByText("Молоко 3,2%");
    await user.click(screen.getByRole("button", { name: "Вперёд" }));

    expect(await screen.findByText("Сыр весовой")).toBeTruthy();
    expect(stockApi.listStockBalances).toHaveBeenLastCalledWith({
      q: "",
      warehouseId: "",
      limit: 20,
      offset: 20,
    });
  });
});

describe("StockManager permissions and mutations", () => {
  it("keeps Cashier read-only while preserving balances and history", async () => {
    renderManager({ canManage: false, roles: ["Cashier"] });

    expect(await screen.findByText("Молоко 3,2%")).toBeTruthy();
    expect(screen.getByText(/Режим просмотра/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Новая операция" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Отменить" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Движения склада" })).toBeTruthy();
  });

  it("hides transfer completely when only one active warehouse exists", async () => {
    stockApi.getStockOptions.mockResolvedValue(
      optionsSuccess({
        ...stockOptions,
        warehouses: [primaryWarehouse],
      }),
    );
    const user = userEvent.setup();

    renderManager();

    await screen.findByText("Молоко 3,2%");
    expect(await screen.findByText(/Перемещение недоступно/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Перемещение" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Оприходование" }));
    const typeSelect = screen.getByRole("combobox", { name: "Тип операции" });

    expect(within(typeSelect).queryByRole("option", { name: "Перемещение" })).toBeNull();
  });

  it("blocks repeated submission while a movement is pending", async () => {
    let resolveMovement:
      | ((value: { status: "success"; data: StockMovement }) => void)
      | undefined;
    stockApi.createStockMovement.mockReturnValue(
      new Promise((resolve) => {
        resolveMovement = resolve;
      }),
    );
    const user = userEvent.setup();

    renderManager();
    await screen.findByText("Молоко 3,2%");
    await user.click(screen.getByRole("button", { name: "Оприходование" }));
    await user.type(screen.getByRole("textbox", { name: "Количество" }), "1.000");

    await user.click(screen.getByRole("button", { name: "Провести операцию" }));

    const savingButton = screen.getByRole("button", { name: "Сохраняем…" });
    expect(savingButton).toHaveProperty("disabled", true);
    await user.click(savingButton);
    expect(stockApi.createStockMovement).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveMovement?.({ status: "success", data: receiptMovement });
    });

    expect(await screen.findByText("Операция «Оприходование» проведена.")).toBeTruthy();
  });

  it("shows a mutation error next to the affected field", async () => {
    stockApi.createStockMovement.mockResolvedValue(
      apiError("Недостаточно доступного остатка.", {
        "lines.0.quantity": "Доступно 8.000",
      }),
    );
    const user = userEvent.setup();

    renderManager();
    await screen.findByText("Молоко 3,2%");
    await user.click(screen.getByRole("button", { name: "Списание" }));
    await user.type(screen.getByRole("textbox", { name: "Количество" }), "9.000");
    await user.selectOptions(screen.getByRole("combobox", { name: "Причина" }), "damage");
    await user.click(screen.getByRole("button", { name: "Провести операцию" }));

    expect(await screen.findByText("Недостаточно доступного остатка.")).toBeTruthy();
    expect(screen.getByText("Доступно 8.000")).toBeTruthy();
    expect(window.confirm).toHaveBeenCalledOnce();
  });
});
