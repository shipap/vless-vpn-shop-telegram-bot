import { expect, test } from "@playwright/test";
import { syntheticInstance } from "./instance-fixture";

test("subscription and saved-order support stay safe, published FAQ navigates and linking notice opens protected unlink", async ({
  page,
}) => {
  const writes: string[] = [];
  const account = {
    id: "9007199254740993123",
    email: "synthetic@example.test",
    email_verified: true,
    telegram_id: "700000001",
  };
  await page.route("https://telegram.org/**", (route) =>
    route.fulfill({ body: "", contentType: "application/javascript" }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account,
        csrf_token: "synthetic",
        is_admin: false,
        mfa_verified: false,
        capabilities: [],
      };
    if (path === "/public-config")
      json = {
        instance: {
          ...syntheticInstance,
          support_url: "https://t.me/synthetic_support",
        },
        local_test: true,
      };
    if (path === "/catalog")
      json = {
        products: [],
        periods: [],
        content: {
          help_center: {
            custom_faq: [
              {
                id: "help_payment_access",
                question: "Деньги списались, но доступа нет",
                answer: [
                  {
                    type: "paragraph",
                    children: [
                      {
                        type: "text",
                        text: "Проверьте статус заказа. Не оплачивайте повторно.",
                      },
                    ],
                  },
                ],
              },
            ],
          },
        },
        legal: { configured: false, documents: [] },
      };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [
          {
            id: "subscription-safe",
            product_id: "synthetic",
            name: "Моя подписка",
            status: "active",
            expires_at: "2027-01-01T00:00:00Z",
            connection_url: "https://vpn.example.test/SENSITIVE_CONNECTION",
            devices: [],
            devices_limit: 2,
            segments: [],
            recurring: null,
          },
        ],
        payments: [
          {
            id: "PROVIDER_SENSITIVE",
            order_id: "order-safe",
            product_name: "Моя подписка",
            amount: "125.00",
            currency: "RUB",
            status: "paid",
            paid_at: "2026-10-09T08:00:00Z",
          },
        ],
        bonus_balance: "0",
        credit_balance: "0",
        credit_available: "0",
        recurring_enrollment_enabled: false,
      };
    if (path === "/legal" || path === "/legal/status")
      json = { configured: false, accepted: true, missing: [], documents: [] };
    if (path === "/notifications")
      json = {
        items: [
          {
            id: "identity-event",
            kind: "identity_linked",
            title: "Telegram привязан",
            body: "Если это были не вы, проверьте привязку.",
            severity: "info",
            created_at: "2026-10-09T08:00:00Z",
            read_at: null,
            target: "/#account",
          },
        ],
        unread_count: 1,
      };
    if (path === "/notifications/stream")
      return route.fulfill({ contentType: "text/event-stream", body: "" });
    if (route.request().method() !== "GET") writes.push(path);
    await route.fulfill({ json });
  });
  await page.goto("/");
  const subscription = page.getByRole("link", {
    name: "Поддержка по подписке",
    exact: true,
  });
  await expect(subscription).toBeVisible();
  const subscriptionDraft = new URL(
    (await subscription.getAttribute("href"))!,
  ).searchParams.get("text");
  expect(subscriptionDraft).toContain("subscription-safe");
  expect(subscriptionDraft).not.toContain("SENSITIVE_CONNECTION");
  await page.goto("/#help");
  await page
    .getByText("Деньги списались, но доступа нет", { exact: true })
    .click();
  await expect(
    page.getByText("Проверьте статус заказа. Не оплачивайте повторно.", {
      exact: true,
    }),
  ).toBeVisible();
  await page
    .getByRole("link", { name: "Открыть платежи", exact: true })
    .click();
  const order = page.getByRole("link", {
    name: "Поддержка по заказу",
    exact: true,
  });
  await expect(order).toBeVisible();
  const orderDraft = new URL(
    (await order.getAttribute("href"))!,
  ).searchParams.get("text");
  expect(orderDraft).toContain("order-safe");
  expect(orderDraft).not.toContain("PROVIDER_SENSITIVE");
  expect(orderDraft).not.toContain("125.00");
  await page.getByRole("button", { name: /^Уведомления,/ }).click();
  const notice = page.getByRole("dialog", { name: "Уведомления", exact: true });
  const unlink = notice.getByRole("link", {
    name: "Отвязать Telegram",
    exact: true,
  });
  await expect(unlink).toHaveAttribute("href", "/#account");
  await unlink.click();
  await expect(
    page.getByRole("button", { name: "Отвязать Telegram", exact: true }),
  ).toBeVisible();
  expect(writes).toEqual([]);
});
