import { AdminMfa } from "./admin-mfa";
import { AdminDateField } from "./admin-date-field";
import { NotificationCenter } from "@ui/notification-center";
import {
  StaffPermissions,
  hasCapability,
  sectionCapability,
  staffRole,
  useCapability,
} from "@ui/staff-permissions";
import { AnalyticsPanels } from "./analytics-panels";
import { analyticsCopy as copy } from "@ui/analytics-copy";
import { messages } from "@ui/messages";
import { useInstance } from "@ui/instance";
import { analyticsView, exportRows } from "@ui/analytics";
import { Editor } from "./admin-editor";
import { broadcastCopy } from "./broadcast-audience";
import { AdminV11, UserCommunication, v11Sections } from "./admin-v11";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  BarChart3,
  BookOpen,
  Check,
  CreditCard,
  FileText,
  Gift,
  Menu,
  RefreshCw,
  Search,
  Send,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Users,
  Wifi,
  X,
} from "lucide-react";
import { ApiError, api, useApi } from "@api/client";
import type { Session } from "@api/types";
import {
  Brand,
  Badge,
  Button,
  Dialog,
  Empty,
  ErrorState,
  Loading,
  SectionTitle,
} from "@ui/components";
import { date, money, SemanticContent, type ContentNode } from "@ui/content";
import { ru, fieldLabel, statusLabel } from "@ui/ru";
import { AdminValue } from "@ui/admin-values";
import { OperationalControls, ResourceDetail } from "./admin-operations";
import { LegalAdmin, RefundAdmin, TelegramAdmin } from "./admin-v6";
import { CatalogAdmin } from "./admin-v6-catalog";
import { PublicContentAdmin } from "./admin-v6-content";
import CodeSlots, {
  authCodeSlotsProps,
  type CodeSlotsStatus,
} from "@ui/CodeSlots";

type Row = Record<string, unknown>;
export type Mutation = {
  requireReason?: boolean;
  freshMfa?: boolean;
  title: string;
  confirmLabel?: string;
  description: string;
  path: string;
  method: string;
  body: Row;
  preview?: ReactNode;
};
const icons = {
  overview: BarChart3,
  users: Users,
  subscriptions: Wifi,
  orders: CreditCard,
  recurring: RefreshCw,
  catalog: BookOpen,
  discounts: Gift,
  content: FileText,
  broadcasts: Send,
  security: ShieldCheck,
  operations: Activity,
  legal: FileText,
  refunds: CreditCard,
  telegram: Settings,
  referrals: Gift,
  promos: Gift,
  messages: Send,
  campaigns: BarChart3,
  backups: ArrowDownToLine,
  identities: ShieldCheck,
  nodes: Wifi,
};
const preferredColumns: Record<string, string[]> = {
  "financial-events": [
    "id",
    "date",
    "kind",
    "amount",
    "currency",
    "fee",
    "fee_currency",
    "net_usdt",
    "source",
    "product_id",
    "canonical_account_id",
    "provenance",
  ],
  users: ["id", "email", "telegram_id", "email_verified", "created_at"],
  subscriptions: [
    "id",
    "account_id",
    "name",
    "status",
    "desired_panel_status",
    "provisioning",
    "expires_at",
    "devices_limit",
  ],
  orders: ["id", "account_id", "total", "currency", "status", "created_at"],
  recurring: ["id", "subscription_id", "state", "amount", "next_charge_at"],
  catalog: ["name", "price", "included_devices", "available", "version"],
  discounts: ["id", "value"],
  content: ["id", "version", "updated_at", "content"],
  broadcasts: ["id", "state", "attempts", "recipient_count"],
  security: [
    "id",
    "scope",
    "rule",
    "count",
    "threshold",
    "started_at",
    "ends_at",
  ],
  operations: ["id", "kind", "state", "reason", "attempts", "available_at"],
};
export function Admin({
  session,
  refreshSession,
  webPushPublicKey,
}: {
  session: Session;
  refreshSession: () => void;
  webPushPublicKey?: string | null;
}) {
  const instance = useInstance();
  const [section, setSection] = useState(location.hash.slice(1) || "overview");
  const [mobile, setMobile] = useState(false);
  const [mutation, setMutation] = useState<Mutation | null>(null);
  const [version, setVersion] = useState(0);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  useEffect(() => {
    const handler = () => {
      const next = location.hash.slice(1);
      if (next === "main-content") return;
      setSection(next in ru.admin ? next : "overview");
      setMobile(false);
      requestAnimationFrame(() =>
        document.getElementById("main-content")?.focus(),
      );
    };
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);
  useEffect(() => {
    window.addEventListener("sunrise:mfa-required", refreshSession);
    return () =>
      window.removeEventListener("sunrise:mfa-required", refreshSession);
  }, [refreshSession]);
  if (!session.is_admin || !staffRole(session))
    return (
      <main className="standalone">
        <Empty title="Страница не найдена" />
      </main>
    );
  if (!session.mfa_verified) return <AdminMfa onVerified={refreshSession} />;
  const key = section in ru.admin ? section : "overview";
  const allowed =
    hasCapability(session, sectionCapability[key]) &&
    (key !== "backups" || staffRole(session) === "owner");
  return (
    <StaffPermissions session={session}>
      <div className="dashboard-layout admin-layout">
        <a
          href="#main-content"
          className="skip-link"
          onClick={(event) => {
            event.preventDefault();
            document.getElementById("main-content")?.focus();
          }}
        >
          {messages.m0ebff3642a}
        </a>
        <header className="mobile-header">
          <Brand />
          <button
            id="mobile-menu-toggle"
            className="icon-button"
            onClick={() => setMobile(!mobile)}
            aria-label={messages.m645e1ac30d}
            aria-expanded={mobile}
          >
            {mobile ? <X /> : <Menu />}
          </button>
        </header>
        <aside
          className={`sidebar ${mobile ? "open" : ""}`}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setMobile(false);
              document.getElementById("mobile-menu-toggle")?.focus();
            }
          }}
        >
          <Brand />
          <div className="sidebar-caption">{messages.m412535c211}</div>
          <nav aria-label={messages.mddd9726ff4}>
            {Object.entries(ru.admin)
              .filter(
                ([name]) =>
                  hasCapability(session, sectionCapability[name]) &&
                  (name !== "backups" || staffRole(session) === "owner"),
              )
              .map(([name, label]) => {
                const Icon = icons[name as keyof typeof icons];
                return (
                  <a
                    href={`#${name}`}
                    key={name}
                    className={key === name ? "active" : ""}
                    aria-current={key === name ? "page" : undefined}
                  >
                    <Icon size={17} />
                    {label}
                  </a>
                );
              })}
          </nav>
          <div className="sidebar-bottom">
            <span className="fine-print">
              {staffRole(session) === "owner" ? "Владелец" : "Администратор"}
            </span>
            <a href="/" className="admin-link">
              <ArrowLeft size={17} />
              {messages.m4563694dea}
            </a>
            <div className="admin-identity">
              <ShieldCheck size={17} />
              <span>{session.account.email}</span>
            </div>
          </div>
        </aside>
        <div className="workspace">
          <div
            className="workspace-top"
            role="region"
            aria-label={messages.currentSection}
          >
            <span>
              {messages.mf2174279f2}
              <span className="slash">/</span>{" "}
              {ru.admin[key as keyof typeof ru.admin]}
            </span>
            <div className="workspace-actions">
              <span className="badge good">{messages.mf11bb7fee4}</span>
              <NotificationCenter
                key={session.account.id}
                accountId={session.account.id}
                publicKey={webPushPublicKey}
              />
            </div>
          </div>
          <main className="workspace-main" id="main-content" tabIndex={-1}>
            <SectionTitle
              eyebrow="УПРАВЛЕНИЕ"
              title={ru.admin[key as keyof typeof ru.admin]}
            >
              <Button tone="secondary" onClick={() => setVersion((v) => v + 1)}>
                <RefreshCw size={16} />
                {messages.m603e460bf5}
              </Button>
            </SectionTitle>
            {actionNotice && (
              <div className="notice v11-action-notice" role="status">
                <p>{actionNotice}</p>
                <Button tone="ghost" onClick={() => setActionNotice(null)}>
                  Закрыть сообщение
                </Button>
              </div>
            )}
            {!allowed ? (
              <Empty title="Страница не найдена" />
            ) : v11Sections.has(key) ? (
              <AdminV11
                key={`${key}-${version}`}
                section={key}
                mutate={setMutation}
                refreshVersion={version}
              />
            ) : key === "overview" ? (
              <Overview key={version} />
            ) : key === "legal" ? (
              <LegalAdmin mutate={setMutation} refreshVersion={version} />
            ) : key === "refunds" ? (
              <RefundAdmin mutate={setMutation} refreshVersion={version} />
            ) : key === "telegram" ? (
              <TelegramAdmin mutate={setMutation} refreshVersion={version} />
            ) : key === "catalog" ? (
              <CatalogAdmin mutate={setMutation} refreshVersion={version} />
            ) : (
              <AdminSection
                key={key}
                section={key}
                refreshVersion={version}
                mutate={setMutation}
                isOwner={staffRole(session) === "owner"}
              />
            )}
          </main>
          <footer className="workspace-footer">
            <span>{instance.app_name} · управление</span>
            <span>{messages.m60596948c8}</span>
          </footer>
        </div>
        {mutation && (
          <SensitiveDialog
            mutation={mutation}
            onClose={() => setMutation(null)}
            onComplete={() => {
              setActionNotice(
                `${mutation.title}. Запрос принят. Проверьте результат в соответствующем разделе.`,
              );
              setMutation(null);
              setVersion((v) => v + 1);
            }}
          />
        )}
      </div>
    </StaffPermissions>
  );
}
function SensitiveDialog({
  mutation,
  onClose,
  onComplete,
}: {
  mutation: Mutation;
  onClose: () => void;
  onComplete: () => void;
}) {
  const [code, setCode] = useState("");
  const [codeStatus, setCodeStatus] = useState<CodeSlotsStatus>("idle");
  const errorId = useId();
  const submitting = useRef(false);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const needsFreshMfa = mutation.freshMfa !== false;
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (
      !confirmed ||
      submitting.current ||
      (needsFreshMfa && (code.length !== 6 || codeStatus !== "idle"))
    )
      return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      if (needsFreshMfa)
        await api("/admin/mfa/verify", { method: "POST", body: { code } });
      await api(mutation.path, {
        method: mutation.method,
        body: {
          ...mutation.body,
          confirmed: true,
        },
      });
      setCodeStatus("success");
      onComplete();
    } catch (e) {
      setError(e);
      setCodeStatus("error");
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <Dialog title={mutation.title} onClose={onClose}>
      <form onSubmit={submit} className="form">
        <p>{mutation.description}</p>
        {mutation.preview}
        {needsFreshMfa && (
          <label>
            {messages.me4ce7ee1be}
            <CodeSlots
              {...authCodeSlotsProps}
              value={code}
              status={codeStatus}
              disabled={busy}
              ariaLabel={messages.me4ce7ee1be}
              ariaDescribedBy={error ? errorId : undefined}
              mask
              onChange={(next) => {
                setCode(next);
                if (next) {
                  setCodeStatus("idle");
                  setError(null);
                }
              }}
            />
          </label>
        )}
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
            required
          />
          {messages.m86f9819757}
        </label>
        {Boolean(error) && (
          <div id={errorId}>
            <ErrorState error={error} />
          </div>
        )}
        <div className="dialog-actions">
          <Button tone="secondary" type="button" onClick={onClose}>
            {messages.m8fbe9b75cb}
          </Button>
          <Button type="submit" busy={busy} disabled={!confirmed}>
            {mutation.confirmLabel || messages.md2283e8541}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function scalar(
  value: unknown,
  key: string,
  currency = "RUB",
  timezone = "Europe/Moscow",
): ReactNode {
  if (value == null)
    return <span className="muted">{messages.m6d8e839d9f}</span>;
  if (typeof value === "boolean")
    return value ? messages.m93e6073f90 : messages.m43e5ab70f6;
  if (key === "provisioning" && typeof value === "object") {
    const job = value as Row;
    return (
      <>
        <Badge value={String(job.state || "pending")} />
        {job.last_error != null && (
          <p className="small-copy">
            <AdminValue value={job.last_error} name="last_error" />
          </p>
        )}
      </>
    );
  }
  if (typeof value === "object")
    return (
      <span className="muted">
        {Array.isArray(value)
          ? `${value.length} записей`
          : messages.m0fefbd67d8}
      </span>
    );
  if (key === "last_error" || key === "reason")
    return (
      <span className="table-error-summary" title={String(value)}>
        <AdminValue value={value} name={key} currency={currency} />
      </span>
    );
  if (key === "status" || key === "state" || key === "environment")
    return <Badge value={String(value)} />;
  if (key === "date" || /(_at|_date)$/.test(key))
    return date(String(value), true, timezone);
  if (
    [
      "price",
      "amount",
      "total",
      "fee",
      "bonus_balance",
      "extra_device_price",
    ].includes(key)
  )
    return money(String(value), currency);
  return <AdminValue value={value} name={key} currency={currency} />;
}
function DataTable({
  items,
  section,
  onView,
  actions,
  timezone,
}: {
  items: Row[];
  section: string;
  onView?: (row: Row) => void;
  actions?: (row: Row) => ReactNode;
  timezone?: string;
}) {
  if (!items.length)
    return (
      <Empty title={messages.mef07a4f4c3}>
        <p>{messages.m80ef046cf2}</p>
      </Empty>
    );
  const keys = Array.from(new Set(items.flatMap((item) => Object.keys(item))));
  const columns = (preferredColumns[section] || keys.slice(0, 6)).filter(
    (key) => keys.includes(key),
  );
  return (
    <div
      className="table-scroll"
      tabIndex={0}
      role="region"
      aria-label={`${ru.admin[section as keyof typeof ru.admin] || "Данные"}: таблица`}
    >
      <table>
        <thead>
          <tr>
            {columns.map((key) => (
              <th key={key}>{fieldLabel(key)}</th>
            ))}
            {(onView || actions) && (
              <th>
                <span className="visually-hidden">{messages.mcad123465b}</span>
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {items.map((row, i) => (
            <tr key={String(row.id || row.key || i)}>
              {columns.map((key) => (
                <td
                  key={key}
                  className={
                    [
                      "id",
                      "account_id",
                      "subscription_id",
                      "order_id",
                      "telegram_id",
                      "email",
                      "canonical_account_id",
                      "original_account_id",
                      "kind",
                      "state",
                      "status",
                    ].includes(key)
                      ? "identifier-cell"
                      : key === "date" || /(_at|_date)$/.test(key)
                        ? "date-cell"
                        : undefined
                  }
                >
                  {scalar(
                    row[key],
                    key,
                    key === "fee"
                      ? String(row.fee_currency || row.currency || "RUB")
                      : key === "net_usdt"
                        ? "USDT"
                        : String(row.currency || "RUB"),
                    timezone,
                  )}
                </td>
              ))}
              {(onView || actions) && (
                <td className="table-actions-cell">
                  <div className="row table-actions">
                    {onView && (
                      <Button
                        tone="ghost"
                        className="small"
                        onClick={() => onView(row)}
                      >
                        {messages.mea1f408246}
                        <ArrowUpRight size={14} />
                      </Button>
                    )}
                    {actions?.(row)}
                  </div>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
function Detail({
  row,
  onClose,
  mutate,
}: {
  row: Row;
  onClose: () => void;
  mutate: (value: Mutation) => void;
}) {
  const canResolveDispute = useCapability("refunds_manage");
  const payment = row.payment as Row | undefined;
  const disputed = Boolean((row.dispute as Row | undefined)?.enabled);
  return (
    <Dialog title={messages.md9d1ea50d7} onClose={onClose}>
      {payment?.id != null && disputed && canResolveDispute && (
        <div className="notice">
          <p>
            Спорный платёж ограничивает новые операции этой подписки.
            Возобновление доступа не снимает это финансовое ограничение.
          </p>
          <Button
            tone="secondary"
            onClick={() => {
              onClose();
              mutate({
                title: "Разрешить платёжный спор",
                description:
                  "Снять ограничение новых операций после проверки спора. Оплаченные права и финансовая история сохраняются. Возврат или компенсация денежных средств не выполняются.",
                path: `/admin/payments/${encodeURIComponent(String(payment.id))}/resolve-dispute`,
                method: "POST",
                body: {},
              });
            }}
          >
            Разрешить платёжный спор
          </Button>
        </div>
      )}
      <dl className="detail-list">
        {Object.entries(row).map(([key, value]) => (
          <div key={key}>
            <dt>{fieldLabel(key)}</dt>
            <dd>
              <AdminValue
                value={value}
                name={key}
                currency={String(row.currency || "RUB")}
              />
            </dd>
          </div>
        ))}
      </dl>
    </Dialog>
  );
}

function AdminSection({
  section,
  mutate,
  refreshVersion,
  isOwner,
}: {
  section: string;
  mutate: (value: Mutation) => void;
  refreshVersion: number;
  isOwner: boolean;
}) {
  const canManageWallet = useCapability("wallet_write");
  const canManageSecurity = useCapability("security_manage");
  const canManageOperations = useCapability("operations_manage");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [view, setView] = useState<Row | null>(null);
  const [editing, setEditing] = useState<Row | null>(null);
  const [page, setPage] = useState(1);
  const catalogState = useApi<{ recurring_enrollment_enabled: boolean }>(
    section === "recurring" ? "/catalog" : null,
    false,
    refreshVersion,
  );
  const resource = useApi<{
    items: Row[];
    enabled?: boolean;
    recurring_enrollment_enabled?: boolean;
    settings?: Row;
    health?: Row;
    total?: number;
    page_size?: number;
  }>(
    `/admin/${section}?page=${page}&page_size=25${search ? `&search=${encodeURIComponent(search)}` : ""}`,
    false,
    refreshVersion,
  );
  if (resource.loading && !resource.data) return <Loading />;
  if (resource.error)
    return <ErrorState error={resource.error} onRetry={resource.reload} />;
  const data = resource.data;
  const items = (data?.items || []).map((row) =>
    section === "content"
      ? { ...row, id: row.id || row.key || "Материал без ключа" }
      : row,
  );
  return (
    <div className="stack">
      <div className="admin-toolbar">
        <form
          className="search-form"
          onSubmit={(e) => {
            e.preventDefault();
            setSearch(query);
            setPage(1);
          }}
        >
          <label>
            <span className="visually-hidden">{messages.mfe2fb9db79}</span>
            <Search size={17} />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={
                section === "users"
                  ? "Email, Telegram ID или @username"
                  : messages.maf55def4d0
              }
            />
          </label>
          <Button tone="secondary" type="submit">
            {messages.mbe6156b8e4}
          </Button>
        </form>
        {section === "content" && (
          <Button onClick={() => setEditing({ key: "", nodes: [] })}>
            <FileText size={16} />
            {messages.mf441e43014}
          </Button>
        )}
        {section === "broadcasts" && (
          <Button onClick={() => setEditing({ nodes: [] })}>
            <Send size={16} />
            {messages.m23c82e3742}
          </Button>
        )}
      </div>
      <OperationalControls
        section={section}
        mutate={mutate}
        onView={setView}
        refreshVersion={refreshVersion}
        isOwner={isOwner}
      />
      {section === "content" && (
        <details className="admin-disclosure">
          <summary>FAQ и инструкции подключения</summary>
          <div className="admin-disclosure-body">
            <PublicContentAdmin
              mutate={mutate}
              refreshVersion={refreshVersion}
            />
          </div>
        </details>
      )}
      {section === "recurring" && (
        <div className="card recurring-setting">
          <div>
            <h2>{messages.m3f002ef63b}</h2>
            <p>{messages.me7015581f4}</p>
          </div>
          <Button
            disabled={catalogState.loading || !catalogState.data}
            tone={
              catalogState.data?.recurring_enrollment_enabled
                ? "danger"
                : "secondary"
            }
            onClick={() =>
              mutate({
                title: messages.m12cc4b19d7,
                description: messages.mc9585f25f9,
                path: "/admin/recurring",
                method: "PATCH",
                body: {
                  enabled: !catalogState.data?.recurring_enrollment_enabled,
                },
              })
            }
          >
            {catalogState.data?.recurring_enrollment_enabled
              ? messages.m0206624e24
              : messages.m1c170f077f}
          </Button>
        </div>
      )}
      {section === "orders" && (
        <div className="notice">{messages.m003e9b2a1e}</div>
      )}
      {section === "security" && (
        <div className="notice">{messages.mbf88aa2ff2}</div>
      )}
      {section === "broadcasts" && (
        <div className="notice warning">{broadcastCopy.local}</div>
      )}
      {section === "operations" && data?.health && (
        <div className="health-grid">
          {Object.entries(data.health).map(([key, value]) => (
            <div className="card" key={key}>
              <Activity size={19} />
              <span>{fieldLabel(key)}</span>
              <strong>
                {typeof value === "string"
                  ? statusLabel(value)
                  : typeof value === "boolean"
                    ? value
                      ? messages.m460806a5f3
                      : messages.m2d953dfbc5
                    : messages.m61a18fa270}
              </strong>
            </div>
          ))}
        </div>
      )}
      <DataTable
        items={items}
        section={section}
        onView={setView}
        actions={(row) => (
          <>
            {["catalog", "content", "discounts"].includes(section) && (
              <Button
                className="small"
                tone="secondary"
                onClick={() => setEditing(row)}
              >
                {messages.mb8b151d417}
              </Button>
            )}
            {section === "security" && (
              <Button
                className="small"
                tone="secondary"
                onClick={() =>
                  mutate({
                    title: messages.m8d4c92f0e1,
                    description: `Ограничение № ${String(row.id)}. Изменение будет записано в аудит вместе с причиной.`,
                    path: `/admin/security/${encodeURIComponent(String(row.id))}/revoke`,
                    method: "POST",
                    body: {},
                  })
                }
              >
                {messages.md4b51f7a04}
              </Button>
            )}
            {section === "orders" &&
              canManageOperations &&
              [
                "paid",
                "provisioning_pending",
                "paid_pending_provisioning",
              ].includes(String(row.status)) && (
                <Button
                  className="small"
                  tone="secondary"
                  onClick={() =>
                    mutate({
                      title: messages.mb638fe319b,
                      description: messages.ma89ce0f3c0,
                      path: `/admin/orders/${encodeURIComponent(String(row.id))}/retry-provisioning`,
                      method: "POST",
                      body: {},
                    })
                  }
                >
                  {messages.m6b85d11d0c}
                </Button>
              )}
            {section === "recurring" && (
              <Button
                className="small"
                tone="danger"
                onClick={() =>
                  mutate({
                    title: messages.m2e1f3b0ca9,
                    description: `Будет запрошена отмена соглашения № ${String(row.id)}. Проверяйте окончательное подтверждение отдельно.`,
                    path: `/admin/recurring/${encodeURIComponent(String(row.id))}/cancel`,
                    method: "POST",
                    body: {},
                  })
                }
              >
                {messages.m7c47f7292b}
              </Button>
            )}
            {section === "subscriptions" && (
              <>
                <Button
                  className="small"
                  tone="secondary"
                  onClick={() => setEditing(row)}
                >
                  {messages.m104138c612}
                </Button>
                <Button
                  className="small"
                  tone="danger"
                  onClick={() => {
                    const suspended =
                      typeof row.restriction === "object" &&
                      row.restriction !== null &&
                      Boolean((row.restriction as Row).suspended);
                    mutate({
                      title: suspended
                        ? "Возобновить доступ"
                        : "Приостановить доступ",
                      description:
                        "Срок, оплаченные периоды и VPN-идентификатор сохраняются. Будет поставлена задача изменения доступа; результат панели проверяется отдельно.",
                      path: `/admin/subscriptions/${encodeURIComponent(String(row.id))}/${suspended ? "resume" : "suspend"}`,
                      method: "POST",
                      body: {},
                    });
                  }}
                >
                  {typeof row.restriction === "object" &&
                  row.restriction !== null &&
                  Boolean((row.restriction as Row).suspended)
                    ? "Возобновить"
                    : "Приостановить"}
                </Button>
                <Button
                  className="small"
                  tone="secondary"
                  onClick={() =>
                    mutate({
                      title: "Сверить подписку с панелью",
                      description:
                        "Будет поставлена задача применения сохранённого целевого состояния. Она не создаёт оплату, не добавляет срок и не меняет идентификатор. Успех показывается после проверки результата.",
                      path: `/admin/subscriptions/${encodeURIComponent(String(row.id))}/synchronize`,
                      method: "POST",
                      body: {},
                    })
                  }
                >
                  Сверить с панелью
                </Button>
              </>
            )}
            {section === "users" && (
              <>
                <UserCommunication accountId={String(row.id)} mutate={mutate} />
                {canManageWallet && (
                  <>
                    <Button
                      className="small"
                      tone="secondary"
                      onClick={() => setEditing(row)}
                    >
                      {messages.m74124f6703}
                    </Button>
                  </>
                )}
                {canManageWallet && (
                  <Button
                    className="small"
                    tone="secondary"
                    onClick={() => setEditing({ ...row, edit_kind: "credit" })}
                  >
                    Денежные средства
                  </Button>
                )}
                {canManageSecurity && (
                  <Button
                    className="small"
                    tone="secondary"
                    onClick={() =>
                      mutate({
                        title: messages.m4e7abc30cc,
                        description: `Аккаунт № ${String(row.id)}. Пользователь должен будет войти снова.`,
                        path: `/admin/users/${encodeURIComponent(String(row.id))}/revoke-sessions`,
                        method: "POST",
                        body: {},
                      })
                    }
                  >
                    {messages.mcfce77cd9f}
                  </Button>
                )}
              </>
            )}
            {section === "broadcasts" && row.state === "pending" && (
              <Button
                className="small"
                tone="danger"
                onClick={() =>
                  mutate({
                    title: messages.mafd492c804,
                    description: messages.m1552b6f65a,
                    path: `/admin/broadcasts/${encodeURIComponent(String(row.id))}/stop`,
                    method: "POST",
                    body: {},
                  })
                }
              >
                {messages.m3396400da0}
              </Button>
            )}
            {section === "operations" &&
              ["provision", "notification"].includes(String(row.kind)) && (
                <Button
                  className="small"
                  tone="secondary"
                  onClick={() =>
                    mutate({
                      title: messages.retryJobConfirm,
                      description: messages.retryJobDescription,
                      path: `/admin/operations/jobs/${encodeURIComponent(String(row.id))}/retry`,
                      method: "POST",
                      body: {},
                    })
                  }
                >
                  {messages.retryJob}
                </Button>
              )}
          </>
        )}
      />
      {data?.total != null && (
        <div className="admin-pagination">
          <span>
            Всего записей: {data.total} · страница {page}
          </span>
          <Button
            tone="secondary"
            disabled={page === 1}
            onClick={() => setPage((value) => value - 1)}
          >
            Назад
          </Button>
          <Button
            tone="secondary"
            disabled={page * (data.page_size || 25) >= data.total}
            onClick={() => setPage((value) => value + 1)}
          >
            Далее
          </Button>
        </div>
      )}
      {view &&
        (["users", "content", "broadcasts"].includes(section) ? (
          <ResourceDetail
            section={section}
            row={view}
            mutate={mutate}
            onClose={() => setView(null)}
          />
        ) : (
          <Detail row={view} onClose={() => setView(null)} mutate={mutate} />
        ))}
      {editing && (
        <Editor
          section={section}
          row={editing}
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

function Overview() {
  const initial = new URLSearchParams(location.search);
  const [from, setFrom] = useState(
    initial.get("date_from")?.slice(0, 10) || "",
  );
  const [to, setTo] = useState(initial.get("date_to")?.slice(0, 10) || "");
  const [currency, setCurrency] = useState(initial.get("currency") || "RUB");
  const [zone, setZone] = useState(initial.get("timezone") || "Europe/Moscow");
  const [interval, setInterval] = useState(initial.get("interval") || "day");
  const [source, setSource] = useState(initial.get("source") || "");
  const [product, setProduct] = useState(initial.get("product_id") || "");
  const [mode, setMode] = useState(initial.get("mode") || "cash");
  const [params, setParams] = useState(initial.toString());
  const resource = useApi<Row>(`/admin/overview${params ? "?" + params : ""}`);
  const [exportError, setExportError] = useState<unknown>(null);
  function filter(event: FormEvent) {
    event.preventDefault();
    const search = new URLSearchParams({
      currency,
      timezone: zone,
      interval,
      mode,
    });
    if (source) search.set("source", source);
    if (product) search.set("product_id", product);
    if (from)
      search.set(
        "date_from",
        from + "T00:00:00" + (zone === "UTC" ? "Z" : "+03:00"),
      );
    if (to)
      search.set(
        "date_to",
        to + "T00:00:00" + (zone === "UTC" ? "Z" : "+03:00"),
      );
    setParams(search.toString());
    history.replaceState(null, "", `/admin?${search}#overview`);
  }
  async function exportCsv() {
    try {
      const blob = new Blob(
        [exportRows(analyticsView(resource.data, appliedCurrency).rows)],
        { type: "text/csv;charset=utf-8" },
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "payments.csv";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setExportError(e);
    }
  }
  const data = resource.data;
  const applied = new URLSearchParams(params);
  const appliedCurrency = String(
    data?.currency || applied.get("currency") || "RUB",
  );
  const appliedZone = String(
    data?.timezone || applied.get("timezone") || "Europe/Moscow",
  );
  const appliedMode = String(data?.mode || applied.get("mode") || "cash");
  const view = analyticsView(data, appliedCurrency);
  const totals = view.totals;
  const rows = view.series;
  const metrics = [
    ["external_payments", messages.mc46b443b7e],
    ["refunds", messages.me8fdd44359],
    ["known_fees", messages.mb1bc2ec8ad],
    ["active_subscriptions", messages.m6382570f9c],
    ["net_after_refunds", copy.net],
    ["net_usdt", copy.settlement],
    ["previous", copy.previous],
  ];
  return (
    <div className="stack">
      <form className="analytics-filters" onSubmit={filter}>
        <div className="admin-date-label">
          {messages.m2553bb415a}
          <AdminDateField
            value={from}
            onChange={setFrom}
            label={messages.m2553bb415a}
          />
        </div>
        <div className="admin-date-label">
          {messages.mf70e3483d1}
          <AdminDateField
            value={to}
            onChange={setTo}
            min={from}
            label={messages.mf70e3483d1}
          />
        </div>
        <label>
          {messages.m6aa14ce33b}
          <select
            value={currency}
            onChange={(e) => setCurrency(e.target.value)}
          >
            <option>RUB</option>
            <option>EUR</option>
            <option>USDT</option>
          </select>
        </label>
        <label>
          {messages.mce2e24e73a}
          <select value={zone} onChange={(e) => setZone(e.target.value)}>
            <option value="Europe/Moscow">{messages.meb63f79a22}</option>
            <option value="UTC">UTC</option>
          </select>
        </label>
        <label>
          {copy.interval}
          <select
            value={interval}
            onChange={(event) => setInterval(event.target.value)}
          >
            <option value="day">{copy.day}</option>
            <option value="week">{copy.week}</option>
            <option value="month">{copy.month}</option>
          </select>
        </label>
        <label>
          {copy.mode}
          <select
            value={mode}
            onChange={(event) => setMode(event.target.value)}
          >
            <option value="cash">{copy.cash}</option>
            <option value="cohort">{copy.cohort}</option>
          </select>
        </label>
        <label>
          {copy.source}
          <select
            value={source}
            onChange={(event) => setSource(event.target.value)}
          >
            <option value="">{copy.all}</option>
            <option value="web">{copy.web}</option>
            <option value="telegram">{copy.telegram}</option>
          </select>
        </label>
        <label>
          {copy.product}
          <input
            value={product}
            onChange={(event) => setProduct(event.target.value)}
          />
        </label>
        <Button tone="secondary" type="submit">
          <SlidersHorizontal size={16} />
          {messages.me15f960d93}
        </Button>
      </form>
      {resource.loading ? (
        <Loading />
      ) : resource.error ? (
        <ErrorState error={resource.error} onRetry={resource.reload} />
      ) : (
        <>
          <div className="metrics-grid">
            {metrics.map(([key, label]) => (
              <div className="metric" key={key}>
                <span>{label}</span>
                <strong>
                  {totals[key] == null
                    ? "—"
                    : key === "active_subscriptions"
                      ? String(totals[key])
                      : money(
                          String(totals[key]),
                          key === "net_usdt" ? "USDT" : appliedCurrency,
                        )}
                </strong>
                <small>
                  {totals[key] == null
                    ? messages.m3b0f06c265
                    : key === "active_subscriptions"
                      ? messages.m115f2795dd
                      : key === "net_usdt"
                        ? "USDT · подтверждённое чистое зачисление"
                        : appliedCurrency}
                </small>
              </div>
            ))}
          </div>
          <section className="card">
            <div className="card-head">
              <div>
                <h2>{messages.mc6e3103f61}</h2>
                <p className="small-copy">
                  {messages.mb7dbd79db1}
                  {appliedCurrency} · {appliedZone}
                </p>
              </div>
              <Button tone="secondary" onClick={exportCsv}>
                <ArrowDownToLine size={16} />
                CSV
              </Button>
            </div>
            {!rows.length ? (
              <Empty title={messages.m01ece85d7f}>
                <p>{messages.mf7d789e1b4}</p>
              </Empty>
            ) : (
              <>
                <FinancialChart rows={rows} currency={appliedCurrency} />
                <div className="chart-legend">
                  <span>
                    <i />
                    {messages.ma712574ed4}
                  </span>
                  <span>
                    <i />
                    {messages.me8fdd44359}
                  </span>
                </div>
                <details className="chart-details">
                  <summary>{messages.m76e8c51b90}</summary>
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>{messages.m232a0ead18}</th>
                          <th>
                            {messages.mae24304be6}
                            {appliedCurrency}
                          </th>
                          <th>
                            {messages.m56c6f3f2cd}
                            {appliedCurrency}
                          </th>
                          <th>
                            {messages.maf7f55939a}
                            {appliedCurrency}
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((row, i) => (
                          <tr key={i}>
                            <td>{String(row.date || row.bucket || "—")}</td>
                            <td>
                              {money(
                                row.payments == null
                                  ? null
                                  : String(row.payments),
                                appliedCurrency,
                              )}
                            </td>
                            <td>
                              {money(
                                row.refunds == null
                                  ? null
                                  : String(row.refunds),
                                appliedCurrency,
                              )}
                            </td>
                            <td>
                              {money(
                                row.fees == null ? null : String(row.fees),
                                appliedCurrency,
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              </>
            )}
            {Boolean(exportError) && <ErrorState error={exportError} />}
          </section>
          <details className="card">
            <summary>{copy.rowDetails}</summary>
            <DataTable
              items={view.rows}
              section="financial-events"
              timezone={appliedZone}
            />
          </details>
          <AnalyticsPanels
            data={data}
            currency={appliedCurrency}
            timezone={appliedZone}
          />
          <div className="notice">
            {appliedMode === "cohort" ? copy.cohortNote : messages.mb856bd37da}
          </div>
          {data?.updated_at != null && (
            <p className="fine-print">
              {messages.md3a75ac5ad}
              {date(String(data.updated_at), true, appliedZone)}.{" "}
              {data?.history_complete === false ? messages.m8cb773cdb1 : ""}
            </p>
          )}
          <div className="dashboard-bottom-grid">
            <section className="card">
              <div className="card-head">
                <h2>{messages.m8ff80c24b9}</h2>
                <Activity size={20} />
              </div>
              <dl className="definition">
                <dt>{messages.m809272ba83}</dt>
                <dd>
                  {String(data?.provisioning_pending ?? messages.m76848548e2)}
                </dd>
                <dt>{messages.mc4379d1059}</dt>
                <dd>
                  {String(
                    data?.unknown_fee_count ??
                      totals.unknown_fee_count ??
                      messages.m76848548e2,
                  )}
                </dd>
              </dl>
              <a className="text-link" href="#orders">
                {messages.m0a440a3818}
                <ArrowUpRight size={15} />
              </a>
            </section>
            <section className="card">
              <div className="card-head">
                <h2>{messages.mfb3af7870f}</h2>
                <ShieldCheck size={20} />
              </div>
              <p>{messages.m52e3b63e9c}</p>
              <a className="text-link" href="#operations">
                {messages.m7bbaf5fe4c}
                <ArrowUpRight size={15} />
              </a>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
function FinancialChart({ rows, currency }: { rows: Row[]; currency: string }) {
  const values = rows.map((row) => ({
    label: String(row.date || row.bucket || ""),
    payments: Math.max(0, Number(row.payments || 0)),
    refunds: Math.max(0, Number(row.refunds || 0)),
    raw: row,
  }));
  const max = Math.max(1, ...values.flatMap((v) => [v.payments, v.refunds]));
  return (
    <div
      className="financial-chart"
      role="img"
      aria-label={`Подтверждённые оплаты и возвраты в ${currency}. Точные значения доступны в таблице ниже.`}
    >
      {values.map((v, i) => (
        <div className="chart-column" key={i}>
          <div className="bar-pair">
            <div
              className="chart-bar"
              style={{ height: `${(v.payments / max) * 100}%` }}
              title={`${v.label}: ${money(String(v.raw.payments || "0"), currency)}`}
            />
            <div
              className="chart-bar refund"
              style={{ height: `${(v.refunds / max) * 100}%` }}
              title={`${v.label}: ${money(String(v.raw.refunds || "0"), currency)}`}
            />
          </div>
          <span>{v.label.slice(5)}</span>
        </div>
      ))}
    </div>
  );
}
