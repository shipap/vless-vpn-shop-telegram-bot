import { useState, type FormEvent, type ReactNode } from "react";
import { useApi } from "@api/client";
import type { components } from "@api/schema";
import { Button, ErrorState, Loading } from "@ui/components";
import { AdminValue } from "@ui/admin-values";
import { date, money } from "@ui/content";
import type { Mutation } from "./admin";
import { adminSaveCopy } from "./admin-save-copy";
import { AdminDateField } from "./admin-date-field";

type Schemas = components["schemas"];
type Row = Record<string, unknown>;
type Props = { mutate: (value: Mutation) => void; refreshVersion: number };
type Items = { items: Row[]; total: number; page_size?: number };
export function ReviewChange({
  title,
  path,
  body,
  before,
  mutate,
  children,
  requireReason = true,
}: {
  title: string;
  path: string;
  body: Row;
  before?: unknown;
  mutate: Props["mutate"];
  children?: ReactNode;
  requireReason?: boolean;
}) {
  function submit(event: FormEvent) {
    event.preventDefault();
    mutate({
      requireReason,
      title,
      confirmLabel: adminSaveCopy.confirmSave,
      path,
      method: "PATCH",
      body,
      description: requireReason
        ? adminSaveCopy.reviewDescription
        : "Проверьте изменения и подтвердите свежим кодом MFA. Состояния до и после изменения будут записаны в аудит автоматически.",
      preview: (
        <>
          {before !== undefined && (
            <>
              <h3>До изменения</h3>
              <AdminValue value={before} />
            </>
          )}
          <h3>После изменения</h3>
          <AdminValue value={body} />
        </>
      ),
    });
  }
  return (
    <form
      className="form card v6-form"
      onSubmit={submit}
      onInvalidCapture={(event) => {
        // Открываем скрытый редактор до стандартного фокуса браузера на ошибке.
        let details = (event.target as HTMLElement).closest("details");
        while (details) {
          details.open = true;
          details = details.parentElement?.closest("details") ?? null;
        }
      }}
    >
      {children}
      <Button type="submit">{title}</Button>
    </form>
  );
}
export function LegalAdmin({ mutate, refreshVersion }: Props) {
  const resource = useApi<Schemas["LegalDto"]>(
    "/admin/legal",
    false,
    refreshVersion,
  );
  const [page, setPage] = useState(1);
  const missing = useApi<Items>(
    `/admin/legal/missing?page=${page}&page_size=25`,
    false,
    refreshVersion,
  );
  if (resource.error)
    return <ErrorState error={resource.error} onRetry={resource.reload} />;
  if (!resource.data) return <Loading />;
  return (
    <div className="stack">
      <LegalForm
        key={JSON.stringify(resource.data)}
        data={resource.data}
        mutate={mutate}
      />
      <section className="card">
        <h2>Без согласия с текущими документами</h2>
        <p>
          Всего: {missing.data?.total ?? "Неизвестно"}. Действующий оплаченный
          доступ сохраняется. Перед новой покупкой потребуется согласие.
        </p>
        {missing.error ? (
          <ErrorState error={missing.error} onRetry={missing.reload} />
        ) : missing.loading ? (
          <Loading />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Аккаунт</th>
                  <th>Email</th>
                  <th>Не приняты</th>
                </tr>
              </thead>
              <tbody>
                {missing.data?.items.map((row, i) => (
                  <tr key={String(row.id ?? i)}>
                    <td>{String(row.id ?? row.account_id ?? "—")}</td>
                    <td>{String(row.email ?? "—")}</td>
                    <td>
                      <ul>
                        {Array.isArray(row.missing) &&
                          row.missing.map((value) => (
                            <li key={String(value)}>
                              {value === "offer"
                                ? "Публичная оферта"
                                : value === "privacy"
                                  ? "Политика конфиденциальности"
                                  : "Текущий документ"}
                            </li>
                          ))}
                      </ul>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!missing.data?.items.length && (
              <p>Все найденные пользователи приняли текущие документы.</p>
            )}
          </div>
        )}
        <div className="dialog-actions">
          <Button
            tone="secondary"
            disabled={page === 1}
            onClick={() => setPage(page - 1)}
          >
            Назад
          </Button>
          <span>Страница {page}</span>
          <Button
            tone="secondary"
            disabled={!missing.data || page * 25 >= missing.data.total}
            onClick={() => setPage(page + 1)}
          >
            Далее
          </Button>
        </div>
      </section>
    </div>
  );
}
function LegalForm({
  data,
  mutate,
}: {
  data: Schemas["LegalDto"];
  mutate: Props["mutate"];
}) {
  const [documents, setDocuments] = useState<Schemas["LegalDocument"][]>(
    ["offer", "privacy"].map(
      (type) =>
        data.documents.find((d) => d.document_type === type) ?? {
          document_type: type as "offer" | "privacy",
          version: "",
          url: "",
        },
    ),
  );
  const set = (index: number, field: "url" | "version", value: string) =>
    setDocuments((current) =>
      current.map((d, i) => (i === index ? { ...d, [field]: value } : d)),
    );
  const body: Omit<Schemas["LegalUpdate"], "reason" | "confirmed"> = {
    documents,
    partial: false,
    expected_revision: data.revision,
  };
  return (
    <ReviewChange
      title={adminSaveCopy.documents}
      path="/admin/legal"
      body={body}
      before={data}
      mutate={mutate}
    >
      <h2>Юридические документы</h2>
      <p>
        Новая версия или URL потребуют повторного согласия перед новой покупкой.
        История согласий сохраняется.
      </p>
      {documents.map((d, i) => (
        <fieldset key={d.document_type}>
          <legend>
            {d.document_type === "offer"
              ? "Публичная оферта"
              : "Политика конфиденциальности"}
          </legend>
          <label>
            {d.document_type === "offer" ? "URL оферты" : "URL политики"}
            <input
              type="url"
              required
              pattern="https://.*"
              value={d.url}
              onChange={(e) => set(i, "url", e.target.value)}
            />
          </label>
          <label>
            {d.document_type === "offer" ? "Версия оферты" : "Версия политики"}
            <input
              required
              maxLength={64}
              value={d.version}
              onChange={(e) => set(i, "version", e.target.value)}
            />
          </label>
        </fieldset>
      ))}
    </ReviewChange>
  );
}
export function RefundAdmin({ mutate, refreshVersion }: Props) {
  const policy = useApi<{ max_age_hours: number; max_traffic_bytes: number }>(
    "/admin/refund-policy",
    false,
    refreshVersion,
  );
  const [page, setPage] = useState(1);
  const reviews = useApi<Items>(
    `/admin/refund-reviews?page=${page}&page_size=25`,
    false,
    refreshVersion,
  );
  return (
    <div className="stack">
      {policy.error ? (
        <ErrorState error={policy.error} onRetry={policy.reload} />
      ) : policy.data ? (
        <RefundPolicy
          key={JSON.stringify(policy.data)}
          data={policy.data}
          mutate={mutate}
        />
      ) : (
        <Loading />
      )}
      <section className="stack">
        <h2>Возвраты требуют решения</h2>
        <p>
          Решение меняет связанный оплаченный период. Банковский возврат эта
          форма не выполняет.
        </p>
        {reviews.error ? (
          <ErrorState error={reviews.error} onRetry={reviews.reload} />
        ) : !reviews.data ? (
          <Loading />
        ) : reviews.data.items.length ? (
          reviews.data.items.map((row) => (
            <RefundCase key={String(row.id)} row={row} mutate={mutate} />
          ))
        ) : (
          <div className="card">Очередь пуста.</div>
        )}
        <div className="dialog-actions">
          <Button
            tone="secondary"
            disabled={page === 1}
            onClick={() => setPage(page - 1)}
          >
            Назад
          </Button>
          <span>
            Страница {page} · всего {reviews.data?.total ?? "—"}
          </span>
          <Button
            tone="secondary"
            disabled={!reviews.data || page * 25 >= reviews.data.total}
            onClick={() => setPage(page + 1)}
          >
            Далее
          </Button>
        </div>
      </section>
    </div>
  );
}
function RefundPolicy({
  data,
  mutate,
}: {
  data: { max_age_hours: number; max_traffic_bytes: number };
  mutate: Props["mutate"];
}) {
  const [hours, setHours] = useState(data.max_age_hours),
    [traffic, setTraffic] = useState(data.max_traffic_bytes);
  const body: Omit<Schemas["RefundPolicyUpdate"], "reason" | "confirmed"> = {
    max_age_hours: hours,
    max_traffic_bytes: traffic,
  };
  return (
    <ReviewChange
      title={adminSaveCopy.refundPolicy}
      path="/admin/refund-policy"
      body={body}
      before={data}
      mutate={mutate}
    >
      <h2>Автоматический отзыв прав после полного возврата</h2>
      <p>
        Только подтверждённый полный возврат, возраст ≤ порога{" "}
        <strong>И</strong> трафик ≤ порога. Частичный возврат, неизвестный
        расход или превышение любого порога требуют ручного решения.
      </p>
      <div className="field-grid">
        <label>
          Максимальный возраст, часы
          <input
            type="number"
            required
            min={0}
            max={24}
            step={1}
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
          />
        </label>
        <label>
          Максимальный трафик, байты
          <input
            type="number"
            required
            min={0}
            max={100000000}
            step={1}
            value={traffic}
            onChange={(e) => setTraffic(Number(e.target.value))}
          />
        </label>
      </div>
      <p className="fine-print">
        Не более 24 часов и 100 000 000 байт (100 МБ).
      </p>
    </ReviewChange>
  );
}
function RefundCase({ row, mutate }: { row: Row; mutate: Props["mutate"] }) {
  const [action, setAction] =
      useState<Schemas["RefundResolve"]["action"]>("keep"),
    [ends, setEnds] = useState("");
  const changesRights = ["revoke", "shorten", "recalculate"].includes(action);
  const needsDate = ["shorten", "recalculate"].includes(action);
  const version =
    typeof row.subscription_version === "number"
      ? row.subscription_version
      : null;
  function submit(e: FormEvent) {
    e.preventDefault();
    if (changesRights && version === null) return;
    const body: Omit<Schemas["RefundResolve"], "reason" | "confirmed"> = {
      action,
      expected_subscription_version: version,
      ...(needsDate
        ? { ends_at: new Date(ends + ":00+03:00").toISOString() }
        : {}),
    };
    mutate({
      title: "Подтвердить решение по возврату",
      description:
        "Будет изменён только связанный период. При изменении версии подписки сервер отклонит решение: обновите очередь и проверьте его снова. Причина и состояния до/после сохраняются в аудите.",
      path: `/admin/refund-reviews/${encodeURIComponent(String(row.id))}/resolve`,
      method: "POST",
      body,
      preview: (
        <>
          <AdminValue value={row} />
          <AdminValue value={body} />
        </>
      ),
    });
  }
  return (
    <form className="card form v6-form" onSubmit={submit}>
      <div className="row">
        <h3>Возврат № {String(row.id)}</h3>
        <span>{money(String(row.amount), String(row.currency))}</span>
      </div>
      <p>
        Аккаунт {String(row.account_id)} · {date(String(row.created_at), true)}{" "}
        · версия подписки {version ?? "неизвестна"}
      </p>
      <details>
        <summary>Доказательства и состояние</summary>
        <AdminValue value={row} />
      </details>
      {row.state === "resolved" ? (
        <p>Решение уже сохранено.</p>
      ) : (
        <>
          <label>
            Решение по возврату
            <select
              value={action}
              onChange={(e) => setAction(e.target.value as typeof action)}
            >
              <option value="keep">Сохранить права</option>
              <option value="revoke">Отозвать связанный период</option>
              <option value="shorten">Сократить связанный период</option>
              <option value="recalculate">Пересчитать связанный период</option>
              <option value="manual">
                Записать ручное решение без изменения прав
              </option>
            </select>
          </label>
          {needsDate && (
            <div className="admin-date-label">
              Новая дата окончания (МСК)
              <AdminDateField
                mode="datetime-local"
                required
                value={ends}
                onChange={setEnds}
                label="Новая дата окончания (МСК)"
              />
            </div>
          )}
          {changesRights && version === null && (
            <p role="alert">
              Версия подписки неизвестна. Обновите очередь перед изменением
              прав.
            </p>
          )}
          <Button type="submit" disabled={changesRights && version === null}>
            {adminSaveCopy.refundDecision}
          </Button>
        </>
      )}
    </form>
  );
}
type Icon = { fallback: string; custom_emoji_id?: string | null };
type Theme = { icons: Record<string, Icon>; styles: Record<string, string> };
const styles = {
  primary: "Основное действие",
  success: "Подтверждение",
  danger: "Опасное действие",
  neutral: "Второстепенное",
  admin: "Админ-панель",
};
export function TelegramAdmin({ mutate, refreshVersion }: Props) {
  const resource = useApi<Theme>(
    "/admin/telegram-theme",
    false,
    refreshVersion,
  );
  if (resource.error)
    return <ErrorState error={resource.error} onRetry={resource.reload} />;
  return resource.data ? (
    <ThemeForm
      key={JSON.stringify(resource.data)}
      data={resource.data}
      mutate={mutate}
    />
  ) : (
    <Loading />
  );
}
function ThemeForm({ data, mutate }: { data: Theme; mutate: Props["mutate"] }) {
  const terms = instanceTerminology(useInstance());
  const [theme, setTheme] = useState(data),
    [key, setKey] = useState("");
  const iconChange = (name: string, field: keyof Icon, value: string) =>
    setTheme((t) => ({
      ...t,
      icons: {
        ...t.icons,
        [name]: {
          ...t.icons[name],
          [field]: value || (field === "custom_emoji_id" ? null : ""),
        },
      },
    }));
  return (
    <ReviewChange
      title={adminSaveCopy.theme}
      path="/admin/telegram-theme"
      body={
        { theme } satisfies Omit<Schemas["ThemeUpdate"], "reason" | "confirmed">
      }
      before={data}
      mutate={mutate}
    >
      <h2>Тема Telegram</h2>
      <p>
        Предпросмотр показывает текстовый запасной вариант. Отображение custom
        emoji и цветов зависит от возможностей тестового бота и клиента;
        сохранение не подтверждает проверку Telegram API.
      </p>
      <details className="admin-disclosure">
        <summary>Оформление кнопок</summary>
        <div className="field-grid admin-disclosure-body">
          {Object.entries(styles).map(([name, label]) => (
            <label key={name}>
              {label}
              <select
                value={
                  theme.styles[name] || (name === "admin" ? "primary" : name)
                }
                onChange={(e) =>
                  setTheme((t) => ({
                    ...t,
                    styles: { ...t.styles, [name]: e.target.value },
                  }))
                }
              >
                {Object.entries(styles)
                  .filter(([s]) => s !== "admin")
                  .map(([s, l]) => (
                    <option key={s} value={s}>
                      {l}
                    </option>
                  ))}
              </select>
            </label>
          ))}
        </div>
      </details>
      <details className="admin-disclosure">
        <summary>Значки меню · {Object.keys(theme.icons).length}</summary>
        <div className="admin-disclosure-body stack">
          {Object.entries(theme.icons).map(([name, icon]) => (
            <fieldset key={name}>
              <legend>Значок: {name}</legend>
              <div className="field-grid">
                <label>
                  Запасной символ: {name}
                  <input
                    required
                    maxLength={16}
                    value={icon.fallback}
                    onChange={(e) =>
                      iconChange(name, "fallback", e.target.value)
                    }
                  />
                </label>
                <label>
                  ID эмодзи: {name}
                  <input
                    inputMode="numeric"
                    pattern="[0-9]{1,32}"
                    value={icon.custom_emoji_id || ""}
                    onChange={(e) =>
                      iconChange(name, "custom_emoji_id", e.target.value)
                    }
                  />
                </label>
              </div>
              <Button
                type="button"
                tone="ghost"
                onClick={() =>
                  setTheme((t) => ({
                    ...t,
                    icons: Object.fromEntries(
                      Object.entries(t.icons).filter(([k]) => k !== name),
                    ),
                  }))
                }
              >
                Удалить значок {name}
              </Button>
            </fieldset>
          ))}
          <label>
            Ключ нового значка
            <input
              value={key}
              maxLength={64}
              onChange={(e) => setKey(e.target.value)}
            />
          </label>
          <Button
            type="button"
            tone="secondary"
            disabled={
              !/^[a-z][a-z0-9_]*$/.test(key) ||
              key in theme.icons ||
              Object.keys(theme.icons).length >= 50
            }
            onClick={() => {
              setTheme((t) => ({
                ...t,
                icons: {
                  ...t.icons,
                  [key]: { fallback: "☀", custom_emoji_id: null },
                },
              }));
              setKey("");
            }}
          >
            Добавить значок
          </Button>
        </div>
      </details>
      <div
        className="telegram-preview content-preview v6-bot-preview"
        aria-label="Предпросмотр меню Telegram"
      >
        {[
          ["connect", terms.connect_label, "primary"],
          ["subscriptions", "Подписки", "neutral"],
          ["profile", "Профиль", "neutral"],
          ["bonuses", "Бонусы", "neutral"],
          ["history", "Платежи", "neutral"],
          ["promo", "Промокод", "neutral"],
          ["help", "Помощь", "neutral"],
          ["admin", "Админ-панель", "admin"],
        ].map(([name, label, semantic]) => (
          <span
            className={`v6-bot-button ${theme.styles[semantic] || semantic}`}
            key={name}
          >
            {theme.icons[name]?.fallback || ""} {label}
          </span>
        ))}
      </div>
    </ReviewChange>
  );
}
import { useInstance } from "@ui/instance";
import { instanceTerminology } from "@ui/terminology";
