import { expect, test } from "@playwright/test";
import { syntheticInstance } from "./instance-fixture";
test.beforeEach(async ({ page }) => {
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}};",
    }),
  );
});

test("comparison uses current catalog periods and devices without exposing hidden landing products", async ({
  page,
}) => {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const products = [1, 2, 3].map((i) => ({
      id: String(i),
      code: `tier${i}`,
      name: `Тариф ${i}`,
      description: `Описание ${i}`,
      price: String(i * 100),
      currency: "RUB",
      included_devices: i + 1,
      version: 1,
      available: true,
      extra_device_price: i === 1 ? null : "50",
      content: {},
      semantic_role: "vpn",
      visible_landing: i !== 3,
      visible_bot: true,
      sort_order: i,
      highlighted: false,
      icon: null,
      base_period_days: 30,
      periods: [
        { days: 30, months: 1, discount_percent: "0", price: String(i * 100) },
        { days: 90, months: 3, discount_percent: "10", price: String(i * 270) },
      ],
      eligibility: { eligible: true, reason: null },
    }));
    await route.fulfill({
      json: path.endsWith("/catalog")
        ? {
            products,
            periods: [],
            content: {},
            legal: { configured: false, documents: [] },
          }
        : {
            instance: syntheticInstance,
            dashboard_url: "http://127.0.0.1:3200",
            local_test: true,
          },
    });
  });
  for (const width of [320, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("http://127.0.0.1:3100/#tariffs");
    await page.reload();
    await page.getByText("Сравнить тарифы", { exact: true }).click();
    const comparison = page.getByRole("table", { name: "Сравнение тарифов" });
    await expect(comparison).toContainText("Тариф 1");
    await expect(comparison).toContainText("Тариф 2");
    await expect(comparison).not.toContainText("Тариф 3");
    await expect(
      comparison.getByRole("row", { name: /Устройств включено/ }),
    ).toContainText("2");
    await expect(
      comparison.getByRole("row", { name: /90 дней/ }),
    ).toContainText("540 ₽");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
});

test("panel inventory distinguishes unknown traffic and confirms explicit devices with stable operation identity", async ({
  page,
}) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  const account = {
    id: "synthetic-account",
    email: "synthetic@example.test",
    email_verified: true,
    telegram_id: null,
    unlink_blocked: false,
  };
  const requests: unknown[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    let json: unknown = {};
    if (path === "/legal/status")
      json = { accepted: true, configured: false, documents: [], missing: [] };
    if (path === "/auth/me")
      json = {
        account,
        csrf_token: "synthetic",
        is_admin: false,
        mfa_verified: false,
        staff_role: null,
        capabilities: [],
      };
    if (path === "/catalog")
      json = {
        products: [],
        periods: [],
        content: {},
        legal: { configured: false, documents: [] },
      };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [
          {
            id: "synthetic-sub",
            name: "Мой VPN",
            product_id: "synthetic-product",
            status: "active",
            expires_at: "2099-01-01T00:00:00Z",
            devices_limit: 4,
            version: 1,
            connection_url: null,
            devices: [],
            segments: [],
            recurring: null,
          },
        ],
        payments: [],
        bonus_balance: "0",
        bonus_reserved: "0",
        credit_balance: "0",
        credit_available: "0",
        credit_reserved: "0",
      };
    if (path === "/subscriptions/synthetic-sub/inventory")
      json = {
        subscription_id: "synthetic-sub",
        source: "remnawave",
        status: "DISABLED",
        observed_at: "2026-10-08T00:00:00Z",
        error: null,
        traffic_bytes: null,
        traffic_limit_bytes: null,
        devices: [
          {
            id: "synthetic-device-1",
            name: "Телефон",
            platform: "android",
            generation: 1,
            revoked: false,
            observed_at: "2026-10-08T00:00:00Z",
          },
          {
            id: "synthetic-device-2",
            name: "Ноутбук",
            platform: "windows",
            generation: 1,
            revoked: false,
            observed_at: "2026-10-08T00:00:00Z",
          },
        ],
        paid_expires_at: "2099-01-01T00:00:00Z",
      };
    if (path === "/subscriptions/synthetic-sub/devices/revoke-bulk") {
      requests.push(route.request().postDataJSON());
      json = {
        status: "revoke_pending",
        device_ids: ["synthetic-device-1", "synthetic-device-2"],
        job_ids: ["synthetic-job-1", "synthetic-job-2"],
      };
    }
    await route.fulfill({ json });
  });
  await page.setViewportSize({ width: 320, height: 900 });
  await page.goto("/#devices");
  await expect(page.locator("main")).toContainText("Трафик пока неизвестен");
  await expect(page.locator("main")).toContainText("Оплаченный доступ");
  await expect(page.locator("main")).toContainText("Телефон");
  await page.getByRole("button", { name: "Отвязать все устройства" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("2 устройства");
  await expect(dialog).toContainText("каждое устройство отдельно");
  await dialog.getByRole("button", { name: "Подтвердить отвязку" }).click();
  await expect(page.locator("main")).toContainText(
    "Отвязка поставлена в очередь",
  );
  expect(requests).toHaveLength(1);
  expect(pageErrors).toEqual([]);
  expect(requests[0]).toMatchObject({
    device_ids: ["synthetic-device-1", "synthetic-device-2"],
    expected_generations: { "synthetic-device-1": 1, "synthetic-device-2": 1 },
    confirmed: true,
  });
  expect((requests[0] as { idempotency_key: string }).idempotency_key).toMatch(
    /^[0-9a-f-]{36}$/,
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

for (const feeCase of [
  { fee: null, fee_currency: null, expected: "Комиссия неизвестна" },
  { fee: "0.48", fee_currency: "USDT", expected: "0,48 USDT" },
  { fee: "0.48", fee_currency: null, expected: "Валюта комиссии неизвестна" },
]) {
  test(`payment history preserves actual fee currency: ${feeCase.expected}`, async ({
    page,
  }) => {
    const account = {
      id: "9007199254740993123",
      email: "synthetic@example.test",
      email_verified: true,
      telegram_id: null,
    };
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname.replace(
        "/api/v1",
        "",
      );
      let json: unknown = {};
      if (path === "/auth/me")
        json = {
          account,
          csrf_token: "synthetic",
          is_admin: false,
          mfa_verified: false,
          staff_role: null,
          capabilities: [],
        };
      if (path === "/catalog")
        json = {
          products: [],
          periods: [],
          content: {},
          legal: { configured: false, documents: [] },
        };
      if (path === "/legal/status")
        json = {
          accepted: true,
          configured: false,
          documents: [],
          missing: [],
        };
      if (path === "/dashboard")
        json = {
          account,
          subscriptions: [],
          payments: [
            {
              id: "synthetic-payment",
              order_id: "synthetic-order",
              amount: "105.50",
              currency: "RUB",
              status: "paid",
              paid_at: "2026-10-07T21:10:00Z",
              fee: feeCase.fee,
              fee_currency: feeCase.fee_currency,
              environment: "local_test",
            },
          ],
          bonus_balance: "0",
          bonus_reserved: "0",
          credit_balance: "0",
          credit_available: "0",
          credit_reserved: "0",
        };
      await route.fulfill({ json });
    });
    await page.setViewportSize({ width: 320, height: 900 });
    await page.goto("/#payments");
    await page.getByRole("button", { name: "Подробнее о платеже" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("105,50 ₽");
    await expect(dialog).toContainText("8 октября 2026");
    await expect(dialog).toContainText("Тариф не указан в старой записи");
    await expect(dialog).toContainText(feeCase.expected);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Подробнее о платеже" }),
    ).toBeFocused();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}
