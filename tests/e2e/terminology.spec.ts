import { expect, test } from "@playwright/test";
import { syntheticInstance } from "./instance-fixture";

const landing = process.env.SUNRISE_TERMINOLOGY_SURFACE === "landing";
const protectedTerms = {
  service_label: "Защищённое подключение",
  service_summary: "Защищённое подключение для ваших устройств",
  connections_label: "Мои подключения",
  connect_label: "Настроить подключение",
  client_label: "совместимое приложение",
  connection_label: "защищённое подключение",
  hero_title: "Ваше защищённое подключение.",
};

for (const configured of [false, true]) {
  for (const width of [390, 1440]) {
    test(`${landing ? "landing" : "dashboard"} uses ${configured ? "configured" : "legacy VPN"} terminology at ${width}px`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width, height: 950 });
      let authenticated = false;
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
        const path = new URL(route.request().url()).pathname.replace(
          "/api/v1",
          "",
        );
        let json: unknown = {};
        if (path === "/public-config")
          json = {
            local_test: true,
            instance: {
              ...syntheticInstance,
              app_name: "Тестовый сервис",
              app_short_name: "Тест",
              app_description: configured
                ? "Подписка на защищённое подключение для ваших устройств."
                : syntheticInstance.app_description,
              terminology: configured ? protectedTerms : undefined,
            },
          };
        if (path === "/auth/me" && !authenticated)
          return route.fulfill({
            status: 401,
            json: { detail: "authentication_required" },
          });
        if (path === "/auth/me")
          json = {
            account: authenticated ? account : null,
            csrf_token: "synthetic",
            is_admin: false,
            mfa_verified: false,
            capabilities: [],
          };
        if (path === "/catalog")
          json = {
            products: [],
            periods: [],
            content: {
              help_center: {
                connection_instructions:
                  "Установите совместимое приложение и импортируйте личную ссылку.",
              },
            },
            legal: { configured: false, documents: [] },
          };
        if (path === "/legal" || path === "/legal/status")
          json = {
            configured: false,
            accepted: true,
            missing: [],
            documents: [],
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
                connection_url: "https://vpn.example.test/synthetic",
                devices: [],
                devices_limit: 2,
                segments: [],
                recurring: null,
              },
            ],
            payments: [],
            bonus_balance: "0",
            credit_balance: "0",
            credit_available: "0",
            recurring_enrollment_enabled: false,
          };
        if (path === "/notifications") json = { items: [], unread_count: 0 };
        if (path === "/notifications/stream")
          return route.fulfill({ contentType: "text/event-stream", body: "" });
        expect(route.request().method()).toBe("GET");
        await route.fulfill({ json });
      });
      await page.goto("/");
      if (landing) {
        await expect(page.getByRole("heading", { level: 1 })).toContainText(
          configured ? "Ваше защищённое подключение." : "Ваш VPN.",
        );
        await expect(
          page.getByRole("heading", {
            name: configured ? "Настроить подключение" : "Подключить VPN",
            exact: true,
          }),
        ).toBeVisible();
        await expect(page).toHaveTitle(
          `${configured ? "Защищённое подключение для ваших устройств" : "VPN для ваших устройств"} — Тестовый сервис`,
        );
      } else {
        await expect(
          page.getByText(
            `Тестовый сервис · ${configured ? "Защищённое подключение для ваших устройств" : "VPN для ваших устройств"}`,
            { exact: true },
          ),
        ).toBeAttached();
        authenticated = true;
        await page.reload();
        await expect(
          page.getByRole("heading", {
            name: configured ? "Мои подключения" : "Мой VPN",
            exact: true,
          }),
        ).toBeVisible();
        await page
          .getByRole("button", {
            name: configured ? "Настроить подключение" : "Подключить VPN",
            exact: true,
          })
          .click();
        await expect(
          page.getByRole("dialog", {
            name: configured ? "Настроить подключение" : "Подключить VPN",
            exact: true,
          }),
        ).toBeVisible();
      }
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({
        path: testInfo.outputPath("terminology.png"),
        fullPage: true,
      });
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth + 1,
        ),
      ).toBe(true);
    });
  }
}
