import { type ReactNode } from "react";

import type { CartLine } from "@/app/pos/pos-types";

export const inputClass =
  "mt-2 w-full rounded-xl border border-[var(--border)] bg-[var(--surface)] px-4 py-3 text-base outline-none transition focus:border-[var(--accent)] focus:ring-4 focus:ring-[var(--accent-soft)] disabled:cursor-not-allowed disabled:opacity-70";

export const secondaryButtonClass =
  "rounded-xl border border-[var(--border)] bg-[var(--surface)] px-4 py-2.5 text-sm font-semibold transition hover:border-[var(--accent)] hover:text-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-60";

export const primaryButtonClass =
  "rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-60";

export const dangerButtonClass =
  "rounded-xl bg-red-700 px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-60";

export type POSActionResult =
  | {
      ok: true;
      message?: string;
    }
  | {
      ok: false;
      code: string;
      message: string;
      fields?: Record<string, string>;
    };

export function FieldError({ message }: { message?: string }) {
  if (!message) {
    return null;
  }

  return (
    <p className="mt-2 text-sm leading-5 text-red-700 dark:text-red-300" role="alert">
      {message}
    </p>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--surface-muted)] p-5 text-sm leading-6 text-[var(--muted)]">
      {children}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="rounded-xl border border-red-200 bg-red-50 p-5 text-sm leading-6 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
      <p>{message}</p>
      <button type="button" onClick={onRetry} className={`${secondaryButtonClass} mt-4`}>
        Повторить запрос
      </button>
    </div>
  );
}

export function formatDateTime(value: string | null) {
  if (!value) {
    return "—";
  }

  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(date);
}

export function parseDecimal(value: string) {
  const normalized = value.replace(",", ".").trim();
  const parsed = Number.parseFloat(normalized);

  return Number.isFinite(parsed) ? parsed : 0;
}

export function formatMoney(value: number) {
  return value.toFixed(2);
}

export function normalizeMoneyInput(value: string) {
  return value.replace(",", ".").trim();
}

export function calculateCartTotals(lines: CartLine[]) {
  const subtotal = lines.reduce(
    (sum, line) => sum + parseDecimal(line.product.sale_price) * parseDecimal(line.quantity),
    0,
  );
  const discountTotal = lines.reduce((sum, line) => {
    const lineSubtotal = parseDecimal(line.product.sale_price) * parseDecimal(line.quantity);
    return sum + (lineSubtotal * parseDecimal(line.discount_percent || "0")) / 100;
  }, 0);
  const total = Math.max(0, subtotal - discountTotal);

  return {
    subtotal,
    discountTotal,
    total,
  };
}

export function getFieldError(fields: Record<string, string> | undefined, ...keys: string[]) {
  if (!fields) {
    return undefined;
  }

  for (const key of keys) {
    const message = fields[key];

    if (message) {
      return message;
    }
  }

  return undefined;
}

export function roleLabel(roles: string[]) {
  return roles.length > 0 ? roles.join(", ") : "без роли";
}
