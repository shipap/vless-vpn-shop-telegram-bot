import { syntheticInstance } from "./instance-fixture";
import { expect, test, type Page } from "@playwright/test";
test.beforeEach(async ({ page }) => {
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}};",
    }),
  );
});

async function identityFixture(page: Page, initiallyLinked = false) {
  let linked = initiallyLinked;
  let logoutCalls = 0;
  const account = () => ({
    id: "9007199254740993123",
    email: "synthetic@example.test",
    email_verified: true,
    telegram_id: linked ? "9007199254740993999" : null,
    telegram_username: linked ? "synthetic_user" : null,
  });
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account: account(),
        csrf_token: "synthetic",
        is_admin: false,
        mfa_verified: false,
        staff_role: null,
        capabilities: [],
      };
    if (path === "/public-config")
      json = {
        instance: syntheticInstance,
        local_test: true,
        turnstile_test_bypass: true,
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
        account: account(),
        subscriptions: [],
        payments: [],
        bonus_balance: "0",
        bonus_reserved: "0",
        credit_balance: "0",
        credit_available: "0",
        credit_reserved: "0",
      };
    if (path === "/legal/status")
      json = { accepted: true, configured: false, missing: [], documents: [] };
    if (path === "/account/link/request")
      json = {
        proof: "synthetic-proof",
        expires_in: 300,
        requires_merge: false,
      };
    if (path === "/auth/logout") logoutCalls++;
    await route.fulfill({ json });
  });
  return {
    confirm: () => {
      linked = true;
    },
    logoutCalls: () => logoutCalls,
  };
}

test("pending owner review remains visible after reload without exposing proof or sending email", async ({
  page,
}) => {
  await identityFixture(page, true);
  let emailRequests = 0;
  await page.route("**/api/v1/account/identity/status", (route) =>
    route.fulfill({
      json: {
        account: { telegram_id: "9007199254740993999" },
        operations: [
          {
            operation_id: "synthetic-review",
            stage: "owner_review",
            expires_at: "2099-01-01T00:00:00Z",
          },
        ],
      },
    }),
  );
  await page.route("**/api/v1/account/link/email/request", (route) => {
    emailRequests++;
    return route.fulfill({ json: {} });
  });
  await page.goto("/#account");
  await expect(page.locator("main")).toContainText("Ожидает решения владельца");
  await page.reload();
  await expect(page.locator("main")).toContainText("Ожидает решения владельца");
  expect(emailRequests).toBe(0);
  await expect(page.locator("main")).not.toContainText("/unlink");
});

test("linked canonical account shows its identity without inviting another Telegram link", async ({
  page,
}) => {
  await identityFixture(page, true);
  await page.goto("/#account");
  await expect(
    page.getByRole("heading", { name: "Telegram подключён" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Связать Telegram" }),
  ).toHaveCount(0);
  await expect(page.locator("main")).toContainText("9007199254740993999");
});

test("bot confirmation refreshes canonical identity without logout or losing the proof dialog", async ({
  page,
}) => {
  const fixture = await identityFixture(page);
  await page.goto("/#account");
  await page.getByRole("button", { name: "Связать Telegram" }).click();
  await page.getByLabel("Username или Telegram ID").fill("@synthetic_user");
  await page.getByRole("button", { name: "Начать подтверждение" }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("button", { name: "Проверить привязку" }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Проверить привязку" }).click();
  await expect(dialog).toBeVisible();
  fixture.confirm();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(
    page.getByRole("heading", { name: "Telegram подключён" }),
  ).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(fixture.logoutCalls()).toBe(0);
});

test("email change verifies current and new mailbox with separate challenges before canonical refresh", async ({
  page,
}) => {
  await identityFixture(page);
  const verification: unknown[] = [];
  await page.route("**/api/v1/account/email/request", (route) =>
    route.fulfill({
      json: {
        operation_id: "synthetic-email-operation",
        stage: "current_email",
        challenge_id: "synthetic-current",
        retry_after: 120,
        expires_at: "2099-01-01T00:00:00Z",
      },
    }),
  );
  await page.route("**/api/v1/account/email/verify", (route) => {
    const payload = route.request().postDataJSON();
    verification.push(payload);
    return route.fulfill({
      json:
        payload.challenge_id === "synthetic-current"
          ? {
              operation_id: "synthetic-email-operation",
              stage: "new_email",
              challenge_id: "synthetic-new",
              retry_after: 120,
              expires_at: "2099-01-01T00:00:00Z",
            }
          : {
              operation_id: "synthetic-email-operation",
              stage: "completed",
              expires_at: null,
            },
    });
  });
  await page.goto("/#account");
  await page.getByRole("button", { name: "Изменить почту" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Новая почта").fill("new-synthetic@example.test");
  await dialog.getByRole("button", { name: "Отправить код" }).click();
  await dialog
    .getByRole("textbox", { name: "Код из текущей почты" })
    .fill("123456");
  await dialog.getByRole("button", { name: "Подтвердить код" }).click();
  const next = dialog.getByRole("textbox", { name: "Код из новой почты" });
  await expect(next).toHaveValue("");
  await next.fill("654321");
  await dialog.getByRole("button", { name: "Подтвердить код" }).click();
  await expect(dialog).toHaveCount(0);
  expect(verification).toEqual([
    {
      operation_id: "synthetic-email-operation",
      challenge_id: "synthetic-current",
      code: "123456",
    },
    {
      operation_id: "synthetic-email-operation",
      challenge_id: "synthetic-new",
      code: "654321",
    },
  ]);
});

test("sessions identify current device and confirm only the selected session revocation", async ({
  page,
}) => {
  await identityFixture(page);
  let revoked = false;
  await page.route("**/api/v1/account/sessions", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: "synthetic-current-session",
            current: true,
            auth_method: "email",
            created_at: "2026-10-08T00:00:00Z",
            last_seen_at: "2026-10-08T01:00:00Z",
            expires_at: "2099-01-01T00:00:00Z",
            ip_address: "192.0.2.1",
            browser_label: "Текущий браузер",
          },
          ...(!revoked
            ? [
                {
                  id: "synthetic-other-session",
                  current: false,
                  auth_method: "telegram",
                  created_at: "2026-10-07T00:00:00Z",
                  last_seen_at: "2026-10-07T01:00:00Z",
                  expires_at: "2099-01-01T00:00:00Z",
                  ip_address: "192.0.2.2",
                  browser_label: "Другой браузер",
                },
              ]
            : []),
        ],
      },
    }),
  );
  await page.route(
    "**/api/v1/account/sessions/synthetic-other-session/revoke",
    (route) => {
      revoked = true;
      return route.fulfill({
        json: { revoked: true, current_session_revoked: false },
      });
    },
  );
  await page.goto("/#account");
  await expect(page.locator("main")).toContainText("Это устройство");
  const other = page.getByRole("article").filter({ hasText: "Другой браузер" });
  await other.getByRole("button", { name: "Завершить сеанс" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Другой браузер");
  await dialog.getByRole("button", { name: "Завершить сеанс" }).click();
  await expect(other).toHaveCount(0);
  await expect(page.locator("main")).toContainText("Это устройство");
});

test("owner fallback retries the same appeal after inline email proof without starting Telegram unlink", async ({
  page,
}) => {
  await identityFixture(page, true);
  let appeals = 0;
  let unlink = 0;
  await page.route("**/api/v1/account/telegram/unlink", (route) => {
    unlink++;
    return route.fulfill({
      json: {
        operation_id: "incorrect-branch",
        stage: "awaiting_telegram",
        expires_at: "2099-01-01T00:00:00Z",
        proof: "synthetic",
      },
    });
  });
  await page.route("**/api/v1/account/telegram/unlink-request", (route) => {
    appeals++;
    return appeals === 1
      ? route.fulfill({
          status: 403,
          json: {
            error: {
              code: "fresh_login_required",
              message: "Подтвердите почту",
            },
          },
        })
      : route.fulfill({
          json: {
            operation_id: "synthetic-review",
            stage: "owner_review",
            expires_at: "2099-01-01T00:00:00Z",
          },
        });
  });
  await page.route("**/api/v1/account/link/email/request", (route) =>
    route.fulfill({
      json: {
        challenge_id: "synthetic-current-proof",
        resend_after: 120,
        expires_in: 600,
      },
    }),
  );
  await page.route("**/api/v1/account/link/email/verify", (route) =>
    route.fulfill({ json: { confirmed: true, expires_in: 300 } }),
  );
  await page.goto("/#account");
  await page.getByRole("button", { name: "Отвязать Telegram" }).click();
  await page
    .getByText("Нет доступа к связанному Telegram", { exact: true })
    .click();
  await page
    .getByLabel("Причина обращения")
    .fill("Нет доступа к прежнему Telegram-аккаунту");
  await page
    .getByRole("button", { name: "Отправить обращение владельцу" })
    .click();
  await page
    .getByRole("button", { name: "Подтвердить почту", exact: true })
    .click();
  await page
    .getByRole("textbox", { name: "Код подтверждения почты" })
    .fill("123456");
  await page.getByRole("button", { name: "Подтвердить и продолжить" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "Обращение передано владельцу",
  );
  expect(appeals).toBe(2);
  expect(unlink).toBe(0);
});
