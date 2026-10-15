import { useState, type FormEvent } from "react";
import { useApi } from "@api/client";
import type { Catalog } from "@api/types";
import {
  Badge,
  Button,
  Dialog,
  Empty,
  ErrorState,
  Loading,
} from "@ui/components";
import {
  date,
  money,
  safeUrl,
  SemanticContent,
  type ContentNode,
} from "@ui/content";
import { useCapability } from "@ui/staff-permissions";
import type { Mutation } from "./admin";
import { PromoEditor, promoPayload, type PromoDraft } from "./promo-editor";

type Props = { mutate: (value: Mutation) => void; refreshVersion: number };
type Campaign = {
  id: string;
  name: string;
  source: string;
  medium: string;
  campaign: string;
  url: string | null;
  website_url: string | null;
  created_at: string;
};
type CampaignStats = {
  web_visits: number;
  web_unique_visitors: number;
  telegram_starts: number;
  accounts: number;
  paid_orders: number;
  turnover: string;
  currency: string;
};
export const v11Sections = new Set([
  "campaigns",
  "promos",
  "messages",
  "backups",
  "identities",
  "nodes",
  "referrals",
]);
export function AdminV11({ section, ...props }: Props & { section: string }) {
  const canBackUp = useCapability("backups_manage");
  switch (section) {
    case "campaigns":
      return <Campaigns {...props} />;
    case "promos":
      return <Promos {...props} />;
    case "messages":
      return (
        <section className="card stack">
          <h2>Сообщение пользователю</h2>
          <p>
            Откройте пользователя или укажите его внутренний ID. Сообщение
            сохраняется в кабинете; канал доставки выбирает общий сервис.
          </p>
          <UserCommunication mutate={props.mutate} />
        </section>
      );
    case "backups":
      return canBackUp ? (
        <Backups {...props} />
      ) : (
        <Empty title="Страница не найдена" />
      );
    case "identities":
      return <IdentityReviews {...props} />;
    case "nodes":
      return <AdminNodes {...props} />;
    case "referrals":
      return <ReferralOverview refreshVersion={props.refreshVersion} />;
    default:
      return <Empty title="Страница не найдена" />;
  }
}
function ShareLink({ label, url }: { label: string; url: string | null }) {
  const [copied, setCopied] = useState(false);
  const [failure, setFailure] = useState(false);
  const href = safeUrl(url);
  if (!href)
    return <p className="fine-print">{label}: ссылка пока не настроена.</p>;
  return (
    <div className="v11-share-link">
      <a href={href} target="_blank" rel="noreferrer">
        {label}
      </a>
      <Button
        tone="secondary"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(href);
            setCopied(true);
            setFailure(false);
          } catch {
            setFailure(true);
          }
        }}
      >
        {copied ? "Скопировано" : `Скопировать: ${label}`}
      </Button>
      {failure && (
        <p role="status">Не удалось скопировать. Откройте ссылку вручную.</p>
      )}
    </div>
  );
}
function Campaigns({ mutate, refreshVersion }: Props) {
  const state = useApi<{ items: Campaign[] }>(
    "/admin/campaigns",
    false,
    refreshVersion,
  );
  const writable = useCapability("campaigns_write");
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<Campaign | null>(null);
  return (
    <div className="stack">
      <div className="notice">
        Переходы сайта, запуски бота и оплаты считаются отдельно. Рекламная
        кампания не изменяет приглашавшего пользователя или бонусные права.
        Рекламные доказательства хранятся в браузере до 30 дней.
      </div>
      {writable && (
        <Button onClick={() => setCreating(true)}>Добавить кампанию</Button>
      )}
      {state.error ? (
        <ErrorState error={state.error} onRetry={state.reload} />
      ) : !state.data ? (
        <Loading />
      ) : !state.data.items?.length ? (
        <Empty title="Кампаний пока нет" />
      ) : (
        <div className="v11-card-list">
          {state.data.items.map((item) => (
            <article className="card stack" key={item.id}>
              <h2>{item.name}</h2>
              <p className="fine-print">
                {item.source} · {item.medium} · {item.campaign}
              </p>
              <p className="fine-print">
                Создана {date(item.created_at, true)} · МСК
              </p>
              <ShareLink label="Ссылка на сайт" url={item.website_url} />
              <ShareLink label="Ссылка на бота" url={item.url} />
              <Button tone="secondary" onClick={() => setSelected(item)}>
                Статистика кампании
              </Button>
            </article>
          ))}
        </div>
      )}
      {creating && (
        <CampaignForm
          onClose={() => setCreating(false)}
          onSubmit={(body) => {
            setCreating(false);
            mutate({
              title: "Создание кампании",
              description:
                "Будет создана отдельная рекламная ссылка для сайта и бота. Данные приглашений сохраняются.",
              confirmLabel: "Создать кампанию",
              path: "/admin/campaigns",
              method: "POST",
              body,
              freshMfa: false,
            });
          }}
        />
      )}
      {selected && (
        <Dialog
          title={`Статистика: ${selected.name}`}
          onClose={() => setSelected(null)}
        >
          <CampaignStatistics id={selected.id} />
        </Dialog>
      )}
    </div>
  );
}
function CampaignForm({
  onClose,
  onSubmit,
}: {
  onClose: () => void;
  onSubmit: (body: Record<string, unknown>) => void;
}) {
  const [fields, setFields] = useState({
    name: "",
    source: "",
    medium: "",
    campaign: "",
  });
  return (
    <Dialog title="Новая кампания" onClose={onClose}>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit(
            Object.fromEntries(
              Object.entries(fields).map(([k, v]) => [k, v.trim()]),
            ),
          );
        }}
      >
        {(
          [
            ["name", "Название кампании"],
            ["source", "Источник"],
            ["medium", "Канал"],
            ["campaign", "Кампания"],
          ] as const
        ).map(([key, label]) => (
          <label key={key}>
            {label}
            <input
              value={fields[key]}
              required
              maxLength={200}
              onChange={(e) => setFields({ ...fields, [key]: e.target.value })}
            />
          </label>
        ))}
        <Button type="submit">Продолжить</Button>
      </form>
    </Dialog>
  );
}
function CampaignStatistics({ id }: { id: string }) {
  const state = useApi<CampaignStats>(
    `/admin/campaigns/${encodeURIComponent(id)}/stats`,
  );
  if (state.error)
    return <ErrorState error={state.error} onRetry={state.reload} />;
  if (!state.data) return <Loading />;
  const labels = {
    web_visits: "Переходов сайта",
    web_unique_visitors: "Уникальных посетителей",
    telegram_starts: "Запусков бота",
    accounts: "Аккаунтов",
    paid_orders: "Оплаченных заказов",
  } as const;
  return (
    <div className="stack">
      <dl className="definition">
        {Object.entries(labels).map(([key, label]) => (
          <div key={key}>
            <dt>{label}</dt>
            <dd>{state.data?.[key as keyof typeof labels] ?? "Неизвестно"}</dd>
          </div>
        ))}
        <div>
          <dt>Оборот</dt>
          <dd>{money(state.data.turnover, state.data.currency)}</dd>
        </div>
      </dl>
      <p className="fine-print">
        Оплаты относятся к последней подтверждённой кампании сайта. Перезагрузка
        страницы не создаёт нового уникального посетителя.
      </p>
    </div>
  );
}
function Promos({ mutate, refreshVersion }: Props) {
  const state = useApi<{ items: PromoDraft[] }>(
    "/admin/promos",
    false,
    refreshVersion,
  );
  const catalog = useApi<Catalog>("/catalog");
  const [draft, setDraft] = useState<PromoDraft | null>(null);
  return (
    <div className="stack">
      <div className="notice">
        Управление отдельными промокодами. Глобальные скидки, периоды и денежные
        настройки здесь не изменяются.
      </div>
      <Button
        onClick={() =>
          setDraft({
            code: "",
            enabled: true,
            percent: "10",
            reward_type: "percent",
            total_limit: null,
            audience: "all",
          })
        }
      >
        Добавить промокод
      </Button>
      {state.error ? (
        <ErrorState error={state.error} onRetry={state.reload} />
      ) : !state.data ? (
        <Loading />
      ) : !state.data.items?.length ? (
        <Empty title="Промокодов пока нет" />
      ) : (
        <div className="v11-card-list">
          {state.data.items.map((item) => (
            <article className="card stack" key={item.code}>
              <h2>{item.code}</h2>
              <p>
                {item.reward_type === "free_days"
                  ? `${item.reward_days} дней доступа`
                  : item.reward_type === "fixed_rub"
                    ? money(item.amount_rub, "RUB")
                    : `${item.percent}%`}
              </p>
              <p>
                {item.enabled ? "Действует" : "Отключён"} ·{" "}
                {item.total_limit == null
                  ? "Без общего лимита"
                  : `Лимит: ${item.total_limit}`}
              </p>
              <p className="fine-print">
                Окончание: {date(item.ends_at, true)} · МСК
              </p>
              <dl className="definition">
                <dt>Активировали пользователей</dt>
                <dd>{item.activated_users ?? "Нет данных"}</dd>
                <dt>Использований</dt>
                <dd>{item.redeemed_count ?? "Нет данных"}</dd>
                <dt>Осталось использований</dt>
                <dd>
                  {item.remaining_uses === null
                    ? "Без ограничения"
                    : (item.remaining_uses ?? "Нет данных")}
                </dd>
              </dl>
              {item.historical_usage_unknown && (
                <p className="fine-print">
                  Показаны подтверждённые активации и использования. Старые
                  данные неполные; оставшийся лимит учитывает исторические
                  резервы.
                </p>
              )}
              <Button tone="secondary" onClick={() => setDraft(item)}>
                Изменить промокод {item.code}
              </Button>
              <Button
                tone="danger"
                onClick={() =>
                  mutate({
                    title: `Удалить промокод ${item.code}?`,
                    description:
                      "Промокод будет отключён. История активаций, покупок и уже выданных прав сохранится.",
                    path: `/admin/promos/${encodeURIComponent(item.code)}`,
                    method: "DELETE",
                    body: {},
                  })
                }
              >
                Удалить промокод {item.code}
              </Button>
            </article>
          ))}
        </div>
      )}
      {draft && (
        <Dialog title="Промокод" onClose={() => setDraft(null)}>
          <form
            className="form"
            onSubmit={(e) => {
              e.preventDefault();
              const body = promoPayload(draft);
              setDraft(null);
              mutate({
                title: "Сохранение промокода",
                description: `Промокод ${body.code}. Изменяются только условия этого кода.`,
                path: "/admin/promos",
                method: "POST",
                body,
                freshMfa: false,
              });
            }}
          >
            <PromoEditor
              promo={draft}
              index={0}
              products={catalog.data?.products ?? []}
              loading={catalog.loading}
              error={catalog.error}
              onRetry={catalog.reload}
              onChange={setDraft}
            />
            <Button type="submit">Продолжить</Button>
          </form>
        </Dialog>
      )}
    </div>
  );
}
export function UserCommunication({
  mutate,
  accountId,
}: {
  mutate: Props["mutate"];
  accountId?: string;
}) {
  const messages = useCapability("messages_write");
  const grant = useCapability("subscriptions_manage");
  const [mode, setMode] = useState<"message" | "grant" | null>(null);
  const [target, setTarget] = useState(accountId ?? "");
  const [text, setText] = useState("");
  const [product, setProduct] = useState("");
  const [days, setDays] = useState(30);
  const [subscriptionId, setSubscriptionId] = useState("");
  const [devices, setDevices] = useState("");
  const catalog = useApi<Catalog>(mode === "grant" ? "/catalog" : null);
  const nodes: ContentNode[] = text.trim()
    ? [{ type: "paragraph", children: [{ type: "text", text: text.trim() }] }]
    : [];
  function submit(e: FormEvent) {
    e.preventDefault();
    if (!target.trim() || !mode) return;
    const body =
      mode === "message"
        ? { nodes, idempotency_key: crypto.randomUUID() }
        : {
            product_id: product,
            days,
            subscription_id: subscriptionId.trim() || null,
            devices: devices ? Number(devices) : null,
            idempotency_key: crypto.randomUUID(),
          };
    const selected = mode;
    setMode(null);
    mutate({
      title: selected === "message" ? "Отправка сообщения" : "Выдача доступа",
      description:
        selected === "message"
          ? `Сообщение аккаунту № ${target}. Будет создана одна доставка общим сервисом.`
          : `Выдать ${days} дней аккаунту № ${target}. Существующие платежи не меняются; подключение выполняется очередью.`,
      confirmLabel:
        selected === "message" ? "Отправить сообщение" : "Выдать доступ",
      path: `/admin/users/${encodeURIComponent(target.trim())}/${selected === "message" ? "message" : "grant"}`,
      method: "POST",
      body,
      freshMfa: selected === "grant",
      preview:
        selected === "message" ? <SemanticContent nodes={nodes} /> : undefined,
    });
  }
  return (
    <>
      <div className="account-feature-actions">
        {messages && (
          <Button
            className="small"
            tone="secondary"
            onClick={() => {
              setText("");
              setMode("message");
            }}
          >
            Написать пользователю
          </Button>
        )}
        {grant && accountId && (
          <Button
            className="small"
            tone="secondary"
            onClick={() => setMode("grant")}
          >
            Выдать доступ
          </Button>
        )}
      </div>
      {mode && (
        <Dialog
          title={
            mode === "message" ? "Сообщение пользователю" : "Выдача доступа"
          }
          onClose={() => setMode(null)}
        >
          <form className="form" onSubmit={submit}>
            {accountId ? (
              <p>Аккаунт № {accountId}</p>
            ) : (
              <label>
                ID аккаунта
                <input
                  required
                  maxLength={32}
                  value={target}
                  onChange={(e) => setTarget(e.target.value)}
                />
              </label>
            )}
            {mode === "message" ? (
              <>
                <label>
                  Текст сообщения
                  <textarea
                    required
                    maxLength={10000}
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                  />
                </label>
                <div className="notice">
                  <SemanticContent nodes={nodes} />
                </div>
              </>
            ) : (
              <>
                {catalog.error ? (
                  <ErrorState error={catalog.error} onRetry={catalog.reload} />
                ) : (
                  <label>
                    Тариф
                    <select
                      required
                      disabled={catalog.loading}
                      value={product}
                      onChange={(e) => setProduct(e.target.value)}
                    >
                      <option value="">Выберите тариф</option>
                      {(catalog.data?.products ?? []).map((item) => (
                        <option
                          key={item.id}
                          value={item.id}
                          disabled={!item.available}
                        >
                          {item.name}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <label>
                  Дней доступа
                  <input
                    type="number"
                    required
                    min={1}
                    max={3660}
                    value={days}
                    onChange={(e) => setDays(Number(e.target.value))}
                  />
                </label>
                <label>
                  ID существующей подписки · необязательно
                  <input
                    maxLength={64}
                    value={subscriptionId}
                    onChange={(e) => setSubscriptionId(e.target.value)}
                  />
                </label>
                <label>
                  Число устройств · необязательно
                  <input
                    type="number"
                    min={1}
                    max={100}
                    value={devices}
                    onChange={(e) => setDevices(e.target.value)}
                  />
                </label>
              </>
            )}
            <Button
              type="submit"
              disabled={
                mode === "grant" &&
                (!product || catalog.loading || Boolean(catalog.error))
              }
            >
              Продолжить
            </Button>
          </form>
        </Dialog>
      )}
    </>
  );
}
function Backups({ mutate, refreshVersion }: Props) {
  const state = useApi<{
    items: {
      id: string;
      state: string;
      created_at: string;
      completed_at?: string | null;
      last_error?: string | null;
    }[];
    schedule: string;
    timezone: string;
    recipient: string;
    scope: string;
  }>("/admin/backups", false, refreshVersion);
  return (
    <div className="stack">
      <p className="notice">
        Полная копия базы магазина отправляется только владельцу ежедневно в{" "}
        {state.data?.schedule ?? "16:00"} МСК. Файлы базы и ссылки на них не
        публикуются в кабинете.
      </p>
      <Button
        onClick={() =>
          mutate({
            title: "Резервная копия",
            description:
              "Поставить полную резервную копию базы магазина в очередь. Получатель — владелец.",
            path: "/admin/backups",
            method: "POST",
            body: { idempotency_key: crypto.randomUUID() },
          })
        }
      >
        Создать резервную копию
      </Button>
      {state.error ? (
        <ErrorState error={state.error} onRetry={state.reload} />
      ) : !state.data ? (
        <Loading />
      ) : !state.data.items?.length ? (
        <Empty title="Заданий резервного копирования пока нет" />
      ) : (
        state.data.items.map((item) => (
          <article className="card stack" key={item.id}>
            <h2>Резервная копия</h2>
            <Badge value={item.state} />
            <p>Создана {date(item.created_at, true)} · МСК</p>
            {item.completed_at && (
              <p>Завершена {date(item.completed_at, true)} · МСК</p>
            )}
            {item.last_error && <p className="fine-print">{item.last_error}</p>}
          </article>
        ))
      )}
    </div>
  );
}
function IdentityReviews({ mutate, refreshVersion }: Props) {
  const state = useApi<{
    items: {
      id: string;
      account_id: string;
      reason: string;
      created_at: string;
      expires_at: string;
    }[];
  }>("/admin/identity-requests", false, refreshVersion);
  return (
    <div className="stack">
      <div className="notice">
        Отдельное решение владельца при недоступности связанного Telegram.
        Проверьте обращение и владельца аккаунта перед удалением привязки.
        Подписки и история сохраняются.
      </div>
      {state.error ? (
        <ErrorState error={state.error} onRetry={state.reload} />
      ) : !state.data ? (
        <Loading />
      ) : !state.data.items?.length ? (
        <Empty title="Обращений по привязке нет" />
      ) : (
        state.data.items.map((item) => (
          <article className="card stack" key={item.id}>
            <h2>Аккаунт № {item.account_id}</h2>
            <p className="plain-message">{item.reason}</p>
            <p className="fine-print">
              Создано {date(item.created_at, true)} · МСК. Действует до{" "}
              {date(item.expires_at, true)} · МСК.
            </p>
            <Button
              tone="danger"
              onClick={() =>
                mutate({
                  title: "Удаление привязки Telegram",
                  description: `Обращение аккаунта № ${item.account_id}: ${item.reason}. Будет удалена текущая привязка и отозваны затронутые сессии. Оплаченные права сохраняются.`,
                  path: `/admin/identity-requests/${encodeURIComponent(item.id)}/resolve`,
                  method: "POST",
                  body: { idempotency_key: crypto.randomUUID() },
                })
              }
            >
              Рассмотреть удаление привязки
            </Button>
          </article>
        ))
      )}
    </div>
  );
}
type Node = {
  id: string;
  name: string;
  visible: boolean;
  status: string | null;
  observed_at: string | null;
  error: string | null;
};
function AdminNodes({ mutate, refreshVersion }: Props) {
  const state = useApi<{ items: Node[] }>(
    "/admin/nodes",
    false,
    refreshVersion,
  );
  const [draft, setDraft] = useState<Node | null>(null);
  return (
    <div className="stack">
      <p className="notice">
        Публичные названия и видимость разрешённых серверов. Настройки
        соединения и адреса панели сюда не передаются.
      </p>
      {state.error ? (
        <ErrorState error={state.error} onRetry={state.reload} />
      ) : !state.data ? (
        <Loading />
      ) : !state.data.items?.length ? (
        <Empty title="Нет разрешённых серверов" />
      ) : (
        state.data.items.map((item) => (
          <article className="card stack" key={item.id}>
            <h2>{item.name}</h2>
            <p>
              {item.visible ? "Виден пользователям" : "Скрыт"} ·{" "}
              {item.status === "online"
                ? "Работает"
                : item.status === "offline"
                  ? "Недоступен"
                  : "Статус неизвестен"}
            </p>
            <p className="fine-print">{date(item.observed_at, true)} · МСК</p>
            <Button tone="secondary" onClick={() => setDraft(item)}>
              Изменить название и видимость
            </Button>
          </article>
        ))
      )}
      {draft && (
        <Dialog title="Сервер" onClose={() => setDraft(null)}>
          <form
            className="form"
            onSubmit={(e) => {
              e.preventDefault();
              const body = { name: draft.name.trim(), visible: draft.visible };
              const id = draft.id;
              setDraft(null);
              mutate({
                title: "Настройки сервера",
                description: `Публичное название: ${body.name}. ${body.visible ? "Показывать" : "Скрыть"} сервер в кабинете.`,
                path: `/admin/nodes/${encodeURIComponent(id)}`,
                method: "PATCH",
                body,
              });
            }}
          >
            <label>
              Публичное название
              <input
                required
                maxLength={100}
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              />
            </label>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={draft.visible}
                onChange={(e) =>
                  setDraft({ ...draft, visible: e.target.checked })
                }
              />
              Показывать пользователям
            </label>
            <Button type="submit">Продолжить</Button>
          </form>
        </Dialog>
      )}
    </div>
  );
}
function ReferralOverview({ refreshVersion }: { refreshVersion: number }) {
  const [metric, setMetric] = useState<"rewards" | "invited">("rewards");
  const state = useApi<{
    items: {
      account_id: string;
      email: string | null;
      telegram_id: string | null;
      invited_users: number;
      rewards: string;
    }[];
  }>(`/admin/referrals?metric=${metric}`, false, refreshVersion);
  return (
    <div className="stack">
      <p className="notice">
        Приглашения и бонусы отделены от рекламных кампаний. Для подробного
        дерева откройте пользователя.
      </p>
      <label>
        Порядок списка
        <select
          value={metric}
          onChange={(e) => setMetric(e.target.value as "rewards" | "invited")}
        >
          <option value="rewards">По начисленным бонусам</option>
          <option value="invited">По приглашённым</option>
        </select>
      </label>
      {state.error ? (
        <ErrorState error={state.error} onRetry={state.reload} />
      ) : !state.data ? (
        <Loading />
      ) : !state.data.items?.length ? (
        <Empty title="Реферальных данных пока нет" />
      ) : (
        state.data.items.map((item) => (
          <article key={item.account_id} className="card stack">
            <h2>Аккаунт № {item.account_id}</h2>
            <p>
              {item.email ||
                (item.telegram_id
                  ? `Telegram ${item.telegram_id}`
                  : "Контакт не указан")}
            </p>
            <dl className="definition">
              <dt>Приглашено</dt>
              <dd>{item.invited_users}</dd>
              <dt>Заработано</dt>
              <dd>{money(item.rewards, "RUB")}</dd>
            </dl>
          </article>
        ))
      )}
    </div>
  );
}
