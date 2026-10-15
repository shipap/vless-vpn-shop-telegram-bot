import { messages } from "@ui/messages";
import { useInstance } from "@ui/instance";
import { instanceTerminology } from "@ui/terminology";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  Check,
  Copy,
  CreditCard,
  Gift,
  Laptop,
  Link,
  Plus,
  Power,
  QrCode,
  RefreshCw,
  Smartphone,
  Sun,
  Wifi,
  X,
} from "lucide-react";
import QRCode from "qrcode";
import { ApiError, api, useApi } from "@api/client";
import { LegalCheckbox, LegalLinks } from "@ui/legal";
import type {
  Catalog,
  Dashboard,
  PublicConfig,
  Session,
  Subscription,
  Quote,
  Order,
} from "@api/types";
import {
  Badge,
  Button,
  Dialog,
  Empty,
  ErrorState,
  Loading,
  SectionTitle,
} from "@ui/components";
import {
  CatalogDescription,
  date,
  money,
  safeUrl,
  plainTelegramText,
} from "@ui/content";
import { ru } from "@ui/ru";
import { subscriptionLabel } from "@ui/subscriptions";
import { PaidHorizon, SubscriptionFreshness } from "@ui/subscription-freshness";
import { AccountPromo, CheckoutPromo } from "./account-promo";
import { ConnectionGuide } from "./connection-guide";
import { PaymentHistory } from "./payment-history";
import { PaymentMethodChooser, paymentMethodLabel } from "./payment-methods";
import { AccountFeatures } from "./account-features";
import { CustomerDevices } from "./customer-devices";
import { CatalogComparison } from "@ui/catalog-comparison";
import { ServiceFAQ, helpCenter, type HelpCenter } from "./service-faq";
import { SupportLink } from "./SupportLink";
import { NodeNews } from "./node-news";
import {
  validNotificationAction,
  type NotificationAction,
} from "@ui/notification-center";
import CodeSlots, {
  authCodeSlotsProps,
  type CodeSlotsStatus,
} from "@ui/CodeSlots";

type Resource<T> = ReturnType<typeof useApi<T>>;
type Action = "purchase" | "renew" | "upgrade" | "downgrade" | "devices";
function checkoutPeriodLabel(period: Catalog["periods"][number]) {
  const months = Number(period.months);
  const plural = new Intl.PluralRules("ru").select(months);
  const unit =
    plural === "one"
      ? "месяц"
      : plural === "few" || plural === "other"
        ? "месяца"
        : "месяцев";
  const duration =
    Number.isFinite(months) && months > 0
      ? `${months.toLocaleString("ru-RU")} ${unit} (${period.days} дн.)`
      : `${period.days} дн.`;
  const percent = period.discount_percent ?? "0";
  return /^0+(?:\.0+)?$/.test(percent)
    ? duration
    : `${duration} −${percent.replace(".", ",")}%`;
}
type CheckoutTarget = {
  action: Action;
  subscription?: Subscription;
  quote?: Quote;
  order?: Order;
};
function retentionBlocked(subscription: Subscription) {
  return Boolean(
    subscription.retention_state && subscription.retention_state !== "none",
  );
}
function SubscriptionDeletionNotice({
  notice,
  supportUrl,
  onDismiss,
}: {
  notice: NonNullable<Dashboard["subscription_deletion_notices"]>[number];
  supportUrl?: string | null;
  onDismiss: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const request = useRef(new AbortController());
  useEffect(() => () => request.current.abort(), []);
  async function dismiss() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api<{ dismissed: boolean }>(
        `/subscriptions/${encodeURIComponent(notice.subscription_id)}/deletion-notice/dismiss`,
        { method: "POST", body: {}, signal: request.current.signal },
      );
      if (request.current.signal.aborted) return;
      if (result.dismissed !== true)
        throw new Error(
          "Не удалось сохранить скрытие уведомления. Попробуйте ещё раз.",
        );
      onDismiss();
    } catch (failure) {
      if (!request.current.signal.aborted) setError(failure);
    } finally {
      if (!request.current.signal.aborted) setBusy(false);
    }
  }
  return (
    <section className="notice subscription-deletion-notice">
      <div>
        <strong>{notice.name}</strong>
        <p>Подписка была удалена. Если это ошибка, обратитесь в поддержку.</p>
        <p>{date(notice.deleted_at, true)} · МСК</p>
        <SupportLink
          url={supportUrl}
          context={{
            kind: "subscription",
            id: notice.subscription_id,
            status: "unavailable",
          }}
        />
        {Boolean(error) && <ErrorState error={error} />}
      </div>
      <button
        type="button"
        className="icon-button"
        disabled={busy}
        aria-busy={busy}
        aria-label="Скрыть уведомление об удалении подписки"
        onClick={() => {
          void dismiss();
        }}
      >
        <X size={18} />
      </button>
    </section>
  );
}
export function SubscriptionRetention({
  subscription,
}: {
  subscription: Subscription;
}) {
  const state = subscription.retention_state;
  if (state === "archived")
    return (
      <div className="notice" role="status">
        <p>
          VPN-подписка удалена из панели. Аккаунт и история платежей сохранены.
          Можно оформить новую подписку.
        </p>
        {subscription.archived_at && (
          <p>Удалена: {date(subscription.archived_at, true)}.</p>
        )}
      </div>
    );
  if (retentionBlocked(subscription))
    return (
      <div className="notice" role="status">
        <p>
          {state === "pending"
            ? "Удаление VPN-подписки ожидает обработки. Изменения этой подписки временно недоступны."
            : state === "in_flight"
              ? "Результат удаления VPN-подписки проверяется. Изменения этой подписки временно недоступны."
              : "Удаление VPN-подписки требует проверки. Изменения этой подписки временно недоступны."}
        </p>
        <p>
          Аккаунт и история платежей сохраняются. Уже принятые платежи не
          теряются.
        </p>
      </div>
    );
  if (!subscription.cleanup_at) return null;
  return (
    <div className="notice" role="status">
      <p>
        Удаление VPN-подписки из панели запланировано через 7 дней после
        окончания срока при отсутствии препятствий. Аккаунт и история платежей
        сохранятся.
      </p>
      <p>
        Плановая дата удаления из панели: {date(subscription.cleanup_at, true)}.
      </p>
    </div>
  );
}
export function RenewalDiscount({ quote }: { quote: Quote }) {
  const selected = quote.selected_secondary;
  if (quote.action !== "renew" || typeof selected?.percent !== "string")
    return null;
  const labels: Record<string, string> = {
    renewal_campaign: "Кампания продления",
    loyalty: "Скидка за стаж",
    promo: "Промокод",
    configured_secondary: "Скидка по условиям тарифа",
  };
  return (
    <p className="notice">
      {selected.source === "near_expiry_renewal"
        ? `Скидка на продление: ${selected.percent}%`
        : `${labels[String(selected.source)] || "Выбранная скидка"}: ${selected.percent}%`}
    </p>
  );
}
export function QuoteDiscounts({ quote }: { quote: Quote }) {
  return (
    <dl className="definition">
      {(
        [
          [
            quote.promo_code
              ? `Промокод ${quote.promo_code}`
              : "Скидка по промокоду",
            quote.promo_discount_amount,
          ],
          ["Скидка за период", quote.period_discount_amount],
          ["Другие скидки", quote.secondary_discount_amount],
          ["Скидка за способ оплаты", quote.payment_method_discount_amount],
        ] as const
      ).map(
        ([label, amount]) =>
          typeof amount === "string" && (
            <div className="preview-row" key={label}>
              <dt>{label}</dt>
              <dd>{money(amount, quote.currency)}</dd>
            </div>
          ),
      )}
      {quote.minimum_external_required && quote.minimum_subscription_amount && (
        <div className="preview-row">
          <dt>Минимальная сумма к оплате</dt>
          <dd>{money(quote.minimum_subscription_amount, quote.currency)}</dd>
        </div>
      )}
    </dl>
  );
}
type CashWallet = Pick<
  Dashboard,
  | "credit_balance"
  | "credit_reserved"
  | "credit_available"
  | "credit_currency"
  | "credit_provenance"
>;
function CashBalance({ wallet }: { wallet: CashWallet }) {
  const needsCurrencyReview =
    !wallet.credit_currency && /[1-9]/.test(wallet.credit_balance || "");
  const currency = wallet.credit_currency || (needsCurrencyReview ? "" : "RUB");
  const reserved = /[1-9]/.test(wallet.credit_reserved || "");
  return (
    <section className="card cash-wallet">
      <h2>Денежные средства</h2>
      <div className="cash-balance">
        <span>Доступно для оплаты</span>
        <strong>{money(wallet.credit_available, currency).trim()}</strong>
      </div>
      {reserved && (
        <p className="cash-reserve fine-print">
          <span>Зарезервировано</span>
          <span>{money(wallet.credit_reserved, currency).trim()}</span>
        </p>
      )}
      <p className="small-copy">
        {!needsCurrencyReview
          ? "Можно оплатить стоимость после скидок и бонусов в этой валюте. Использование выбирается перед расчётом заказа."
          : "Валюта уточняется. Использование для оплаты пока недоступно."}
      </p>
    </section>
  );
}
export function Customer({
  page,
  session,
  dashboard,
  catalog,
  config,
  notificationAction = null,
  refreshSession,
  onAuthenticationRequired = refreshSession,
  onEmailMergeLogin,
  telegramLinkHint,
  onTelegramLinkHintConsumed,
}: {
  page: string;
  session: Session;
  dashboard: Resource<Dashboard>;
  catalog: Resource<Catalog>;
  config: PublicConfig | null;
  notificationAction?: NotificationAction | null;
  refreshSession: () => void;
  onAuthenticationRequired?: () => void;
  onEmailMergeLogin?: (email: string) => void;
  telegramLinkHint?: { telegramId: string; expiresAt: number } | null;
  onTelegramLinkHintConsumed?: () => void;
}) {
  const instance = useInstance();
  const terms = instanceTerminology(instance);
  const nav = {
    ...ru.nav,
    home: terms.connections_label,
    news: "Новости серверов",
  };
  const [selected, setSelected] = useState("");
  const [checkout, setCheckout] = useState<CheckoutTarget | null>(null);
  const [connect, setConnect] = useState<Subscription | null>(null);
  const [confirmation, setConfirmation] = useState<{
    title: string;
    body: string;
    path: string;
    subscriptionId: string;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    const orderId =
      typeof location === "undefined"
        ? null
        : new URLSearchParams(location.search).get("payment_order");
    if (!orderId || !/^[A-Za-z0-9_-]{1,64}$/.test(orderId)) return;
    const controller = new AbortController();
    setCheckout(null);
    api<Order>(`/orders/${encodeURIComponent(orderId)}`, {
      signal: controller.signal,
    })
      .then((order) => {
        if (controller.signal.aborted) return;
        if (order.id !== orderId)
          throw new Error("Не удалось проверить заказ.");
        setCheckout({ action: "purchase", order });
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(failure);
      });
    return () => controller.abort();
  }, [session.account.id]);
  const data =
    dashboard.data?.account.id === session.account.id ? dashboard.data : null;
  const subscriptions = data?.subscriptions || [];
  useEffect(() => {
    if (!notificationAction || !validNotificationAction(notificationAction))
      return;
    const target = subscriptions.find(
      (item) => item.id === notificationAction.metadata.subscription_id,
    );
    if (!target || retentionBlocked(target)) {
      setError(
        new Error(
          "Эта подписка недоступна для выбранного действия. Обновите список подписок.",
        ),
      );
      return;
    }
    setSelected(target.id);
    setError(null);
    if (notificationAction.operation === "connect") {
      setConnect(target);
      return;
    }
    if (notificationAction.operation !== "renew") return;
    const controller = new AbortController();
    const quoteId = notificationAction.metadata.quote_id;
    api<Quote>(
      `/subscriptions/${encodeURIComponent(target.id)}/renewal-offer${quoteId ? `?quote_id=${encodeURIComponent(quoteId)}` : ""}`,
      { signal: controller.signal },
    )
      .then((quote) => {
        if (controller.signal.aborted) return;
        if (quote.subscription_id !== target.id || quote.action !== "renew") {
          throw new Error("Не удалось проверить расчёт продления.");
        }
        setCheckout({ action: "renew", subscription: target, quote });
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(failure);
      });
    return () => controller.abort();
  }, [notificationAction]);
  const subscription =
    subscriptions.find((s) => s.id === selected) || subscriptions[0];
  const targetHeld = Boolean(subscription && retentionBlocked(subscription));
  const checkoutSubscription =
    checkout?.subscription &&
    subscriptions.find((s) => s.id === checkout.subscription!.id);
  const checkoutHeld = Boolean(
    checkout?.subscription &&
    (!checkoutSubscription || retentionBlocked(checkoutSubscription)),
  );
  const currentProduct = catalog.data?.products.find(
    (p) => p.id === subscription?.product_id,
  );
  const hasCurrentRank = Boolean(
    currentProduct && Number.isFinite(currentProduct.tier_level),
  );
  const tierProducts = hasCurrentRank
    ? (catalog.data?.products.filter(
        (p) =>
          p.available &&
          Number.isFinite(p.tier_level) &&
          p.tier_level !== currentProduct!.tier_level,
      ) ?? [])
    : [];
  const canDowngrade = tierProducts.some(
    (p) => p.tier_level < currentProduct!.tier_level,
  );
  const canUpgrade = tierProducts.some(
    (p) => p.tier_level > currentProduct!.tier_level,
  );
  const tierActions: ("upgrade" | "downgrade")[] =
    canUpgrade && canDowngrade
      ? ["upgrade", "downgrade"]
      : [canDowngrade ? "downgrade" : "upgrade"];
  const connectionSubscription =
    connect &&
    (subscriptions.find((item) => item.id === connect.id) ?? {
      ...connect,
      connection_url: null,
    });
  async function confirm() {
    if (!confirmation) return;
    const target =
      subscriptions.find((s) => s.id === confirmation.subscriptionId) ||
      data?.archived_subscription_agreements?.find(
        (item) => item.subscription_id === confirmation.subscriptionId,
      );
    if (!target) {
      setConfirmation(null);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api(confirmation.path, { method: "POST" });
      setConfirmation(null);
      dashboard.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  if (dashboard.loading && !data) return <Loading />;
  if (dashboard.error)
    return <ErrorState error={dashboard.error} onRetry={dashboard.reload} />;
  if (!data) return <Loading />;
  const subscriptionSelect =
    subscriptions.length > 1 ? (
      <label className="subscription-select">
        {messages.m83130eb296}
        <select
          value={subscription?.id}
          onChange={(e) => setSelected(e.target.value)}
        >
          {subscriptions.map((s) => (
            <option value={s.id} key={s.id}>
              {subscriptionLabel(
                s,
                subscriptions,
                catalog.data?.products || [],
              )}
            </option>
          ))}
        </select>
      </label>
    ) : null;
  return (
    <>
      <SectionTitle
        eyebrow={messages.m0543475d8a}
        title={nav[page as keyof typeof nav] || nav.home}
      >
        {page === "home" && (
          <Button
            tone="secondary"
            onClick={() => setCheckout({ action: "purchase" })}
          >
            <Plus size={17} />
            {messages.me788c66398}
          </Button>
        )}
      </SectionTitle>
      {Boolean(error) && !confirmation && <ErrorState error={error} />}
      {page === "news" && (
        <NodeNews hasSubscription={subscriptions.length > 0} />
      )}
      {(data.subscription_deletion_notices ?? []).map((notice) => (
        <SubscriptionDeletionNotice
          key={`${session.account.id}:${notice.subscription_id}`}
          notice={notice}
          supportUrl={instance.support_url || config?.support_url}
          onDismiss={dashboard.reload}
        />
      ))}
      {page === "home" &&
        (data.archived_subscription_agreements ?? []).map((item) => (
          <div className="stack" key={item.subscription_id}>
            <h3>Автопродление: {item.name}</h3>
            <Recurring
              subscription={{
                id: item.subscription_id,
                recurring: item.recurring,
              }}
              enabled={false}
              config={catalog.data}
              reload={dashboard.reload}
              onCancel={() =>
                setConfirmation({
                  title: messages.m6bc1737952,
                  body: messages.m54b97c39fc,
                  path: `/subscriptions/${encodeURIComponent(item.subscription_id)}/recurring/cancel`,
                  subscriptionId: item.subscription_id,
                })
              }
            />
          </div>
        ))}
      {page === "home" && (
        <div className="stack">
          {catalog.data && (
            <CatalogComparison
              products={catalog.data.products.filter(
                (product) => product.available,
              )}
            />
          )}
          {subscriptionSelect}
          {!subscription ? (
            <div className="welcome-card">
              {instance.visual_profile === "private" ? (
                <Sun size={43} aria-hidden="true" />
              ) : (
                <Smartphone size={43} aria-hidden="true" />
              )}
              <p className="eyebrow">{messages.m8649d38639}</p>
              <h2>
                {instance.visual_profile === "private" ? (
                  <>
                    {messages.me95af03cfd}
                    <br />
                    {messages.mbb5baca989}
                  </>
                ) : (
                  "Выберите подходящую подписку."
                )}
              </h2>
              <p>{messages.m570e2c42c1}</p>
              <Button onClick={() => setCheckout({ action: "purchase" })}>
                {messages.m514ef7e4fa}
                <ArrowUpRight size={18} />
              </Button>
              {instance.visual_profile === "private" && (
                <div className="welcome-lines" aria-hidden="true" />
              )}
            </div>
          ) : (
            <>
              <section className="subscription-card">
                <div className="subscription-main">
                  <div className="row between">
                    <span className="eyebrow">{messages.m9c651967f0}</span>
                    {targetHeld ? (
                      <span className="badge">
                        {subscription.retention_state === "archived"
                          ? "Архив"
                          : "На проверке"}
                      </span>
                    ) : (
                      <Badge value={subscription.status} />
                    )}
                  </div>
                  <h2>{subscription.name}</h2>
                  {!targetHeld && (
                    <p>
                      {subscription.credential_rotation_state
                        ? "Перевыпуск ссылки ещё проверяется. Оплаченный доступ сохраняется."
                        : subscription.connection_url
                          ? messages.m5806bd6087
                          : subscription.status.includes("provision")
                            ? messages.connectionPending
                            : messages.connectionUnavailable}
                    </p>
                  )}
                  <div className="subscription-meta">
                    <div>
                      <span>{messages.m97b8783453}</span>
                      <strong>{date(subscription.expires_at)}</strong>
                    </div>
                    <div>
                      <span>{messages.m2e29ee032c}</span>
                      <strong>
                        {subscription.devices.length}{" "}
                        <small>
                          {subscription.devices_limit === null ? (
                            "Лимит не задан в панели"
                          ) : subscription.panel_device_limit === 0 ? (
                            "Без ограничения"
                          ) : (
                            <>
                              {messages.mef9f13d03f}
                              {subscription.panel_device_limit ??
                                subscription.devices_limit}
                            </>
                          )}
                        </small>
                      </strong>
                    </div>
                  </div>
                  <SubscriptionFreshness subscription={subscription} />
                  <PaidHorizon subscription={subscription} />
                  <SubscriptionRetention subscription={subscription} />
                  {subscription.ever_connected === true && (
                    <p className="fine-print">
                      Первое подключение подтверждено
                      {subscription.first_connected_at
                        ? `: ${date(subscription.first_connected_at, true)} · МСК`
                        : "."}
                    </p>
                  )}
                  {subscription.traffic_progress_percent != null && (
                    <div className="stack">
                      <p>
                        Использовано лимита трафика:{" "}
                        {subscription.traffic_progress_percent}%.
                      </p>
                      <progress
                        aria-label="Использовано лимита трафика"
                        max={100}
                        value={Number(subscription.traffic_progress_percent)}
                      />
                    </div>
                  )}
                  {subscription.traffic_warning === true && (
                    <p className="notice">Лимит трафика почти исчерпан.</p>
                  )}
                  {!targetHeld && subscription.connection_help_due === true && (
                    <div className="notice">
                      Подписка создана больше суток назад, но первого
                      подключения пока нет. Откройте инструкцию подключения.
                      Если нужна помощь, обратитесь в поддержку.
                      <Button
                        tone="ghost"
                        onClick={() => setConnect(subscription)}
                      >
                        Инструкция подключения
                      </Button>
                    </div>
                  )}
                  <div className="subscription-actions">
                    {!targetHeld && (
                      <Button
                        className="connect-button"
                        onClick={() => setConnect(subscription)}
                      >
                        <Power size={18} />
                        {!subscription.credential_rotation_state &&
                        subscription.connection_url
                          ? terms.connect_label
                          : messages.connectionStatus}
                        <ArrowUpRight size={18} />
                      </Button>
                    )}
                    <SupportLink
                      url={instance.support_url || config?.support_url}
                      context={{
                        kind: "subscription",
                        id: subscription.id,
                        status: subscription.status,
                      }}
                    />
                  </div>
                </div>
                <div className="subscription-orbit" aria-hidden="true">
                  <div>
                    <Sun size={48} />
                  </div>
                  <span className="orbit-label">
                    {terms.service_label.toUpperCase()} / CONNECT
                  </span>
                </div>
              </section>
              {!targetHeld && (
                <div className="quick-actions">
                  <button
                    onClick={() =>
                      setCheckout({ action: "renew", subscription })
                    }
                  >
                    <RefreshCw />
                    <span>
                      <strong>{messages.m8d5e68a7ea}</strong>
                      <small>
                        {subscription.manual_renewal_discount_eligible &&
                        subscription.manual_renewal_discount_percent
                          ? "Доступная скидка учитывается при расчёте перед оплатой."
                          : messages.mb15a8c93be}
                      </small>
                    </span>
                    <ArrowUpRight />
                  </button>
                  {tierActions.map((action) => (
                    <button
                      key={action}
                      disabled={!tierProducts.length}
                      onClick={() => {
                        if (!tierProducts.length) return;
                        setCheckout({
                          action,
                          subscription,
                        });
                      }}
                    >
                      <Wifi />
                      <span>
                        <strong>
                          {canUpgrade && canDowngrade
                            ? action === "upgrade"
                              ? "Повысить тариф"
                              : "Понизить тариф"
                            : messages.mab7cfb7167}
                        </strong>
                        <small>
                          {!hasCurrentRank
                            ? "Смена тарифа недоступна: текущий тариф отсутствует в каталоге."
                            : !tierProducts.length
                              ? "Доступных тарифов другого ранга нет."
                              : messages.m0f91eec68c}
                        </small>
                      </span>
                      <ArrowUpRight />
                    </button>
                  ))}
                  <a href="#devices">
                    <Smartphone />
                    <span>
                      <strong>{messages.m676fdbab2f}</strong>
                      <small>{messages.m96d5a68892}</small>
                    </span>
                    <ArrowUpRight />
                  </a>
                </div>
              )}
              {subscription.segments.length > 0 && (
                <section className="card">
                  <div className="card-head">
                    <h2>{messages.me9a07df5db}</h2>
                    <span className="muted">
                      {subscription.segments.length}
                    </span>
                  </div>
                  <div className="segments">
                    {subscription.segments.map((segment) => (
                      <div key={segment.id}>
                        <span className="segment-dot" />
                        <div>
                          <strong>
                            {catalog.data?.products.find(
                              (p) => p.id === segment.product_id,
                            )?.name || messages.m83130eb296}
                          </strong>
                          <p>
                            {date(segment.starts_at)} — {date(segment.ends_at)}
                          </p>
                        </div>
                        <span>
                          {segment.devices}
                          {messages.m136bc829a0}
                        </span>
                      </div>
                    ))}
                  </div>
                </section>
              )}
              <Recurring
                subscription={subscription}
                enabled={!targetHeld && data.recurring_enrollment_enabled}
                config={catalog.data}
                onCancel={() =>
                  setConfirmation({
                    title: messages.m6bc1737952,
                    body: messages.m54b97c39fc,
                    path: `/subscriptions/${encodeURIComponent(subscription.id)}/recurring/cancel`,
                    subscriptionId: subscription.id,
                  })
                }
                reload={dashboard.reload}
              />
            </>
          )}
          <div className="dashboard-bottom-grid">
            <a href="#help" className="help-card">
              <span className="square-icon">
                <Laptop />
              </span>
              <div>
                <h3>{messages.m68badf9bde}</h3>
                <p>{messages.mf20ae8b252}</p>
              </div>
              <ArrowUpRight />
            </a>
            <a href="#bonuses" className="bonus-card">
              <Gift />
              <div>
                <span>{messages.m41edffff56}</span>
                <strong>{money(data.bonus_balance)}</strong>
              </div>
              <ArrowUpRight />
            </a>
          </div>
        </div>
      )}
      {page === "devices" && (
        <div className="stack">
          {subscriptionSelect}
          {!subscription ? (
            <Empty title={messages.ma967b33a2e}>
              <p>{messages.m4c1c75b28b}</p>
              <Button onClick={() => setCheckout({ action: "purchase" })}>
                {messages.m514ef7e4fa}
              </Button>
            </Empty>
          ) : targetHeld ? (
            <>
              <SubscriptionRetention subscription={subscription} />
              {subscription.retention_state === "archived" && (
                <Button onClick={() => setCheckout({ action: "purchase" })}>
                  Оформить новую подписку
                </Button>
              )}
            </>
          ) : (
            <>
              <CustomerDevices
                key={subscription.id}
                subscription={subscription}
                blocked={Boolean(data.account.unlink_blocked)}
                onAdd={() => setCheckout({ action: "devices", subscription })}
                onConnect={() => setConnect(subscription)}
                onRefresh={dashboard.reload}
                onRotate={() => {
                  setConnect(null);
                  dashboard.setData({
                    ...data,
                    subscriptions: data.subscriptions.map((item) =>
                      item.id === subscription.id
                        ? {
                            ...item,
                            credential_rotation_state: "pending",
                            connection_url: null,
                          }
                        : item,
                    ),
                  });
                }}
              />
              <div className="notice">{messages.m360be0d648}</div>
              {(data.account as unknown as { unlink_blocked?: boolean })
                .unlink_blocked && (
                <div className="notice">
                  Отвязка устройств заблокирована из-за предупреждений.
                  Остальные возможности подписки доступны. Обратитесь в
                  поддержку.
                </div>
              )}
            </>
          )}
        </div>
      )}
      {page === "payments" && (
        <PaymentHistory
          payments={data.payments}
          supportUrl={instance.support_url || config?.support_url}
        />
      )}
      {page === "bonuses" && <Bonuses key={data.account.id} data={data} />}
      {page === "account" && (
        <div className="stack">
          <Account
            session={session}
            refreshSession={refreshSession}
            botUrl={config?.bot_url}
            telegramLinkHint={telegramLinkHint}
            onTelegramLinkHintConsumed={onTelegramLinkHintConsumed}
          />
          <AccountFeatures
            key={session.account.id}
            session={session}
            refreshSession={refreshSession}
            onAuthenticationRequired={onAuthenticationRequired}
            onEmailMergeLogin={onEmailMergeLogin}
            freshProof={(onConfirmed) => (
              <LinkEmailConfirmation
                email={session.account.email}
                onConfirmed={onConfirmed}
                purpose="Для этого действия"
              />
            )}
          />
          <AccountPromo
            initial={data.active_promo || null}
            subscriptions={subscriptions.filter(
              (item) => !retentionBlocked(item),
            )}
            catalog={catalog.data}
            onComplete={dashboard.reload}
            onLegalStale={catalog.reload}
          />
        </div>
      )}
      {page === "help" && (
        <div className="stack">
          <ServiceFAQ catalog={catalog.data} config={config} />
        </div>
      )}
      {checkout && checkoutHeld && (
        <Dialog
          title="Изменение подписки недоступно"
          onClose={() => setCheckout(null)}
        >
          {checkoutSubscription && (
            <SubscriptionRetention subscription={checkoutSubscription} />
          )}
          <p>
            Уже принятые платежи сохраняются. Состояние заказа можно проверить в
            истории платежей.
          </p>
        </Dialog>
      )}
      {checkout && !checkoutHeld && catalog.data && (
        <Checkout
          key={`${checkout.action}:${checkout.subscription?.id ?? "new"}:${checkout.order?.id ?? checkout.quote?.id ?? "manual"}`}
          target={
            checkoutSubscription
              ? { ...checkout, subscription: checkoutSubscription }
              : checkout
          }
          catalog={catalog.data}
          wallet={data}
          supportUrl={instance.support_url || config?.support_url}
          onClose={() => setCheckout(null)}
          onLegalStale={catalog.reload}
          onComplete={() => {
            dashboard.reload();
          }}
        />
      )}
      {checkout && !checkoutHeld && !catalog.data && (
        <Dialog title={messages.mb2fbb44bb3} onClose={() => setCheckout(null)}>
          {catalog.loading ? (
            <Loading />
          ) : (
            <ErrorState error={catalog.error} onRetry={catalog.reload} />
          )}
        </Dialog>
      )}
      {connect &&
        connectionSubscription &&
        !retentionBlocked(connectionSubscription) && (
          <Connection
            subscription={connectionSubscription!}
            help={helpCenter(catalog.data)}
            onClose={() => setConnect(null)}
          />
        )}
      {connect &&
        connectionSubscription &&
        retentionBlocked(connectionSubscription) && (
          <Dialog
            title="Подключение недоступно"
            onClose={() => setConnect(null)}
          >
            <SubscriptionRetention subscription={connectionSubscription} />
          </Dialog>
        )}
      {confirmation && (
        <Dialog
          title={confirmation.title}
          onClose={() => setConfirmation(null)}
        >
          <p>{confirmation.body}</p>
          {Boolean(error) && <ErrorState error={error} />}
          <div className="dialog-actions">
            <Button tone="secondary" onClick={() => setConfirmation(null)}>
              {messages.m53cd4960ed}
            </Button>
            <Button tone="danger" busy={busy} onClick={confirm}>
              {messages.m27bca309f5}
            </Button>
          </div>
        </Dialog>
      )}
    </>
  );
}

function Recurring({
  subscription,
  enabled,
  config,
  onCancel,
  reload,
}: {
  subscription: Pick<Subscription, "id" | "recurring">;
  enabled: boolean;
  config: Catalog | null;
  onCancel: () => void;
  reload: () => void;
}) {
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const status = useApi<{
    loyalty: {
      percent: string;
      next_milestone_at: string | null;
      applied_to_external_amount: boolean;
    };
    dynamic_amount_supported: boolean;
  }>(
    subscription.recurring
      ? `/subscriptions/${encodeURIComponent(subscription.id)}/recurring/status`
      : null,
  );
  async function enroll() {
    setBusy(true);
    try {
      await api(
        `/subscriptions/${encodeURIComponent(subscription.id)}/recurring`,
        { method: "POST" },
      );
      reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  if (!subscription.recurring && !(enabled && config?.capabilities.recurring))
    return null;
  return (
    <section className="card">
      <div className="card-head">
        <h2>{messages.mb4a6944d2f}</h2>
        {subscription.recurring && (
          <Badge value={subscription.recurring.state} />
        )}
      </div>
      {subscription.recurring ? (
        <>
          <p>
            {messages.m1de0a2df07}
            {money(
              subscription.recurring.amount,
              subscription.recurring.currency,
            )}{" "}
            · {date(subscription.recurring.next_charge_at)}
          </p>
          <Button tone="secondary" onClick={onCancel}>
            {messages.m9d41839fa4}
          </Button>
          {status.data && (
            <div className="notice">
              <p>
                Скидка за стаж: {status.data.loyalty.percent}%.{" "}
                {status.data.loyalty.next_milestone_at
                  ? `Следующий этап: ${date(status.data.loyalty.next_milestone_at, true)}.`
                  : "Следующий этап пока не определён."}
              </p>
              {!status.data.loyalty.applied_to_external_amount && (
                <p>
                  Эта скидка не меняет согласованную сумму банковского списания.
                  Доступный расчёт ручного продления показан до оплаты.
                </p>
              )}
            </div>
          )}
          {Boolean(status.error) && (
            <ErrorState error={status.error} onRetry={status.reload} />
          )}
        </>
      ) : (
        <Button busy={busy} onClick={enroll}>
          {messages.m3aa4514168}
        </Button>
      )}
      {Boolean(error) && <ErrorState error={error} />}
    </section>
  );
}

export function Checkout({
  target,
  catalog,
  wallet,
  supportUrl,
  onClose,
  onComplete,
  onLegalStale,
}: {
  target: CheckoutTarget;
  catalog: Catalog;
  wallet: Dashboard;
  supportUrl?: string | null;
  onClose: () => void;
  onComplete: () => void;
  onLegalStale: () => void;
}) {
  const current = catalog.products.find(
    (p) => p.id === target.subscription?.product_id,
  );
  const products = catalog.products.filter(
    (p) =>
      p.available &&
      (target.action === "upgrade"
        ? Boolean(
            current &&
            Number.isFinite(current.tier_level) &&
            Number.isFinite(p.tier_level) &&
            p.tier_level > current.tier_level,
          )
        : target.action === "downgrade"
          ? Boolean(
              current &&
              Number.isFinite(current.tier_level) &&
              Number.isFinite(p.tier_level) &&
              p.tier_level < current.tier_level,
            )
          : target.action === "devices"
            ? p.id === current?.id
            : true),
  );
  const initial = new URLSearchParams(location.search).get("product");
  const [productId, setProductId] = useState(
    products.find((p) => p.id === target.quote?.product_id)?.id ||
      products.find((p) => p.id === initial)?.id ||
      products.find((p) => p.id === current?.id)?.id ||
      products[0]?.id ||
      "",
  );
  const selectedProduct = products.find((p) => p.id === productId);
  const availablePeriods = selectedProduct?.periods?.length
    ? selectedProduct.periods
    : catalog.periods;
  const [period, setPeriod] = useState(
    target.quote?.period_days ?? availablePeriods[0]?.days ?? 0,
  );
  const selectedPeriod = availablePeriods.find((item) => item.days === period);
  useEffect(() => {
    setPeriod((previous) =>
      availablePeriods.some((p) => p.days === previous)
        ? previous
        : availablePeriods[0]?.days || 0,
    );
  }, [availablePeriods]);
  const [bonus, setBonus] = useState("0.00");
  const [useCredit, setUseCredit] = useState(false);
  const [credit, setCredit] = useState("0.00");
  const [extra, setExtra] = useState(target.action === "devices" ? 1 : 0);
  const [quote, setQuote] = useState<Quote | null>(target.quote ?? null);
  const paymentMethods = catalog.payment_methods;
  const [paymentMethod, setPaymentMethod] = useState<
    NonNullable<Quote["payment_method"]>["id"] | null
  >(target.quote?.payment_method?.id ?? null);
  const methodRequired = paymentMethods !== undefined;
  const calculation = useRef(0);
  const [order, setOrder] = useState<Order | null>(target.order ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [testConfirm, setTestConfirm] = useState(false);
  const [acceptedLegal, setAcceptedLegal] = useState(false);
  const [orderReadFailed, setOrderReadFailed] = useState(false);
  const [orderAutoPaused, setOrderAutoPaused] = useState(false);
  const completion = useRef(onComplete);
  const completedSignature = useRef("");
  completion.current = onComplete;
  function reportOrder(result: Order) {
    if (!["paid", "completed", "fulfilled"].includes(result.status)) return;
    const signature = JSON.stringify([
      result.id,
      result.status,
      result.provisioning_status ?? null,
      result.subscription_id,
    ]);
    if (completedSignature.current === signature) return;
    completedSignature.current = signature;
    completion.current();
  }
  useEffect(() => {
    if (
      !order ||
      !(
        order.status === "awaiting_payment" ||
        (order.status === "paid" && order.provisioning_status !== "ready")
      )
    )
      return;
    const automatic = order.provisioning_status !== "review";
    const controller = new AbortController();
    let deadline = Date.now() + 10 * 60 * 1000;
    setOrderAutoPaused(false);
    let timer: ReturnType<typeof setTimeout>;
    let inFlight = false;
    let failures = 0;
    async function readOrder() {
      if (
        controller.signal.aborted ||
        inFlight ||
        document.visibilityState !== "visible"
      )
        return;
      clearTimeout(timer);
      inFlight = true;
      const request = new AbortController();
      const abort = () => request.abort();
      controller.signal.addEventListener("abort", abort, { once: true });
      const timeout = setTimeout(abort, 10000);
      try {
        const result = await api<Order>(
          `/orders/${encodeURIComponent(order!.id)}`,
          { signal: request.signal },
        );
        if (controller.signal.aborted) return;
        setOrder(result);
        setOrderReadFailed(false);
        failures = 0;
        reportOrder(result);
      } catch {
        if (!controller.signal.aborted) {
          failures += 1;
          setOrderReadFailed(true);
        }
      } finally {
        clearTimeout(timeout);
        controller.signal.removeEventListener("abort", abort);
        inFlight = false;
        if (
          !controller.signal.aborted &&
          automatic &&
          Date.now() < deadline &&
          document.visibilityState === "visible"
        )
          timer = setTimeout(
            readOrder,
            Math.min(30000, 4000 * 2 ** Math.min(failures, 3)),
          );
        else if (!controller.signal.aborted && Date.now() >= deadline)
          setOrderAutoPaused(true);
      }
    }
    const visible = () => {
      if (document.visibilityState === "visible") {
        deadline = Date.now() + 10 * 60 * 1000;
        setOrderAutoPaused(false);
        void readOrder();
      } else clearTimeout(timer);
    };
    window.addEventListener("focus", visible);
    document.addEventListener("visibilitychange", visible);
    if (automatic) void readOrder();
    return () => {
      controller.abort();
      clearTimeout(timer);
      window.removeEventListener("focus", visible);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [order?.id, order?.status, order?.provisioning_status]);
  useEffect(() => setAcceptedLegal(false), [catalog.legal.revision]);
  const stepContent = useRef<HTMLDivElement>(null);
  useEffect(() => {
    stepContent.current
      ?.closest("dialog")
      ?.scrollTo({ top: 0, behavior: "instant" });
  }, [quote?.id, order?.id]);
  const key = useRef(crypto.randomUUID());
  const cash = wallet;
  const productCurrency = catalog.products.find(
    (product) => product.id === productId,
  )?.currency;
  const cashUsable = Boolean(
    cash.credit_currency &&
    cash.credit_currency === productCurrency &&
    cash.credit_available &&
    /^\d+(\.\d+)?$/.test(cash.credit_available) &&
    BigInt(cash.credit_available.replace(".", "")) > 0n,
  );
  const positiveAmount = (value: string) =>
    /^\d+(?:\.\d+)?$/.test(value) && /[1-9]/.test(value);
  const internalCashRequested = Boolean(
    methodRequired &&
    !paymentMethods?.some((method) => method.available) &&
    useCredit &&
    cashUsable &&
    positiveAmount(credit),
  );
  const selectedMethodAvailable = Boolean(
    paymentMethods?.some(
      (method) => method.id === paymentMethod && method.available,
    ),
  );
  const methodAvailable =
    !methodRequired || selectedMethodAvailable || internalCashRequested;
  const internalCashQuote = Boolean(
    internalCashRequested &&
    quote &&
    quote.payment_method === null &&
    /^0+(?:\.0+)?$/.test(quote.total) &&
    positiveAmount(quote.credit_amount) &&
    quote.minimum_external_required === false,
  );
  const quoteMatchesMethod =
    !methodRequired ||
    internalCashQuote ||
    Boolean(
      selectedMethodAvailable && quote?.payment_method?.id === paymentMethod,
    );
  const titles = {
    purchase: messages.ma36ee8fe08,
    renew: messages.m8d5e68a7ea,
    upgrade: "Повысить тариф",
    downgrade: "Понизить тариф",
    devices: messages.mf6f9fc9f6d,
  };
  async function calculate(event: FormEvent) {
    event.preventDefault();
    if (busy || !acceptedLegal || !catalog.legal.configured || !methodAvailable)
      return;
    if (internalCashRequested) setPaymentMethod(null);
    await requestQuote(internalCashRequested ? null : paymentMethod);
  }
  async function requestQuote(method: typeof paymentMethod, previous?: Quote) {
    const version = ++calculation.current;
    setBusy(true);
    setError(null);
    try {
      if (acceptedLegal)
        await api("/legal/accept", {
          method: "POST",
          body: {
            documents: catalog.legal.documents,
            context: "checkout",
          },
        });
      const result = await api<Quote>("/quotes", {
        method: "POST",
        body: {
          product_id: previous?.product_id ?? productId,
          period_days: previous?.period_days ?? period,
          action: target.action,
          subscription_id: target.subscription?.id,
          bonus_amount: previous?.bonus_amount ?? (bonus || "0.00"),
          credit_amount:
            previous?.credit_amount ??
            (useCredit && cashUsable ? credit || "0.00" : "0.00"),
          extra_devices: previous?.device_pricing?.extra_devices ?? extra,
          ...(methodRequired ? { payment_method: method } : {}),
        },
      });
      if (version !== calculation.current) return;
      setQuote(result);
      key.current = crypto.randomUUID();
    } catch (e) {
      if (version !== calculation.current) return;
      setError(e);
      if (e instanceof ApiError && e.code.startsWith("legal_")) {
        setAcceptedLegal(false);
        onLegalStale();
      }
    } finally {
      if (version === calculation.current) setBusy(false);
    }
  }
  async function choosePaymentMethod(method: typeof paymentMethod) {
    if (
      busy ||
      !paymentMethods?.some((item) => item.id === method && item.available)
    )
      return;
    setPaymentMethod(method);
    if (quote) {
      setQuote(null);
      await requestQuote(method, quote);
    }
  }
  async function createOrder() {
    if (
      busy ||
      !quote ||
      !quoteMatchesMethod ||
      !acceptedLegal ||
      !catalog.legal.configured
    )
      return;
    setBusy(true);
    setError(null);
    try {
      const result = await api<Order>("/orders", {
        method: "POST",
        body: { quote_id: quote.id, idempotency_key: key.current },
      });
      setOrder(result);
      reportOrder(result);
    } catch (e) {
      setError(e);
      if (e instanceof ApiError && e.code.startsWith("legal_")) {
        setQuote(null);
        setAcceptedLegal(false);
        onLegalStale();
      }
    } finally {
      setBusy(false);
    }
  }
  async function refresh() {
    if (!order) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api<Order>(
        `/orders/${encodeURIComponent(order.id)}`,
      );
      setOrder(result);
      reportOrder(result);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function testPay() {
    if (!order || !testConfirm) return;
    setBusy(true);
    setError(null);
    try {
      await api(`/orders/${encodeURIComponent(order.id)}/test-pay`, {
        method: "POST",
      });
      await refresh();
      setTestConfirm(false);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      title={target.order ? "Статус заказа" : titles[target.action]}
      onClose={onClose}
    >
      {Boolean(error) && <ErrorState error={error} />}
      <div className="stack" ref={stepContent}>
        {!order && paymentMethods !== undefined && (
          <PaymentMethodChooser
            methods={paymentMethods}
            value={paymentMethod}
            disabled={busy}
            onChange={choosePaymentMethod}
          />
        )}
        {!order && !quote && (
          <form onSubmit={calculate} className="form">
            {target.subscription && (
              <div className="notice">
                {messages.mdc27534cfb}
                {target.subscription.name} · {current?.name}
              </div>
            )}
            <label>
              {messages.mc593284717}
              <select
                value={productId}
                onChange={(e) => setProductId(e.target.value)}
                required
              >
                {products.map((p) => (
                  <option value={p.id} key={p.id}>
                    {plainTelegramText(p.name)}
                  </option>
                ))}
              </select>
            </label>
            <div className="field-grid">
              {target.action !== "devices" && (
                <label>
                  {messages.me97ca72e2c}
                  <select
                    value={period}
                    onChange={(e) => setPeriod(Number(e.target.value))}
                  >
                    {availablePeriods.map((p) => (
                      <option key={p.days} value={p.days}>
                        {checkoutPeriodLabel(p)}
                        {"price" in p && typeof p.price === "string"
                          ? ` · ${money(p.price, selectedProduct?.currency)}`
                          : ""}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {Number(wallet.bonus_balance) > 0 && (
                <label>
                  {messages.m559ce48578}
                  <input
                    inputMode="decimal"
                    pattern="\d+(\.\d{1,2})?"
                    value={bonus}
                    onChange={(e) => setBonus(e.target.value.replace(",", "."))}
                  />
                </label>
              )}
              {target.action === "devices" && (
                <label>
                  {messages.m69112bdeb9}
                  <input
                    type="number"
                    min="1"
                    max="20"
                    value={extra}
                    onChange={(e) => setExtra(Number(e.target.value))}
                  />
                </label>
              )}
            </div>
            {target.action !== "devices" &&
              selectedPeriod &&
              "price" in selectedPeriod &&
              typeof selectedPeriod.price === "string" && (
                <p className="notice" role="status">
                  Стоимость срока:{" "}
                  {money(selectedPeriod.price, selectedProduct?.currency)}
                </p>
              )}
            {selectedProduct && (
              <CatalogDescription
                nodes={selectedProduct.content?.description}
                text={selectedProduct.description}
              />
            )}
            <CheckoutPromo onProfile={onClose} />
            {target.action === "devices" && (
              <p className="notice">
                Докупка действует до окончания оплаченного срока. При продлении
                устройство оплачивается за каждый месяц выбранного периода.
                Скидки и итоговая сумма указаны в расчёте.
              </p>
            )}
            <CashBalance wallet={cash} />
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={useCredit}
                disabled={!cashUsable}
                onChange={(event) => setUseCredit(event.target.checked)}
              />
              Использовать денежные средства
            </label>
            {useCredit && cashUsable && (
              <label>
                Сумма из денежных средств
                <input
                  inputMode="decimal"
                  pattern="\d+(\.\d{1,2})?"
                  value={credit}
                  onChange={(event) =>
                    setCredit(event.target.value.replace(",", "."))
                  }
                  required
                />
                <span className="fine-print">
                  {cash.credit_currency}; до оставшейся стоимости заказа. Лимит
                  промобонусов к денежным средствам не применяется.
                </span>
              </label>
            )}
            <p className="fine-print">{messages.mac8570f36b}</p>
            <LegalCheckbox
              legal={catalog.legal}
              checked={acceptedLegal}
              onChange={setAcceptedLegal}
            />
            <Button
              type="submit"
              busy={busy}
              disabled={
                !productId ||
                !period ||
                !methodAvailable ||
                !acceptedLegal ||
                !catalog.legal.configured
              }
            >
              {messages.m3fdf9970d5}
              <ArrowRight size={17} />
            </Button>
          </form>
        )}
        {quote && !order && (
          <>
            <LegalLinks legal={catalog.legal} />
            <LegalCheckbox
              legal={catalog.legal}
              checked={acceptedLegal}
              onChange={setAcceptedLegal}
            />
            <div className="quote-total">
              <span>{messages.mb99649088e}</span>
              <strong>{money(quote.total, quote.currency)}</strong>
            </div>
            <RenewalDiscount quote={quote} />
            <QuoteDiscounts quote={quote} />
            {methodRequired && !quoteMatchesMethod && (
              <p className="notice">
                Для оставшейся суммы нужен доступный способ оплаты. Измените
                параметры заказа или обратитесь в поддержку.
              </p>
            )}
            {quote.payment_method && (
              <p className="notice">
                {paymentMethodLabel(quote.payment_method)}
              </p>
            )}
            <dl className="definition">
              <dt>{messages.mc593284717}</dt>
              <dd>
                {plainTelegramText(
                  catalog.products.find((p) => p.id === quote.product_id)
                    ?.name || "",
                )}
              </dd>
              {quote.period_days != null && (
                <>
                  <dt>Период</dt>
                  <dd>{quote.period_days} дней</dd>
                </>
              )}
              <dt>{messages.m57269a0290}</dt>
              <dd>{money(quote.base_amount, quote.currency)}</dd>
              <dt>{messages.m426aa84535}</dt>
              <dd>{money(quote.discount_amount, quote.currency)}</dd>
              <dt>{messages.m74124f6703}</dt>
              <dd>{money(quote.bonus_amount, quote.currency)}</dd>
              <dt>Денежные средства</dt>
              <dd>
                {money(
                  quote.credit_amount,
                  quote.credit_currency || quote.currency,
                )}
              </dd>
              <dt>{messages.m2e29ee032c}</dt>
              <dd>{quote.devices_limit}</dd>
              {quote.device_pricing && (
                <>
                  <dt>Дополнительные устройства</dt>
                  <dd>
                    {money(quote.device_pricing.amount, quote.currency)} ·{" "}
                    {quote.device_pricing.mode === "fixed_remaining_term"
                      ? "на оставшийся срок"
                      : `за ${quote.device_pricing.billing_months} мес.`}
                  </dd>
                </>
              )}
              <dt>{messages.md637f744af}</dt>
              <dd>{date(quote.resulting_expires_at, true)}</dd>
              <dt>Цена актуальна до</dt>
              <dd>{date(quote.expires_at, true)}</dd>
            </dl>
            <p className="fine-print">
              Это срок расчёта цены. Срок оплаты созданного счёта определяет
              платёжный сервис.
            </p>
            {quote.breakdown.length > 0 && (
              <details>
                <summary>{messages.breakdown}</summary>
                <dl className="definition">
                  {quote.breakdown.map((step, index) => (
                    <div className="preview-row" key={index}>
                      <dt>{step.label}</dt>
                      <dd>{money(step.amount, quote.currency)}</dd>
                    </div>
                  ))}
                </dl>
              </details>
            )}
            <div className="dialog-actions">
              <Button tone="secondary" onClick={() => setQuote(null)}>
                {messages.mb8b151d417}
              </Button>
              <Button
                busy={busy}
                disabled={
                  !acceptedLegal ||
                  !catalog.legal.configured ||
                  !quoteMatchesMethod
                }
                onClick={createOrder}
              >
                {messages.m816fdce097}
                <ArrowRight size={17} />
              </Button>
            </div>
          </>
        )}
        {order && (
          <>
            <div className="row between">
              <span className="eyebrow">
                {messages.m0d76109bb4}
                {order.id}
              </span>
              <Badge value={order.status} />
            </div>
            <div className="quote-total">
              <span>{messages.mb6e7ac23ef}</span>
              <strong>{money(order.total, order.currency)}</strong>
            </div>
            {order.payment_method && (
              <p>{paymentMethodLabel(order.payment_method)}</p>
            )}
            {["fulfilled", "completed", "paid"].includes(order.status) ? (
              <div className="notice">
                {order.status === "paid"
                  ? order.provisioning_status === "ready"
                    ? "Оплата подтверждена. Доступ готов. Откройте подписку."
                    : order.provisioning_status === "pending"
                      ? "Оплата подтверждена. Подготовка подключения продолжается."
                      : order.provisioning_status === "review"
                        ? "Оплата подтверждена. Выдача доступа требует проверки. Обратитесь в поддержку по заказу."
                        : "Оплата подтверждена. Состояние подключения ещё уточняется."
                  : messages.m119415b0ef}
              </div>
            ) : (
              <p>
                {order.status === "awaiting_payment"
                  ? orderAutoPaused
                    ? "Автоматическая проверка приостановлена. Статус обновится при возвращении к этому окну. Заказ также доступен в платежах."
                    : "Статус оплаты обновляется автоматически. Переход со страницы оплаты сам по себе не подтверждает платёж."
                  : "Сохранённый статус заказа показан выше. Подробности доступны в платежах; при необходимости обратитесь в поддержку."}
              </p>
            )}
            {order.status === "awaiting_payment" &&
              order.payment_expires_at && (
                <p className="fine-print">
                  Оплатить до: {date(order.payment_expires_at, true)} · МСК
                </p>
              )}
            {order.status === "awaiting_payment" &&
              safeUrl(order.checkout_url) && (
                <a
                  className="button success"
                  href={safeUrl(order.checkout_url)}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {messages.mb5d247f76d}
                  <ArrowUpRight size={17} />
                </a>
              )}
            {order.environment === "local_test" &&
              order.status === "awaiting_payment" && (
                <div className="notice warning">
                  <p>{messages.m667981646e}</p>
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={testConfirm}
                      onChange={(e) => setTestConfirm(e.target.checked)}
                    />
                    {messages.m0275b600b3}
                  </label>
                  <Button
                    tone="secondary"
                    disabled={!testConfirm}
                    busy={busy}
                    onClick={testPay}
                  >
                    {messages.ma2ba31c3f0}
                  </Button>
                </div>
              )}
            {orderReadFailed && (
              <p className="fine-print">
                Не удалось обновить данные. Последний подтверждённый статус
                сохранён;{" "}
                {orderAutoPaused || order.provisioning_status === "review"
                  ? "проверим при возвращении к этому окну."
                  : "повторим проверку автоматически."}
              </p>
            )}
            <a href="#payments" onClick={onClose}>
              Открыть платежи
            </a>
            <SupportLink
              url={supportUrl}
              context={{ kind: "order", id: order.id, status: order.status }}
            />
          </>
        )}
      </div>
    </Dialog>
  );
}

function Connection({
  subscription,
  onClose,
  help,
}: {
  subscription: Subscription;
  onClose: () => void;
  help: HelpCenter;
}) {
  const terms = instanceTerminology(useInstance());
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [showQr, setShowQr] = useState(false);
  const [qr, setQr] = useState("");
  const url = subscription.credential_rotation_state
    ? undefined
    : safeUrl(subscription.connection_url);
  useEffect(() => {
    let live = true;
    if (url && showQr)
      QRCode.toDataURL(url, {
        width: 230,
        margin: 2,
        errorCorrectionLevel: "M",
      })
        .then((value) => {
          if (live) setQr(value);
        })
        .catch(setError);
    return () => {
      live = false;
    };
  }, [url, showQr]);
  async function copy() {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
    } catch (error) {
      setError(error);
    }
  }
  return (
    <Dialog title={terms.connect_label} onClose={onClose}>
      <div className="stack">
        <p>
          {messages.mba08a4d762}
          {subscription.name}»
        </p>
        <ConnectionGuide
          key={subscription.id}
          subscriptionId={subscription.id}
          fallback={help.connection_instructions}
        />
        {url ? (
          <>
            <div className="row">
              <Button tone="primary" onClick={() => setShowQr(!showQr)}>
                <QrCode size={17} />
                {showQr ? messages.mf5e0dba0cf : messages.m6df4af1a3b}
              </Button>
              <a
                className="button secondary"
                href={url}
                target="_blank"
                rel="noreferrer"
              >
                Открыть подписку
                <ArrowUpRight size={17} />
              </a>
              <Button onClick={copy}>
                {copied ? <Check size={17} /> : <Copy size={17} />}{" "}
                {copied ? messages.mbb67fa3d1a : messages.m8d81013698}
              </Button>
            </div>
            <div className="connection-config">
              <span className="small-copy">Ссылка на конфигурацию</span>
              <button
                type="button"
                className="connection-url"
                onClick={copy}
                aria-label="Скопировать ссылку на конфигурацию"
              >
                <code>{url}</code>
              </button>
              <p className="small-copy" aria-live="polite">
                {copied
                  ? "Ссылка скопирована."
                  : "Нажмите на ссылку, чтобы скопировать её, и импортируйте подписку в приложение."}
              </p>
            </div>
            {showQr && qr && (
              <img
                src={qr}
                width={230}
                height={230}
                className="qr-code"
                alt={`QR-код для импорта вашей подписки в приложение (${terms.client_label})`}
              />
            )}
          </>
        ) : (
          <div className="notice warning">
            {subscription.credential_rotation_state
              ? "Перевыпуск ссылки ещё проверяется. Старая ссылка временно скрыта; новая появится после подтверждения панели. Оплаченный срок сохраняется."
              : messages.mb50f010ca4}
          </div>
        )}
        {Boolean(error) && <ErrorState error={error} />}
      </div>
    </Dialog>
  );
}

function Bonuses({ data }: { data: Dashboard }) {
  const [copied, setCopied] = useState<"bot" | "website" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const details = useApi<Record<string, unknown>>("/account/bonuses");
  const referralState = useApi<NonNullable<Dashboard["referral"]>>(
    data.referral ? null : "/referral",
  );
  const referralData = data.referral ?? referralState.data;
  const referral =
    typeof referralData?.referral_url === "string"
      ? safeUrl(referralData.referral_url)
      : undefined;
  const websiteReferral =
    typeof referralData?.website_referral_url === "string"
      ? websiteReferralForBrowser(
          referralData.website_referral_url,
          typeof window === "undefined" ? "" : window.location.hostname,
        )
      : undefined;
  return (
    <div className="stack">
      <CashBalance wallet={data} />
      <div className="bonus-hero">
        <Gift size={42} />
        <div>
          <span>{messages.me97b824182}</span>
          <strong>{money(data.bonus_balance)}</strong>
          <p>{messages.mf7c9e10372}</p>
        </div>
      </div>
      <section className="card">
        <div className="card-head">
          <h2>{messages.m11410a952d}</h2>
          <ArrowUpRight />
        </div>
        <p>{messages.me8e3a4be1b}</p>
        {!referralData && referralState.loading ? (
          <Loading />
        ) : !referralData && referralState.error ? (
          <ErrorState
            error={referralState.error}
            onRetry={referralState.reload}
          />
        ) : referral || websiteReferral ? (
          <div className="stack">
            {referral && (
              <div className="referral-share-row">
                <h3>Пригласить в Telegram</h3>
                <a href={referral} target="_blank" rel="noreferrer">
                  {referral}
                </a>
                <Button
                  tone="secondary"
                  onClick={async () => {
                    try {
                      setError(null);
                      await navigator.clipboard.writeText(referral);
                      setCopied("bot");
                    } catch (e) {
                      setError(e);
                    }
                  }}
                >
                  <Copy size={17} />
                  {copied === "bot"
                    ? messages.mbb67fa3d1a
                    : "Скопировать ссылку в Telegram"}
                </Button>
              </div>
            )}
            {websiteReferral && (
              <div className="referral-share-row">
                <h3>Пригласить на сайт</h3>
                <a href={websiteReferral} target="_blank" rel="noreferrer">
                  {websiteReferral}
                </a>
                <Button
                  tone="secondary"
                  onClick={async () => {
                    try {
                      setError(null);
                      await navigator.clipboard.writeText(websiteReferral);
                      setCopied("website");
                    } catch (error) {
                      setError(error);
                    }
                  }}
                >
                  <Copy size={17} />
                  {copied === "website"
                    ? "Ссылка на сайт скопирована"
                    : "Скопировать ссылку на сайт"}
                </Button>
              </div>
            )}
          </div>
        ) : (
          <p className="fine-print">{messages.mb9b8b8ddef}</p>
        )}
        {Boolean(error) && <ErrorState error={error} />}
      </section>
      {Boolean(details.error) && (
        <ErrorState error={details.error} onRetry={details.reload} />
      )}
      {Array.isArray(details.data?.ledger) && (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>{messages.m232a0ead18}</th>
                <th>{messages.m822d93cab5}</th>
                <th>{messages.m46a0edbb58}</th>
              </tr>
            </thead>
            <tbody>
              {(
                details.data.ledger as {
                  id: string;
                  created_at: string;
                  reason: string;
                  amount: string;
                }[]
              ).map((row) => (
                <tr key={row.id}>
                  <td>{date(row.created_at, true)}</td>
                  <td>{row.reason}</td>
                  <td>{money(row.amount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function websiteReferralForBrowser(value: string, browserHostname: string) {
  const href = safeUrl(value);
  if (!href) return undefined;
  try {
    const target = new URL(href, `https://${browserHostname || "invalid"}/`);
    const isLoopback = (hostname: string) => {
      const host = hostname
        .toLowerCase()
        .replace(/^\[|\]$/g, "")
        .replace(/\.$/, "");
      return (
        host === "localhost" ||
        host === "::1" ||
        /^127(?:\.\d{1,3}){3}$/.test(host)
      );
    };
    if (isLoopback(target.hostname) && !isLoopback(browserHostname))
      return undefined;
    return href;
  } catch {
    return undefined;
  }
}

function LinkEmailConfirmation({
  email,
  onConfirmed,
  purpose = "Для привязки",
}: {
  email: string | null | undefined;
  onConfirmed: () => Promise<void>;
  purpose?: string;
}) {
  const [challenge, setChallenge] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [codeStatus, setCodeStatus] = useState<CodeSlotsStatus>("idle");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [deadline, setDeadline] = useState(0);
  const [expiresAt, setExpiresAt] = useState(0);
  const [now, setNow] = useState(Date.now());
  const errorId = useId();
  const submitting = useRef(false);
  const mounted = useRef(true);
  const controller = useRef<AbortController | null>(null);
  const left = Math.max(0, Math.ceil((deadline - now) / 1000));
  const expired = Boolean(challenge && now >= expiresAt);
  useEffect(() => {
    mounted.current = true;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      mounted.current = false;
      controller.current?.abort();
      clearInterval(timer);
    };
  }, []);
  useEffect(() => {
    if (expired) {
      setCode("");
      setCodeStatus("error");
    }
  }, [expired]);
  async function send() {
    if (submitting.current || (challenge && left > 0)) return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    const request = new AbortController();
    controller.current = request;
    try {
      const result = await api<{
        challenge_id: string;
        resend_after: number;
        expires_in: number;
      }>("/account/link/email/request", {
        method: "POST",
        body: {},
        signal: request.signal,
      });
      if (!mounted.current) return;
      const sentAt = Date.now();
      setChallenge(result.challenge_id);
      setCode("");
      setCodeStatus("idle");
      setDeadline(sentAt + result.resend_after * 1000);
      setExpiresAt(sentAt + result.expires_in * 1000);
      setNow(sentAt);
    } catch (error) {
      if (mounted.current && !request.signal.aborted) setError(error);
    } finally {
      submitting.current = false;
      controller.current = null;
      if (mounted.current) setBusy(false);
    }
  }
  async function verify(event: FormEvent) {
    event.preventDefault();
    if (submitting.current || !challenge || code.length !== 6 || expired)
      return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    const request = new AbortController();
    controller.current = request;
    try {
      const result = await api<{ confirmed: boolean; expires_in: number }>(
        "/account/link/email/verify",
        {
          method: "POST",
          body: { challenge_id: challenge, code },
          signal: request.signal,
        },
      );
      if (!mounted.current) return;
      if (!result.confirmed)
        throw new Error("Не удалось подтвердить почту. Попробуйте ещё раз.");
      await onConfirmed();
    } catch (error) {
      if (mounted.current && !request.signal.aborted) {
        setCode("");
        setCodeStatus("error");
        setError(error);
      }
    } finally {
      submitting.current = false;
      controller.current = null;
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <section
      className="stack"
      aria-label="Подтверждение почты для привязки Telegram"
    >
      <h3>Подтвердите почту</h3>
      <p>
        {purpose} отправим код на {email}. Входить в аккаунт заново не нужно.
      </p>
      {!challenge ? (
        <>
          <Button busy={busy} onClick={send}>
            Подтвердить почту
          </Button>
          {Boolean(error) && <ErrorState error={error} />}
        </>
      ) : (
        <form className="form" onSubmit={verify}>
          <p className="fine-print">
            Письмо не пришло? Проверьте папку «Спам». Код действует 10 минут.
            Повторная отправка доступна через 120 секунд.
          </p>
          <label>
            Код подтверждения почты
            <CodeSlots
              {...authCodeSlotsProps}
              key={challenge}
              value={code}
              status={codeStatus}
              disabled={busy || expired}
              ariaLabel="Код подтверждения почты"
              ariaDescribedBy={error ? errorId : undefined}
              autoFocus
              onChange={(next) => {
                setCode(next);
                setCodeStatus("idle");
                setError(null);
              }}
            />
          </label>
          {expired && (
            <p className="notice warning" role="status">
              Код истёк. Запросите новое письмо.
            </p>
          )}
          {Boolean(error) && (
            <div id={errorId}>
              <ErrorState error={error} />
            </div>
          )}
          <Button
            type="submit"
            busy={busy}
            disabled={code.length !== 6 || expired}
          >
            Подтвердить и продолжить
          </Button>
          <Button
            type="button"
            tone="secondary"
            disabled={busy || left > 0}
            onClick={send}
          >
            {left > 0
              ? `Повторить через ${left} сек.`
              : "Отправить код ещё раз"}
          </Button>
        </form>
      )}
    </section>
  );
}

function Account({
  session,
  refreshSession,
  botUrl,
  telegramLinkHint,
  onTelegramLinkHintConsumed,
}: {
  session: Session;
  refreshSession: () => void;
  botUrl?: string | null;
  telegramLinkHint?: { telegramId: string; expiresAt: number } | null;
  onTelegramLinkHintConsumed?: () => void;
}) {
  const botLink = safeUrl(botUrl);
  const [link, setLink] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [proof, setProof] = useState<{
    proof: string;
    expires_in: number;
    requires_merge: boolean;
  } | null>(null);
  const [telegramIdentity, setTelegramIdentity] = useState("");
  const [copied, setCopied] = useState(false);
  const [needsEmail, setNeedsEmail] = useState(false);
  const pendingIdentity = useRef("");
  const submitting = useRef(false);
  const [proofExpired, setProofExpired] = useState(false);
  useEffect(() => {
    if (
      !telegramLinkHint ||
      session.account.telegram_id ||
      Date.now() >= telegramLinkHint.expiresAt
    )
      return;
    setTelegramIdentity(telegramLinkHint.telegramId);
    setProof(null);
    setError(null);
    setCopied(false);
    setNeedsEmail(false);
    setProofExpired(false);
    setLink(true);
    onTelegramLinkHintConsumed?.();
  }, [
    telegramLinkHint,
    session.account.telegram_id,
    onTelegramLinkHintConsumed,
  ]);
  useEffect(() => {
    if (session.account.telegram_id) setLink(false);
  }, [session.account.telegram_id]);
  useEffect(() => {
    if (!link || !proof) return;
    const deadline = Date.now() + proof.expires_in * 1000;
    const timer = window.setInterval(() => {
      if (Date.now() >= deadline) {
        setProofExpired(true);
        window.clearInterval(timer);
      } else if (document.visibilityState === "visible") refreshSession();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [link, proof, refreshSession]);
  const linkCopy = {
    introduction:
      "Укажите username или ID Telegram-аккаунта, который хотите связать. Если потребуется, подтвердите почту прямо в этом окне. Затем подтвердите привязку в Telegram.",
    label: "Username или Telegram ID",
    hint: "Введите @username или числовой ID. Если бот ещё не знает ваш username, сначала откройте его и нажмите /start, затем повторите привязку.",
    format:
      "Укажите числовой ID или username из 5–32 латинских букв, цифр и знаков подчёркивания.",
  };
  async function createProof(identity: string) {
    const result = await api<NonNullable<typeof proof>>(
      "/account/link/request",
      {
        method: "POST",
        body: /^\d+$/.test(identity)
          ? { telegram_id: identity }
          : { telegram_username: identity.replace(/^@/, "") },
      },
    );
    if (
      typeof result.proof !== "string" ||
      !result.proof.trim() ||
      !Number.isFinite(result.expires_in) ||
      result.expires_in <= 0
    )
      throw new Error(
        "Не удалось получить подтверждение привязки. Попробуйте ещё раз.",
      );
    setProof(result);
  }
  function linkError(error: unknown, identity: string) {
    if (error instanceof ApiError && error.code === "fresh_login_required") {
      pendingIdentity.current = identity;
      setNeedsEmail(true);
      setError(null);
    } else setError(error);
  }
  async function continueLink(identity: string) {
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      await createProof(identity);
    } catch (e) {
      linkError(e, identity);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  async function request(event: FormEvent) {
    event.preventDefault();
    if (needsEmail) return;
    await continueLink(telegramIdentity.trim());
  }
  return (
    <div className="stack">
      <section className="card">
        <div className="card-head">
          <h2>{messages.m1ce9cb8f10}</h2>
          <span className="avatar">
            {session.account.email?.slice(0, 1).toUpperCase() || "S"}
          </span>
        </div>
        <dl className="definition">
          <dt>{messages.mac69556439}</dt>
          <dd>{session.account.id}</dd>
          <dt>Email</dt>
          <dd>{session.account.email || messages.mf66b7c67cb}</dd>
          <dt>{messages.m1a8289caa4}</dt>
          <dd>
            {session.account.email_verified
              ? messages.mf19bb8c78d
              : messages.m4d48e9d484}
          </dd>
          <dt>Telegram</dt>
          <dd>{session.account.telegram_id || messages.mf66b7c67cb}</dd>
        </dl>
      </section>
      <section className="card">
        <div className="card-head">
          <h2>
            {session.account.telegram_id
              ? "Telegram подключён"
              : messages.m0f041f83ac}
          </h2>
          <Link />
        </div>
        {session.account.telegram_id ? (
          <p>
            Аккаунт Telegram связан с этим кабинетом. Подписки и история
            доступны в обоих каналах.
          </p>
        ) : (
          <>
            <p>{messages.mae32e01555}</p>
            <Button
              tone="secondary"
              onClick={() => {
                setProof(null);
                setError(null);
                setCopied(false);
                setNeedsEmail(false);
                setProofExpired(false);
                setLink(true);
              }}
            >
              {messages.m852ae81707}
              <ArrowUpRight size={17} />
            </Button>
          </>
        )}
      </section>
      {link && (
        <Dialog title={messages.m08ca65e1e3} onClose={() => setLink(false)}>
          <div className="stack">
            <p>{linkCopy.introduction}</p>
            {!proof ? (
              <>
                <form className="form" onSubmit={request}>
                  <label>
                    {linkCopy.label}
                    <input
                      required
                      disabled={busy || needsEmail}
                      autoCapitalize="none"
                      autoCorrect="off"
                      spellCheck={false}
                      pattern="[0-9]+|@?[A-Za-z0-9_]{5,32}"
                      title={linkCopy.format}
                      aria-describedby="telegram-link-hint"
                      value={telegramIdentity}
                      onChange={(e) => setTelegramIdentity(e.target.value)}
                    />
                  </label>
                  <p id="telegram-link-hint" className="fine-print">
                    {linkCopy.hint}
                  </p>
                  {!needsEmail && (
                    <Button type="submit" busy={busy}>
                      {messages.m5fe9d1babe}
                    </Button>
                  )}
                </form>
                {needsEmail && (
                  <LinkEmailConfirmation
                    email={session.account.email}
                    onConfirmed={async () => {
                      setNeedsEmail(false);
                      await continueLink(pendingIdentity.current);
                    }}
                  />
                )}
              </>
            ) : (
              <>
                <div className="notice">
                  Отправьте команду подтверждения вашему Telegram-боту из
                  указанного аккаунта. Срок действия — {proof.expires_in}
                  {messages.m87ffe80768}
                  {proof.requires_merge ? messages.mb64ef698bd : ""}
                </div>
                <div className="row">
                  <Button
                    disabled={proofExpired}
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(
                          `/link ${proof.proof}`,
                        );
                        setCopied(true);
                      } catch (e) {
                        setError(e);
                      }
                    }}
                  >
                    <Copy size={17} />
                    {copied ? messages.m6cf3bb618c : messages.ma27fe4dd59}
                  </Button>
                  {botLink && !proofExpired && (
                    <a
                      className="button secondary"
                      href={botLink}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      Перейти в бота
                      <ArrowUpRight size={17} />
                    </a>
                  )}
                </div>
                <Button
                  tone="secondary"
                  disabled={proofExpired}
                  onClick={refreshSession}
                >
                  Проверить привязку
                </Button>
                <p className="fine-print" role="status">
                  {proofExpired
                    ? "Срок подтверждения истёк. Закройте окно и начните привязку заново."
                    : "Не передавайте команду другим людям. Подтвердите её в боте — кабинет обновится автоматически при возвращении. Выходить из аккаунта не нужно."}
                </p>
              </>
            )}
            {Boolean(error) && <ErrorState error={error} />}
          </div>
        </Dialog>
      )}
    </div>
  );
}
