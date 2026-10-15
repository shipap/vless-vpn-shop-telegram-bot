import { syntheticInstance } from "./instance-fixture";
import { expect, test } from "@playwright/test";

test("email resend countdown begins at the server cooldown despite fractional request duration", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-10-08T10:00:00Z") });
  await page.clock.pauseAt(new Date("2026-10-08T10:00:00Z"));
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}}",
    }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me"))
      return route.fulfill({
        status: 401,
        json: { error: { code: "auth_required" } },
      });
    if (path.endsWith("/public-config"))
      return route.fulfill({
        json: {
          instance: syntheticInstance,
          local_test: true,
          turnstile_test_bypass: true,
        },
      });
    if (path.endsWith("/auth/email/request")) {
      await page.clock.runFor(250);
      return route.fulfill({
        json: { challenge_id: "synthetic-challenge", resend_after: 120 },
      });
    }
    return route.fulfill({
      json: {
        configured: true,
        documents: [
          {
            document_type: "offer",
            version: "1",
            url: "https://example.test/offer",
          },
          {
            document_type: "privacy",
            version: "1",
            url: "https://example.test/privacy",
          },
        ],
      },
    });
  });
  await page.goto("/");
  await page
    .getByLabel("Email", { exact: true })
    .fill("synthetic@example.test");
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Получить код", exact: true }).click();
  await expect(page.getByLabel("Код из письма", { exact: true })).toBeVisible();
  await expect(page.locator(".login-resend > .button")).toHaveText(
    "Повторить через 120 сек.",
  );
  await expect(page.locator(".login-resend > .button")).toBeDisabled();
  await page.clock.runFor(119_000);
  await expect(page.locator(".login-resend > .button")).toBeDisabled();
  await page.clock.runFor(2_000);
  await expect(page.locator(".login-resend > .button")).toBeEnabled();
});
