import { syntheticInstance } from "./instance-fixture";
import { expect, test, type Page } from "@playwright/test";

const documents = [
  { document_type: "offer", version: "v1", url: "https://example.test/offer" },
  {
    document_type: "privacy",
    version: "v1",
    url: "https://example.test/privacy",
  },
];
async function syntheticRoutes(page: Page, miniApp = false) {
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: miniApp
        ? "window.Telegram={WebApp:{initData:'synthetic-init-data',ready(){},expand(){},onEvent(){},offEvent(){}}};"
        : "window.Telegram={WebApp:{initData:''}};",
    }),
  );
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    if (route.request().method() !== "GET")
      writes.push({ path, body: route.request().postDataJSON() });
    if (path === "/auth/me")
      return route.fulfill({
        status: 401,
        json: { error: { code: "auth_required" } },
      });
    let json: unknown = {};
    if (path === "/public-config")
      json = {
        instance: syntheticInstance,
        dashboard_url: "http://127.0.0.1:3200/",
        local_test: true,
        turnstile_test_bypass: true,
        telegram_miniapp_enabled: miniApp,
        telegram_login_enabled: true,
      };
    if (path === "/catalog")
      json = {
        products: [],
        content: {},
        legal: { configured: true, documents },
      };
    if (path === "/legal") json = { configured: true, documents };
    if (path === "/auth/email/request")
      json = { challenge_id: "synthetic-challenge", resend_after: 0 };
    if (path === "/auth/email/verify")
      return route.fulfill({
        status: 400,
        json: {
          error: { code: "code_invalid", message: "Синтетический код неверен" },
        },
      });
    if (path === "/auth/telegram/miniapp")
      return route.fulfill({
        status: 409,
        json: { error: { code: "legal_consent_required" } },
      });
    if (path === "/auth/telegram/start")
      return route.fulfill({
        status: 503,
        json: { error: { code: "telegram_auth_unavailable" } },
      });
    return route.fulfill({ json });
  });
  return writes;
}

test("landing referral survives another landing visit and cross-origin email registration", async ({
  page,
}) => {
  const writes = await syntheticRoutes(page);
  await page.goto("http://127.0.0.1:3100/?ref=ref_9007199254740993");
  const invitation = page.getByRole("link", {
    name: "Личный кабинет",
    exact: true,
  });
  await expect(invitation).toHaveAttribute(
    "href",
    /http:\/\/127\.0\.0\.1:3200\/\?ref=ref_9007199254740993&ref_expires=[0-9]+/,
  );
  const expiry = new URL(
    (await invitation.getAttribute("href"))!,
  ).searchParams.get("ref_expires");
  await page.goto("http://127.0.0.1:3100/?ref=ref_800");
  await page.getByRole("link", { name: "Личный кабинет", exact: true }).click();
  await page.reload();
  await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => JSON.parse(localStorage.getItem("sunrise.referral.v1")!).expiresAt,
    ),
  ).toBe(Number(expiry));
  await page
    .getByLabel("Email", { exact: true })
    .fill("synthetic@example.test");
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Получить код", exact: true }).click();
  await page.getByLabel("Код из письма", { exact: true }).fill("123456");
  await page
    .getByRole("button", { name: "Войти в кабинет", exact: true })
    .click();
  await expect
    .poll(
      () =>
        writes.find((entry) => entry.path === "/auth/email/verify")?.body
          .referral_code,
    )
    .toBe("ref_9007199254740993");
});

test("website Telegram and Mini App registration receive the same persisted referral", async ({
  page,
}) => {
  const writes = await syntheticRoutes(page);
  await page.goto("/?ref=ref_700");
  await page.getByRole("checkbox").check();
  await page
    .getByRole("button", { name: "Войти через Telegram", exact: true })
    .click();
  await expect
    .poll(
      () =>
        writes.find((entry) => entry.path === "/auth/telegram/start")?.body
          .referral_code,
    )
    .toBe("ref_700");
});

test("Mini App retry after explicit legal consent keeps referral without storing signed initData", async ({
  page,
}) => {
  const writes = await syntheticRoutes(page, true);
  await page.goto("/?ref=ref_800");
  await page.getByRole("checkbox").check();
  await page
    .getByRole("button", { name: "Продолжить в Telegram", exact: true })
    .click();
  await expect
    .poll(
      () =>
        writes.filter((entry) => entry.path === "/auth/telegram/miniapp")
          .length,
    )
    .toBe(2);
  expect(
    writes
      .filter((entry) => entry.path === "/auth/telegram/miniapp")
      .every((entry) => entry.body.referral_code === "ref_800"),
  ).toBe(true);
  expect(
    await page.evaluate(() => JSON.stringify({ ...localStorage })),
  ).not.toContain("synthetic-init-data");
});
