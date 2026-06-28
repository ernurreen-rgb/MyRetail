"use client";

import { type FormEvent, useEffect, useMemo, useState } from "react";

import {
  archiveProduct,
  createProduct,
  emptyProductFormValues,
  getProductOptions,
  listProducts,
  productToFormValues,
  updateProduct,
  type Product,
  type ProductFormValues,
  type ProductOptions,
} from "@/lib/products";

const inputClass =
  "mt-2 w-full rounded-xl border border-[var(--border)] bg-[var(--surface)] px-4 py-3 text-base outline-none transition focus:border-[var(--accent)] focus:ring-4 focus:ring-[var(--accent-soft)] disabled:cursor-not-allowed disabled:opacity-70";

const secondaryButtonClass =
  "rounded-xl border border-[var(--border)] bg-[var(--surface)] px-4 py-2.5 text-sm font-semibold transition hover:border-[var(--accent)] hover:text-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-60";

const primaryButtonClass =
  "rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-60";

const emptyOptions: ProductOptions = {
  categories: [],
  brands: [],
  units: [],
};

type ProductFormState =
  | {
      mode: "create";
      values: ProductFormValues;
    }
  | {
      mode: "edit";
      productId: string;
      values: ProductFormValues;
    };

function statusLabel(product: Product) {
  return product.is_active ? "Активен" : "Архив";
}

function fieldLabel(field: keyof ProductFormValues) {
  const labels: Record<keyof ProductFormValues, string> = {
    sku: "Артикул",
    name: "Название",
    barcode: "Штрихкод",
    category: "Категория",
    brand: "Бренд",
    unit: "Единица измерения",
    sale_price: "Цена продажи",
    purchase_price: "Закупочная цена",
    description: "Описание",
  };

  return labels[field];
}

function formatOptional(value: string | null) {
  return value && value.trim() ? value : "—";
}

function hasFormDictionaries(options: ProductOptions | null) {
  return Boolean(options && options.categories.length > 0 && options.units.length > 0);
}

function FieldError({ message }: { message?: string }) {
  if (!message) {
    return null;
  }

  return (
    <p className="mt-2 text-sm leading-5 text-red-700 dark:text-red-300" role="alert">
      {message}
    </p>
  );
}

export function ProductManager() {
  const [products, setProducts] = useState<Product[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [isLoadingProducts, setIsLoadingProducts] = useState(true);
  const [productsError, setProductsError] = useState<string | null>(null);

  const [options, setOptions] = useState<ProductOptions | null>(null);
  const [isLoadingOptions, setIsLoadingOptions] = useState(true);
  const [optionsError, setOptionsError] = useState<string | null>(null);

  const [formState, setFormState] = useState<ProductFormState | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [archivingId, setArchivingId] = useState<string | null>(null);

  const canSubmitForm = hasFormDictionaries(options);
  const isFormOpen = formState !== null;

  const activeProductsCount = useMemo(
    () => products.filter((product) => product.is_active).length,
    [products],
  );

  async function refreshProducts(next?: { q?: string; includeArchived?: boolean }) {
    const nextQuery = next?.q ?? appliedQuery;
    const nextIncludeArchived = next?.includeArchived ?? includeArchived;

    setIsLoadingProducts(true);
    setProductsError(null);

    const result = await listProducts({
      q: nextQuery,
      includeArchived: nextIncludeArchived,
      limit: 100,
      offset: 0,
    });

    if (result.status === "success") {
      setProducts(result.data.items);
      setTotalCount(result.data.count);
    } else {
      setProductsError(result.error.message);
    }

    setIsLoadingProducts(false);
  }

  async function refreshOptions() {
    setIsLoadingOptions(true);
    setOptionsError(null);

    const result = await getProductOptions();

    if (result.status === "success") {
      setOptions(result.data);
    } else {
      setOptionsError(result.error.message);
    }

    setIsLoadingOptions(false);
  }

  useEffect(() => {
    let ignore = false;

    async function loadInitialData() {
      const [productsResult, optionsResult] = await Promise.all([
        listProducts({ limit: 100, offset: 0 }),
        getProductOptions(),
      ]);

      if (ignore) {
        return;
      }

      if (productsResult.status === "success") {
        setProducts(productsResult.data.items);
        setTotalCount(productsResult.data.count);
      } else {
        setProductsError(productsResult.error.message);
      }

      if (optionsResult.status === "success") {
        setOptions(optionsResult.data);
      } else {
        setOptionsError(optionsResult.error.message);
      }

      setIsLoadingProducts(false);
      setIsLoadingOptions(false);
    }

    void loadInitialData();

    return () => {
      ignore = true;
    };
  }, []);

  async function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedQuery = query.trim();
    setAppliedQuery(normalizedQuery);
    await refreshProducts({ q: normalizedQuery });
  }

  async function handleArchivedToggle(nextValue: boolean) {
    setIncludeArchived(nextValue);
    await refreshProducts({ includeArchived: nextValue });
  }

  function openCreateForm() {
    setFieldErrors({});
    setFormError(null);
    setNotice(null);
    setFormState({
      mode: "create",
      values: emptyProductFormValues(options ?? undefined),
    });
  }

  function openEditForm(product: Product) {
    setFieldErrors({});
    setFormError(null);
    setNotice(null);
    setFormState({
      mode: "edit",
      productId: product.id,
      values: productToFormValues(product),
    });
  }

  function closeForm() {
    if (isSaving) {
      return;
    }

    setFormState(null);
    setFieldErrors({});
    setFormError(null);
  }

  function updateFormField(field: keyof ProductFormValues, value: string) {
    setFormState((current) => {
      if (!current) {
        return current;
      }

      return {
        ...current,
        values: {
          ...current.values,
          [field]: value,
        },
      };
    });

    setFieldErrors((current) => {
      const next = { ...current };
      delete next[field];
      return next;
    });
  }

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!formState) {
      return;
    }

    setIsSaving(true);
    setFormError(null);
    setFieldErrors({});

    const result =
      formState.mode === "create"
        ? await createProduct(formState.values)
        : await updateProduct(formState.productId, formState.values);

    if (result.status === "success") {
      setNotice(
        formState.mode === "create"
          ? "Товар создан и список обновлён."
          : "Изменения товара сохранены.",
      );
      setFormState(null);
      await refreshProducts();
    } else {
      setFormError(result.error.message);
      setFieldErrors(result.error.fields);
    }

    setIsSaving(false);
  }

  async function handleArchive(product: Product) {
    const confirmed = window.confirm(
      `Архивировать товар «${product.name}»? Он исчезнет из обычного списка, но останется в ERPNext.`,
    );

    if (!confirmed) {
      return;
    }

    setNotice(null);
    setArchivingId(product.id);
    const result = await archiveProduct(product.id);

    if (result.status === "success") {
      setNotice(`Товар «${product.name}» архивирован.`);
      await refreshProducts();
    } else {
      setProductsError(result.error.message);
    }

    setArchivingId(null);
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_420px] xl:items-start">
      <section
        aria-labelledby="products-list-heading"
        className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)] sm:p-6"
      >
        <div className="mb-5 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <p className="text-sm text-[var(--muted)]">Каталог ERPNext через MyRetail API</p>
            <h2 id="products-list-heading" className="text-2xl font-semibold tracking-tight">
              Товары
            </h2>
            <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
              Показано {products.length} из {totalCount}. Активных в текущей выдаче:{" "}
              {activeProductsCount}.
            </p>
          </div>
          <button
            type="button"
            onClick={openCreateForm}
            className={primaryButtonClass}
            disabled={isLoadingOptions || !canSubmitForm}
          >
            Добавить товар
          </button>
        </div>

        <form onSubmit={handleSearch} className="mb-4 grid gap-3 lg:grid-cols-[1fr_auto]">
          <label className="block">
            <span className="text-sm font-semibold">Поиск по названию, артикулу или штрихкоду</span>
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              className={inputClass}
              placeholder="Например: DEMO-001 или молоко"
            />
          </label>
          <div className="flex items-end gap-3">
            <button type="submit" className={secondaryButtonClass} disabled={isLoadingProducts}>
              Найти
            </button>
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={isLoadingProducts}
              onClick={() => {
                setQuery("");
                setAppliedQuery("");
                void refreshProducts({ q: "" });
              }}
            >
              Сбросить
            </button>
          </div>
        </form>

        <label className="mb-5 flex w-fit items-center gap-3 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] px-4 py-3 text-sm">
          <input
            type="checkbox"
            checked={includeArchived}
            onChange={(event) => void handleArchivedToggle(event.target.checked)}
            className="size-4 accent-[var(--accent)]"
          />
          Показывать архивные товары
        </label>

        {notice ? (
          <div className="mb-4 rounded-xl border border-[var(--border)] bg-[var(--accent-soft)] px-4 py-3 text-sm leading-6 text-[var(--accent)]">
            {notice}
          </div>
        ) : null}

        {isLoadingProducts ? (
          <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--surface-muted)] p-8 text-center text-sm text-[var(--muted)]">
            Загружаем товары…
          </div>
        ) : productsError ? (
          <div className="rounded-xl border border-red-200 bg-red-50 p-5 text-sm leading-6 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
            <p className="font-semibold">Не удалось получить товары</p>
            <p className="mt-1">{productsError}</p>
            <button
              type="button"
              onClick={() => void refreshProducts()}
              className="mt-4 rounded-xl bg-red-700 px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-95"
            >
              Повторить запрос
            </button>
          </div>
        ) : products.length === 0 ? (
          <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--surface-muted)] p-8 text-center">
            <h3 className="text-lg font-semibold">Товаров пока нет</h3>
            <p className="mx-auto mt-2 max-w-xl text-sm leading-6 text-[var(--muted)]">
              Создайте первую позицию каталога. После сохранения она появится в списке и будет
              доступна через MyRetail API.
            </p>
            <button
              type="button"
              onClick={openCreateForm}
              className={`${primaryButtonClass} mt-5`}
              disabled={isLoadingOptions || !canSubmitForm}
            >
              Добавить товар
            </button>
          </div>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-[var(--border)]">
            <table className="min-w-[980px] w-full border-collapse text-left text-sm">
              <thead className="bg-[var(--surface-muted)] text-xs uppercase tracking-[0.08em] text-[var(--muted)]">
                <tr>
                  <th scope="col" className="px-4 py-3 font-semibold">
                    Товар
                  </th>
                  <th scope="col" className="px-4 py-3 font-semibold">
                    Категория
                  </th>
                  <th scope="col" className="px-4 py-3 font-semibold">
                    Штрихкод
                  </th>
                  <th scope="col" className="px-4 py-3 font-semibold">
                    Цена
                  </th>
                  <th scope="col" className="px-4 py-3 font-semibold">
                    Статус
                  </th>
                  <th scope="col" className="px-4 py-3 font-semibold">
                    Действия
                  </th>
                </tr>
              </thead>
              <tbody>
                {products.map((product) => (
                  <tr
                    key={product.id}
                    className="border-t border-[var(--border)] align-top transition hover:bg-[var(--surface-muted)]"
                  >
                    <td className="px-4 py-4">
                      <p className="font-semibold">{product.name}</p>
                      <p className="mt-1 font-mono text-xs text-[var(--muted)]">{product.sku}</p>
                      <p className="mt-2 line-clamp-2 max-w-sm text-sm leading-6 text-[var(--muted)]">
                        {product.description ?? "Описание не заполнено"}
                      </p>
                    </td>
                    <td className="px-4 py-4">
                      <p>{product.category}</p>
                      <p className="mt-1 text-xs text-[var(--muted)]">
                        Бренд: {formatOptional(product.brand)}
                      </p>
                      <p className="mt-1 text-xs text-[var(--muted)]">Ед.: {product.unit}</p>
                    </td>
                    <td className="px-4 py-4 font-mono text-xs">
                      {formatOptional(product.barcode)}
                    </td>
                    <td className="px-4 py-4">
                      <p className="font-semibold">
                        {product.sale_price} {product.currency}
                      </p>
                      <p className="mt-1 text-xs text-[var(--muted)]">
                        Закуп: {formatOptional(product.purchase_price)}
                      </p>
                    </td>
                    <td className="px-4 py-4">
                      <span
                        className={`rounded-full px-2.5 py-1 text-xs font-semibold ${
                          product.is_active
                            ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                            : "bg-[var(--warning-soft)] text-[var(--warning)]"
                        }`}
                      >
                        {statusLabel(product)}
                      </span>
                    </td>
                    <td className="px-4 py-4">
                      <div className="flex flex-col gap-2">
                        <button
                          type="button"
                          className={secondaryButtonClass}
                          onClick={() => openEditForm(product)}
                        >
                          Редактировать
                        </button>
                        <button
                          type="button"
                          className="rounded-xl border border-red-200 bg-red-50 px-4 py-2.5 text-sm font-semibold text-red-700 transition hover:bg-red-100 disabled:cursor-not-allowed disabled:opacity-60 dark:border-red-900 dark:bg-red-950 dark:text-red-300"
                          onClick={() => void handleArchive(product)}
                          disabled={!product.is_active || archivingId === product.id}
                        >
                          {archivingId === product.id ? "Архивируем…" : "Архивировать"}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <aside className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[0_12px_36px_rgba(20,32,24,0.04)] sm:p-6">
        <div className="mb-5">
          <p className="text-sm text-[var(--muted)]">Форма товара</p>
          <h2 className="text-2xl font-semibold tracking-tight">
            {formState?.mode === "edit" ? "Редактирование" : "Создание"}
          </h2>
        </div>

        {isLoadingOptions ? (
          <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--surface-muted)] p-5 text-sm leading-6 text-[var(--muted)]">
            Загружаем категории, бренды и единицы измерения…
          </div>
        ) : optionsError ? (
          <div className="rounded-xl border border-red-200 bg-red-50 p-5 text-sm leading-6 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
            <p>{optionsError}</p>
            <button
              type="button"
              className="mt-4 rounded-xl bg-red-700 px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-95"
              onClick={() => void refreshOptions()}
            >
              Повторить
            </button>
          </div>
        ) : !canSubmitForm ? (
          <div className="rounded-xl border border-[var(--border)] bg-[var(--warning-soft)] p-5 text-sm leading-6 text-[var(--warning)]">
            В ERPNext не найдены обязательные справочники категорий или единиц измерения. Создание
            товара станет доступно после заполнения справочников.
          </div>
        ) : !isFormOpen ? (
          <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--surface-muted)] p-5 text-sm leading-6 text-[var(--muted)]">
            Выберите товар для редактирования или нажмите «Добавить товар». Архивирование вынесено
            в отдельное подтверждаемое действие, поэтому `is_active` не редактируется в форме.
          </div>
        ) : (
          <ProductForm
            formState={formState}
            options={options ?? emptyOptions}
            fieldErrors={fieldErrors}
            formError={formError}
            isSaving={isSaving}
            onCancel={closeForm}
            onFieldChange={updateFormField}
            onSubmit={handleSave}
          />
        )}
      </aside>
    </div>
  );
}

function ProductForm({
  formState,
  options,
  fieldErrors,
  formError,
  isSaving,
  onCancel,
  onFieldChange,
  onSubmit,
}: {
  formState: ProductFormState;
  options: ProductOptions;
  fieldErrors: Record<string, string>;
  formError: string | null;
  isSaving: boolean;
  onCancel: () => void;
  onFieldChange: (field: keyof ProductFormValues, value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  const values = formState.values;

  return (
    <form onSubmit={onSubmit} className="space-y-5" noValidate>
      <ProductInput
        field="sku"
        value={values.sku}
        error={fieldErrors.sku}
        onChange={onFieldChange}
        disabled={isSaving || formState.mode === "edit"}
        required
        hint={formState.mode === "edit" ? "Артикул нельзя изменить после создания." : undefined}
      />

      <ProductInput
        field="name"
        value={values.name}
        error={fieldErrors.name}
        onChange={onFieldChange}
        disabled={isSaving}
        required
      />

      <ProductInput
        field="barcode"
        value={values.barcode}
        error={fieldErrors.barcode}
        onChange={onFieldChange}
        disabled={isSaving}
      />

      <ProductSelect
        field="category"
        value={values.category}
        error={fieldErrors.category}
        options={options.categories}
        onChange={onFieldChange}
        disabled={isSaving}
        required
      />

      <ProductSelect
        field="brand"
        value={values.brand}
        error={fieldErrors.brand}
        options={options.brands}
        onChange={onFieldChange}
        disabled={isSaving}
        emptyLabel="Без бренда"
      />

      <ProductSelect
        field="unit"
        value={values.unit}
        error={fieldErrors.unit}
        options={options.units}
        onChange={onFieldChange}
        disabled={isSaving}
        required
      />

      <ProductInput
        field="sale_price"
        value={values.sale_price}
        error={fieldErrors.sale_price}
        onChange={onFieldChange}
        disabled={isSaving}
        required
        inputMode="decimal"
        placeholder="650.00"
      />

      <ProductInput
        field="purchase_price"
        value={values.purchase_price}
        error={fieldErrors.purchase_price}
        onChange={onFieldChange}
        disabled={isSaving}
        inputMode="decimal"
        placeholder="510.00"
      />

      <div>
        <label htmlFor="description" className="text-sm font-semibold">
          {fieldLabel("description")}
        </label>
        <textarea
          id="description"
          value={values.description}
          onChange={(event) => onFieldChange("description", event.target.value)}
          className={`${inputClass} min-h-28 resize-y`}
          disabled={isSaving}
          maxLength={2000}
        />
        <FieldError message={fieldErrors.description} />
      </div>

      {formError ? (
        <div
          role="alert"
          className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm leading-6 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"
        >
          {formError}
        </div>
      ) : null}

      <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
        <button type="button" className={secondaryButtonClass} onClick={onCancel} disabled={isSaving}>
          Отмена
        </button>
        <button type="submit" className={primaryButtonClass} disabled={isSaving}>
          {isSaving ? "Сохраняем…" : formState.mode === "create" ? "Создать товар" : "Сохранить"}
        </button>
      </div>
    </form>
  );
}

function ProductInput({
  field,
  value,
  error,
  onChange,
  disabled,
  required,
  hint,
  inputMode,
  placeholder,
}: {
  field: keyof ProductFormValues;
  value: string;
  error?: string;
  onChange: (field: keyof ProductFormValues, value: string) => void;
  disabled?: boolean;
  required?: boolean;
  hint?: string;
  inputMode?: "decimal";
  placeholder?: string;
}) {
  return (
    <div>
      <label htmlFor={field} className="text-sm font-semibold">
        {fieldLabel(field)}
      </label>
      <input
        id={field}
        value={value}
        onChange={(event) => onChange(field, event.target.value)}
        className={inputClass}
        disabled={disabled}
        required={required}
        inputMode={inputMode}
        placeholder={placeholder}
      />
      {hint ? <p className="mt-2 text-sm leading-5 text-[var(--muted)]">{hint}</p> : null}
      <FieldError message={error} />
    </div>
  );
}

function ProductSelect({
  field,
  value,
  error,
  options,
  onChange,
  disabled,
  required,
  emptyLabel,
}: {
  field: keyof ProductFormValues;
  value: string;
  error?: string;
  options: { id: string; name: string }[];
  onChange: (field: keyof ProductFormValues, value: string) => void;
  disabled?: boolean;
  required?: boolean;
  emptyLabel?: string;
}) {
  return (
    <div>
      <label htmlFor={field} className="text-sm font-semibold">
        {fieldLabel(field)}
      </label>
      <select
        id={field}
        value={value}
        onChange={(event) => onChange(field, event.target.value)}
        className={inputClass}
        disabled={disabled}
        required={required}
      >
        {emptyLabel ? <option value="">{emptyLabel}</option> : null}
        {options.map((option) => (
          <option key={option.id} value={option.id}>
            {option.name}
          </option>
        ))}
      </select>
      <FieldError message={error} />
    </div>
  );
}
