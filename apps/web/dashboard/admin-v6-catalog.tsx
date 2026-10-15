import { useState, type FormEvent } from "react";
import { api, useApi } from "@api/client";
import type { components } from "@api/schema";
import { Button, Dialog, ErrorState, Loading } from "@ui/components";
import {
  money,
  SemanticContent,
  CatalogDescription,
  plainTelegramText,
  type ContentNode,
} from "@ui/content";
import { AdminValue } from "@ui/admin-values";
import type { Mutation } from "./admin";
import { StructuredEditor } from "./admin-v6-content";
import { ReviewChange } from "./admin-v6";
import { adminSaveCopy } from "./admin-save-copy";
import { useCapability } from "@ui/staff-permissions";

type Row = Record<string, unknown>;
type Product = components["schemas"]["ProductDto"] & {
  internal_squad_uuid?: string | null;
  recurring_plan_mapping?: Record<string, string>;
};
type Props = { mutate: (value: Mutation) => void; refreshVersion: number };
const decimal = (s: string) => {
  const [whole, fraction = ""] = s.replace(",", ".").split(".");
  return whole + "." + fraction.padEnd(2, "0");
};
const positivePricePattern = "(?=[0-9.,]*[1-9])[0-9]{1,8}([.,][0-9]{1,2})?";
export function CatalogAdmin({ mutate, refreshVersion }: Props) {
  const canWrite = useCapability("catalog_write");
  const resource = useApi<{ items: Product[] }>(
    "/admin/catalog?page=1&page_size=200",
    false,
    refreshVersion,
  );
  const [editing, setEditing] = useState<Product | "new" | null>(null);
  if (resource.error)
    return <ErrorState error={resource.error} onRetry={resource.reload} />;
  if (!resource.data) return <Loading />;
  return (
    <div className="stack">
      <div className="admin-toolbar">
        <p>
          Название, цена, порядок и видимость берутся из общего каталога.
          Скрытие тарифа сохраняет оплаченные права.
        </p>
        {canWrite && (
          <Button onClick={() => setEditing("new")}>Добавить тариф</Button>
        )}
      </div>
      <div className="v6-catalog-grid">
        {resource.data.items.map((p) => (
          <article className="card" key={p.id}>
            <span className="eyebrow">
              {p.code} · версия {p.version}
            </span>
            <h2>
              {p.icon?.fallback} {plainTelegramText(p.name)}
            </h2>
            <CatalogDescription
              nodes={p.content?.description}
              text={p.description}
            />
            <strong>
              {money(p.price, p.currency)}{" "}
              {p.base_period_days == null
                ? "· Базовый период не задан"
                : `/ ${p.base_period_days} дней`}
            </strong>
            <p>
              {p.included_devices} устройств ·{" "}
              {p.available ? "Включён" : "Выключен"}
            </p>
            <p className="fine-print">
              {p.visible_landing ? "На сайте" : "Скрыт на сайте"} ·{" "}
              {p.visible_bot ? "В боте" : "Скрыт в боте"}
            </p>
            {canWrite && (
              <Button tone="secondary" onClick={() => setEditing(p)}>
                Изменить
              </Button>
            )}
          </article>
        ))}
      </div>
      {!resource.data.items.length && (
        <p>Каталог пуст. Добавьте первый тариф.</p>
      )}
      {canWrite && (
        <PanelMapping mutate={mutate} refreshVersion={refreshVersion} />
      )}
      {editing && canWrite && (
        <CatalogEditor
          product={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSave={(value) => {
            setEditing(null);
            mutate(value);
          }}
        />
      )}
    </div>
  );
}
function PanelMapping({ mutate, refreshVersion }: Props) {
  const resource = useApi<components["schemas"]["PanelMappingDto"]>(
    "/admin/panel-mapping",
    false,
    refreshVersion,
  );
  if (resource.error)
    return <ErrorState error={resource.error} onRetry={resource.reload} />;
  return resource.data ? (
    <PanelMappingForm
      key={JSON.stringify(resource.data)}
      data={resource.data}
      mutate={mutate}
    />
  ) : (
    <Loading />
  );
}
function PanelMappingForm({
  data,
  mutate,
}: {
  data: components["schemas"]["PanelMappingDto"];
  mutate: Props["mutate"];
}) {
  const [external, setExternal] = useState(data.external_squad_uuid || ""),
    [prefix, setPrefix] = useState(data.project_prefix);
  return (
    <ReviewChange
      title={adminSaveCopy.panelMapping}
      path="/admin/panel-mapping"
      body={
        {
          external_squad_uuid: external || null,
          project_prefix: prefix,
        } satisfies Omit<
          components["schemas"]["PanelMappingUpdate"],
          "reason" | "confirmed"
        >
      }
      before={data}
      mutate={mutate}
    >
      <h2>Общие настройки Remnawave</h2>
      <label>
        UUID внешнего сквада
        <input
          pattern="[a-fA-F0-9]{8}(-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"
          value={external}
          onChange={(e) => setExternal(e.target.value)}
        />
      </label>
      <label>
        Префикс проекта
        <input
          required
          pattern="[A-Za-z][A-Za-z0-9_]{1,23}"
          value={prefix}
          maxLength={24}
          onChange={(e) => setPrefix(e.target.value)}
        />
      </label>
      <p className="fine-print">
        Префикс: 2–24 латинских символа, цифры и подчёркивание, первый символ —
        буква. Внутренний сквад задаётся в каждом тарифе. Эти настройки не
        разрешают внешние запросы: отдельный allowlist и проверка тестовой
        панели остаются обязательными.
      </p>
    </ReviewChange>
  );
}
function CatalogEditor({
  product,
  onClose,
  onSave,
}: {
  product: Product | null;
  onClose: () => void;
  onSave: Props["mutate"];
}) {
  const [v, setV] = useState({
    code: product?.code || "",
    name: product?.name || "",
    description: product?.description || "",
    price: product?.price || "",
    currency: product?.currency || "",
    tier_level: product?.tier_level ?? "",
    semantic_role: product?.semantic_role || "",
    included_devices: product?.included_devices ?? "",
    extra_device_price: product?.extra_device_price || "",
    internal_squad_uuid: product?.internal_squad_uuid || "",
    available: product?.available ?? false,
    visible_landing: product?.visible_landing ?? false,
    visible_bot: product?.visible_bot ?? false,
    sort_order: product?.sort_order ?? 0,
    highlighted: product?.highlighted ?? false,
    base_period_days: product?.base_period_days ?? "",
  });
  const [icon, setIcon] = useState({
    fallback: product?.icon?.fallback || "",
    custom_emoji_id: product?.icon?.custom_emoji_id || "",
  });
  const [periods, setPeriods] = useState<
    {
      days: number | string;
      months: number | string;
      discount_percent: string;
    }[]
  >(
    (product?.periods || []).map((p) => ({
      days: p.days,
      months: p.months,
      discount_percent: p.discount_percent,
    })),
  );
  const [eligibility, setEligibility] = useState(
    product?.eligibility || { allow_upgrade: false, allow_downgrade: false },
  );
  const [rules, setRules] = useState(product?.extra_device_rules || []);
  const [plans, setPlans] = useState(
    Object.entries(product?.recurring_plan_mapping || {}).map(
      ([period, plan]) => ({ period, plan }),
    ),
  );
  const [content, setContent] = useState(product?.content || {});
  const [initialDraft] = useState(() => ({
    v,
    icon,
    periods,
    eligibility,
    rules,
    plans,
    content,
  }));
  const [error, setError] = useState<unknown>(null),
    [busy, setBusy] = useState(false);
  function samePeriods(left: typeof periods, right: typeof periods) {
    const normalized = (rows: typeof periods) =>
      rows.map((row) => ({
        ...row,
        months: String(row.months),
      }));
    return (
      JSON.stringify(normalized(left)) === JSON.stringify(normalized(right))
    );
  }
  const validatePeriods =
    !product || !samePeriods(periods, initialDraft.periods);
  const set = (key: keyof typeof v, value: string | number | boolean) =>
    setV((current) => ({ ...current, [key]: value }));
  function valuesFor(
    draft: typeof initialDraft,
  ): components["schemas"]["ProductValues"] {
    const { code: _code, base_period_days, ...fields } = draft.v;
    return {
      ...fields,
      tier_level: Number(draft.v.tier_level),
      included_devices: Number(draft.v.included_devices),
      ...(base_period_days !== ""
        ? { base_period_days: Number(base_period_days) }
        : {}),
      price: decimal(draft.v.price),
      extra_device_price: draft.v.extra_device_price
        ? decimal(draft.v.extra_device_price)
        : null,
      internal_squad_uuid: draft.v.internal_squad_uuid || null,
      icon: draft.icon.fallback
        ? {
            fallback: draft.icon.fallback,
            custom_emoji_id: draft.icon.custom_emoji_id || null,
          }
        : null,
      periods:
        product && samePeriods(draft.periods, initialDraft.periods)
          ? initialDraft.periods
          : draft.periods.map((p) => ({
              ...p,
              days: Number(p.days),
              months: Number(p.months),
            })),
      eligibility: draft.eligibility,
      content: draft.content,
      extra_device_rules: draft.rules.map((r) => ({
        ...r,
        price: decimal(r.price),
      })),
      recurring_plan_mapping: Object.fromEntries(
        draft.plans.map((p) => [p.period, p.plan]),
      ),
    };
  }
  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (
      rules.length &&
      (rules.at(-1)?.through_extra !== null ||
        rules
          .slice(0, -1)
          .some(
            (r, i) =>
              r.through_extra === null ||
              r.through_extra <= (i ? (rules[i - 1].through_extra ?? 0) : 0),
          ))
    ) {
      setError(
        new Error(
          "Границы ступеней должны возрастать; последняя ступень — без верхней границы.",
        ),
      );
      return;
    }
    if (
      new Set(periods.map((p) => p.days)).size !== periods.length ||
      new Set(plans.map((p) => p.period)).size !== plans.length
    ) {
      setError(
        new Error("Периоды и соответствия планов не должны повторяться."),
      );
      return;
    }
    const values = valuesFor({
      v,
      icon,
      periods,
      eligibility,
      content,
      rules,
      plans,
    });
    const initialValues = valuesFor(initialDraft);
    const body: Row = product
      ? Object.fromEntries(
          Object.entries(values).filter(
            ([key, value]) =>
              JSON.stringify(value) !==
              JSON.stringify(initialValues[key as keyof typeof initialValues]),
          ),
        )
      : { ...values, code: v.code };
    const path = product
      ? `/admin/catalog/${encodeURIComponent(product.id)}`
      : "/admin/catalog";
    let impact: Row | null = null;
    if (product) {
      setBusy(true);
      try {
        impact = await api<Row>(`${path}/impact`, { method: "POST", body });
      } catch (failure) {
        setError(failure);
        return;
      } finally {
        setBusy(false);
      }
    }
    onSave({
      requireReason:
        !product ||
        Object.keys(body).some(
          (key) => !["name", "description", "content", "icon"].includes(key),
        ),
      confirmLabel: adminSaveCopy.confirmSave,
      title: product
        ? "Подтвердить изменение тарифа"
        : "Подтвердить создание тарифа",
      description:
        "Цены и права действующих оплаченных периодов сохраняются. Проверьте данные и влияние изменений перед сохранением.",
      path,
      method: product ? "PATCH" : "POST",
      body,
      preview: (
        <>
          <h3>До изменения</h3>
          {product ? <AdminValue value={product} /> : <p>Новый тариф</p>}
          <h3>После изменения</h3>
          <AdminValue value={body} />
          {impact && (
            <>
              <h3>Влияние по данным сервера</h3>
              <AdminValue value={impact} />
            </>
          )}
        </>
      ),
    });
  }
  const textFields: [keyof typeof v, string][] = [
    ["name", "Название тарифа"],
    ["semantic_role", "Семантический ключ тарифа"],
    ["currency", "Валюта"],
    ["internal_squad_uuid", "UUID внутреннего сквада Remnawave"],
  ];
  const numberFields: [keyof typeof v, string, number, number][] = [
    ["tier_level", "Ранг тарифа", 1, 1000],
    ["included_devices", "Включённые устройства", 1, 100],
    ["sort_order", "Порядок отображения", -10000, 10000],
    ["base_period_days", "Базовый период, дни", 1, 730],
  ];
  return (
    <Dialog
      title={product ? "Изменить тариф" : "Добавить тариф"}
      onClose={onClose}
    >
      <form className="form v6-form" onSubmit={submit}>
        {product ? (
          <p>
            ID: {product.id} · ключ: {product.code} · версия {product.version}
          </p>
        ) : (
          <label>
            Постоянный ключ тарифа
            <input
              required
              pattern="[a-z0-9_]{1,64}"
              maxLength={64}
              value={v.code}
              onChange={(e) => set("code", e.target.value)}
            />
          </label>
        )}
        {textFields.map(([key, label]) => (
          <label key={key}>
            {label}
            <input
              required={key !== "internal_squad_uuid"}
              pattern={
                key === "currency"
                  ? "[A-Z]{3}"
                  : key === "semantic_role"
                    ? "[A-Za-z0-9_]{1,64}"
                    : key === "internal_squad_uuid"
                      ? "[a-fA-F0-9]{8}(-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"
                      : undefined
              }
              maxLength={
                key === "currency"
                  ? 3
                  : key === "name"
                    ? 200
                    : key === "semantic_role"
                      ? 64
                      : 36
              }
              value={String(v[key])}
              onChange={(e) => set(key, e.target.value)}
            />
          </label>
        ))}
        <label>
          Краткое описание
          <textarea
            maxLength={4000}
            value={v.description}
            onChange={(e) => set("description", e.target.value)}
          />
        </label>
        <div className="field-grid">
          <label>
            Базовая цена
            <input
              required
              inputMode="decimal"
              pattern={positivePricePattern}
              value={v.price}
              onChange={(e) => set("price", e.target.value)}
            />
          </label>
          {numberFields.map(([key, label, min, max]) => (
            <label key={key}>
              {label}
              <input
                type="number"
                required={!product || key !== "base_period_days"}
                step={1}
                min={min}
                max={max}
                value={String(v[key])}
                onChange={(e) =>
                  set(key, e.target.value === "" ? "" : Number(e.target.value))
                }
              />
              {key === "base_period_days" && product && (
                <span className="fine-print">
                  Пустое поле сохраняет текущие условия.
                </span>
              )}
            </label>
          ))}
        </div>
        <div className="field-grid">
          {(
            [
              ["available", "Тариф включён"],
              ["visible_landing", "Показывать на сайте"],
              ["visible_bot", "Показывать в боте"],
              ["highlighted", "Рекомендуемый тариф"],
            ] as const
          ).map(([key, label]) => (
            <label className="checkbox-label" key={key}>
              <input
                type="checkbox"
                checked={v[key]}
                onChange={(e) => set(key, e.target.checked)}
              />
              {label}
            </label>
          ))}
        </div>
        <fieldset>
          <legend>Значок тарифа</legend>
          <div className="field-grid">
            <label>
              Запасной символ тарифа
              <input
                maxLength={16}
                required={Boolean(icon.custom_emoji_id)}
                value={icon.fallback}
                onChange={(e) =>
                  setIcon((i) => ({ ...i, fallback: e.target.value }))
                }
              />
            </label>
            <label>
              ID эмодзи тарифа
              <input
                inputMode="numeric"
                pattern="[0-9]{1,32}"
                value={icon.custom_emoji_id}
                onChange={(e) =>
                  setIcon((i) => ({ ...i, custom_emoji_id: e.target.value }))
                }
              />
            </label>
          </div>
        </fieldset>
        <fieldset>
          <legend>Переходы между тарифами</legend>
          {(
            [
              ["allow_upgrade", "Разрешить повышение тарифа"],
              ["allow_downgrade", "Разрешить понижение тарифа"],
            ] as const
          ).map(([key, label]) => (
            <label className="checkbox-label" key={key}>
              <input
                type="checkbox"
                checked={eligibility[key]}
                onChange={(e) =>
                  setEligibility((v) => ({ ...v, [key]: e.target.checked }))
                }
              />
              {label}
            </label>
          ))}
        </fieldset>
        <fieldset>
          <legend>Периоды и скидки</legend>
          <p className="fine-print">
            Стоимость периода рассчитывает сервер. Дни доступа и число расчётных
            месяцев задаются отдельно.
          </p>
          {periods.map((p, index) => (
            <div className="v6-period-row" key={index}>
              {(
                [
                  ["days", "Дни доступа"],
                  ["months", "Расчётные месяцы"],
                  ["discount_percent", "Скидка, %"],
                ] as const
              ).map(([key, label]) => (
                <label key={key}>
                  {label}
                  <input
                    required={validatePeriods}
                    type="number"
                    min={
                      validatePeriods
                        ? key === "discount_percent"
                          ? 0
                          : 1
                        : undefined
                    }
                    max={
                      !validatePeriods
                        ? undefined
                        : key === "discount_percent"
                          ? 99.99
                          : key === "days"
                            ? 730
                            : 24
                    }
                    step={
                      !validatePeriods
                        ? "any"
                        : key === "discount_percent"
                          ? "0.01"
                          : 1
                    }
                    value={p[key]}
                    onChange={(e) =>
                      setPeriods((v) =>
                        v.map((r, i) =>
                          i === index
                            ? {
                                ...r,
                                [key]:
                                  key === "discount_percent" ||
                                  key === "months" ||
                                  e.target.value === ""
                                    ? e.target.value
                                    : Number(e.target.value),
                              }
                            : r,
                        ),
                      )
                    }
                  />
                </label>
              ))}
              <Button
                type="button"
                tone="ghost"
                onClick={() =>
                  setPeriods((v) => v.filter((_, i) => i !== index))
                }
              >
                Удалить период
              </Button>
            </div>
          ))}
          <Button
            tone="secondary"
            type="button"
            onClick={() =>
              setPeriods((v) => [
                ...v,
                {
                  days: "",
                  months: "",
                  discount_percent: "",
                },
              ])
            }
          >
            Добавить период
          </Button>
        </fieldset>
        <fieldset>
          <legend>Дополнительные устройства</legend>
          <label>
            Линейная цена места (если нет ступеней)
            <input
              pattern={positivePricePattern}
              inputMode="decimal"
              value={v.extra_device_price}
              onChange={(e) => set("extra_device_price", e.target.value)}
            />
          </label>
          <p className="fine-print">
            Пустая цена означает неизвестную стоимость. При наличии ступеней
            линейная цена не используется. Цена ступени — за каждое
            дополнительное место в её диапазоне.
          </p>
          {rules.map((r, index) => (
            <div className="v6-period-row" key={index}>
              <label>
                До дополнительного места включительно
                <input
                  type="number"
                  min={1}
                  max={100}
                  step={1}
                  placeholder="Без верхней границы"
                  value={r.through_extra ?? ""}
                  onChange={(e) =>
                    setRules((v) =>
                      v.map((x, i) =>
                        i === index
                          ? {
                              ...x,
                              through_extra: e.target.value
                                ? Number(e.target.value)
                                : null,
                            }
                          : x,
                      ),
                    )
                  }
                />
              </label>
              <label>
                Цена места на ступени
                <input
                  required
                  pattern={positivePricePattern}
                  inputMode="decimal"
                  value={r.price}
                  onChange={(e) =>
                    setRules((v) =>
                      v.map((x, i) =>
                        i === index ? { ...x, price: e.target.value } : x,
                      ),
                    )
                  }
                />
              </label>
              <Button
                type="button"
                tone="ghost"
                onClick={() => setRules((v) => v.filter((_, i) => i !== index))}
              >
                Удалить ступень
              </Button>
            </div>
          ))}
          <Button
            tone="secondary"
            type="button"
            onClick={() =>
              setRules((v) => [...v, { through_extra: null, price: "" }])
            }
          >
            Добавить ступень
          </Button>
        </fieldset>
        <fieldset>
          <legend>Сопоставление периодов с планами автопродления</legend>
          <p className="fine-print">
            ID плана не подтверждает доступность или разрешение на списание.
            Возможности проверяются сервером и провайдером.
          </p>
          {plans.map((p, index) => (
            <div className="v6-period-row" key={index}>
              <label>
                Период плана, дни
                <input
                  required
                  type="number"
                  min={1}
                  step={1}
                  value={p.period}
                  onChange={(e) =>
                    setPlans((v) =>
                      v.map((r, i) =>
                        i === index ? { ...r, period: e.target.value } : r,
                      ),
                    )
                  }
                />
              </label>
              <label>
                ID плана провайдера
                <input
                  required
                  maxLength={128}
                  value={p.plan}
                  onChange={(e) =>
                    setPlans((v) =>
                      v.map((r, i) =>
                        i === index ? { ...r, plan: e.target.value } : r,
                      ),
                    )
                  }
                />
              </label>
              <Button
                type="button"
                tone="ghost"
                onClick={() => setPlans((v) => v.filter((_, i) => i !== index))}
              >
                Удалить сопоставление
              </Button>
            </div>
          ))}
          <Button
            tone="secondary"
            type="button"
            onClick={() => setPlans((v) => [...v, { period: "", plan: "" }])}
          >
            Добавить план
          </Button>
        </fieldset>
        {(["name", "description"] as const).map((key) => (
          <StructuredEditor
            key={key}
            label={
              key === "name" ? "Оформление названия" : "Подробное описание"
            }
            nodes={(content[key] as ContentNode[]) || []}
            onChange={(nodes) => setContent((c) => ({ ...c, [key]: nodes }))}
          />
        ))}
        <div className="content-previews">
          <section>
            <h3>Предпросмотр сайта</h3>
            <div className="content-preview">
              <span className="eyebrow">
                {v.highlighted ? "Рекомендуемый тариф" : "Тариф"}
              </span>
              <h3>
                {icon.fallback}{" "}
                {(content.name as ContentNode[])?.length ? (
                  <SemanticContent nodes={content.name as ContentNode[]} />
                ) : (
                  v.name
                )}
              </h3>
              <CatalogDescription
                nodes={content.description}
                text={v.description}
              />
              <strong>
                {money(v.price.replace(",", "."), v.currency)}{" "}
                {v.base_period_days === ""
                  ? "· Базовый период не задан"
                  : `/ ${v.base_period_days} дней`}
              </strong>
              <p>{v.included_devices} устройств</p>
              <p>
                {v.available && v.visible_landing
                  ? "Показывается на сайте"
                  : "Скрыт на сайте"}
              </p>
            </div>
          </section>
          <section>
            <h3>Предпросмотр Telegram</h3>
            <div className="content-preview telegram-preview">
              <h3>
                {icon.fallback}{" "}
                {(content.name as ContentNode[])?.length ? (
                  <SemanticContent nodes={content.name as ContentNode[]} />
                ) : (
                  v.name
                )}
              </h3>
              <CatalogDescription
                nodes={content.description}
                text={v.description}
              />
              <strong>
                {money(v.price.replace(",", "."), v.currency)}{" "}
                {v.base_period_days === ""
                  ? "· Базовый период не задан"
                  : `/ ${v.base_period_days} дней`}
              </strong>
              <p>
                {v.included_devices} устройств ·{" "}
                {v.available && v.visible_bot
                  ? "Показывается в боте"
                  : "Скрыт в боте"}
              </p>
            </div>
          </section>
        </div>
        <p className="fine-print">
          Предпросмотр использует текущую форму и запасной символ. Цены
          периодов, комиссии и себестоимость проверяются сервером; неизвестная
          комиссия не равна нулю. Реальное отображение эмодзи требует проверки
          тестового Telegram API.
        </p>
        {Boolean(error) && <ErrorState error={error} />}
        <div className="dialog-actions">
          <Button type="button" tone="secondary" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" busy={busy}>
            {adminSaveCopy.tariff}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
