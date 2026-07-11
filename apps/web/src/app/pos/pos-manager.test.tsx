// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POSManager } from "@/app/pos/pos-manager";
import type {
  HeldReceipt,
  POSOptions,
  POSProduct,
  POSReturn,
  ReturnList,
  ReturnOptions,
  Sale,
  Shift,
} from "@/lib/pos";

const posApi = vi.hoisted(() => ({
  cancelReturn: vi.fn(),
  closeShift: vi.fn(),
  createHeldReceipt: vi.fn(),
  createIdempotencyKey: vi.fn(),
  createReturn: vi.fn(),
  createSale: vi.fn(),
  deleteHeldReceipt: vi.fn(),
  getCurrentShift: vi.fn(),
  getPOSOptions: vi.fn(),
  getReturn: vi.fn(),
  getReturnOptions: vi.fn(),
  getSale: vi.fn(),
  listHeldReceipts: vi.fn(),
  listPOSProducts: vi.fn(),
  listReturns: vi.fn(),
  listSales: vi.fn(),
  openShift: vi.fn(),
  updateHeldReceipt: vi.fn(),
}));

vi.mock("@/lib/pos", async () => {
  const actual = await vi.importActual<typeof import("@/lib/pos")>("@/lib/pos");

  return {
    ...actual,
    ...posApi,
  };
});

const options: POSOptions = {
  registers: [
    {
      id: "REG-1",
      name: "Касса 1",
      warehouse: { id: "WH-1", name: "Основной склад" },
      currency: "KZT",
      payment_methods: ["cash"],
      is_active: true,
    },
    {
      id: "REG-2",
      name: "Касса 2",
      warehouse: { id: "WH-2", name: "Второй склад" },
      currency: "KZT",
      payment_methods: ["cash"],
      is_active: true,
    },
  ],
  payment_methods: [{ code: "cash", name: "Наличные" }],
  discount_limit_percent: "10.00",
};

const milk: POSProduct = {
  id: "QA-MILK-001",
  sku: "QA-MILK-001",
  name: "Молоко 3,2%",
  barcode: "460123",
  unit: "шт",
  sale_price: "800.00",
  currency: "KZT",
  available: "5.000",
  is_active: true,
  allows_fractional_quantity: false,
};

const shift: Shift = {
  id: "SHIFT-1",
  register: { id: "REG-1", name: "Касса 1" },
  warehouse: { id: "WH-1", name: "Основной склад" },
  cashier: { email: "cashier@example.test", full_name: "Miko Cashier" },
  status: "open",
  opening_cash: "500.00",
  sales_total: "1000.00",
  expected_cash: "1500.00",
  actual_cash: null,
  difference: null,
  opened_at: "2026-07-08T09:00:00Z",
  closed_at: null,
  updated_at: "2026-07-08T09:00:00Z",
};

const closedShift: Shift = {
  ...shift,
  status: "closed",
  actual_cash: "1500.00",
  difference: "0.00",
  closed_at: "2026-07-08T18:00:00Z",
  updated_at: "2026-07-08T18:00:00Z",
};

const sale: Sale = {
  id: "SALE-1",
  receipt_number: "POS-0001",
  status: "completed",
  shift_id: shift.id,
  register: shift.register,
  warehouse: shift.warehouse,
  cashier: shift.cashier,
  currency: "KZT",
  lines: [
    {
      line_id: "LINE-1",
      product_id: milk.id,
      sku: milk.sku,
      name: milk.name,
      unit: milk.unit,
      quantity: "1.000",
      unit_price: "800.00",
      subtotal: "800.00",
      discount_percent: "0.00",
      discount_amount: "0.00",
      total: "800.00",
      returned_quantity: "0.000",
      available_to_return_quantity: "1.000",
    },
  ],
  subtotal: "800.00",
  discount_total: "0.00",
  grand_total: "800.00",
  cash_received: "1000.00",
  change: "200.00",
  created_at: "2026-07-08T10:00:00Z",
  return_status: "none",
  returned_total: "0.00",
};

const returnOptions: ReturnOptions = {
  sale_id: sale.id,
  receipt_number: sale.receipt_number,
  status: "submitted",
  return_status: "none",
  register_id: "REG-1",
  shift_id: shift.id,
  cashier_email: "cashier@example.test",
  created_at: sale.created_at,
  currency: "KZT",
  lines: [
    {
      line_id: "LINE-1",
      item_id: milk.id,
      item_name: milk.name,
      sold_quantity: "1.000",
      already_returned_quantity: "0.000",
      available_to_return_quantity: "1.000",
      unit: milk.unit,
      unit_price: "800.00",
      line_total: "800.00",
    },
  ],
  totals: {
    refund_total: "800.00",
    sold_total: "800.00",
    already_returned_total: "0.00",
    available_to_return_total: "800.00",
  },
};

const posReturn: POSReturn = {
  return_id: "RETURN-1",
  sale_id: sale.id,
  receipt_number: sale.receipt_number,
  return_receipt_number: "RET-0001",
  state: "submitted",
  return_status_after: "full",
  refund_method: "cash",
  reason: "customer_request",
  comment: "Клиент передумал",
  currency: "KZT",
  register_id: "REG-1",
  shift_id: shift.id,
  lines: [
    {
      line_id: "LINE-1",
      item_id: milk.id,
      item_name: milk.name,
      quantity: "1.000",
      unit: milk.unit,
      unit_price: "800.00",
      line_total: "800.00",
    },
  ],
  totals: {
    refund_total: "800.00",
    sold_total: null,
    already_returned_total: null,
    available_to_return_total: null,
  },
  created_by: "cashier@example.test",
  created_at: "2026-07-08T10:05:00Z",
  cancelled_by: null,
  cancelled_at: null,
};

const returnList: ReturnList = {
  items: [
    {
      return_id: posReturn.return_id,
      sale_id: posReturn.sale_id,
      receipt_number: posReturn.receipt_number,
      return_receipt_number: posReturn.return_receipt_number,
      state: posReturn.state,
      refund_total: posReturn.totals.refund_total,
      currency: posReturn.currency,
      register_id: posReturn.register_id,
      shift_id: posReturn.shift_id,
      cashier_email: "cashier@example.test",
      created_at: posReturn.created_at,
    },
  ],
  count: 1,
  limit: 10,
  offset: 0,
};

const held: HeldReceipt = {
  id: "HELD-1",
  shift_id: shift.id,
  label: "Клиент вернётся",
  lines: sale.lines,
  subtotal: "800.00",
  discount_total: "0.00",
  grand_total: "800.00",
  created_by: shift.cashier,
  created_at: "2026-07-08T09:20:00Z",
  updated_at: "2026-07-08T09:20:00Z",
};

function success<T>(data: T) {
  return { status: "success" as const, data };
}

function error(code: string, message: string, statusCode = 409, fields: Record<string, string> = {}) {
  return {
    status: "error" as const,
    statusCode,
    error: { code, message, fields },
  };
}

function renderPOS(canUsePOS = true, userRoles = canUsePOS ? ["Cashier"] : ["Warehouse Clerk"]) {
  return render(
    <POSManager
      canUsePOS={canUsePOS}
      userRoles={userRoles}
      userEmail="cashier@example.test"
    />,
  );
}

async function renderReady() {
  renderPOS();
  await screen.findByRole("heading", { name: "Касса MyRetail" });
  await waitFor(() => expect(posApi.getPOSOptions).toHaveBeenCalled());
  await waitFor(() => expect(posApi.getCurrentShift).toHaveBeenCalled());
  await screen.findByText("Смена открыта");
}

async function scanMilk(user: ReturnType<typeof userEvent.setup>) {
  const scanner = screen.getByLabelText("Сканер штрихкода");
  await user.clear(scanner);
  await user.type(scanner, "460123{Enter}");
  await screen.findByText("QA-MILK-001");
  return scanner;
}

beforeEach(() => {
  posApi.cancelReturn.mockReset().mockResolvedValue(
    success({
      ...posReturn,
      state: "cancelled",
      cancelled_by: "owner@example.test",
      cancelled_at: "2026-07-08T10:10:00Z",
    }),
  );
  posApi.closeShift.mockReset().mockResolvedValue(success(closedShift));
  posApi.createHeldReceipt.mockReset().mockResolvedValue(success(held));
  posApi.createIdempotencyKey
    .mockReset()
    .mockReturnValue("123e4567-e89b-42d3-a456-426614174000");
  posApi.createReturn.mockReset().mockResolvedValue(success(posReturn));
  posApi.createSale.mockReset().mockResolvedValue(success(sale));
  posApi.deleteHeldReceipt.mockReset().mockResolvedValue(success(null));
  posApi.getCurrentShift.mockReset().mockResolvedValue(success(shift));
  posApi.getPOSOptions.mockReset().mockResolvedValue(success(options));
  posApi.getReturn.mockReset().mockResolvedValue(success(posReturn));
  posApi.getReturnOptions.mockReset().mockResolvedValue(success(returnOptions));
  posApi.getSale.mockReset().mockResolvedValue(success(sale));
  posApi.listHeldReceipts.mockReset().mockResolvedValue(
    success({
      items: [held],
      count: 1,
      limit: 10,
      offset: 0,
    }),
  );
  posApi.listPOSProducts.mockReset().mockResolvedValue(
    success({
      items: [milk],
      count: 1,
      limit: 20,
      offset: 0,
    }),
  );
  posApi.listReturns.mockReset().mockResolvedValue(success(returnList));
  posApi.listSales.mockReset().mockResolvedValue(
    success({
      items: [sale],
      count: 1,
      limit: 10,
      offset: 0,
    }),
  );
  posApi.openShift.mockReset().mockResolvedValue(success(shift));
  posApi.updateHeldReceipt.mockReset().mockResolvedValue(success(held));
});

afterEach(() => {
  cleanup();
});

describe("POSManager permissions and loading/error/retry", () => {
  it("shows a Russian 403 screen without calling POS APIs", () => {
    renderPOS(false);

    expect(screen.getByRole("heading", { name: "Доступ к кассе запрещён" })).toBeTruthy();
    expect(posApi.getPOSOptions).not.toHaveBeenCalled();
    expect(posApi.getCurrentShift).not.toHaveBeenCalled();
  });

  it("shows options error and retries safely", async () => {
    posApi.getPOSOptions
      .mockResolvedValueOnce(error("API_UNAVAILABLE", "API временно недоступен", 503))
      .mockResolvedValueOnce(success(options));

    const user = userEvent.setup();
    renderPOS();

    await screen.findByText("API временно недоступен");
    await user.click(screen.getByRole("button", { name: "Повторить запрос" }));

    await waitFor(() => expect(posApi.getPOSOptions).toHaveBeenCalledTimes(2));
    await screen.findByRole("heading", { name: "Операционный контур POS" });
  });
});

describe("POSManager shift flow", () => {
  it("opens a shift when current shift is empty", async () => {
    posApi.getCurrentShift.mockResolvedValueOnce(error("SHIFT_NOT_FOUND", "Смена не найдена", 404));
    const user = userEvent.setup();
    renderPOS();

    await screen.findByRole("button", { name: "Открыть смену" });
    await user.clear(screen.getByLabelText("Разменная наличность на старте"));
    await user.type(screen.getByLabelText("Разменная наличность на старте"), "250,50");
    await user.click(screen.getByRole("button", { name: "Открыть смену" }));

    await waitFor(() =>
      expect(posApi.openShift).toHaveBeenCalledWith(
        { register_id: "REG-1", opening_cash: "250.50" },
        "123e4567-e89b-42d3-a456-426614174000",
      ),
    );
  });

  it("closes the current shift with expected updated_at", async () => {
    const user = userEvent.setup();
    await renderReady();

    await user.click(screen.getByRole("button", { name: "Закрыть смену" }));

    await waitFor(() =>
      expect(posApi.closeShift).toHaveBeenCalledWith(
        "SHIFT-1",
        {
          actual_cash: "1500.00",
          expected_updated_at: "2026-07-08T09:00:00Z",
          reason: "",
        },
        "123e4567-e89b-42d3-a456-426614174000",
      ),
    );
  });
});

describe("POSManager scanner, cart and sale", () => {
  it("adds a barcode lookup result on Enter and keeps scanner focus", async () => {
    const user = userEvent.setup();
    await renderReady();

    const scanner = await scanMilk(user);

    expect(posApi.listPOSProducts).toHaveBeenCalledWith({
      registerId: "REG-1",
      barcode: "460123",
      limit: 1,
      offset: 0,
    });
    expect(document.activeElement).toBe(scanner);
  });

  it("blocks discounts above role limit before sale", async () => {
    const user = userEvent.setup();
    await renderReady();
    await scanMilk(user);

    const discountInput = screen.getByLabelText("Скидка Молоко 3,2%, %");
    await user.clear(discountInput);
    await user.type(discountInput, "50");

    expect(await screen.findByText("Скидка должна быть от 0 до 10.00%.")).toBeTruthy();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Пробить продажу" }).disabled).toBe(
      true,
    );
  });

  it("keeps cart on insufficient stock and retries with the same idempotency key", async () => {
    posApi.createSale
      .mockResolvedValueOnce(error("INSUFFICIENT_STOCK", "Недостаточно товара на складе", 409))
      .mockResolvedValueOnce(success(sale));
    const user = userEvent.setup();
    await renderReady();
    await scanMilk(user);

    await user.type(screen.getByLabelText("Получено наличными"), "1000");
    await user.click(screen.getByRole("button", { name: "Пробить продажу" }));

    await screen.findByText("Недостаточно товара на складе");
    expect(screen.getByText("QA-MILK-001")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Повторить продажу безопасно" }));

    await waitFor(() => expect(posApi.createSale).toHaveBeenCalledTimes(2));
    expect(posApi.createSale.mock.calls[0][1]).toBe("123e4567-e89b-42d3-a456-426614174000");
    expect(posApi.createSale.mock.calls[1][1]).toBe("123e4567-e89b-42d3-a456-426614174000");
    expect(await screen.findByRole("heading", { name: "Чек POS-0001" })).toBeTruthy();
  });
});

describe("POSManager held receipts", () => {
  it("creates, reloads, updates and deletes held receipts", async () => {
    const user = userEvent.setup();
    await renderReady();
    await scanMilk(user);

    await user.type(screen.getByLabelText("Метка чека"), "Клиент вернётся");
    await user.click(screen.getByRole("button", { name: "Отложить чек" }));

    await waitFor(() => expect(posApi.createHeldReceipt).toHaveBeenCalled());
    expect(posApi.createHeldReceipt.mock.calls[0][0]).toMatchObject({
      shift_id: "SHIFT-1",
      label: "Клиент вернётся",
      lines: [{ product_id: "QA-MILK-001", quantity: "1.000", discount_percent: "0.00" }],
    });

    const heldCard = screen.getByText("HELD-1").closest("article");
    expect(heldCard).toBeTruthy();
    await user.click(within(heldCard as HTMLElement).getByRole("button", { name: "Загрузить" }));
    expect(screen.getByText("Отложенный чек HELD-1 загружен в корзину.")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Обновить отложенный чек" }));
    await waitFor(() =>
      expect(posApi.updateHeldReceipt).toHaveBeenCalledWith(
        "HELD-1",
        expect.objectContaining({
          expected_updated_at: "2026-07-08T09:20:00Z",
        }),
      ),
    );

    await user.click(within(heldCard as HTMLElement).getByRole("button", { name: "Удалить" }));
    await waitFor(() => expect(posApi.deleteHeldReceipt).toHaveBeenCalledWith("HELD-1"));
  });
});

describe("POSManager sales history and receipt", () => {
  it("applies sales history filters and opens sale details", async () => {
    const user = userEvent.setup();
    await renderReady();

    const salesSection = screen.getByRole("heading", { name: "История продаж" }).closest("section");
    expect(salesSection).toBeTruthy();
    const sales = within(salesSection as HTMLElement);

    await user.type(sales.getByLabelText("Поиск"), "milk");
    await user.type(sales.getByLabelText("Кассир email"), "cashier@example.test");
    await user.type(sales.getByLabelText("Дата от"), "2026-07-08");
    await user.type(sales.getByLabelText("Дата до"), "2026-07-09");
    await user.click(sales.getByRole("button", { name: "Применить фильтры продаж" }));

    await waitFor(() =>
      expect(posApi.listSales).toHaveBeenLastCalledWith({
        q: "milk",
        registerId: "REG-1",
        cashierEmail: "cashier@example.test",
        dateFrom: "2026-07-08",
        dateTo: "2026-07-09",
        limit: 10,
        offset: 0,
      }),
    );

    await user.click(sales.getByRole("button", { name: "Детали" }));
    await waitFor(() => expect(posApi.getSale).toHaveBeenCalledWith("SALE-1"));
    expect(await screen.findByText("Детали продажи POS-0001")).toBeTruthy();
  });

  it("renders printable receipt markup", async () => {
    const printSpy = vi.spyOn(window, "print").mockImplementation(() => undefined);
    const user = userEvent.setup();
    await renderReady();
    await scanMilk(user);
    await user.type(screen.getByLabelText("Получено наличными"), "1000");
    await user.click(screen.getByRole("button", { name: "Пробить продажу" }));

    const receipt = await screen.findByLabelText("HTML-чек");
    expect(within(receipt).getByText("MyRetail POS")).toBeTruthy();
    await user.click(within(receipt).getByRole("button", { name: "Печать чека" }));
    expect(printSpy).toHaveBeenCalledOnce();
  });
});

describe("POSManager returns flow", () => {
  it("loads return options, blocks over-return and safely retries create with the same key", async () => {
    posApi.createReturn
      .mockResolvedValueOnce(error("API_TIMEOUT", "Backend timeout", 504))
      .mockResolvedValueOnce(success(posReturn));
    const user = userEvent.setup();
    await renderReady();

    await user.click(screen.getAllByRole("button", { name: "Детали" })[0]);
    await user.click(await screen.findByRole("button", { name: "Оформить возврат" }));

    await waitFor(() => expect(posApi.getReturnOptions).toHaveBeenCalledWith("SALE-1"));
    await screen.findByRole("heading", { name: "Оформить возврат по продаже POS-0001" });

    const quantityInput = screen.getByPlaceholderText("0.000");
    await user.type(quantityInput, "2");
    await user.selectOptions(screen.getByLabelText("Причина возврата"), "customer_request");
    await user.click(screen.getByRole("button", { name: "Оформить cash refund KZT" }));

    expect(await screen.findByText("Доступно к возврату не больше 1.000.")).toBeTruthy();
    expect(posApi.createReturn).not.toHaveBeenCalled();

    await user.clear(quantityInput);
    await user.type(quantityInput, "1");
    await user.click(screen.getByRole("button", { name: "Оформить cash refund KZT" }));

    await screen.findByText(
      "Backend не ответил вовремя. Для безопасного retry используется тот же Idempotency-Key.",
    );
    expect(posApi.createReturn).toHaveBeenCalledWith(
      {
        sale_id: "SALE-1",
        register_id: "REG-1",
        shift_id: "SHIFT-1",
        refund_method: "cash",
        reason: "customer_request",
        comment: "",
        lines: [{ line_id: "LINE-1", quantity: "1" }],
      },
      "123e4567-e89b-42d3-a456-426614174000",
    );

    await user.click(screen.getByRole("button", { name: "Оформить cash refund KZT" }));

    await screen.findByRole("heading", { name: "Чек возврата RET-0001" });
    expect(posApi.createReturn).toHaveBeenCalledTimes(2);
    expect(posApi.createReturn.mock.calls[0][1]).toBe(
      "123e4567-e89b-42d3-a456-426614174000",
    );
    expect(posApi.createReturn.mock.calls[1][1]).toBe(
      "123e4567-e89b-42d3-a456-426614174000",
    );
  });

  it("applies returns filters and opens return detail", async () => {
    const user = userEvent.setup();
    await renderReady();

    const returnsSection = screen
      .getByRole("heading", { name: "История возвратов" })
      .closest("section");
    expect(returnsSection).toBeTruthy();
    const returns = within(returnsSection as HTMLElement);

    await user.type(returns.getByLabelText("Поиск"), "RET");
    await user.type(returns.getByLabelText("Sale ID"), "SALE-1");
    await user.type(returns.getByLabelText("Кассир email"), "cashier@example.test");
    await user.selectOptions(returns.getByLabelText("Статус"), "submitted");
    await user.type(returns.getByLabelText("Дата от"), "2026-07-08");
    await user.type(returns.getByLabelText("Дата до"), "2026-07-09");
    await user.click(returns.getByRole("button", { name: "Применить фильтры возвратов" }));

    await waitFor(() =>
      expect(posApi.listReturns).toHaveBeenLastCalledWith({
        q: "RET",
        saleId: "SALE-1",
        registerId: "REG-1",
        cashierEmail: "cashier@example.test",
        dateFrom: "2026-07-08",
        dateTo: "2026-07-09",
        state: "submitted",
        limit: 10,
        offset: 0,
      }),
    );

    const returnCard = screen.getByText("RET-0001").closest("article");
    expect(returnCard).toBeTruthy();
    await user.click(within(returnCard as HTMLElement).getByRole("button", { name: "Детали" }));

    await waitFor(() => expect(posApi.getReturn).toHaveBeenCalledWith("RETURN-1"));
    expect(await screen.findByRole("heading", { name: "Чек возврата RET-0001" })).toBeTruthy();
  });

  it("hides cancel action from Cashier", async () => {
    const user = userEvent.setup();
    await renderReady();

    const returnCard = await screen.findByText("RET-0001");
    await user.click(
      within(returnCard.closest("article") as HTMLElement).getByRole("button", {
        name: "Детали",
      }),
    );

    expect(await screen.findByText("Отмена возврата доступна только Owner/Admin. Для Cashier действие скрыто.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Отменить возврат" })).toBeNull();
  });

  it("allows Owner/Admin to cancel a submitted return", async () => {
    const user = userEvent.setup();
    renderPOS(true, ["Owner"]);
    await screen.findByRole("heading", { name: "Касса MyRetail" });
    await screen.findByText("Смена открыта");

    const returnCard = await screen.findByText("RET-0001");
    await user.click(
      within(returnCard.closest("article") as HTMLElement).getByRole("button", {
        name: "Детали",
      }),
    );

    await user.selectOptions(
      await screen.findByLabelText("Причина отмены"),
      "cashier_error",
    );
    await user.click(screen.getByRole("button", { name: "Отменить возврат" }));

    await waitFor(() =>
      expect(posApi.cancelReturn).toHaveBeenCalledWith(
        "RETURN-1",
        { reason: "cashier_error", comment: "" },
        "123e4567-e89b-42d3-a456-426614174000",
      ),
    );
    expect(await screen.findByText(/Отменён:/)).toBeTruthy();
  });
});
