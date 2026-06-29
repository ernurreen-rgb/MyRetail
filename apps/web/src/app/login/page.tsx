import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { getAuthSession } from "@/lib/session";

import { LoginForm } from "./login-form";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Вход",
  description: "Вход в защищённый интерфейс MyRetail.",
};

const LOGIN_ERROR_MESSAGES: Record<string, string> = {
  invalid_credentials: "Неверный email или пароль.",
  tenant_not_found: "Тенант не найден. Проверьте код компании.",
  invalid_request: "Укажите tenant, email и пароль.",
  rate_limited: "Слишком много попыток входа. Подождите несколько минут и попробуйте снова.",
  unavailable: "Сервис входа временно недоступен. Попробуйте позже.",
};

type LoginPageProps = {
  searchParams: Promise<{
    error?: string | string[];
  }>;
};

export default async function LoginPage({ searchParams }: LoginPageProps) {
  const session = await getAuthSession();

  if (session) {
    redirect("/");
  }

  const params = await searchParams;
  const errorCode = Array.isArray(params.error) ? params.error[0] : params.error;
  const initialError = errorCode ? LOGIN_ERROR_MESSAGES[errorCode] ?? null : null;

  return (
    <main className="flex min-h-screen flex-1 items-center justify-center px-5 py-8 sm:px-8">
      <div className="grid w-full max-w-5xl gap-6 lg:grid-cols-[0.95fr_1.05fr] lg:items-stretch">
        <section className="rounded-3xl bg-[var(--accent)] p-8 text-white shadow-[0_24px_80px_rgba(21,115,71,0.24)] sm:p-10">
          <div className="flex h-full min-h-[360px] flex-col justify-between gap-10">
            <div>
              <div
                aria-hidden="true"
                className="mb-8 grid size-12 place-items-center rounded-2xl bg-white/15 text-xl font-bold"
              >
                M
              </div>
              <p className="mb-4 font-mono text-xs font-semibold uppercase tracking-[0.2em] text-white/75">
                Защищённый вход
              </p>
              <h1 className="max-w-xl text-4xl font-semibold tracking-[-0.04em] sm:text-5xl">
                Войдите в MyRetail, чтобы открыть рабочую панель.
              </h1>
            </div>
            <p className="max-w-xl text-base leading-7 text-white/80">
              Веб-приложение получает только сессию MyRetail. Ключи ERPNext остаются на backend,
              а tenant context проверяется через MyRetail API.
            </p>
          </div>
        </section>

        <section className="rounded-3xl border border-[var(--border)] bg-[var(--surface)] p-6 shadow-[0_18px_60px_rgba(20,32,24,0.08)] sm:p-8 lg:p-10">
          <div className="mb-8">
            <p className="text-sm text-[var(--muted)]">MyRetail Sprint 1</p>
            <h2 className="mt-2 text-3xl font-semibold tracking-tight">Вход в систему</h2>
            <p className="mt-3 text-sm leading-6 text-[var(--muted)]">
              Укажите tenant, email и пароль. После успешного входа токен будет сохранён в
              HttpOnly cookie.
            </p>
          </div>

          <LoginForm initialError={initialError} />
        </section>
      </div>
    </main>
  );
}
