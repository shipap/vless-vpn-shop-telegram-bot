export type SupportContext = {
  kind: "order" | "subscription";
  id: string;
  status: string;
};
const statuses: Record<string, string> = {
  active: "Действует",
  awaiting_payment: "Ожидает оплаты",
  paid: "Оплачено",
  paid_provisioning_pending: "Оплачено, подключаем",
  provisioning_pending: "Подключение ожидает обработки",
  paid_pending: "Оплачено, ожидает обработки",
  paid_pending_provisioning: "Оплачено, подключаем",
  pending_provisioning: "Оплачено, подключаем",
  provisioning: "Подключаем",
  pending: "Ожидает обработки",
  fulfilled: "Доступ выдан",
  completed: "Завершено",
  canceled: "Отменено",
  cancelled: "Отменено",
  failed: "Не удалось",
  expired: "Срок истёк",
  suspended: "Приостановлено",
  suspension_pending: "Приостановка ожидает проверки",
  unavailable: "Недоступно",
  review: "На проверке",
  reconciliation_required: "Требуется сверка оплаты",
  refund_review: "Возврат на проверке",
  unknown: "Не определён",
};
const reserved = new Set([
  "addemoji",
  "addlist",
  "addstickers",
  "addstyle",
  "addtheme",
  "auction",
  "auth",
  "boost",
  "call",
  "confirmphone",
  "contact",
  "giftcode",
  "invoice",
  "joinchat",
  "login",
  "m",
  "nft",
  "proxy",
  "setlanguage",
  "share",
  "socks",
  "web",
  "a",
  "k",
  "z",
  "c",
  "msg",
  "www",
]);
export function supportDraftUrl(
  value: string | null | undefined,
  context: SupportContext,
): string | null {
  if (!value || /[\u0000-\u0020\\]/.test(value)) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password) return null;
    const username = url.pathname.slice(1);
    if (
      url.hostname !== "t.me" ||
      url.port ||
      url.search ||
      url.hash ||
      !/^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(username) ||
      reserved.has(username.toLowerCase()) ||
      username.toLowerCase().endsWith("bot") ||
      !["order", "subscription"].includes(context.kind) ||
      !/^[A-Za-z0-9_-]{1,64}$/.test(context.id)
    )
      return value;
    const label = context.kind === "order" ? "заказу" : "подписке";
    const state =
      typeof context.status === "string" &&
      Object.hasOwn(statuses, context.status)
        ? statuses[context.status]
        : "Не определён";
    url.searchParams.set(
      "text",
      `Нужна помощь по ${label}.\nID: ${context.id}\nСтатус: ${state}`,
    );
    return url.href;
  } catch {
    return null;
  }
}
