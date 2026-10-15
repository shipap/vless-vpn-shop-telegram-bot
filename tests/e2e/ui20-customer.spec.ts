import { expect, test, type Page } from "@playwright/test";
import { syntheticInstance } from "./instance-fixture";

async function fixture(
  page: Page,
  options: { deletion?: boolean; dismissFailure?: boolean } = {},
) {
  let dismissed = false;
  const account = {
    id: "9007199254740993123",
    email: null,
    email_verified: false,
    telegram_id: "700000001",
  };
  await page.route("https://telegram.org/**", (route) =>
    route.fulfill({ body: "", contentType: "application/javascript" }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
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
        products: [],
        periods: [],
        content: {
          help_center: {
            custom_faq: [
              {
                id: "local",
                question: "Как подключиться?",
                answer: "Откройте инструкцию подключения.",
              },
            ],
          },
        },
        legal: { configured: false, documents: [] },
      };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [
          {
            id: "own-sub",
            product_id: "local",
            name: "Моя подписка",
            status: "active",
            expires_at: "2027-01-01T00:00:00Z",
            connection_url: "https://vpn.example.test/synthetic",
            devices: [],
            devices_limit: 2,
            segments: [],
            recurring: null,
            panel_sync_state: "stale",
            observed_at: "2026-10-13T00:00:00Z",
          },
        ],
        payments: [],
        bonus_balance: "0",
        credit_balance: "0",
        credit_available: "0",
        recurring_enrollment_enabled: false,
        subscription_deletion_notices:
          options.deletion && !dismissed
            ? [
                {
                  subscription_id: "deleted-own-sub",
                  name: "Удалённая подписка",
                  deleted_at: "2026-10-14T00:00:00Z",
                },
              ]
            : [],
      };
    if (path === "/nodes")
      json = { items: [], source: "panel", state: "unknown" };
    if (path === "/subscriptions/deleted-own-sub/deletion-notice/dismiss") {
      expect(route.request().method()).toBe("POST");
      expect(route.request().postDataJSON()).toEqual({});
      expect(route.request().headers()["x-csrf-token"]).toBe("synthetic");
      if (options.dismissFailure)
        return route.fulfill({
          status: 503,
          json: {
            error: {
              code: "synthetic_unavailable",
              message: "Не удалось сохранить. Попробуйте ещё раз.",
            },
          },
        });
      dismissed = true;
      json = { dismissed: true };
    }
    if (path === "/legal" || path === "/legal/status")
      json = { configured: false, accepted: true, documents: [], missing: [] };
    if (path === "/notifications")
      json = {
        items: [
          {
            id: "own-notice",
            kind: "security",
            title: "Новый вход",
            body: "Проверьте свои сессии",
            severity: "info",
            created_at: "2026-10-14T00:00:00Z",
            read_at: null,
            target: "/#account",
          },
        ],
        unread_count: 1,
      };
    if (path === "/notifications/stream")
      return route.fulfill({ contentType: "text/event-stream", body: "" });
    if (path === "/notifications/preferences")
      json = { browser_enabled: false, push_subscriptions: [] };
    await route.fulfill({ json });
  });
}

for (const width of [390, 1440]) {
  test(`notification popover stays under the bell and leaves background usable at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await fixture(page);
    await page.goto("/#home");
    const bell = page.getByRole("button", { name: /^Уведомления,/ });
    await bell.click();
    const panel = page.getByRole("dialog", {
      name: "Уведомления",
      exact: true,
    });
    await expect(panel).toContainText("Новый вход");
    expect(await panel.evaluate((element) => element.matches(":modal"))).toBe(
      false,
    );
    const box = (await panel.boundingBox())!;
    const anchor = (await bell.boundingBox())!;
    expect(box.y).toBeGreaterThanOrEqual(anchor.y + anchor.height);
    expect(box.width).toBeLessThanOrEqual(440);
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(box.height).toBeLessThanOrEqual(560);
    expect(
      await page.evaluate(() => getComputedStyle(document.body).overflow),
    ).not.toBe("hidden");
    await bell.click();
    await expect(panel).toHaveCount(0);
    await bell.click();
    await expect(panel).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(panel).toHaveCount(0);
    await expect(bell).toHaveAttribute("aria-expanded", "false");
    await expect(bell).toBeFocused();
    await bell.click();
    await page.mouse.click(4, 880);
    await expect(panel).toHaveCount(0);
  });
}

test("stale subscription notice has a gap before the connection buttons", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/#home");
  const notice = page.locator(".subscription-main .notice.warning");
  await expect(notice).toContainText("Показаны последние сохранённые данные");
  const bottom = (await notice.boundingBox())!;
  const actions = (await page.locator(".subscription-actions").boundingBox())!;
  expect(actions.y - bottom.y - bottom.height).toBeGreaterThanOrEqual(12);
});

test("archive notice dismissal survives reload after the owned server acknowledgement", async ({
  page,
}) => {
  await fixture(page, { deletion: true });
  await page.goto("/#home");
  await expect(
    page.getByText("Удалённая подписка", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Скрыть уведомление об удалении подписки" })
    .click();
  await expect(
    page.getByText("Удалённая подписка", { exact: true }),
  ).toHaveCount(0);
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Моя подписка", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Удалённая подписка", { exact: true }),
  ).toHaveCount(0);
});

test("failed archive notice dismissal keeps the notice and shows an error", async ({
  page,
}) => {
  await fixture(page, { deletion: true, dismissFailure: true });
  await page.goto("/#home");
  await page
    .getByRole("button", { name: "Скрыть уведомление об удалении подписки" })
    .click();
  await expect(
    page.getByText("Удалённая подписка", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Не удалось сохранить. Попробуйте ещё раз.", {
      exact: true,
    }),
  ).toBeVisible();
});

test("published FAQ starts collapsed and can be opened and closed", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/#help");
  const question = page.locator(".service-faq details").first();
  const answer = page.getByText("Откройте инструкцию подключения.", {
    exact: true,
  });
  await expect(answer).toBeHidden();
  await question.locator("summary").click();
  await expect(answer).toBeVisible();
  await question.locator("summary").click();
  await expect(answer).toBeHidden();
});
