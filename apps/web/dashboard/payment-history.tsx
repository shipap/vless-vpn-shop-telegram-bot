import { useState } from "react";
import type { Dashboard } from "@api/types";
import { Badge, Button, Dialog, Empty } from "@ui/components";
import { date, money } from "@ui/content";
import { messages } from "@ui/messages";
import { SupportLink } from "./SupportLink";

type Payment = Dashboard["payments"][number];
const actionName = (action: string | null | undefined) =>
  ({
    purchase: "Покупка",
    renew: "Продление",
    upgrade: "Повышение тарифа",
    downgrade: "Понижение тарифа",
    devices: "Дополнительные устройства",
    promo_claim: "Промокод",
  })[action ?? ""] ?? null;
export function PaymentHistory({
  payments,
  supportUrl,
}: {
  payments: Payment[];
  supportUrl?: string | null;
}) {
  const [selected, setSelected] = useState<Payment | null>(null);
  return (
    <section className="stack payment-history">
      <div className="notice">{messages.mb92ae8920a}</div>
      {!payments.length ? (
        <Empty title={messages.me6f75b666d}>
          <p>{messages.mb30de2863d}</p>
        </Empty>
      ) : (
        <div className="payment-history-list">
          {payments.map((payment) => (
            <article className="card payment-history-item" key={payment.id}>
              <div className="payment-history-heading">
                <div>
                  <h2>{payment.product_name || "Оплата подписки"}</h2>
                  <p className="fine-print">
                    {date(payment.paid_at, true)} · МСК
                  </p>
                </div>
                <strong>{money(payment.amount, payment.currency)}</strong>
              </div>
              {(actionName(payment.action) ||
                payment.period_days != null ||
                payment.devices != null) && (
                <p className="fine-print">
                  {[
                    actionName(payment.action),
                    payment.period_days != null
                      ? `${payment.period_days} дней`
                      : null,
                    payment.devices != null
                      ? `${payment.devices} устройства`
                      : null,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              )}
              <div className="payment-history-actions">
                <Badge value={payment.status} />
                <Button tone="secondary" onClick={() => setSelected(payment)}>
                  Подробнее о платеже
                </Button>
                <SupportLink
                  url={supportUrl}
                  context={{
                    kind: "order",
                    id: payment.order_id,
                    status: payment.status,
                  }}
                />
              </div>
            </article>
          ))}
        </div>
      )}
      {selected && (
        <Dialog title="Подробности платежа" onClose={() => setSelected(null)}>
          <div className="stack">
            {!selected.product_name && (
              <p className="fine-print">
                Тариф не указан в старой записи. Исторические условия не
                заменяются текущим каталогом.
              </p>
            )}
            <dl className="definition">
              <dt>Тариф на момент оплаты</dt>
              <dd>{selected.product_name || "Не указан в старой записи"}</dd>
              <dt>Действие</dt>
              <dd>
                {actionName(selected.action) || "Не указано в старой записи"}
              </dd>
              <dt>Оплаченный период</dt>
              <dd>
                {selected.period_days == null
                  ? "Не указан в старой записи"
                  : `${selected.period_days} дней`}
              </dd>
              <dt>Устройства</dt>
              <dd>{selected.devices ?? "Не указаны в старой записи"}</dd>
              <dt>Способ оплаты</dt>
              <dd>
                {{
                  external: "Внешняя оплата",
                  rollypay: "Внешняя оплата",
                  bonus: "Бонусы",
                  credit: "Денежный баланс",
                  mixed: "Смешанная оплата",
                  recurring: "Автооплата",
                }[selected.payment_method ?? ""] || "Не указан в старой записи"}
              </dd>
              <dt>Подключение</dt>
              <dd>
                {selected.provisioning_status ? (
                  <Badge value={selected.provisioning_status} />
                ) : (
                  "Сведения не сохранены"
                )}
              </dd>
              <dt>Сумма</dt>
              <dd>{money(selected.amount, selected.currency)}</dd>
              <dt>Комиссия</dt>
              <dd>
                {selected.fee == null
                  ? "Комиссия неизвестна"
                  : selected.fee_currency
                    ? money(selected.fee, selected.fee_currency)
                    : "Валюта комиссии неизвестна"}
              </dd>
              <dt>Дата оплаты · МСК</dt>
              <dd>{date(selected.paid_at, true)}</dd>
              <dt>Статус</dt>
              <dd>
                <Badge value={selected.status} />
              </dd>
              <dt>Заказ</dt>
              <dd>
                <code>{selected.order_id}</code>
              </dd>
              <dt>Платёж</dt>
              <dd>
                <code>{selected.id}</code>
              </dd>
              {selected.environment === "local_test" && (
                <>
                  <dt>Среда</dt>
                  <dd>
                    <Badge value="local_test" />
                  </dd>
                </>
              )}
            </dl>
          </div>
        </Dialog>
      )}
    </section>
  );
}
