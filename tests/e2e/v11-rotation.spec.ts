import { expect, test } from "@playwright/test";
test.beforeEach(async ({ page }) => {
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}};",
    }),
  );
});
test("durable pending credential rotation hides old URL and QR across page refresh without changing paid rights", async ({
  page,
}) => {
  const account = {
    id: "synthetic-account",
    email: "synthetic@example.test",
    email_verified: true,
    telegram_id: null,
  };
  const oldUrl = "https://vpn.example.test/synthetic-old-private-link";
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
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
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/legal/status")
      json = { accepted: true, configured: false, documents: [], missing: [] };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [
          {
            id: "synthetic-sub",
            name: "Мой VPN",
            product_id: null,
            status: "active",
            expires_at: "2099-01-01T00:00:00Z",
            devices_limit: 4,
            version: 1,
            connection_url: oldUrl,
            credential_rotation_state: "review",
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
        source: "unavailable",
        status: null,
        observed_at: null,
        error: "panel_unavailable",
        traffic_bytes: null,
        traffic_limit_bytes: null,
        devices: [],
        paid_expires_at: "2099-01-01T00:00:00Z",
      };
    await route.fulfill({ json });
  });
  await page.goto("/#home");
  await page.locator(".connect-button").click();
  await expect(page.getByRole("dialog")).toContainText("Перевыпуск ссылки");
  await expect(
    page.getByRole("dialog").getByRole("link", { name: "Перейти по ссылке" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("dialog").getByRole("button", { name: /QR/ }),
  ).toHaveCount(0);
  await expect(page.getByRole("dialog")).not.toContainText(oldUrl);
  await page.reload();
  await page.evaluate(() => {
    location.hash = "devices";
  });
  await expect(page.locator("main")).toContainText("Оплаченный доступ");
  await expect(page.locator("main")).toContainText("2099");
  await expect(
    page.getByRole("button", { name: "Перевыпустить ссылку" }),
  ).toBeDisabled();
  await expect(page.locator("main")).toContainText("Перевыпуск ссылки");
});
