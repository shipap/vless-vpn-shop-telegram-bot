import { expect, test, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { ownerStaffFixture, adminStaffFixture } from "./staff-fixture";
import { syntheticInstance } from "./instance-fixture";

async function setup(page: Page, section: string, owner = true) {
  const writes: string[] = [];
  await page.addInitScript(
    (section) => history.replaceState(null, "", `/admin#${section}`),
    section,
  );
  await page.route("https://telegram.org/**", (route) =>
    route.fulfill({ body: "", contentType: "application/javascript" }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    if (route.request().method() !== "GET") {
      writes.push(path);
      return route.fulfill({
        status: 403,
        json: {
          error: { code: "synthetic_read_only", message: "Только просмотр" },
        },
      });
    }
    let json: unknown = { items: [], total: 0 };
    if (path === "/auth/me")
      json = {
        account: {
          id: "synthetic-owner",
          email: "owner@example.test",
          email_verified: true,
        },
        csrf_token: "synthetic",
        is_admin: true,
        mfa_verified: true,
        ...(owner
          ? {
              ...ownerStaffFixture,
              capabilities: [
                ...ownerStaffFixture.capabilities,
                "device_unlinks_manage",
              ],
            }
          : adminStaffFixture),
      };
    if (path === "/public-config")
      json = { instance: syntheticInstance, local_test: true };
    if (path === "/catalog")
      json = {
        products: [],
        periods: [],
        content: {},
        legal: { configured: false, documents: [] },
      };
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/admin/users")
      json = {
        items: [
          {
            id: "synthetic-user",
            email: "user@example.test",
            telegram_id: "9007199254740993123",
            email_verified: true,
            created_at: "2026-10-14T00:00:00Z",
          },
        ],
        total: 30,
      };
    if (path === "/admin/users/synthetic-user")
      json = {
        account: {
          id: "synthetic-user",
          email: "very-long-synthetic-address-for-layout@example.test",
          bonus_balance: "1234.00",
          cash_balance: "5678.00",
        },
        identities: [{ provider: "telegram", subject: "9007199254740993123" }],
        subscriptions: [
          {
            id: "synthetic-subscription",
            name: "Длинное название выданной услуги для проверки переноса текста в карточке",
            status: "active",
            rights: {
              entitled_until: "2026-11-14T00:00:00Z",
              source: "paid_order",
            },
          },
        ],
        orders: [
          {
            id: "synthetic-paid-order",
            status: "paid",
            total: "199.00",
            currency: "RUB",
          },
        ],
        payments: [
          {
            id: "synthetic-paid-payment",
            status: "paid",
            amount: "199.00",
            currency: "RUB",
          },
        ],
        agreements: [],
        ledger: [],
        devices: [
          {
            id: "synthetic-device",
            name: "Синтетическое устройство с длинным названием для переноса",
            platform: "iOS",
            status: "active",
          },
        ],
        sessions: [
          { id: "synthetic-session", created_at: "2026-10-14T00:00:00Z" },
        ],
        blocks: [],
        audit: [],
        active_subscriptions_total: 1,
        active_agreements_total: 0,
        can_moderate: true,
        moderation: { current_warnings: 1, unlink_blocked: false },
        referral: { balance: "10.00", earned_total: "20.00", invited_total: 1 },
        pagination: { totals: { orders: 30, payments: 30, sessions: 30 } },
      };
    if (path === "/admin/operations/health")
      json = {
        maintenance: { enabled: false },
        subscription_retention: { enabled: false },
        database: { state: "healthy" },
        redis: { state: "healthy" },
        backup: { state: "healthy" },
        queue: { items: [] },
        issues: { items: [] },
      };
    if (path === "/admin/subscriptions")
      json = {
        items: [
          {
            id: "synthetic-subscription",
            account_id: "synthetic-user",
            name: "Длинное название услуги для проверки горизонтальной таблицы подписок",
            status: "active",
            desired_panel_status: "ACTIVE",
            provisioning: { state: "done" },
            expires_at: "2026-11-14T00:00:00Z",
            devices_limit: 3,
          },
        ],
        total: 30,
      };
    if (path === "/admin/legal")
      json = {
        configured: true,
        revision: "v1",
        documents: [
          {
            document_type: "offer",
            version: "v1",
            url: "https://example.test/offer",
          },
          {
            document_type: "privacy",
            version: "v1",
            url: "https://example.test/privacy",
          },
        ],
      };
    if (path === "/admin/legal/missing")
      json = {
        items: [
          {
            id: "synthetic-user",
            email: "very-long-synthetic-address-for-layout@example.test",
            missing: ["offer", "privacy"],
          },
        ],
        total: 30,
        page_size: 25,
      };
    if (path === "/admin/security/logins")
      json = {
        items: Array.from({ length: 10 }, (_, index) => ({
          id: `synthetic-login-${index}`,
          email: "very-long-synthetic-address-for-layout@example.test",
          account_id: "synthetic-user",
          result: "success",
          reason: "verified_code",
          created_at: "2026-10-14T00:00:00Z",
          ip: "192.0.2.10",
          ip_provenance: "direct",
        })),
        total: 30,
      };
    if (path === "/admin/broadcasts")
      json = {
        items: [
          {
            id: "synthetic-report",
            state: "done",
            attempts: 0,
            recipient_count: 3,
          },
        ],
        total: 1,
      };
    if (path === "/admin/broadcasts/synthetic-report")
      json = {
        state: "done",
        recipient_count: 3,
        delivered_count: 1,
        blocked_count: 1,
        provider_rejected_count: 0,
        unknown_count: 1,
        waiting_count: 0,
        cancelled_count: 0,
      };
    if (path === "/admin/panel-event-routes")
      json = {
        configured: false,
        approved_node_uuids: [],
        approved_product_ids: [],
        registration_context: null,
      };
    if (path === "/admin/telegram-theme")
      json = {
        styles: {
          primary: "primary",
          success: "success",
          danger: "danger",
          neutral: "neutral",
          admin: "primary",
        },
        icons: {
          connect: { fallback: "★", custom_emoji_id: null },
          help: { fallback: "?", custom_emoji_id: null },
        },
      };
    if (path === "/admin/public-content")
      json = { faq: [], how_to_connect: [] };
    await route.fulfill({ json });
  });
  return writes;
}

async function separatedRow(page: Page, selector: string) {
  const rows = await page.locator(selector).evaluateAll((elements) =>
    elements.map((row) => ({
      boxes: [...row.children].map((item) => {
        const r = item.getBoundingClientRect();
        return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
      }),
    })),
  );
  expect(rows.length).toBeGreaterThan(0);
  for (const row of rows) {
    for (let i = 0; i < row.boxes.length; i++)
      for (let j = i + 1; j < row.boxes.length; j++) {
        const a = row.boxes[i],
          b = row.boxes[j];
        const horizontalGap =
          Math.max(a.left, b.left) - Math.min(a.right, b.right);
        const verticalGap =
          Math.max(a.top, b.top) - Math.min(a.bottom, b.bottom);
        expect(
          Math.max(horizontalGap, verticalGap),
          JSON.stringify({ a, b, horizontalGap, verticalGap }),
        ).toBeGreaterThanOrEqual(8);
      }
  }
}

for (const width of [390, 1280]) {
  test(`user detail groups, actions and history remain separated at ${width}px`, async ({
    page,
  }) => {
    const writes = await setup(page, "users");
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    const table = page.locator(".table-scroll").first();
    await expect(table.locator("thead")).toBeVisible();
    await table.scrollIntoViewIfNeeded();
    const headerColor = await table
      .locator("thead")
      .evaluate((node) => getComputedStyle(node).backgroundColor);
    expect(headerColor).not.toBe("rgba(0, 0, 0, 0)");
    await table.focus();
    await expect(table).toBeFocused();
    await table.evaluate((node) => {
      node.scrollTo({ left: node.scrollWidth, behavior: "instant" });
    });
    await table.locator("th").last().scrollIntoViewIfNeeded();
    await expect(table.locator("th").last()).toBeInViewport();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    expect(
      await table
        .locator("th")
        .last()
        .evaluate((node) => getComputedStyle(node).backgroundColor),
    ).toBe(headerColor);
    await table.evaluate((node) => {
      node.scrollLeft = 0;
    });
    await page.locator(".table-actions .button").first().click();
    const dialog = page.getByRole("dialog");
    await expect(
      dialog.getByText("Отвязка устройств", { exact: true }),
    ).toBeVisible();
    await separatedRow(page, ".user-summary .button-row");
    const identities = dialog.locator("summary", { hasText: "Способы входа" });
    await expect(identities).toBeVisible();
    await identities.focus();
    await page.keyboard.press("Enter");
    await expect(
      dialog.getByText("9007199254740993123", { exact: true }),
    ).toBeVisible();
    const services = dialog
      .locator("summary")
      .filter({ hasText: /^Подписки$/ });
    await services.click();
    await expect(
      dialog.getByText("Длинное название выданной услуги", { exact: false }),
    ).toBeVisible();
    await separatedRow(page, ".admin-detail-pagination");
    await dialog
      .locator("summary")
      .filter({ hasText: /^Устройства$/ })
      .click();
    await expect(
      dialog.getByText(
        "Синтетическое устройство с длинным названием для переноса",
        { exact: true },
      ),
    ).toBeVisible();
    expect(
      await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth),
    ).toBe(true);
    await mkdir(".artifacts/ui20", { recursive: true });
    await page.screenshot({
      path: `.artifacts/ui20/user-detail-${width}.png`,
      fullPage: true,
    });
    expect(writes).toEqual([]);
  });

  test(`promo checkbox and operational/theme sections use compact accessible layout at ${width}px`, async ({
    page,
  }) => {
    const writes = await setup(page, "promos");
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    await page
      .getByRole("button", { name: "Добавить промокод", exact: true })
      .click();
    const checkbox = page
      .getByRole("dialog")
      .locator('input[type="checkbox"]')
      .first();
    const size = await checkbox.evaluate((node) => {
      const s = getComputedStyle(node);
      return {
        padding: s.padding,
        height: node.getBoundingClientRect().height,
      };
    });
    expect(size.padding).toBe("0px");
    expect(size.height).toBeLessThanOrEqual(20);
    const captionAligned = await checkbox.evaluate((node) => {
      const text = [...node.parentElement!.childNodes].find(
        (child) =>
          child.nodeType === Node.TEXT_NODE && child.textContent?.trim(),
      );
      if (!text) return false;
      const range = document.createRange();
      range.selectNodeContents(text);
      const caption = range.getBoundingClientRect(),
        control = node.getBoundingClientRect();
      return (
        Math.abs(
          caption.top + caption.height / 2 - control.top - control.height / 2,
        ) <= 4
      );
    });
    expect(captionAligned).toBe(true);
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Закрыть" })
      .click();
    await page.evaluate(() => {
      location.hash = "operations";
    });
    const controls = page.locator("summary", { hasText: "Состояние операций" });
    await expect(controls).toBeVisible();
    await controls.click();
    await expect(
      page.getByRole("button", { name: "Включить обслуживание", exact: true }),
    ).toBeVisible();
    await page.evaluate(() => {
      location.hash = "telegram";
    });
    const styles = page.locator("summary", { hasText: "Оформление кнопок" });
    await expect(styles).toBeVisible();
    await styles.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(".v6-form select").first()).toBeVisible();
    const icons = page.locator("summary", { hasText: "Значки меню" });
    await icons.click();
    const fallback = page.getByLabel("Запасной символ: connect", {
      exact: true,
    });
    await fallback.fill("");
    await icons.click();
    await page
      .getByRole("button", { name: "Сохранить тему", exact: true })
      .click();
    await expect(fallback).toBeVisible();
    await expect(fallback).toBeFocused();
    await fallback.fill("★");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `.artifacts/ui20/telegram-${width}.png`,
      fullPage: true,
    });
    expect(writes).toEqual([]);
  });
}

for (const width of [390, 1280]) {
  test(`subscription headers, legal labels, login controls and recipient report fit at ${width}px`, async ({
    page,
  }) => {
    const writes = await setup(page, "subscriptions");
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    const subscriptions = page.locator(".table-scroll").first();
    await subscriptions.scrollIntoViewIfNeeded();
    await subscriptions.evaluate((node) =>
      node.scrollTo({ left: node.scrollWidth, behavior: "instant" }),
    );
    const lastHeader = subscriptions.locator("th").last();
    await lastHeader.scrollIntoViewIfNeeded();
    await expect(lastHeader).toBeInViewport();
    expect(
      await lastHeader.evaluate(
        (node) => getComputedStyle(node).backgroundColor,
      ),
    ).not.toBe("rgba(0, 0, 0, 0)");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await mkdir(".artifacts/ui20", { recursive: true });
    await page.screenshot({
      path: `.artifacts/ui20/subscriptions-${width}.png`,
      fullPage: true,
    });
    await page.evaluate(() => {
      location.hash = "legal";
    });
    const legal = page.locator(".table-scroll");
    await expect(
      legal.getByText("Политика конфиденциальности", { exact: true }),
    ).toBeVisible();
    const itemsFit = await legal.locator("td li").evaluateAll((items) =>
      items.every((item) => {
        const a = item.getBoundingClientRect(),
          b = item.closest("td")!.getBoundingClientRect();
        return a.left >= b.left && a.right <= b.right;
      }),
    );
    expect(itemsFit).toBe(true);
    await page.screenshot({
      path: `.artifacts/ui20/legal-${width}.png`,
      fullPage: true,
    });
    await page.evaluate(() => {
      location.hash = "security";
    });
    await page
      .getByLabel("Поиск в истории входов", { exact: true })
      .fill("user@example.test");
    const searched = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        url.pathname.endsWith("/admin/security/logins") &&
        url.searchParams.get("search") === "user@example.test"
      );
    });
    await page
      .getByRole("button", { name: "Найти входы", exact: true })
      .click();
    await (await searched).finished();
    const history = page.locator(".login-history");
    await expect(history.locator("table tbody tr")).toHaveCount(10);
    await separatedRow(page, ".login-history .admin-pagination");
    await history
      .getByRole("button", { name: "Далее по истории", exact: true })
      .click();
    await expect(history.locator(".admin-pagination")).toContainText(
      /страница 2/i,
    );
    await page.screenshot({
      path: `.artifacts/ui20/login-history-${width}.png`,
      fullPage: true,
    });
    await page.evaluate(() => {
      location.hash = "broadcasts";
    });
    await expect(
      page.getByText("Все пользователи с действующим согласием.", {
        exact: false,
      }),
    ).toBeVisible();
    await page
      .getByLabel("Текст сообщения для предпросмотра")
      .fill("Только визуальная синтетическая проверка без отправки");
    await page.locator(".table-actions .button").first().click();
    const report = page.getByRole("dialog");
    await expect(
      report.getByText("Заблокировано правилами или согласием", {
        exact: true,
      }),
    ).toBeVisible();
    expect(
      await report.evaluate((node) => node.scrollWidth <= node.clientWidth),
    ).toBe(true);
    await page.screenshot({
      path: `.artifacts/ui20/recipient-report-${width}.png`,
      fullPage: true,
    });
    expect(writes).toEqual([]);
  });
}

test("backup navigation is visible to the owner and absent for ordinary staff", async ({
  page,
}) => {
  const writes = await setup(page, "users", false);
  await page.goto("/");
  await expect(page.locator('.sidebar nav a[href="#users"]')).toHaveCount(1);
  await expect(page.locator('.sidebar nav a[href="#backups"]')).toHaveCount(0);
  expect(writes).toEqual([]);
});

test("attachment chooser and overview dates have branded keyboard-accessible controls", async ({
  page,
}) => {
  const writes = await setup(page, "broadcasts");
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "Выбрать файл", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Фото или видео")).toHaveAttribute(
    "accept",
    "image/jpeg,image/png,video/mp4",
  );
  await page.evaluate(() => {
    location.hash = "overview";
  });
  const dates = page.locator(".admin-date-field > .admin-date-control > input");
  await expect(dates).toHaveCount(2);
  await dates.first().fill("2026-10-14");
  await expect(dates.first()).toHaveValue("2026-10-14");
  await dates.first().focus();
  await expect(dates.first()).toBeFocused();
  const calendar = page
    .getByRole("button", { name: /Открыть календарь/ })
    .first();
  await calendar.click();
  const popup = page.getByRole("dialog", { name: /Календарь/ });
  await expect(popup).toBeVisible();
  await popup
    .getByRole("button", { name: "15 октября 2026", exact: true })
    .click();
  await expect(dates.first()).toHaveValue("2026-10-15");
  await calendar.click();
  await page.keyboard.press("Escape");
  await expect(popup).toHaveCount(0);
  await expect(calendar).toBeFocused();
  expect(writes).toEqual([]);
});

test("malformed manual date opens the calendar without breaking the page", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const writes = await setup(page, "overview");
  await page.goto("/");
  const dates = page.locator(".admin-date-control > input");
  await dates.first().fill('2026"10[');
  await page
    .getByRole("button", { name: /Открыть календарь/ })
    .first()
    .click();
  const popup = page.getByRole("dialog", { name: /Календарь/ });
  await expect(popup).toBeVisible();
  await expect(popup.locator("button[data-day]:enabled").first()).toBeFocused();
  expect(errors).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(dates).toHaveCount(2);
  expect(writes).toEqual([]);
});
