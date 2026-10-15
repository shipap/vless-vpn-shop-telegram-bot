import { expect, test, type Page } from "@playwright/test";
import { syntheticInstance } from "./instance-fixture";

const targetEmail = "existing-synthetic@example.test";
const telegramId = "9007199254740993222";
type Write = { path: string; body: Record<string, unknown> };

async function fixture(
  page: Page,
  options: {
    existingEmail?: boolean;
    unusedEmail?: boolean;
    differentEmail?: boolean;
    freshRequired?: boolean;
    missingProof?: boolean;
  } = {},
) {
  const writes: Write[] = [];
  const linkCsrf: (string | undefined)[] = [];
  let emailLogin = false,
    linked = false,
    changedEmail = false,
    emailConfirmed = false;
  const account = () => ({
    id: emailLogin ? "9007199254740993999" : "9007199254740993123",
    email: emailLogin
      ? options.differentEmail
        ? "different-synthetic@example.test"
        : targetEmail
      : changedEmail
        ? targetEmail
        : options.existingEmail
          ? "current-synthetic@example.test"
          : null,
    email_verified:
      emailLogin || changedEmail || Boolean(options.existingEmail),
    telegram_id: emailLogin && !linked ? null : telegramId,
  });
  const session = () => ({
    account: account(),
    csrf_token: emailLogin ? "synthetic-email-u22" : "synthetic-telegram-u22",
    is_admin: false,
    mfa_verified: false,
    staff_role: null,
    capabilities: [],
  });
  const legal = {
    configured: true,
    documents: [
      {
        document_type: "offer",
        version: "synthetic-u22",
        url: "https://example.test/offer",
      },
      {
        document_type: "privacy",
        version: "synthetic-u22",
        url: "https://example.test/privacy",
      },
    ],
  };
  await page.route("https://telegram.org/**", (route) =>
    route.fulfill({
      body: "window.Telegram={WebApp:{initData:''}}",
      contentType: "application/javascript",
    }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const method = route.request().method();
    const body = method !== "GET" ? route.request().postDataJSON() || {} : {};
    if (method !== "GET") writes.push({ path, body });
    let json: unknown = { items: [], total: 0 };
    if (path === "/auth/me") json = session();
    if (path === "/public-config")
      json = {
        instance: syntheticInstance,
        local_test: true,
        turnstile_test_bypass: true,
        telegram_login_enabled: true,
        bot_url: "https://t.me/synthetic_u23_bot",
      };
    if (path === "/catalog")
      json = { products: [], periods: [], content: {}, legal };
    if (path === "/legal") json = legal;
    if (path === "/legal/status")
      json = { ...legal, accepted: true, missing: [] };
    if (path === "/notifications") json = { items: [], unread_count: 0 };
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
    if (path === "/account/identity/status")
      json = { account: account(), operations: [] };
    if (path === "/account/email/request") {
      if (!options.unusedEmail && !options.existingEmail)
        return route.fulfill({
          status: 409,
          json: {
            error: {
              code: "email_identity_conflict",
              message: "Почта связана с другим аккаунтом.",
            },
          },
        });
      json = {
        operation_id: "synthetic-operation",
        stage: options.existingEmail ? "current_email" : "new_email",
        challenge_id: options.existingEmail
          ? "synthetic-current"
          : "synthetic-new",
        retry_after: 120,
        expires_at: "2099-01-01T00:00:00Z",
      };
    }
    if (path === "/account/email/verify") {
      if (body.challenge_id === "synthetic-current")
        json = {
          operation_id: "synthetic-operation",
          stage: "new_email",
          challenge_id: "synthetic-new",
          retry_after: 120,
          expires_at: "2099-01-01T00:00:00Z",
        };
      else {
        changedEmail = true;
        json = {
          operation_id: "synthetic-operation",
          stage: "completed",
          expires_at: null,
        };
      }
    }
    if (path === "/auth/email/request")
      json = { challenge_id: "synthetic-login", resend_after: 120 };
    if (path === "/auth/email/verify") {
      if (body.code !== "654321")
        return route.fulfill({
          status: 400,
          json: {
            error: { code: "code_invalid", message: "Код неверен или истёк." },
          },
        });
      emailLogin = true;
      json = session();
    }
    if (path === "/account/link/request") {
      linkCsrf.push(route.request().headers()["x-csrf-token"]);
      if (options.freshRequired && !emailConfirmed)
        return route.fulfill({
          status: 403,
          json: {
            error: {
              code: "fresh_login_required",
              message: "Подтвердите почту для привязки.",
            },
          },
        });
      json = {
        ...(options.missingProof ? {} : { proof: "synthetic-u22-proof" }),
        expires_in: 300,
        requires_merge: true,
      };
    }
    if (path === "/account/link/email/request")
      json = {
        challenge_id: "synthetic-fresh",
        resend_after: 120,
        expires_in: 600,
      };
    if (path === "/account/link/email/verify") {
      emailConfirmed = true;
      json = { confirmed: true, expires_in: 300 };
    }
    if (path === "/account/telegram/unlink")
      json = {
        operation_id: "synthetic-unlink",
        stage: "awaiting_telegram",
        expires_at: "2099-01-01T00:00:00Z",
        proof: "synthetic-unlink-proof",
      };
    if (
      method !== "GET" &&
      ![
        "/account/email/request",
        "/account/email/verify",
        "/auth/email/request",
        "/auth/email/verify",
        "/account/link/request",
        "/account/link/email/request",
        "/account/link/email/verify",
        "/account/telegram/unlink",
      ].includes(path)
    )
      return route.fulfill({
        status: 403,
        json: {
          error: {
            code: "synthetic_read_only",
            message: "Действие запрещено синтетической проверкой.",
          },
        },
      });
    await route.fulfill({ json });
  });
  await page.goto("/#account");
  return {
    writes,
    linkCsrf,
    confirmBot: () => {
      linked = true;
    },
  };
}

async function offer(page: Page) {
  await page
    .getByRole("button", { name: "Добавить почту", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Новая почта", { exact: true }).fill(targetEmail);
  await dialog
    .getByRole("button", { name: "Отправить код", exact: true })
    .click();
  const action = dialog.getByRole("button", {
    name: "Войти по этой почте и связать аккаунты",
    exact: true,
  });
  await expect(action).toBeVisible();
  return action;
}

async function emailLogin(page: Page) {
  await (await offer(page)).click();
  await completeEmailLogin(page);
}

async function completeEmailLogin(page: Page) {
  await expect(page.getByLabel("Email", { exact: true })).toHaveValue(
    targetEmail,
  );
  await expect(page.getByRole("button", { name: /Telegram/ })).toHaveCount(0);
  await page.getByRole("checkbox").check();
  await page.locator(".login-form button[type=submit]").click();
  await page.locator(".login-code-field input").fill("654321");
  await page.locator(".login-form button[type=submit]").click();
}

test("occupied email keeps canonical Telegram until explicit email login and bot confirmation", async ({
  page,
}) => {
  const { writes, confirmBot, linkCsrf } = await fixture(page);
  await emailLogin(page);
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByLabel("Username или Telegram ID", { exact: true }),
  ).toHaveValue(telegramId);
  expect(writes.map((write) => write.path)).toEqual([
    "/account/email/request",
    "/auth/email/request",
    "/auth/email/verify",
  ]);
  expect(writes[1].body).toEqual({
    email: targetEmail,
    turnstile_token: "local-test",
  });
  expect(writes[2].body).toMatchObject({
    challenge_id: "synthetic-login",
    code: "654321",
    legal_accepted: true,
  });
  await dialog
    .getByRole("button", { name: "Начать подтверждение", exact: true })
    .click();
  await expect(
    dialog.getByRole("button", { name: /Скопировать/ }),
  ).toBeVisible();
  expect(writes.at(-1)).toEqual({
    path: "/account/link/request",
    body: { telegram_id: telegramId },
  });
  expect(linkCsrf).toEqual(["synthetic-email-u22"]);
  await expect(
    page.getByRole("heading", { name: "Telegram подключён", exact: true }),
  ).toHaveCount(0);
  expect(page.url()).not.toContain(targetEmail);
  expect(page.url()).not.toContain("synthetic-u22-proof");
  confirmBot();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(
    page.getByRole("heading", { name: "Telegram подключён", exact: true }),
  ).toBeVisible();
  await expect(dialog).toHaveCount(0);
  expect(writes.some((write) => write.path === "/auth/logout")).toBe(false);
});

test("cancelling target-email login preserves the Telegram session and creates no mail or proof", async ({
  page,
}) => {
  const { writes } = await fixture(page);
  await (await offer(page)).click();
  await page
    .getByRole("button", { name: "Отмена привязки", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Telegram подключён", exact: true }),
  ).toBeVisible();
  expect(writes.map((write) => write.path)).toEqual(["/account/email/request"]);
});

test("mobile conflict, email login and Telegram handoff keep controls inside the viewport", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 900 });
  const { writes } = await fixture(page);
  const action = await offer(page);
  const dialog = page.getByRole("dialog");
  expect(
    await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("conflict-390.png"),
    fullPage: true,
  });
  await action.click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("email-login-390.png"),
    fullPage: true,
  });
  await completeEmailLogin(page);
  await expect(
    dialog.getByLabel("Username или Telegram ID", { exact: true }),
  ).toHaveValue(telegramId);
  expect(
    await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("telegram-link-390.png"),
    fullPage: true,
  });
  expect(writes.some((write) => write.path === "/account/link/request")).toBe(
    false,
  );
});

test("mismatched email OTP response does not open a Telegram merge dialog", async ({
  page,
}) => {
  const { writes } = await fixture(page, { differentEmail: true });
  await emailLogin(page);
  await expect(page.getByRole("alert")).toContainText("Привязка не начата");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(writes.some((write) => write.path === "/account/link/request")).toBe(
    false,
  );
  await expect(page.locator(".account-mini")).toContainText(
    "different-synthetic@example.test",
  );
});

test("missing link proof is rejected instead of exposing an invalid copy command", async ({
  page,
}) => {
  const { writes } = await fixture(page, { missingProof: true });
  await emailLogin(page);
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("button", { name: "Начать подтверждение", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(dialog.getByRole("button", { name: /Скопировать/ })).toHaveCount(
    0,
  );
  expect(writes.at(-1)?.path).toBe("/account/link/request");
});

test("configured bot link contains no proof and expires with manual linking actions", async ({
  page,
}) => {
  await page.clock.install();
  const { writes } = await fixture(page);
  await emailLogin(page);
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("button", { name: "Начать подтверждение", exact: true })
    .click();
  await expect(
    dialog.getByRole("button", { name: /Скопировать/ }),
  ).toBeEnabled();
  const botLink = dialog.getByRole("link", {
    name: "Перейти в бота",
    exact: true,
  });
  await expect(botLink).toHaveAttribute(
    "href",
    "https://t.me/synthetic_u23_bot",
  );
  await expect(botLink).toHaveAttribute("target", "_blank");
  await expect(botLink).toHaveAttribute("rel", "noopener noreferrer");
  expect(await botLink.getAttribute("href")).not.toContain(
    "synthetic-u22-proof",
  );
  await page.clock.runFor(300001);
  await expect(botLink).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: /Скопировать/ }),
  ).toBeDisabled();
  await dialog.getByRole("button", { name: "Закрыть", exact: true }).click();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(
    writes.filter((write) => write.path === "/account/link/request"),
  ).toHaveLength(1);
});

test("stale email-session proof is confirmed inline before retrying the exact prior Telegram ID", async ({
  page,
}) => {
  const { writes } = await fixture(page, { freshRequired: true });
  await emailLogin(page);
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("button", { name: "Начать подтверждение", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "Подтвердить почту", exact: true })
    .click();
  await expect(
    dialog.getByRole("button", { name: /Повторить через/ }),
  ).toBeDisabled();
  await dialog
    .getByLabel("Код подтверждения почты", { exact: true })
    .fill("654321");
  await dialog
    .getByRole("button", { name: "Подтвердить и продолжить", exact: true })
    .click();
  await expect(
    dialog.getByRole("button", { name: /Скопировать/ }),
  ).toBeVisible();
  expect(
    writes
      .filter((write) => write.path === "/account/link/request")
      .map((write) => write.body),
  ).toEqual([{ telegram_id: telegramId }, { telegram_id: telegramId }]);
  expect(
    writes.find((write) => write.path === "/account/link/email/request")?.body,
  ).toEqual({});
});

for (const existingEmail of [false, true])
  test(`${existingEmail ? "email replacement" : "unused email addition"} keeps the original identity operation endpoints`, async ({
    page,
  }) => {
    const { writes } = await fixture(page, {
      existingEmail,
      unusedEmail: true,
    });
    await page
      .getByRole("button", {
        name: existingEmail ? "Изменить почту" : "Добавить почту",
        exact: true,
      })
      .click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Новая почта", { exact: true }).fill(targetEmail);
    await dialog
      .getByRole("button", { name: "Отправить код", exact: true })
      .click();
    if (existingEmail) {
      await dialog
        .getByLabel("Код из текущей почты", { exact: true })
        .fill("123456");
      await dialog
        .getByRole("button", { name: "Подтвердить код", exact: true })
        .click();
    }
    await dialog
      .getByLabel("Код из новой почты", { exact: true })
      .fill("654321");
    await dialog
      .getByRole("button", { name: "Подтвердить код", exact: true })
      .click();
    await expect(dialog).toHaveCount(0);
    expect(
      writes.every(
        (write) =>
          write.path === "/account/email/request" ||
          write.path === "/account/email/verify",
      ),
    ).toBe(true);
    expect(
      writes.filter((write) => write.path === "/account/email/verify"),
    ).toHaveLength(existingEmail ? 2 : 1);
  });

test("Telegram unlink still requires explicit acknowledgement and its original endpoint", async ({
  page,
}) => {
  const { writes } = await fixture(page, { existingEmail: true });
  await page
    .getByRole("button", { name: "Отвязать Telegram", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("button", { name: "Начать отвязку", exact: true }),
  ).toBeDisabled();
  await dialog.getByRole("checkbox").check();
  await dialog
    .getByRole("button", { name: "Начать отвязку", exact: true })
    .click();
  await expect(dialog).toContainText(
    "Подтвердите отвязку в связанном Telegram-аккаунте",
  );
  expect(writes).toEqual([{ path: "/account/telegram/unlink", body: {} }]);
});
