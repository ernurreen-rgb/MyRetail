"use client";

import { type FormEvent, useState } from "react";

import { DEFAULT_TENANT, login } from "@/lib/auth";

const inputClass =
  "mt-2 w-full rounded-xl border border-[var(--border)] bg-[var(--surface)] px-4 py-3 text-base outline-none transition focus:border-[var(--accent)] focus:ring-4 focus:ring-[var(--accent-soft)] disabled:cursor-not-allowed disabled:opacity-70";

export function LoginForm() {
  const [tenant, setTenant] = useState(DEFAULT_TENANT);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setIsLoading(true);

    const result = await login({ tenant, email, password });

    if (result.status === "success") {
      window.location.assign("/");
      return;
    }

    setError(result.message);
    setIsLoading(false);
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-5" noValidate>
      <div>
        <label htmlFor="tenant" className="text-sm font-semibold text-[var(--foreground)]">
          Код tenant
        </label>
        <input
          id="tenant"
          name="tenant"
          type="text"
          value={tenant}
          onChange={(event) => setTenant(event.target.value)}
          className={inputClass}
          autoComplete="organization"
          disabled={isLoading}
          required
        />
        <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
          Для локального контура используйте значение `myretail`.
        </p>
      </div>

      <div>
        <label htmlFor="email" className="text-sm font-semibold text-[var(--foreground)]">
          Email пользователя
        </label>
        <input
          id="email"
          name="email"
          type="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          className={inputClass}
          autoComplete="email"
          disabled={isLoading}
          required
        />
      </div>

      <div>
        <label htmlFor="password" className="text-sm font-semibold text-[var(--foreground)]">
          Пароль
        </label>
        <input
          id="password"
          name="password"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className={inputClass}
          autoComplete="current-password"
          disabled={isLoading}
          required
        />
        <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
          Пароль отправляется только в MyRetail API и не сохраняется в браузере.
        </p>
      </div>

      {error ? (
        <div
          role="alert"
          className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm leading-6 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"
        >
          {error}
        </div>
      ) : null}

      <button
        type="submit"
        disabled={isLoading}
        className="w-full rounded-xl bg-[var(--accent)] px-5 py-3 text-base font-semibold text-white transition hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-70"
      >
        {isLoading ? "Входим…" : "Войти в MyRetail"}
      </button>
    </form>
  );
}