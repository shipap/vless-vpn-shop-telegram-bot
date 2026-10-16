import { expect, test, type Page, type Route } from "@playwright/test";
import { syntheticInstance } from "./instance-fixture";

async function fixture(page: Page, product = "high") {
  const account = {
    id: "9007199254740993123",
    email: null,
    email_verified: true,
    telegram_id: null,
  };
  const subscription = {
    id: "own-sub",
    product_id: "middle",
    name: "Моя подписка",
    status: "active",
    expires_at: "2027-01-01T00:00:00Z",
    devices_limit: 4,
    devices: [],
    segments: [],
    recurring: null,
    retention_state: "none",
  };
  const dashboard = {
    account,
    subscriptions: [
      subscription,
      { ...subscription, id: "other-own-sub", name: "Вторая подписка" },
    ],
    payments: [],
    bonus_balance: "0",
    credit_balance: "0",
    credit_available: "0",
    recurring_enrollment_enabled: false,
  };
  const targeted: string[] = [];
  const quotes: Record<string, unknown>[] = [];
  let pending: Route | null = null;
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1") return route.abort();
    if (!url.pathname.startsWith("/api/v1/")) return route.continue();
    const path = url.pathname.replace("/api/v1", "");
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account,
        csrf_token: "synthetic",
        is_admin: false,
        capabilities: [],
      };
    if (path === "/public-config")
      json = { instance: syntheticInstance, local_test: true };
    if (path === "/catalog")
      json = {
        products: ["low", "middle", "high"].map((id, index) => ({
          id,
          code: id,
          name: `Тариф ${id}`,
          tier_level: index + 1,
          available: true,
          price: "100.00",
          currency: "RUB",
          included_devices: 4,
          description: "",
          periods: [
            { days: 30, months: 1, discount_percent: "0", price: "100.00" },
          ],
        })),
        periods: [{ days: 30, months: 1, discount_percent: "0" }],
        legal: { configured: true, documents: [], revision: "1" },
        capabilities: {},
      };
    if (path === "/dashboard") {
      if (url.searchParams.has("refresh_subscription_id")) {
        targeted.push(url.search);
        pending = route;
        return;
      }
      json = dashboard;
    }
    if (path === "/quotes") {
      quotes.push(route.request().postDataJSON());
      return route.fulfill({
        status: 409,
        json: {
          error: {
            code: "synthetic_observed",
            message: "Синтетический расчёт принят для проверки.",
          },
        },
      });
    }
    if (path === "/legal/status" || path === "/legal")
      json = { configured: true, accepted: true, documents: [], missing: [] };
    if (path === "/nodes") json = { items: [], state: "unknown" };
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/notifications/stream")
      return route.fulfill({ contentType: "text/event-stream", body: "" });
    if (path === "/notifications/preferences")
      json = { browser_enabled: false, push_subscriptions: [] };
    await route.fulfill({ json });
  });
  return {
    targeted,
    quotes,
    release: async (failure = false) => {
      await expect.poll(() => Boolean(pending)).toBe(true);
      await pending!
        .fulfill(
          failure
            ? {
                status: 503,
                json: {
                  error: {
                    code: "panel_unavailable",
                    message: "Панель временно недоступна.",
                  },
                },
              }
            : {
                json: {
                  ...dashboard,
                  subscriptions: [
                    {
                      ...subscription,
                      product_id: product,
                      devices_limit: 9,
                      expires_at: "2027-03-01T00:00:00Z",
                      panel_sync_state: "fresh",
                      renewal_pricing_state: "ready",
                    },
                    dashboard.subscriptions[1],
                  ],
                },
              },
        )
        .catch(() => {});
    },
  };
}
for (const [action, label] of [
  ["renew", "Продлить подписку"],
  ["upgrade", "Повысить тариф"],
  ["downgrade", "Понизить тариф"],
  ["devices", "Добавить устройства"],
]) {
  test(`${action} waits for selected panel facts before opening choices`, async ({
    page,
  }) => {
    const state = await fixture(page, action === "upgrade" ? "low" : "high");
    await page.goto(action === "devices" ? "/#devices" : "/#home");
    await page.getByRole("button", { name: new RegExp(`^${label}`) }).click();
    await expect(page.getByRole("status")).toContainText(
      "Проверяем данные подписки",
    );
    await expect(
      page.getByRole("button", { name: new RegExp(`^${label}`) }),
    ).toBeDisabled();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(state.targeted).toEqual([
      "?refresh_panel=true&refresh_subscription_id=own-sub",
    ]);
    await state.release();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText(
      action === "upgrade" ? "Тариф low" : "Тариф high",
    );
    if (action === "upgrade" || action === "downgrade")
      await expect(dialog.locator("option[value=middle]")).toHaveCount(1);
    else await expect(dialog.getByRole("combobox").first()).toHaveValue("high");
    expect(state.targeted).toHaveLength(1);
    await dialog.getByRole("checkbox").last().check();
    await dialog.getByRole("button", { name: "Рассчитать стоимость" }).click();
    await expect.poll(() => state.quotes.length).toBe(1);
    expect(state.quotes[0]).toMatchObject({
      subscription_id: "own-sub",
      action,
      product_id:
        action === "upgrade"
          ? "middle"
          : action === "downgrade"
            ? "low"
            : "high",
      extra_devices: action === "devices" ? 1 : 0,
    });
  });
}
test("failed action refresh keeps stale checkout closed", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/#home");
  await page.getByRole("button", { name: /^Продлить подписку/ }).click();
  await state.release(true);
  await expect(
    page.getByText("Панель временно недоступна.", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
for (const navigation of ["selection", "page"])
  test(`late refresh cannot open checkout after changing ${navigation}`, async ({
    page,
  }) => {
    const state = await fixture(page);
    await page.goto("/#home");
    await page.getByRole("button", { name: /^Продлить подписку/ }).click();
    await expect(page.getByRole("status")).toBeVisible();
    if (navigation === "selection")
      await page
        .locator(".subscription-select select")
        .selectOption("other-own-sub");
    else
      await page.evaluate(() => {
        location.hash = "#payments";
      });
    await state.release();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByRole("status")).toHaveCount(0);
  });
