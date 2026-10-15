import { expect, test, type Page } from "@playwright/test";
import { syntheticInstance } from "./instance-fixture";

async function fixture(
  page: Page,
  state = "stale",
  overrides: Record<string, unknown> = {},
) {
  const requests: string[] = [];
  let orderState = "awaiting_payment";
  let ready = false;
  let review = false;
  let unavailable = false;
  const account = {
    id: "9007199254740993123",
    email: "synthetic@example.test",
    email_verified: true,
    telegram_id: null,
  };
  const legal = {
    configured: true,
    accepted: true,
    revision: "synthetic",
    missing: [],
    documents: [
      {
        document_type: "offer",
        version: "1",
        url: "https://example.test/offer",
      },
    ],
  };
  const period = { days: 30, price: "125.00" };
  const subscription = {
    id: "subscription-safe",
    product_id: "synthetic",
    name: "Моя подписка",
    status: "active",
    expires_at: "2027-01-01T00:00:00Z",
    connection_url: "https://vpn.example.test/synthetic",
    devices: [],
    devices_limit: 5,
    segments: [],
    recurring: null,
    observed_at: state === "unknown" ? null : "2026-10-09T06:00:00Z",
    device_inventory_state: state,
    ...overrides,
  };
  const order = () => ({
    id: "order-safe",
    quote_id: "quote-safe",
    status: orderState,
    total: "125.00",
    currency: "RUB",
    environment: "sandbox",
    subscription_id: "subscription-safe",
    checkout_url:
      orderState === "awaiting_payment" ? "https://example.test/pay" : null,
    provisioning_status:
      orderState === "paid"
        ? ready
          ? "ready"
          : review
            ? "review"
            : "pending"
        : null,
  });
  await page.route("https://telegram.org/**", (route) =>
    route.fulfill({ body: "", contentType: "application/javascript" }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const method = route.request().method();
    requests.push(`${method} ${path}`);
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
      json = { instance: syntheticInstance, local_test: true };
    if (path === "/catalog")
      json = {
        products: [
          {
            id: "synthetic",
            name: "Тестовый тариф",
            available: true,
            tier_level: 1,
            currency: "RUB",
            periods: [period],
            included_devices: 5,
            presentation: {},
          },
        ],
        periods: [period],
        content: {},
        legal,
      };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [
          orderState === "paid"
            ? {
                ...subscription,
                status: ready ? "active" : "provisioning_pending",
                connection_url: ready ? subscription.connection_url : null,
              }
            : subscription,
        ],
        payments: [],
        bonus_balance: "0",
        credit_balance: "0",
        credit_available: "0",
        recurring_enrollment_enabled: false,
      };
    if (
      path === "/legal" ||
      path === "/legal/status" ||
      path === "/legal/accept"
    )
      json = legal;
    if (path === "/quotes")
      json = {
        id: "quote-safe",
        product_id: "synthetic",
        total: "125.00",
        currency: "RUB",
        base_amount: "125.00",
        discount_amount: "0",
        bonus_amount: "0",
        credit_amount: "0",
        devices_limit: 5,
        resulting_expires_at: "2027-01-01T00:00:00Z",
        expires_at: "2027-01-01T00:00:00Z",
        breakdown: [],
      };
    if (path === "/orders" || path === "/orders/order-safe") {
      if (method === "GET" && unavailable)
        return route.fulfill({
          status: 503,
          json: { detail: "synthetic_unavailable" },
        });
      json = order();
    }
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/notifications/stream")
      return route.fulfill({ contentType: "text/event-stream", body: "" });
    if (method !== "GET")
      expect(["/legal/accept", "/quotes", "/orders"]).toContain(path);
    await route.fulfill({ json });
  });
  return {
    requests,
    paid: () => {
      orderState = "paid";
    },
    ready: () => {
      ready = true;
    },
    review: () => {
      review = true;
    },
    fail: () => {
      unavailable = true;
    },
    recover: () => {
      unavailable = false;
    },
  };
}

test("cached panel data is clearly identified without changing saved rights", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  const card = page.locator(".subscription-card");
  await expect(card).toContainText("Показаны последние сохранённые данные");
  await expect(card).toContainText("9 октября 2026 г. в 09:00 · МСК");
  await expect(card).toContainText("5");
  await expect(
    card.getByRole("button", { name: "Подключить VPN", exact: true }),
  ).toBeVisible();
});

test("an unknown demo observation adds no freshness warning", async ({
  page,
}) => {
  await fixture(page, "unknown");
  await page.goto("/");
  await expect(page.locator(".subscription-card")).toBeVisible();
  await expect(
    page.getByText("Показаны последние сохранённые данные", { exact: false }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Данные обновлены", { exact: false }),
  ).toHaveCount(0);
});

test("pending panel changes preserve future paid rights and an actual unlimited limit", async ({
  page,
}) => {
  await fixture(page, "fresh", {
    observed_at: new Date().toISOString(),
    panel_sync_state: "pending",
    panel_sync_error: "synthetic_private_error",
    paid_horizon: "2027-02-01T00:00:00Z",
    panel_device_limit: 0,
  });
  await page.goto("/");
  const card = page.locator(".subscription-card");
  await expect(card).toContainText("Изменения ещё применяются");
  await expect(card).toContainText("В истории оплачено до 1 февраля 2027 г.");
  await expect(card).toContainText("Без ограничения");
  await expect(card).not.toContainText("synthetic_private_error");
});

async function checkout(page: Page) {
  await page.goto("/");
  await page
    .getByRole("button", { name: "Добавить подписку", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Новая подписка",
    exact: true,
  });
  await dialog
    .getByRole("checkbox", { name: "Принимаю оферту", exact: false })
    .check();
  await dialog
    .getByRole("button", { name: "Рассчитать стоимость", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "Подтвердить заказ", exact: true })
    .click();
  await expect(
    dialog.getByRole("link", { name: "Перейти к оплате" }),
  ).toBeVisible();
  return dialog;
}

test("payment and provisioning update automatically without another payment operation", async ({
  page,
}) => {
  const api = await fixture(page);
  const dialog = await checkout(page);
  api.paid();
  await expect(
    dialog.getByText("Оплата подтверждена", { exact: false }),
  ).toBeVisible();
  await expect(dialog).toContainText("Подготовка подключения продолжается");
  const dashboardCount = api.requests.filter(
    (x) => x === "GET /dashboard",
  ).length;
  await page.waitForTimeout(4500);
  expect(api.requests.filter((x) => x === "GET /dashboard")).toHaveLength(
    dashboardCount,
  );
  await expect(
    dialog.getByRole("button", { name: "Проверить статус", exact: true }),
  ).toHaveCount(0);
  await expect(
    dialog.getByRole("link", { name: "Перейти к оплате" }),
  ).toHaveCount(0);
  api.ready();
  await expect(dialog).toContainText("Доступ готов");
  await expect(
    page.locator(".subscription-card button.connect-button"),
  ).toContainText("Подключить VPN");
  await expect
    .poll(() => api.requests.filter((x) => x === "GET /dashboard").length)
    .toBeGreaterThanOrEqual(3);
  const readyCount = api.requests.filter(
    (x) => x === "GET /orders/order-safe",
  ).length;
  await page.waitForTimeout(4500);
  expect(
    api.requests.filter((x) => x === "GET /orders/order-safe"),
  ).toHaveLength(readyCount);
  await dialog.getByRole("button", { name: "Закрыть", exact: true }).click();
  await expect(
    page
      .locator(".subscription-card")
      .getByRole("button", { name: "Подключить VPN", exact: true }),
  ).toBeVisible();
  expect(api.requests.filter((x) => x.startsWith("POST"))).toEqual([
    "POST /legal/accept",
    "POST /quotes",
    "POST /orders",
  ]);
  const count = api.requests.filter(
    (x) => x === "GET /orders/order-safe",
  ).length;
  await page.waitForTimeout(4500);
  expect(
    api.requests.filter((x) => x === "GET /orders/order-safe"),
  ).toHaveLength(count);
});

test("unchanged paid state does not repeatedly refresh the panel dashboard", async ({
  page,
}) => {
  const api = await fixture(page);
  const dialog = await checkout(page);
  api.paid();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(
    dialog.getByText("Оплата подтверждена", { exact: false }),
  ).toBeVisible();
  await page.waitForTimeout(500);
  const count = api.requests.filter((x) => x === "GET /dashboard").length;
  await page.waitForTimeout(4500);
  expect(api.requests.filter((x) => x === "GET /dashboard")).toHaveLength(
    count,
  );
});

test("confirmed ready provisioning stops own order reads while the dialog remains open", async ({
  page,
}) => {
  const api = await fixture(page);
  const dialog = await checkout(page);
  api.paid();
  api.ready();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(
    dialog.getByText("Оплата подтверждена", { exact: false }),
  ).toBeVisible();
  await page.waitForTimeout(500);
  const count = api.requests.filter(
    (x) => x === "GET /orders/order-safe",
  ).length;
  await page.waitForTimeout(4500);
  expect(
    api.requests.filter((x) => x === "GET /orders/order-safe"),
  ).toHaveLength(count);
});

test("a reviewed issuance only refreshes on focus and can recover to ready", async ({
  page,
}) => {
  const api = await fixture(page);
  const dialog = await checkout(page);
  api.paid();
  api.review();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(dialog).toContainText("Выдача доступа требует проверки");
  await page.waitForTimeout(500);
  const count = api.requests.filter(
    (x) => x === "GET /orders/order-safe",
  ).length;
  await page.waitForTimeout(4500);
  expect(
    api.requests.filter((x) => x === "GET /orders/order-safe"),
  ).toHaveLength(count);
  api.ready();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(dialog).toContainText("Доступ готов");
  expect(api.requests.filter((x) => x.startsWith("POST"))).toHaveLength(3);
});

test("focus recovers a failed read without a payment mutation or error dialog", async ({
  page,
}) => {
  const api = await fixture(page);
  const dialog = await checkout(page);
  api.fail();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect
    .poll(
      () => api.requests.filter((x) => x === "GET /orders/order-safe").length,
    )
    .toBeGreaterThan(0);
  await expect(
    dialog.getByText("Последний подтверждённый статус сохранён", {
      exact: false,
    }),
  ).toBeVisible();
  api.recover();
  api.paid();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(
    dialog.getByText("Оплата подтверждена", { exact: false }),
  ).toBeVisible();
  expect(api.requests.filter((x) => x.startsWith("POST"))).toHaveLength(3);
});

test("automatic reads pause on a hidden page and stop after their bounded window", async ({
  page,
}) => {
  const api = await fixture(page);
  const dialog = await checkout(page);
  await expect
    .poll(
      () => api.requests.filter((x) => x === "GET /orders/order-safe").length,
    )
    .toBeGreaterThan(0);
  await page.clock.install();
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const count = api.requests.filter(
    (x) => x === "GET /orders/order-safe",
  ).length;
  await page.clock.fastForward(8000);
  expect(
    api.requests.filter((x) => x === "GET /orders/order-safe"),
  ).toHaveLength(count);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect
    .poll(
      () => api.requests.filter((x) => x === "GET /orders/order-safe").length,
    )
    .toBeGreaterThan(count);
  await page.clock.fastForward(610000);
  await expect(
    dialog.getByText("Автоматическая проверка приостановлена", {
      exact: false,
    }),
  ).toBeVisible();
  const boundedCount = api.requests.filter(
    (x) => x === "GET /orders/order-safe",
  ).length;
  await page.clock.fastForward(60000);
  expect(
    api.requests.filter((x) => x === "GET /orders/order-safe"),
  ).toHaveLength(boundedCount);
  expect(api.requests.filter((x) => x.startsWith("POST"))).toHaveLength(3);
});
