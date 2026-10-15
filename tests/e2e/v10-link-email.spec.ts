import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";

async function linkPage(
  page: Page,
  options: {
    fresh?: boolean;
    sendError?: string;
    verifyError?: string;
    retryError?: boolean;
    identity?: string;
  } = {},
) {
  const writes: { path: string; body: unknown }[] = [];
  let confirmed = options.fresh ?? false;
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}}",
    }),
  );
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const method = route.request().method();
    if (method !== "GET")
      writes.push({ path, body: route.request().postDataJSON() });
    const account = {
      id: "9007199254740993123",
      email: "synthetic@example.test",
      email_verified: true,
      telegram_id: null,
    };
    if (path === "/auth/me")
      return route.fulfill({
        json: {
          account,
          csrf_token: "synthetic",
          is_admin: false,
          mfa_verified: false,
        },
      });
    if (path === "/dashboard")
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
        },
      });
    if (path === "/catalog")
      return route.fulfill({
        json: {
          products: [],
          periods: [],
          content: {},
          legal: { configured: false, documents: [] },
        },
      });
    if (path === "/promos/active") return route.fulfill({ json: null });
    if (path === "/account/link/request")
      return route.fulfill(
        confirmed
          ? options.retryError
            ? {
                status: 400,
                json: {
                  error: {
                    code: "link_target_unavailable",
                    message: "Начните диалог с ботом и повторите привязку.",
                  },
                },
              }
            : {
                json: {
                  proof: "synthetic-proof",
                  expires_in: 300,
                  requires_merge: true,
                },
              }
          : {
              status: 403,
              json: {
                error: {
                  code: "fresh_login_required",
                  message: "Подтвердите email для привязки.",
                },
              },
            },
      );
    if (path === "/account/link/email/request")
      return route.fulfill(
        options.sendError
          ? {
              status: options.sendError === "auth_required" ? 401 : 503,
              json: {
                error: {
                  code: options.sendError,
                  message:
                    options.sendError === "auth_required"
                      ? "Войдите в аккаунт."
                      : "Письмо временно не отправляется.",
                },
              },
            }
          : {
              json: {
                challenge_id: `synthetic-challenge-${writes.filter((write) => write.path === path).length}`,
                resend_after: 120,
                expires_in: 600,
              },
            },
      );
    if (path === "/account/link/email/verify") {
      if (
        options.verifyError ||
        route.request().postDataJSON().code !== "654321"
      )
        return route.fulfill({
          status: options.verifyError === "auth_required" ? 401 : 400,
          json: {
            error: {
              code: options.verifyError ?? "code_invalid",
              message:
                options.verifyError === "auth_required"
                  ? "Войдите в аккаунт."
                  : "Код неверен или истёк.",
            },
          },
        });
      confirmed = true;
      return route.fulfill({ json: { confirmed: true, expires_in: 300 } });
    }
    return route.fulfill({
      json: {
        local_test: true,
        turnstile_test_bypass: true,
        documents: [],
        configured: false,
      },
    });
  });
  await page.goto("/#account");
  await page
    .getByRole("button", { name: "Связать Telegram", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Username или Telegram ID", { exact: true })
    .fill(options.identity ?? "@Synthetic_User");
  await dialog
    .getByRole("button", { name: "Начать подтверждение", exact: true })
    .click();
  return { writes, dialog };
}

test("stale link proof is confirmed by email inline and retries the exact identity without logout", async ({
  page,
}) => {
  const { writes, dialog } = await linkPage(page);
  await expect(
    dialog.getByRole("button", { name: "Подтвердить почту", exact: true }),
  ).toBeVisible({ timeout: 5000 });
  expect(writes).toEqual([
    {
      path: "/account/link/request",
      body: { telegram_username: "Synthetic_User" },
    },
  ]);
  await dialog
    .getByRole("button", { name: "Подтвердить почту", exact: true })
    .click();
  await expect(dialog.getByText(/Спам/)).toBeVisible();
  await expect(dialog).toContainText("synthetic@example.test");
  await expect(
    dialog.getByLabel("Username или Telegram ID", { exact: true }),
  ).toHaveValue("@Synthetic_User");
  await dialog
    .getByLabel("Код подтверждения почты", { exact: true })
    .fill("654321");
  await dialog
    .getByRole("button", { name: "Подтвердить и продолжить", exact: true })
    .click();
  await expect(
    dialog.getByRole("button", { name: /Скопировать/ }),
  ).toBeVisible();
  expect(writes).toEqual([
    {
      path: "/account/link/request",
      body: { telegram_username: "Synthetic_User" },
    },
    { path: "/account/link/email/request", body: {} },
    {
      path: "/account/link/email/verify",
      body: { challenge_id: "synthetic-challenge-1", code: "654321" },
    },
    {
      path: "/account/link/request",
      body: { telegram_username: "Synthetic_User" },
    },
  ]);
  expect(new URL(page.url()).hash).toBe("#account");
});

test("wrong email code clears the input for retry and resend waits for the server deadline", async ({
  page,
}) => {
  await page.clock.install();
  const { writes, dialog } = await linkPage(page);
  await dialog
    .getByRole("button", { name: "Подтвердить почту", exact: true })
    .click();
  const code = dialog.getByLabel("Код подтверждения почты", { exact: true });
  await code.fill("111111");
  await dialog
    .getByRole("button", { name: "Подтвердить и продолжить", exact: true })
    .click();
  await expect(
    dialog.getByText("Код неверен или истёк.", { exact: true }),
  ).toBeVisible();
  await expect(code).toHaveValue("");
  await expect(
    dialog.getByRole("button", { name: /Повторить через/ }),
  ).toBeDisabled();
  await page.clock.runFor(120001);
  await dialog
    .getByRole("button", { name: "Отправить код ещё раз", exact: true })
    .click();
  await expect(
    dialog.getByText("Код неверен или истёк.", { exact: true }),
  ).toHaveCount(0);
  await code.fill("654321");
  await dialog
    .getByRole("button", { name: "Подтвердить и продолжить", exact: true })
    .click();
  await expect(
    dialog.getByRole("button", { name: /Скопировать/ }),
  ).toBeVisible();
  expect(
    writes
      .filter((write) => write.path === "/account/link/email/verify")
      .slice(-1)[0]?.body,
  ).toEqual({ challenge_id: "synthetic-challenge-2", code: "654321" });
});

for (const sendError of ["mail_unavailable", "auth_required"]) {
  test(`inline email send error ${sendError} preserves the dialog and typed identity`, async ({
    page,
  }) => {
    const { writes, dialog } = await linkPage(page, { sendError });
    await dialog
      .getByRole("button", { name: "Подтвердить почту", exact: true })
      .click();
    await expect(dialog.getByRole("alert")).toBeVisible();
    await expect(
      dialog.getByLabel("Username или Telegram ID", { exact: true }),
    ).toHaveValue("@Synthetic_User");
    await expect(
      dialog.getByLabel("Код подтверждения почты", { exact: true }),
    ).toHaveCount(0);
    expect(writes.some((write) => write.path === "/auth/logout")).toBe(false);
    await expect(
      dialog.getByRole("button", { name: "Подтвердить почту", exact: true }),
    ).toBeEnabled();
  });
}

test("normal fresh linking does not send an email challenge", async ({
  page,
}) => {
  const { writes, dialog } = await linkPage(page, { fresh: true });
  await expect(
    dialog.getByRole("button", { name: /Скопировать/ }),
  ).toBeVisible();
  expect(writes).toHaveLength(1);
});

test("expired inline email code requires a new challenge and retains BIGINT identity", async ({
  page,
}) => {
  await page.clock.install();
  const { writes, dialog } = await linkPage(page, {
    identity: "9007199254740993123",
  });
  await dialog
    .getByRole("button", { name: "Подтвердить почту", exact: true })
    .click();
  await page.clock.runFor(600001);
  await expect(
    dialog.getByText("Код истёк. Запросите новое письмо.", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", {
      name: "Подтвердить и продолжить",
      exact: true,
    }),
  ).toBeDisabled();
  await dialog
    .getByRole("button", { name: "Отправить код ещё раз", exact: true })
    .click();
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
  ).toEqual([
    { telegram_id: "9007199254740993123" },
    { telegram_id: "9007199254740993123" },
  ]);
});

test("expired session during inline verification clears the code without logging out", async ({
  page,
}) => {
  const { writes, dialog } = await linkPage(page, {
    verifyError: "auth_required",
  });
  await dialog
    .getByRole("button", { name: "Подтвердить почту", exact: true })
    .click();
  await dialog
    .getByLabel("Код подтверждения почты", { exact: true })
    .fill("654321");
  await dialog
    .getByRole("button", { name: "Подтвердить и продолжить", exact: true })
    .click();
  await expect(
    dialog.getByText("Войдите в аккаунт.", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByLabel("Код подтверждения почты", { exact: true }),
  ).toHaveValue("");
  expect(
    writes.filter((write) => write.path === "/account/link/request"),
  ).toHaveLength(1);
  expect(writes.some((write) => write.path === "/auth/logout")).toBe(false);
});

test("link retry failure restores editable identity without sending another email", async ({
  page,
}) => {
  const { writes, dialog } = await linkPage(page, { retryError: true });
  await dialog
    .getByRole("button", { name: "Подтвердить почту", exact: true })
    .click();
  await dialog
    .getByLabel("Код подтверждения почты", { exact: true })
    .fill("654321");
  await dialog
    .getByRole("button", { name: "Подтвердить и продолжить", exact: true })
    .click();
  await expect(
    dialog.getByText("Начните диалог с ботом и повторите привязку.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    dialog.getByLabel("Username или Telegram ID", { exact: true }),
  ).toHaveValue("@Synthetic_User");
  await expect(
    dialog.getByLabel("Username или Telegram ID", { exact: true }),
  ).toBeEnabled();
  expect(
    writes.filter((write) => write.path === "/account/link/email/request"),
  ).toHaveLength(1);
});

test("inline link confirmation remains accessible and fits mobile and desktop", async ({
  page,
}) => {
  await mkdir(".artifacts/ui/v10-link-email", { recursive: true });
  for (const width of [360, 390, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const { dialog } = await linkPage(page);
    await dialog
      .getByRole("button", { name: "Подтвердить почту", exact: true })
      .click();
    await expect(
      dialog.getByLabel("Код подтверждения почты", { exact: true }),
    ).toBeFocused();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const audit = await new AxeBuilder({ page }).analyze();
    expect(
      audit.violations.filter((item) =>
        ["serious", "critical"].includes(item.impact ?? ""),
      ),
    ).toEqual([]);
    await page.screenshot({
      path: `.artifacts/ui/v10-link-email/confirm-${width}.png`,
      fullPage: true,
    });
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
  }
});
