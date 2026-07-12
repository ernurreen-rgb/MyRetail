"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { useEffect, useMemo, useState } from "react";

import { CartPanel, hasCartValidationErrors } from "@/app/pos/components/cart-panel";
import { HeldReceiptsPanel } from "@/app/pos/components/held-receipts-panel";
import { PaymentPanel } from "@/app/pos/components/payment-panel";
import { ProductLookup } from "@/app/pos/components/product-lookup";
import { ReceiptView } from "@/app/pos/components/receipt-view";
import { ReturnFlow } from "@/app/pos/components/return-flow";
import { ReturnsHistory } from "@/app/pos/components/returns-history";
import { SalesHistory } from "@/app/pos/components/sales-history";
import {
  EmptyState,
  ErrorState,
  normalizeMoneyInput,
  parseDecimal,
  roleLabel,
  type POSActionResult,
} from "@/app/pos/components/shared";
import { ShiftPanel } from "@/app/pos/components/shift-panel";
import { cartLineToInput, saleLineToCartLine, type CartLine } from "@/app/pos/pos-types";
import {
  closeShift,
  createHeldReceipt,
  createIdempotencyKey,
  createSale,
  deleteHeldReceipt,
  getCurrentShift,
  getPOSOptions,
  listHeldReceipts,
  openShift,
  updateHeldReceipt,
  type HeldReceipt,
  type POSOptions,
  type POSProduct,
  type POSReturn,
  type Sale,
  type Shift,
} from "@/lib/pos";

const HELD_PAGE_SIZE = 10;

function toActionResult(error: {
  code: string;
  message: string;
  fields?: Record<string, string>;
}): POSActionResult {
  return {
    ok: false,
    code: error.code,
    message: error.message,
    fields: error.fields,
  };
}

function incrementQuantity(quantity: string) {
  return (parseDecimal(quantity) + 1).toFixed(3);
}

function canCancelReturnsForRoles(roles: string[]) {
  return roles.some((role) => ["owner", "admin"].includes(role.toLowerCase()));
}

export function POSManager({
  canUsePOS,
  userRoles,
  userEmail,
}: {
  canUsePOS: boolean;
  userRoles: string[];
  userEmail: string;
}) {
  const [options, setOptions] = useState<POSOptions | null>(null);
  const [isLoadingOptions, setIsLoadingOptions] = useState(true);
  const [optionsError, setOptionsError] = useState<string | null>(null);

  const [registerId, setRegisterId] = useState("");
  const [shift, setShift] = useState<Shift | null>(null);
  const [isLoadingShift, setIsLoadingShift] = useState(false);
  const [shiftError, setShiftError] = useState<string | null>(null);

  const [cartLines, setCartLines] = useState<CartLine[]>([]);
  const [cashReceived, setCashReceived] = useState("");
  const [saleIdempotencyKey, setSaleIdempotencyKey] = useState(createIdempotencyKey);
  const [isSelling, setIsSelling] = useState(false);
  const [saleError, setSaleError] = useState<string | null>(null);
  const [saleFieldErrors, setSaleFieldErrors] = useState<Record<string, string>>({});
  const [lastSale, setLastSale] = useState<Sale | null>(null);

  const [heldItems, setHeldItems] = useState<HeldReceipt[]>([]);
  const [heldCount, setHeldCount] = useState(0);
  const [heldOffset, setHeldOffset] = useState(0);
  const [isLoadingHeld, setIsLoadingHeld] = useState(false);
  const [heldError, setHeldError] = useState<string | null>(null);
  const [selectedHeld, setSelectedHeld] = useState<HeldReceipt | null>(null);
  const [selectedHeldDirty, setSelectedHeldDirty] = useState(false);

  const [historyRefreshToken, setHistoryRefreshToken] = useState(0);
  const [returnsRefreshToken, setReturnsRefreshToken] = useState(0);
  const [returnTargetSale, setReturnTargetSale] = useState<Sale | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const activeRegisters = useMemo(
    () => options?.registers.filter((register) => register.is_active) ?? [],
    [options],
  );
  const selectedRegister = useMemo(
    () => options?.registers.find((register) => register.id === registerId) ?? null,
    [options, registerId],
  );
  const discountLimitPercent = options?.discount_limit_percent ?? "0.00";
  const canOperateShift = Boolean(shift && shift.status === "open");
  const canCancelReturns = canCancelReturnsForRoles(userRoles);
  const cartHasErrors = hasCartValidationErrors(cartLines, discountLimitPercent);
  const selectedHeldForSale = selectedHeld && !selectedHeldDirty ? selectedHeld : null;

  async function loadOptions() {
    setIsLoadingOptions(true);
    setOptionsError(null);

    const result = await getPOSOptions();

    if (result.status === "success") {
      setOptions(result.data);
      const active = result.data.registers.filter((register) => register.is_active);
      setRegisterId((current) =>
        current && active.some((register) => register.id === current)
          ? current
          : active[0]?.id ?? "",
      );
    } else {
      setOptionsError(result.error.message);
    }

    setIsLoadingOptions(false);
  }

  async function refreshCurrentShift(nextRegisterId = registerId) {
    if (!nextRegisterId) {
      setShift(null);
      return;
    }

    setIsLoadingShift(true);
    setShiftError(null);

    const result = await getCurrentShift(nextRegisterId);

    if (result.status === "success") {
      setShift(result.data);
    } else if (result.error.code === "SHIFT_NOT_FOUND" || result.statusCode === 404) {
      setShift(null);
    } else {
      setShiftError(result.error.message);
    }

    setIsLoadingShift(false);
  }

  async function refreshHeldReceipts(next?: { offset?: number }) {
    if (!shift) {
      setHeldItems([]);
      setHeldCount(0);
      setHeldOffset(0);
      return;
    }

    const nextOffset = next?.offset ?? heldOffset;

    setIsLoadingHeld(true);
    setHeldError(null);

    const result = await listHeldReceipts({
      shiftId: shift.id,
      limit: HELD_PAGE_SIZE,
      offset: nextOffset,
    });

    if (result.status === "success") {
      setHeldItems(result.data.items);
      setHeldCount(result.data.count);
      setHeldOffset(result.data.offset);
    } else {
      setHeldError(result.error.message);
    }

    setIsLoadingHeld(false);
  }

  useEffect(() => {
    if (!canUsePOS) {
      return;
    }

    void loadOptions();
  }, [canUsePOS]);

  useEffect(() => {
    if (!canUsePOS || !registerId) {
      return;
    }

    void refreshCurrentShift(registerId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canUsePOS, registerId]);

  useEffect(() => {
    if (!shift) {
      setHeldItems([]);
      setHeldCount(0);
      setHeldOffset(0);
      setSelectedHeld(null);
      setSelectedHeldDirty(false);
      return;
    }

    void refreshHeldReceipts({ offset: 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shift?.id]);

  function resetSaleIntent() {
    setSaleIdempotencyKey(createIdempotencyKey());
    setSaleError(null);
    setSaleFieldErrors({});
  }

  function markCartChanged() {
    resetSaleIntent();
    if (selectedHeld) {
      setSelectedHeldDirty(true);
    }
  }

  function handleRegisterChange(nextRegisterId: string) {
    setRegisterId(nextRegisterId);
    setShift(null);
    setCartLines([]);
    setSelectedHeld(null);
    setSelectedHeldDirty(false);
    setCashReceived("");
    resetSaleIntent();
  }

  function handleAddProduct(product: POSProduct) {
    setCartLines((current) => {
      const existingIndex = current.findIndex((line) => line.product.id === product.id);

      if (existingIndex === -1) {
        return [
          ...current,
          {
            product,
            quantity: "1.000",
            discount_percent: "0.00",
          },
        ];
      }

      return current.map((line, index) =>
        index === existingIndex ? { ...line, quantity: incrementQuantity(line.quantity) } : line,
      );
    });
    markCartChanged();
  }

  function handleUpdateCartLine(
    index: number,
    patch: Partial<Pick<CartLine, "quantity" | "discount_percent">>,
  ) {
    setCartLines((current) =>
      current.map((line, lineIndex) => (lineIndex === index ? { ...line, ...patch } : line)),
    );
    markCartChanged();
  }

  function handleRemoveCartLine(index: number) {
    setCartLines((current) => current.filter((_, lineIndex) => lineIndex !== index));
    markCartChanged();
  }

  function handleClearCart() {
    setCartLines([]);
    setSelectedHeld(null);
    setSelectedHeldDirty(false);
    setCashReceived("");
    resetSaleIntent();
  }

  function handleCashReceivedChange(value: string) {
    setCashReceived(value);
    resetSaleIntent();
  }

  async function handleOpenShift(
    openingCash: string,
    idempotencyKey: string,
  ): Promise<POSActionResult> {
    if (!registerId) {
      return {
        ok: false,
        code: "VALIDATION_ERROR",
        message: "Выберите активную кассу.",
        fields: { register_id: "Касса обязательна" },
      };
    }

    const result = await openShift(
      {
        register_id: registerId,
        opening_cash: normalizeMoneyInput(openingCash),
      },
      idempotencyKey,
    );

    if (result.status === "success") {
      setShift(result.data);
      setNotice(`Смена ${result.data.id} открыта.`);
      setHistoryRefreshToken((token) => token + 1);
      return { ok: true };
    }

    if (result.error.code === "SHIFT_ALREADY_OPEN") {
      await refreshCurrentShift(registerId);
    }

    return toActionResult(result.error);
  }

  async function handleCloseShift(
    actualCash: string,
    reason: string,
    idempotencyKey: string,
  ): Promise<POSActionResult> {
    if (!shift) {
      return { ok: false, code: "SHIFT_NOT_FOUND", message: "Открытая смена не найдена." };
    }

    const result = await closeShift(
      shift.id,
      {
        actual_cash: normalizeMoneyInput(actualCash),
        expected_updated_at: shift.updated_at,
        reason,
      },
      idempotencyKey,
    );

    if (result.status === "success") {
      setShift(null);
      setNotice(`Смена ${result.data.id} закрыта.`);
      setHistoryRefreshToken((token) => token + 1);
      return { ok: true };
    }

    if (result.error.code === "SHIFT_CHANGED") {
      await refreshCurrentShift(registerId);
      return {
        ok: false,
        code: result.error.code,
        message: "Смена изменилась. Я обновил данные — повторите закрытие с новым updated_at.",
        fields: result.error.fields,
      };
    }

    return toActionResult(result.error);
  }

  async function handleCreateHeld(
    label: string,
    idempotencyKey: string,
  ): Promise<POSActionResult> {
    if (!shift) {
      return { ok: false, code: "SHIFT_NOT_FOUND", message: "Сначала откройте смену." };
    }

    const result = await createHeldReceipt(
      {
        shift_id: shift.id,
        label,
        lines: cartLines.map(cartLineToInput),
      },
      idempotencyKey,
    );

    if (result.status === "success") {
      setSelectedHeld(result.data);
      setSelectedHeldDirty(false);
      setCartLines(result.data.lines.map((line) => saleLineToCartLine(line, selectedRegister?.currency)));
      setNotice(`Чек ${result.data.id} отложен.`);
      await refreshHeldReceipts({ offset: 0 });
      return { ok: true };
    }

    return toActionResult(result.error);
  }

  async function handleUpdateSelectedHeld(label: string): Promise<POSActionResult> {
    if (!selectedHeld) {
      return { ok: false, code: "HELD_RECEIPT_NOT_FOUND", message: "Выберите отложенный чек." };
    }

    const result = await updateHeldReceipt(selectedHeld.id, {
      expected_updated_at: selectedHeld.updated_at,
      label,
      lines: cartLines.map(cartLineToInput),
    });

    if (result.status === "success") {
      setSelectedHeld(result.data);
      setSelectedHeldDirty(false);
      setCartLines(result.data.lines.map((line) => saleLineToCartLine(line, selectedRegister?.currency)));
      setNotice(`Отложенный чек ${result.data.id} обновлён.`);
      await refreshHeldReceipts();
      return { ok: true };
    }

    if (result.error.code === "HELD_RECEIPT_CHANGED") {
      await refreshHeldReceipts();
      return {
        ok: false,
        code: result.error.code,
        message: "Отложенный чек изменился. Загрузите свежую версию и повторите обновление.",
        fields: result.error.fields,
      };
    }

    return toActionResult(result.error);
  }

  function handleReloadHeld(held: HeldReceipt) {
    setSelectedHeld(held);
    setSelectedHeldDirty(false);
    setCartLines(held.lines.map((line) => saleLineToCartLine(line, selectedRegister?.currency)));
    setCashReceived(held.grand_total);
    resetSaleIntent();
    setNotice(`Отложенный чек ${held.id} загружен в корзину.`);
  }

  async function handleDeleteHeld(held: HeldReceipt): Promise<POSActionResult> {
    const result = await deleteHeldReceipt(held.id);

    if (result.status === "success") {
      if (selectedHeld?.id === held.id) {
        setSelectedHeld(null);
        setSelectedHeldDirty(false);
      }
      setNotice(`Отложенный чек ${held.id} удалён.`);
      await refreshHeldReceipts({ offset: 0 });
      return { ok: true };
    }

    return toActionResult(result.error);
  }

  async function handleSubmitSale() {
    if (!shift) {
      setSaleError("Сначала откройте смену.");
      return;
    }
    if (cartLines.length === 0) {
      setSaleError("Добавьте товары в чек.");
      return;
    }
    if (cartHasErrors) {
      setSaleError("Исправьте количество или скидку в корзине.");
      return;
    }

    setIsSelling(true);
    setSaleError(null);
    setSaleFieldErrors({});

    const result = await createSale(
      {
        shift_id: shift.id,
        held_receipt_id: selectedHeldForSale?.id ?? null,
        lines: cartLines.map(cartLineToInput),
        cash_received: cashReceived,
      },
      saleIdempotencyKey,
    );

    if (result.status === "success") {
      setLastSale(result.data);
      setCartLines([]);
      setSelectedHeld(null);
      setSelectedHeldDirty(false);
      setCashReceived("");
      setSaleIdempotencyKey(createIdempotencyKey());
      setNotice(`Продажа ${result.data.receipt_number} создана.`);
      await Promise.all([
        refreshCurrentShift(registerId),
        refreshHeldReceipts({ offset: 0 }),
      ]);
      setHistoryRefreshToken((token) => token + 1);
    } else {
      setSaleError(result.error.message);
      setSaleFieldErrors(result.error.fields);
      if (result.error.code === "IDEMPOTENCY_CONFLICT") {
        setSaleIdempotencyKey(createIdempotencyKey());
      }
      if (
        result.error.code === "SHIFT_CLOSED" ||
        result.error.code === "SHIFT_CHANGED" ||
        result.error.code === "SHIFT_NOT_FOUND"
      ) {
        await refreshCurrentShift(registerId);
      }
      if (
        result.error.code === "HELD_RECEIPT_NOT_FOUND" ||
        result.error.code === "HELD_RECEIPT_CHANGED"
      ) {
        await refreshHeldReceipts();
      }
    }

    setIsSelling(false);
  }

  function handleReturnCompleted(posReturn: POSReturn) {
    setNotice(
      `Возврат ${posReturn.return_receipt_number || posReturn.return_id} оформлен на ${posReturn.totals.refund_total} ${posReturn.currency}.`,
    );
    setHistoryRefreshToken((token) => token + 1);
    setReturnsRefreshToken((token) => token + 1);
    void refreshCurrentShift(registerId);
  }

  if (!canUsePOS) {
    return (
      <main className="flex-1 px-5 py-6 sm:px-8 lg:px-12 lg:py-10">
        <div className="mx-auto max-w-5xl rounded-2xl border border-red-200 bg-red-50 p-6 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
          <p className="text-sm">Роли: {roleLabel(userRoles)}</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight">Доступ к кассе запрещён</h1>
          <p className="mt-3 text-sm leading-6">
            Для Sprint 5 касса доступна владельцу, администратору или кассиру. API не вызывается,
            токены не читаются из браузера.
          </p>
        </div>
      </main>
    );
  }

  return (
    <main className="flex-1 px-5 py-6 sm:px-8 lg:px-12 lg:py-10">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-6">
        <header className="flex flex-col gap-4 border-b border-[var(--border)] pb-5 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <p className="font-mono text-xs font-semibold uppercase tracking-[0.18em] text-[var(--accent)]">
              Sprint 6 · POS returns frontend
            </p>
            <h1 className="mt-2 text-4xl font-semibold tracking-[-0.04em] sm:text-5xl">
              Касса MyRetail
            </h1>
            <p className="mt-3 max-w-3xl text-sm leading-6 text-[var(--muted)]">
              Работает через same-origin route handlers и HttpOnly session. Browser не обращается к
              ERPNext напрямую и не хранит access token в localStorage/sessionStorage.
            </p>
          </div>
          <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4 text-sm">
            <p className="text-[var(--muted)]">Пользователь</p>
            <p className="font-semibold">{userEmail}</p>
            <p className="text-[var(--muted)]">Роли: {roleLabel(userRoles)}</p>
          </div>
        </header>

        {notice ? (
          <div className="rounded-xl border border-[var(--accent)] bg-[var(--accent-soft)] p-4 text-sm font-semibold text-[var(--accent)]">
            {notice}
          </div>
        ) : null}

        {isLoadingOptions ? (
          <EmptyState>Загружаю справочники кассы…</EmptyState>
        ) : null}
        {optionsError ? <ErrorState message={optionsError} onRetry={() => void loadOptions()} /> : null}

        {!isLoadingOptions && !optionsError ? (
          <>
            <ShiftPanel
              options={options}
              registerId={registerId}
              shift={shift}
              isLoading={isLoadingShift}
              error={shiftError}
              canOperate={Boolean(registerId)}
              onRegisterChange={handleRegisterChange}
              onRetry={() => void refreshCurrentShift(registerId)}
              onOpenShift={handleOpenShift}
              onCloseShift={handleCloseShift}
            />

            <div className="grid gap-6 xl:grid-cols-[1fr_1fr]">
              <ProductLookup
                registerId={registerId}
                disabled={!canOperateShift}
                onAddProduct={handleAddProduct}
              />
              <CartPanel
                lines={cartLines}
                discountLimitPercent={discountLimitPercent}
                backendFieldErrors={saleFieldErrors}
                onUpdateLine={handleUpdateCartLine}
                onRemoveLine={handleRemoveCartLine}
                onClearCart={handleClearCart}
              />
            </div>

            <div className="grid gap-6 xl:grid-cols-[1fr_0.85fr]">
              <HeldReceiptsPanel
                items={heldItems}
                count={heldCount}
                offset={heldOffset}
                pageSize={HELD_PAGE_SIZE}
                selectedHeld={selectedHeld}
                canOperate={canOperateShift}
                canHoldCart={cartLines.length > 0 && !cartHasErrors}
                isLoading={isLoadingHeld}
                error={heldError}
                onRetry={() => void refreshHeldReceipts()}
                onPrevious={() =>
                  void refreshHeldReceipts({
                    offset: Math.max(0, heldOffset - HELD_PAGE_SIZE),
                  })
                }
                onNext={() =>
                  void refreshHeldReceipts({
                    offset: heldOffset + HELD_PAGE_SIZE,
                  })
                }
                onCreateHeld={handleCreateHeld}
                onUpdateSelectedHeld={handleUpdateSelectedHeld}
                onReloadHeld={handleReloadHeld}
                onDeleteHeld={handleDeleteHeld}
              />
              <PaymentPanel
                shift={shift}
                cartLines={cartLines}
                selectedHeld={selectedHeldForSale}
                cashReceived={cashReceived}
                isSelling={isSelling}
                saleError={saleError}
                saleFieldErrors={saleFieldErrors}
                hasCartErrors={cartHasErrors}
                onCashReceivedChange={handleCashReceivedChange}
                onSubmitSale={handleSubmitSale}
              />
            </div>

            <ReceiptView sale={lastSale} />

            <SalesHistory
              registers={activeRegisters}
              currentRegisterId={registerId}
              refreshToken={historyRefreshToken}
              onStartReturn={setReturnTargetSale}
            />

            <ReturnFlow
              sale={returnTargetSale}
              onClose={() => setReturnTargetSale(null)}
              onCompleted={handleReturnCompleted}
            />

            <ReturnsHistory
              registers={activeRegisters}
              currentRegisterId={registerId}
              canCancelReturns={canCancelReturns}
              refreshToken={returnsRefreshToken}
              onChanged={handleReturnCompleted}
            />
          </>
        ) : null}
      </div>
    </main>
  );
}
