import Link from "next/link";
import { redirect } from "next/navigation";

import { ProductManager } from "@/app/products/product-manager";
import { canManageProducts, canUsePOS } from "@/lib/auth";
import { getAuthSession } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function ProductsPage() {
  const session = await getAuthSession();

  if (!session) {
    redirect("/login");
  }

  const canManage = canManageProducts(session.user.roles);

  if (!canManage) {
    const roleLabel = session.user.roles.length > 0 ? session.user.roles.join(", ") : "нет ролей";

    return (
      <main className="flex-1 px-5 py-6 sm:px-8 lg:px-12 lg:py-10">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-6">
          <header className="flex flex-col gap-4 border-b border-[var(--border)] pb-5 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <Link href="/" className="text-sm font-semibold text-[var(--accent)]">
                MyRetail
              </Link>
              <h1 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">
                Доступ к товарам запрещён
              </h1>
            </div>
            <form action="/api/auth/logout" method="post">
              <button
                type="submit"
                className="rounded-full border border-[var(--border)] bg-[var(--surface)] px-4 py-2 text-sm font-semibold transition hover:border-[var(--accent)] hover:text-[var(--accent)]"
              >
                Выйти
              </button>
            </form>
          </header>

          <section className="rounded-2xl border border-red-200 bg-red-50 p-6 text-red-800 shadow-[0_12px_36px_rgba(20,32,24,0.04)] dark:border-red-900 dark:bg-red-950 dark:text-red-200">
            <p className="text-sm font-semibold uppercase tracking-[0.16em]">403</p>
            <h2 className="mt-2 text-2xl font-semibold">Нет доступа к управлению товарами</h2>
            <p className="mt-3 text-sm leading-6">
              Раздел товаров, закупочные цены и архивный каталог доступны только ролям Owner и
              Admin. Текущие роли: {roleLabel}.
            </p>
            <div className="mt-5 flex flex-wrap gap-3">
              <Link
                href="/"
                className="rounded-xl border border-red-200 bg-white px-4 py-2.5 text-sm font-semibold text-red-800 transition hover:bg-red-100 dark:border-red-800 dark:bg-red-950 dark:text-red-100"
              >
                На главную
              </Link>
              {canUsePOS(session.user.roles) ? (
                <Link
                  href="/pos"
                  className="rounded-xl bg-red-700 px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-95"
                >
                  Открыть кассу
                </Link>
              ) : null}
            </div>
          </section>
        </div>
      </main>
    );
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
                Управление товарами
              </h1>
              <p className="mt-1 text-sm text-[var(--muted)]">
                Tenant: {session.tenant}. Данные идут через MyRetail API, без прямого доступа к
                ERPNext из браузера.
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <span className="rounded-full bg-[var(--accent-soft)] px-3 py-1.5 text-xs font-semibold text-[var(--accent)]">
              Sprint 2
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

        <ProductManager canManage={canManage} />
      </div>
    </main>
  );
}
