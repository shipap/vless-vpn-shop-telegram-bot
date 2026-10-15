import type { ReactNode } from "react";
import { SemanticContent, date, money, type ContentNode } from "./content";
import { fieldLabel, statusLabel } from "./ru";

const values: Record<string, string> = {
  context: "Контекст входа",
  account_email: "Аккаунт и Email",
  ip_login_failures: "Слишком много неверных кодов с IP",
  route_scanning: "Сканирование неизвестных маршрутов",
  route_scan: "Сканирование неизвестных маршрутов",
  mfa_failures: "Ошибки подтверждения входа владельца",
  wrong_code: "Неверный код",
  verified_code: "Код подтверждён",
  code_invalid: "Код неверен или истёк",
  login_blocked: "Вход временно ограничен",
  email_request: "Запрошен код входа",
  resend_cooldown: "Слишком ранний повтор отправки кода",
  success: "Успешный вход",
  failure: "Неудачная попытка",
  direct: "Прямое соединение",
  owner_adjustment: "Изменение владельцем",
  refund_compensation: "Компенсация возврата",
  verified_legacy: "Проверенный исторический остаток",
  unverified: "Требует проверки",
  manual_review: "Ручная проверка",
  automatic_revocation: "Отзыв возвращённой покупки",
  dispute_freeze: "Операции по спорной подписке приостановлены",
  refund_entitlement_decision: "Решение по доступу при возврате",
  partial_refund: "Частичный возврат требует ручной проверки",
  attributable_usage_unavailable:
    "Нет достоверного использования конкретной покупки",
  usage_source_unverified: "Источник использования не подтверждён",
  usage_entitlement_mismatch:
    "Использование не связано с этим оплаченным периодом",
  usage_time_unverified: "Время использования не подтверждено",
  usage_stale: "Нужны свежие сведения об использовании",
  entitlement_not_started: "Оплаченный период ещё не начался",
  traffic_unverified: "Объём использования не подтверждён",
  partly_consumed_entitlement:
    "Превышен срок или объём для автоматического отзыва",
  full_refund_nearly_unused:
    "Полный возврат в пределах подтверждённых ограничений",
  provider_dispute: "Спорный платёж требует проверки",
  provider_chargeback: "Спор инициирован платёжным сервисом",
  paid_entitlement_mapping_unverified:
    "Связь с оплаченными правами требует проверки",
  verified_panel_attestation:
    "Доверенная серверная проверка конкретного периода",
  paid_basis_missing_or_changed:
    "Оплаченная ценовая база отсутствует или изменена; требуется ручная сверка",
  immutable_paid_quote: "Сохранённый расчёт оплаченного заказа",
  resolved: "Спор разрешён",
  ACTIVE: "Доступ разрешён",
  DISABLED: "Доступ приостановлен",
  panel_unavailable: "Панель временно недоступна",
  panel_target_not_observed: "Целевое состояние пока не подтверждено панелью",
  account: "Аккаунт",
  ip: "IP-адрес",
  challenge: "Проверка входа",
  provider_confirmed: "Подтверждено платёжным сервисом",
  configured_estimate: "Настроенная оценка",
  unknown: "Неизвестно",
  local: "Локальная среда",
  email: "Email",
  telegram: "Telegram",
  web: "Сайт",
  bot: "Бот",
  mock: "Локальная проверка",
  all: "Все пользователи",
  first_purchase: "Первая покупка",
  renewal: "Продление",
  ready: "Готово",
  healthy: "Работает",
  ok: "Работает",
  not_verified: "Не проверено",
  blocked: "Заблокировано",
  restored: "Восстановлено",
  verified: "Проверено",
  local_test: "Локальная проверка",
  observed: "Проверено",
  not_tested: "Пока не проверено",
  application: "Приложение",
  received_at: "Дата получения события",
  fulfilled_at: "Дата фактической выдачи услуги",
  legacy_paid_date: "Историческая дата оплаты",
  legacy_order_created_at: "Историческая дата заказа",
  synthetic_demo: "Синтетический стенд",
  capability: "Возможности Telegram",
  local_file: "Локальный тестовый файл",
  provision: "Выдача доступа",
  notification: "Уведомление",
  cancel_agreement: "Остановка автопродления",
  broadcast: "Рассылка",
};
export function AdminValue({
  value,
  name = "",
  currency = "RUB",
}: {
  value: unknown;
  name?: string;
  currency?: string;
}): ReactNode {
  if (value == null) return <span className="muted">Нет данных</span>;
  if (typeof value === "boolean") return value ? "Да" : "Нет";
  if (Array.isArray(value)) {
    if (!value.length) return <span className="muted">Нет записей</span>;
    if (
      name === "nodes" ||
      value.every(
        (node) => typeof node === "object" && node !== null && "type" in node,
      )
    )
      return <SemanticContent nodes={value as ContentNode[]} />;
    return (
      <ul className="admin-records">
        {value.map((item, index) => (
          <li key={index}>
            <AdminValue value={item} currency={currency} />
          </li>
        ))}
      </ul>
    );
  }
  if (typeof value === "object")
    return (
      <dl className="detail-list">
        {Object.entries(value).map(([key, item]) => (
          <div key={key}>
            <dt>{fieldLabel(key)}</dt>
            <dd>
              <AdminValue
                value={item}
                name={key}
                currency={
                  ["fee", "provider_fee"].includes(key)
                    ? String(
                        (value as Record<string, unknown>).fee_currency ||
                          (value as Record<string, unknown>).currency ||
                          currency,
                      )
                    : key === "external_refundable_amount"
                      ? String(
                          (value as Record<string, unknown>)
                            .external_currency || currency,
                        )
                      : key === "net_usdt"
                        ? "USDT"
                        : String(
                            (value as Record<string, unknown>).currency ||
                              currency,
                          )
                }
              />
            </dd>
          </div>
        ))}
      </dl>
    );
  if (/(_at|_date)$/.test(name)) return date(String(value), true);
  if (
    [
      "price",
      "amount",
      "total",
      "fee",
      "provider_fee",
      "external_refundable_amount",
      "remaining_service_value",
      "bonus_balance",
      "extra_device_price",
      "service_value",
      "balance",
      "net_usdt",
      "external",
      "discounted",
      "base",
      "bonus",
    ].includes(name)
  )
    return money(String(value), currency);
  if (values[String(value)]) return values[String(value)];
  if (["status", "state", "environment"].includes(name))
    return statusLabel(String(value));
  return String(value);
}
