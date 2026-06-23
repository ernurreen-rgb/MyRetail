import { getProducts } from "@/lib/products";

export const dynamic = "force-dynamic";

const modules = [
  "Товары",
  "Склад",
  "Закупки",
  "Продажи",
  "Возвраты",
  "Инвентаризация",
  "Отчёты",
];

function statusToneClass(tone: "ready" | "waiting" | "neutral" | "error") {
  if (tone === "ready") {
    return "bg-[var(--accent-soft)] text-[var(--accent)]";
  }

  if (tone === "waiting") {
    return "bg-[var(--warning-soft)] text-[var(--warning)]";
  }

  if (tone === "error") {
    return "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300";
  }

  return "bg-[var(--surface-muted)] text-[var(--muted)]";
}

export default async function Home() {
  const products = await getProducts();
  const productsReady = products.status === "ready";

  const services = [
    {
      name: "Веб-приложение",
      detail: "Next.js 16.2, React 19.2, TypeScript",
      status: "Готово",
      tone: "ready",
    },
    {
      name: "MyRetail API",
      detail: productsReady
        ? "FastAPI-шлюз читает товары из ERPNext"
        : "FastAPI-шлюз настроен, но сейчас не вернул товары",
      status: productsReady ? "Готово" : "Проверить",
      tone: productsReady ? "ready" : "error",
    },
    {
      name: "ERPNext",
      detail: productsReady
        ? `Локальная база доступна, товаров: ${products.data.count}`
        : "Локальная база поднята, требуется проверка API",
      status: productsReady ? "Готово" : "Проверить",
      tone: productsReady ? "ready" : "waiting",
    },
    {
      name: "Кассовое приложение",
      detail: "Tauri после стабилизации API и web-компонентов",
      status: "Запланировано",
      tone: "neutral",
    },
  ] as const;

  return (
    <main className="flex-1 px-5 py-6 sm:px-8 lg:px-12 lg:py-10">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-8">
        <header className="flex items-center justify-between border-b border-[var(--border)] pb-5">
          <div className="flex items-center gap-3">
            <div
              aria-hidden="true"
              className="grid size-10 place-items-center rounded-xl bg-[var(--accent)] text-lg font-bold text-white"
            >
              M
            </div>
            <div>
              <p className="text-lg font-semibold tracking-tight">MyRetail</p>
              <p className="text-sm text-[var(--muted)]">Основа проекта</p>
            </div>
          </div>
          <span className="rounded-full bg-[var(--accent-soft)] px-3 py-1.5 text-xs font-semibold text-[var(--accent)]">
            Sprint 1
          </span>
        </header>

        <section className="grid gap-6 lg:grid-cols-[1.25fr_0.75fr] lg:items-end">
          <div>
            <p className="mb-3 font-mono text-xs font-semibold uppercase tracking-[0.18em] text-[var(--accent)]">
              Розничные операции без лишней сложности
            </p>
            <h1 className="max-w-3xl text-4xl font-semibold tracking-[-0.04em] sm:text-5xl lg:text-6xl">
              Первый рабочий контур MyRetail уже читает товары из ERPNext.
            </h1>
          </div>
          <p className="max-w-xl text-base leading-7 text-[var(--muted)] lg:pb-1">
            Веб-страница обращается только к MyRetail API. Backend получает данные из ERPNext
            через сервисного пользователя с ограниченными правами, поэтому внутренние ключи и
            модели ERPNext не попадают в браузер.
          </p>
        </section>

        <section aria-labelledby="services-heading">
          <div className="mb-4 flex items-end justify-between gap-4">
            <div>
              <p className="text-sm text-[var(--muted)]">Текущее состояние</p>
              <h2 id="services-heading" className="text-2xl font-semibold tracking-tight">
                Компоненты платформы
              </h2>
            </div>
            <p className="hidden font-mono text-xs text-[var(--muted)] sm:block">v0.1.0</p>
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            {services.map((service) => (
              <article
                key={service.name}
                className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)]"
              >
                <div className="mb-8 flex items-start justify-between gap-4">
                  <span aria-hidden="true" className="mt-1 size-2 rounded-full bg-[var(--accent)]" />
                  <span
                    className={`rounded-full px-2.5 py-1 text-xs font-semibold ${statusToneClass(service.tone)}`}
                  >
                    {service.status}
                  </span>
                </div>
                <h3 className="text-lg font-semibold">{service.name}</h3>
                <p className="mt-1 text-sm leading-6 text-[var(--muted)]">{service.detail}</p>
              </article>
            ))}
          </div>
        </section>

        <section
          aria-labelledby="products-heading"
          className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)] sm:p-6"
        >
          <div className="mb-5 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <p className="text-sm text-[var(--muted)]">Данные из ERPNext</p>
              <h2 id="products-heading" className="text-2xl font-semibold tracking-tight">
                Товары
              </h2>
            </div>
            <span className="w-fit rounded-full bg-[var(--surface-muted)] px-3 py-1.5 font-mono text-xs text-[var(--muted)]">
              /products
            </span>
          </div>

          {products.status === "ready" ? (
            products.data.items.length > 0 ? (
              <div className="grid gap-3 md:grid-cols-2">
                {products.data.items.map((product) => (
                  <article
                    key={product.id}
                    className="rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-4"
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div>
                        <p className="font-mono text-xs text-[var(--muted)]">{product.id}</p>
                        <h3 className="mt-1 text-lg font-semibold">{product.name}</h3>
                      </div>
                      <span className="rounded-full bg-[var(--accent-soft)] px-2.5 py-1 text-xs font-semibold text-[var(--accent)]">
                        {product.unit}
                      </span>
                    </div>
                    <p className="mt-3 text-sm leading-6 text-[var(--muted)]">
                      {product.description ?? "Описание пока не заполнено."}
                    </p>
                  </article>
                ))}
              </div>
            ) : (
              <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--surface-muted)] p-5 text-sm leading-6 text-[var(--muted)]">
                ERPNext доступен, но активных товаров пока нет. Следующий шаг — наполнить
                каталог тестовыми позициями для сценариев продаж и склада.
              </div>
            )
          ) : (
            <div className="rounded-xl border border-red-200 bg-red-50 p-5 text-sm leading-6 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
              Не удалось получить товары: {products.message}
            </div>
          )}
        </section>

        <section className="rounded-2xl border border-[var(--border)] bg-[var(--surface-muted)] p-5 sm:p-6">
          <div className="grid gap-5 md:grid-cols-[0.85fr_1.15fr] md:items-start">
            <div>
              <p className="text-sm text-[var(--muted)]">MVP v1.0</p>
              <h2 className="text-2xl font-semibold tracking-tight">Зафиксированный объём</h2>
            </div>
            <ul className="flex flex-wrap gap-2" aria-label="Модули MVP">
              {modules.map((module) => (
                <li
                  key={module}
                  className="rounded-full border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-sm"
                >
                  {module}
                </li>
              ))}
            </ul>
          </div>
        </section>
      </div>
    </main>
  );
}
