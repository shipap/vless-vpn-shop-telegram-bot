import { test, expect } from "@playwright/test";

test("bonus page copies distinct safe website and Telegram invitation links", async ({
  page,
}) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.addInitScript(() => {
    const copied: string[] = [];
    (
      window as unknown as { copiedReferralLinks: string[] }
    ).copiedReferralLinks = copied;
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: async (text: string) => {
          copied.push(text);
        },
      },
    });
  });
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}}",
    }),
  );
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const account = {
      id: "9007199254740993123",
      email: "synthetic@example.test",
      email_verified: true,
    };
    let json: unknown = {
      documents: [],
      configured: false,
      local_test: true,
      turnstile_test_bypass: true,
    };
    if (path.endsWith("/promos/active")) json = null;
    if (path.endsWith("/auth/me"))
      json = {
        account,
        csrf_token: "synthetic",
        is_admin: false,
        mfa_verified: false,
      };
    if (path.endsWith("/catalog"))
      json = {
        products: [],
        periods: [],
        content: {},
        legal: { configured: false, documents: [] },
      };
    if (path.endsWith("/dashboard"))
      json = {
        account,
        subscriptions: [],
        payments: [],
        bonus_balance: "0",
        bonus_reserved: "0",
        credit_balance: "0",
        credit_available: "0",
        credit_reserved: "0",
      };
    if (path.endsWith("/account/bonuses"))
      json = { referral_url: null, ledger: [] };
    if (path.endsWith("/referral"))
      json = {
        referral_url:
          "https://t.me/synthetic_bot?start=ref_9007199254740993123",
        website_referral_url:
          "https://example.test/?ref=ref_9007199254740993123",
        referral_code: "ref_9007199254740993123",
        reward_percent: "15",
        invited_count: 0,
        rewarded_amount: "0",
        currency: "RUB",
        enrollment_available: true,
      };
    return route.fulfill({ json });
  });
  await page.goto("/#bonuses");
  await expect(
    page.getByRole("button", {
      name: "Скопировать ссылку на сайт",
      exact: true,
    }),
  ).toBeVisible({ timeout: 5000 });
  expect(pageErrors).toEqual([]);
  await page
    .getByRole("button", { name: "Скопировать ссылку на сайт", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Скопировать ссылку в Telegram", exact: true })
    .click();
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { copiedReferralLinks: string[] })
          .copiedReferralLinks,
    ),
  ).toEqual([
    "https://example.test/?ref=ref_9007199254740993123",
    "https://t.me/synthetic_bot?start=ref_9007199254740993123",
  ]);
  await page.setViewportSize({ width: 360, height: 800 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
