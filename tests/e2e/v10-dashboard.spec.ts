import { syntheticInstance } from "./instance-fixture";
import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";
import { ownerStaffFixture } from "./staff-fixture";

const account = {
  id: "9007199254740993123",
  email: "synthetic@example.test",
  email_verified: true,
  telegram_id: null,
};
const documents = [
  { document_type: "offer", version: "v1", url: "https://example.test/offer" },
  {
    document_type: "privacy",
    version: "v1",
    url: "https://example.test/privacy",
  },
];

async function mockDashboard(
  page: Page,
  loggedIn = true,
  admin = false,
  subscriptionName = "Моя подписка",
) {
  const writes: Record<string, unknown>[] = [];
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}};",
    }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    if (path === "/auth/me" && !loggedIn)
      return route.fulfill({
        status: 401,
        json: { message: "Требуется вход" },
      });
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account,
        csrf_token: "synthetic",
        is_admin: admin,
        ...(admin ? ownerStaffFixture : { staff_role: null, capabilities: [] }),
        mfa_verified: admin,
      };
    if (path === "/public-config")
      json = {
        instance: syntheticInstance,
        local_test: true,
        turnstile_test_bypass: true,
      };
    if (path === "/promos/active") json = null;
    if (path === "/admin/users") json = { items: [], total: 0 };
    if (path === "/auth/email/request")
      json = { challenge_id: "synthetic-challenge", resend_after: 120 };
    if (path === "/legal" || path === "/legal/status")
      json = { configured: true, accepted: true, missing: [], documents };
    if (path === "/catalog")
      json = {
        products: [],
        periods: [],
        content: {},
        legal: { configured: true, documents },
      };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [
          {
            id: "synthetic-sub",
            product_id: "standard",
            name: subscriptionName,
            status: "active",
            expires_at: "2099-12-01T00:00:00Z",
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
    if (path === "/account/link/request") {
      writes.push(route.request().postDataJSON());
      json = {
        proof: "synthetic-one-time-proof",
        expires_in: 300,
        requires_merge: false,
      };
    }
    await route.fulfill({ json });
  });
  return writes;
}

async function checkDarkAndAccessible(page: Page) {
  const luminance = await page.locator("body").evaluate((body) => {
    const channels = getComputedStyle(body)
      .backgroundColor.match(/\d+/g)!
      .map(Number);
    return (channels[0] + channels[1] + channels[2]) / 3;
  });
  expect(luminance).toBeLessThan(40);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const audit = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(
    audit.violations.filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    ),
  ).toEqual([]);
}

test("long subscription names wrap within the card at mobile, tablet and desktop widths", async ({
  page,
}) => {
  await mockDashboard(
    page,
    true,
    false,
    "ДомашняяПодпискаДляВсехСемейныхУстройств",
  );
  for (const width of [320, 360, 390, 768, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/#home");
    const heading = page.locator(".subscription-card h2");
    await expect(heading).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    const bounds = await heading.evaluate((element) => ({
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
    }));
    expect(bounds.scrollWidth).toBeLessThanOrEqual(bounds.clientWidth + 1);
  }
});

test("денежные средства показывают доступный остаток без пустого резерва и ровно выравнивают строки", async ({
  page,
}) => {
  await mockDashboard(page);
  await mkdir(".artifacts/ui/v10-cash-wallet", { recursive: true });
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/#bonuses");
    const card = page.locator(".cash-wallet");
    await expect(card).toBeVisible();
    await expect(card.locator(".cash-balance strong")).toHaveText("0 ₽");
    await expect(card).not.toContainText("На балансе");
    await expect(card).not.toContainText("Зарезервировано");
    await expect(card).not.toContainText("Источник:");
    await expect(card).not.toContainText("Валюта уточняется");
    expect(
      await page
        .locator("html")
        .evaluate((element) => getComputedStyle(element).scrollbarColor),
    ).toBe("rgb(101, 113, 122) rgb(21, 25, 29)");
    const geometry = await card.locator(".cash-balance").evaluate((row) => {
      const label = row.querySelector("span")!.getBoundingClientRect();
      const value = row.querySelector("strong")!.getBoundingClientRect();
      return {
        gap: value.left - label.right,
        centerDifference: Math.abs(
          label.top + label.height / 2 - value.top - value.height / 2,
        ),
      };
    });
    expect(geometry.gap).toBeGreaterThanOrEqual(12);
    expect(geometry.centerDifference).toBeLessThanOrEqual(1);
    await page.screenshot({
      path: `.artifacts/ui/v10-cash-wallet/unknown-${width}.png`,
      fullPage: true,
    });
    await checkDarkAndAccessible(page);
  }
  await page.route("**/api/v1/dashboard", (route) =>
    route.fulfill({
      json: {
        account,
        subscriptions: [],
        payments: [],
        bonus_balance: "0",
        bonus_reserved: "0",
        credit_balance: "125.50",
        credit_available: "100.50",
        credit_reserved: "25.00",
        credit_currency: "RUB",
        credit_provenance: "owner_adjustment",
      },
    }),
  );
  await page.goto("/#bonuses");
  await page.reload();
  const card = page.locator(".cash-wallet");
  await expect(card.locator(".cash-balance strong")).toHaveText("100,50 ₽");
  await expect(card).toContainText("Зарезервировано");
  await expect(card).toContainText("25 ₽");
  await expect(card).not.toContainText("Валюта уточняется");
});

test("тёмный кабинет и профиль читаемы на desktop, tablet и mobile", async ({
  page,
}) => {
  await mockDashboard(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await mkdir(".artifacts/ui/v10-dashboard", { recursive: true });
  for (const width of [1440, 768, 390, 360]) {
    await page.setViewportSize({ width, height: 900 });
    for (const section of ["home", "account"]) {
      await page.goto(`/#${section}`);
      await expect(
        page.locator(".workspace-main .card, .subscription-card").first(),
      ).toBeVisible();
      await page.locator(".workspace-main h1").click();
      await expect(page.getByRole("dialog")).toHaveCount(0);
      await page.screenshot({
        path: `.artifacts/ui/v10-dashboard/${section}-${width}.png`,
        fullPage: true,
      });
      await checkDarkAndAccessible(page);
    }
  }
  await page.getByRole("button", { name: "Открыть меню", exact: true }).click();
  await expect(
    page.getByRole("navigation", { name: "Разделы кабинета" }),
  ).toBeVisible();
  await page
    .getByRole("navigation", { name: "Разделы кабинета" })
    .getByRole("link")
    .first()
    .focus();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Открыть меню", exact: true }),
  ).toBeFocused();
  expect(errors).toEqual([]);
});

test("клиентская тёмная тема сохраняет палитру административного кабинета", async ({
  page,
}) => {
  await page.route("**/admin", async (route) =>
    route.fulfill({
      response: await route.fetch({ url: "http://127.0.0.1:3200/" }),
    }),
  );
  await mockDashboard(page, true, true);
  await page.goto("/admin#users");
  await expect(page.locator(".admin-layout")).toBeVisible();
  for (const selector of ["body", ".admin-layout .sidebar"]) {
    expect(
      await page.locator(selector).evaluate((element) => {
        const rgb = getComputedStyle(element)
          .backgroundColor.match(/\d+/g)!
          .map(Number);
        return (rgb[0] + rgb[1] + rgb[2]) / 3;
      }),
    ).toBeGreaterThan(200);
  }
});

test("экран входа использует ту же тёмную палитру", async ({ page }) => {
  await mockDashboard(page, false);
  await mkdir(".artifacts/ui/v10-dashboard", { recursive: true });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    await expect(page.locator('input[type="email"]')).toBeVisible();
    await page.screenshot({
      path: `.artifacts/ui/v10-dashboard/login-${width}.png`,
      fullPage: true,
    });
    await checkDarkAndAccessible(page);
    await page.locator('input[type="email"]').fill("synthetic@example.test");
    await page.getByRole("checkbox").check();
    await page
      .getByRole("button", { name: "Получить код", exact: true })
      .click();
    await expect(page.locator(".code-slots")).toBeVisible();
    await page.screenshot({
      path: `.artifacts/ui/v10-dashboard/code-${width}.png`,
      fullPage: true,
    });
    await checkDarkAndAccessible(page);
  }
});

for (const [identity, body] of [
  ["@Synthetic_User", { telegram_username: "Synthetic_User" }],
  ["Synthetic_User", { telegram_username: "Synthetic_User" }],
  ["9007199254740993123", { telegram_id: "9007199254740993123" }],
] as const) {
  test(`привязка ${identity} передаёт точную идентичность и требует подтверждения в боте`, async ({
    page,
  }) => {
    const writes = await mockDashboard(page);
    await page.goto("/#account");
    await page
      .getByRole("button", { name: "Связать Telegram", exact: true })
      .click();
    const dialog = page.getByRole("dialog");
    await dialog
      .getByLabel("Username или Telegram ID", { exact: true })
      .fill(identity);
    await dialog
      .getByRole("button", { name: "Начать подтверждение", exact: true })
      .click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0]).toEqual(body);
    await expect(
      dialog.getByRole("button", { name: /Скопировать/ }),
    ).toBeVisible();
    await expect(dialog).toContainText("бот");
    await checkDarkAndAccessible(page);
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    await expect(
      page.getByRole("button", { name: "Связать Telegram", exact: true }),
    ).toBeFocused();
  });
}

test("ошибочный username не запускает привязку; мобильная форма доступна с клавиатуры", async ({
  page,
}) => {
  const writes = await mockDashboard(page);
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto("/#account");
  await page
    .getByRole("button", { name: "Связать Telegram", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  const input = dialog.getByLabel("Username или Telegram ID", { exact: true });
  for (const value of ["abc", "имя_пользователя", "@name-with-dash"]) {
    await input.fill(value);
    await dialog
      .getByRole("button", { name: "Начать подтверждение", exact: true })
      .click();
    expect(
      await input.evaluate(
        (element: HTMLInputElement) => element.validity.patternMismatch,
      ),
    ).toBe(true);
    expect(writes).toHaveLength(0);
  }
  await input.fill("@synthetic_user");
  await input.focus();
  await page.keyboard.press("Tab");
  await expect(
    dialog.getByRole("button", { name: "Начать подтверждение", exact: true }),
  ).toBeFocused();
  await mkdir(".artifacts/ui/v10-dashboard", { recursive: true });
  await page.screenshot({
    path: ".artifacts/ui/v10-dashboard/link-360.png",
    fullPage: true,
  });
  await checkDarkAndAccessible(page);
});
