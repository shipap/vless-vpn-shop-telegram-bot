import { messages } from "@ui/messages";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { ArrowRight, Plus } from "lucide-react";
import { Button, Dialog, ErrorState } from "@ui/components";
import {
  SemanticContent,
  validatedContentNodes,
  type ContentNode,
} from "@ui/content";
import { fieldLabel } from "@ui/ru";
import type { Mutation } from "./admin";
import { api, useApi } from "@api/client";
import type { Catalog } from "@api/types";
import { PromoEditor, promoPayload, type PromoDraft } from "./promo-editor";
import { AdminValue } from "@ui/admin-values";
import { adminSaveCopy } from "./admin-save-copy";
import {
  BroadcastAudience,
  BroadcastCounts,
  broadcastCopy,
} from "./broadcast-audience";
type Row = Record<string, unknown>;
export type BroadcastMediaRef = {
  asset_id: string;
  sha256: string;
  mime: "image/jpeg" | "image/png" | "video/mp4";
  size_bytes: number;
  proof: string;
};
export function BroadcastAttachment({
  value,
  onChange,
  onBlockedChange,
}: {
  value: BroadcastMediaRef | null;
  onChange: (value: BroadcastMediaRef | null) => void;
  onBlockedChange: (blocked: boolean) => void;
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const fileId = useId();
  const fileInput = useRef<HTMLInputElement>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  async function upload(file: File) {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setName(file.name);
    setError(null);
    onChange(null);
    onBlockedChange(true);
    const limit = file.type === "video/mp4" ? 8_000_000 : 2_000_000;
    if (
      !["image/jpeg", "image/png", "video/mp4"].includes(file.type) ||
      !file.size ||
      file.size > limit
    ) {
      setBusy(false);
      setError(
        new Error("Выберите фото JPEG/PNG до 2 МБ или видео MP4 до 8 МБ."),
      );
      return;
    }
    setBusy(true);
    try {
      const result = await api<BroadcastMediaRef>("/admin/broadcasts/media", {
        method: "POST",
        rawBody: file,
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (
        !result ||
        typeof result.asset_id !== "string" ||
        !/^[a-f0-9]{32}$/.test(result.asset_id) ||
        typeof result.sha256 !== "string" ||
        !/^[a-f0-9]{64}$/.test(result.sha256) ||
        typeof result.proof !== "string" ||
        !/^[a-f0-9]{64}$/.test(result.proof) ||
        result.mime !== file.type ||
        !Number.isSafeInteger(result.size_bytes) ||
        result.size_bytes <= 0 ||
        result.size_bytes > limit
      ) {
        throw new Error(
          "Не удалось подтвердить загруженный файл. Выберите его снова.",
        );
      }
      onChange({
        asset_id: result.asset_id,
        sha256: result.sha256,
        mime: result.mime,
        size_bytes: result.size_bytes,
        proof: result.proof,
      });
      onBlockedChange(false);
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure);
    } finally {
      if (request.current === controller) setBusy(false);
    }
  }
  function remove() {
    request.current?.abort();
    request.current = null;
    setName("");
    setError(null);
    setBusy(false);
    onChange(null);
    onBlockedChange(false);
  }
  return (
    <div className="stack">
      <label htmlFor={fileId}>Фото или видео</label>
      <div className="admin-file-control">
        <input
          id={fileId}
          ref={fileInput}
          hidden
          type="file"
          accept="image/jpeg,image/png,video/mp4"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void upload(file);
          }}
        />
        <Button
          type="button"
          tone="secondary"
          onClick={() => fileInput.current?.click()}
        >
          Выбрать файл
        </Button>
        <span className="small-copy">JPEG, PNG или MP4</span>
      </div>
      <p className="fine-print">
        Один файл. Фото JPEG/PNG — до 2 МБ, видео MP4 — до 8 МБ. В email файл
        придёт вложением; общий размер письма — до 12 МБ.
      </p>
      {name && (
        <div className="row">
          <span>
            {name}
            {busy
              ? " · Загрузка…"
              : value
                ? " · Файл загружен"
                : " · Файл не прикреплён"}
          </span>
          <Button type="button" tone="secondary" onClick={remove}>
            Убрать файл
          </Button>
        </div>
      )}
      {Boolean(error) && <ErrorState error={error} />}
    </div>
  );
}
const decimal = (value: unknown) => {
  const text = String(value ?? "").replace(",", ".");
  return /^-?\d+$/.test(text)
    ? `${text}.00`
    : /^-?\d+\.\d$/.test(text)
      ? `${text}0`
      : text;
};
export function Editor({
  section,
  row,
  onClose,
  onSave,
}: {
  section: string;
  row: Row;
  onClose: () => void;
  onSave: (value: Mutation) => void;
}) {
  const source =
    section === "discounts" && row.value && !Array.isArray(row.value)
      ? (row.value as Row)
      : row;
  const [values, setValues] = useState<Row>({
    ...source,
    key: row.key || String(row.id || "").replace("content:", ""),
  });
  const [nodes, setNodes] = useState(
    JSON.stringify(row.nodes || (row.content as Row)?.nodes || [], null, 2),
  );
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [media, setMedia] = useState<BroadcastMediaRef | null>(null);
  const [mediaBlocked, setMediaBlocked] = useState(false);
  const [catalogContent, setCatalogContent] = useState({
    name: JSON.stringify((row.content as Row)?.name || [], null, 2),
    description: JSON.stringify(
      (row.content as Row)?.description || [],
      null,
      2,
    ),
  });
  const [compensation, setCompensation] = useState("1");
  const [loyalty, setLoyalty] = useState<
    { elapsed_periods: number | string; percent: string }[]
  >(
    Array.isArray(source.loyalty_milestones)
      ? (source.loyalty_milestones as {
          elapsed_periods: number;
          percent: string;
        }[])
      : [],
  );
  const [promos, setPromos] = useState<PromoDraft[]>(
    Array.isArray(source.promos) ? (source.promos as PromoDraft[]) : [],
  );
  const promoCatalog = useApi<Catalog>(
    section === "discounts" && row.id !== "periods" ? "/catalog" : null,
  );
  const [periods, setPeriods] = useState<
    { days: number; months: number; discount_percent: string }[]
  >(
    Array.isArray(row.value)
      ? (row.value as {
          days: number;
          months: number;
          discount_percent: string;
        }[])
      : [],
  );
  const set = (key: string, value: unknown) =>
    setValues((v) => ({ ...v, [key]: value }));
  let parsed: ContentNode[] | null = null;
  try {
    const value = JSON.parse(nodes);
    parsed =
      Array.isArray(value) && value.length === 0
        ? []
        : validatedContentNodes(value);
  } catch {}
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (mediaBlocked) return;
    setError("");
    let body: Row = {};
    let path = "";
    let method = "PATCH";
    if (section === "catalog") {
      body = {
        name: values.name,
        description: values.description,
        price: decimal(values.price),
        included_devices: Number(values.included_devices),
        available: Boolean(values.available),
      };
      try {
        const content = {
          name: JSON.parse(catalogContent.name),
          description: JSON.parse(catalogContent.description),
        };
        if (!Array.isArray(content.name) || !Array.isArray(content.description))
          throw new Error();
        if (content.name.length || content.description.length || row.content)
          body.content = content;
      } catch {
        setError(messages.m2b016350eb);
        return;
      }
      if (
        values.extra_device_price != null &&
        String(values.extra_device_price) !== ""
      )
        body.extra_device_price = decimal(values.extra_device_price);
      if (values.internal_squad_uuid)
        body.internal_squad_uuid = values.internal_squad_uuid;
      path = `/admin/catalog/${encodeURIComponent(String(row.id))}`;
    }
    if (section === "content") {
      try {
        const content = JSON.parse(nodes);
        if (parsed === null) throw new Error();
        body = { nodes: content };
      } catch {
        setError(messages.m2b016350eb);
        return;
      }
      path = `/admin/content/${encodeURIComponent(String(values.key))}`;
    }
    if (section === "discounts") {
      if (row.id === "periods") {
        body = { periods };
        path = "/admin/periods";
      } else {
        body = {
          cap: String(values.cap ?? ""),
          bonus_cap: String(values.bonus_cap ?? ""),
          referral_percent: String(values.referral_percent ?? ""),
          promos: promos.map(promoPayload),
          loyalty_period_days: Number(values.loyalty_period_days),
          loyalty_milestones: loyalty.map((item) => ({
            ...item,
            elapsed_periods: Number(item.elapsed_periods),
          })),
          ...(values.renewal_campaign
            ? { renewal_campaign: values.renewal_campaign }
            : {}),
          ...(values.near_expiry_renewal
            ? { near_expiry_renewal: values.near_expiry_renewal }
            : {}),
          ...(values.referral_welcome
            ? { referral_welcome: values.referral_welcome }
            : {}),
        };
        path = "/admin/discounts";
      }
    }
    if (section === "broadcasts") {
      body = {
        nodes: [{ type: "paragraph", children: [{ type: "text", text }] }],
        media,
      };
      path = "/admin/broadcasts";
      method = "POST";
    }
    if (section === "subscriptions") {
      body = { days: Number(compensation) };
      path = `/admin/subscriptions/${encodeURIComponent(String(row.id))}/compensate`;
      method = "POST";
    }
    if (section === "users") {
      body = {
        amount: decimal(values.amount),
        operation_id: crypto.randomUUID(),
      };
      path = `/admin/users/${encodeURIComponent(String(row.id))}/bonus`;
      method = "POST";
      if (row.edit_kind === "credit") {
        body = {
          ...body,
          currency: String(
            values.currency || row.credit_currency || "",
          ).toUpperCase(),
          provenance: values.provenance || "owner_adjustment",
          confirm_existing_currency: Boolean(values.confirm_existing_currency),
        };
        path = `/admin/users/${encodeURIComponent(String(row.id))}/credit`;
      }
    }
    let impact: Row | null = null;
    if (section === "catalog" || section === "broadcasts") {
      setBusy(true);
      try {
        impact = await api<Row>(
          section === "broadcasts"
            ? "/admin/broadcasts/estimate"
            : `${path}/impact`,
          { method: "POST", body },
        );
      } catch (failure) {
        setError(
          failure instanceof Error
            ? failure.message
            : "Не удалось проверить влияние изменений.",
        );
        return;
      } finally {
        setBusy(false);
      }
    }
    onSave({
      requireReason: section !== "content",
      confirmLabel:
        section === "broadcasts"
          ? adminSaveCopy.confirmCreate
          : adminSaveCopy.confirmSave,
      title:
        section === "broadcasts" ? messages.m322c19c740 : messages.mb6b0a0b86b,
      description:
        section === "broadcasts"
          ? broadcastCopy.confirmation
          : messages.me79c35d868,
      path,
      method,
      body,
      preview: (
        <>
          {impact &&
            (section === "broadcasts" ? (
              <BroadcastCounts value={impact} />
            ) : (
              <AdminValue value={impact} />
            ))}
          <dl className="definition">
            {Object.entries(body)
              .filter(([key]) => key !== "operation_id")
              .map(([key, value]) => (
                <div key={key} className="preview-row">
                  <dt>{fieldLabel(key)}</dt>
                  <dd>
                    {typeof value === "object" ? (
                      <AdminValue value={value} name={key} />
                    ) : (
                      `${String(source[key] ?? "—")} → ${String(value)}`
                    )}
                  </dd>
                </div>
              ))}
          </dl>
        </>
      ),
    });
  }
  const title =
    section === "content"
      ? messages.m3f3c524485
      : section === "catalog"
        ? messages.mf25c315f5d
        : section === "subscriptions"
          ? messages.mc72751d40b
          : section === "broadcasts"
            ? messages.m15f7fedc31
            : section === "users"
              ? row.edit_kind === "credit"
                ? "Изменить денежные средства"
                : messages.m9aa22a2541
              : messages.m226edfe85d;
  return (
    <Dialog title={title} onClose={onClose}>
      <form className="form" onSubmit={submit}>
        {section === "catalog" && (
          <>
            <label>
              {messages.m0918b4ba92}
              <input
                required
                value={String(values.name || "")}
                onChange={(e) => set("name", e.target.value)}
              />
            </label>
            <label>
              {messages.mb3680f2cba}
              <textarea
                value={String(values.description || "")}
                onChange={(e) => set("description", e.target.value)}
              />
            </label>
            <div className="field-grid">
              <label>
                {messages.m24edb0ce64}
                <input
                  required
                  inputMode="decimal"
                  pattern="\d+(\.\d{1,2})?"
                  value={String(values.price || "")}
                  onChange={(e) =>
                    set("price", e.target.value.replace(",", "."))
                  }
                />
              </label>
              <label>
                {messages.mcd7b1c370a}
                <input
                  required
                  type="number"
                  min="1"
                  max="100"
                  value={String(values.included_devices ?? "")}
                  onChange={(e) => set("included_devices", e.target.value)}
                />
              </label>
              <label>
                {messages.m0570f6b44b}
                <input
                  inputMode="decimal"
                  pattern="\d+(\.\d{1,2})?"
                  value={String(values.extra_device_price ?? "")}
                  onChange={(e) => set("extra_device_price", e.target.value)}
                />
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={Boolean(values.available)}
                  onChange={(e) => set("available", e.target.checked)}
                />
                {messages.ma85c69a5cf}
              </label>
            </div>
            <label>
              {messages.mf3d879426b}
              <input
                value={String(values.internal_squad_uuid || "")}
                onChange={(e) => set("internal_squad_uuid", e.target.value)}
                pattern="[a-fA-F0-9-]{36}"
                placeholder={messages.m0311b10327}
              />
            </label>
            <p className="fine-print">{messages.m8a77a1701f}</p>
            <details>
              <summary>Форматирование и пользовательские эмодзи</summary>
              {(["name", "description"] as const).map((key) => (
                <label key={key}>
                  {key === "name" ? "Структура названия" : "Структура описания"}
                  <textarea
                    className="code-editor"
                    rows={5}
                    value={catalogContent[key]}
                    onChange={(event) =>
                      setCatalogContent((current) => ({
                        ...current,
                        [key]: event.target.value,
                      }))
                    }
                    spellCheck={false}
                  />
                </label>
              ))}
              <p className="fine-print">
                Структурированный контент сохраняет оформление и ID эмодзи
                строками. Каждый эмодзи должен иметь текстовый запасной символ и
                понятную подпись. Пустая структура использует обычный текст.
              </p>
            </details>
          </>
        )}
        {section === "content" && (
          <>
            <label>
              {messages.mddf87e5cb3}
              <input
                required
                value={String(values.key || "")}
                onChange={(e) => set("key", e.target.value)}
                pattern="[a-zA-Z0-9_-]+"
              />
            </label>
            <label>
              {messages.m7cc69046c5}
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
            </label>
            <Button
              type="button"
              tone="secondary"
              disabled={!text.trim() || parsed === null}
              onClick={() => {
                setNodes(
                  JSON.stringify(
                    [
                      ...(parsed ?? []),
                      { type: "paragraph", children: [{ type: "text", text }] },
                    ],
                    null,
                    2,
                  ),
                );
                setText("");
              }}
            >
              <Plus size={15} />
              {messages.mb26b2140c2}
            </Button>
            <details>
              <summary>{messages.mae9bb54804}</summary>
              <label>
                {messages.mc47316b597}
                <textarea
                  className="code-editor"
                  rows={9}
                  value={nodes}
                  onChange={(e) => setNodes(e.target.value)}
                  spellCheck={false}
                />
              </label>
            </details>
            {parsed === null && (
              <p className="notice warning" role="alert">
                Проверьте структуру JSON. Предпросмотр недоступен; исходный
                текст сохранён в редакторе.
              </p>
            )}
            <div className="content-previews">
              <div>
                <span className="eyebrow">{messages.mda2c41d49b}</span>
                <div className="content-preview">
                  <SemanticContent nodes={parsed ?? []} />
                </div>
              </div>
              <div>
                <span className="eyebrow">{messages.mbeff4a7d2e}</span>
                <div className="content-preview telegram-preview">
                  <SemanticContent nodes={parsed ?? []} />
                </div>
              </div>
            </div>
            <p className="fine-print">{messages.m77d1cceee7}</p>
          </>
        )}
        {section === "discounts" &&
          (row.id === "periods" ? (
            <>
              {periods.map((period, index) => (
                <div className="field-grid" key={index}>
                  <label>
                    {messages.m7498014ab3}
                    <input
                      required
                      type="number"
                      min="1"
                      value={period.days}
                      onChange={(e) =>
                        setPeriods((items) =>
                          items.map((p, i) =>
                            i === index
                              ? { ...p, days: Number(e.target.value) }
                              : p,
                          ),
                        )
                      }
                    />
                  </label>
                  <label>
                    {messages.m5f919b5387}
                    <input
                      required
                      type="number"
                      min="1"
                      value={period.months}
                      onChange={(e) =>
                        setPeriods((items) =>
                          items.map((p, i) =>
                            i === index
                              ? { ...p, months: Number(e.target.value) }
                              : p,
                          ),
                        )
                      }
                    />
                  </label>
                  <label>
                    {messages.m7153e4db89}
                    <input
                      required
                      inputMode="decimal"
                      value={period.discount_percent}
                      onChange={(e) =>
                        setPeriods((items) =>
                          items.map((p, i) =>
                            i === index
                              ? { ...p, discount_percent: e.target.value }
                              : p,
                          ),
                        )
                      }
                    />
                  </label>
                </div>
              ))}
              <p className="fine-print">{messages.m1057695ca1}</p>
            </>
          ) : (
            <>
              <div className="field-grid">
                {[
                  ["cap", messages.m4a56dad1b4],
                  ["bonus_cap", messages.mf5f67c424d],
                  ["referral_percent", messages.md4d5b4f817],
                ].map(([key, label]) => (
                  <label key={key}>
                    {label}
                    <input
                      required
                      inputMode="decimal"
                      value={String(values[key] ?? "")}
                      onChange={(e) => set(key, e.target.value)}
                    />
                  </label>
                ))}
              </div>
              <h3>{messages.mc35782a277}</h3>
              <h3>Стаж непрерывной оплаты</h3>
              <label>
                Длительность периода стажа, дней
                <input
                  type="number"
                  min="1"
                  max="365"
                  required
                  value={String(values.loyalty_period_days ?? "")}
                  onChange={(event) =>
                    set("loyalty_period_days", event.target.value)
                  }
                />
              </label>
              <p className="fine-print">
                Стаж растёт по фактически прошедшему оплаченному времени.
                Предоплата за год не начисляет стаж заранее. Срок подписки и
                число месяцев расчёта цены учитываются отдельно.
              </p>
              {loyalty.map((item, index) => (
                <div className="field-grid" key={index}>
                  <label>
                    Оплаченных периодов
                    <input
                      type="number"
                      required
                      min="1"
                      value={item.elapsed_periods}
                      onChange={(event) =>
                        setLoyalty((rows) =>
                          rows.map((row, i) =>
                            i === index
                              ? {
                                  ...row,
                                  elapsed_periods: event.target.value,
                                }
                              : row,
                          ),
                        )
                      }
                    />
                  </label>
                  <label>
                    Скидка за стаж, %
                    <input
                      required
                      inputMode="decimal"
                      pattern="\d+(\.\d{1,2})?"
                      value={item.percent}
                      onChange={(event) =>
                        setLoyalty((rows) =>
                          rows.map((row, i) =>
                            i === index
                              ? { ...row, percent: event.target.value }
                              : row,
                          ),
                        )
                      }
                    />
                  </label>
                </div>
              ))}
              <Button
                type="button"
                tone="secondary"
                onClick={() =>
                  setLoyalty((items) => [
                    ...items,
                    { elapsed_periods: "", percent: "" },
                  ])
                }
              >
                Добавить ступень стажа
              </Button>
              {(
                [
                  "renewal_campaign",
                  "near_expiry_renewal",
                  "referral_welcome",
                ] as const
              ).map((key) => {
                const policy = values[key] as Row | undefined;
                return (
                  <fieldset key={key}>
                    <legend>
                      {key === "renewal_campaign"
                        ? "Кампания продления"
                        : key === "near_expiry_renewal"
                          ? "Продление перед окончанием срока"
                          : "Скидка приглашённому клиенту"}
                    </legend>
                    {key === "near_expiry_renewal" && (
                      <p className="fine-print">
                        Для ручного продления, когда до подтверждённого
                        окончания доступа осталось меньше 24 часов или срок уже
                        истёк. Итоговый расчёт выбирает одну наиболее выгодную
                        вторичную скидку.
                      </p>
                    )}
                    <label className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={Boolean(policy?.enabled)}
                        onChange={(event) =>
                          set(key, {
                            ...policy,
                            enabled: event.target.checked,
                            percent: policy?.percent ?? "",
                          })
                        }
                      />
                      Включена
                    </label>
                    <label>
                      Скидка кампании, %
                      <input
                        required={Boolean(policy)}
                        inputMode="decimal"
                        pattern="\d+(\.\d{1,2})?"
                        value={String(policy?.percent ?? "")}
                        onChange={(event) =>
                          set(key, {
                            ...policy,
                            enabled: Boolean(policy?.enabled),
                            percent: event.target.value,
                          })
                        }
                      />
                    </label>
                  </fieldset>
                );
              })}
              {promos.map((promo, index) => (
                <PromoEditor
                  key={index}
                  promo={promo}
                  index={index}
                  products={promoCatalog.data?.products || []}
                  loading={promoCatalog.loading}
                  error={promoCatalog.error}
                  onRetry={promoCatalog.reload}
                  onChange={(next) =>
                    setPromos((items) =>
                      items.map((item, i) => (i === index ? next : item)),
                    )
                  }
                  onRemove={() =>
                    setPromos((items) => items.filter((_, i) => i !== index))
                  }
                />
              ))}
              <Button
                type="button"
                tone="secondary"
                onClick={() =>
                  setPromos((items) => [
                    ...items,
                    {
                      code: "",
                      percent: "0",
                      enabled: false,
                      reward_type: "percent",
                      total_limit: null,
                      per_account_limit: 1,
                    },
                  ])
                }
              >
                <Plus size={16} />
                {messages.m1767eef279}
              </Button>
            </>
          ))}
        {section === "broadcasts" && (
          <>
            <BroadcastAudience />
            <BroadcastAttachment
              value={media}
              onChange={setMedia}
              onBlockedChange={setMediaBlocked}
            />
            <label>
              {messages.meb8051efbc}
              <textarea
                required
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
            </label>
            <div className="notice warning">{broadcastCopy.local}</div>
            <div className="content-preview">
              <p>{text}</p>
            </div>
          </>
        )}
        {section === "subscriptions" && (
          <>
            <div className="notice">
              {messages.m5cad82dd6d}
              {String(row.id)}
              {messages.mc6f6f42165}
            </div>
            <label>
              {messages.mcf2944b94f}
              <input
                type="number"
                min="1"
                max="365"
                required
                value={compensation}
                onChange={(e) => setCompensation(e.target.value)}
              />
            </label>
          </>
        )}
        {section === "users" && row.edit_kind !== "credit" && (
          <>
            <div className="notice">
              {messages.md05b6f3e91}
              {String(row.id)}
              {messages.m158d354de9}
              {String(row.bonus_balance)}.
            </div>
            <label>
              {messages.mb50a595796}
              <input
                required
                inputMode="decimal"
                pattern="-?\d+(\.\d{1,2})?"
                value={String(values.amount || "")}
                onChange={(e) =>
                  set("amount", e.target.value.replace(",", "."))
                }
              />
            </label>
            <p className="fine-print">{messages.mb830481ff8}</p>
          </>
        )}
        {section === "users" && row.edit_kind === "credit" && (
          <>
            <div className="notice">
              Аккаунт № {String(row.id)}. Текущий денежный остаток:{" "}
              {String(row.credit_balance ?? "Неизвестно")}{" "}
              {String(row.credit_currency || "Валюта не подтверждена")}.
            </div>
            <label>
              Изменение денежных средств
              <input
                required
                inputMode="decimal"
                pattern="-?\d+(\.\d{1,2})?"
                value={String(values.amount || "")}
                onChange={(event) =>
                  set("amount", event.target.value.replace(",", "."))
                }
              />
            </label>
            <label>
              Валюта денежных средств
              <input
                required
                pattern="[A-Z]{3}"
                maxLength={3}
                value={String(values.currency || row.credit_currency || "")}
                onChange={(event) =>
                  set("currency", event.target.value.toUpperCase())
                }
              />
            </label>
            <label>
              Источник денежных средств
              <select
                value={String(values.provenance || "owner_adjustment")}
                onChange={(event) => set("provenance", event.target.value)}
              >
                <option value="owner_adjustment">Изменение владельцем</option>
                <option value="refund_compensation">
                  Компенсация возврата
                </option>
                <option value="verified_legacy">
                  Проверенный исторический остаток
                </option>
              </select>
            </label>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={Boolean(values.confirm_existing_currency)}
                onChange={(event) =>
                  set("confirm_existing_currency", event.target.checked)
                }
              />
              Подтверждаю валюту существующего исторического остатка по
              проверенному источнику.
            </label>
            <p className="fine-print">
              Для подтверждения ранее неизвестной валюты: изменение 0.00,
              источник «Проверенный исторический остаток» и отдельное
              подтверждение. Конвертации валют и банковского возврата эта форма
              не выполняет.
            </p>
          </>
        )}
        {error && (
          <p className="error-text" role="alert">
            {error}
          </p>
        )}
        <div className="dialog-actions">
          <Button type="button" tone="secondary" onClick={onClose}>
            {messages.m8fbe9b75cb}
          </Button>
          <Button type="submit" busy={busy} disabled={mediaBlocked}>
            {section === "broadcasts"
              ? adminSaveCopy.createBroadcast
              : adminSaveCopy.changes}
            <ArrowRight size={17} />
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
