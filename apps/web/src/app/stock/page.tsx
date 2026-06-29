import Link from "next/link";
import { redirect } from "next/navigation";

import { StockManager } from "@/app/stock/stock-manager";
import { canManageStock } from "@/lib/auth";
import { getAuthSession } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function StockPage() {
  const session = await getAuthSession();

  if (!session) {
    redirect("/login");
  }

  return (
    <main className="flex-1 px-5 py-6 sm:px-8 lg:px-12 lg:py-10">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-8">
        <header className="flex flex-col gap-4 border-b border-[var(--border)] pb-5 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex items-center gap-3">
            <div
              aria-hidden="true"
              className="grid size-10 place-items-center rounded-xl bg-[var(--accent)] text-lg font-bold text-white"
            >
              M
            </div>
            <div>
              <Link href="/" className="text-sm font-semibold text-[var(--accent)]">
                MyRetail
              </Link>
              <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
                Склад и движения
              </h1>
              <p className="mt-1 text-sm text-[var(--muted)]">
                Tenant: {session.tenant}. Остатки и операции идут через MyRetail API
                без прямого доступа браузера к ERPNext.
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <span className="rounded-full bg-[var(--accent-soft)] px-3 py-1.5 text-xs font-semibold text-[var(--accent)]">
              Sprint 3
            </span>
            <form action="/api/auth/logout" method="post">
              <button
                type="submit"
                className="rounded-full border border-[var(--border)] bg-[var(--surface)] px-4 py-2 text-sm font-semibold transition hover:border-[var(--accent)] hover:text-[var(--accent)]"
              >
                Выйти
              </button>
            </form>
          </div>
        </header>

        <StockManager
          canManage={canManageStock(session.user.roles)}
          userRoles={session.user.roles}
        />
      </div>
    </main>
  );
}
