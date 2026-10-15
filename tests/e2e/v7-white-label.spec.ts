import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";
import type { PublicInstance } from "../../packages/api-client/types";
import { ownerStaffFixture } from "./staff-fixture";
const instance = (
  privateProfile = false,
  name = "Северный VPN",
): PublicInstance => ({
  app_name: name,
  app_short_name: name,
  app_slug: "synthetic-vpn",
  app_description: "Подписка на VPN для ваших устройств.",
  visual_profile: privateProfile ? "private" : "neutral",
  accent_color: "#B8D0DD",
  sender_name: name,
  public_site_url: "http://127.0.0.1:3100/",
  dashboard_url: "http://127.0.0.1:3200/",
  telegram_bot_username: null,
  support_url: "https://example.test/support",
  support_email: null,
  logo_url: null,
  favicon_url: null,
  hero_url: null,
  og_image_url: null,
});
async function fixture(
  page: Page,
  branding: PublicInstance = instance(),
  unavailable = false,
  authenticated: "customer" | "owner" | null = null,
) {
  const account = {
    id: "9007199254740993123",
    email: "synthetic@example.test",
    email_verified: true,
    telegram_id: null,
  };
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}};",
    }),
  );
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/public-config"))
      return unavailable
        ? route.fulfill({
            status: 503,
            json: { error: { code: "config_unavailable" } },
          })
        : route.fulfill({
            json: {
              instance: branding,
              dashboard_url: branding.dashboard_url,
              bot_url: null,
              support_url: branding.support_url,
              local_test: true,
              turnstile_test_bypass: true,
            },
          });
    if (path.endsWith("/auth/me") && authenticated)
      return route.fulfill({
        json: {
          account,
          csrf_token: "synthetic",
          is_admin: authenticated === "owner",
          mfa_verified: authenticated === "owner",
          ...(authenticated === "owner"
            ? ownerStaffFixture
            : { staff_role: null, capabilities: [] }),
        },
      });
    if (path.endsWith("/auth/me"))
      return route.fulfill({
        status: 401,
        json: { error: { code: "unauthorized" } },
      });
    if (path.endsWith("/legal"))
      return route.fulfill({ json: { configured: false, documents: [] } });
    if (path.endsWith("/legal/status"))
      return route.fulfill({
        json: { configured: false, accepted: true, missing: [], documents: [] },
      });
    if (path.endsWith("/dashboard"))
      return route.fulfill({
        json: {
          account,
          subscriptions: [],
          payments: [],
          bonus_balance: "0",
          bonus_reserved: "0",
          credit_balance: "0",
          credit_available: "0",
          credit_reserved: "0",
          credit_currency: "RUB",
        },
      });
    if (path.endsWith("/promos/active")) return route.fulfill({ json: null });
    if (path.endsWith("/notifications"))
      return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path.endsWith("/admin/users"))
      return route.fulfill({
        json: { items: [], total: 0, page: 1, page_size: 20 },
      });
    return route.fulfill({
      json: {
        products: [
          {
            id: "9007199254740993123",
            code: "synthetic",
            name: "Тариф команды",
            description: "Настраиваемые условия подписки.",
            price: "173.25",
            currency: "RUB",
            available: true,
            version: 1,
            included_devices: 7,
            base_period_days: 28,
            visible_landing: true,
            sort_order: 0,
            highlighted: false,
            content: {},
            icon: null,
            periods: [],
          },
        ],
        periods: [],
        content: {},
        legal: { configured: false, documents: [] },
      },
    });
  });
}
test("neutral landing uses configured identity and catalog without private mascot or sunrise wording", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("http://127.0.0.1:3100/");
  await expect(page.getByRole("banner")).toContainText("Северный VPN");
  await expect(page.locator("body")).not.toContainText(/sunrise|bububi/i);
  await expect(page.locator('img[src*="mascot"],.sunrise-horizon')).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("heading", { name: "Тариф команды", exact: true }),
  ).toBeVisible();
  await expect(page.locator("body")).toContainText("173,25 ₽");
  await expect(page).toHaveTitle(/Северный VPN/);
});
for (const role of ["customer", "owner"] as const)
  test(`neutral ${role} workspace uses dark tokens and responsive configured identity`, async ({
    page,
  }) => {
    await fixture(page, instance(), false, role);
    if (role === "owner")
      await page.route("**/admin", async (route) =>
        route.fulfill({
          response: await page.request.get("http://127.0.0.1:3200/"),
        }),
      );
    await mkdir(".artifacts/ui/v7-instance", { recursive: true });
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(
        role === "owner"
          ? "http://127.0.0.1:3200/admin#users"
          : "http://127.0.0.1:3200/#home",
      );
      await expect(page.locator(".workspace-main")).toBeVisible();
      await expect(page.locator("html")).toHaveAttribute(
        "data-visual-profile",
        "neutral",
      );
      if (role === "customer") {
        await expect(
          page.locator(".welcome-lines,.welcome-card>.lucide-sun"),
        ).toHaveCount(0);
        await expect(page.locator(".welcome-card")).not.toContainText(
          "горизонт",
        );
      }
      expect(
        await page.evaluate(
          () => getComputedStyle(document.body).backgroundColor,
        ),
      ).toBe("rgb(16, 19, 22)");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      const scan = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa"])
        .analyze();
      expect(scan.violations).toEqual([]);
      await page.screenshot({
        path: `.artifacts/ui/v7-instance/neutral-${role}-${width}.png`,
        fullPage: true,
      });
    }
  });
test("neutral authentication uses configured identity without private sun art", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("http://127.0.0.1:3200/");
  await expect(
    page.getByRole("link", { name: "Северный VPN — главная" }).first(),
  ).toBeVisible();
  await expect(page.locator("body")).not.toContainText(/sunrise|bububi/i);
  await expect(page.locator(".login-sun")).toHaveCount(0);
});
test("unavailable public identity stays neutral and broken optional assets disappear without hiding catalog actions", async ({
  page,
}) => {
  await fixture(page, instance(), true);
  await page.goto("http://127.0.0.1:3100/");
  await expect(page.getByRole("banner")).toContainText("VPN-сервис");
  await expect(page.locator("body")).not.toContainText(/sunrise|bububi/i);
  await page.unroute("**/api/v1/**");
  await fixture(page, {
    ...instance(),
    logo_url: "/api/v1/instance/assets/logo",
    hero_url: "/api/v1/instance/assets/hero",
  });
  await page.route("**/api/v1/instance/assets/*", (route) =>
    route.fulfill({ status: 404, body: "Not found" }),
  );
  await page.reload();
  await expect(page.getByRole("banner")).toContainText("Северный VPN");
  await expect(page.locator(".brand img,.sunrise-mascot")).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "Выбрать тариф: Тариф команды" }),
  ).toBeVisible();
});
for (const privateProfile of [false, true])
  test(`profile ${privateProfile ? "private" : "neutral"} preserves responsive layout and keyboard access with a long name`, async ({
    page,
  }) => {
    const name =
      "Сервис безопасного подключения для команды и повседневных устройств";
    const value = {
      ...instance(privateProfile, name),
      accent_color: privateProfile ? "#FFB44E" : "#B8D0DD",
    };
    await fixture(page, value);
    await mkdir(".artifacts/ui/v7-instance", { recursive: true });
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("http://127.0.0.1:3100/");
      await expect(page.getByRole("banner")).toContainText(name);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      const clipped = await page
        .locator(".brand,.sunrise-hero h1,.neutral-service-summary")
        .evaluateAll((elements) =>
          elements
            .filter(
              (element) =>
                element.scrollWidth > element.clientWidth + 1 ||
                element.getBoundingClientRect().right > innerWidth + 1,
            )
            .map((element) => element.className),
        );
      expect(clipped).toEqual([]);
      await page.keyboard.press("Tab");
      await expect(page.locator(":focus")).toHaveText("Перейти к содержимому");
      const scan = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa"])
        .analyze();
      expect(scan.violations).toEqual([]);
      await page.keyboard.press("Tab");
      await page.screenshot({
        path: `.artifacts/ui/v7-instance/${privateProfile ? "private" : "neutral"}-landing-${width}.png`,
        fullPage: true,
      });
      await page.goto("http://127.0.0.1:3200/");
      await expect(page.locator(".login-form")).toBeVisible();
      await expect(page.locator(".login-sun")).toHaveCount(
        privateProfile ? 1 : 0,
      );
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      const loginScan = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa"])
        .analyze();
      expect(loginScan.violations).toEqual([]);
      await page.screenshot({
        path: `.artifacts/ui/v7-instance/${privateProfile ? "private" : "neutral"}-login-${width}.png`,
        fullPage: true,
      });
    }
  });
