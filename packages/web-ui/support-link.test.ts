import { expect, it } from "vitest";
import { supportDraftUrl } from "./support-link";

it("prepares personal Telegram support draft with exact opaque ID and status only", () => {
  const href = supportDraftUrl("https://t.me/synthetic_support", {
    kind: "order",
    id: "9007199254740993123",
    status: "paid_provisioning_pending",
  });
  const url = new URL(href!);
  expect(url.origin + url.pathname).toBe("https://t.me/synthetic_support");
  expect(url.searchParams.get("text")).toBe(
    "Нужна помощь по заказу.\nID: 9007199254740993123\nСтатус: Оплачено, подключаем",
  );
});

it("never embeds unvalidated IDs or statuses and preserves other safe configured support destinations", () => {
  expect(
    supportDraftUrl("https://t.me/synthetic_support", {
      kind: "subscription",
      id: "vpn://secret",
      status: "active",
    }),
  ).toBe("https://t.me/synthetic_support");
  const href = supportDraftUrl("https://t.me/synthetic_support", {
    kind: "subscription",
    id: "synthetic-sub",
    status: "SECRET_NEWLINE\nhttps://vpn.secret",
  });
  expect(new URL(href!).searchParams.get("text")).toBe(
    "Нужна помощь по подписке.\nID: synthetic-sub\nСтатус: Не определён",
  );
  for (const url of [
    "https://support.example.test/form",
    "https://t.me/share/url?url=https://example.test",
    "https://t.me/+invite",
    "https://t.me/m/business",
    "https://t.me/synthetic_bot",
    "https://t.me/synthetic_support?start=link",
    "https://t.me/addstickers",
    "https://t.me/addemoji",
    "https://t.me/boost",
    "https://t.me/invoice",
  ]) {
    expect(
      supportDraftUrl(url, { kind: "order", id: "o1", status: "paid" }),
    ).toBe(url);
  }
  for (const url of [
    "javascript:alert(1)",
    "https://user:secret@t.me/synthetic_support",
    "//t.me/synthetic_support",
  ]) {
    expect(
      supportDraftUrl(url, { kind: "order", id: "o1", status: "paid" }),
    ).toBeNull();
  }
});

it.each([
  ["active", "Действует"],
  ["paid", "Оплачено"],
  ["reconciliation_required", "Требуется сверка оплаты"],
  ["refund_review", "Возврат на проверке"],
  ["unknown", "Не определён"],
])("uses Russian label for safe state %s", (status, label) => {
  const href = supportDraftUrl("https://t.me/synthetic_support", {
    kind: "order",
    id: "o1",
    status,
  });
  expect(new URL(href!).searchParams.get("text")).toBe(
    `Нужна помощь по заказу.\nID: o1\nСтатус: ${label}`,
  );
});
