import { useState, type FormEvent } from "react";
import { api, useApi } from "@api/client";
import type { Catalog } from "@api/types";
import type { components } from "@api/schema";
import { Button, Dialog, ErrorState, Loading, Empty } from "@ui/components";
import { AdminValue } from "@ui/admin-values";
import { fieldLabel } from "@ui/ru";
import type { Mutation } from "./admin";
import { AdminUserSummary } from "./admin-user-summary";
import { BroadcastAudience, BroadcastCounts } from "./broadcast-audience";
import { useCapability } from "@ui/staff-permissions";
import { OperationalIssues, type IssueQueue } from "./operational-issues";
import { BroadcastReport } from "./broadcast-report";
import { BroadcastAttachment, type BroadcastMediaRef } from "./admin-editor";

type Row = Record<string, unknown>;
const copy = {
  paymentLookup: "Найти платёж",
  paymentId: "ID платежа или номер у платёжного сервиса",
  lookup: "Открыть платёж",
  operations: "Состояние сервисов",
  maintenance: "Режим обслуживания",
  maintenanceOn: "Включить обслуживание",
  maintenanceOff: "Завершить обслуживание",
  maintenanceNote:
    "Режим обслуживания блокирует новые покупки и изменения подписок. Вход, просмотр данных, обработка уже принятых платежей, обслуживание и отмена существующего автопродления остаются доступны. Изменение записывается в аудит.",
  user: "Карточка пользователя",
  content: "История материала",
  broadcast: "Результаты рассылки",
  details: "Подробности",
  versions: "Сохранённые версии",
  noVersions: "Версий пока нет",
  broadcastPreview: "Предпросмотр получателей",
  estimate: "Рассчитать получателей",
  estimateTitle: "Предпросмотр получателей",
  self: "Отправить тест себе",
  text: "Текст сообщения для предпросмотра",
  selfNote:
    "Сообщение получит только подтверждённый email текущего администратора из разрешённого списка.",
};

export function ResourceDetail({
  section,
  row,
  onClose,
  mutate,
}: {
  section: string;
  row: Row;
  onClose: () => void;
  mutate: (value: Mutation) => void;
}) {
  const id = String(row.key || row.id || "");
  const [page, setPage] = useState(1);
  const path =
    section === "users"
      ? `/admin/users/${encodeURIComponent(id)}?page=${page}&page_size=25`
      : section === "content"
        ? `/admin/content/${encodeURIComponent(id.replace(/^content:/, ""))}/versions`
        : `/admin/broadcasts/${encodeURIComponent(id)}`;
  const resource = useApi<Row>(path);
  const pagination = resource.data?.pagination as
    { totals?: Record<string, number> } | undefined;
  const total = Math.max(0, ...Object.values(pagination?.totals || {}));
  const labels =
    section === "users"
      ? [
          "account",
          "identities",
          "subscriptions",
          "orders",
          "payments",
          "agreements",
          "ledger",
          "devices",
          "sessions",
          "blocks",
          "audit",
        ]
      : [];
  return (
    <Dialog
      title={
        section === "users"
          ? copy.user
          : section === "content"
            ? copy.content
            : copy.broadcast
      }
      onClose={onClose}
    >
      {resource.loading ? (
        <Loading />
      ) : resource.error ? (
        <ErrorState error={resource.error} onRetry={resource.reload} />
      ) : section === "users" ? (
        <div className="stack admin-user-detail">
          {resource.data && (
            <AdminUserSummary
              data={resource.data}
              id={id}
              mutate={mutate}
              onClose={onClose}
            />
          )}
          {labels.map((key) => (
            <details
              key={key}
              className="admin-disclosure"
              open={key === "account"}
            >
              <summary>
                {key === "devices"
                  ? "Устройства"
                  : key === "agreements"
                    ? "Автооплата"
                    : fieldLabel(key)}
              </summary>
              <div className="admin-disclosure-body">
                <AdminValue value={resource.data?.[key]} name={key} />
              </div>
            </details>
          ))}
          <div
            className="button-row admin-detail-pagination"
            aria-label="Страницы истории пользователя"
          >
            <Button
              tone="secondary"
              disabled={page === 1}
              onClick={() => setPage(page - 1)}
            >
              Назад
            </Button>
            <span>История · страница {page}</span>
            <Button
              tone="secondary"
              disabled={page * 25 >= total}
              onClick={() => setPage(page + 1)}
            >
              Далее
            </Button>
          </div>
        </div>
      ) : section === "broadcasts" && resource.data ? (
        <BroadcastReport value={resource.data} />
      ) : (
        <AdminValue value={resource.data} />
      )}
    </Dialog>
  );
}

export function OperationalControls({
  section,
  mutate,
  onView,
  refreshVersion,
  isOwner = false,
}: {
  section: string;
  mutate: (value: Mutation) => void;
  onView: (value: Row) => void;
  refreshVersion: number;
  isOwner?: boolean;
}) {
  if (section === "operations")
    return (
      <Operations
        mutate={mutate}
        refreshVersion={refreshVersion}
        isOwner={isOwner}
      />
    );
  if (section === "orders") return <PaymentLookup onView={onView} />;
  if (section === "discounts")
    return <PriceCalculator refreshVersion={refreshVersion} />;
  if (section === "broadcasts") return <BroadcastTools mutate={mutate} />;
  if (section === "security")
    return <LoginHistory refreshVersion={refreshVersion} />;
  return null;
}

function LoginHistory({ refreshVersion }: { refreshVersion: number }) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(1);
  const pageSize = 10;
  const resource = useApi<{ items: Row[]; total: number }>(
    `/admin/security/logins?search=${encodeURIComponent(filter)}&page=${page}&page_size=${pageSize}`,
    false,
    refreshVersion,
  );
  const keys = [
    "created_at",
    "email",
    "account_id",
    "result",
    "reason",
    "ip",
    "ip_provenance",
  ];
  return (
    <section className="card login-history">
      <div className="card-head">
        <h2>История входов</h2>
        <Button tone="secondary" onClick={resource.reload}>
          Обновить историю
        </Button>
      </div>
      <p className="small-copy">
        Проверенные входы и неудачные попытки. IP показан с фактическим
        источником; коды и секреты не отображаются.
      </p>
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          setFilter(search.trim());
          setPage(1);
        }}
      >
        <label>
          Поиск в истории входов
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <Button tone="secondary" type="submit">
          Найти входы
        </Button>
      </form>
      {resource.loading ? (
        <Loading />
      ) : resource.error ? (
        <ErrorState error={resource.error} onRetry={resource.reload} />
      ) : !resource.data?.items.length ? (
        <Empty title="Входов по этому запросу нет" />
      ) : (
        <>
          <div
            className="table-scroll"
            tabIndex={0}
            role="region"
            aria-label="Таблица истории входов"
          >
            <table>
              <thead>
                <tr>
                  {keys.map((key) => (
                    <th key={key}>{fieldLabel(key)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {resource.data.items.map((row, index) => (
                  <tr key={String(row.id || index)}>
                    {keys.map((key) => (
                      <td
                        key={key}
                        className={
                          ["email", "account_id", "ip", "result"].includes(key)
                            ? "identifier-cell"
                            : undefined
                        }
                      >
                        <AdminValue value={row[key]} name={key} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="admin-pagination">
            <span>
              Всего входов: {resource.data.total} · страница {page}
            </span>
            <Button
              tone="secondary"
              disabled={page === 1}
              onClick={() => setPage((value) => value - 1)}
            >
              Ранее
            </Button>
            <Button
              tone="secondary"
              disabled={page * pageSize >= resource.data.total}
              onClick={() => setPage((value) => value + 1)}
            >
              Далее по истории
            </Button>
          </div>
        </>
      )}
    </section>
  );
}

function PriceCalculator({ refreshVersion }: { refreshVersion: number }) {
  const catalog = useApi<Catalog>("/catalog", false, refreshVersion);
  const discounts = useApi<{ items: Row[] }>(
    "/admin/discounts",
    false,
    refreshVersion,
  );
  const policy = discounts.data?.items.find((item) => item.id === "discounts")
    ?.value as Row | undefined;
  const [productOverride, setProduct] = useState<string | null>(null);
  const [periodOverride, setPeriod] = useState<string | null>(null);
  const product = productOverride ?? catalog.data?.products[0]?.id ?? "";
  const period = periodOverride ?? String(catalog.data?.periods[0]?.days ?? "");
  const [secondary, setSecondary] = useState("0");
  const [bonus, setBonus] = useState("0.00");
  const [capOverride, setCap] = useState<string | null>(null);
  const [bonusCapOverride, setBonusCap] = useState<string | null>(null);
  const cap = capOverride ?? String(policy?.cap ?? "");
  const bonusCap = bonusCapOverride ?? String(policy?.bonus_cap ?? "");
  const [result, setResult] = useState<Row | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (
      !catalog.data?.products.some((item) => item.id === product) ||
      !catalog.data.periods.some((item) => String(item.days) === period) ||
      !cap ||
      !bonusCap
    )
      return;
    setBusy(true);
    setError(null);
    try {
      setResult(
        await api<Row>("/admin/discounts/preview", {
          method: "POST",
          body: {
            product_id: product,
            period_days: Number(period),
            secondary_percent: secondary,
            bonus_amount: bonus,
            cap,
            bonus_cap: bonusCap,
          },
        }),
      );
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  if (catalog.loading || discounts.loading) return <Loading />;
  if (catalog.error)
    return <ErrorState error={catalog.error} onRetry={catalog.reload} />;
  if (discounts.error)
    return <ErrorState error={discounts.error} onRetry={discounts.reload} />;
  return (
    <section className="card">
      <h2>Калькулятор стоимости</h2>
      <p className="small-copy">
        Проверьте расчёт перед изменением политики. Комиссия без подтверждённого
        источника остаётся неизвестной.
      </p>
      <form className="form" onSubmit={submit}>
        <div className="field-grid">
          <label>
            Тариф для расчёта
            <select
              required
              value={product}
              onChange={(event) => setProduct(event.target.value)}
            >
              {!catalog.data?.products.length && (
                <option value="">Тарифы не настроены</option>
              )}
              {catalog.data?.products.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Период для расчёта
            <select
              required
              value={period}
              onChange={(event) => setPeriod(event.target.value)}
            >
              {!catalog.data?.periods.length && (
                <option value="">Периоды не настроены</option>
              )}
              {catalog.data?.periods.map((item) => (
                <option key={item.days} value={item.days}>
                  {item.days} дней
                </option>
              ))}
            </select>
          </label>
          {[
            ["Дополнительная скидка, %", secondary, setSecondary],
            ["Бонусы для расчёта", bonus, setBonus],
            ["Лимит скидки, %", cap, setCap],
            ["Лимит бонусной оплаты, %", bonusCap, setBonusCap],
          ].map(([label, value, setValue]) => (
            <label key={String(label)}>
              {String(label)}
              <input
                required
                inputMode="decimal"
                pattern="\d+(\.\d{1,2})?"
                value={String(value)}
                onChange={(event) =>
                  (setValue as (value: string) => void)(
                    event.target.value.replace(",", "."),
                  )
                }
              />
            </label>
          ))}
        </div>
        <Button type="submit" tone="secondary" busy={busy}>
          Рассчитать без сохранения
        </Button>
      </form>
      {Boolean(error) && <ErrorState error={error} />}
      {result && (
        <div aria-live="polite">
          <AdminValue value={result} />
        </div>
      )}
    </section>
  );
}

function PaymentLookup({ onView }: { onView: (value: Row) => void }) {
  const [identifier, setIdentifier] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onView(
        await api<Row>(
          `/admin/payments/${encodeURIComponent(identifier.trim())}`,
        ),
      );
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="card">
      <h2>{copy.paymentLookup}</h2>
      <form className="form" onSubmit={submit}>
        <label>
          {copy.paymentId}
          <input
            required
            value={identifier}
            onChange={(event) => setIdentifier(event.target.value)}
          />
        </label>
        <Button busy={busy} type="submit" tone="secondary">
          {copy.lookup}
        </Button>
        {Boolean(error) && <ErrorState error={error} />}
      </form>
    </section>
  );
}

function Operations({
  mutate,
  refreshVersion,
  isOwner,
}: {
  mutate: (value: Mutation) => void;
  refreshVersion: number;
  isOwner: boolean;
}) {
  const canManage = useCapability("operations_manage");
  const resource = useApi<Row>(
    "/admin/operations/health",
    false,
    refreshVersion,
  );
  if (resource.loading) return <Loading />;
  if (resource.error)
    return <ErrorState error={resource.error} onRetry={resource.reload} />;
  const maintenance = resource.data?.maintenance;
  const enabled =
    typeof maintenance === "object" && maintenance !== null
      ? Boolean((maintenance as Row).enabled)
      : Boolean(maintenance);
  return (
    <details className="card admin-disclosure">
      <summary>Состояние операций</summary>
      <div className="admin-disclosure-body stack">
        <div className="card-head">
          <h2>{copy.operations}</h2>
          <Button
            tone={enabled ? "secondary" : "danger"}
            onClick={() =>
              mutate({
                title: copy.maintenance,
                description: copy.maintenanceNote,
                path: "/admin/operations/maintenance",
                method: "PATCH",
                body: { enabled: !enabled },
              })
            }
          >
            {enabled ? copy.maintenanceOff : copy.maintenanceOn}
          </Button>
        </div>
        <p className="small-copy">{copy.maintenanceNote}</p>
        <RetentionControls
          enabled={
            (resource.data?.subscription_retention as Row | undefined)
              ?.enabled === true
          }
          isOwner={isOwner}
          mutate={mutate}
        />
        {isOwner && (
          <PanelEventRoutesControls
            mutate={mutate}
            refreshVersion={refreshVersion}
          />
        )}
        <div className="health-grid">
          {["database", "redis", "backup", "maintenance"].map((key) => {
            const value = resource.data?.[key] as Row | undefined;
            return (
              <section className="health-summary" key={key}>
                <h3>{fieldLabel(key)}</h3>
                <AdminValue
                  value={value?.state ?? value?.enabled}
                  name="state"
                />
                {value?.message != null && <p>{String(value.message)}</p>}
                {value?.verified_at != null && (
                  <p>
                    <AdminValue value={value.verified_at} name="verified_at" />
                  </p>
                )}
                {key === "backup" && value?.scope != null && (
                  <p>
                    <AdminValue value={value.scope} name="scope" />
                  </p>
                )}
              </section>
            );
          })}
        </div>
        <QueueHealth value={resource.data?.queue as Row | undefined} />
        <OperationalIssues
          value={resource.data?.issues as IssueQueue | undefined}
          canManage={canManage}
          mutate={mutate}
        />
        <p className="fine-print">
          Проверено:{" "}
          <AdminValue value={resource.data?.checked_at} name="checked_at" />
        </p>
      </div>
    </details>
  );
}

type EventRoutesResponse = components["schemas"]["PanelEventRoutesResponse"];
type NodeRoute = components["schemas"]["NodeRoute"];
type InfraRoute = components["schemas"]["InfraRoute"];
function PanelEventRoutesControls({
  mutate,
  refreshVersion,
}: {
  mutate: (value: Mutation) => void;
  refreshVersion: number;
}) {
  const canWrite = useCapability("settings_write");
  const resource = useApi<EventRoutesResponse>(
    "/admin/panel-event-routes",
    false,
    refreshVersion,
  );
  return (
    <details className="card admin-disclosure">
      <summary>Маршруты событий панели</summary>
      <div className="admin-disclosure-body">
        {resource.loading ? (
          <Loading />
        ) : resource.error ? (
          <ErrorState error={resource.error} onRetry={resource.reload} />
        ) : (
          resource.data && (
            <PanelEventRouteForm
              key={JSON.stringify(resource.data)}
              value={resource.data}
              canWrite={canWrite}
              mutate={mutate}
            />
          )
        )}
      </div>
    </details>
  );
}
export function PanelEventRouteForm({
  value,
  canWrite,
  mutate,
}: {
  value: EventRoutesResponse;
  canWrite: boolean;
  mutate: (value: Mutation) => void;
}) {
  const [nodes, setNodes] = useState<NodeRoute[]>(value.policy?.nodes ?? []);
  const [infra, setInfra] = useState<InfraRoute[]>(value.policy?.infra ?? []);
  const [error, setError] = useState<unknown>(null);
  const context = value.registration_context;
  if (!context)
    return (
      <p>
        В текущем окружении регистрация недоступна. Нужен разрешённый контракт
        панели 3.4 с числовой идентичностью. Связи не подставляются
        автоматически.
      </p>
    );
  const canAddNode =
    value.approved_node_uuids.length > 0 &&
    value.approved_product_ids.length > 0;
  function node(index: number, patch: Partial<NodeRoute>) {
    setNodes((rows) =>
      rows.map((item, i) => (i === index ? { ...item, ...patch } : item)),
    );
  }
  function infrastructure(index: number, patch: Partial<InfraRoute>) {
    setInfra((rows) =>
      rows.map((item, i) => (i === index ? { ...item, ...patch } : item)),
    );
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!canWrite) return;
    if (
      nodes.some(
        (item) =>
          !value.approved_node_uuids.includes(item.node_uuid) ||
          !item.product_ids.length ||
          item.product_ids.some(
            (id) => !value.approved_product_ids.includes(id),
          ),
      )
    ) {
      setError(
        new Error(
          "Выберите разрешённый сервер и хотя бы один доступный тариф.",
        ),
      );
      return;
    }
    setError(null);
    mutate({
      title: "Маршруты событий панели",
      description:
        "Сохраните только явно проверенные связи проекта. Новостная лента не отправляет уведомлений. Напоминания CRM предназначены только владельцу. Изменение записывается в аудит.",
      path: "/admin/panel-event-routes",
      method: "PATCH",
      freshMfa: true,
      body: { policy: { ...context, nodes, infra } },
    });
  }
  return (
    <form className="form" onSubmit={submit}>
      <p>{value.configured ? "Связи настроены." : "Связи ещё не настроены."}</p>
      <p className="small-copy">
        Окружение: {context.environment} · Панель: {context.panel_origin} ·
        Контракт: {context.contract}
      </p>
      <p className="fine-print">
        Сохранение связей не включает вебхуки и не подтверждает доставку
        событий. Без явной связи новости сервера и напоминания CRM не
        публикуются.
      </p>
      <fieldset disabled={!canWrite}>
        <legend>Серверы и тарифы для новостей</legend>
        {!canAddNode && (
          <p>
            Нет разрешённых серверов или доступных тарифов. Связи для новостей
            недоступны.
          </p>
        )}
        {nodes.map((item, index) => (
          <fieldset key={index}>
            <legend>Связь сервера {index + 1}</legend>
            <label>
              UUID разрешённого сервера
              <select
                required
                value={item.node_uuid}
                onChange={(event) =>
                  node(index, { node_uuid: event.target.value })
                }
              >
                <option value="">Выберите разрешённый UUID</option>
                {value.approved_node_uuids.map((id) => (
                  <option key={id} value={id}>
                    {id}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Публичное название сервера
              <input
                required
                maxLength={80}
                value={item.safe_label}
                onChange={(event) =>
                  node(index, { safe_label: event.target.value })
                }
              />
            </label>
            <p className="fine-print">
              Название для клиента: без IP, UUID и внутренних адресов.
            </p>
            <fieldset>
              <legend>Доступные тарифы</legend>
              {value.approved_product_ids.map((id) => (
                <label className="checkbox-label" key={id}>
                  <input
                    type="checkbox"
                    checked={item.product_ids.includes(id)}
                    onChange={(event) =>
                      node(index, {
                        product_ids: event.target.checked
                          ? [...item.product_ids, id]
                          : item.product_ids.filter(
                              (product) => product !== id,
                            ),
                      })
                    }
                  />
                  Тариф {id}
                </label>
              ))}
            </fieldset>
            <Button
              type="button"
              tone="secondary"
              onClick={() =>
                setNodes((rows) => rows.filter((_, i) => i !== index))
              }
            >
              Убрать связь сервера {index + 1}
            </Button>
          </fieldset>
        ))}
        <Button
          type="button"
          tone="secondary"
          disabled={!canAddNode || nodes.length >= 100}
          onClick={() =>
            setNodes((rows) => [
              ...rows,
              { node_uuid: "", product_ids: [], safe_label: "" },
            ])
          }
        >
          Добавить сервер
        </Button>
      </fieldset>
      <fieldset disabled={!canWrite}>
        <legend>Инфраструктура CRM — только владельцу</legend>
        <p className="small-copy">
          Названия провайдера и узла должны точно совпадать с CRM, включая
          регистр. Платёжные ссылки здесь не сохраняются.
        </p>
        {infra.map((item, index) => (
          <fieldset key={index}>
            <legend>Связь CRM {index + 1}</legend>
            <label>
              Название провайдера в CRM
              <input
                required
                maxLength={200}
                value={item.provider_name}
                onChange={(event) =>
                  infrastructure(index, { provider_name: event.target.value })
                }
              />
            </label>
            <label>
              Название узла в CRM
              <input
                required
                maxLength={200}
                value={item.node_name}
                onChange={(event) =>
                  infrastructure(index, { node_name: event.target.value })
                }
              />
            </label>
            <label>
              Безопасное название для владельца
              <input
                required
                maxLength={80}
                value={item.safe_label}
                onChange={(event) =>
                  infrastructure(index, { safe_label: event.target.value })
                }
              />
            </label>
            <Button
              type="button"
              tone="secondary"
              onClick={() =>
                setInfra((rows) => rows.filter((_, i) => i !== index))
              }
            >
              Убрать связь CRM {index + 1}
            </Button>
          </fieldset>
        ))}
        <Button
          type="button"
          tone="secondary"
          disabled={infra.length >= 100}
          onClick={() =>
            setInfra((rows) => [
              ...rows,
              { provider_name: "", node_name: "", safe_label: "" },
            ])
          }
        >
          Добавить связь CRM
        </Button>
      </fieldset>
      {Boolean(error) && <ErrorState error={error} />}
      <Button type="submit" disabled={!canWrite}>
        Сохранить связи событий
      </Button>
    </form>
  );
}

export function RetentionControls({
  enabled,
  isOwner,
  mutate,
}: {
  enabled: boolean;
  isOwner: boolean;
  mutate: (value: Mutation) => void;
}) {
  return (
    <details className="card admin-disclosure">
      <summary>Удаление истёкших VPN-подписок</summary>
      <div className="admin-disclosure-body stack">
        <p>
          Срок после окончания: 7 дней. {enabled ? "Включено." : "Выключено."}
        </p>
        <p className="small-copy">
          Аккаунт и история платежей сохраняются. Перед удалением проверяются
          сроки и оплаченные права. Банковские соглашения не отменяются.
        </p>
        {isOwner && (
          <Button
            tone={enabled ? "secondary" : "danger"}
            onClick={() =>
              mutate({
                freshMfa: true,
                title: "Удаление истёкших VPN-подписок",
                description:
                  "Удаление из панели выполняется через 7 дней после окончания срока при отсутствии препятствий. Отключение остановит новые удаления, а проверка уже начатых операций продолжится. Аккаунты и платежи сохраняются.",
                path: "/admin/subscription-retention",
                method: "PATCH",
                body: { enabled: !enabled, days_after_expiry: 7 },
              })
            }
          >
            {enabled
              ? "Выключить удаление истёкших подписок"
              : "Включить удаление истёкших подписок"}
          </Button>
        )}
      </div>
    </details>
  );
}

function QueueHealth({ value }: { value?: Row }) {
  const items = Array.isArray(value?.items) ? (value.items as Row[]) : [];
  return (
    <details className="queue-health">
      <summary>Очереди · {items.length} групп состояния</summary>
      <p className="small-copy">
        Самая старая ожидающая задача:{" "}
        <AdminValue value={value?.oldest_pending_at} name="oldest_pending_at" />
      </p>
      <div className="table-scroll">
        <table className="queue-health-table">
          <thead>
            <tr>
              {["kind", "state", "count"].map((key) => (
                <th key={key}>{fieldLabel(key)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {items.map((row, index) => (
              <tr key={index}>
                {["kind", "state", "count"].map((key) => (
                  <td key={key}>
                    <AdminValue value={row[key]} name={key} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function BroadcastTools({ mutate }: { mutate: (value: Mutation) => void }) {
  const [text, setText] = useState("");
  const [estimate, setEstimate] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [media, setMedia] = useState<BroadcastMediaRef | null>(null);
  const [mediaBlocked, setMediaBlocked] = useState(false);
  const nodes = () => [
    { type: "paragraph", children: [{ type: "text", text }] },
  ];
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (mediaBlocked) return;
    setBusy(true);
    setError(null);
    try {
      setEstimate(
        await api<Row>("/admin/broadcasts/estimate", {
          method: "POST",
          body: {
            nodes: nodes(),
            media,
          },
        }),
      );
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="card stack">
      <h2>{copy.broadcastPreview}</h2>
      <BroadcastAudience />
      <form className="form" onSubmit={submit}>
        <BroadcastAttachment
          value={media}
          onChange={(value) => {
            setMedia(value);
            setEstimate(null);
          }}
          onBlockedChange={setMediaBlocked}
        />
        <label>
          {copy.text}
          <textarea
            required
            maxLength={10000}
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setEstimate(null);
            }}
          />
        </label>
        <div className="row">
          <Button
            tone="secondary"
            type="submit"
            busy={busy}
            disabled={mediaBlocked}
          >
            {copy.estimate}
          </Button>
          <Button
            tone="secondary"
            type="button"
            disabled={!text.trim() || mediaBlocked}
            onClick={() =>
              mutate({
                title: copy.self,
                description: copy.selfNote,
                path: "/admin/broadcasts/test-self",
                method: "POST",
                body: { nodes: nodes(), media },
              })
            }
          >
            {copy.self}
          </Button>
        </div>
        {estimate && (
          <div aria-live="polite">
            <h3>{copy.estimateTitle}</h3>
            <BroadcastCounts value={estimate} />
          </div>
        )}
        {Boolean(error) && <ErrorState error={error} />}
      </form>
    </section>
  );
}
