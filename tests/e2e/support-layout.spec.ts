import { expect, test, type Locator, type Page } from "@playwright/test";
import { syntheticInstance } from "./instance-fixture";

async function syntheticDashboard(page: Page) {
  const account = {
    id: "9007199254740993123",
    email: "synthetic@example.test",
    email_verified: true,
    telegram_id: null,
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
        content: {},
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
            connection_url: "https://vpn.example.test/synthetic-connection",
            devices: [],
            devices_limit: 2,
            segments: [],
            recurring: null,
          },
        ],
        payments: [
          {
            id: "synthetic-payment",
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
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/notifications/stream")
      return route.fulfill({ contentType: "text/event-stream", body: "" });
    expect(route.request().method()).toBe("GET");
    await route.fulfill({ json });
  });
}

async function separatedActions(first: Locator, support: Locator) {
  await expect(first).toBeVisible();
  await expect(support).toBeVisible();
  const a = (await first.boundingBox())!;
  const b = (await support.boundingBox())!;
  // A real gap must separate clickable surfaces, whether beside or below.
  const gap = Math.max(
    b.x - (a.x + a.width),
    a.x - (b.x + b.width),
    b.y - (a.y + a.height),
    a.y - (b.y + b.height),
  );
  expect
    .soft(gap, "adjacent actions need visible spacing")
    .toBeGreaterThanOrEqual(8);
  for (const action of [first, support]) {
    const bounds = await action.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const parent = element.closest("article, section")!;
      const container = parent.getBoundingClientRect();
      return {
        left: rect.left - container.left,
        right: container.right - rect.right,
        height: rect.height,
        horizontalOverflow: element.scrollWidth - element.clientWidth,
      };
    });
    expect
      .soft(bounds.left, "action fits the card's left edge")
      .toBeGreaterThanOrEqual(0);
    expect
      .soft(bounds.right, "action fits the card's right edge")
      .toBeGreaterThanOrEqual(0);
    expect
      .soft(bounds.height, "usable touch target")
      .toBeGreaterThanOrEqual(44);
    expect
      .soft(bounds.horizontalOverflow, "button content is not clipped")
      .toBeLessThanOrEqual(1);
  }
}

for (const width of [320, 390, 768, 1440]) {
  test(`support actions remain separate and fit their cards at ${width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 950 });
    await syntheticDashboard(page);
    await page.goto("/");
    const subscriptionSupport = page.getByRole("link", {
      name: "Поддержка по подписке",
      exact: true,
    });
    await expect(subscriptionSupport).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({
      path: testInfo.outputPath("my-vpn.png"),
      fullPage: true,
    });
    await separatedActions(
      page.getByRole("button", { name: "Подключить VPN", exact: true }),
      subscriptionSupport,
    );
    await page.goto("/#payments");
    const orderSupport = page.getByRole("link", {
      name: "Поддержка по заказу",
      exact: true,
    });
    await expect(orderSupport).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath("payments.png"),
      fullPage: true,
    });
    await separatedActions(
      page.getByRole("button", { name: "Подробнее о платеже", exact: true }),
      orderSupport,
    );
    await page
      .getByRole("button", { name: "Подробнее о платеже", exact: true })
      .click();
    await expect(
      page.getByRole("dialog", { name: "Подробности платежа", exact: true }),
    ).toBeVisible();
  });
}
