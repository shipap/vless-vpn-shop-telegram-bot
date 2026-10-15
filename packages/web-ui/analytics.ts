import { messages } from "./messages";
type Row = Record<string, unknown>;
export function factRows(
  data: Row | null,
  key: string,
  currency?: string,
): Row[] | null {
  if (!Array.isArray(data?.[key])) return null;
  return (data[key] as Row[]).filter(
    (row) => !currency || row.currency === currency,
  );
}
export function analyticsView(data: Row | null, currency: string) {
  const currencies = (data?.currencies || {}) as Row;
  const refunds = (data?.refunds || {}) as Row;
  const fees = (data?.confirmed_fees || {}) as Row;
  const totals: Row = {
    external_payments:
      currencies[currency] ?? (data?.paid_count === 0 ? "0.00" : null),
    refunds: refunds[currency] ?? (data?.paid_count === 0 ? "0.00" : null),
    known_fees: fees[currency] ?? null,
    active_subscriptions: data?.active_paid_subscriptions ?? null,
    net_after_refunds:
      ((data?.net_after_refunds || {}) as Row)[currency] ?? null,
    net_usdt: data?.net_usdt ?? null,
    previous: ((data?.previous || {}) as Row)[currency] ?? null,
  };
  const series: Row[] = (
    Array.isArray(data?.series) ? (data.series as Row[]) : []
  )
    .filter((row) => row.currency === currency)
    .map((row) => ({
      ...row,
      payments: row.payments ?? row.amount ?? null,
      refunds: row.refunds ?? null,
      fees: row.fees ?? null,
    }));
  const rows = (Array.isArray(data?.rows) ? (data.rows as Row[]) : []).filter(
    (row) => row.currency === currency,
  );
  return { totals, series, rows };
}
export function exportRows(rows: Row[]) {
  const fields = [
    "id",
    "payment_id",
    "order_id",
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
    "original_account_id",
    "provenance",
    "time_source",
  ];
  const cell = (value: unknown) => {
    let text = value == null ? messages.m6d8e839d9f : String(value);
    if (/^[=+@\t\r]|^-(?!\d+(?:\.\d+)?$)/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  return (
    "\uFEFF" +
    [
      [
        "ID",
        "Платёж",
        "Заказ",
        messages.m6611d56d61,
        "Событие",
        messages.m785a0b151f,
        messages.m6aa14ce33b,
        messages.m1a1bcc06f6,
        "Валюта комиссии",
        "Чистое зачисление USDT",
        "Канал",
        "Тариф",
        "Текущий аккаунт",
        "Исходный аккаунт",
        "Источник факта",
        "Источник даты",
      ],
      ...rows.map((row) => fields.map((key) => row[key])),
    ]
      .map((row) => row.map(cell).join(";"))
      .join("\r\n")
  );
}
